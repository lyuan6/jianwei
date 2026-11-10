// 立体：每只持仓是一棵树，树干是时间（新的在上），事项线从树干分出并前后错开；上游从左上、下游从右下用曲线连到树干的对应日期。
// Real 3D lines on one canvas; text is drawn from cached sprites and always faces the viewer, so it stays sharp and turning stays cheap.
(function (A) {
  const { D } = A;
  const DAY = 864e5, AGE = 370, PITCH = 9 * Math.PI / 180, RAD = Math.PI / 180, MAX_NAMES = 8, MAX_ZOOM = 6, MIN_ZOOM = 180 / AGE, REST_SNAP = 8, RING_ROOM = 78;
  const COLORS = ["#7A5A2B", "#7B3F6E", "#2F5F8A", "#3E7167", "#A0522D", "#5B5F97", "#8A7B2E", "#4F6F52"];
  const ACCENT = "#7A5A2B", INK = "#1C1B18", INK2 = "#47443D", MUTED = "#7A756A", PAPER = "#F5F2EA", RULE = "#CFC8B8", UP = "#7B3F6E", DOWN = "#2F5F8A";
  const SANS = '"PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif';
  const SERIF = '"Songti SC", "STSong", "Noto Serif SC", "Source Han Serif SC", serif';
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const measure = document.createElement("canvas").getContext("2d");

  let trees = null, loadedFor = "", loading = false, error = "", NOW = 0, months = [];
  let box = null, canvas = null, ctx = null, tip = null, card = null, tabs = [];
  let W = 0, H = 0, dpr = 1, cx = 0, cy = 0, r = 400, railGap = 70, railNames = true, R = 1200, F = 1500, yTop = -300, viewH = 600, flatX = 1;
  let zoom = 1, ppd = 3.5, viewDays = 180, scroll = 0, aim = 0, zoomAim = 1, anchor = null;
  let angle = 0, current = 0, flat = false, flatK = 0, n = 0, slots = 6, step = 60;
  let macro = [], ringAim = 0, ringEvents = [], analysisOpen = false, cxShift = 0, shiftFrame = 0;
  let activeRes = 0, lastState = "", restTimer = 0, openedAt = 0;
  let selected = null, hover = null, hits = [], queued = false, settling = false, turnFrame = 0, driftFrame = 0, morphFrame = 0, glideFrame = 0;

  const ageOf = date => Math.min(AGE, (NOW - Date.parse(date)) / DAY);
  const maxScroll = () => Math.max(0, AGE - viewDays);
  const yOf = age => (age - scroll) * ppd + yTop;
  const flatLike = () => 1 + (flatX - 1) * flatK;
  // Narrow at the top (today) and spreading as lines go back in time, like a tree on its trunk; laid flat the lines stand straight.
  // The spread is kept gentle so laying the tree flat reads as a widening rather than every line swinging upright.
  const taper = age => { const t = clamp(age / AGE, 0, 1), w = .8 + .2 * (1 - (1 - t) * (1 - t)); return 1 - (1 - w) * (1 - flatK); };
  const inWindow = (age, margin = 0) => age >= scroll - margin && age <= scroll + viewDays + margin;

  function toTree(view, holding, index) {
    const lines = (view.threads || []).map(th => {
      const nodes = th.nodes.filter(nd => nd.date).map(nd => ({ ...nd, age: ageOf(nd.date) }));
      const ages = nodes.map(nd => nd.age);
      return { th, nodes, young: Math.min(...ages), old: Math.max(...ages) };
    }).filter(line => line.nodes.length).sort((a, b) => b.nodes.length - a.nodes.length);
    // The busiest lines stand nearest the trunk; every other line steps back so turning shows real depth.
    const m = lines.length;
    lines.forEach((line, k) => {
      const seat = k % 2 ? (k + 1) / 2 : -k / 2, u = m > 1 ? (seat + (m % 2 ? 0 : .5)) / (m / 2) : 0;
      Object.assign(line, { u, zn: .3 * (1 - u * u) + (k % 2 ? -.14 : .14), color: COLORS[k % COLORS.length], open: line.th.status === "进行中" });
    });
    const links = side => (view.chain?.links || []).filter(l => l.side === side && l.date).map(l => ({ ...l, age: ageOf(l.date), slot: null }));
    return { code: view.code, index, name: view.chain?.name || holding.name || view.code, industry: "", lines, ups: links("上游"), downs: links("下游") };
  }

  // ---- Text --------------------------------------------------------------------------------------
  const sprites = new Map();
  function sprite(str, font, color, halo) {
    const key = `${str}|${font}|${color}|${halo}|${dpr}`;
    let s = sprites.get(key);
    if (s) return s;
    const size = parseFloat(font.match(/([\d.]+)px/)[1]);
    measure.font = font;
    const tw = Math.ceil(measure.measureText(str).width), w = tw + halo * 2 + 2, h = Math.ceil(size * 1.5) + halo * 2, base = halo + Math.ceil(size * 1.12);
    const c = document.createElement("canvas");
    c.width = w * dpr; c.height = h * dpr;
    const g = c.getContext("2d");
    g.scale(dpr, dpr);
    g.font = font;
    if (halo) { g.lineJoin = "round"; g.lineWidth = halo; g.strokeStyle = PAPER; g.strokeText(str, halo + 1, base); }
    g.fillStyle = color;
    g.fillText(str, halo + 1, base);
    s = { c, w, h, base, pad: halo + 1, tw };
    sprites.set(key, s);
    return s;
  }

  function write(str, x, y, font, color, align = "center", halo = 3, scale = 1) {
    const s = sprite(str, font, color, halo), tw = s.tw * scale;
    let left = (align === "center" ? x - tw / 2 : align === "right" ? x - tw : x) - s.pad * scale, top = y - s.base * scale;
    if (scale === 1) { left = Math.round(left * dpr) / dpr; top = Math.round(top * dpr) / dpr; }
    ctx.drawImage(s.c, left, top, s.w * scale, s.h * scale);
    return tw;
  }

  const wrapped = new Map();
  function wrap(str, width, font) {
    const key = `${str}|${Math.round(width / 6)}|${font}`;
    if (wrapped.has(key)) return wrapped.get(key);
    measure.font = font;
    const out = [];
    let row = "";
    for (const ch of str) {
      if (measure.measureText(row + ch).width > width && row) { out.push(row); row = ch; } else row += ch;
    }
    out.push(row);
    wrapped.set(key, out);
    return out;
  }

  // ---- Geometry ----------------------------------------------------------------------------------
  function layout() {
    // clientWidth ignores the view's entrance animation; a measured rectangle would be scaled while it runs.
    const host = box.querySelector(".tree-host");
    W = host.clientWidth; H = host.clientHeight;
    if (!W || !H) return;
    const ratio = Math.min(2, devicePixelRatio || 1);
    if (ratio !== dpr) { dpr = ratio; sprites.clear(); }
    activeRes = 0;
    resolution(dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    cxShift = shiftTarget();
    cx = W / 2 - cxShift; cy = H / 2;
    railNames = W > 640;
    r = railNames ? Math.min(W * .31, 430) : W * .36;
    railGap = railNames ? r * .17 : 14;
    R = r * (railNames ? 2.3 : 2) / Math.sin(step * RAD);
    F = Math.max(1200, R * 1.25);
    yTop = -H / 2 + 74 + RING_ROOM;
    viewH = H - 74 - RING_ROOM - 16;
    flatX = railNames ? clamp((W / 2 - 160 - railGap) / r, 1, 1.5) : 1;
    setZoom(zoom);
    requestDraw();
  }

  function setZoom(z) {
    zoom = clamp(z, MIN_ZOOM, MAX_ZOOM);
    ppd = viewH / 180 * zoom;
    viewDays = viewH / ppd;
    scroll = clamp(scroll, 0, maxScroll());
    aim = clamp(aim, 0, maxScroll());
  }

  function projector(i) {
    const a = (i * step + angle) * RAD, ca = Math.cos(a), sa = Math.sin(a), pitch = PITCH * (1 - flatK);
    const cp = Math.cos(pitch), sp = Math.sin(pitch), xs = flatLike(), zs = 1 - flatK;
    return (x, y, z) => {
      const lx = x * xs, lz = z * zs;
      const X = lx * ca + lz * sa + R * sa, Z = -lx * sa + lz * ca + R * ca - R;
      const Y = y * cp + Z * sp;
      // Perspective scale ignores height, like a shift lens: upright lines stay upright on screen, so every tree leans only by its taper.
      const s = F / Math.max(60, F - Z * cp);
      return { x: cx + X * s, y: cy + Y * s, s };
    };
  }

  function stroke(points, color, width) {
    ctx.beginPath();
    points.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  }

  // ---- One tree ----------------------------------------------------------------------------------
  function linePath(P, line, from, to, segments) {
    const pts = [];
    for (let k = 0; k <= segments; k++) {
      const age = from + (to - from) * k / segments, t = taper(age);
      pts.push(P(line.u * r * t, yOf(age), line.zn * r * t));
    }
    return pts;
  }

  function fork(P, line) {
    const t = taper(line.old), x = line.u * r * t, z = line.zn * r * t, y = yOf(line.old), low = y + 26, pts = [];
    for (let k = 0; k <= 6; k++) {
      const q = k / 6, u = 1 - q, out = q * (2 - q);
      pts.push(P(out * x, (u * u + 2 * u * q) * low + q * q * y, out * z));
    }
    return pts;
  }

  function drawTree(i) {
    const tree = trees[i], P = projector(i), a = (i * step + angle) * RAD, c = Math.cos(a), depth = (1 - c) / 2;
    const isCurrent = i === current, near = isCurrent ? clamp(1 - Math.abs(Math.atan2(Math.sin(a), c)) / (38 * RAD), 0, 1) : 0;
    // Fading by depth alone keeps the opacity continuous when the current tree changes mid-turn.
    const base = Math.max(.14 + .86 * Math.pow(1 - depth, 10), selected?.macro?.codes.has(tree.code) ? .62 : 0) * (isCurrent ? 1 : 1 - flatK);
    if (base < .01) return;
    const scale = P(0, 0, 0).s, close = depth < .2, segments = close ? 10 : 3;
    const dimmed = line => selected?.line && selected.line !== line;
    ctx.lineCap = "round";

    if (close) {
      ctx.globalAlpha = base * .55;
      ctx.beginPath();
      const radius = r + railGap;
      months.forEach(m => {
        if (!inWindow(m.age, 30)) return;
        const y = yOf(m.age);
        for (let k = 0; k <= 24; k++) {
          const p = P(Math.cos(k * 15 * RAD) * radius, y, Math.sin(k * 15 * RAD) * radius * .55);
          k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y);
        }
      });
      ctx.strokeStyle = RULE;
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    ctx.globalAlpha = base;
    stroke([P(0, Math.max(yOf(0) - 34, yTop - 34), 0), P(0, Math.min(yOf(AGE), yOf(scroll + viewDays + 30)), 0)], INK2, Math.max(1, 2.6 * scale));

    if (near > .02) { rail(tree.ups, -1, UP, P, near, base); rail(tree.downs, 1, DOWN, P, near, base); }

    if (near > .02) {
      ctx.globalAlpha = near * base;
      ctx.fillStyle = ACCENT;
      macro.forEach(e => {
        if (!inWindow(e.age) || !e.codes.has(tree.code)) return;
        const p = P(0, yOf(e.age), 0), q = selected?.macro === e || hover?.macro === e ? 7 : 4.6;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y - q); ctx.lineTo(p.x + q, p.y); ctx.lineTo(p.x, p.y + q); ctx.lineTo(p.x - q, p.y);
        ctx.closePath();
        ctx.fill();
        hits.push({ x: p.x, y: p.y, kind: "macro", macro: e, pad: 9 });
      });
    }

    tree.lines.forEach(line => {
      const from = Math.max(line.young, scroll - 20), to = Math.min(line.old, scroll + viewDays + 20), strong = selected?.line === line;
      if (from > to && !inWindow(line.young, 20)) return;
      const width = Math.max(.8, (strong ? 3.2 : 2) * scale);
      ctx.globalAlpha = base * (dimmed(line) ? .22 : 1);
      if (from <= to && to > from) stroke(linePath(P, line, from, to, segments), line.color, width);
      if (inWindow(line.old, 30)) stroke(fork(P, line), line.color, width);
      // An open matter runs on to today as a dotted line, so its state shows without reading the label.
      if (line.open && close && line.young > 0 && scroll < line.young) {
        ctx.setLineDash([2, 6]);
        ctx.globalAlpha *= .7;
        stroke(linePath(P, line, Math.max(0, scroll - 20), line.young, 4), line.color, Math.max(.8, 1.4 * scale));
        ctx.setLineDash([]);
        ctx.globalAlpha = base * (dimmed(line) ? .22 : 1);
      }
      const rows = line.nodes.filter(nd => inWindow(nd.age, 8) && (nd.major || close)).map(nd => {
        const t = taper(nd.age);
        return { nd, p: P(line.u * r * t, yOf(nd.age), line.zn * r * t) };
      });
      if (close) {
        ctx.beginPath();
        rows.forEach(({ nd, p }) => { if (nd.major) { const q = 5.4 * Math.max(.45, p.s); ctx.moveTo(p.x + q, p.y); ctx.arc(p.x, p.y, q, 0, 7); } });
        ctx.fillStyle = PAPER;
        ctx.fill();
      }
      ctx.beginPath();
      rows.forEach(({ nd, p }) => {
        const size = (nd.major ? 4 : 1.9) + (selected?.node === nd || hover?.node === nd ? 2 : 0), q = size * Math.max(.45, p.s);
        ctx.moveTo(p.x + q, p.y);
        ctx.arc(p.x, p.y, q, 0, 7);
        if (isCurrent && !dimmed(line) && near > .3) hits.push({ x: p.x, y: p.y, kind: "node", node: nd, line, pad: nd.major ? 10 : 6 });
      });
      ctx.fillStyle = line.color;
      ctx.fill();
    });

    ctx.globalAlpha = Math.min(1, base * 1.3);
    const head = P(0, yTop - 44, 0), size = isCurrent ? 25 : 18, em = 25 * head.s;
    const nameWidth = write(tree.name, head.x, head.y, `600 ${size}px ${SERIF}`, INK, "center", 4, 25 / size * head.s);
    let tail = 0;
    if (tree.industry && near > .02) {
      ctx.globalAlpha = near * base;
      tail = 10 + write(tree.industry, head.x + nameWidth / 2 + 10, head.y, `12px ${SANS}`, MUTED, "left");
    }
    if (near > .02) labels(tree, P, near, base, { x: head.x - nameWidth / 2 - 4, y: head.y - em, w: nameWidth + tail + 8, h: em * 1.25 });
  }

  // Suppliers come in from the top left and customers leave at the bottom right; each curve lands on the trunk at its date.
  function rail(links, sign, color, P, near, base) {
    if (!links.length) return;
    const x0 = sign * (r + railGap), z0 = -r * .35, faded = selected?.line ? .35 : 1;
    const shown = links.filter(l => inWindow(l.age)).sort((p, q) => p.age - q.age), rows = shown.slice(0, MAX_NAMES), bottom = H / 2 - 34;
    const title = `${sign < 0 ? "上游" : "下游"} ${links.length} 家`;
    const headY = sign < 0 ? yTop - 10 : bottom + 20, head = P(x0, headY, 0);
    ctx.globalAlpha = near * base;
    write(title, railNames ? head.x : sign < 0 ? 10 : W - 10, head.y, `600 12px ${SANS}`, color, (sign < 0) === railNames ? "right" : "left");
    rows.forEach((l, k) => {
      const target = sign < 0 ? yTop + 8 + k * 19 : bottom - (rows.length - 1 - k) * 19;
      l.slot = l.slot === null ? target : l.slot + (target - l.slot) * .22;
      if (Math.abs(target - l.slot) > .5) settling = true;
      const edge = clamp(Math.min(scroll > 0 ? l.age - scroll : 9, scroll < maxScroll() ? scroll + viewDays - l.age : 9) / 5, 0, 1), hot = hover?.link === l || selected?.link === l;
      const end = yOf(l.age), pts = [];
      for (let q = 0; q <= 12; q++) {
        const t = q / 12, u = 1 - t, b0 = u * u * u, b1 = 3 * u * u * t, b2 = 3 * u * t * t;
        pts.push(P(b0 * x0 + b1 * x0 * .42 + b2 * x0 * .12, (b0 + b1) * l.slot + (b2 + t * t * t) * end, (b0 + b1) * z0 + b2 * z0 * .3));
      }
      ctx.globalAlpha = near * base * edge * faded * (hot ? 1 : .3);
      stroke(pts, color, hot ? 1.8 : .9);
      const land = P(0, end, 0);
      ctx.globalAlpha = near * base * edge * faded;
      ctx.beginPath();
      ctx.arc(land.x, land.y, hot ? 3.6 : 2.4, 0, 7);
      ctx.fillStyle = color;
      ctx.fill();
      hits.push({ x: land.x, y: land.y, kind: "link", link: l, pad: 8 });
      if (!railNames) return;
      const at = P(x0, l.slot, z0), font = `${hot ? "600 " : ""}12px ${SANS}`, fit = wrap(l.name, sign < 0 ? at.x - 18 : W - at.x - 18, font);
      const tw = write(fit.length > 1 ? fit[0].slice(0, -1) + "…" : fit[0], at.x + sign * 6, at.y + 4, font, color, sign < 0 ? "right" : "left");
      hits.push({ kind: "link", link: l, box: { x: sign < 0 ? at.x - 6 - tw : at.x + 6, y: at.y - 9, w: tw, h: 18 } });
    });
    if (railNames && shown.length > rows.length) {
      const y = sign < 0 ? yTop + 8 + rows.length * 19 : bottom - rows.length * 19, at = P(x0, y, z0);
      ctx.globalAlpha = near * base * faded;
      write(`还有 ${shown.length - rows.length} 家`, at.x + sign * 6, at.y + 4, `12px ${SANS}`, MUTED, sign < 0 ? "right" : "left");
    }
  }

  function labels(tree, P, near, base, name) {
    // The tree's name is taken space too, so no label is drawn over it.
    const boxes = [name];
    const place = (b, limit) => {
      for (let guard = 0; guard < 12; guard++) {
        const hit = boxes.find(q => b.x < q.x + q.w && b.x + b.w > q.x && b.y < q.y + q.h && b.y + b.h > q.y);
        if (!hit) break;
        b.y = hit.y + hit.h + 2;
        if (b.y > limit) return false;
      }
      boxes.push(b);
      return true;
    };

    ctx.globalAlpha = near * base;
    months.forEach(m => {
      if (!inWindow(m.age)) return;
      const p = P(0, yOf(m.age), 0);
      write(m.label, p.x + 7, p.y - 3, `11px ${SANS}`, MUTED, "left");
    });

    const m = tree.lines.length || 1, spacing = 2 * r * flatLike() / m;
    const room = Math.max(railNames ? 92 : 52, spacing - 6), font = `600 ${railNames ? 13 : 11}px ${SANS}`, rowH = railNames ? 17 : 14;
    [...tree.lines].sort((p, q) => p.young - q.young).forEach(line => {
      if (line.old < scroll || line.young > scroll + viewDays) return;
      const at = Math.max(line.young, scroll + 2), t = taper(at), p = P(line.u * r * t, yOf(at), line.zn * r * t);
      const rows = wrap(line.th.title, room, font), h = rows.length * rowH + (railNames ? 15 : 0) + 4;
      const b = { x: p.x - room / 2, y: Math.max(40, p.y - h - 7), w: room, h };
      // A label that would cover the name steps aside, toward the side its line stands on, and keeps its height.
      if (b.y < name.y + name.h && b.y + h > name.y && b.x < name.x + name.w && b.x + room > name.x) b.x = p.x >= name.x + name.w / 2 ? name.x + name.w + 4 : name.x - room - 4;
      if (!place(b, H - 40)) return;
      const mid = b.x + room / 2;
      ctx.globalAlpha = near * base * (selected?.line && selected.line !== line ? .3 : 1);
      rows.forEach((row, k) => write(row, mid, b.y + (k + 1) * rowH - 3, font, line.color, "center", 4));
      if (railNames) write(line.th.status, mid, b.y + rows.length * rowH + 11, `11px ${SANS}`, MUTED);
      hits.push({ kind: "line", line, box: b });
    });

    // Zooming in, or laying the tree flat, leaves room to name the key events themselves.
    if (ppd < (flatK > .5 ? 3 : 6.5) || !railNames) return;
    const textRoom = Math.max(108, spacing * 1.2), lines = ppd >= 9 ? 2 : 1, small = `12px ${SANS}`;
    tree.lines.forEach(line => {
      if (selected?.line && selected.line !== line) return;
      line.nodes.forEach(nd => {
        if (!nd.major || !inWindow(nd.age)) return;
        const t = taper(nd.age), p = P(line.u * r * t, yOf(nd.age), line.zn * r * t), all = wrap(nd.title, textRoom, small);
        const rows = all.slice(0, lines), b = { x: p.x + 8, y: p.y - 8, w: textRoom, h: rows.length * 15 + 2 };
        const free = !boxes.some(q => b.x < q.x + q.w && b.x + b.w > q.x && b.y < q.y + q.h && b.y + b.h > q.y);
        if (!free || b.y < 40 || b.y > H - 30) return;
        boxes.push(b);
        ctx.globalAlpha = near * base;
        rows.forEach((row, k) => write(k === rows.length - 1 && all.length > rows.length ? row.slice(0, -1) + "…" : row, b.x, b.y + (k + 1) * 15 - 3, small, INK2, "left"));
      });
    });
  }

  // While anything moves the canvas drops to one pixel per CSS pixel, which is what keeps turning smooth; it sharpens once the view rests.
  function resolution(d) {
    if (d === activeRes) return;
    activeRes = d;
    canvas.width = W * d; canvas.height = H * d;
    ctx.setTransform(d, 0, 0, d, 0, 0);
  }

  function draw() {
    if (!ctx || !W || !trees || box?.hidden) return;
    const state = `${angle}|${scroll}|${zoom}|${flatK}|${cx}`;
    if (state !== lastState) {
      if (lastState) { resolution(1); clearTimeout(restTimer); restTimer = setTimeout(() => { resolution(dpr); draw(); }, 140); }
      lastState = state;
    }
    settling = false;
    ctx.clearRect(0, 0, W, H);
    hits = [];
    trees.map((_, i) => i).sort((p, q) => Math.cos((p * step + angle) * RAD) - Math.cos((q * step + angle) * RAD)).forEach(drawTree);
    ctx.globalAlpha = 1;
    const fade = ctx.createLinearGradient(0, H - 26, 0, H);
    fade.addColorStop(0, "rgba(245, 242, 234, 0)");
    fade.addColorStop(1, PAPER);
    ctx.fillStyle = fade;
    ctx.fillRect(0, H - 26, W, 26);
    drawRing();
    drawMacroLinks();
    if (settling) requestDraw();
  }

  // ---- Macro ring: fixed above the trees, it holds the events of the current time window and brings one that concerns the front tree to the middle.
  function ringList() {
    const list = macro.filter(e => inWindow(e.age));
    const key = list.map(e => e.id).join();
    if (key !== ringEvents.key) { ringEvents = list; ringEvents.key = key; aimRing(); }
    return ringEvents;
  }

  const ringStep = () => 360 / Math.max(ringEvents.length, 8);

  function aimRing() {
    const list = ringEvents, code = trees?.[current]?.code;
    const target = selected?.macro && list.includes(selected.macro) ? selected.macro : list.find(e => e.codes.has(code));
    if (!target) return;
    const wanted = -list.indexOf(target) * ringStep();
    ringAim += ((wanted - ringAim + 540) % 360 + 360) % 360 - 180;
    requestDraw();
  }

  const ringPoint = theta => {
    const rad = theta * RAD, rx = Math.min(W * .4, 500), ry = 15 * (1 - flatK * .7), c = Math.cos(rad);
    return { x: cx + Math.sin(rad) * rx, y: 28 + c * ry, facing: (c + 1) / 2 };
  };

  function drawRing() {
    const list = ringList();
    if (!macro.length) return;
    ctx.globalAlpha = .75;
    ctx.beginPath();
    for (let k = 0; k <= 48; k++) {
      const p = ringPoint(k * 7.5);
      k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y);
    }
    ctx.strokeStyle = RULE;
    ctx.lineWidth = 1;
    ctx.stroke();
    const code = trees[current]?.code, gap = ringStep();
    const placed = list.map((e, k) => {
      const target = k * gap + ringAim;
      e.theta = e.theta === undefined ? target : e.theta;
      const diff = ((target - e.theta + 540) % 360 + 360) % 360 - 180;
      e.theta += diff * .16;
      if (Math.abs(diff) > .3) settling = true;
      return { e, p: ringPoint(e.theta) };
    }).sort((a, b) => a.p.facing - b.p.facing);
    const boxes = [];
    placed.forEach(({ e, p }) => {
      const own = e.codes.has(code), hot = selected?.macro === e || hover?.macro === e;
      const edge = clamp(Math.min(scroll > 0 ? e.age - scroll : 9, scroll < maxScroll() ? scroll + viewDays - e.age : 9) / 4, 0, 1);
      ctx.globalAlpha = edge * (.28 + .72 * p.facing);
      ctx.beginPath();
      const size = (hot ? 6 : own ? 4.6 : 3.2) * (.6 + .4 * p.facing);
      ctx.arc(p.x, p.y, size, 0, 7);
      ctx.fillStyle = own || hot ? ACCENT : MUTED;
      ctx.fill();
      hits.push({ x: p.x, y: p.y, kind: "macro", macro: e, pad: 10 });
    });
    // Words only on the near side; the nearest event wins when labels would overlap.
    [...placed].reverse().forEach(({ e, p }) => {
      if (p.facing < .5) return;
      const own = e.codes.has(code), hot = selected?.macro === e, front = p.facing > .93;
      const font = `${front || hot ? "600 " : ""}${railNames ? 12.5 : 11}px ${SANS}`, room = railNames ? 230 : 130;
      const rows = wrap(`${e.date.slice(5)} ${e.title}`, room, font), label = rows.length > 1 ? rows[0].slice(0, -1) + "…" : rows[0];
      measure.font = font;
      const w = measure.measureText(label).width + 8, b = { x: p.x - w / 2, y: p.y + 6, w, h: 16 };
      if (b.x < 2 || b.x + b.w > W - 2 || boxes.some(q => b.x < q.x + q.w && b.x + b.w > q.x)) return;
      boxes.push(b);
      ctx.globalAlpha = clamp((p.facing - .5) * 2.4, 0, 1) * (own || hot ? 1 : .72);
      write(label, p.x, p.y + 18, font, own || hot ? ACCENT : INK2);
      hits.push({ kind: "macro", macro: e, box: b });
    });
    ctx.globalAlpha = 1;
  }

  // A selected event links to every holding it concerns; otherwise only the front tree's own event does, faintly.
  function drawMacroLinks() {
    const front = ringEvents.find(e => e.theta !== undefined && ringPoint(e.theta).facing > .93);
    const event = selected?.macro || (front?.codes.has(trees[current]?.code) ? front : null);
    if (!event || event.theta === undefined) return;
    const from = ringPoint(event.theta);
    ctx.strokeStyle = ACCENT;
    ctx.lineWidth = 1.2;
    trees.forEach((tree, i) => {
      if (!event.codes.has(tree.code) || (flat && i !== current) || (!selected?.macro && i !== current)) return;
      const head = projector(i)(0, yTop - 66, 0), c = Math.cos((i * step + angle) * RAD);
      ctx.globalAlpha = selected?.macro ? (c > .5 ? .8 : .42) : .4;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y + 20);
      ctx.quadraticCurveTo(from.x, (from.y + head.y) / 2, head.x, head.y);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  }

  const requestDraw = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; draw(); }); } };

  // ---- Picking and cards -------------------------------------------------------------------------
  function pick(x, y) {
    let best = null, gap = Infinity;
    hits.forEach(hit => {
      if (hit.box) return;
      const d = Math.hypot(hit.x - x, hit.y - y);
      if (d <= hit.pad && d < gap) { best = hit; gap = d; }
    });
    return best || hits.find(hit => hit.box && x >= hit.box.x && x <= hit.box.x + hit.box.w && y >= hit.box.y && y <= hit.box.y + hit.box.h) || null;
  }

  function treeAt(x) {
    if (Math.abs(x - cx) < (r + railGap) * flatLike()) return -1;
    let best = -1, nearest = -2;
    trees.forEach((_, i) => {
      if (i === current) return;
      const c = Math.cos((i * step + angle) * RAD), p = projector(i)(0, 0, 0);
      if (Math.abs(x - p.x) <= Math.max(36, r * Math.abs(c) * p.s) && c > nearest) { best = i; nearest = c; }
    });
    return best;
  }

  function closeCard() {
    if (card) card.hidden = true;
    selected = null;
    requestDraw();
  }

  function showCard(meta, title, body, url, unverified, actions) {
    card.replaceChildren();
    const add = (tag, cls, str) => { const e = document.createElement(tag); e.className = cls; e.textContent = str; card.append(e); return e; };
    add("p", "meta", meta);
    add("h3", "", title);
    if (body) add("p", "tree-body", body);
    const open = (unverified || []).map(String).filter(Boolean);
    if (open.length) add("p", "meta", "未核对：" + open.join("、"));
    const links = document.createElement("p");
    links.className = "tree-links";
    if (/^https?:\/\//.test(url || "")) {
      const a = document.createElement("a");
      a.textContent = "查看来源"; a.href = url; a.target = "_blank"; a.rel = "noopener";
      links.append(a);
    }
    actions.forEach(([label, run]) => {
      const go = document.createElement("button");
      go.textContent = label;
      go.addEventListener("click", run);
      links.append(go);
    });
    card.append(links);
    const x = add("button", "tree-x", "×");
    x.setAttribute("aria-label", "关闭");
    x.addEventListener("click", closeCard);
    card.hidden = false;
  }

  function choose(hit) {
    if (hit.kind === "line") {
      selected = { line: hit.line };
      const th = hit.line.th;
      showCard(`${th.theme} · ${th.status} · ${hit.line.nodes.length} 条`, th.title, th.summary, "", th.unverified, [["在脉络中查看", () => A.timelineShowStock(trees[current].code)]]);
    } else if (hit.kind === "node") {
      selected = { line: hit.line, node: hit.node };
      showCard(`${hit.line.th.title} · ${hit.node.date}`, hit.node.title, hit.node.summary, hit.node.url, hit.node.unverified, [["在脉络中查看", () => A.timelineShowNode(hit.node.id)]]);
    } else {
      if (hit.kind === "macro") return pickMacro(hit.macro);
      const l = hit.link;
      selected = { link: l };
      showCard(`${l.side} · ${l.date}`, l.name, l.note, l.url, l.unverified, [["在脉络中查看", () => A.timelineShowChain(l.id)]]);
    }
    requestDraw();
  }

  const macroQuestion = e => `分析这条宏观事件对我的持仓的影响，并给出后续预判：${e.title}（${e.date}）。${e.summary}`;

  function pickMacro(event) {
    selected = { macro: event };
    const names = trees.filter(t => event.codes.has(t.code)).map(t => t.name);
    const body = `${event.summary}\n关联（智能体判断）：${event.reason || "未标注"}\n相关持仓：${names.join("、") || "暂无"}`;
    showCard(`${event.kind} · ${event.date}`, event.title, body, event.url, event.unverified, [["分析影响", () => ask(macroQuestion(event))]]);
    aimRing();
    if (analysisOpen) fillPresets();
    requestDraw();
  }

  // The analysis drawer sits inside this view: presets follow the front tree, answers come from the same research job as anywhere else.
  function presets() {
    const tree = trees?.[current], list = [];
    if (!tree) return list;
    if (selected?.macro) list.push(["这条宏观事件的影响", macroQuestion(selected.macro)]);
    const recent = macro.slice(0, 6).map(e => `${e.title}（${e.date}）`).join("；");
    if (recent) list.push(["近期宏观对持仓的影响", `综合近期宏观事件，分析对我的持仓的影响，并给出后续预判。近期事件：${recent}`]);
    if (tree.industry) list.push([`${tree.industry} 近期情况`, `分析${tree.industry}行业近期情况，以及对${tree.name}的影响，并给出后续预判。`]);
    (tree.concepts || []).forEach(name => list.push([`${name} 近期情况`, `分析“${name}”这一概念近期情况，以及对${tree.name}的影响，并给出后续预判。`]));
    list.push([`${tree.name} 近期情况`, `分析${tree.name}近期的重要事件和所处阶段，并给出后续预判。`]);
    return list;
  }

  function fillPresets() {
    const row = box?.querySelector(".tree-presets");
    if (!row) return;
    row.replaceChildren(...presets().map(([label, question]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", () => ask(question));
      return b;
    }));
  }

  // The panel slides in by CSS while the trees glide aside on the canvas, so the two arrive together.
  const shiftTarget = () => (analysisOpen && W > 900 ? 220 : 0);

  function setAnalysis(open) {
    analysisOpen = open;
    box.querySelector(".tree-analysis").classList.toggle("open", open);
    box.querySelector("[data-tree-analyze]").classList.toggle("on", open);
    if (open) fillPresets();
    cancelAnimationFrame(shiftFrame);
    const from = cxShift, to = shiftTarget(), start = performance.now(), ms = reduce ? 0 : 320;
    const tick = now => {
      const k = ms ? Math.min(1, (now - start) / ms) : 1;
      cxShift = from + (to - from) * (1 - Math.pow(1 - k, 3));
      cx = W / 2 - cxShift;
      draw();
      if (k < 1) shiftFrame = requestAnimationFrame(tick);
    };
    shiftFrame = requestAnimationFrame(tick);
  }

  function ask(question) {
    if (!analysisOpen) setAnalysis(true);
    A.askResearch(question);
  }

  // ---- Motion ------------------------------------------------------------------------------------
  // Runs only when the front tree actually changes: touching the tab row every frame forced a page layout per frame.
  function setCurrent() {
    const i = ((Math.round(-angle / step) % slots) + slots) % slots;
    if (i >= n || i === current) return;
    // A macro event belongs to every tree, so it stays open while they turn.
    if (!selected?.macro) closeCard();
    current = i;
    aimRing();
    if (analysisOpen) fillPresets();
    syncTabs();
  }

  function syncTabs() {
    tabs.forEach((t, k) => { t.classList.toggle("on", k === current); t.setAttribute("aria-selected", k === current); });
    tabs[current]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function animateTurn(to) {
    cancelAnimationFrame(turnFrame);
    const from = angle, start = performance.now(), ms = reduce ? 0 : 520;
    const tick = now => {
      const k = ms ? Math.min(1, (now - start) / ms) : 1;
      angle = k >= 1 ? to : from + (to - from) * (1 - Math.pow(1 - k, 3));
      setCurrent();
      draw();
      if (k < 1) turnFrame = requestAnimationFrame(tick);
    };
    turnFrame = requestAnimationFrame(tick);
  }

  // Slots without a tree only exist when there are fewer than six holdings; a turn never stops on one.
  function snap(a) {
    let k = Math.round(-a / step);
    const m = ((k % slots) + slots) % slots;
    if (m >= n) k += m - n + 1 <= slots - m ? -(m - n + 1) : slots - m;
    return -k * step;
  }

  // A turn stops wherever it ends; only a tree already close to the front is pulled to the centre.
  function rest(a) {
    const centre = snap(a);
    return Math.abs(centre - a) <= REST_SNAP ? centre : a;
  }

  function go(i) {
    const base = -i * step, to = base + Math.round((angle - base) / 360) * 360;
    if (flat) { angle = to; setCurrent(); requestDraw(); return; }
    animateTurn(to);
  }
  const shift = d => go(((current + d) % n + n) % n);

  function setFlat(on) {
    flat = on;
    box.querySelector("[data-tree-flat]").classList.toggle("on", on);
    box.querySelector("[data-tree-flat]").setAttribute("aria-pressed", on);
    if (on) { cancelAnimationFrame(turnFrame); angle = snap(angle); setCurrent(); }
    cancelAnimationFrame(morphFrame);
    const from = flatK, to = on ? 1 : 0, start = performance.now(), ms = reduce ? 0 : 560;
    const tick = now => {
      const k = ms ? Math.min(1, (now - start) / ms) : 1, e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      flatK = from + (to - from) * e;
      draw();
      if (k < 1) morphFrame = requestAnimationFrame(tick);
    };
    morphFrame = requestAnimationFrame(tick);
  }

  // Wheel notches and zoom steps ease toward their target, so a large step does not jump a whole screen in one frame.
  function glide() {
    if (glideFrame) return;
    let last = performance.now();
    const tick = now => {
      const k = reduce ? 1 : 1 - Math.exp(-(now - last) / 70);
      last = now;
      if (anchor) {
        setZoom(zoom + (zoomAim - zoom) * k);
        scroll = clamp(anchor.age - (anchor.y - yTop) / ppd, 0, maxScroll());
        aim = scroll;
        if (Math.abs(zoomAim - zoom) < .002) { setZoom(zoomAim); anchor = null; }
      } else {
        scroll += (aim - scroll) * k;
        if (Math.abs(aim - scroll) < .02) scroll = aim;
      }
      draw();
      glideFrame = anchor || scroll !== aim ? requestAnimationFrame(tick) : 0;
    };
    glideFrame = requestAnimationFrame(tick);
  }

  function zoomBy(factor, localY = H / 2) {
    zoomAim = clamp((anchor ? zoomAim : zoom) * factor, MIN_ZOOM, MAX_ZOOM);
    const y = localY - cy;
    anchor = { y, age: (y - yTop) / ppd + scroll };
    glide();
  }

  function coast(v) {
    cancelAnimationFrame(driftFrame);
    if (reduce || Math.abs(v) < .01) return;
    let last = performance.now();
    const tick = now => {
      const dt = now - last;
      last = now;
      scroll = clamp(scroll + v * dt, 0, maxScroll());
      aim = scroll;
      v *= Math.pow(.995, dt);
      requestDraw();
      if (Math.abs(v) > .004 && scroll > 0 && scroll < maxScroll()) driftFrame = requestAnimationFrame(tick);
    };
    driftFrame = requestAnimationFrame(tick);
  }

  // Speed comes from the last 100 ms of the press and is capped: one fast pair of events must not fling the view across the year.
  const speed = (track, axis) => {
    const end = track.at(-1), first = track.find(q => end.t - q.t <= 100) || end, dt = end.t - first.t;
    return dt < 10 || performance.now() - end.at > 80 ? 0 : clamp((end[axis] - first[axis]) / dt, -2.5, 2.5);
  };

  // "x" or "y" when the recent stroke is within 18 degrees of that axis, otherwise "" for a true diagonal.
  const heading = track => {
    const end = track.at(-1), first = track.find(q => end.t - q.t <= 140) || track[0];
    const across = Math.abs(end.x - first.x), along = Math.abs(end.y - first.y);
    if (along <= across * .325) return "x";
    return across <= along * .325 ? "y" : "";
  };

  function bind() {
    let press = null, pinch = null;
    const pointers = new Map();
    const at = e => { const b = canvas.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
    canvas.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      cancelAnimationFrame(turnFrame);
      cancelAnimationFrame(driftFrame);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      canvas.setPointerCapture(e.pointerId);
      if (pointers.size === 2) {
        const [p, q] = [...pointers.values()], d = Math.hypot(p.x - q.x, p.y - q.y), y = at({ clientX: 0, clientY: (p.y + q.y) / 2 })[1] - cy;
        pinch = { d, zoom, y, age: (y - yTop) / ppd + scroll };
        press = null;
        return;
      }
      press = { x: e.clientX, y: e.clientY, lx: e.clientX, ly: e.clientY, id: e.pointerId, moved: false, track: [{ x: e.clientX, y: e.clientY, t: e.timeStamp, at: performance.now() }] };
    });
    canvas.addEventListener("pointermove", e => {
      const [x, y] = at(e);
      // A context menu (Ctrl+click on a Mac) swallows the release, so a mouse moving with no button held has already let go.
      if (press && e.pointerId === press.id && !e.buttons) return release(e);
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && pointers.size === 2) {
        const [p, q] = [...pointers.values()];
        setZoom(pinch.zoom * Math.hypot(p.x - q.x, p.y - q.y) / pinch.d);
        zoomAim = zoom;
        scroll = aim = clamp(pinch.age - (pinch.y - yTop) / ppd, 0, maxScroll());
        requestDraw();
        return;
      }
      if (!press || e.pointerId !== press.id) {
        const hit = pick(x, y), node = hit?.node || null, link = hit?.link || null, event = hit?.macro || null;
        canvas.style.cursor = hit || treeAt(x) >= 0 ? "pointer" : "grab";
        if (node !== hover?.node || link !== hover?.link || event !== hover?.macro) { hover = { node, link, macro: event }; requestDraw(); }
        if (node || link || event) {
          tip.textContent = node ? `${node.date.slice(5)}  ${node.title}` : link ? `${link.side} · ${link.name}  ${link.date}` : `宏观 · ${event.title}  ${event.date}`;
          tip.hidden = false;
          tip.style.left = Math.max(8, Math.min(x + 12, W - tip.offsetWidth - 8)) + "px";
          tip.style.top = Math.max(8, y - tip.offsetHeight - 10) + "px";
        } else tip.hidden = true;
        return;
      }
      if (!press.moved) {
        if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < 8) return;
        press.moved = true;
        tip.hidden = true;
        canvas.style.cursor = "grabbing";
      }
      const dx = e.clientX - press.lx, dy = e.clientY - press.ly;
      press.lx = e.clientX; press.ly = e.clientY;
      press.track.push({ x: e.clientX, y: e.clientY, t: e.timeStamp, at: performance.now() });
      if (press.track.length > 12) press.track.shift();
      // Turning and moving through time run together on a diagonal; a nearly straight stroke snaps to one of them so a shaky hand does not nudge the other.
      const lean = heading(press.track);
      if (lean !== "y" && !flat) { angle += dx * step / (r * 1.1); setCurrent(); }
      if (lean !== "x" || flat) scroll = aim = clamp(scroll - dy / ppd, 0, maxScroll());
      requestDraw();
    });
    const release = e => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!press || e.pointerId !== press.id) return;
      const was = press;
      press = null;
      canvas.style.cursor = "grab";
      if (was.moved) {
        const lean = heading(was.track);
        const target = rest(angle + (lean === "y" || flat ? 0 : clamp(speed(was.track, "x") * 160 / (r * 1.1), -1.5, 1.5) * step));
        if (target !== angle) animateTurn(target);
        if (lean !== "x" || flat) coast(-speed(was.track, "y") / ppd);
      } else if (e.type === "pointerup") {
        const [x, y] = at(e), hit = pick(x, y), other = hit ? -1 : treeAt(x);
        if (hit) choose(hit);
        else if (other >= 0 && !flat) go(other);
        else closeCard();
      }
    };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("pointerleave", () => { tip.hidden = true; });

    let settle = null;
    canvas.addEventListener("wheel", e => {
      e.preventDefault();
      // Wheeling out of the industry map lands here; the rest of that burst must not scroll the trees back in time.
      if (performance.now() - openedAt < 600) return;
      tip.hidden = true;
      // A mouse notch is about 100 against a few for a pinch step; capped, one notch zooms like the + button.
      if (e.ctrlKey) { zoomBy(Math.exp(-clamp(e.deltaY, -40, 40) * .01), at(e)[1]); return; }
      const across = Math.abs(e.deltaX), along = Math.abs(e.deltaY);
      if (across > along * .325 && !flat) {
        cancelAnimationFrame(turnFrame);
        angle -= e.deltaX * step / (r * 1.1);
        setCurrent();
        requestDraw();
        clearTimeout(settle);
        settle = setTimeout(() => { const target = rest(angle); if (target !== angle) animateTurn(target); }, 130);
      }
      if (along > across * .325) {
        cancelAnimationFrame(driftFrame);
        aim = clamp(aim + e.deltaY / ppd, 0, maxScroll());
        anchor = null;
        glide();
      }
    }, { passive: false });
    new ResizeObserver(layout).observe(box.querySelector(".tree-host"));
  }

  document.addEventListener("click", e => {
    if (!e.target.closest?.("#tl-layers") || !trees) return;
    const tab = e.target.closest("[data-tree-tab]");
    if (tab) return go(+tab.dataset.treeTab);
    if (e.target.closest("[data-tree-flat]")) return setFlat(!flat);
    if (e.target.closest("[data-tree-in]")) return zoomBy(1.5);
    if (e.target.closest("[data-tree-out]")) return zoomBy(1 / 1.5);
    if (e.target.closest("[data-tree-analyze]")) return setAnalysis(!analysisOpen);
    if (e.target.closest("[data-tree-analysis-close]")) return setAnalysis(false);
  });

  // Capture phase: with a card open, Esc closes the card before the page would leave this view.
  document.addEventListener("keydown", e => {
    if (D.tlMode !== "layers" || !trees || !location.hash.startsWith("#timeline") || e.target.closest?.("input, select, textarea")) return;
    if (e.key === "Escape" && analysisOpen) { setAnalysis(false); e.stopImmediatePropagation(); }
    else if (e.key === "Escape" && card && !card.hidden) { closeCard(); e.stopImmediatePropagation(); }
    else if (e.key === "ArrowLeft") shift(-1);
    else if (e.key === "ArrowRight") shift(1);
    else if (e.key === "ArrowUp") { aim = clamp(aim - 20, 0, maxScroll()); glide(); }
    else if (e.key === "ArrowDown") { aim = clamp(aim + 20, 0, maxScroll()); glide(); }
  }, true);

  // ---- Page wiring -------------------------------------------------------------------------------
  function render() {
    box = document.getElementById("tl-layers");
    if (!box) return;
    const empty = box.querySelector(".tree-empty");
    if (!trees?.length) {
      empty.hidden = false;
      empty.textContent = loading ? "正在读取" : error || "暂无持仓";
      return;
    }
    empty.hidden = true;
    const node = box.querySelector("canvas");
    if (node !== canvas) {
      canvas = node;
      ctx = canvas.getContext("2d");
      tip = box.querySelector(".tl-tip");
      card = box.querySelector(".tree-card");
      selected = null;
      const row = box.querySelector(".tree-tabs");
      row.replaceChildren(...trees.map((tree, i) => {
        const b = document.createElement("button");
        b.textContent = tree.name;
        b.dataset.treeTab = i;
        b.setAttribute("role", "tab");
        return b;
      }));
      tabs = [...row.children];
      bind();
      box.querySelector("[data-tree-analyze]").classList.toggle("on", analysisOpen);
      fillPresets();
      syncTabs();
      box.querySelector("[data-tree-flat]").classList.toggle("on", flat);
    }
    layout();
  }

  async function load() {
    const codes = D.holdings.map(h => h.code), key = codes.join();
    openedAt = performance.now();
    if (loading) return render();
    if (trees && key === loadedFor) return render();
    if (!codes.length) { trees = []; loadedFor = key; return render(); }
    loading = true;
    trees = null;
    render();
    try {
      NOW = Date.parse(new Date().toISOString().slice(0, 10));
      months = [];
      for (let k = 0; ; k++) {
        const today = new Date(NOW), first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() - k, 1)), age = (NOW - first.getTime()) / DAY;
        if (age > AGE) break;
        months.push({ age, label: `${first.getUTCMonth() + 1}月` });
      }
      const views = await Promise.all(codes.map(code => A.api("/api/timeline/" + encodeURIComponent(code))));
      trees = views.map((view, i) => toTree(view, D.holdings[i], i));
      n = trees.length; slots = Math.max(n, 6); step = 360 / slots;
      // Open on the tree with the most suppliers and customers.
      current = trees.reduce((best, t, i) => t.ups.length + t.downs.length > trees[best].ups.length + trees[best].downs.length ? i : best, 0);
      angle = -current * step;
      loadedFor = key;
      error = "";
      canvas = null;
      // Macro events and industries add to the trees, so the view opens without waiting for them; the industry map can be slow, so neither waits for the other.
      const link = () => {
        macro.forEach(e => { e.codes = new Set(e.industries.flatMap(name => reach.get(name) || [])); });
        ringEvents.key = "";
        aimRing();
        if (analysisOpen) fillPresets();
        requestDraw();
      };
      let reach = new Map();
      A.api("/api/macro").then(info => {
        trees?.forEach(t => { t.concepts = info.concepts[t.code] || []; });
        macro = info.events.map(e => ({ ...e, age: ageOf(e.date), codes: new Set() }));
        link();
      }).catch(() => {});
      A.api("/api/industry").then(graph => {
        reach = new Map(graph.nodes.map(node => [node.name, node.reach || []]));
        const home = new Map(graph.nodes.flatMap(node => node.holdings.map(h => [h.code, node.name])));
        trees?.forEach(t => { t.industry = home.get(t.code) || ""; });
        link();
      }).catch(() => {});
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
      render();
    }
  }

  A.layers = { load, draw: render, markup: () => `<div class="tl-industry tree-view" id="tl-layers"${D.tlMode === "layers" ? "" : " hidden"}>
      <div class="tree-bar"><div class="tree-tabs" role="tablist" aria-label="持仓"></div>
        <span class="tree-tools"><button class="btn" data-tree-out aria-label="缩小">−</button><button class="btn" data-tree-in aria-label="放大">+</button>
        <button class="btn" data-tree-flat aria-pressed="${flat}">平面图</button>
        <button class="btn${analysisOpen ? " on" : ""}" data-tree-analyze>分析</button></span></div>
      <div class="tree-host"><canvas aria-label="持仓事件的立体脉络"></canvas><p class="quiet tree-empty" hidden></p>
        <div class="tl-tip" hidden></div><aside class="tree-card" hidden></aside>
        <aside class="tree-analysis${analysisOpen ? " open" : ""}"><div class="tree-analysis-head"><h3>分析</h3><button class="tree-x" data-tree-analysis-close aria-label="关闭">×</button></div>
          <div class="tree-presets"></div>
          <form class="ask" data-research-form><input name="question" placeholder="输入问题" aria-label="分析问题" value="${A.esc(D.question || "")}"><button type="submit">提问</button></form>
          <div id="research-content" aria-live="polite">${A.researchContent()}</div></aside></div></div>` };
})(App);
