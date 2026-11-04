"""v3 提示词（XML 标签 + 一个示例）的测试：它是实验配置，不是默认。

两个提示词工程技巧：用 XML 标签分段、给一个示例（few-shot）。要保证的是：
  1. 内容没有悄悄变：v3 = v2 的全部内容 + 标签 + 示例（所以对照实验里变的只有"写法"）；
  2. 标签是成对的、顺序对；
  3. 示例是通用的，没有点名评估场景里的服务、关键词，也没有暗示留出集的题型（和 v2 的守卫测试同一套标准）。
"""

import json

from app.agent.common import AGENT_VARIANTS, SYSTEM_PROMPT, SYSTEM_PROMPT_V2, SYSTEM_PROMPT_V3
from app.config import Settings
from app.evals.agent_eval import run_case
from app.evals.baselines import BASELINES
from app.scenario import SCENARIO_DIR, load_scenario

MANIFEST = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))
V2_ADDENDUM = SYSTEM_PROMPT_V2[len(SYSTEM_PROMPT):].strip()


def only_what_v3_adds() -> str:
    return SYSTEM_PROMPT_V3.replace(SYSTEM_PROMPT, "").replace(V2_ADDENDUM, "")


def test_v3_contains_all_of_v2_and_only_adds_tags_and_an_example():
    assert SYSTEM_PROMPT in SYSTEM_PROMPT_V3 and V2_ADDENDUM in SYSTEM_PROMPT_V3
    added = only_what_v3_adds()
    assert "<example>" in added and "list_services" in added


def test_the_tags_are_paired_and_in_order():
    positions = [SYSTEM_PROMPT_V3.index(t) for t in ("<instructions>", "</instructions>", "<method>", "</method>", "<example>", "</example>")]
    assert positions == sorted(positions)
    assert all(SYSTEM_PROMPT_V3.count(t) == 1 for t in ("<instructions>", "<method>", "<example>"))


def test_the_example_does_not_name_evaluation_services_or_hint_at_held_out_types():
    added = only_what_v3_adds().lower()
    forbidden, held_out = set(), set()
    for entry in MANIFEST:
        s = load_scenario(entry["scenario"])
        forbidden |= {l.service for l in s.logs} | {d.service for d in s.deploys} | {s.ticket.service}
        forbidden |= {a.split("|")[0] for g in s.ground_truth.must_have for a in g.split("|")}
        if entry["split"] == "holdout":
            held_out |= {alt.strip().lower() for alt in s.ground_truth.keywords[0].split("|")}
            held_out |= {alt.strip().lower() for g in s.ground_truth.must_have for alt in g.split("|")}
    held_out = {t for t in held_out if len(t) >= 3 and not t[0].isdigit()}
    leaked = sorted({n for n in forbidden if len(n) > 4 and n.lower() in added} | {t for t in held_out if t in added})
    assert held_out and leaked == []


def test_v3_is_registered_with_service_discovery_and_is_not_the_default():
    assert AGENT_VARIANTS["v3"].prompt == SYSTEM_PROMPT_V3 and AGENT_VARIANTS["v3"].service_discovery is True
    assert Settings.model_fields["agent_variant"].default == "v2"  # 测试环境把它固定成 v1，所以看字段本身的默认值
    assert Settings(_env_file=None, agent_variant="v3").agent_variant == "v3"


async def test_an_evaluation_run_can_use_v3_and_records_it():
    entry = MANIFEST[0]
    result = await run_case(entry, BASELINES["oracle"](load_scenario(entry["scenario"])), variant="v3")
    assert result.variant == "v3" and result.status == "ok"
