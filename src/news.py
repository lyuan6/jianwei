"""Exa web search for news coverage and context. Numbers in answers still come from src/events.py."""
import http.client
import json
import time
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlparse

from src import market

SEARCH_URL = "https://api.exa.ai/search"
KNOWN_FIELDS = {"results", "costDollars", "requestId", "resolvedSearchType", "searchType"}


class NewsError(ValueError):
    pass


def parse_results(data):
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"]
        if not url.startswith(("https://", "http://")):
            continue
        results.append({"title": str(item.get("title") or "")[:200], "url": url[:2000],
                        "domain": urlparse(url).hostname or "", "date": str(item.get("publishedDate") or "")[:10],
                        "text": " ".join(str(item.get("text") or "").split())})
    return results


def search_news(query, api_key, *, search_type="auto", days=30, num=5, max_chars=300, timeout=120, since=None, until=None):
    """since（YYYY-MM-DD）给出时代替 days，用于按事项发生的时间段检索；until 默认到今天。"""
    if not api_key:
        raise NewsError("未配置 EXA_API_KEY。")
    today = market.now()
    start = since or (today - timedelta(days=days)).strftime("%Y-%m-%d")
    body = {"query": query, "type": search_type, "numResults": num,
            "startPublishedDate": start + "T00:00:00.000Z",
            "endPublishedDate": (until or today.strftime("%Y-%m-%d")) + "T23:59:59.999Z",
            "contents": {"text": {"maxCharacters": max_chars}}}
    request = urllib.request.Request(SEARCH_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"x-api-key": api_key, "Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(5_000_001))
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace").replace(api_key, "[KEY]")
        raise NewsError(f"Exa 返回 HTTP {exc.code}：{detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise NewsError("无法连接 Exa 搜索服务。") from None
    except ValueError:
        raise NewsError("Exa 返回的不是有效 JSON。") from None
    if not isinstance(data, dict):
        raise NewsError("Exa 返回结构无效。")
    cost = data.get("costDollars")
    cost = cost.get("total") if isinstance(cost, dict) else None
    return {"results": parse_results(data), "cost_usd": cost if isinstance(cost, (int, float)) else None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "resolved_type": data.get("resolvedSearchType"),
            # Unknown top-level fields are kept for comparing search types (deep may add a synthesized answer).
            "other": {k: str(v)[:300] for k, v in data.items() if k not in KNOWN_FIELDS}}
