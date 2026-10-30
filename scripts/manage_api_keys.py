"""管理 API 密钥：创建、列出、吊销。

    .venv/bin/python scripts/manage_api_keys.py create --name alice --role operator --expires-in-days 90
    .venv/bin/python scripts/manage_api_keys.py list
    .venv/bin/python scripts/manage_api_keys.py revoke <key_id>

【为什么是命令行脚本，而不是 HTTP 接口】
能创建密钥的接口，本身就是最值钱的攻击目标。做成只有能连上数据库的人才能运行的脚本，
就从根上去掉了"远程创建管理员密钥"这条攻击路径（减少攻击面）。
Java 类比：像一个只在运维机上跑的管理命令，而不是挂在 Controller 上的接口。

完整密钥只在 create 时打印这一次；数据库里只有哈希，之后任何人（包括我们）都无法再看到它。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.auth import ROLE_SCOPES, create_key, list_keys, revoke_key  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import create_pool, init_schema  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    pool = create_pool(Settings().database_url, max_size=2)
    try:
        await pool.open(wait=True, timeout=5)
    except Exception:
        print("Cannot connect to the database. Is it running (docker compose up -d)?")
        return 1
    try:
        await init_schema(pool)  # 第一次使用时表可能还不存在

        if args.command == "create":
            try:
                key, key_id = await create_key(pool, args.name, args.role, args.expires_in_days)
            except psycopg.errors.UniqueViolation:
                print(f"An active key named {args.name!r} already exists. Revoke it first, or pick another name.")
                return 1
            print(f"Created key {key_id} for {args.name!r} (role: {args.role}).")
            print("Copy it now. It is shown only once and cannot be recovered:\n")
            print(f"    {key}\n")
        elif args.command == "list":
            rows = await list_keys(pool)
            if not rows:
                print("No API keys yet. Create one with the 'create' command.")
            for r in rows:
                state = "revoked" if r["revoked_at"] else "active"
                print(f"{r['key_id']}  {r['name']:<20} {r['role']:<9} {state:<8} last used: {r['last_used_at'] or 'never'}")
        else:  # revoke
            if await revoke_key(pool, args.key_id):
                print(f"Revoked {args.key_id}. It stops working immediately.")
            else:
                print(f"No active key with id {args.key_id!r}.")
                return 1
        return 0
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage API keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a key (shown once)")
    create.add_argument("--name", required=True, help="who or what this key is for")
    create.add_argument("--role", choices=sorted(ROLE_SCOPES), default="operator")
    create.add_argument("--expires-in-days", type=float, default=None)
    sub.add_parser("list", help="list keys (never shows secrets)")
    revoke = sub.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("key_id")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
