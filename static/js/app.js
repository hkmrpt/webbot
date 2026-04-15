/* ── Socket.IO setup ─────────────────────────────────────────────────────── */
const socket = io({ transports: ["websocket"] });

/* ── App state ───────────────────────────────────────────────────────────── */
let ticks   = [];
let lines   = { entry: 0, sl: 0, tp: 0 };
let tfSecs  = 300;
let state   = {};

/* ── Canvas ──────────────────────────────────────────────────────────────── */
const canvas = document.getElementById("chart");
const ctx    = canvas.getContext("2d");

let candleW    = 10;
let panOffset  = 0;
let dragLine   = null;     // "entry"|"sl"|"tp"|null  — currently dragging
let hoverLine  = null;     // "entry"|"sl"|"tp"|null  — mouse is near this line
let isPanning  = false;
let panStartX  = 0;
let panStartOff = 0;
let mouseX     = -1;
let mouseY     = -1;

/* price ↔ y converters (updated every frame) */
let priceToY = () => 0;
let yToPrice = () => 0;

const HIT = 14;            /* px — grab area around a line */

const C = {
  up:    "#00e87a",
  down:  "#ff3d5c",
  wick:  "#3a4d6a",
  entry: "#4d9fff",
  sl:    "#ff3d5c",
  tp:    "#00e87a",
  now:   "rgba(200,218,240,0.45)",
  grid:  "rgba(28,36,56,0.8)",
  text:  "#4e6585",
  xhair: "rgba(200,218,240,0.18)",
  bg:    "#060810",
};

const LINE_META = {
  entry: { color: C.entry, label: "ENTRY", textColor: "#000" },
  sl:    { color: C.sl,    label: "SL",    textColor: "#fff" },
  tp:    { color: C.tp,    label: "TP",    textColor: "#000" },
};

/* ── Candle builder ──────────────────────────────────────────────────────── */
function buildCandles() {
  if (!ticks.length) return [];
  const out = [];
  let b = null;
  for (const t of ticks) {
    const tSec = Math.floor(t.ts / 1000 / tfSecs) * tfSecs;
    if (!b || b.t !== tSec) {
      if (b) out.push(b);
      b = { t: tSec, o: t.p, h: t.p, l: t.p, c: t.p };
    } else {
      if (t.p > b.h) b.h = t.p;
      if (t.p < b.l) b.l = t.p;
      b.c = t.p;
    }
  }
  if (b) out.push(b);
  return out;
}

/* ── RAF render loop ─────────────────────────────────────────────────────── */
function rafLoop() {
  draw();
  requestAnimationFrame(rafLoop);
}

/* ── Resize ──────────────────────────────────────────────────────────────── */
function resizeCanvas() {
  const wrap = document.getElementById("chart-wrap");
  canvas.width  = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
}

/* ── Main draw ───────────────────────────────────────────────────────────── */
function draw() {
  const W = canvas.width;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const candles = buildCandles();
  const pad = { top: 30, bot: 32, left: 4, right: 64 };
  const chartW = W - pad.left - pad.right;
  const chartH = H - pad.top  - pad.bot;
  const nCandles = Math.max(1, Math.floor(chartW / candleW));
  const startIdx = Math.max(0, candles.length - nCandles - panOffset);
  const endIdx   = Math.max(0, candles.length - panOffset);
  const visible  = candles.slice(startIdx, endIdx);

  /* price range */
  let allP = [];
  visible.forEach(c => allP.push(c.h, c.l));
  Object.values(lines).forEach(v => { if (v > 0) allP.push(v); });
  if (state.nifty_price) allP.push(state.nifty_price);

  if (!allP.length) {
    ctx.fillStyle = C.text;
    ctx.font = "13px monospace";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for NIFTY data…", W / 2, H / 2);
    return;
  }

  const minP  = Math.min(...allP);
  const maxP  = Math.max(...allP);
  const range = (maxP - minP) || 10;
  const mg    = range * 0.06;

  priceToY = (p) => pad.top + (1 - (p - (minP - mg)) / (range + mg * 2)) * chartH;
  yToPrice = (y) => (minP - mg) + (1 - (y - pad.top) / chartH) * (range + mg * 2);

  drawGrid(pad, W, H, minP, maxP, range, mg);
  drawTimeAxis(visible, pad, W, H);
  drawCandles(visible, pad);
  drawCurrentPrice(pad, W);
  drawLines(pad, W);
  drawTradeMarker(pad);
  drawCrosshair(pad, W, H);
  updateOHLC(visible);
}

/* ── Grid ────────────────────────────────────────────────────────────────── */
function drawGrid(pad, W, H, minP, maxP, range, mg) {
  const steps = 6;
  ctx.strokeStyle = C.grid;
  ctx.lineWidth   = 1;
  ctx.setLineDash([]);
  for (let i = 0; i <= steps; i++) {
    const p = (minP - mg) + (range + mg * 2) * (i / steps);
    const y = priceToY(p);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(W - pad.right, y);
    ctx.stroke();
    ctx.fillStyle  = C.text;
    ctx.font       = "10px monospace";
    ctx.textAlign  = "left";
    ctx.fillText(p.toFixed(0), W - pad.right + 4, y + 3);
  }
}

/* ── Time axis ───────────────────────────────────────────────────────────── */
function drawTimeAxis(visible, pad, W, H) {
  if (!visible.length) return;
  const step = Math.max(1, Math.floor(visible.length / 6));
  ctx.fillStyle = C.text;
  ctx.font      = "10px monospace";
  ctx.textAlign = "center";
  visible.forEach((c, i) => {
    if (i % step !== 0) return;
    const x = pad.left + (i + 0.5) * candleW;
    const d = new Date(c.t * 1000);
    ctx.fillText(
      `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`,
      x, H - pad.bot + 14
    );
  });
  ctx.strokeStyle = C.grid;
  ctx.lineWidth   = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, H - pad.bot);
  ctx.lineTo(W - pad.right, H - pad.bot);
  ctx.stroke();
}

/* ── Candles ─────────────────────────────────────────────────────────────── */
function drawCandles(visible, pad) {
  visible.forEach((c, i) => {
    const x    = pad.left + i * candleW;
    const midX = x + candleW / 2;
    const bull = c.c >= c.o;
    const col  = bull ? C.up : C.down;
    const yO   = priceToY(c.o);
    const yC   = priceToY(c.c);
    const yH   = priceToY(c.h);
    const yL   = priceToY(c.l);
    const bw   = Math.max(1, candleW - 2);
    const bodyT = Math.min(yO, yC);
    const bodyH = Math.max(1, Math.abs(yC - yO));

    ctx.strokeStyle = C.wick;
    ctx.lineWidth   = 1;
    ctx.beginPath();
    ctx.moveTo(midX, yH);
    ctx.lineTo(midX, yL);
    ctx.stroke();

    ctx.fillStyle = col;
    ctx.fillRect(x + 1, bodyT, bw, bodyH);
  });
}

/* ── Current NIFTY price ─────────────────────────────────────────────────── */
function drawCurrentPrice(pad, W) {
  if (!state.nifty_price) return;
  const y = priceToY(state.nifty_price);
  ctx.strokeStyle = C.now;
  ctx.lineWidth   = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(pad.left, y);
  ctx.lineTo(W - pad.right, y);
  ctx.stroke();
  ctx.setLineDash([]);

  const tagW = pad.right - 2;
  ctx.fillStyle = "rgba(200,218,240,0.12)";
  ctx.fillRect(W - pad.right + 1, y - 9, tagW, 18);
  ctx.strokeStyle = "rgba(200,218,240,0.3)";
  ctx.strokeRect(W - pad.right + 1, y - 9, tagW, 18);
  ctx.fillStyle  = "#c8daf0";
  ctx.font       = "bold 10px monospace";
  ctx.textAlign  = "left";
  ctx.fillText(state.nifty_price.toFixed(2), W - pad.right + 3, y + 4);
}

/* ── Draggable Lines ─────────────────────────────────────────────────────── */
function drawLines(pad, W) {
  for (const key of ["sl", "tp", "entry"]) {   /* draw entry last so it's on top */
    const price = lines[key];
    if (!price) continue;
    const m        = LINE_META[key];
    const isDrag   = dragLine  === key;
    const isHover  = hoverLine === key;
    const active   = isDrag || isHover;
    const y        = priceToY(price);
    const lineW    = isDrag ? 2.5 : isHover ? 2 : 1.5;
    const alpha    = active ? 1.0 : 0.75;

    /* glow behind line when active */
    if (active) {
      ctx.save();
      ctx.shadowColor = m.color;
      ctx.shadowBlur  = isDrag ? 12 : 7;
      ctx.strokeStyle = m.color;
      ctx.lineWidth   = lineW;
      ctx.globalAlpha = 0.3;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.restore();
    }

    /* main dashed line */
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = m.color;
    ctx.lineWidth   = lineW;
    ctx.setLineDash(isDrag ? [10, 4] : [7, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(W - pad.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    /* grip handle — pill in centre-left of chart */
    const hx = pad.left + 80;
    drawGripHandle(hx, y, m.color, active, isDrag);

    /* price tag on right axis */
    const tagW = pad.right - 2;
    ctx.fillStyle = m.color;
    ctx.fillRect(W - pad.right + 1, y - 10, tagW, 20);
    /* tag border highlight when active */
    if (active) {
      ctx.strokeStyle = "#fff";
      ctx.lineWidth   = 0.5;
      ctx.strokeRect(W - pad.right + 1, y - 10, tagW, 20);
    }
    ctx.fillStyle  = m.textColor;
    ctx.font       = `bold ${isDrag ? 10 : 9}px monospace`;
    ctx.textAlign  = "left";
    ctx.fillText(`${m.label} ${price.toFixed(2)}`, W - pad.right + 3, y + 4);

    /* floating price badge near cursor while dragging */
    if (isDrag && mouseY > 0) {
      drawFloatingBadge(m.color, m.textColor, price, mouseX, mouseY, W, pad);
    }
  }
}

function drawGripHandle(cx, cy, color, active, isDrag) {
  const rOuter = isDrag ? 8 : active ? 7 : 5;
  const rInner = rOuter - 2.5;

  /* outer circle */
  ctx.save();
  if (active) {
    ctx.shadowColor = color;
    ctx.shadowBlur  = isDrag ? 16 : 8;
  }
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(cx, cy, rOuter, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  /* inner dark hole — gives it a ring/handle look */
  ctx.fillStyle = C.bg;
  ctx.beginPath();
  ctx.arc(cx, cy, rInner, 0, Math.PI * 2);
  ctx.fill();

  /* three grip dots */
  if (active) {
    ctx.fillStyle = color;
    for (let d = -2; d <= 2; d += 2) {
      ctx.beginPath();
      ctx.arc(cx, cy + d, 1.2, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

function drawFloatingBadge(bgColor, textColor, price, mx, my, W, pad) {
  const label   = price.toFixed(2);
  const badgeW  = 72;
  const badgeH  = 24;
  let bx = mx + 18;
  let by = my - badgeH / 2;
  /* keep inside canvas */
  if (bx + badgeW > W - pad.right - 4) bx = mx - badgeW - 14;
  if (by < 4) by = 4;

  /* shadow */
  ctx.save();
  ctx.shadowColor  = bgColor;
  ctx.shadowBlur   = 12;
  ctx.fillStyle    = bgColor;
  roundRect(ctx, bx, by, badgeW, badgeH, 5);
  ctx.fill();
  ctx.restore();

  ctx.fillStyle    = textColor;
  ctx.font         = "bold 12px monospace";
  ctx.textAlign    = "center";
  ctx.fillText(label, bx + badgeW / 2, by + 16);
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.arcTo(x + w, y, x + w, y + r, r);
  ctx.lineTo(x + w, y + h - r);
  ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
  ctx.lineTo(x + r, y + h);
  ctx.arcTo(x, y + h, x, y + h - r, r);
  ctx.lineTo(x, y + r);
  ctx.arcTo(x, y, x + r, y, r);
  ctx.closePath();
}

/* ── Trade entry marker ──────────────────────────────────────────────────── */
function drawTradeMarker(pad) {
  if (state.status !== "in_trade" || !state.trade_entry_nifty) return;
  const y  = priceToY(state.trade_entry_nifty);
  const cx = pad.left + 40;

  ctx.save();
  ctx.shadowColor = C.up;
  ctx.shadowBlur  = 10;
  ctx.fillStyle   = C.up;
  ctx.beginPath();
  ctx.moveTo(cx, y + 12);
  ctx.lineTo(cx - 8, y + 22);
  ctx.lineTo(cx + 8, y + 22);
  ctx.closePath();
  ctx.fill();
  ctx.restore();

  ctx.fillStyle = "#000";
  ctx.font      = "bold 7px monospace";
  ctx.textAlign = "center";
  ctx.fillText("IN", cx, y + 20);
}

/* ── Crosshair ───────────────────────────────────────────────────────────── */
function drawCrosshair(pad, W, H) {
  if (dragLine) return;          /* hide crosshair while dragging */
  if (mouseX < pad.left || mouseX > W - pad.right) return;
  if (mouseY < pad.top  || mouseY > H - pad.bot)   return;

  ctx.strokeStyle = C.xhair;
  ctx.lineWidth   = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(mouseX, pad.top);
  ctx.lineTo(mouseX, H - pad.bot);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(pad.left, mouseY);
  ctx.lineTo(W - pad.right, mouseY);
  ctx.stroke();
  ctx.setLineDash([]);

  /* price label */
  const p = yToPrice(mouseY).toFixed(2);
  ctx.fillStyle = "rgba(15,18,32,0.9)";
  ctx.fillRect(W - pad.right + 1, mouseY - 9, pad.right - 2, 18);
  ctx.fillStyle  = C.text;
  ctx.font       = "10px monospace";
  ctx.textAlign  = "left";
  ctx.fillText(p, W - pad.right + 3, mouseY + 4);
}

function updateOHLC(visible) {
  if (!visible.length) return;
  const c = visible[visible.length - 1];
  document.getElementById("ohlc-o").textContent = c.o.toFixed(2);
  document.getElementById("ohlc-h").textContent = c.h.toFixed(2);
  document.getElementById("ohlc-l").textContent = c.l.toFixed(2);
  document.getElementById("ohlc-c").textContent = c.c.toFixed(2);
}

/* ── Hover detection (runs on mousemove) ─────────────────────────────────── */
function detectHover(my) {
  if (dragLine) return;
  let found = null;
  for (const key of ["entry", "sl", "tp"]) {
    if (lines[key] > 0 && Math.abs(my - priceToY(lines[key])) <= HIT) {
      found = key;
      break;
    }
  }
  if (found !== hoverLine) {
    hoverLine = found;
    canvas.style.cursor = found ? "ns-resize" : "crosshair";
  }
}

/* ── Mouse events ────────────────────────────────────────────────────────── */
canvas.addEventListener("mousedown", (e) => {
  e.preventDefault();
  const my = e.offsetY;

  for (const key of ["entry", "sl", "tp"]) {
    if (lines[key] > 0 && Math.abs(my - priceToY(lines[key])) <= HIT) {
      dragLine  = key;
      hoverLine = key;
      canvas.style.cursor = "ns-resize";
      return;
    }
  }

  isPanning   = true;
  panStartX   = e.offsetX;
  panStartOff = panOffset;
  canvas.style.cursor = "grab";
});

canvas.addEventListener("mousemove", (e) => {
  mouseX = e.offsetX;
  mouseY = e.offsetY;

  if (dragLine) {
    /* snap to 0.5-point grid for cleaner values */
    const raw = yToPrice(mouseY);
    lines[dragLine] = Math.round(raw * 2) / 2;
    updateLineInputs();
    updateOffsetLabels();
    return;
  }

  if (isPanning) {
    const dx  = e.offsetX - panStartX;
    panOffset = Math.max(0, panStartOff - Math.round(dx / candleW));
    return;
  }

  detectHover(mouseY);
});

canvas.addEventListener("mouseup", (e) => {
  if (dragLine) {
    socket.emit("update_lines", {
      entry_line: lines.entry || null,
      sl_line:    lines.sl    || null,
      tp_line:    lines.tp    || null,
    });
    dragLine = null;
    detectHover(e.offsetY);
  }
  isPanning = false;
  if (!hoverLine) canvas.style.cursor = "crosshair";
});

canvas.addEventListener("mouseleave", () => {
  mouseX = -1;
  mouseY = -1;
  if (dragLine) {
    socket.emit("update_lines", {
      entry_line: lines.entry || null,
      sl_line:    lines.sl    || null,
      tp_line:    lines.tp    || null,
    });
    dragLine = null;
  }
  hoverLine = null;
  isPanning = false;
  canvas.style.cursor = "crosshair";
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  candleW = Math.min(28, Math.max(4, candleW + (e.deltaY < 0 ? 1 : -1)));
}, { passive: false });

/* ── Input → lines ───────────────────────────────────────────────────────── */
function bindLineInput(id, key) {
  document.getElementById(id).addEventListener("change", (e) => {
    const v = parseFloat(e.target.value);
    if (!isNaN(v) && v > 0) {
      lines[key] = v;
      updateOffsetLabels();
      socket.emit("update_lines", {
        entry_line: lines.entry || null,
        sl_line:    lines.sl    || null,
        tp_line:    lines.tp    || null,
      });
    }
  });
}

bindLineInput("inp-entry", "entry");
bindLineInput("inp-sl",    "sl");
bindLineInput("inp-tp",    "tp");

function updateLineInputs() {
  if (lines.entry) document.getElementById("inp-entry").value = lines.entry.toFixed(2);
  if (lines.sl)    document.getElementById("inp-sl").value    = lines.sl.toFixed(2);
  if (lines.tp)    document.getElementById("inp-tp").value    = lines.tp.toFixed(2);
}

function updateOffsetLabels() {
  const e = lines.entry;
  if (!e) return;
  const fmt = (v) => (v >= 0 ? `+${v}` : `${v}`) + " pts";
  document.getElementById("sl-offset").textContent = lines.sl ? fmt((lines.sl - e).toFixed(0)) : "—";
  document.getElementById("tp-offset").textContent = lines.tp ? fmt((lines.tp - e).toFixed(0)) : "—";
}

/* ── Quick offset apply ──────────────────────────────────────────────────── */
function getInstrumentSide() {
  const sym = document.getElementById("inp-instrument").value.trim().toUpperCase().split("/")[0];
  if (sym.endsWith("PE")) return "PE";
  if (sym.endsWith("CE")) return "CE";
  return "";
}

document.getElementById("btn-apply-offsets").addEventListener("click", () => {
  const base = lines.entry || state.nifty_price || 0;
  if (!base) return;
  let slOff = parseFloat(document.getElementById("inp-sl-off").value) || 0;
  let tpOff = parseFloat(document.getElementById("inp-tp-off").value) || 0;
  const side = getInstrumentSide();
  if (side === "PE") {
    if (slOff < 0) slOff = -slOff;
    if (tpOff > 0) tpOff = -tpOff;
  } else if (side === "CE") {
    if (slOff > 0) slOff = -slOff;
    if (tpOff < 0) tpOff = -tpOff;
  }
  lines.sl = Math.round((base + slOff) * 2) / 2;
  lines.tp = Math.round((base + tpOff) * 2) / 2;
  updateLineInputs();
  updateOffsetLabels();
  socket.emit("update_lines", { sl_line: lines.sl, tp_line: lines.tp });
});

document.getElementById("btn-set-entry").addEventListener("click", () => {
  if (!state.nifty_price) return;
  lines.entry = Math.round(state.nifty_price * 2) / 2;
  updateLineInputs();
  updateOffsetLabels();
  socket.emit("update_lines", { entry_line: lines.entry });
});

/* ── Timeframe buttons ───────────────────────────────────────────────────── */
document.querySelectorAll(".tf-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    tfSecs    = parseInt(btn.dataset.tf);
    panOffset = 0;
  });
});

/* ── Mode toggle ─────────────────────────────────────────────────────────── */
document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const mode = btn.dataset.mode;
    if (state.status !== "idle") { alert("Cannot change mode while armed or in trade."); return; }
    if (mode === "real" && !confirm("Switch to REAL mode? Actual orders will be placed!")) return;
    socket.emit("set_mode", { mode });
  });
});

/* ── ARM / DISARM ────────────────────────────────────────────────────────── */
document.getElementById("btn-arm").addEventListener("click", () => {
  const instrument = document.getElementById("inp-instrument").value.trim().toUpperCase();
  const qty = parseInt(document.getElementById("inp-qty").value) || 1;
  if (!instrument) { alert("Enter instrument symbol"); return; }
  if (!lines.entry || !lines.sl || !lines.tp) { alert("Set all three lines (Entry, SL, TP) first"); return; }
  socket.emit("arm", { instrument, qty, entry_line: lines.entry, sl_line: lines.sl, tp_line: lines.tp });
});

document.getElementById("btn-disarm").addEventListener("click", () => socket.emit("disarm"));

document.getElementById("btn-manual-buy").addEventListener("click", () => {
  if (!confirm("Place MANUAL BUY now?")) return;
  socket.emit("manual_buy");
});

document.getElementById("btn-manual-exit").addEventListener("click", () => {
  if (!confirm("Exit trade manually?")) return;
  socket.emit("manual_exit");
});

/* ── Enctoken ────────────────────────────────────────────────────────────── */
document.getElementById("btn-save-enc").addEventListener("click", () => {
  const enc = document.getElementById("inp-enctoken").value.trim();
  if (enc) socket.emit("update_enctoken", { enctoken: enc });
});

/* ── Socket.IO events ────────────────────────────────────────────────────── */
socket.on("ticks", (data) => {
  ticks = data.map(([ts, p]) => ({ ts, p }));
});

socket.on("tick", ([ts, p]) => {
  ticks.push({ ts, p });
  if (ticks.length > 12000) ticks.shift();
});

socket.on("state", (data) => {
  state = data;
  applyState(data);
});

socket.on("log", appendLog);

socket.on("trade_opened", (d) => {
  appendLog({ ts: now(), msg: `Trade opened: ${d.instrument} @ NIFTY ${d.nifty_price}`, level: "trade" });
});

socket.on("trade_closed", (d) => {
  appendLog({ ts: d.exit_time, msg: `Trade closed (${d.reason}): P&L ${d.pnl_pts >= 0 ? "+" : ""}${d.pnl_pts} pts`, level: d.pnl_pts >= 0 ? "trade" : "warn" });
});

/* ── Apply state to UI ───────────────────────────────────────────────────── */
function applyState(s) {
  document.getElementById("nifty-price").textContent =
    s.nifty_price ? s.nifty_price.toFixed(2) : "—";
  document.getElementById("ws-dot").className =
    "ws-dot" + (s.ws_connected ? " on" : "");

  const badge = document.getElementById("status-badge");
  badge.className = `status-badge status-${s.status}`;
  badge.innerHTML = `<span class="dot"></span>${s.status.replace("_", " ").toUpperCase()}`;

  document.querySelectorAll(".mode-btn").forEach(b => {
    b.className = "mode-btn" + (b.dataset.mode === s.mode ? ` active-${s.mode}` : "");
  });

  if (!dragLine) {
    if (s.entry_line) { lines.entry = s.entry_line; document.getElementById("inp-entry").value = s.entry_line.toFixed(2); }
    if (s.sl_line)    { lines.sl    = s.sl_line;    document.getElementById("inp-sl").value    = s.sl_line.toFixed(2); }
    if (s.tp_line)    { lines.tp    = s.tp_line;    document.getElementById("inp-tp").value    = s.tp_line.toFixed(2); }
    updateOffsetLabels();
  }

  if (s.status === "idle") {
    if (s.instrument) document.getElementById("inp-instrument").value = s.instrument;
    document.getElementById("inp-qty").value = s.qty || 1;
  }

  const isArmed   = s.status === "armed";
  const isInTrade = s.status === "in_trade";
  document.getElementById("btn-arm").style.display    = isArmed || isInTrade ? "none" : "block";
  document.getElementById("btn-disarm").style.display = isArmed ? "block" : "none";
  document.getElementById("btn-manual-buy").disabled  = isInTrade || !s.instrument;
  document.getElementById("btn-manual-exit").disabled = !isInTrade;

  const tradeCard = document.getElementById("trade-card");
  if (isInTrade) {
    tradeCard.style.display = "block";
    document.getElementById("tc-instrument").textContent = s.instrument;
    document.getElementById("tc-side").textContent       = s.side;
    document.getElementById("tc-entry").textContent      = s.trade_entry_nifty ? s.trade_entry_nifty.toFixed(2) : "—";
    document.getElementById("tc-current").textContent    = s.nifty_price ? s.nifty_price.toFixed(2) : "—";
    const pnl = s.trade_entry_nifty && s.nifty_price
      ? (s.side === "CE" ? s.nifty_price - s.trade_entry_nifty : s.trade_entry_nifty - s.nifty_price) : 0;
    const pnlEl = document.getElementById("tc-pnl");
    pnlEl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + " pts";
    pnlEl.className   = "trade-val " + (pnl >= 0 ? "pos" : "neg");
    document.getElementById("tc-sl").textContent  = s.sl_line ? s.sl_line.toFixed(2) : "—";
    document.getElementById("tc-tp").textContent  = s.tp_line ? s.tp_line.toFixed(2) : "—";
    document.getElementById("tc-oid").textContent = s.order_id || "—";
  } else {
    tradeCard.style.display = "none";
  }

  const lt = s.last_trade;
  const ltCard = document.getElementById("last-trade-card");
  if (lt) {
    ltCard.style.display = "block";
    document.getElementById("lt-instrument").textContent = lt.instrument;
    document.getElementById("lt-reason").textContent     = lt.reason;
    document.getElementById("lt-entry").textContent      = lt.entry_nifty;
    document.getElementById("lt-exit").textContent       = lt.exit_nifty;
    const ltPnl = document.getElementById("lt-pnl");
    ltPnl.textContent = (lt.pnl_pts >= 0 ? "+" : "") + lt.pnl_pts + " pts";
    ltPnl.className   = "trade-val " + (lt.pnl_pts >= 0 ? "pos" : "neg");
  } else {
    ltCard.style.display = "none";
  }

  const pts = s.session_pnl_pts || 0;
  const pnlEl = document.getElementById("sess-pnl");
  pnlEl.textContent = (pts >= 0 ? "+" : "") + pts.toFixed(2);
  pnlEl.className   = "stat-val " + (pts > 0 ? "pos" : pts < 0 ? "neg" : "neutral");
  document.getElementById("sess-trades").textContent = s.trade_count || 0;
  document.getElementById("sess-wr").textContent =
    s.trade_count ? Math.round(s.wins / s.trade_count * 100) + "%" : "—";

  if (s.logs && s.logs.length) populateLogs(s.logs);
}

/* ── Log helpers ─────────────────────────────────────────────────────────── */
function now() { return new Date().toLocaleTimeString("en-IN", { hour12: false }); }

function appendLog(entry) {
  const panel = document.getElementById("log-panel");
  const row   = document.createElement("div");
  row.className = `log-row ${entry.level || "info"}`;
  row.innerHTML = `<span class="log-ts">${entry.ts}</span><span class="log-msg">${entry.msg}</span>`;
  panel.insertBefore(row, panel.firstChild);
  while (panel.children.length > 100) panel.removeChild(panel.lastChild);
}

function populateLogs(logs) {
  const panel = document.getElementById("log-panel");
  panel.innerHTML = "";
  (logs || []).forEach(appendLog);
}

/* ── Init ────────────────────────────────────────────────────────────────── */
window.addEventListener("resize", resizeCanvas);
resizeCanvas();
requestAnimationFrame(rafLoop);   /* start 60fps render loop */
