// Shared helpers, hash routing, stock drawer.
window.App = (function () {
  const live = location.protocol !== "file:";
  const D = window.DATA || {
    issue: {}, events: [], holdings: [], calendar: [], recent: [], brief: null, research: null, system: null, stock: null, job: null, error: "", loading: true,
    indices: [["sh000001", "上证指数"], ["sz399001", "深证成指"], ["sz399006", "创业板指"], ["sh000688", "科创 50"]]
      .map(([code, name]) => ({ code, name, value: null, chg: null })),
  };
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const dir = v => (v > 0 ? "up" : v < 0 ? "down" : "flat");
  const fixed = (v, p = 2) => v == null ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: p, maximumFractionDigits: p });
  const signed = (v, p = 2, suffix = "") =>
    v == null ? "—" : `<span class="${dir(v)}">${v > 0 ? "+" : v < 0 ? "−" : ""}${fixed(Math.abs(v), p)}${suffix}</span>`;
  const holding = code => D.holdings.find(h => h.code === code) || D.holdingDraft?.find(h => h.code === code) || { code, name: code };
  const codeOf = code => code.slice(2);
  const board = c => c.startsWith("sh688") ? "上交所 · 科创板" : c.startsWith("sz300") ? "深交所 · 创业板"
    : c.startsWith("sh") ? "上交所 · 主板" : "深交所 · 主板";
  const LEVELS = { high: ["lvl-high", "重点"], mid: ["lvl-mid", "留意"], low: ["lvl-low", "一般"] };
  const lvl = l => `<span class="lvl ${LEVELS[l][0]}">${LEVELS[l][1]}</span>`;

  // Deterministic sample price path ending at the given close, so screenshots are stable.
  function series(code, end, n) {
    let seed = [...code].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
    const rnd = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296;
    const out = [end];
    for (let i = 1; i < n; i++) out.unshift(out[0] / (1 + (rnd() - 0.48) * 0.045));
    return out;
  }

  function lineChart(values, w, h, cls) {
    if (values.length < 2) return "";
    const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
    const line = values.map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`).join(" ");
    return `<svg class="${cls} ${dir(values[values.length - 1] - values[0])}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none" aria-hidden="true">` +
      `<polygon class="chart-area" points="0,${h} ${line} ${w},${h}"/><polyline class="chart-line" points="${line}"/></svg>`;
  }

  const ROUTES = { brief: "持仓晨报", holdings: "持仓", timeline: "脉络", commitments: "承诺", research: "研究", system: "系统" };
  let current = null;

  function route(resetScroll = true) {
    const [page, arg] = location.hash.slice(1).split("/");
    const name = page in ROUTES ? page : "brief";
    if (name !== current) {
      current = name;
      document.getElementById("main").dataset.page = name;
      document.getElementById("main").innerHTML = App.pages[name]();
      document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.route === name));
      document.title = `见微 · ${ROUTES[name]}`;
      if (resetScroll) {
        window.scrollTo(0, 0);
        App.loadPage?.(name);
      }
    }
    arg ? openDrawer(arg) : closeDrawer();
  }

  function filter(level) {
    document.querySelectorAll("[data-filter]").forEach(c => c.classList.toggle("on", c.dataset.filter === level));
    document.querySelectorAll(".entry").forEach(el => { el.hidden = level !== "all" && el.dataset.level !== level; });
  }

  function openDrawer(code) {
    if (live) App.loadStock?.(code);
    const html = App.drawer(code);
    if (!html) return closeDrawer();
    const drawer = document.getElementById("drawer");
    const wasOpen = drawer.classList.contains("open");
    drawer.innerHTML = html;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    document.getElementById("scrim").hidden = false;
    if (!wasOpen) drawer.querySelector(".drawer-close").focus();
  }

  function refreshDrawer(code) {
    if (location.hash.split("/")[1] === code) openDrawer(code);
  }

  function closeDrawer() {
    const drawer = document.getElementById("drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    document.getElementById("scrim").hidden = true;
  }

  function renderRail() {
    document.getElementById("nav-brief").textContent = D.events.filter(e => e.level !== "low").length;
    document.getElementById("nav-holdings").textContent = D.holdings.length;
    document.getElementById("recent").innerHTML = live
      ? (D.researchRecords || []).slice(0, 6).map(r => `<li><a href="#research" data-research-record="${esc(r.id)}">${esc(r.input.question)}</a></li>`).join("")
      : D.recent.map(q => `<li><a href="#research">${esc(q)}</a></li>`).join("");
  }

  let started = false;

  function rerender() {
    const focused = document.activeElement;
    const inputKey = focused?.getAttribute("aria-label");
    const selection = focused?.selectionStart == null ? null : [focused.selectionStart, focused.selectionEnd];
    const opened = [...document.querySelectorAll("details[data-paper][open]")].map(el => el.dataset.paper);
    const activeFilter = document.querySelector("[data-filter].on")?.dataset.filter;
    current = null;
    renderRail();
    route(false);
    document.querySelectorAll("details[data-paper]").forEach(el => { el.open = opened.includes(el.dataset.paper); });
    if (activeFilter) filter(activeFilter);
    if (inputKey && selection) {
      const input = [...document.querySelectorAll("input[aria-label]")].find(el => el.getAttribute("aria-label") === inputKey);
      if (input) { input.focus({preventScroll: true}); input.setSelectionRange(...selection); }
    }
  }

  function start() {
    if (started) return rerender();
    started = true;
    renderRail();
    document.addEventListener("click", e => {
      const stock = e.target.closest("[data-stock]");
      if (stock) { e.preventDefault(); location.hash = `${current}/${stock.dataset.stock}`; return; }
      if (e.target.closest("[data-close]")) { location.hash = current; return; }
      const chip = e.target.closest("[data-filter]");
      if (chip) filter(chip.dataset.filter);
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && document.getElementById("drawer").classList.contains("open")) location.hash = current;
    });
    document.addEventListener("submit", e => {
      if (live || !e.target.closest(".ask")) return;
      e.preventDefault();
      document.getElementById("ask-hint").textContent = "研究功能还没有接入后端。";
    });
    window.addEventListener("hashchange", route);
    // A deep link to a stock opens the drawer in place; only later opens slide in.
    document.body.classList.add("booting");
    route();
    requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.remove("booting")));
  }

  return { D, live, esc, dir, fixed, signed, holding, codeOf, board, lvl, series, lineChart, pages: {}, start, rerender, renderRail, refreshDrawer };
})();
