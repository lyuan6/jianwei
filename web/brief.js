// Morning brief: masthead, prioritized event feed, side modules.
(function (A) {
  const { D, esc, fixed, signed, holding, codeOf, lvl, series, lineChart } = A;

  const stock = code => `<a class="stock-link" href="#" data-stock="${code}">${esc(holding(code).name)}</a><span class="code">${codeOf(code)}</span>`;

  const figures = list => !list.length ? "" : `<dl class="figures">${list.map(([k, v, tone]) =>
    `<div><dt>${esc(k)}</dt><dd class="${tone || ""}">${esc(v)}</dd></div>`).join("")}</dl>`;

  function trace(t, open) {
    const steps = t.steps.map(([tool, d, r, warn], i) => `<li class="${warn ? "warn" : ""}">` +
      `<span class="n">${String(i + 1).padStart(2, "0")}</span><span class="tool">${esc(tool)}</span>` +
      `<span class="d">${esc(d)}</span><span class="r">${esc(r)}</span></li>`).join("");
    return `<details class="trace"${open ? " open" : ""}>` +
      `<summary><span><b>工作底稿</b>${t.steps.length} 步</span></summary>` +
      `<ol class="steps">${steps}</ol>` +
      `<p class="trace-src">来源${t.sources.map(s => `<a href="#">${esc(s)}</a>`).join("")}</p></details>`;
  }

  const entry = (e, i) => `<article class="entry" data-level="${e.level}">
    <div class="entry-meta">${lvl(e.level)}<span>${stock(e.code)}</span><span class="tag">${esc(e.tag)}</span><time>${esc(e.date)}</time></div>
    <h3 class="entry-title">${esc(e.title)}</h3>
    ${figures(e.figures)}
    <div class="entry-cols">
      <div><h4>建议关注</h4><ul class="points">${e.points.map(p => `<li>${esc(p)}</li>`).join("")}</ul></div>
      ${e.risk ? `<div class="risk"><h4>风险提示</h4><p>${esc(e.risk)}</p></div>` : ""}
    </div>
    ${trace(e.trace, i === 0)}
  </article>`;

  function side() {
    const flagged = new Set(D.events.filter(e => e.level !== "low").map(e => e.code));
    const rows = D.holdings.map(h => `<tr data-stock="${h.code}">
      <td>${flagged.has(h.code) ? `<span class="nm"><i class="flag"></i>` : `<span class="nm noflag">`}${esc(h.name)}</span></td>
      <td class="r">${fixed(h.close)}</td><td class="r">${signed(h.chg, 2, "%")}</td>
      <td class="r">${lineChart(series(h.code, h.close, 30), 52, 18, "spark")}</td></tr>`).join("");
    const cal = D.calendar.map(c =>
      `<li><span class="d">${c.d}</span><span class="t"><b>${esc(holding(c.code).name)}</b>${esc(c.t)}</span></li>`).join("");
    return `<aside class="side">
      <section><div class="section-head"><h3>持仓</h3><a class="link" href="#holdings">全部</a></div>
        <table class="mini"><tbody>${rows}</tbody></table></section>
      <section><div class="section-head"><h3>未来 30 天</h3></div><ul class="cal">${cal}</ul></section>
    </aside>`;
  }

  A.pages.brief = function () {
    const count = l => D.events.filter(e => l === "all" || e.level === l).length;
    const chips = [["all", "全部"], ["high", "重点"], ["mid", "留意"], ["low", "一般"]].map(([k, t]) =>
      `<button class="chip${k === "all" ? " on" : ""}" type="button" data-filter="${k}">${t}<span class="n">${count(k)}</span></button>`).join("");
    const idx = D.indices.map(i =>
      `<div class="idx"><span>${esc(i.name)}</span><span><span class="v">${fixed(i.value)}</span>${signed(i.chg, 2, "%")}</span></div>`).join("");
    // The lede is trusted static copy and may contain <b>.
    return `<header class="masthead">
      <div class="mast-top"><span>${esc(D.issue.date)}</span></div>
      <h1 class="mast-title">${esc(D.issue.title)}</h1>
      <p class="lede">${D.issue.lede}</p>
      <div class="indices">${idx}</div>
    </header>
    <div class="brief-grid">
      <section>
        <div class="feed-bar"><div class="chips" role="group" aria-label="按重要程度筛选">${chips}</div>
          <button class="textbtn" type="button">导出晨报</button></div>
        ${D.events.map(entry).join("")}
      </section>
      ${side()}
    </div>`;
  };
})(App);

// Live brief keeps references scoped to each stock, including the original tool results.
(function (A) {
  if (!A.live) return;
  const { D, esc, fixed, signed, lvl, holding } = A;
  const names = { get_commitments: "承诺台账", list_announcements: "公告检索", read_announcement: "公告正文", get_unlocks: "限售解禁",
    get_holder_changes: "股东增减持", get_forecasts: "业绩预告", get_financials: "财务指标",
    get_price_history: "行情计算", compare: "同期比较", search_news: "新闻检索", get_holdings: "当前持仓" };

  function sources(value) {
    if (Array.isArray(value)) return value.flatMap(sources);
    if (!value || typeof value !== "object") return [];
    return Object.entries(value).flatMap(([key, item]) => {
      if ((key === "url" || key === "pdf_url") && typeof item === "string" && /^https?:\/\//i.test(item))
        return [{ url: item, title: value.title || value.source || "来源原文" }];
      return typeof item === "object" ? sources(item) : [];
    });
  }

  function paper(steps, key, references = {}) {
    return `<details class="trace" data-paper="${esc(key)}"><summary><span><b>工作底稿</b>${steps.length} 步</span></summary>
      <ol class="steps">${steps.map((s, i) => `<li class="${s.error ? "warn" : ""}">
      <span class="n">${i + 1}</span><span class="tool">${esc(names[s.tool] || s.tool)}</span>
      <span class="d">${s.code ? esc(holding(s.code).name) + " · " : ""}${esc(s.summary)}
      <small class="step-args">${esc(JSON.stringify(s.arguments))}</small></span>
      <span class="r">${esc(s.ref)} · ${esc(s.elapsed_seconds)} 秒${s.error ? " · 出错" : ""}</span></li>`).join("")}</ol>
      ${Object.entries(references).map(([ref, value]) => `<details class="reference"><summary>${esc(ref)} · 工具结果</summary>
        <div class="trace-src">${sources(value).map(s => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>`).join("")}</div>
        <pre>${esc(JSON.stringify(value, null, 2))}</pre></details>`).join("")}</details>`;
  }

  function entry(e, i) {
    const result = D.brief.stocks.find(s => s.code === e.code);
    return `<article class="entry" data-level="${esc(e.level)}">
      <div class="entry-meta">${lvl(e.level)}<span>${esc(holding(e.code).name)} <span class="code">${esc(e.code)}</span></span>
      <span>${esc(e.tag)}</span><time>${esc(e.date)}</time></div><h3 class="entry-title">${esc(e.title)}</h3>
      <dl class="figures">${e.figures.map(f => `<div><dt>${esc(f.label)}</dt><dd>${esc(f.value)}</dd>
      <small>${esc(f.ref)}${f.verification === "未核对" ? ' · <b class="unverified">未核对</b>' : ""}</small></div>`).join("")}</dl>
      <div class="entry-cols"><div><h4>事实与关注</h4><ul class="points">${e.points.map(p => `<li>${esc(p)}</li>`).join("")}</ul></div>
      ${e.risk ? `<div class="risk"><h4>风险提示</h4><p>${esc(e.risk)}</p></div>` : ""}</div>
      <p class="trace-src">${e.refs.map(ref => sources(result.references[ref]).map(s =>
        `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(ref)} · ${esc(s.title)}</a>`).join("")).join("")}</p>
      ${paper(result.steps, `${e.code}-${i}`, result.references)}</article>`;
  }

  A.paper = paper;
  A.sources = sources;

  A.progress = function () {
    const job = D.job;
    if (!job) return "";
    const state = { queued: "排队中", running: "正在生成", done: "生成完成", failed: "生成失败" }[job.status];
    return `<div class="section-head"><h3>${state}</h3><span class="meta">${job.steps.length} 步</span></div>
      ${job.error ? `<p class="quiet" role="alert">${esc(job.error)}</p>` : ""}
      ${paper(job.steps, "job")}`;
  };

  A.pages.brief = function () {
    const busy = D.starting || D.job && ["queued", "running"].includes(D.job.status);
    const today = new Date().toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "long", day: "numeric", weekday: "long" });
    const count = level => D.events.filter(e => level === "all" || e.level === level).length;
    const title = D.loading ? "正在读取晨报" : D.brief?.title || "今日晨报尚未生成";
    const days = [...new Set([D.today, ...(D.briefs || []).map(b => b.date)])].filter(Boolean);
    const picker = days.length < 2 ? "" : `<select id="brief-date" aria-label="晨报日期">${days.map(d =>
      `<option value="${esc(d)}"${d === (D.briefDate || D.today) ? " selected" : ""}>${d === D.today ? "今天" : esc(d)}</option>`).join("")}</select>`;
    return `<header class="masthead"><div class="mast-top"><span>${esc(D.brief?.date || today)}</span><span>${picker}
      ${!D.brief && !D.briefDate ? `<button class="textbtn" type="button" data-generate ${busy || D.loading ? "disabled" : ""}>${busy ? "生成中" : "生成晨报"}</button>` : ""}</span></div>
      <h1 class="mast-title">${esc(title)}</h1><p class="lede">${esc(D.brief?.lede || "")}</p>
      ${D.brief?.status === "数据不全" ? '<p class="quiet">部分数据未取得，请查看工作底稿。</p>' : ""}
      ${D.briefDate ? "" : `<div class="indices">${D.indices.map(i => `<div class="idx"><span>${esc(i.name)}</span><span><span class="v">${fixed(i.value)}</span>${signed(i.chg, 2, "%")}</span></div>`).join("")}</div>`}</header>
      ${D.error ? `<p class="quiet" role="alert">${esc(D.error)} <button class="textbtn" data-refresh>重新读取</button></p>` : ""}
      <section id="job-progress" aria-live="polite">${A.progress()}</section>
      <div class="brief-grid"><section><div class="feed-bar"><div class="chips">
      ${[["all", "全部"], ["high", "重点"], ["mid", "留意"], ["low", "一般"]].map(([k, name]) => `<button class="chip ${k === "all" ? "on" : ""}" data-filter="${k}">${name}<span class="n">${count(k)}</span></button>`).join("")}</div></div>
      ${D.events.map(entry).join("")}
      ${(D.brief?.stocks || []).filter(s => !s.events.length).map(s => `<section class="entry"><h3>${esc(holding(s.code).name)}</h3><p class="quiet">${esc(s.status === "完成" ? "未形成事件结论" : s.status === "失败" ? `分析失败：${s.error}` : s.status)}</p>${paper(s.steps, s.code, s.references)}</section>`).join("")}
      </section><aside class="side"><section><div class="section-head"><h3>持仓</h3></div><table class="mini"><tbody>
      ${D.holdings.map(h => `<tr data-stock="${esc(h.code)}"><td>${esc(h.name)}</td><td class="r">${fixed(h.close)}</td><td class="r">${signed(h.chg, 2, "%")}</td></tr>`).join("")}
      </tbody></table></section></aside></div>`;
  };
})(App);
