"""为所有启用账号按持仓去重生成晨报：python -m scripts.daily_brief [2026-10-27 ...]。"""

import argparse
from contextlib import nullcontext
from datetime import date, datetime
from unittest.mock import patch

from src import chain, industry, macro, market
from src.accounts import default_store
from src.agent import OutputError
from src.brief import get_or_generate
from src.config import Settings, load_env
from src.model_client import ModelError
from src.storage import Storage, portfolio_key


def generate_daily(store, settings):
    day, seen = market.now().date().isoformat(), set()
    generated = skipped = failed = 0
    for username, disabled, *_ in store.list():
        if disabled:
            continue
        codes = [holding["code"] for holding in store.holdings(username)]
        key = portfolio_key(codes)
        if not codes or key in seen:
            continue
        seen.add(key)
        if store.brief(codes, day) is not None:
            skipped += 1
            continue
        progress = []
        def record(steps):
            progress[:] = steps
        try:
            get_or_generate(codes, settings, store, day, on_step=record)
            generated += 1
        except Exception as exc:
            failed += 1
            reason = str(exc) if isinstance(exc, (ModelError, OutputError)) else "内部处理失败。"
            last_code = progress[-1]["code"] if progress else "尚无底稿"
            print(f"持仓 {key} 生成失败（最近底稿 {last_code}，已记录 {len(progress)} 步）：{reason}")
    return {"generated": generated, "skipped": skipped, "failed": failed}


def update_chains(store, settings):
    """晨报之后给已有上下游的股票做增量更新；单只失败只打印，不影响晨报结果。"""
    added = 0
    for code in store.chain_codes():
        try:
            added += chain.update(code, settings, store)["added"]
        except ValueError as exc:
            print(f"{code} 上下游未更新：{exc}")
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成当天晨报；给出日期时按那天 08:30 回放，数据只取到那天为止")
    parser.add_argument("dates", nargs="*", type=date.fromisoformat)
    args = parser.parse_args(argv)
    load_env()
    store, settings, failed = Storage(default_store().path), Settings.from_env(), 0
    for day in args.dates or [None]:
        clock = nullcontext() if day is None else patch(
            "src.market.now", return_value=datetime(day.year, day.month, day.day, 8, 30, tzinfo=market.TZ))
        with clock:
            result = generate_daily(store, settings)
        print(f"{day or '今天'}：生成 {result['generated']} 份，已有 {result['skipped']} 份，失败 {result['failed']} 份。")
        failed += result["failed"]
    # 回放过去的日期时不更新上下游，检索只能取到今天的结果。
    if not args.dates:
        print(f"上下游新增 {update_chains(store, settings)} 条。")
        print(f"行业重点新闻新增 {industry.collect_news(store, settings)} 条。")
        print(f"宏观事件新增 {macro.collect(store, settings)} 条。")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
