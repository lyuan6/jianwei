// Holdings, research and system pages, plus the stock drawer.
(function (A) {
  const { D, esc, fixed, signed, holding, codeOf, board, lvl, series, lineChart } = A;
  const value = h => h.close * h.qty;
  const events = code => D.events.filter(e => e.code === code);

  const jobState = status => ({queued:"排队中", running:"正在研究", done:"研究完成", failed:"研究失败"})[status] || "";
  const sourceLink = (url, title, description = title) => typeof url === "string" && /^https?:\/\//i.test(url)
    ? `<a class="link" href="${esc(url)}" title="${esc(description)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>` : "";
  const units = {shares_wan:"股数（万股）", float_ratio_pct:"占流通股（%）", total_ratio_pct:"占总股本（%）", market_cap_yi:"市值（亿元）",
    revenue_yi:"营收（亿元）", net_profit_yi:"归母净利润（亿元）", revenue_yoy_pct:"营收同比（%）", net_profit_yoy_pct:"净利润同比（%）",
    amount_low_yi:"预告下限（亿元）", amount_high_yi:"预告上限（亿元）", yoy_low_pct:"同比下限（%）", yoy_high_pct:"同比上限（%）",
    hold_ratio_after_pct:"变动后持股比例（%）", direction:"变动方向", forecast_type:"预告类型", eps:"每股收益（元）"};
  const unchecked = item => {
    const values = (item.numbers || []).filter(n => n.verification === "未核对").map(n => n.value);
    return values.length ? `<small class="unverified">未核对：${esc(values.join("、"))}</small>` : "";
  };
  function citations(refs, references) {
    return `<span class="trace-src">${refs.map(ref => {
      const links = [...new Map(A.sources(references[ref]).map(s => [s.url, s])).values()];
      if (!links.length) return esc(ref);
      const all = links.map((s, i) => sourceLink(s.url, links.length === 1 ? ref : `${ref}.${i + 1}`, s.title)).join(" ");
      // A paragraph can cite a whole ledger of 30-odd sources; listing them all buries the text under it.
      return links.length > 3 ? `<button class="textbtn cite-more" type="button" data-cite-more>${esc(ref)} · ${links.length} 个来源</button><span hidden>${all}</span>` : all;
    }).join(" ")}</span>`;
  }
  A.researchContent = function () {
    const r = D.research, job = D.researchJob;
    const refs = r?.references || {};
    const table = r?.table;
    return `${D.researchError ? `<p class="quiet" role="alert">${esc(D.researchError)} <button class="textbtn" data-resume-research>重新读取</button></p>` : ""}
      ${job ? `<p class="quiet">${esc(jobState(job.status))} · ${(job.steps || []).length} 步</p>` : ""}
      ${r ? `<article class="note-body"><h2 class="note-title">${esc(r.title)}</h2>
        ${r.paragraphs.map(p => `<p>${esc(p.text)} ${citations(p.refs, refs)} ${unchecked(p)}</p>`).join("")}
        ${table?.columns ? `<table class="table data-table"><caption>${esc(table.caption)}</caption><thead><tr>${table.columns.map(c => `<th>${esc(c)}</th>`).join("")}<th>来源链接</th></tr></thead><tbody>
        ${table.rows.map(row => `<tr class="static">${row.cells.map(cell => `<td>${esc(cell)}</td>`).join("")}<td>${citations(row.refs, refs)} ${unchecked(row)}</td></tr>`).join("")}</tbody></table>` : ""}
        ${r.outlook?.length ? `<section class="outlook"><h3>后续预判 <small>智能体判断，不构成投资建议</small></h3>${r.outlook.map(o => `<div class="outlook-item"><p><span class="outlook-level">把握${esc(o.confidence)}</span> ${esc(o.text)} ${citations(o.refs, refs)} ${unchecked(o)}</p><p class="meta">依据：${esc(o.basis)}</p></div>`).join("")}</section>` : ""}
        ${r.gaps.length ? `<div class="gap"><b>尚未证实</b>${r.gaps.map(g => `<p>${esc(g)}</p>`).join("")}</div>` : ""}</article>` : ""}
      ${job || r ? A.paper(job?.steps || r.steps, "research", refs) : ""}`;
  };

  A.pages.holdings = function () {
    if (A.live) {
      const rows = D.holdingDraft || D.holdings, disabled = D.holdingBusy || D.loading ? "disabled" : "";
      return `<header class="page-head"><h1 class="page-title">持仓</h1><div class="holding-actions">
        <button class="btn btn-solid" type="button" data-save-holdings ${disabled}>保存</button>
        <button class="btn" type="button" data-reset-holdings ${disabled}>恢复示例持仓</button></div></header>
        <p class="quiet" role="status">${esc(D.holdingError || D.holdingMessage || "")}</p>
        <form id="holding-form"><table class="table holding-table"><thead><tr><th>名称</th><th class="r">数量（股）</th><th class="r">成本（元/股）</th><th class="r">现价（元）</th><th></th></tr></thead><tbody>
        ${rows.map(h => `<tr class="static" data-holding-row="${esc(h.code)}"><td><button class="stock-link textbtn" type="button" data-stock="${esc(h.code)}">${esc(holding(h.code).name || h.name)}</button><span class="code">${esc(h.code)}</span></td>
        <td class="r"><input name="qty" inputmode="decimal" value="${esc(h.qty ?? "")}" aria-label="${esc(h.code)} 数量" ${disabled}></td>
        <td class="r"><input name="cost" inputmode="decimal" value="${esc(h.cost ?? "")}" aria-label="${esc(h.code)} 成本" ${disabled}></td>
        <td class="r">${fixed(holding(h.code).close)}</td><td><button class="textbtn" type="button" data-remove-holding="${esc(h.code)}" ${disabled}>删除</button></td></tr>`).join("")}</tbody></table></form>
        <form id="holding-search-form" class="holding-search"><div class="field"><input id="holding-search" placeholder="搜索股票" aria-label="搜索股票" value="${esc(D.searchQuery || "")}"><button class="btn" ${D.searching ? "disabled" : ""}>搜索</button></div></form>
        <div class="search-results">${(D.searchResults || []).map(h => `<button class="btn" data-add-code="${esc(h.code)}" ${rows.some(row => row.code === h.code) || D.holdingBusy ? "disabled" : ""}>${esc(h.name)} · ${esc(h.code)}</button>`).join("")}</div>
        <p class="quiet" role="status">${esc(D.searchError || (!D.searching && D.searchedQuery === D.searchQuery?.trim() && D.searchResults?.length === 0 ? "暂无匹配股票" : ""))}</p>`;
    }
    let total = 0, cost = 0, day = 0;
    D.holdings.forEach(h => { total += value(h); cost += h.cost * h.qty; day += value(h) - value(h) / (1 + h.chg / 100); });
    const rows = D.holdings.map(h => {
      const evs = events(h.code);
      return `<tr data-stock="${h.code}">
        <td class="nm"><b>${esc(h.name)}</b><span class="code">${codeOf(h.code)}</span></td>
        <td class="r">${h.qty.toLocaleString("zh-CN")}</td><td class="r">${fixed(h.cost)}</td><td class="r">${fixed(h.close)}</td>
        <td class="r">${signed(h.chg, 2, "%")}</td><td class="r">${fixed(value(h))}</td>
        <td class="r">${signed((h.close - h.cost) * h.qty)}<br><small>${signed((h.close / h.cost - 1) * 100, 2, "%")}</small></td>
        <td>${evs.length ? evs.map(e => `<span class="ev">${lvl(e.level)} ${esc(e.tag)}</span>`).join("") : `<span class="ev meta">无</span>`}</td></tr>`;
    }).join("");
    return `<header class="page-head">
        <h1 class="page-title">持仓</h1>
        <div class="field"><input placeholder="添加股票" aria-label="添加股票"><button class="btn btn-solid" type="button">添加</button></div>
      </header>
      <dl class="summary">
        <div><dt>持仓市值</dt><dd>${fixed(total)}</dd></div>
        <div><dt>当日盈亏</dt><dd>${signed(day)}</dd></div>
        <div><dt>累计盈亏</dt><dd>${signed(total - cost)}<small>${signed((total / cost - 1) * 100, 2, "%")}</small></dd></div>
        <div><dt>本期事件</dt><dd>${D.events.length}<small>重点 ${D.events.filter(e => e.level === "high").length} 项</small></dd></div>
      </dl>
      <table class="table"><thead><tr><th>名称</th><th class="r">数量</th><th class="r">成本</th><th class="r">现价</th>
        <th class="r">涨跌</th><th class="r">市值</th><th class="r">盈亏</th><th>本期事件</th></tr></thead><tbody>${rows}</tbody></table>
`;
  };

  const cite = s => esc(s).replace(/\[(\d)\]/g, "<sup><a>[$1]</a></sup>");

  A.pages.research = function () {
    if (A.live) {
      const busy = D.researchStarting || ["queued", "running"].includes(D.researchJob?.status);
      return `<header class="page-head"><h1 class="page-title">研究</h1></header>
        <form class="ask" data-research-form><input name="question" placeholder="输入问题" aria-label="研究问题" value="${esc(D.question || "")}" required maxlength="2000"><button type="submit" ${busy ? "disabled" : ""}>${busy ? "研究中" : "提问"}</button></form>
        <div id="research-content" aria-live="polite">${A.researchContent()}</div>
        <section class="research-history"><div class="section-head"><h3>研究记录</h3></div>
        ${(D.researchRecords || []).map(r => `<p><button class="textbtn" data-research-record="${esc(r.id)}">${esc(r.input.question)}</button> <span class="meta">${esc(jobState(r.status))}</span></p>`).join("")}</section>`;
    }
    const r = D.research, t = r.table;
    const cells = (row, tag) => row.map((c, i) => `<${tag}${i ? ' class="r"' : ""}>${esc(c)}</${tag}>`).join("");
    return `<header class="page-head"><h1 class="page-title">研究</h1></header>
      <form class="ask"><input placeholder="输入问题" aria-label="研究问题"><button type="submit">提问</button></form>
      <p class="ask-hint" id="ask-hint"></p>
      <div class="note-grid">
        <article class="note-body">
          <div class="kicker">${esc(r.asof)}</div>
          <h2 class="note-title">${esc(r.title)}</h2>
          ${r.paras.map(p => `<p>${cite(p)}</p>`).join("")}
          <table class="table data-table"><caption>${esc(t.caption)}</caption><thead><tr>${cells(t.head, "th")}</tr></thead>
            <tbody>${t.rows.map(row => `<tr class="static">${cells(row, "td")}</tr>`).join("")}</tbody></table>
          <div class="gap"><b>尚未证实</b>${esc(r.gap)}</div>
        </article>
        <aside class="side">
          <section><div class="section-head"><h3>工作底稿</h3><span class="meta">${r.steps.length} 步</span></div>
            <ol class="timeline">${r.steps.map(([tool, d, res, warn]) => `<li class="${warn ? "warn" : ""}">` +
              `<span class="r">${esc(res)}</span><span class="t">${esc(tool)}</span><span class="d">${esc(d)}</span></li>`).join("")}</ol></section>
          <section><div class="section-head"><h3>来源</h3></div>
            <ol class="src">${r.sources.map(([title, s], i) =>
              `<li><span class="i">[${i + 1}]</span><span>${esc(title)}<span class="s">${esc(s)}</span></span></li>`).join("")}</ol></section>
        </aside>
      </div>`;
  };

  A.pages.system = function () {
    const s = D.system;
    if (A.live) return `<header class="page-head"><h1 class="page-title">系统</h1><button class="btn" data-refresh-system>刷新记录</button></header>
      <p class="quiet" role="status">${esc(D.systemError || "")}</p>
      <table class="table"><thead><tr><th>来源</th><th>用途</th><th>配置</th><th>最近请求</th><th>记录时间</th><th>耗时</th></tr></thead><tbody>${(s?.sources || []).map(x =>
      `<tr class="static"><td>${esc(x.name)}</td><td>${esc(x.use)}</td><td>${x.configured ? "已配置" : "未配置"}</td>
      <td>${({ok:"成功", error:"失败", unknown:"无记录"})[x.state] || "无记录"}</td><td>${x.checked_at ? esc(new Date(x.checked_at * 1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"})) : "—"}</td><td>${x.elapsed_seconds == null ? "—" : esc(x.elapsed_seconds) + " 秒"}</td></tr>`).join("")}</tbody></table>
      <dl class="summary usage-summary"><div><dt>今日晨报</dt><dd>${s ? `${s.usage.brief} / ${s.limits.brief ?? "不限"}` : "—"}</dd></div><div><dt>今日研究</dt><dd>${s ? `${s.usage.research} / ${s.limits.research ?? "不限"}` : "—"}</dd></div></dl>`;
    return `<header class="page-head"><h1 class="page-title">系统</h1>
        <button class="btn" type="button">重新检查</button></header>
      <div class="note-grid">
        <section><div class="section-head"><h3>数据源</h3></div>
          <table class="table"><thead><tr><th>来源</th><th>用途</th><th>状态</th><th class="r">近 7 天成功率</th><th class="r">平均耗时</th></tr></thead>
          <tbody>${s.sources.map(x => `<tr class="static"><td><b>${esc(x.name)}</b></td><td>${esc(x.use)}</td>` +
            `<td><span class="state ${x.state}">${esc(x.label)}</span></td><td class="r">${x.rate}</td><td class="r">${x.ms}</td></tr>`).join("")}</tbody></table>
        </section>
        <aside><div class="section-head"><h3>设置</h3></div>
          <dl class="facts">${s.settings.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></aside>
      </div>`;
  };

  A.drawer = function (code) {
    const h = holding(code);
    if (!h) return "";
    const evs = events(code);
    if (A.live) {
      const stock = D.stock?.symbol === code ? D.stock : null;
      const bars = (stock?.history?.bars || []).slice(-90);
      return `<button class="drawer-close" type="button" data-close>关闭</button>
        <p class="kicker">${esc(code)}</p><h2 class="page-title">${esc(h.name)}</h2>
        <div class="quote-big"><span class="p">${fixed(h.close)}</span>${signed(h.chg, 2, "%")}<span class="meta">${esc(h.asof || "")}</span></div>
        ${D.stockErrorCode === code ? `<p class="quiet">${esc(D.stockError)} <button class="textbtn" data-retry-stock>重试</button></p>` : ""}
        ${bars.length > 1 ? `<figure class="chart-box">${lineChart(bars.map(bar => Number(bar.close)), 452, 150, "chart")}<figcaption>${esc(bars[0].date)} 至 ${esc(bars.at(-1).date)} · ${bars.length} 个交易日 · ${esc(stock.history.adjustment)}</figcaption></figure>` : `<p class="quiet">${stock ? "未取得日线" : D.stockLoading === code ? "正在读取日线" : ""}</p>`}
        ${stock?.history ? sourceLink(stock.history.url, stock.history.source) : ""}
        ${(stock?.errors || []).map(error => `<p class="quiet">${esc(error.source)}：${esc(error.detail)}</p>`).join("")}
        <div class="section-head"><h3>近期事件</h3></div>
        ${(stock?.events || []).map(event => `<div class="d-ev"><span class="meta">${esc(event.date)} · ${esc(event.type || "公告")}</span>
        <p>${esc(event.title || event.holder || event.metric || event.period || event.share_type || event.type)}</p>
        <dl class="facts">${Object.entries(event).filter(([key, value]) => units[key] && value !== "").map(([key, value]) => `<dt>${esc(units[key])}</dt><dd>${esc(value)}</dd>`).join("")}</dl>
        ${sourceLink(event.url, event.source || "来源原文")}</div>`).join("")}
        ${stock && !stock.events.length ? `<p class="quiet">${stock.errors.length ? "部分来源未取得数据" : "窗口内无事件记录"}</p>` : ""}
        <a class="btn" href="#research">研究这只股票</a>`;
    }
    return `<button class="drawer-close" type="button" data-close>关闭</button>
      <p class="kicker">${board(code)} · ${codeOf(code)}</p>
      <h2 class="page-title">${esc(h.name)}</h2>
      <div class="quote-big"><span class="p">${fixed(h.close)}</span>${signed(h.chg, 2, "%")}<span class="meta">${esc(h.asof || "10-23 15:00")}</span></div>
      <figure class="chart-box">${lineChart(series(code, h.close, 90), 452, 150, "chart")}<figcaption>近 90 日</figcaption></figure>
      <dl class="kv">
        <div><dt>持有</dt><dd>${h.qty.toLocaleString("zh-CN")} 股</dd></div><div><dt>成本</dt><dd>${fixed(h.cost)}</dd></div>
        <div><dt>市值</dt><dd>${fixed(value(h))}</dd></div><div><dt>盈亏</dt><dd>${signed((h.close - h.cost) * h.qty)}</dd></div>
      </dl>
      <div class="section-head"><h3>本期事件</h3></div>
      ${evs.length ? evs.map(e => `<div class="d-ev">${lvl(e.level)} <span class="meta">${esc(e.tag)} · ${esc(e.date)}</span><p>${esc(e.title)}</p></div>`).join("")
        : `<p class="quiet">本期没有新事件。</p>`}
      <a class="btn btn-solid" href="#research">研究这只股票</a>`;
  };
})(App);
