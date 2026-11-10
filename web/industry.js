// Industry map: the holdings' supply chains folded into second-level industries, upstream on the left.
// Edges carry the supplier and customer evidence behind them. Colours follow the timeline: plum for supply,
// blue for sales, red and green only for the day's board change. Zooming out goes on to the field map (fields.js).
(function (A) {
  const { D, esc } = A;
  const NODE_W = 168, ROW = 18, HEAD_H = 30, GAP_Y = 10, TOP = 40;
  let data = null, loading = null, lit = null, picked = null;
  const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;

  // The day's board change lives in the cards only; on the map it was noise.
  function change(code) {
    const v = data.changes?.[code];
    if (v === undefined) return "";
    const n = Number(v);
    return `<span class="ind-chg ${n > 0 ? "up" : n < 0 ? "down" : ""}">今日 ${n > 0 ? "+" : ""}${esc(v)}%</span>`;
  }
  const holdingName = code => data.nodes.flatMap(n => n.holdings).find(h => h.code === code)?.name || code;

  // Columns follow the chain; inside a column, nodes are ordered by where their neighbours sit to cut crossings.
  function layout(W) {
    const cols = Math.max(1, ...data.nodes.map(n => n.layer + 1)), byLayer = [];
    data.nodes.forEach(n => (byLayer[n.layer] ||= []).push(n));
    const near = n => data.edges.flatMap(e => e.from === n.name ? [e.to] : e.to === n.name ? [e.from] : []);
    const rank = new Map();
    byLayer.forEach(col => col.sort((a, b) => b.holdings.length - a.holdings.length || a.name.localeCompare(b.name))
      .forEach((n, i) => rank.set(n.name, i)));
    for (let pass = 0; pass < 3; pass++) byLayer.forEach(col => {
      col.forEach(n => { const ns = near(n).map(m => rank.get(m)); n.bary = ns.length ? ns.reduce((a, b) => a + b, 0) / ns.length : rank.get(n.name); });
      col.sort((a, b) => a.bary - b.bary).forEach((n, i) => rank.set(n.name, i));
    });
    const colW = Math.max(NODE_W + 70, (W - 80) / cols);
    let height = 0;
    byLayer.forEach((col, layer) => {
      let y = TOP;
      col.forEach(n => {
        n.h = HEAD_H + (n.holdings.length ? n.holdings.length * ROW + 4 : 0);
        n.x = 40 + layer * colW + (colW - NODE_W) / 2;
        n.y = y;
        y += n.h + GAP_Y;
      });
      height = Math.max(height, y);
    });
    const width = 40 + cols * colW + 40;
    // Centre short columns against the tallest one.
    byLayer.forEach(col => {
      const last = col.at(-1), shift = (height - (last.y + last.h + GAP_Y)) / 2;
      col.forEach(n => { n.y += shift; });
    });
    const at = new Map(data.nodes.map(n => [n.name, n]));
    return { width, height: height + 20, at };
  }

  function edgePath(a, b) {
    const x1 = a.x + NODE_W, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    if (b.layer > a.layer) { const m = (x1 + x2) / 2; return `M${x1},${y1} C${m},${y1} ${m},${y2} ${x2},${y2}`; }
    // A link back to an earlier column (a loop in the chain) arcs above both nodes.
    const top = Math.min(a.y, b.y) - 30;
    return `M${a.x + NODE_W / 2},${a.y} C${a.x + NODE_W / 2},${top} ${b.x + NODE_W / 2},${top} ${b.x + NODE_W / 2},${b.y}`;
  }

  function svg(W) {
    const { width, height, at } = layout(W);
    const upstream = e => e.evidence.filter(ev => ev.side === "上游").length >= e.evidence.length / 2;
    let edges = "", nodes = "";
    data.edges.forEach((e, i) => {
      const a = at.get(e.from), b = at.get(e.to), w = Math.min(1.4 + e.evidence.length * 1.1, 8);
      edges += `<path class="ind-edge ${upstream(e) ? "sup" : "cus"}" data-ind-edge="${i}" data-ends="${esc(e.from)}|${esc(e.to)}" stroke-width="${w}" d="${edgePath(a, b)}"/>` +
        `<path class="ind-edge-hit" data-ind-edge="${i}" d="${edgePath(a, b)}"/>`;
    });
    data.nodes.forEach(n => {
      // Prominence is exposure: how many holdings this industry reaches directly or one link away.
      const own = n.holdings.length > 0, wide = n.reach.length >= 2, quiet = !own && !n.items.length;
      const flag = n.items.some(i => i.signal);
      nodes += `<g class="ind-node${own ? " own" : ""}${wide ? " wide" : ""}${quiet ? " quiet" : ""}${picked?.node === n.name ? " picked" : ""}" data-ind-node="${esc(n.name)}">` +
        `<rect x="${n.x}" y="${n.y}" width="${NODE_W}" height="${n.h}" rx="6"/>` +
        `<text class="ind-name" x="${n.x + 12}" y="${n.y + 20}">${esc(clip(n.name, 9))}</text>` +
        (n.items.length ? `<circle class="ind-mark${flag ? " flag" : ""}" cx="${n.x + NODE_W - 4}" cy="${n.y + 4}" r="9"/><text class="ind-mark-n" x="${n.x + NODE_W - 4}" y="${n.y + 8}" text-anchor="middle">${n.items.length}</text>` : "") +
        n.holdings.map((h, i) => `<g class="ind-holding" data-ind-stock="${esc(h.code)}"><rect x="${n.x + 10}" y="${n.y + HEAD_H + i * ROW}" width="${NODE_W - 20}" height="${ROW - 3}" rx="3"/>` +
          `<text x="${n.x + 18}" y="${n.y + HEAD_H + i * ROW + 11.5}">${esc(clip(h.name, 10))}</text></g>`).join("") + `</g>`;
    });
    return `<svg class="ind-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet">${edges}${nodes}</svg>`;
  }

  // ---- Cards -----------------------------------------------------------------------------------------

  const link = (url, text) => /^https?:\/\//i.test(url || "") ? `<a class="link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : "";
  const evidence = ev => `<li><p class="meta">${esc(ev.date)} · ${ev.side === "上游" ? `${esc(ev.company)} → ${esc(ev.holding)}` : `${esc(ev.holding)} → ${esc(ev.company)}`}${ev.kind ? ` · ${esc(ev.kind)}` : ""}</p>` +
    `<blockquote class="tl-quote">${esc(clip(ev.quote, 120))}</blockquote>${link(ev.url, "来源原文")}</li>`;

  const KIND = { "站点": "上下游", "事项": "脉络", "行业新闻": "行业新闻" };
  function itemsList(n) {
    return `<ul class="ind-items">${n.items.map((it, i) => `<li class="${picked?.item === i ? "picked" : ""}">
      <p class="meta">${esc(it.date)} · ${KIND[it.kind]}${it.code ? ` · ${esc(holdingName(it.code))}` : ""}</p>
      <button class="ind-item textbtn" data-ind-item="${i}">${esc(it.title)}</button>
      ${it.signal ? `<p class="tl-flag">${esc(it.signal)}</p>` : ""}${it.unverified?.length ? `<p class="unverified">未核对：${esc(it.unverified.join("、"))}</p>` : ""}
      <p class="ind-item-links">${link(it.url, it.source || "来源原文")}${it.chain ? `<button class="textbtn" data-ind-chain="${it.chain}">在脉络中查看</button>` : ""}${it.node ? `<button class="textbtn" data-ind-tnode="${it.node}">在脉络中查看</button>` : ""}</p></li>`).join("")}</ul>`;
  }

  function nodeCard(n) {
    const ins = data.edges.filter(e => e.to === n.name), outs = data.edges.filter(e => e.from === n.name);
    const list = (edges, key) => edges.length ? `<ul class="tl-list">${edges.map(e => `<li><button class="textbtn" data-ind-edge="${data.edges.indexOf(e)}">${esc(e[key])}</button> <span class="meta">${e.evidence.length} 条</span></li>`).join("")}</ul>` : `<p class="meta">无</p>`;
    return `<p class="meta">二级行业 ${change(n.code)}</p><h3 class="tl-card-title">${esc(n.name)}</h3>
      ${n.reach.length ? `<p>连到持仓：${n.reach.map(c => esc(holdingName(c))).join("、")}</p>` : ""}
      ${n.items.length ? `<h4>近期重点</h4>${itemsList(n)}` : ""}
      ${n.holdings.length ? `<h4>持仓</h4><div class="ind-chips">${n.holdings.map(h => `<button class="btn" data-ind-stock="${esc(h.code)}">${esc(h.name)}</button>`).join("")}</div>` : ""}
      <h4>上游行业</h4>${list(ins, "from")}<h4>下游行业</h4>${list(outs, "to")}
      ${n.inner.length ? `<h4>同行业往来</h4><ul class="ind-evidence">${n.inner.map(evidence).join("")}</ul>` : ""}
      ${n.companies.length ? `<h4>相关公司</h4><p>${n.companies.map(esc).join("、")}</p>` : ""}`;
  }

  function edgeCard(e) {
    return `<p class="meta">产业链 · ${e.evidence.length} 条证据</p><h3 class="tl-card-title">${esc(e.from)} → ${esc(e.to)}</h3>
      <ul class="ind-evidence">${e.evidence.map(evidence).join("")}</ul>`;
  }

  function card() {
    const box = document.getElementById("ind-card");
    if (!box) return;
    let body = "";
    if (picked?.node) body = nodeCard(data.nodes.find(n => n.name === picked.node));
    else if (picked?.edge !== undefined) body = edgeCard(data.edges[picked.edge]);
    box.hidden = !body;
    box.innerHTML = body ? `<button class="tl-close" data-ind-close aria-label="关闭">×</button>${body}` : "";
  }

  // ---- Drawing and input -----------------------------------------------------------------------------

  function draw() {
    const box = document.getElementById("tl-industry");
    if (!box) return;
    const map = box.querySelector(".ind-map");
    if (!data) { map.innerHTML = `<p class="quiet ind-empty">${loading ? "正在读取" : esc(D.indError || "")}</p>`; return; }
    if (!data.nodes.length) { map.innerHTML = `<p class="quiet ind-empty">持仓还没有整理上下游</p>`; return; }
    map.innerHTML = svg(map.clientWidth || 1000);
    card();
    // A redraw starts unlit; the picked node or edge stays lit until the pointer moves elsewhere.
    // A hover change still waiting to settle belongs to the old drawing and must not undo the pick.
    clearTimeout(settle);
    wanted = undefined;
    lit = null;
    if (picked?.item !== undefined) return path(data.nodes.find(n => n.name === picked.node));
    if (picked) light(map.querySelector(picked.node ? `[data-ind-node="${CSS.escape(picked.node)}"]` : `.ind-edge[data-ind-edge="${picked.edge}"]`));
  }

  async function load(force) {
    if (loading || data && !force) return draw();
    loading = true; draw();
    try { data = await A.api("/api/industry"); D.indError = ""; }
    catch (err) { D.indError = err.message; }
    finally { loading = false; draw(); }
  }

  function light(target) {
    const svg = document.querySelector("#tl-industry .ind-svg");
    if (!svg) return;
    let names = null;
    const node = target?.closest?.("[data-ind-node]"), edge = target?.closest?.("[data-ind-edge]");
    if (node) {
      const name = node.dataset.indNode;
      names = new Set([name, ...data.edges.flatMap(e => e.from === name ? [e.to] : e.to === name ? [e.from] : [])]);
    } else if (edge && edge.dataset.ends) names = new Set(edge.dataset.ends.split("|"));
    else if (edge) { const e = data.edges[Number(edge.dataset.indEdge)]; names = new Set([e.from, e.to]); }
    const key = names ? [...names].sort().join("|") : null;
    if (key === lit) return;
    lit = key;
    svg.classList.toggle("lit", Boolean(names));
    svg.querySelectorAll(".on").forEach(el => el.classList.remove("on"));
    if (!names) return;
    svg.querySelectorAll("[data-ind-node]").forEach(el => el.classList.toggle("on", names.has(el.dataset.indNode)));
    svg.querySelectorAll(".ind-edge").forEach(el => { const [a, b] = el.dataset.ends.split("|"); el.classList.toggle("on", names.has(a) && names.has(b) && (!node || a === node.dataset.indNode || b === node.dataset.indNode)); });
  }

  // A picked key item lights the way from its industry to the holdings it reaches.
  function path(n) {
    const svg = document.querySelector("#tl-industry .ind-svg"), it = n.items[picked.item];
    const codes = it.code ? [it.code] : n.reach;
    const homes = new Set(data.nodes.filter(m => m.holdings.some(h => codes.includes(h.code))).map(m => m.name));
    lit = "path";
    svg.classList.add("lit");
    svg.querySelectorAll("[data-ind-node]").forEach(el => el.classList.toggle("on", el.dataset.indNode === n.name || homes.has(el.dataset.indNode)));
    svg.querySelectorAll(".ind-edge").forEach(el => { const [a, b] = el.dataset.ends.split("|");
      el.classList.toggle("on", (a === n.name && homes.has(b)) || (b === n.name && homes.has(a))); });
  }

  // Hovering a node with key items shows them; the card holds the rest.
  function tip(e) {
    const box = document.getElementById("ind-tip"), node = e?.target.closest?.("[data-ind-node]");
    const n = node && data?.nodes.find(m => m.name === node.dataset.indNode);
    if (!box) return;
    box.hidden = !n?.items.length;
    if (box.hidden) return;
    box.innerHTML = `<b>${esc(n.name)}</b>` + n.items.map(it => `<p><span class="meta">${esc(it.date.slice(5))} ${KIND[it.kind]}</span> ${esc(clip(it.title, 46))}</p>`).join("");
    const r = document.getElementById("tl-industry").getBoundingClientRect();
    box.style.left = Math.min(e.clientX - r.left + 14, r.width - 300) + "px";
    box.style.top = Math.min(e.clientY - r.top + 14, r.height - box.offsetHeight - 8) + "px";
  }

  // Highlights follow the pointer only once it settles: a quick sweep across the map must not flash the whole graph.
  // Moving from one lit item to the next switches directly; gaps between nodes do not unlight.
  let settle = null, wanted, lastMove = null;
  function hover(e) {
    const el = e.target.closest?.("[data-ind-node],[data-ind-edge]") || null;
    lastMove = e;
    if (el === wanted) return;
    wanted = el;
    clearTimeout(settle);
    settle = setTimeout(() => { light(el); tip(lastMove); }, el ? (lit ? 60 : 120) : 200);
  }

  document.addEventListener("pointermove", e => {
    if (!e.target.closest?.("#tl-industry")) return;
    const box = document.getElementById("ind-tip");
    if (box && !box.hidden && wanted === e.target.closest?.("[data-ind-node]")) tip(e);
    // While a key item's path is picked, the map keeps that view; hovering only changes it once nothing is picked.
    if (lit !== "path") hover(e);
  });
  document.addEventListener("pointerleave", () => light(null));

  document.addEventListener("click", e => {
    if (!e.target.closest?.("#tl-industry")) return;
    const stock = e.target.closest("[data-ind-stock]");
    if (stock) return A.timelineShowStock(stock.dataset.indStock);
    if (e.target.closest("[data-ind-close]")) { picked = null; return draw(); }
    const chain = e.target.closest("[data-ind-chain]"), tnode = e.target.closest("[data-ind-tnode]");
    if (chain) return A.timelineShowChain(Number(chain.dataset.indChain));
    if (tnode) return A.timelineShowNode(Number(tnode.dataset.indTnode));
    const item = e.target.closest("[data-ind-item]");
    if (item) { picked = { node: picked.node, item: Number(item.dataset.indItem) }; return draw(); }
    if (e.target.closest(".ind-card")) return;
    const node = e.target.closest("[data-ind-node]"), edge = e.target.closest("[data-ind-edge]");
    if (node) picked = { node: node.dataset.indNode };
    else if (edge) picked = { edge: Number(edge.dataset.indEdge) };
    else if (e.target.closest(".ind-map")) picked = null;
    else return;
    draw();
  });

  window.addEventListener("resize", () => { if (D.tlMode === "industry") draw(); });

  A.industry = { load, draw, markup: () => `<div class="tl-industry" id="tl-industry"${D.tlMode === "industry" ? "" : " hidden"}>
      <div class="ind-map"></div><aside class="tl-card ind-card" id="ind-card" hidden></aside><div class="tl-tip ind-tip" id="ind-tip" hidden></div></div>` };
})(App);
