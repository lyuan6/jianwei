"""Check every data source from this machine using the app's own fetch code. Standard library only.

    python3 -m scripts.check_sources

Optional keys are read from the environment or .env: EXA_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY.
"""
import os
import platform
import time

import src.events as events
from src.config import ROOT, Settings, load_env
from src.events import (EventError, fetch_announcement_content, fetch_announcements, fetch_financials,
                        fetch_forecasts, fetch_holder_changes, fetch_unlocks)
from src.market import fetch_history, fetch_quotes, now, search_symbols
from src.model_client import complete_json
from src.news import search_news


def check(name, call, times=1):
    ok, detail, errors, elapsed = 0, "", [], []
    for _ in range(times):
        started = time.monotonic()
        try:
            detail = call()
            ok += 1
        except Exception as exc:  # diagnostic: show every failure type
            errors.append(f"{type(exc).__name__}: {exc}")
        elapsed.append(time.monotonic() - started)
    status = "OK  " if ok == times else "FAIL" if not ok else "PART"
    print(f"{status} {name}  {ok}/{times}  平均 {sum(elapsed) / times:.2f} 秒  {detail}")
    for error in dict.fromkeys(errors):
        print(f"     {error}")


def announcement_text():
    for item in fetch_announcements("sz300750", days=30)[:5]:
        try:
            return f"{len(fetch_announcement_content(item['art_code'])['text'])} 字正文"
        except EventError as exc:
            if "没有可读取的正文" not in str(exc):  # PDF-only announcements are fine, transport errors are not
                raise
    return "近期公告只有 PDF"


def exa(key):
    result = search_news("宁德时代 三季报", key, num=1)
    return f"{len(result['results'])} 条 · {result['cost_usd']} 美元"


def model():
    settings = Settings(mode="api", base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
                        model=os.getenv("LLM_MODEL", ""), api_key=os.getenv("LLM_API_KEY", ""))
    result = complete_json([{"role": "system", "content": '只返回 JSON {"ok":true}。'},
                            {"role": "user", "content": "连接测试"}], settings, max_tokens=40)
    return "连接成功" if result.get("ok") is True else f"返回 {result}"


def main():
    load_env(ROOT / ".env")
    print(f"Python {platform.python_version()} · {now():%Y-%m-%d %H:%M} 北京时间\n")
    check("腾讯报价", lambda: f"{len(fetch_quotes(('sh000001', 'sz300750')))} 项", 2)
    check("腾讯日线", lambda: f"{len(fetch_history('sz300750')['bars'])} 条")
    check("腾讯搜索", lambda: f"{len(search_symbols('宁德'))} 个候选")
    raw = events._get_json
    events._get_json = lambda url: raw(url, attempts=1)  # retry off: measures how often reads are cut off
    check("东财公告（不重试）", lambda: f"{len(fetch_announcements('sz300750', days=30))} 条", 5)
    events._get_json = raw
    check("东财公告", lambda: f"{len(fetch_announcements('sz300750', days=30))} 条", 5)
    check("东财公告正文", announcement_text)
    check("东财财报", lambda: "{period} 营收 {revenue_yi} 亿元".format(**fetch_financials("sz300750", 1)[0]), 3)
    check("东财解禁", lambda: f"{len(fetch_unlocks('sz002079', past_days=30, future_days=30))} 条")
    check("东财增减持", lambda: f"{len(fetch_holder_changes('sh601212', days=60))} 条")
    check("东财业绩预告", lambda: f"{len(fetch_forecasts('sh600519', days=800))} 条")
    key = os.getenv("EXA_API_KEY", "").strip()
    if key:
        check("Exa 搜索", lambda: exa(key))
    else:
        print("SKIP Exa 搜索  未配置 EXA_API_KEY")
    if all(os.getenv(k) for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")):
        check("模型接口", model)
    else:
        print("SKIP 模型接口  未配置 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY")


if __name__ == "__main__":
    main()
