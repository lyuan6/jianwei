"""Manage reviewer accounts. Generated passwords are printed once and never stored in plain text.

    python -m scripts.accounts add reviewer1 reviewer2 reviewer3 reviewer4 reviewer5 team
    python -m scripts.accounts reset reviewer1
    python -m scripts.accounts disable reviewer1
    python -m scripts.accounts enable reviewer1
    python -m scripts.accounts list
"""
import argparse
import sys
from datetime import datetime

from src.accounts import AccountError, default_store
from src.config import load_env


def main():
    parser = argparse.ArgumentParser(description="见微账号管理")
    parser.add_argument("command", choices=["add", "reset", "disable", "enable", "list"])
    parser.add_argument("usernames", nargs="*")
    args = parser.parse_args()
    load_env()
    store = default_store()
    if args.command == "list":
        for name, disabled, created, sessions in store.list():
            print(f"{name:<16} {'停用' if disabled else '正常'}  创建 {datetime.fromtimestamp(created):%Y-%m-%d}  有效会话 {sessions}")
        return
    if not args.usernames:
        parser.error("请给出至少一个用户名。")
    try:
        for name in args.usernames:
            if args.command in ("add", "reset"):
                password = store.add(name) if args.command == "add" else store.reset(name)
                print(f"{name}  {password}")
            else:
                store.set_disabled(name, args.command == "disable")
                print(f"{name}  已{'停用' if args.command == 'disable' else '启用'}")
    except AccountError as exc:
        sys.exit(str(exc))
    if args.command in ("add", "reset"):
        print("\n密码只显示这一次，请现在保存。")


if __name__ == "__main__":
    main()
