"""守卫测试：代码里用到的每个第三方包，都必须能从 requirements.txt 声明的依赖里得到。

为什么需要它？我在开发时手动 `pip install` 了 mcp 和 pyyaml，却没写进 requirements.txt。
在我自己的虚拟环境里一切正常，别人克隆仓库、按 requirements.txt 安装之后，却会在导入时直接失败。
这类问题在"我这里能跑"的环境里永远发现不了，所以做成自动检查：每次跑测试都核对一遍。

做法：用 ast 解析所有 Python 文件里的 import，找出对应的安装包（distribution），
再检查它是否出现在 requirements.txt 声明的包及其【传递依赖】之中。
（pydantic 没有直接写进 requirements.txt，但它是 fastapi 的依赖，所以算声明过；而 mcp 不是任何已声明包的依赖。）
"""

import ast
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse(requirement: str) -> tuple[str, set[str], str | None]:
    """解析一条依赖声明，返回 (包名, 要求安装的 extras, 这条依赖自身所属的 extra)。

    例如 "psycopg[binary,pool]>=3.2"  -> ("psycopg", {"binary", "pool"}, None)
         "psycopg-pool>=3.2; extra == 'pool'" -> ("psycopg-pool", set(), "pool")  # 只有请求了 pool 这个 extra 才会装
    """
    spec, _, marker = requirement.partition(";")
    name = _normalize(re.split(r"[\[<>=!~ (]", spec.strip(), maxsplit=1)[0])
    extras_match = re.search(r"\[([^\]]*)\]", spec)
    extras = {e.strip() for e in extras_match.group(1).split(",")} if extras_match else set()
    own_extra = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
    return name, extras, own_extra.group(1) if own_extra else None


def declared_closure() -> set[str]:
    """requirements.txt 里的包，加上它们的传递依赖（用已安装包的元数据逐层展开）。

    要处理 extras：`psycopg[binary,pool]` 明确要求安装 pool 这个可选依赖组，
    所以带有 `extra == "pool"` 标记的依赖（psycopg-pool）也算声明过；没被要求的 extra 则不算。
    """
    pending: list[tuple[str, set[str]]] = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            name, extras, _ = _parse(line)
            pending.append((name, extras))

    seen: set[tuple[str, frozenset[str]]] = set()
    names: set[str] = set()
    while pending:
        name, extras = pending.pop()
        key = (name, frozenset(extras))
        if key in seen:
            continue
        seen.add(key)
        names.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for requirement in requires:
            dep_name, dep_extras, own_extra = _parse(requirement)
            if own_extra is not None and own_extra not in extras:
                continue  # 属于一个没被要求安装的可选依赖组
            pending.append((dep_name, dep_extras))
    return names


def third_party_imports() -> dict[str, set[str]]:
    stdlib = set(sys.stdlib_module_names)
    used: dict[str, set[str]] = {}
    for folder in ("app", "scripts", "tests"):
        for file in (ROOT / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(file.read_text())):
                if isinstance(node, ast.Import):
                    modules = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module.split(".")[0]]
                else:
                    continue
                for module in modules:
                    if module not in stdlib and module not in ("app", "tests", "scripts"):
                        used.setdefault(module, set()).add(str(file.relative_to(ROOT)))
    return used


def test_every_third_party_import_is_covered_by_requirements_txt():
    closure = declared_closure()
    owners = metadata.packages_distributions()  # 顶层模块名 -> 提供它的安装包（可能有多个，比如 google 命名空间）

    missing = {}
    for module, files in third_party_imports().items():
        distributions = {_normalize(d) for d in owners.get(module, [])}
        if not distributions & closure:
            missing[module] = sorted(files)[:2]

    assert missing == {}, (
        f"imported but not covered by requirements.txt: {missing}. "
        "Add the package to requirements.txt (someone who clones the repo will not have it otherwise)."
    )
