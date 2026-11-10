// Portfolio timeline canvas: every holding is a block of 事项 lanes on one shared time axis.
// The view pans and zooms without bounds; detail appears as the lanes grow on screen,
// and at the deepest level milestone nodes open into small cards.
//
// Rendering has two parts. The content layer (lanes, nodes, cards, links, prices) is drawn with a margin
// around the screen and, while the view moves, only shifted and scaled by the GPU; it is redrawn once the
// view settles or the margin runs out, and the new drawing fades in over the old one. The overlay (time axis,
// sticky names, today and playback lines) is light and redrawn every frame.
(function (A) {
  const { D, esc, fixed, holding } = A;
  const DAY = 86400000, DAYPX = 4, HEAD = 96, LANE = 72, PRICE = 56, GAP = 64, EMPTY = 72, AXIS = 32, CHAIN = 46;
  // Wings: suppliers left of the time axis, the revenue split and customers right of it (world units).
  // Wings sit beside the time axis in world units, so they scale with it.
  const WING_L = 440, WING_R = 300, ROWH = 34, CATH = 24, BAR = 48;
  // Left wing: the stage bar just before the time axis and the supplier dots fanned out to its left.
  const LBAR = -82, DOT = -140, STAGES = ["原材料", "材料", "零部件", "设备", "服务"];
  const THEMES = ["投资扩产", "资本运作", "股东变动", "股权激励", "经营业绩", "诉讼监管", "公司治理", "其他"];
  const CARD_W = 196, CARD_H = 74;
  // Detail levels by on-screen lane height: [enter, leave]. The gap keeps them from flickering at the edge.
  const LEVELS = { nodes: [14, 12], titles: [64, 56], detail: [90, 80], cards: [150, 136] };
  const view = { k: 1, tx: 0, ty: 0, ready: false };
  const lod = { nodes: false, titles: false, detail: false, cards: false };
  const pointers = new Map();
  let mapOn = true;
  try { mapOn = localStorage.getItem("tl-map") !== "off"; } catch {}
  let panelOpen = false, layers = [], idle = null, drag = null, pinch = null, pressed = null, motion = null, timer = null, layout = null, play = null;

  const t = d => Date.parse(d + "T00:00:00Z");
  const iso = ms => new Date(ms).toISOString().slice(0, 10);
  const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const name = code => holding(code).name || code;
  const store = () => D.tlData || (D.tlData = {});
  const official = th => th.nodes.filter(n => n.kind === "公告");
  const theme = th => `th-${Math.max(0, THEMES.indexOf(th.theme))}`;
  const sorted = d => (d.threads || []).filter(th => th.nodes.length)
    .sort((a, b) => a.nodes[0].date.localeCompare(b.nodes[0].date) || a.id - b.id);
  const today = () => iso(Date.now());

  function world() {
    const ready = D.holdings.map(h => store()[h.code]).filter(d => d?.initialized);
    let lo = Infinity, hi = -Infinity;
    ready.forEach(d => {
      const soon = t(d.until) + 90 * DAY;
      lo = Math.min(lo, t(d.since));
      hi = Math.max(hi, soon, ...(d.commitments || []).map(c => c.deadline ? t(c.deadline) : 0).filter(v => v <= soon));
    });
    if (!isFinite(lo)) { lo = Date.now() - 365 * DAY; hi = Date.now() + 90 * DAY; }
    lo -= 14 * DAY;
    let y = 0;
    const blocks = D.holdings.map(h => {
      const d = store()[h.code], lanes = d?.initialized ? sorted(d) : [];
      // Suppliers and customers run as two rails above and below the company's own 事项 lines.
      const chain = d?.initialized ? d.chain?.links || [] : [];
      const ups = chain.filter(l => l.side === "上游"), downs = chain.filter(l => l.side === "下游");
      const up = ups.length ? CHAIN : 0, down = downs.length ? CHAIN : 0;
      const block = { code: h.code, data: d, lanes, ups, downs, up, down, top: y };
      block.body = d?.initialized ? wings(block, up + lanes.length * LANE + down) : 0;
      block.height = HEAD + (d?.initialized ? block.body + PRICE : EMPTY);
      y += block.height + GAP;
      return block;
    });
    const width = (hi - lo) / DAY * DAYPX, at = day => (t(day) - lo) / DAY * DAYPX;
    // Each revenue bar stands just after the stock's last date on the canvas (today or a later deadline).
    blocks.forEach(b => {
      const days = [b.data?.until, ...(b.data?.commitments || []).map(c => c.deadline).filter(day => day && t(day) <= hi)].filter(Boolean);
      b.bx = days.length ? Math.max(...days.map(at)) + 40 : width;
    });
    const wide = blocks.filter(b => b.ups.length || b.downs.length);
    return { t0: lo, t1: hi, blocks, width, height: Math.max(0, y - GAP), x0: wide.length ? -WING_L : -40,
      x1: Math.max(width + 40, ...wide.map(b => b.bx + WING_R)) };
  }

  const groupBy = (list, key) => list.reduce((m, l) => m.set(l[key] || "", [...(m.get(l[key] || "") || []), l]), new Map());

  // Places the wing rows of one block in world units and returns the height its body needs.
  // Places the wing rows of one block in world units and returns the height its body needs. Both sides are a bar
  // with names fanned out beside it: on the right the revenue split, on the left the suppliers counted by production stage.
  function wings(block, lanes) {
    const top = block.top + HEAD + 10;
    const order = st => { const i = STAGES.indexOf(st); return i < 0 ? STAGES.length : i; };
    const stages = [...groupBy(block.ups, "stage")].sort(([a], [b]) => order(a) - order(b))
      .map(([stage, list]) => [stage, list.sort((a, b) => (a.category || "").localeCompare(b.category || ""))]);
    const segs = (block.data.chain?.segments || []).filter(g => Number(g.pct) > 0);
    const groups = [...groupBy(block.downs, "segment")];
    const need = (n, rows) => n * CATH + rows * ROWH + 20;
    const body = Math.max(lanes, need(stages.length, block.ups.length), need(groups.length, block.downs.length),
      block.downs.length || block.ups.length ? 240 : 0);
    const height = body - 20;
    // Stage segments are as tall as their share of suppliers: a count, not purchase money.
    let y0 = top;
    block.stageSegs = stages.map(([stage, list]) => { const seg = { name: stage || "其他", count: list.length, list, y: y0,
      h: height * list.length / block.ups.length }; y0 += seg.h; return seg; });
    const total = segs.reduce((a, g) => a + Number(g.pct), 0) || 1;
    y0 = top;
    block.segs = segs.map(g => { const seg = { ...g, y: y0, h: height * Number(g.pct) / total }; y0 += seg.h; return seg; });
    // Names sit beside their segment; groups are pushed down so they never overlap.
    const place = (seg, list) => { cy = Math.max(cy, seg ? seg.y : cy); const head = cy; cy += CATH;
      list.forEach(l => { l.wy = cy + ROWH / 2; cy += ROWH; }); return head; };
    let cy = top;
    block.stageSegs.forEach(seg => { seg.head = place(seg, seg.list); });
    const left = cy;
    cy = top;
    block.groups = groups.sort(([a], [b]) => rank(block.segs, a) - rank(block.segs, b)).map(([name, list]) => {
      const seg = block.segs.find(g => g.name === name);
      return { name, seg, y: place(seg, list) };
    });
    return Math.max(body, cy - top + 20, left - top + 20);
  }
  const rank = (segs, name) => { const i = segs.findIndex(g => g.name === name); return i < 0 ? 99 : i; };

  const X = day => (t(day) - layout.t0) / DAY * DAYPX;
  const sx = wx => wx * view.k + view.tx;
  const sy = wy => wy * view.k + view.ty;
  const laneY = (block, i) => block.top + HEAD + block.up + i * LANE + LANE / 2;
  const railY = (block, side) => side === "上游" ? block.top + HEAD + CHAIN / 2 - 6
    : block.top + HEAD + block.up + block.lanes.length * LANE + CHAIN / 2 + 2;

  function nodes() {
    return layout.blocks.flatMap(b => b.lanes.flatMap((th, i) => th.nodes.map(n => ({ ...n, code: b.code, thread: th, block: b, lane: i }))));
  }
  const findNode = id => nodes().find(n => n.id === id);
  const findChain = id => layout.blocks.flatMap(b => [...b.ups, ...b.downs].map(link => ({ link, block: b }))).find(x => x.link.id === id);
  const findThread = id => layout.blocks.flatMap(b => b.lanes.map((th, i) => ({ th, block: b, lane: i }))).find(x => x.th.id === id);
  const focusCode = () => D.tlFocus && findThread(D.tlFocus)?.block.code;
  const dimmed = (th, block) => D.tlFocus && D.tlFocus !== th.id && focusCode() === block.code;

  const laneSize = () => LANE * view.k;
  // Marks and offsets keep their size while lanes are roomy and shrink with the gap between lanes when zoomed out,
  // so nothing reaches into the lane above.
  const fit = (px, share) => Math.min(px, laneSize() * share);
  function levels() {
    const size = laneSize();
    for (const [key, [enter, leave]] of Object.entries(LEVELS)) lod[key] = size >= (lod[key] ? leave : enter);
  }
  const detail = th => D.tlFocus === th.id || lod.detail;

  // During playback only what had happened by the cursor date is drawn; new nodes grow in.
  const visible = day => !play || t(day) <= play.at;
  const grow = day => !play ? 1 : Math.min(1, (play.at - t(day)) / (10 * DAY));
  const pop = p => p >= 1 ? 1 : 1 + 0.6 * Math.sin(p * Math.PI) - (1 - p) * 0.9;

  function position(n) {
    const y = sy(laneY(n.block, n.lane));
    return { x: sx(X(n.date)), y: n.kind === "新闻" && lod.nodes ? y - fit(20, .4) : y };
  }

  function clusters(list) {
    const out = [];
    list.forEach(n => {
      const x = sx(X(n.date)), last = out.at(-1);
      if (last && x - last.x < 12) last.nodes.push(n); else out.push({ x, nodes: [n] });
    });
    return out;
  }

  function wrap(text, width, lines) {
    const out = [];
    for (let i = 0; i < text.length && out.length < lines; i += width) out.push(text.slice(i, i + width));
    if (out.length === lines && text.length > width * lines) out[lines - 1] = out[lines - 1].slice(0, -1) + "…";
    return out;
  }

  function card(n, x, y) {
    const selected = D.tlSelected?.node === n.id;
    const title = wrap(n.title, 14, 2), summary = n.summary ? wrap(n.summary, 16, 1) : [];
    const left = x - CARD_W / 2, top = y + 14;
    return `<g class="tl-mini-card${selected ? " selected" : ""}" data-tl-node="${n.id}">
      <line class="tl-card-stem" x1="${x}" x2="${x}" y1="${y + 7}" y2="${top}"/>
      <rect x="${left}" y="${top}" width="${CARD_W}" height="${CARD_H}" rx="3"/>
      <text class="tl-card-date" x="${left + 10}" y="${top + 16}">${esc(n.date)} · ${esc(n.kind)}</text>
      ${title.map((line, i) => `<text class="tl-card-line" x="${left + 10}" y="${top + 34 + i * 16}">${esc(line)}</text>`).join("")}
      ${summary.map(line => `<text class="tl-card-sub" x="${left + 10}" y="${top + 34 + title.length * 16 + 2}">${esc(line)}</text>`).join("")}</g>`;
  }

  // ---- Content layer -------------------------------------------------------------------------------

  function lane(th, i, block, area) {
    const y = sy(laneY(block, i)), focused = D.tlFocus === th.id;
    const all = official(th), nodes = all.filter(n => visible(n.date));
    if (!nodes.length || y < area.y0 - 40 || y > area.y1 + 120) return "";
    const end = play ? Math.min(play.at, t(block.data.until)) : t(block.data.until);
    const x0 = sx(X(nodes[0].date)), x1 = sx(X(nodes.at(-1).date)), xe = Math.max(x1, sx(X(iso(end))));
    const finished = !play || nodes.length === all.length;
    let out = `<g class="tl-lane ${theme(th)}${focused ? " focused" : ""}${dimmed(th, block) ? " dim" : ""}">`;
    out += `<line class="tl-hit" data-tl-thread="${th.id}" x1="${x0}" x2="${xe}" y1="${y}" y2="${y}"/>`;
    out += `<line class="tl-trunk" x1="${x0}" x2="${x1}" y1="${y}" y2="${y}"/>`;
    if (th.status === "进行中" || !finished) out += `<line class="tl-open" x1="${x1}" x2="${xe}" y1="${y}" y2="${y}"/>`;
    if (!lod.nodes && !focused) return out + "</g>";
    const tick = fit(7, .28);
    if (finished) out += th.status === "进行中" ? `<circle class="tl-end-open" cx="${xe}" cy="${y}" r="${fit(5, .2)}"/>`
      : `<line class="tl-end ${th.status === "已完成" ? "" : "broken"}" x1="${x1 + 9}" x2="${x1 + 9}" y1="${y - tick}" y2="${y + tick}"/>`;
    if (!play) (block.data.commitments || []).filter(c => c.thread_id === th.id && c.deadline && t(c.deadline) <= layout.t1).forEach(c => {
      const x = sx(X(c.deadline)), tone = c.state === "已兑现" ? "kept" : c.state === "进行中" ? "open" : "flagged";
      const d = fit(7, .28);
      out += `<path class="tl-deadline ${tone}" data-tl-deadline="${c.id}" d="M${x},${y - d} l${d * .86},${d} l${-d * .86},${d} l${-d * .86},${-d}z"/>`;
    });
    // Third-party reports branch off the trunk as hollow nodes above it.
    th.nodes.filter(n => n.kind === "新闻" && visible(n.date)).forEach(n => {
      const x = sx(X(n.date)), r = fit(detail(th) ? 5.5 : 4.5, .17) * pop(grow(n.date)), h = fit(20, .4);
      out += `<path class="tl-branch" d="M${x - 22},${y} C${x - 8},${y} ${x - 4},${y - h} ${x},${y - h}"/>` +
        `<circle class="tl-news${D.tlSelected?.node === n.id ? " selected" : ""}" data-tl-node="${n.id}" cx="${x}" cy="${y - h}" r="${r}"/>`;
    });
    const shown = detail(th) ? nodes : nodes.filter((n, j) => n.major || j === 0 || j === nodes.length - 1);
    const rows = [-Infinity, -Infinity];
    let lastCard = -Infinity;
    clusters(shown).forEach(({ x, nodes: group }) => {
      if (x < area.x0 - CARD_W || x > area.x1 + CARD_W) return;
      const n = group.find(m => m.major) || group[0];
      const selected = group.some(m => D.tlSelected?.node === m.id || D.tlFrom === m.id);
      const r = Math.max(0.5, (n.major ? fit(6.5, .26) : fit(3.6, .16)) * pop(grow(n.date)));
      out += `<circle class="tl-node${n.major ? " major" : ""}${selected ? " selected" : ""}" data-tl-node="${n.id}" cx="${x}" cy="${y}" r="${r}"/>`;
      if (n.major) out += `<circle class="tl-core" cx="${x}" cy="${y}" r="${r * 0.32}"/>`;
      if (group.length > 1 && laneSize() >= 28) out += `<text class="tl-count" x="${x + 9}" y="${y - 9}">${group.length}</text>`;
      if (!n.major) return;
      if (lod.cards && x - CARD_W / 2 > lastCard + 8) { out += card(n, x, y); lastCard = x + CARD_W / 2; return; }
      if (lod.cards || !lod.titles && !focused) return;
      // Node titles sit in two rows under the trunk and are skipped where they would overlap.
      const text = clip(n.title, 16), half = text.length * 5.5, row = rows.findIndex(edge => x - half > edge + 8);
      if (row < 0) return;
      rows[row] = x + half;
      out += `<text class="tl-note" x="${x}" y="${y + (row ? 34 : 21)}" text-anchor="middle">${esc(text)}</text>`;
    });
    return out + "</g>";
  }

  // Other companies' announcements and news join the company's map like interchange stations on a rail.
  function rail(block, side, area) {
    const list = side === "上游" ? block.ups : block.downs;
    if (!list.length) return "";
    const y = sy(railY(block, side)), dir = side === "上游" ? 1 : -1, cls = side === "上游" ? "tl-up" : "tl-down";
    if (y < area.y0 - 40 || y > area.y1 + 40) return "";
    let out = `<g class="tl-rail ${cls}"><line class="tl-rail-line" x1="${sx(X(block.data.since))}" x2="${sx(X(block.data.until))}" y1="${y}" y2="${y}"/>` +
      (laneSize() >= 40 ? `<text class="tl-rail-name" x="${sx(X(block.data.since))}" y="${y - 6}">${side} ${list.length}</text>` : "");
    if (!lod.nodes) return out + "</g>";
    const shown = list.filter(l => t(l.date) >= layout.t0 && visible(l.date)).sort((a, b) => a.date.localeCompare(b.date));
    const rows = [-Infinity, -Infinity];
    shown.forEach(l => {
      const x = sx(X(l.date)), selected = D.tlSelected?.chain === l.id, r = Math.max(0.5, fit(5, .17) * pop(grow(l.date)));
      if (x < area.x0 - 60 || x > area.x1 + 60) return;
      out += `<line class="tl-transfer${l.status === "确认" ? "" : " weak"}" x1="${x}" x2="${x}" y1="${y}" y2="${y + dir * (CHAIN / 2) * view.k}"/>` +
        `<rect class="tl-station${selected ? " selected" : ""}" data-tl-chain="${l.id}" x="${x - r}" y="${y - r}" width="${2 * r}" height="${2 * r}" rx="${r}"/>`;
      if (l.signal) out += `<circle class="tl-signal" cx="${x + 6}" cy="${y - 6}" r="2.6"/>`;
      if (!lod.titles) return;
      const text = clip(l.name, 8), half = text.length * 5.5, row = rows.findIndex(edge => x - half > edge + 6);
      if (row < 0) return;
      rows[row] = x + half;
      out += `<text class="tl-note" x="${x}" y="${y + (side === "上游" ? -10 - row * 12 : 18 + row * 12)}" text-anchor="middle">${esc(text)}</text>`;
    });
    return out + "</g>";
  }

  // The wings: who supplies what on the left, the revenue split and who buys on the right, each name wired to
  // the station where its announcement or report sits on the rail.
  function flanks(block, area) {
    if (!lod.nodes || !block.ups.length && !block.downs.length) return "";
    const named = ROWH * view.k >= 11, sel = D.tlSelected?.chain;
    const wire = (l, x1, y1) => {
      if (t(l.date) < layout.t0) return "";
      const xs = sx(X(l.date)), ys = sy(railY(block, l.side)), mx = (x1 + xs) / 2;
      return `<path class="tl-wire ${l.side === "上游" ? "tl-up" : "tl-down"}${visible(l.date) ? "" : " later"}" data-tl-wire="${l.id}" d="M${x1},${y1} C${mx},${y1} ${mx},${ys} ${xs},${ys}"/>`;
    };
    const entry = (l, x, y, anchor) => `<g class="tl-wing ${l.side === "上游" ? "tl-up" : "tl-down"}${sel === l.id ? " selected" : ""}${visible(l.date) ? "" : " later"}" data-tl-chain="${l.id}" data-tl-stay>` +
      `<circle class="tl-wing-dot${l.dependent ? " dependent" : ""}" cx="${x}" cy="${y}" r="3.5"/>` + (l.signal ? `<circle class="tl-signal" cx="${anchor === "end" ? x + 7 : x - 7}" cy="${y - 6}" r="2.4"/>` : "") +
      (named ? `<text class="tl-wing-name" x="${anchor === "end" ? x - 8 : x + 8}" y="${y + 4}" text-anchor="${anchor}">` +
        (l.category ? `<tspan class="tl-wing-kind">${esc(clip(l.category, 5))} </tspan>` : "") + `${esc(clip(l.name, 10))}</text>` : "") + `</g>`;
    let wires = "", out = "";
    if (sx(0) > area.x0 && block.ups.length) {
      out += upstream(block, named, entry);
      // Like customers, every supplier is wired to its station on the rail.
      block.ups.forEach(l => { wires += wire(l, sx(DOT), sy(l.wy)); });
    }
    const bx = sx(block.bx), bw = BAR * view.k, cx = bx + bw + 50 * view.k;
    if (bx < area.x1 && block.segs.length) {
      block.segs.forEach((g, i) => {
        out += `<rect class="tl-seg s${Math.min(i, 3)}" x="${bx}" y="${sy(g.y)}" width="${bw}" height="${Math.max(1, g.h * view.k - 2)}"/>`;
        if (named && g.h * view.k >= 16 && !block.groups.some(gr => gr.seg === g)) out += `<text class="tl-wing-cat" x="${bx + bw + 8}" y="${sy(g.y + g.h / 2) + 4}">${esc(clip(g.name, 10))} ${esc(g.pct)}%</text>`;
      });
      block.groups.forEach(gr => {
        if (named) out += `<text class="tl-wing-cat" x="${cx - 4}" y="${sy(gr.y + CATH - 7)}">${esc(clip(gr.name || "其他", 12))}${gr.seg ? ` ${esc(gr.seg.pct)}%` : ""}</text>`;
        const from = gr.seg ? sy(gr.seg.y + gr.seg.h / 2) : sy(block.top + HEAD + block.body - 10);
        block.downs.filter(l => (l.segment || "") === gr.name).forEach(l => {
          const y = sy(l.wy), mx = (bx + bw + cx) / 2;
          out += `<path class="tl-fan" d="M${bx + bw},${from} C${mx},${from} ${mx},${y} ${cx},${y}"/>` + entry(l, cx, y, "start");
          wires += wire(l, cx, y);
        });
      });
    }
    return `<g class="tl-flanks">${wires}${out}</g>`;
  }

  // Mirror of the revenue side: suppliers fan into a bar of production stages, and the bar feeds the upstream rail.
  // A filled dot marks a supplier that calls this company its largest or main customer.
  function upstream(block, named, entry) {
    const bx = sx(LBAR), bw = BAR * view.k, dx = sx(DOT), ry = sy(railY(block, "上游")), mouth = sx(X(block.data.since));
    let lines = `<path class="tl-flow tl-up" stroke-width="3" d="M${bx + bw},${ry} L${mouth},${ry}"/>`, marks = "";
    block.stageSegs.forEach((g, i) => {
      marks += `<rect class="tl-stage-seg st${Math.min(i, 4)}" x="${bx}" y="${sy(g.y)}" width="${bw}" height="${Math.max(1, g.h * view.k - 2)}"/>`;
      if (named) marks += `<text class="tl-wing-cat" x="${dx + 4}" y="${sy(g.head + CATH - 7)}" text-anchor="end">${esc(g.name)} · ${g.count} 家</text>`;
      const from = sy(g.y + g.h / 2);
      g.list.forEach(l => {
        const y = sy(l.wy), mx = (bx + dx) / 2;
        lines += `<path class="tl-branchline tl-up${visible(l.date) ? "" : " later"}" data-tl-wire="${l.id}" d="M${bx},${from} C${mx},${from} ${mx},${y} ${dx},${y}"/>`;
        marks += entry(l, dx, y, "end");
      });
    });
    return lines + marks;
  }

  function prices(block) {
    const bars = (block.data.history || []).filter(bar => t(bar.date) >= layout.t0 && visible(bar.date));
    if (bars.length < 2 || !lod.nodes) return "";
    const top = sy(block.top + HEAD + block.body) + 6, height = PRICE * view.k - 12;
    const all = (block.data.history || []).map(bar => Number(bar.close)), min = Math.min(...all), span = Math.max(...all) - min || 1;
    const line = bars.map(bar => `${sx(X(bar.date)).toFixed(1)},${(top + height - ((Number(bar.close) - min) / span) * height).toFixed(1)}`).join(" ");
    const base = (top + height).toFixed(1), first = sx(X(bars[0].date)).toFixed(1), last = sx(X(bars.at(-1).date)).toFixed(1);
    return `<polygon class="tl-price-area" points="${first},${base} ${line} ${last},${base}"/><polyline class="tl-price" points="${line}"/>` +
      `<rect class="tl-price-hit" data-tl-price="${esc(block.code)}" x="${sx(0)}" y="${top - 4}" width="${layout.width * view.k}" height="${height + 8}"/>`;
  }

  function stock(block, area) {
    const top = sy(block.top), bottom = sy(block.top + block.height);
    if (bottom < area.y0 || top > area.y1) return "";
    // Each stock sits on its own soft panel, like a district on a transit map.
    const left = Math.max(area.x0, sx(layout.x0 - 20)), right = Math.min(area.x1, sx(layout.x1 + 20));
    let body = `<rect class="tl-district" x="${left}" y="${top}" width="${Math.max(0, right - left)}" height="${bottom - top}" rx="${Math.min(18, 18 * view.k)}"/>`;
    if (block.data?.initialized) return body + prices(block) + flanks(block, area) + rail(block, "上游", area) + rail(block, "下游", area)
      + block.lanes.map((th, i) => lane(th, i, block, area)).join("");
    const running = D.tlJob?.code === block.code && ["queued", "running"].includes(D.tlJob.status);
    body += `<text class="tl-status" x="16" y="${sy(block.top + HEAD + 20)}">${block.data ? "还没有脉络" : "正在读取"}</text>`;
    if (block.data) body += `<text class="tl-action" data-tl-init="${esc(block.code)}" x="96" y="${sy(block.top + HEAD + 20)}">${running ? "整理中…" : "整理近一年公告"}</text>`;
    return body;
  }

  function links() {
    if (play) return "";
    const byId = new Map(nodes().map(n => [n.id, n])), seen = new Set();
    return layout.blocks.flatMap(b => b.data?.links || []).map(l => {
      const a = byId.get(l.node_a), b = byId.get(l.node_b);
      if (!a || !b || seen.has(l.id)) return "";
      seen.add(l.id);
      const p = position(a), q = position(b), bend = Math.max(30, Math.abs(q.y - p.y) / 2);
      const d = `M${p.x},${p.y} C${p.x + bend},${p.y} ${q.x - bend},${q.y} ${q.x},${q.y}`;
      return `<path class="tl-link ${l.verdict === "无明显关联" ? "weak" : ""}${D.tlSelected?.link === l.id ? " selected" : ""}" d="${d}"/>` +
        `<path class="tl-link-hit" data-tl-link="${l.id}" d="${d}"/>`;
    }).join("");
  }

  // Draws the content layer for the current view, one screen of margin on every side.
  function render(fade) {
    const box = document.getElementById("tl-canvas");
    if (!box) return;
    clearTimeout(idle);
    const [W, H] = size(), area = { x0: -W, x1: 2 * W, y0: -H, y1: 2 * H };
    levels();
    const el = document.createElement("div");
    el.className = "tl-layer";
    el.innerHTML = `<svg class="tl-svg" width="${3 * W}" height="${3 * H}" viewBox="${-W} ${-H} ${3 * W} ${3 * H}">` +
      `${links()}${layout.blocks.map(b => stock(b, area)).join("")}</svg>`;
    // The page's CSP blocks inline style attributes, so the margin offset is set through the DOM.
    Object.assign(el.firstChild.style, { left: -W + "px", top: -H + "px" });
    box.insertBefore(el, document.getElementById("tl-overlay"));
    const layer = { el, k: view.k, tx: view.tx, ty: view.ty };
    const old = layers;
    layers = [...old, layer];
    if (fade && old.length) {
      el.style.opacity = 0;
      old.forEach(o => o.el.classList.add("leaving"));
      requestAnimationFrame(() => { el.style.opacity = 1; old.forEach(o => { o.el.style.opacity = 0; }); });
      setTimeout(() => { old.forEach(o => o.el.remove()); layers = layers.filter(l => !old.includes(l)); }, 260);
    } else {
      old.forEach(o => o.el.remove());
      layers = [layer];
    }
  }

  // ---- Overlay -------------------------------------------------------------------------------------

  function axis(W, H) {
    let grid = "", ticks = "";
    const now = sx(X(today())), pxMonth = 30.4 * DAYPX * view.k, step = pxMonth > 60 ? 1 : pxMonth > 22 ? 3 : 12;
    const first = new Date(layout.t0);
    for (let d = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 1)); d.getTime() < layout.t1; d.setUTCMonth(d.getUTCMonth() + 1)) {
      if (d.getUTCMonth() % step) continue;
      const x = sx(X(iso(d.getTime())));
      if (x < -40 || x > W + 40 || Math.abs(x - now) < 34) continue;
      grid += `<line class="tl-gridline" x1="${x}" x2="${x}" y1="${AXIS}" y2="${H}"/>`;
      ticks += `<text class="tl-tick" x="${x + 4}" y="${AXIS - 12}">${d.getUTCMonth() === 0 ? d.getUTCFullYear() + "年" : d.getUTCMonth() + 1 + "月"}</text>`;
    }
    grid += `<line class="tl-today" x1="${now}" x2="${now}" y1="${AXIS}" y2="${H}"/>`;
    ticks += `<rect class="tl-today-tag" x="${now - 20}" y="7" width="40" height="18" rx="9"/><text class="tl-today-text" x="${now}" y="20" text-anchor="middle">今天</text>`;
    if (play) {
      const x = sx(X(iso(play.at)));
      grid += `<line class="tl-playhead" x1="${x}" x2="${x}" y1="${AXIS}" y2="${H}"/>`;
      ticks += `<rect class="tl-playtag" x="${x - 44}" y="4" width="88" height="22" rx="11"/><text class="tl-playdate" x="${x}" y="19" text-anchor="middle">${iso(play.at)}</text>`;
    }
    return { grid, band: `<rect class="tl-band" x="0" y="0" width="${W}" height="${AXIS}"/><line class="tl-rule" x1="0" x2="${W}" y1="${AXIS}" y2="${AXIS}"/>${ticks}` };
  }

  // Badge widths follow the rendered title: digits and Latin letters are narrower than Chinese characters.
  const widths = new Map();
  let ruler = null;
  function measure(text) {
    if (!widths.has(text)) {
      if (!ruler) {
        ruler = document.createElement("canvas").getContext("2d");
        ruler.font = `600 12.5px ${getComputedStyle(document.documentElement).getPropertyValue("--serif")}`;
      }
      widths.set(text, ruler.measureText(text).width + 0.25 * text.length);
    }
    return widths.get(text);
  }

  function label(th, i, block, W, H, alpha) {
    const y = sy(laneY(block, i)), nodes = official(th).filter(n => visible(n.date)), c = Math.min(23.5, laneSize() / 2);
    const strength = D.tlFocus === th.id ? 1 : alpha;
    if (!nodes.length || strength <= 0 || y < chrome() - 4 || y > H + 20) return "";
    // The 事项 name follows its lane and sticks to the left edge once the lane starts off screen.
    const x = Math.min(Math.max(sx(X(nodes[0].date)), 12), W - 120), title = clip(th.title, 16), width = measure(title);
    return `<g class="tl-label ${theme(th)}${dimmed(th, block) ? " dim" : ""}${D.tlFocus === th.id ? " focused" : ""}" data-tl-thread="${th.id}" opacity="${strength.toFixed(2)}">` +
      `<rect class="tl-badge" x="${x - 2}" y="${y - c - 9.5}" width="${width + 16}" height="19" rx="9.5"/>` +
      `<text class="tl-title" x="${x + 6}" y="${y - c + 4.5}">${esc(title)}</text>` +
      `<text class="tl-status" x="${x + width + 20}" y="${y - c + 4.5}">${esc(th.status)}</text></g>`;
  }

  function overlay() {
    const el = document.getElementById("tl-overlay");
    if (!el) return;
    const [W, H] = size(), { grid, band } = axis(W, H), top = chrome();
    // Labels fade in over a band of lane heights rather than switching on at one size.
    // Badges sit midway between their lane and the one above, and only once that gap can hold one.
    const alpha = clamp((laneSize() - 25) / 6, 0, 1);
    let heads = "";
    layout.blocks.forEach(block => {
      const y0 = sy(block.top), y1 = sy(block.top + block.height);
      if (y1 < top || y0 > H) return;
      if (block.data?.initialized) heads += block.lanes.map((th, i) => label(th, i, block, W, H, alpha)).join("");
      // Stock names stay on screen while any part of their block is visible.
      const y = Math.min(Math.max(y0 + 28, top + 26), y1 - 8);
      heads += `<text class="tl-stock" data-tl-stock="${esc(block.code)}" x="16" y="${y}">${esc(name(block.code))}<tspan class="tl-status"> ${esc(block.code)}</tspan></text>`;
    });
    el.setAttribute("width", W); el.setAttribute("height", H); el.setAttribute("viewBox", `0 0 ${W} ${H}`);
    el.innerHTML = `<g class="tl-grid">${grid}</g>${heads}${band}`;
  }

  // ---- Frame loop ----------------------------------------------------------------------------------

  // Height taken by the floating toolbar, which wraps onto two lines on phones.
  const chrome = () => { const bar = document.querySelector(".tl-toolbar"); return bar ? bar.offsetTop + bar.offsetHeight : 80; };

  function size() {
    const box = document.getElementById("tl-canvas");
    return box ? [box.clientWidth, box.clientHeight] : [0, 0];
  }

  // The dotted paper moves and scales with the view, so panning feels like moving a sheet.
  function grid() {
    const box = document.getElementById("tl-canvas"), step = 24 * view.k * Math.pow(2, -Math.floor(Math.log2(view.k)));
    box.style.backgroundSize = `${step}px ${step}px`;
    box.style.backgroundPosition = `${view.tx}px ${view.ty}px`;
  }

  // Called on every view change: moves the drawn layers with the GPU and redraws the overlay.
  function update() {
    if (!document.getElementById("tl-canvas")) return;
    const [W, H] = size(), current = layers.at(-1);
    if (!current) return render(false), overlay(), minimap(W, H);
    for (const l of layers) {
      const s = view.k / l.k;
      l.el.style.transform = `translate(${view.tx - l.tx * s}px, ${view.ty - l.ty * s}px) scale(${s})`;
    }
    grid();
    const s = view.k / current.k, dx = view.tx - current.tx * s, dy = view.ty - current.ty * s;
    // Redraw at once when the margin runs out or the scale has drifted too far to look sharp.
    if (s < 0.6 || s > 1.7 || Math.abs(dx) > W * 0.8 || Math.abs(dy) > H * 0.8) render(true);
    else { clearTimeout(idle); idle = setTimeout(() => render(true), 140); }
    overlay();
    minimap(W, H);
  }

  function draw() {
    const box = document.getElementById("tl-canvas");
    if (!box) return;
    // The canvas fills the window below anything above it (the top bar on phones).
    const stage = box.parentElement, height = Math.max(360, window.innerHeight - stage.getBoundingClientRect().top - window.scrollY);
    if (Math.abs(stage.clientHeight - height) > 1) stage.style.height = height + "px";
    const [W, H] = size();
    layout = world();
    if (!view.ready && D.holdings.every(h => store()[h.code])) { Object.assign(view, overview(W, H)); view.ready = true; }
    if (D.tlPending && findThread(D.tlPending)) { Object.assign(view, threadView(D.tlPending, W, H)); D.tlPending = null; }
    render(false);
    overlay();
    grid();
    minimap(W, H);
    if (D.tlMode === "industry") A.industry.load();
    if (D.tlMode === "layers") A.layers.load();
  }

  function minimap(W, H) {
    const box = document.getElementById("tl-mini");
    if (!box || box.hidden) return;
    const w = box.clientWidth || 200, ax = w / Math.max(layout.x1 - layout.x0, 1), h = Math.min(150, Math.max(30, layout.height * ax));
    const scale = h / Math.max(layout.height, 1);
    const vx = (-view.tx / view.k - layout.x0) * ax, vy = -view.ty / view.k * scale, vw = W / view.k * ax, vh = H / view.k * scale;
    box.innerHTML = `<svg id="tl-mini-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
      layout.blocks.map(b => `<rect class="tl-mini-block${b.data?.initialized ? "" : " empty"}" x="0" y="${b.top * scale}" width="${w}" height="${Math.max(2, b.height * scale)}"/>` +
        (b.height * scale >= 11 ? `<text class="tl-mini-name" x="4" y="${b.top * scale + Math.min(11, b.height * scale - 1)}">${esc(name(b.code))}</text>` : "")).join("") +
      `<rect class="tl-mini-view" x="${vx}" y="${vy}" width="${vw}" height="${vh}"/></svg>`;
    Object.assign(box.dataset, { ax, ay: scale });
  }

  // ---- Views and motion ----------------------------------------------------------------------------

  // The fitted views leave room for the minimap and the card panel so the customer wing stays in sight.
  function overview(W, H, room = mapOn && W > 780 ? 216 : 0) {
    const k = clamp((usable(W) - room - 48) / (layout.x1 - layout.x0), 0.05, 3);
    return { k, tx: 24 - layout.x0 * k, ty: chrome() + 24 };
  }

  function stockView(code, W) {
    const block = layout.blocks.find(b => b.code === code), { k, tx } = overview(W);
    return { k, tx, ty: chrome() + 16 - block.top * k };
  }

  // The card panel covers the right side (bottom half on phones); focused content is centred in what is left.
  const carded = () => D.tlSelected && !(D.tlSelected.stock && !D.tlSelected.card);
  const usable = W => W > 900 && carded() ? W - 372 : W;
  const middle = H => window.innerWidth <= 780 && carded() ? chrome() + (H * 0.45 - chrome()) / 2 + 20 : H * 0.42;

  function threadView(id, W, H) {
    const { th, block, lane: i } = findThread(id), dates = th.nodes.map(n => t(n.date));
    const lo = (Math.min(...dates) - 30 * DAY - layout.t0) / DAY * DAYPX;
    const hi = (Math.max(...dates, t(block.data.until)) + 30 * DAY - layout.t0) / DAY * DAYPX;
    const k = Math.min(4, Math.max(96 / LANE, usable(W) * 0.85 / (hi - lo)));
    return { k, tx: (usable(W) - (hi - lo) * k) / 2 - lo * k, ty: middle(H) - laneY(block, i) * k };
  }

  function nodeView(n, W, H) {
    const k = Math.max(view.k, 160 / LANE), p = { x: X(n.date), y: laneY(n.block, n.lane) };
    return { k, tx: usable(W) / 2 - p.x * k, ty: middle(H) - 30 - p.y * k };
  }

  function chainView(item, W, H) {
    const k = Math.max(view.k, 120 / LANE);
    return { k, tx: usable(W) / 2 - X(item.link.date) * k, ty: middle(H) - railY(item.block, item.link.side) * k };
  }

  function stop() { if (motion) { cancelAnimationFrame(motion.frame); motion = null; } }

  // Camera as [world x at screen centre, world y at screen centre, log of visible world width].
  function camera() {
    const [W, H] = size();
    return [(W / 2 - view.tx) / view.k, (H / 2 - view.ty) / view.k, Math.log(W / view.k)];
  }

  function place([ux, uy, lw]) {
    const [W, H] = size();
    view.k = W / Math.exp(lw); view.tx = W / 2 - ux * view.k; view.ty = H / 2 - uy * view.k;
  }

  // Every motion records the camera's velocity, so the next one can start from it instead of from rest.
  function run(step, done) {
    stop();
    motion = { frame: 0, vel: [0, 0, 0] };
    let last = performance.now();
    const tick = now => {
      if (!motion) return;
      const dt = Math.min(48, now - last), before = camera(), more = step(now, dt), after = camera();
      if (dt > 0) motion.vel = after.map((v, i) => (v - before[i]) / (dt / 1000));
      last = now;
      update();
      if (more) motion.frame = requestAnimationFrame(tick); else { motion = null; render(true); done?.(); }
    };
    motion.frame = requestAnimationFrame(tick);
  }

  // A critically damped spring pulls the camera to its goal. A new goal mid-flight keeps the current velocity,
  // so redirections bend the motion rather than restart it.
  function fly(to, done) {
    const [W, H] = size(), goal = [(W / 2 - to.tx) / to.k, (H / 2 - to.ty) / to.k, Math.log(W / to.k)];
    const vel = motion ? [...motion.vel] : [0, 0, 0], cam = camera(), omega = 7;
    run((now, dt) => {
      for (let left = dt / 1000; left > 1e-6; left -= 0.008) {
        const h = Math.min(0.008, left), far = Math.hypot(goal[0] - cam[0], goal[1] - cam[1]);
        // Far from the goal the camera aims wider than its final framing, so long trips pull back first
        // and zoom in on arrival (the same shape as van Wijk's smooth zoom path).
        const aim = [goal[0], goal[1], Math.max(goal[2], Math.log(far * 1.4 + 1e-6))];
        for (let i = 0; i < 3; i++) {
          vel[i] += (omega * omega * (aim[i] - cam[i]) - 2 * omega * vel[i]) * h;
          cam[i] += vel[i] * h;
        }
      }
      const k = W / Math.exp(cam[2]);
      const settled = Math.hypot(goal[0] - cam[0], goal[1] - cam[1]) * k < 0.5 && Math.abs(goal[2] - cam[2]) < 0.002
        && Math.hypot(vel[0], vel[1]) * k < 8 && Math.abs(vel[2]) < 0.02;
      place(settled ? goal : cam);
      return !settled;
    }, done);
  }

  function zoomAt(x, y, k) {
    k = clamp(k, 0.04, 12);
    view.tx = x - (x - view.tx) * (k / view.k);
    view.ty = y - (y - view.ty) * (k / view.k);
    view.k = k;
  }

  // Wheel zoom eases toward a target scale anchored at the pointer; further ticks move the target, not the view.
  function glideTo(k, x, y) {
    const target = { k: clamp(k, 0.04, 12), x, y };
    motion?.glide && Object.assign(motion.glide, target);
    if (motion?.glide) return;
    run(() => {
      const g = motion.glide, next = view.k + (g.k - view.k) * 0.18;
      zoomAt(g.x, g.y, Math.abs(g.k - next) < 0.0005 ? g.k : next);
      return view.k !== g.k;
    });
    motion.glide = target;
  }

  // After a drag the view keeps the release speed and slows down with friction.
  function coast(vx, vy) {
    if (Math.hypot(vx, vy) < 0.05) return render(true);
    run((now, dt) => {
      view.tx += vx * dt; view.ty += vy * dt;
      const decay = Math.pow(0.93, dt / 16);
      vx *= decay; vy *= decay;
      return Math.hypot(vx, vy) > 0.02;
    });
  }

  // ---- Navigation ----------------------------------------------------------------------------------

  // Levels: 全貌 → 股票 → 事项 → 节点. Esc and the card's close button step back up one level.
  // Navigation only swaps the panel and the trail; the flight that follows redraws the canvas.
  // Rebuilding the whole page here stalled the first frames of the motion.
  function refresh() {
    const stage = document.getElementById("tl-stage");
    if (!stage) return A.rerender();
    const body = panel(), box = document.getElementById("tl-card");
    box.classList.remove("entering");
    if (body && !panelOpen) { void box.offsetWidth; box.classList.add("entering"); }
    panelOpen = Boolean(body);
    box.hidden = !body;
    box.innerHTML = body;
    stage.querySelector(".tl-trail").innerHTML = trail();
  }

  // Zooming out past the portfolio folds the companies into their industries (产业联动图); zooming in comes back.
  let switchedAt = 0;
  function setMode(mode, code) {
    const stage = document.getElementById("tl-stage");
    if (!stage || D.tlMode === mode && !code) return;
    switchedAt = performance.now();
    D.tlMode = mode;
    stop();
    stage.classList.toggle("industry", mode !== "company");
    document.getElementById("tl-industry").hidden = mode !== "industry";
    document.getElementById("tl-layers").hidden = mode !== "layers";
    stage.querySelectorAll("[data-tl-mode]").forEach(b => b.classList.toggle("on", b.dataset.tlMode === mode));
    if (mode === "industry") return A.industry.load();
    if (mode === "layers") return A.layers.load();
    if (code) return goStock(code);
    fly(overview(...size()));
  }
  A.timelineShowStock = code => setMode("company", code);
  // From the industry map back to a station or a 事项 node on the company canvas.
  const back = go => id => { setMode("company"); requestAnimationFrame(() => go(id)); };
  A.timelineShowChain = back(goChain);
  A.timelineShowNode = back(goNode);

  function goOverview() { D.tlFocus = null; D.tlSelected = null; D.tlFrom = null; refresh(); fly(overview(...size())); }
  // At the stock level the canvas itself is the overview; its card opens on a second click on the stock name.
  function goStock(code) {
    if (D.tlSelected?.stock === code && !D.tlFocus) { D.tlSelected = { stock: code, card: !D.tlSelected.card }; return refresh(); }
    D.tlFocus = null; D.tlSelected = { stock: code }; refresh(); fly(stockView(code, ...size()));
  }
  function goThread(id) { D.tlFocus = id; D.tlSelected = { thread: id }; refresh(); fly(threadView(id, ...size())); }
  function goChain(id) {
    const item = findChain(id);
    if (!item) return;
    D.tlFocus = null; D.tlSelected = { chain: id };
    refresh();
    fly(chainView(item, ...size()));
  }
  function goNode(id) {
    const n = findNode(id);
    if (!n) return;
    if (!D.tlFrom) D.tlFocus = n.thread.id;
    D.tlSelected = { node: id };
    refresh();
    fly(nodeView(n, ...size()));
  }

  // Selection changes without camera motion redraw the canvas directly.
  function reselect() { refresh(); render(true); }

  function up() {
    const s = D.tlSelected || {};
    if (D.tlFrom) { D.tlFrom = null; return reselect(); }
    if (s.node) return goThread(findNode(s.node).thread.id);
    if (s.link) { D.tlSelected = null; return reselect(); }
    if (s.chain) return goStock(findChain(s.chain).block.code);
    if (s.thread || D.tlFocus) return goStock(findThread(s.thread || D.tlFocus).block.code);
    if (s.stock) return goOverview();
  }

  function trail() {
    const s = D.tlSelected || {}, n = s.node && findNode(s.node);
    const th = n ? n.thread : findThread(s.thread || D.tlFocus)?.th;
    const c = s.chain && findChain(s.chain);
    const code = n?.code || (th && findThread(th.id).block.code) || c?.block.code || s.stock;
    const parts = [`<button class="textbtn" data-tl-go="overview">全貌</button>`];
    if (code) parts.push(`<button class="textbtn" data-tl-go="stock" data-code="${esc(code)}">${esc(name(code))}</button>`);
    if (th) parts.push(`<button class="textbtn" data-tl-go="thread" data-id="${th.id}">${esc(clip(th.title, 14))}</button>`);
    if (n) parts.push(`<span>${esc(clip(n.title, 16))}</span>`);
    if (c) parts.push(`<span>${esc(c.link.side)} · ${esc(clip(c.link.name, 12))}</span>`);
    return parts.join(`<span class="tl-sep">›</span>`);
  }

  // ---- Panels --------------------------------------------------------------------------------------

  const unverified = list => list?.length ? `<p class="unverified">未核对：${esc(list.join("、"))}</p>` : "";
  const link = (url, text) => /^https?:\/\//i.test(url || "") ? `<a class="link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : "";

  function nodeCard(n) {
    const th = n.thread, list = official(th), bar = [...(n.block.data.history || [])].reverse().find(b => b.date <= n.date);
    const next = list.find(m => m.major && m.date > n.date && m.id !== n.id);
    const stage = n.major ? list.filter(m => !m.major && m.date >= n.date && (!next || m.date < next.date)) : [];
    const sameDay = list.filter(m => m.date === n.date && m.id !== n.id && !stage.includes(m));
    const from = D.tlFrom && D.tlFrom !== n.id ? findNode(D.tlFrom) : null;
    const index = list.findIndex(m => m.id === n.id), prev = list[index - 1], after = list[index + 1];
    return `<p class="meta">${esc(name(n.code))} · ${esc(n.kind)} · ${esc(n.date)}${n.major ? " · 阶段节点" : ""}${n.added_by ? " · 自己添加" : ""}</p>
      <h3 class="tl-card-title">${esc(n.title)}</h3>
      ${n.summary ? `<h4>原文摘要</h4><p>${esc(n.summary)}</p>` : ""}
      ${n.analysis ? `<h4>分析</h4><p>${esc(n.analysis)}</p>` : ""}
      ${unverified(n.unverified)}
      ${bar ? `<p class="meta">${esc(bar.date)} 收盘 ${fixed(Number(bar.close))} 元</p>` : ""}
      ${sameDay.length ? `<h4>同日公告</h4><ul class="tl-list">${sameDay.map(m => `<li><button class="textbtn" data-tl-node="${m.id}">${esc(clip(m.title, 30))}</button></li>`).join("")}</ul>` : ""}
      ${stage.length ? `<h4>本阶段更新</h4><ul class="tl-list">${stage.map(m => `<li><button class="textbtn" data-tl-node="${m.id}">${esc(m.date)} ${esc(clip(m.title, 26))}</button></li>`).join("")}</ul>` : ""}
      <p>${link(n.url, "来源原文")}</p>
      <div class="tl-steps">${prev ? `<button class="textbtn" data-tl-node="${prev.id}">← 上一条</button>` : "<span></span>"}${after ? `<button class="textbtn" data-tl-node="${after.id}">下一条 →</button>` : ""}</div>
      <div class="tl-actions">${from
        ? `<button class="btn btn-solid" data-tl-pair ${D.tlBusy ? "disabled" : ""}>分析与「${esc(clip(from.title, 10))}」的关联</button><button class="textbtn" data-tl-cancel>取消</button>`
        : D.tlFrom === n.id ? `<span class="meta">再选一个节点，可以是其他股票的</span><button class="textbtn" data-tl-cancel>取消</button>`
        : `<button class="btn" data-tl-from="${n.id}">关联到其他节点</button>`}</div>`;
  }

  function linkCard(l) {
    const a = findNode(l.node_a), b = findNode(l.node_b);
    return `<p class="meta">关联分析 · ${esc(l.verdict)}</p>
      <h3 class="tl-card-title">${a ? esc(clip(a.title, 18)) : ""} ↔ ${b ? esc(clip(b.title, 18)) : ""}</h3>
      <p>${esc(l.explanation)}</p>
      <p class="trace-src">${Object.entries(l.references).map(([ref, value]) => A.sources(value).slice(0, 3).map(s => link(s.url, `${ref} · ${s.title}`)).join(" ")).join(" ")}</p>
      ${A.paper(l.steps, "link-" + l.id, l.references)}`;
  }

  function chainBody(l) {
    return `<h3 class="tl-card-title">${esc(l.name)}${l.sec_code ? `<span class="code">${esc(l.sec_code)}</span>` : ""}</h3>
      ${l.note ? `<p>${esc(l.note)}</p>` : ""}${l.signal ? `<p class="tl-flag">${esc(l.signal)}</p>` : ""}${unverified(l.unverified)}
      <h4>原文</h4><blockquote class="tl-quote">${esc(l.quote)}</blockquote>
      <p>${link(l.url, l.source || "来源原文")}</p>`;
  }

  function chainCard({ link: l, block }) {
    const kind = l.category || l.segment;
    return `<p class="meta">${esc(name(block.code))} · ${esc(l.side)}${kind ? " · " + esc(kind) : ""} · ${esc(l.date)}${l.status === "确认" ? "" : " · " + esc(l.status)}</p>
      ${chainBody(l)}`;
  }

  function threadCard(th, block) {
    return `<p class="meta"><span class="tl-chip ${theme(th)}">${esc(th.theme)}</span> ${esc(th.status)} · ${th.nodes.length} 条</p>
      <h3 class="tl-card-title">${esc(th.title)}</h3>
      ${th.summary ? `<p>${esc(th.summary)}</p>` : ""}${unverified(th.unverified)}
      ${th.error ? `<p class="meta">分析未完成：${esc(th.error)}</p>` : ""}
      ${(block.data.commitments || []).filter(c => c.thread_id === th.id).map(c => `<div class="tl-commitment"><h4>${esc(c.kind)} ${A.commitmentState(c)}</h4>${A.commitmentBody(c)}</div>`).join("")}
      <h4>经过</h4><ol class="tl-story">${official(th).filter(n => n.major).map(n => `<li><button class="textbtn" data-tl-node="${n.id}"><span class="meta">${esc(n.date)}</span> ${esc(clip(n.title, 30))}</button></li>`).join("")}</ol>
      ${th.steps?.length ? A.paper(th.steps, "thread-" + th.id) : ""}`;
  }

  function stockCard(block) {
    const d = block.data;
    if (!d?.initialized) return `<h3 class="tl-card-title">${esc(name(block.code))}</h3><p class="meta">还没有脉络</p>`;
    const list = d.candidates || [], open = (d.commitments || []).filter(c => c.alert);
    return `<p class="meta">${esc(block.code)} · ${esc(d.since)} 至 ${esc(d.until)}</p>
      <h3 class="tl-card-title">${esc(name(block.code))}</h3>
      ${open.length ? `<p class="tl-flag">${open.length} 条承诺需要留意</p>` : ""}
      ${d.chain?.links.length ? `<p class="meta">上游 ${block.ups.length} 家 · 下游 ${block.downs.length} 家${d.chain.report_date ? ` · 主营构成 ${esc(d.chain.report_date)}` : ""}</p>` : ""}
      <ul class="tl-list">${block.lanes.map(th => `<li><button class="textbtn" data-tl-thread="${th.id}"><span class="tl-chip ${theme(th)}"></span>${esc(th.title)}</button> <span class="meta">${esc(th.status)}</span></li>`).join("")}</ul>
      ${list.length ? `<details class="tl-more"${D.tlMoreOpen ? " open" : ""}><summary>未收录公告 ${list.length}</summary>
        <ul>${list.slice(0, 150).map(c => `<li data-tl-candidate="${esc(c.art_code)}"><span class="meta">${esc(c.date)}</span>${link(c.url, clip(c.title, 28))}
          <select aria-label="加入事项">${block.lanes.map(th => `<option value="${th.id}">${esc(clip(th.title, 12))}</option>`).join("")}<option value="">新事项</option></select>
          <button class="textbtn" data-tl-add="${esc(block.code)}">加入</button></li>`).join("")}</ul></details>` : ""}`;
  }

  function panel() {
    const s = D.tlSelected || {};
    let body = "";
    if (s.node && findNode(s.node)) body = nodeCard(findNode(s.node));
    else if (s.link) { const l = layout.blocks.flatMap(b => b.data?.links || []).find(item => item.id === s.link); if (l) body = linkCard(l); }
    else if (s.chain && findChain(s.chain)) body = chainCard(findChain(s.chain));
    else if (s.thread && findThread(s.thread)) { const found = findThread(s.thread); body = threadCard(found.th, found.block); }
    else if (s.stock && s.card) { const block = layout.blocks.find(b => b.code === s.stock); if (block) body = stockCard(block); }
    return body ? `<button class="tl-close" data-tl-up aria-label="返回上一级">×</button>${body}` : "";
  }

  function progress() {
    const job = D.tlJob;
    if (!job) return "";
    const state = { queued: "排队中", running: "正在分析", done: "完成", failed: "失败" }[job.status];
    return `<p class="meta">${esc(job.kind === "link" ? "关联分析" : `整理 ${name(job.code)}`)} · ${state} · ${(job.steps || []).length} 步</p>
      ${job.error ? `<p class="quiet" role="alert">${esc(job.error)}</p>` : ""}`;
  }

  A.pages.timeline = function () {
    if (!A.live) return `<header class="page-head"><h1 class="page-title">脉络</h1></header><p class="quiet">登录后查看。</p>`;
    layout = world();
    layers = [];
    requestAnimationFrame(draw);
    const body = panel(), entering = body && !panelOpen;
    panelOpen = Boolean(body);
    return `<div class="tl-stage${D.tlMode === "industry" || D.tlMode === "layers" ? " industry" : ""}" id="tl-stage">
      <div id="tl-canvas" class="tl-canvas"><svg id="tl-overlay" class="tl-overlay"></svg></div>
      <div class="tl-toolbar"><h1 class="tl-heading">脉络</h1><nav class="tl-trail">${trail()}</nav>
        <span class="tl-spacer"></span>
        <span class="tl-modes" role="group" aria-label="视图">${[["company", "公司"], ["industry", "产业"], ["layers", "立体"]].map(([mode, label]) =>
          `<button class="btn${(D.tlMode || "company") === mode ? " on" : ""}" data-tl-mode="${mode}">${label}</button>`).join("")}</span>
        <select class="tl-jump" aria-label="跳转到股票"><option value="">跳转</option>${D.holdings.map(h => `<option value="${esc(h.code)}">${esc(name(h.code))}</option>`).join("")}</select>
        <button class="btn${mapOn ? " on" : ""}" data-tl-map aria-pressed="${mapOn}">缩略图</button>
        <button class="btn" data-tl-play>${play ? "停止" : "回放"}</button></div>
      ${D.tlError ? `<p class="tl-error" role="alert">${esc(D.tlError)}</p>` : ""}
      <div class="tl-side"><div class="tl-mini" id="tl-mini"${mapOn ? "" : " hidden"}></div>
        <aside class="tl-card${entering ? " entering" : ""}" id="tl-card"${body ? "" : " hidden"}>${body}</aside></div>
      ${A.industry.markup()}
      ${A.layers.markup()}
      <div class="tl-tip" id="tl-tip" hidden></div>
      <div class="tl-job" id="tl-progress"${D.tlJob ? "" : " hidden"}>${progress()}</div></div>`;
  };

  // ---- Data and jobs -------------------------------------------------------------------------------

  async function load(code) {
    try { store()[code] = await A.api("/api/timeline/" + encodeURIComponent(code)); D.tlError = ""; }
    catch (err) { D.tlError = err.message; }
  }

  async function reload(codes) {
    await Promise.all(codes.map(load));
    if (location.hash.split("/")[0] === "#timeline") A.rerender();
  }

  A.loadTimeline = () => reload(D.holdings.map(h => h.code).filter(code => !store()[code]));

  A.openThread = (code, id) => {
    Object.assign(D, { tlFocus: id, tlSelected: { thread: id }, tlFrom: null, tlPending: id });
    location.hash = "timeline";
    if (!store()[code]) reload([code]); else if (location.hash === "#timeline") A.rerender();
  };

  async function poll(id, onDone) {
    clearTimeout(timer);
    try {
      D.tlJob = { ...D.tlJob, ...await A.api("/api/jobs/" + encodeURIComponent(id)) };
    } catch (err) { D.tlError = err.message; A.rerender(); return; }
    if (["queued", "running"].includes(D.tlJob.status)) {
      const box = document.getElementById("tl-progress");
      if (box) { box.hidden = false; box.innerHTML = progress(); }
      timer = setTimeout(() => poll(id, onDone), 1500);
      return;
    }
    if (D.tlJob.status === "done") { await onDone(D.tlJob.result); D.tlJob = null; }
    A.rerender();
  }

  async function start(path, body, meta, onDone) {
    D.tlBusy = true; D.tlError = "";
    try {
      const { job_id } = await A.api(path, body);
      D.tlJob = { status: "queued", steps: [], ...meta };
      poll(job_id, onDone);
    } catch (err) { D.tlError = err.message; }
    finally { D.tlBusy = false; A.rerender(); }
  }

  function toggleMap(button) {
    mapOn = !mapOn;
    try { localStorage.setItem("tl-map", mapOn ? "on" : "off"); } catch {}
    button.classList.toggle("on", mapOn);
    button.setAttribute("aria-pressed", mapOn);
    document.getElementById("tl-mini").hidden = !mapOn;
    minimap(...size());
  }

  function togglePlay() {
    if (play) { cancelAnimationFrame(play.frame); play = null; A.rerender(); return; }
    // Start just before the earliest announcement rather than at the edge of the one-year window.
    const first = Math.min(...nodes().filter(n => n.kind === "公告").map(n => t(n.date)));
    const from = Math.max(layout.t0, first - 10 * DAY), to = t(today()), ms = 14000;
    play = { at: from, follow: true };
    A.rerender();
    const [W] = size();
    // Glide so the start date sits near the left edge, then play.
    fly({ k: view.k, tx: 40 - X(iso(from)) * view.k, ty: view.ty }, () => {
      if (!play) return;
      const began = performance.now();
      // Playback changes what is drawn every frame, so it redraws the content layer instead of moving it.
      const step = now => {
        if (!play) return;
        play.at = from + (to - from) * Math.min(1, (now - began) / ms);
        // Once the cursor reaches about 60% of the visible width the view scrolls with it.
        const x = sx(X(iso(play.at))), edge = usable(W) * 0.6;
        if (play.follow && x > edge) view.tx -= x - edge;
        render(false); overlay(); grid(); minimap(...size());
        if (play.at < to) play.frame = requestAnimationFrame(step);
        else { play = null; A.rerender(); }
      };
      play.frame = requestAnimationFrame(step);
    });
  }

  // ---- Input ---------------------------------------------------------------------------------------

  // Canvas items act on pointerup: layers are redrawn while the view moves, which swallows click events.
  function press(target) {
    const el = target.closest("[data-tl-node],[data-tl-thread],[data-tl-link],[data-tl-stock],[data-tl-init],[data-tl-chain]");
    return el ? { ...el.dataset } : null;
  }

  function act(hit) {
    if (!hit) return;
    if (hit.tlNode) return goNode(Number(hit.tlNode));
    // A name in the wing opens its card in place; a station on the rail flies to its date.
    if (hit.tlChain && hit.tlStay !== undefined) { D.tlFocus = null; D.tlSelected = { chain: Number(hit.tlChain) }; return reselect(); }
    if (hit.tlChain) return goChain(Number(hit.tlChain));
    if (hit.tlThread) return D.tlFocus === Number(hit.tlThread) && !D.tlSelected?.node ? goStock(findThread(D.tlFocus).block.code) : goThread(Number(hit.tlThread));
    if (hit.tlLink) { D.tlSelected = { link: Number(hit.tlLink) }; return reselect(); }
    if (hit.tlStock) return goStock(hit.tlStock);
    if (hit.tlInit) return start(`/api/timeline/${encodeURIComponent(hit.tlInit)}/init`, {}, { kind: "timeline", code: hit.tlInit }, () => reload([hit.tlInit]));
  }

  document.addEventListener("click", async e => {
    const mini = e.target.closest("#tl-mini-svg");
    if (mini) {
      const r = mini.getBoundingClientRect(), { ax, ay } = document.getElementById("tl-mini").dataset, [W, H] = size();
      return fly({ k: view.k, tx: W / 2 - ((e.clientX - r.left) / ax + layout.x0) * view.k, ty: H / 2 - (e.clientY - r.top) / ay * view.k });
    }
    if (!e.target.closest("#tl-stage") || e.target.closest("#tl-canvas")) return;
    if (e.target.closest("[data-tl-up]")) return up();
    const mode = e.target.closest("[data-tl-mode]");
    if (mode) return setMode(mode.dataset.tlMode);
    const go = e.target.closest("[data-tl-go]");
    if (go) return go.dataset.tlGo === "overview" ? goOverview() : go.dataset.tlGo === "stock" ? goStock(go.dataset.code) : goThread(Number(go.dataset.id));
    if (e.target.closest("[data-tl-play]")) return togglePlay();
    const map = e.target.closest("[data-tl-map]");
    if (map) return toggleMap(map);
    const item = e.target.closest("[data-tl-node],[data-tl-thread]");
    if (item) return act({ ...item.dataset });
    const from = e.target.closest("[data-tl-from]");
    if (from) { D.tlFrom = Number(from.dataset.tlFrom); return reselect(); }
    if (e.target.closest("[data-tl-cancel]")) { D.tlFrom = null; return reselect(); }
    if (e.target.closest("[data-tl-pair]")) {
      const a = findNode(D.tlFrom), b = findNode(D.tlSelected.node), pair = { node_a: D.tlFrom, node_b: D.tlSelected.node };
      D.tlFrom = null;
      return start("/api/timeline/links", pair, { kind: "link" }, async result => {
        await reload([...new Set([a.code, b.code])]); D.tlSelected = { link: result.id };
      });
    }
    const add = e.target.closest("[data-tl-add]");
    if (add) {
      const row = add.closest("[data-tl-candidate]"), value = row.querySelector("select").value, code = add.dataset.tlAdd;
      D.tlMoreOpen = true;
      try {
        await A.api(`/api/timeline/${encodeURIComponent(code)}/nodes`, { art_code: row.dataset.tlCandidate, thread_id: value ? Number(value) : null });
      } catch (err) { D.tlError = err.message; }
      return reload([code]);
    }
  });

  document.addEventListener("change", e => {
    if (!e.target.classList?.contains("tl-jump") || !e.target.value) return;
    goStock(e.target.value);
    e.target.value = "";
  });

  document.addEventListener("toggle", e => { if (e.target.classList?.contains("tl-more")) D.tlMoreOpen = e.target.open; }, true);

  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || !location.hash.startsWith("#timeline") || e.target.closest?.("input, select, textarea")) return;
    if (play) return togglePlay();
    if (D.tlMode === "layers") return setMode("industry");
    if (D.tlMode === "industry") return setMode("company");
    up();
  });

  const local = (e, el) => { const r = el.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };

  document.addEventListener("dblclick", e => {
    const box = e.target.closest?.("#tl-canvas");
    if (!box || press(e.target)) return;
    const [x, y] = local(e, box);
    glideTo(view.k * 2, x, y);
  });

  document.addEventListener("wheel", e => {
    // Zooming moves through the levels: 公司 ⇄ 产业 → 立体. Inside the 3D view the wheel zooms its own camera.
    if (e.target.closest?.("#tl-layers")) return;
    const level = e.target.closest?.("#tl-industry");
    if (level) {
      e.preventDefault();
      if (performance.now() - switchedAt <= 600 || e.target.closest(".ind-card")) return;
      if (e.deltaY < -4) setMode("company");
      else if (e.deltaY > 4) setMode("layers");
      return;
    }
    const box = e.target.closest?.("#tl-canvas");
    if (!box) return;
    e.preventDefault();
    const [x, y] = local(e, box);
    if (play) play.follow = false;
    // Trackpad two-finger swipes pan; the mouse wheel and pinch (ctrlKey) zoom around the pointer.
    if (!e.ctrlKey && Math.abs(e.deltaX) > Math.abs(e.deltaY)) { stop(); view.tx -= e.deltaX; view.ty -= e.deltaY; return update(); }
    const delta = clamp(e.deltaY * (e.deltaMode === 1 ? 40 : 1), -120, 120);
    if (delta > 0 && !play && (motion?.glide?.k || view.k) <= overview(...size()).k * 0.7 && performance.now() - switchedAt > 600) return setMode("industry");
    glideTo((motion?.glide?.k || view.k) * Math.exp(-delta * (e.ctrlKey ? 0.006 : 0.0012)), x, y);
  }, { passive: false });

  // Hovering a name or a station lights up both ends and the wire between them, once the pointer settles,
  // so a quick sweep across the wings does not flash the canvas.
  let lit = null, litWanted = null, litTimer = null;
  function lightSoon(id) {
    if (id === litWanted) return;
    litWanted = id;
    clearTimeout(litTimer);
    litTimer = setTimeout(() => light(id), id !== null ? (lit !== null ? 60 : 120) : 200);
  }
  function light(id) {
    if (id === lit) return;
    lit = id;
    const canvas = document.getElementById("tl-canvas");
    canvas.querySelectorAll(".tl-on").forEach(el => el.classList.remove("tl-on"));
    canvas.classList.toggle("tl-lit", id !== null);
    if (id !== null) canvas.querySelectorAll(`[data-tl-chain="${id}"],[data-tl-wire="${id}"]`).forEach(el => el.classList.add("tl-on"));
  }

  function tip(e) {
    const box = document.getElementById("tl-tip"), stage = document.getElementById("tl-stage");
    if (!box) return;
    const over = e.target.closest?.("[data-tl-chain]");
    lightSoon(over ? over.dataset.tlChain : null);
    const r = stage.getBoundingClientRect(), el = e.target.closest?.("[data-tl-node],[data-tl-deadline],[data-tl-price],[data-tl-chain]");
    let html = "";
    if (el?.dataset.tlNode) {
      const n = findNode(Number(el.dataset.tlNode));
      if (n) html = `<p class="meta">${esc(name(n.code))} · ${esc(n.date)} · ${esc(n.kind)}</p><b>${esc(n.title)}</b>${n.summary ? `<p>${esc(clip(n.summary, 70))}</p>` : ""}`;
    } else if (el?.dataset.tlChain) {
      const c = findChain(Number(el.dataset.tlChain));
      if (c) html = `<p class="meta">${esc(c.link.side)} · ${esc(c.link.category || c.link.segment || "")} · ${esc(c.link.date)}</p><b>${esc(c.link.name)}</b><p>${esc(clip(c.link.note, 70))}</p>`;
    } else if (el?.dataset.tlDeadline) {
      const c = layout.blocks.flatMap(b => b.data?.commitments || []).find(item => item.id === Number(el.dataset.tlDeadline));
      if (c) {
        const left = Math.round((t(c.deadline) - t(today())) / DAY);
        html = `<p class="meta">${esc(c.kind)} · 期限 ${esc(c.deadline)} · ${left >= 0 ? `还有 ${left} 天` : `已过 ${-left} 天`}</p><b>${esc(c.state)}</b><p>${esc(clip(c.content, 70))}</p>`;
      }
    } else if (el?.dataset.tlPrice) {
      const block = layout.blocks.find(b => b.code === el.dataset.tlPrice);
      const day = iso(layout.t0 + ((e.clientX - r.left - view.tx) / view.k) / DAYPX * DAY);
      const bar = [...(block.data.history || [])].reverse().find(b => b.date <= day);
      if (bar) html = `<p class="meta">${esc(name(block.code))} · ${esc(bar.date)}</p><b>收盘 ${fixed(Number(bar.close))} 元</b>`;
    }
    box.hidden = !html;
    if (!html) return;
    box.innerHTML = html;
    const x = e.clientX - r.left, y = e.clientY - r.top;
    box.style.left = Math.min(x + 14, r.width - 260) + "px";
    box.style.top = Math.max(8, Math.min(y + 14, r.height - box.offsetHeight - 8)) + "px";
  }

  document.addEventListener("pointerdown", e => {
    if (!e.target.closest?.("#tl-canvas")) return;
    // Taking hold of the view during playback stops it from following the cursor.
    if (play) play.follow = false;
    stop();
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false, trail: [{ x: e.clientX, y: e.clientY, at: e.timeStamp }] };
      pressed = press(e.target);
    }
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: view.k };
      drag = null; pressed = null;
    }
  });
  document.addEventListener("pointermove", e => {
    if (!pointers.has(e.pointerId)) return tip(e);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()], box = document.getElementById("tl-canvas").getBoundingClientRect();
      zoomAt((a.x + b.x) / 2 - box.left, (a.y + b.y) / 2 - box.top, pinch.k * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d);
      return update();
    }
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    drag.moved = true;
    drag.trail = [...drag.trail.filter(p => e.timeStamp - p.at < 100), { x: e.clientX, y: e.clientY, at: e.timeStamp }];
    document.getElementById("tl-tip").hidden = true;
    view.tx = drag.tx + dx; view.ty = drag.ty + dy;
    update();
  });
  const release = e => {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    if (e.type === "pointerup" && drag && !pinch && pointers.size === 0) {
      if (!drag.moved) act(pressed);
      else {
        // Release speed over the last 100 ms carries into a coasting glide.
        const first = drag.trail[0], last = drag.trail.at(-1), dt = Math.max(1, last.at - first.at);
        const fresh = e.timeStamp - last.at < 60;
        coast(fresh ? (last.x - first.x) / dt : 0, fresh ? (last.y - first.y) / dt : 0);
      }
    }
    if (pointers.size < 2) { if (pinch) render(true); pinch = null; }
    if (!pointers.size) { drag = null; pressed = null; }
  };
  document.addEventListener("pointerup", release);
  document.addEventListener("pointercancel", release);
  window.addEventListener("resize", () => { if (location.hash.startsWith("#timeline")) draw(); });
})(App);
