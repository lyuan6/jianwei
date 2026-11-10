// Authenticated API state. Direct file previews keep using data.js.
(function (A) {
  const D = A.D;
  let username = "", stockRequest = 0, searchRequest = 0, session = 0;
  const timers = {}, polling = {};
  const jobKey = kind => `jianwei-${kind}-job:${username}`;
  const running = job => job && ["queued", "running"].includes(job.status);

  function show(loggedIn) {
    document.getElementById("app").hidden = !loggedIn;
    document.getElementById("login").hidden = loggedIn;
  }

  async function api(path, body, method = body === undefined ? "GET" : "POST") {
    const res = await fetch(path, { method, credentials: "same-origin",
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试。");
      err.status = res.status;
      if (res.status === 401) { stopPolling(); show(false); }
      throw err;
    }
    return data;
  }

  function stopPolling() {
    session++;
    for (const kind of ["brief", "research"]) { clearTimeout(timers[kind]); delete polling[kind]; }
  }

  function setHoldings(rows) {
    const known = [...D.holdings, ...(D.holdingDraft || [])];
    D.holdings = rows.map(h => ({ ...h, name: known.find(old => old.code === h.code)?.name || h.code, close: null, chg: null }));
    D.holdingDraft = null;
  }

  function applyBrief(brief) { D.brief = brief; D.events = brief?.events || []; }

  async function loadToday(at = session) {
    const [today, list] = await Promise.all([api("/api/brief"), api("/api/briefs")]);
    // Weekends and weekday mornings before the scheduled job have no brief yet, so open on the latest one.
    const latest = today ? null : list.briefs[0]?.date || null;
    const brief = latest ? await api("/api/brief?date=" + encodeURIComponent(latest)) : today;
    if (at !== session) return;
    applyBrief(brief); D.briefs = list.briefs; D.today = list.today; D.briefDate = latest;
  }

  async function showBrief(day) {
    const at = session;
    D.briefDate = day === D.today ? null : day; D.error = "";
    try {
      const brief = await api("/api/brief?date=" + encodeURIComponent(day));
      if (at === session && (D.briefDate || D.today) === day) applyBrief(brief);
    } catch (err) { if (at === session) D.error = err.message; }
    if (at === session) A.rerender();
  }

  async function loadQuotes() {
    const at = session, codes = [...new Set([...D.indices.map(i => i.code), ...D.holdings.map(h => h.code)])];
    const groups = [];
    for (let i = 0; i < codes.length; i += 12) groups.push(codes.slice(i, i + 12));
    const results = await Promise.allSettled(groups.map(codes => api("/api/quotes?symbols=" + codes.join(","))));
    if (at !== session) return;
    for (const r of results) {
      if (r.status !== "fulfilled") continue;
      Object.entries(r.value.quotes).forEach(([code, q]) => {
        for (const item of [...D.indices, ...D.holdings].filter(h => h.code === code)) {
          if ("value" in item) item.value = q.price; else item.close = q.price;
          item.name = q.name; item.chg = q.change_pct; item.asof = q.as_of;
        }
      });
    }
    A.rerender();
  }

  async function loadSystem() {
    const at = session;
    D.systemError = "";
    try { const result = await api("/api/system"); if (at === session) D.system = result; }
    catch (err) { if (at === session) D.systemError = err.message; }
    if (at === session && location.hash.split("/")[0] === "#system") A.rerender();
  }

  async function loadRecords() {
    const at = session;
    try {
      const records = (await api("/api/research")).records;
      if (at !== session) return;
      D.researchRecords = records;
      A.renderRail();
      if (location.hash.split("/")[0] === "#research") A.rerender();
    } catch (err) {
      if (at === session) { D.researchError = err.message; A.rerender(); }
    }
  }

  async function pollJob(kind, id) {
    clearTimeout(timers[kind]);
    polling[kind] = id;
    const at = session;
    try {
      const job = await api("/api/jobs/" + encodeURIComponent(id));
      if (at !== session || polling[kind] !== id) return;
      if (kind === "brief") {
        D.job = job; D.error = "";
        if (job.status === "done") {
          // 持仓可能已在任务运行期间改变；按当前持仓重新读取，不能展示旧组合结果。
          await loadToday();
        }
      } else {
        D.researchJob = job; D.researchError = job.error || "";
        D.research = job.status === "done" ? job.result : null;
      }
      if (at !== session || polling[kind] !== id) return;
      if (running(job)) {
        const target = document.getElementById(kind === "brief" ? "job-progress" : "research-content");
        if (target) {
          const opened = [...target.querySelectorAll("details[open]")].map(el => el.dataset.paper);
          target.innerHTML = kind === "brief" ? A.progress() : A.researchContent();
          target.querySelectorAll("details[data-paper]").forEach(el => { el.open = opened.includes(el.dataset.paper); });
        }
        timers[kind] = setTimeout(() => pollJob(kind, id), 1000);
      } else {
        if (sessionStorage.getItem(jobKey(kind)) === id) sessionStorage.removeItem(jobKey(kind));
        delete polling[kind];
        A.rerender();
        loadSystem();
        if (kind === "research") loadRecords();
      }
    } catch (err) {
      if (at !== session || polling[kind] !== id) return;
      if (kind === "brief") D.error = err.message; else D.researchError = err.message;
      A.rerender();
    }
  }

  async function loadBrief() {
    const at = session;
    try {
      const [holdings] = await Promise.all([api("/api/holdings"), loadToday(at)]);
      if (at !== session) return;
      setHoldings(holdings.holdings); D.error = "";
      if (location.hash.split("/")[0] === "#timeline") A.loadTimeline();
      A.loadCommitments?.();
    } catch (err) { if (at === session) D.error = err.message; }
    if (at !== session) return;
    D.loading = false;
    A.rerender();
    loadQuotes();
    const pending = sessionStorage.getItem(jobKey("brief"));
    if (pending && !polling.brief) pollJob("brief", pending);
  }

  A.loadStock = async function (code) {
    if (!A.live || D.stockLoading === code || D.stock?.symbol === code || D.stockErrorCode === code) return;
    const request = ++stockRequest, at = session;
    D.stockLoading = code; D.stockError = ""; D.stock = null;
    try {
      const result = await api("/api/stock/" + encodeURIComponent(code));
      if (request === stockRequest && at === session) D.stock = result;
    } catch (err) {
      if (request === stockRequest && at === session) { D.stockError = err.message; D.stockErrorCode = code; }
    } finally {
      if (request === stockRequest && at === session) { D.stockLoading = null; A.refreshDrawer(code); }
    }
  };

  A.loadPage = page => {
    if (!A.live || !username) return;
    if (page === "research") loadRecords();
    if (page === "system") loadSystem();
    if (page === "timeline") A.loadTimeline();
    if (page === "commitments") A.loadCommitments();
  };

  function draft() {
    if (!D.holdingDraft) D.holdingDraft = D.holdings.map(h => ({ ...h }));
    return D.holdingDraft;
  }

  async function saveHoldings(reset = false) {
    if (D.holdingBusy || D.loading) return;
    D.holdingBusy = true; D.holdingMessage = ""; D.holdingError = "";
    const rows = draft().map(h => ({code: h.code, qty: h.qty || null, cost: h.cost || null}));
    A.rerender();
    try {
      const result = reset ? await api("/api/holdings/reset", {}) : await api("/api/holdings", rows, "PUT");
      setHoldings(result.holdings); applyBrief(null);
      D.holdingMessage = "已保存"; D.searchResults = []; D.searchedQuery = null;
      await loadToday();
      loadQuotes();
    } catch (err) { D.holdingError = err.message; }
    finally { D.holdingBusy = false; A.rerender(); }
  }

  async function searchStock() {
    const query = (D.searchQuery || "").trim(), request = ++searchRequest;
    D.searching = true; D.searchError = ""; A.rerender();
    try {
      const result = await api("/api/search?q=" + encodeURIComponent(query));
      if (request === searchRequest) { D.searchResults = result.results; D.searchedQuery = query; }
    } catch (err) { if (request === searchRequest) D.searchError = err.message; }
    finally { if (request === searchRequest) { D.searching = false; A.rerender(); } }
  }

  document.addEventListener("input", e => {
    const row = e.target.closest("[data-holding-row]");
    if (row) { draft().find(h => h.code === row.dataset.holdingRow)[e.target.name] = e.target.value; D.holdingMessage = ""; }
    if (e.target.id === "holding-search") D.searchQuery = e.target.value;
    if (e.target.closest("[data-research-form]")) D.question = e.target.value;
  });

  document.addEventListener("change", e => { if (e.target.id === "brief-date") showBrief(e.target.value); });

  document.addEventListener("click", async e => {
    if (e.target.closest("[data-refresh]")) return loadBrief();
    if (e.target.closest("[data-refresh-system]")) return loadSystem();
    if (e.target.closest("[data-reset-holdings]")) return saveHoldings(true);
    if (e.target.closest("[data-save-holdings]")) return saveHoldings();
    const remove = e.target.closest("[data-remove-holding]");
    if (remove && !D.holdingBusy) { D.holdingDraft = draft().filter(h => h.code !== remove.dataset.removeHolding); A.rerender(); return; }
    const add = e.target.closest("[data-add-code]");
    if (add && !D.holdingBusy) {
      const code = add.dataset.addCode;
      if (draft().length >= 30) D.holdingError = "持仓最多 30 只。";
      else if (!draft().some(h => h.code === code)) draft().push({code, name: D.searchResults.find(h => h.code === code)?.name || code, qty: null, cost: null});
      A.rerender(); return;
    }
    const record = e.target.closest("[data-research-record]");
    if (record) {
      e.preventDefault();
      D.research = null; D.researchError = ""; D.researchJob = {status: "queued", steps: []};
      location.hash = "research"; A.rerender();
      pollJob("research", record.dataset.researchRecord); return;
    }
    if (e.target.closest("[data-resume-research]")) {
      const pending = sessionStorage.getItem(jobKey("research"));
      if (pending) pollJob("research", pending);
      return;
    }
    if (e.target.closest("[data-retry-stock]")) {
      D.stockErrorCode = null; D.stock = null; A.loadStock(location.hash.split("/")[1]); return;
    }
    if (!e.target.closest("[data-generate]") || D.starting || running(D.job)) return;
    D.starting = true; D.error = ""; A.rerender();
    try {
      const result = await api("/api/brief/generate", {});
      sessionStorage.setItem(jobKey("brief"), result.job_id);
      D.job = {status: "queued", steps: []}; pollJob("brief", result.job_id);
    } catch (err) { D.error = err.message; }
    finally { D.starting = false; A.rerender(); }
  });

  document.addEventListener("submit", async e => {
    if (e.target.id === "holding-search-form") { e.preventDefault(); return searchStock(); }
    if (e.target.id === "holding-form") { e.preventDefault(); return saveHoldings(); }
    const form = e.target.closest("[data-research-form]");
    if (!form) return;
    e.preventDefault();
    startResearch();
  });

  async function startResearch() {
    const question = (D.question || "").trim();
    if (!question || D.researchStarting || running(D.researchJob)) return;
    D.researchStarting = true; D.research = null; D.researchError = "";
    A.rerender();
    try {
      const result = await api("/api/research", {question});
      sessionStorage.setItem(jobKey("research"), result.job_id);
      D.researchJob = {status: "queued", steps: []}; pollJob("research", result.job_id);
    } catch (err) { D.researchError = err.message; }
    finally { D.researchStarting = false; A.rerender(); }
  }

  A.api = api;
  // Entry from the 立体 view: asks in place, the answer appears in its analysis panel.
  A.askResearch = question => {
    if (running(D.researchJob) || D.researchStarting) {
      D.researchError = "上一个分析还在进行，请稍后再试。";
      A.rerender();
    } else {
      D.question = question;
      startResearch();
      // The preset question is long; the input goes back to empty for the next free question.
      D.question = "";
    }
  };

  async function boot() {
    if (!A.live) { show(true); return A.start(); }
    try {
      const me = await api("/api/me");
      stopPolling(); username = me.user;
      Object.assign(D, {holdings: [], holdingDraft: null, brief: null, events: [], briefs: [], today: null, briefDate: null,
        job: null, research: null, researchJob: null,
        researchRecords: [], researchError: "", stock: null, stockLoading: null, stockErrorCode: null, error: "", question: "",
        system: null, tlData: {}, tlFocus: null, tlSelected: null, tlFrom: null, tlJob: null, tlError: "", loading: true, holdingError: "", holdingMessage: "", searchQuery: "", searchedQuery: null, searchResults: []});
      document.getElementById("rail-user-name").textContent = username;
      document.getElementById("rail-user").hidden = false;
      show(true); A.start(); loadBrief(); loadRecords(); loadSystem();
      const pending = sessionStorage.getItem(jobKey("research"));
      if (pending) pollJob("research", pending);
    } catch (err) {
      show(false);
      document.getElementById("login-error").textContent = err.status === 401 ? "" : "无法连接服务器，请稍后刷新。";
    }
  }

  document.getElementById("login-form").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.target, button = form.querySelector("button"), error = document.getElementById("login-error");
    button.disabled = true; error.textContent = "";
    try { await api("/api/login", {username: form.username.value, password: form.password.value}); form.reset(); await boot(); }
    catch (err) { form.password.value = ""; error.textContent = err.message; }
    finally { button.disabled = false; }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    stopPolling(); await api("/api/logout", {}).catch(() => {}); location.hash = ""; location.reload();
  });
  boot();
})(App);
