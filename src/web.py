"""FastAPI server for the 见微 web frontend: login, JSON API, static files from web/.

    uvicorn src.web:create_app --factory --host 127.0.0.1 --port 8600
"""
import os
import time
from threading import Lock
from contextlib import asynccontextmanager
from datetime import date as calendar_date, timedelta

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.accounts import SESSION_SECONDS, Throttled, default_store
from src.config import ROOT, Settings, load_env
from src.market import MarketError, fetch_quotes, resolve_symbol, now, fetch_history, search_symbols, is_index
from src import commitments, events, industry
from src.timeline import candidates as timeline_candidates, short_title
from src.jobs import JobManager
from src.storage import Storage, QuotaExceeded, DEFAULT_HOLDINGS

COOKIE = "jw_session"
QUOTE_TTL = 60
BRIEF_LIMIT = 5
RESEARCH_LIMIT = 30
TIMELINE_LIMIT = 10
LINK_LIMIT = 20
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class HoldingInput(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    qty: str | None = Field(default=None, max_length=40)
    cost: str | None = Field(default=None, max_length=40)


class ResearchInput(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class NodeInput(BaseModel):
    art_code: str = Field(min_length=1, max_length=80)
    thread_id: int | None = None
    title: str | None = Field(default=None, max_length=40)


class LinkInput(BaseModel):
    node_a: int
    node_b: int


def client_ip(request):
    # Only cloudflared reaches the server, so its header is the visitor address.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")


def create_app(store=None, quote_loader=fetch_quotes, *, settings=None, generator=None, researcher=None, initializer=None, linker=None):
    load_env()
    store = store or default_store()
    data = Storage(store.path)
    settings = settings or Settings.from_env()
    job_options = {}
    if generator is not None:
        job_options["generator"] = generator
    if researcher is not None:
        job_options["researcher"] = researcher
    if initializer is not None:
        job_options["initializer"] = initializer
    if linker is not None:
        job_options["linker"] = linker
    jobs = JobManager(data, settings, **job_options)

    @asynccontextmanager
    async def lifespan(app):
        data.recover_jobs()
        yield
        jobs.close()

    secure = os.getenv("COOKIE_SECURE", "1") != "0"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.data = data
    app.state.jobs = jobs
    cache = {}
    stock_cache = {}
    history_cache = {}
    observations, observation_lock = {}, Lock()

    def observe(source, function, *args, **kwargs):
        started = time.monotonic()
        state = "error"
        try:
            result = function(*args, **kwargs)
            state = "ok"
            return result
        finally:
            with observation_lock:
                observations[source] = {"state": state, "checked_at": int(time.time()),
                                        "elapsed_seconds": round(time.monotonic() - started, 3)}

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            # Cloudflare 默认让浏览器缓存 js/css 4 小时，部署后会拿旧脚本配新页面；改为每次用 ETag 重新验证。
            response.headers["Cache-Control"] = "no-cache"
        return response

    def stock_symbol(code):
        try:
            symbol = resolve_symbol(code)
            if is_index(symbol):
                raise ValueError("请选择个股。")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return symbol

    def limit(name, value):
        return None if name == "team" else value

    def user(token):
        name = store.session_user(token)
        if not name:
            raise HTTPException(401, "请先登录。")
        return name

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"detail": "请求参数格式不正确。"}, status_code=422)

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        try:
            token = store.authenticate(body.username.strip().lower(), body.password, client_ip(request))
        except Throttled:
            raise HTTPException(429, "尝试次数过多，请 15 分钟后再试。") from None
        if not token:
            raise HTTPException(401, "用户名或密码不正确。")
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure, samesite="lax")
        return {"user": body.username.strip().lower()}

    @app.post("/api/logout")
    def logout(response: Response, jw_session: str = Cookie(default="")):
        user(jw_session)
        store.logout(jw_session)
        response.delete_cookie(COOKIE, httponly=True, secure=secure, samesite="lax")
        return {"ok": True}

    @app.get("/api/me")
    def me(jw_session: str = Cookie(default="")):
        return {"user": user(jw_session), "app_name": os.getenv("APP_NAME", "见微")}

    @app.get("/api/quotes")
    def quotes(symbols: str, jw_session: str = Cookie(default="")):
        user(jw_session)
        try:
            codes = tuple(sorted(dict.fromkeys(resolve_symbol(s) for s in symbols[:400].split(",") if s.strip())))
        except MarketError as exc:
            raise HTTPException(400, str(exc)) from None
        if not 1 <= len(codes) <= 12:
            raise HTTPException(400, "每次查询 1 到 12 个代码。")
        hit = cache.get(codes)
        if hit and time.monotonic() - hit[0] < QUOTE_TTL:
            return hit[1]
        try:
            loaded = observe("腾讯行情", quote_loader, codes)
        except MarketError as exc:
            raise HTTPException(502, str(exc)) from None
        result = {"quotes": {code: {"name": q.name, "price": q.price, "change_pct": q.change_pct, "as_of": q.as_of}
                             for code, q in loaded.items()}}
        if len(cache) > 256:
            cache.clear()
        cache[codes] = (time.monotonic(), result)
        return result

    @app.get("/api/holdings")
    def holdings(jw_session: str = Cookie(default="")):
        return {"holdings": data.holdings(user(jw_session))}

    @app.put("/api/holdings")
    def replace_holdings(body: list[HoldingInput], jw_session: str = Cookie(default="")):
        name = user(jw_session)
        try:
            data.set_holdings(name, [item.model_dump() for item in body])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"holdings": data.holdings(name)}

    @app.post("/api/holdings/reset")
    def reset_holdings(jw_session: str = Cookie(default="")):
        name = user(jw_session)
        data.set_holdings(name, [{"code": code, "qty": qty, "cost": cost} for code, qty, cost in DEFAULT_HOLDINGS])
        return {"holdings": data.holdings(name)}

    @app.get("/api/brief")
    def brief(date: str | None = None, jw_session: str = Cookie(default="")):
        name = user(jw_session)
        day = date or now().date().isoformat()
        try:
            day = calendar_date.fromisoformat(day).isoformat()
        except ValueError:
            raise HTTPException(400, "日期格式不正确。") from None
        codes = [holding["code"] for holding in data.holdings(name)]
        return data.brief(codes, day)

    @app.get("/api/briefs")
    def briefs(jw_session: str = Cookie(default="")):
        codes = [holding["code"] for holding in data.holdings(user(jw_session))]
        return {"today": now().date().isoformat(), "briefs": data.briefs(codes)}

    @app.post("/api/brief/generate")
    def generate(jw_session: str = Cookie(default="")):
        name = user(jw_session)
        if settings.mode != "api":
            raise HTTPException(503, "模型尚未配置。")
        codes = [holding["code"] for holding in data.holdings(name)]
        if not codes:
            raise HTTPException(400, "请先添加持仓。")
        try:
            job_id = jobs.submit(name, codes, now().date().isoformat(), None if name == "team" else BRIEF_LIMIT)
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from None
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, jw_session: str = Cookie(default="")):
        result = data.job(job_id, user(jw_session))
        if result is None:
            raise HTTPException(404, "任务不存在。")
        return result

    @app.post("/api/research")
    def research(body: ResearchInput, jw_session: str = Cookie(default="")):
        name = user(jw_session)
        if not body.question.strip():
            raise HTTPException(400, "请输入研究问题。")
        if settings.mode != "api":
            raise HTTPException(503, "模型尚未配置。")
        holdings = data.holdings(name)
        if not holdings:
            raise HTTPException(400, "请先添加持仓。")
        try:
            job_id = jobs.submit_research(name, body.question.strip(), holdings, now().date().isoformat(),
                                          None if name == "team" else RESEARCH_LIMIT)
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from None
        return {"job_id": job_id}

    @app.get("/api/research")
    def research_list(jw_session: str = Cookie(default="")):
        return {"records": data.jobs(user(jw_session), "research")}

    @app.get("/api/search")
    def search(q: str, jw_session: str = Cookie(default="")):
        user(jw_session)
        if not q.strip() or len(q) > 40:
            return {"results": []}
        try:
            return {"results": [{"code": code, "name": name} for code, name in observe("腾讯行情", search_symbols, q)
                                if not is_index(code)]}
        except MarketError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.get("/api/stock/{code}")
    def stock(code: str, jw_session: str = Cookie(default="")):
        user(jw_session)
        symbol = stock_symbol(code)
        cached = stock_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < QUOTE_TTL:
            return cached[1]
        result = {"symbol": symbol, "history": None, "events": [], "errors": []}
        # 个股接口是外部数据边界；一个来源失败时仍显示其他已取得的数据。
        for label, source, function, kwargs in [
            ("日线", "腾讯行情", fetch_history, {}),
            ("公告", "东方财富", events.fetch_announcements, {"days": 7}),
            ("解禁", "东方财富", events.fetch_unlocks, {"past_days": 30, "future_days": 30}),
            ("增减持", "东方财富", events.fetch_holder_changes, {"days": 7}),
            ("业绩预告", "东方财富", events.fetch_forecasts, {"days": 7}),
            ("财报", "东方财富", events.fetch_financials, {"periods": 4}),
        ]:
            try:
                value = observe(source, function, symbol, **kwargs)
                if label == "日线":
                    result["history"] = value
                else:
                    result["events"].extend(value)
            except (MarketError, events.EventError) as exc:
                result["errors"].append({"source": label, "detail": str(exc)})
        result["events"].sort(key=lambda event: event.get("date", ""), reverse=True)
        if len(stock_cache) > 128:
            stock_cache.clear()
        stock_cache[symbol] = (time.monotonic(), result)
        return result

    @app.get("/api/system")
    def system(jw_session: str = Cookie(default="")):
        name = user(jw_session)
        day = now().date().isoformat()
        observed = data.source_observations(name)
        with observation_lock:
            for source, observation in observations.items():
                if observation["checked_at"] >= observed.get(source, {}).get("checked_at", 0):
                    observed[source] = observation
        sources = []
        for source, use, configured in [("腾讯行情", "报价与日线", True), ("东方财富", "公告与事件", True),
                                         ("Exa", "新闻检索", bool(settings.exa_api_key)), ("DeepSeek", "分析与写作", settings.mode == "api")]:
            sources.append({"name": source, "use": use, "configured": configured,
                            **observed.get(source, {"state": "unknown", "checked_at": None})})
        return {"sources": sources,
                "usage": {"brief": data.usage_count(name, day, "brief"), "research": data.usage_count(name, day, "research")},
                "limits": {"brief": None if name == "team" else BRIEF_LIMIT, "research": None if name == "team" else RESEARCH_LIMIT}}

    def closes(symbol, since):
        cached = history_cache.get(symbol)
        if not cached or time.monotonic() - cached[0] >= QUOTE_TTL:
            try:
                bars = observe("腾讯行情", fetch_history, symbol)["bars"]
            except MarketError:
                return None
            if len(history_cache) > 128:
                history_cache.clear()
            cached = history_cache[symbol] = (time.monotonic(), [{"date": bar["date"], "close": bar["close"]} for bar in bars])
        return [bar for bar in cached[1] if bar["date"] >= since]

    @app.get("/api/timeline/{code}")
    def timeline_view(code: str, jw_session: str = Cookie(default="")):
        name, symbol = user(jw_session), stock_symbol(code)
        stock = data.timeline_stock(symbol)
        if stock is None:
            return {"code": symbol, "initialized": False, "threads": [], "links": [], "candidates": [], "history": None}
        threads = data.threads(symbol, name)
        used = {node["art_code"] for thread in threads for node in thread["nodes"]}
        candidates = [{"art_code": item["art_code"], "date": item["date"], "title": short_title(item["title"]),
                       "categories": item["categories"], "url": item["url"]}
                      for item in reversed(timeline_candidates(stock["announcements"])) if item["art_code"] not in used]
        return {"code": symbol, "initialized": True, "since": stock["since"], "until": stock["until"], "threads": threads,
                "links": data.links(symbol, name), "candidates": candidates, "history": closes(symbol, stock["since"]),
                "commitments": commitments.ledger(data.commitments([symbol]), now().date().isoformat()), "chain": data.chain(symbol)}

    board_cache = {}

    @app.get("/api/industry")
    def industry_view(jw_session: str = Cookie(default="")):
        result = industry.graph(data, [holding["code"] for holding in data.holdings(user(jw_session))])
        codes = tuple(sorted({n["code"] for n in result["nodes"]}))
        hit = board_cache.get(codes)
        if not hit or time.monotonic() - hit[0] >= QUOTE_TTL:
            # 板块涨跌只是着色，取不到时图照常显示。
            try:
                hit = board_cache[codes] = (time.monotonic(), observe("东方财富板块", events.fetch_board_changes, codes) if codes else {})
            except events.EventError:
                hit = (0, {})
            if len(board_cache) > 64:
                board_cache.clear()
        return {**result, "changes": hit[1]}

    @app.get("/api/macro")
    def macro_view(jw_session: str = Cookie(default="")):
        codes = [holding["code"] for holding in data.holdings(user(jw_session))]
        boards = data.company_boards()
        since = (now().date() - timedelta(days=370)).isoformat()
        return {"events": data.macro_events(since),
                "concepts": {code: [c["name"] for c in boards[code[2:]]["concepts"]][:3] for code in codes if code[2:] in boards}}

    @app.get("/api/commitments")
    def commitment_ledger(jw_session: str = Cookie(default="")):
        codes = [holding["code"] for holding in data.holdings(user(jw_session))]
        items = commitments.ledger(data.commitments(codes), now().date().isoformat()) if codes else []
        initialized = set(data.timeline_codes())
        return {"today": now().date().isoformat(), "items": items, "counts": commitments.stats(items),
                "missing": [code for code in codes if code not in initialized]}

    @app.post("/api/timeline/{code}/init")
    def timeline_init(code: str, jw_session: str = Cookie(default="")):
        name, symbol = user(jw_session), stock_symbol(code)
        if settings.mode != "api":
            raise HTTPException(503, "模型尚未配置。")
        if data.timeline_stock(symbol) is not None:
            raise HTTPException(400, "脉络已生成。")
        try:
            return {"job_id": jobs.submit_timeline(name, symbol, now().date().isoformat(), limit(name, TIMELINE_LIMIT))}
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from None

    @app.post("/api/timeline/{code}/nodes")
    def timeline_add(code: str, body: NodeInput, jw_session: str = Cookie(default="")):
        name, symbol = user(jw_session), stock_symbol(code)
        stock = data.timeline_stock(symbol)
        if stock is None:
            raise HTTPException(400, "请先生成脉络。")
        item = next((item for item in stock["announcements"] if item["art_code"] == body.art_code), None)
        if item is None:
            raise HTTPException(400, "公告不在这只股票近一年的列表中。")
        node = {"date": item["date"], "kind": "公告", "major": False, "title": short_title(item["title"]),
                "url": item["url"], "art_code": item["art_code"]}
        try:
            node_id = data.add_user_node(symbol, name, node, thread_id=body.thread_id,
                                         title=(body.title or "").strip() or node["title"][:40])
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"id": node_id}

    @app.post("/api/timeline/links")
    def timeline_link(body: LinkInput, jw_session: str = Cookie(default="")):
        name = user(jw_session)
        if settings.mode != "api":
            raise HTTPException(503, "模型尚未配置。")
        if body.node_a == body.node_b:
            raise HTTPException(400, "请选择两个不同的节点。")
        if data.node(body.node_a, name) is None or data.node(body.node_b, name) is None:
            raise HTTPException(404, "节点不存在。")
        try:
            return {"job_id": jobs.submit_link(name, body.node_a, body.node_b, now().date().isoformat(), limit(name, LINK_LIMIT))}
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from None

    # 脚本和样式地址带上文件版本，浏览器里残留的旧缓存也不会和新页面混用。
    web = ROOT / "web"
    version = format(int(max(path.stat().st_mtime for path in web.iterdir())), "x")
    index = (web / "index.html").read_text(encoding="utf-8")
    for path in web.iterdir():
        if path.suffix in (".js", ".css"):
            index = index.replace(f'"{path.name}"', f'"{path.name}?v={version}"')

    @app.get("/", include_in_schema=False)
    def home():
        return HTMLResponse(index)

    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app
