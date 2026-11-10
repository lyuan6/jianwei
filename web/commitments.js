// Commitment ledger: what companies and shareholders promised, and whether later announcements show it was kept.
(function (A) {
  const { D, esc, holding } = A;
  const GROUPS = [["all", "全部"], ["进行中", "进行中"], ["已兑现", "已兑现"], ["problem", "延期或变更"], ["failed", "未兑现或终止"], ["到期未披露", "到期未披露"]];
  const PROBLEM = ["延期", "变更", "部分兑现"], FAILED = ["未兑现", "终止"];
  const inGroup = (item, group) => group === "all" || item.state === group
    || group === "problem" && PROBLEM.includes(item.state) || group === "failed" && FAILED.includes(item.state);
  const tone = state => state === "已兑现" ? "cm-kept" : state === "进行中" ? "cm-open" : "cm-flag";
  const link = (url, text) => /^https?:\/\//i.test(url || "") ? `<a class="link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : "";

  A.commitmentState = item => `<span class="cm-state ${tone(item.state)}">${esc(item.state)}</span>`;

  const ORDER = ["到期未披露", "未兑现", "终止", "延期", "变更", "部分兑现", "进行中", "已兑现"];
  const clip = (text, n) => text.length > n ? text.slice(0, n - 1) + "…" : text;
  const nameOf = code => holding(code).name || code;
  const opened = () => D.cmOpened || (D.cmOpened = new Set());
  const closed = () => D.cmClosed || (D.cmClosed = new Set());

  A.commitmentBody = item => `<p class="cm-content">${esc(item.content)}</p>
    ${item.amount ? `<p class="meta">数额：${esc(item.amount)}</p>` : ""}
    ${item.deadline_text || item.deadline ? `<p class="meta">期限：${esc(item.deadline_text || item.deadline)}${item.deadline && item.deadline_text ? `（${esc(item.deadline)}）` : ""}</p>` : ""}
    ${item.outcome ? `<p>${esc(item.outcome)}</p>` : ""}
    ${item.unverified?.length ? `<p class="unverified">未核对：${esc(item.unverified.join("、"))}</p>` : ""}
    <p class="cm-sources">${link(item.source_url, item.announced + " 承诺出处")}${item.evidence.map(n => link(n.url, n.date + " 进展")).join("")}</p>`;

  function row(item) {
    const key = "item:" + item.id;
    return `<tr class="static" data-level="${esc(item.state)}">
      <td><button class="stock-link textbtn" data-cm-thread="${item.thread_id}" data-cm-code="${esc(item.code)}">${esc(item.thread_title)}</button></td>
      <td><details class="cm-row" data-cm-open="${key}"${opened().has(key) ? " open" : ""}><summary><span class="meta">${esc(item.kind)}</span> ${esc(clip(item.content, 46))}</summary>${A.commitmentBody(item)}
        <p><button class="textbtn" type="button" data-cm-ask-item="${item.id}">分析这条承诺</button></p></details></td>
      <td class="cm-deadline">${esc(item.deadline || "—")}</td>
      <td>${A.commitmentState(item)}${item.alert ? `<br><small>${esc(item.alert)}</small>` : ""}</td></tr>`;
  }

  const byPriority = (a, b) => ORDER.indexOf(a.state) - ORDER.indexOf(b.state) || (a.deadline || "9").localeCompare(b.deadline || "9");

  function company(code, items, group) {
    const key = "code:" + code, shown = items.filter(item => inGroup(item, group)).sort(byPriority);
    // Companies with an alert start open, until the reader folds them.
    const unfolded = group !== "all" || opened().has(key) || items.some(item => item.alert) && !closed().has(key);
    const count = state => items.filter(item => item.state === state).length;
    // One cell per commitment: the site's CSP blocks inline styles, and counting cells reads better than a ratio anyway.
    const bar = ["cm-kept", "cm-open", "cm-flag"].flatMap(kind => items.filter(item => tone(item.state) === kind).map(() => `<i class="${kind}"></i>`)).join("");
    const stats = ORDER.filter(count).map(state => `<span class="${tone(state)}">${esc(state)} ${count(state)}</span>`).join("");
    return `<details class="cm-company" data-cm-open="${key}"${unfolded ? " open" : ""}>
      <summary><span class="cm-co-name"><b>${esc(nameOf(code))}</b><span class="code">${esc(code)}</span></span>
        <span class="cm-bar" aria-hidden="true">${bar}</span>
        <span class="cm-co-stats"><span>${items.length} 项</span>${stats}</span>
        <button class="textbtn" type="button" data-cm-ask-code="${esc(code)}">分析</button></summary>
      <table class="table cm-table"><thead><tr><th>事项</th><th>承诺与进展</th><th>期限</th><th>状态</th></tr></thead>
        <tbody>${shown.map(row).join("")}</tbody></table></details>`;
  }

  // Holdings order stays put when a filter hides some companies, so the list doesn't reshuffle under the reader.
  function companies(items, group) {
    const codes = [...new Set([...D.holdings.map(h => h.code), ...items.map(item => item.code)])];
    return codes.map(code => [code, items.filter(item => item.code === code)])
      .filter(([, own]) => own.some(item => inGroup(item, group)));
  }

  const overall = "对比我的全部持仓的承诺兑现情况：哪些公司兑现得多，哪些有延期、变更或到期未披露，哪些快到期，并给出后续预判。每家公司写两三句。";
  const ofCompany = code => `分析${nameOf(code)}的承诺兑现情况：已兑现、延期或变更、快到期的各有哪些，这些记录说明了什么，并给出后续预判。`;
  const ofItem = item => `分析${nameOf(item.code)}这条承诺的进展，接下来要观察什么，并给出后续预判：${clip(item.content, 300)}（期限：${item.deadline || item.deadline_text || "未写明"}，当前状态：${item.state}）`;

  function drawer(items, open) {
    const codes = companies(items, "all").map(([code]) => code);
    return `<aside class="tree-analysis cm-analysis${open ? " open" : ""}"><div class="tree-analysis-head"><h3>分析</h3><button class="tree-x" data-cm-analysis-close aria-label="关闭">×</button></div>
      <div class="tree-presets"><button type="button" data-cm-ask-all>全部持仓对比</button>${codes.map(code => `<button type="button" data-cm-ask-code="${esc(code)}">${esc(nameOf(code))}</button>`).join("")}</div>
      <form class="ask" data-research-form><input name="question" placeholder="输入问题" aria-label="分析问题" value="${esc(D.question || "")}"><button type="submit">提问</button></form>
      <div id="research-content" aria-live="polite">${A.researchContent()}</div></aside>`;
  }

  A.pages.commitments = function () {
    if (!A.live) return `<header class="page-head"><h1 class="page-title">承诺</h1></header><p class="quiet">登录后查看。</p>`;
    const data = D.cm, group = D.cmGroup || "all";
    if (!data) return `<header class="page-head"><h1 class="page-title">承诺</h1></header><p class="quiet">${esc(D.cmError || "正在读取")}</p>`;
    const items = data.items, count = g => items.filter(item => inGroup(item, g)).length;
    const alerts = items.filter(item => item.alert);
    const missing = data.missing.map(nameOf);
    const open = !!D.cmAnalysis;
    return `<div class="cm-page${open ? " analysis-open" : ""}"><header class="page-head"><h1 class="page-title">承诺</h1><div class="holding-actions"><button class="btn${open ? " on" : ""}" type="button" data-cm-analyze>分析</button></div></header>
      <dl class="summary"><div><dt>承诺</dt><dd>${items.length}</dd></div><div><dt>已兑现</dt><dd>${count("已兑现")}</dd></div>
        <div><dt>延期、变更或未兑现</dt><dd>${count("problem") + count("failed")}</dd></div><div><dt>到期未披露</dt><dd>${count("到期未披露")}</dd></div></dl>
      ${missing.length ? `<p class="quiet">${esc(missing.join("、"))} 还没有脉络，<a class="link" href="#timeline">去脉络页整理</a></p>` : ""}
      ${alerts.length ? `<section class="cm-alerts"><div class="section-head"><h3>需要留意</h3><span class="meta">${alerts.length} 条</span></div>
        <ul>${alerts.map(item => `<li><button class="textbtn" data-cm-thread="${item.thread_id}" data-cm-code="${esc(item.code)}">${esc(nameOf(item.code))}</button>
          ${A.commitmentState(item)} <span class="cm-alert-text">${esc(clip(item.content, 50))}</span> <small>${esc(item.alert)}</small></li>`).join("")}</ul></section>` : ""}
      <div class="chips cm-chips">${GROUPS.map(([key, name]) => `<button class="chip${key === group ? " on" : ""}" data-cm-group="${key}">${name}<span class="n">${count(key)}</span></button>`).join("")}</div>
      <div class="cm-companies">${companies(items, group).map(([code, own]) => company(code, own, group)).join("")}</div>
      ${drawer(items, open)}</div>`;
  };

  A.loadCommitments = async function () {
    try {
      D.cm = await A.api("/api/commitments"); D.cmError = "";
      document.getElementById("nav-commitments").textContent = D.cm.items.filter(item => item.alert).length || "";
    } catch (err) { D.cmError = err.message; }
    if (location.hash.split("/")[0] === "#commitments") A.rerender();
  };

  // Toggled on the live elements rather than through a rerender, which would swap them out and skip the slide.
  function setAnalysis(open) {
    D.cmAnalysis = open;
    document.querySelector(".cm-page")?.classList.toggle("analysis-open", open);
    document.querySelector(".cm-analysis")?.classList.toggle("open", open);
    document.querySelector("[data-cm-analyze]")?.classList.toggle("on", open);
  }

  // Asking rerenders the page, so a closed drawer finishes sliding in first.
  function ask(question) {
    if (D.cmAnalysis) return A.askResearch(question);
    setAnalysis(true);
    setTimeout(() => A.askResearch(question), 320);
  }

  document.addEventListener("click", e => {
    const chip = e.target.closest("[data-cm-group]");
    if (chip) { D.cmGroup = chip.dataset.cmGroup; return A.rerender(); }
    const thread = e.target.closest("[data-cm-thread]");
    if (thread) return A.openThread(thread.dataset.cmCode, Number(thread.dataset.cmThread));
    if (e.target.closest("[data-cm-analyze]")) return setAnalysis(!D.cmAnalysis);
    if (e.target.closest("[data-cm-analysis-close]")) return setAnalysis(false);
    if (e.target.closest("[data-cm-ask-all]")) return ask(overall);
    const code = e.target.closest("[data-cm-ask-code]");
    if (code) {
      // This button sits in the company's summary row, where a click would also fold the row.
      e.preventDefault();
      return ask(ofCompany(code.dataset.cmAskCode));
    }
    const one = e.target.closest("[data-cm-ask-item]");
    if (one) ask(ofItem(D.cm.items.find(item => String(item.id) === one.dataset.cmAskItem)));
  });

  // Long source lists collapse in every analysis view (see citations in pages.js).
  document.addEventListener("click", e => {
    const more = e.target.closest("[data-cite-more]");
    if (more) more.nextElementSibling.hidden = !more.nextElementSibling.hidden;
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && D.cmAnalysis && location.hash.split("/")[0] === "#commitments") setAnalysis(false);
  });

  // Toggle events don't bubble. Filtered views force every company open, and that shouldn't count as the reader's choice.
  document.addEventListener("toggle", e => {
    const key = e.target.dataset?.cmOpen;
    if (!key || (D.cmGroup || "all") !== "all" && key.startsWith("code:")) return;
    if (e.target.open) { opened().add(key); closed().delete(key); } else { opened().delete(key); closed().add(key); }
  }, true);
})(App);
