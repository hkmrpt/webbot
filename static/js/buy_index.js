// ── Canvas ────────────────────────────────────────────────────
const cv   = document.getElementById('cv');
const ctx  = cv.getContext('2d');
const wrap = document.getElementById('chartWrap');
const UP = '#00e87a', DN = '#ff3d5c';
const AX_W = 60, AX_H = 20;

let TF = 5, chartMode = 'nifty';
let niftyHist = [], optHist = [];
let candles = [], markers = [];
let lastP = null, candleW = 10, viewOff = 0;
let mX = -1, mY = -1, drag = false, dX = 0, dOff = 0;
let lvl = { ref: 0, entry: 0, sl: 0, trail: 0 };
let bestTrade = null, worstTrade = null;

// Config mirrors
let jumpPts = 7;
let cfgSlP1 = 15, cfgSlP2 = 8, cfgSlSecs = 30;
let cfgConfirmFast = 2, cfgConfirmMid = 3, cfgConfirmSlow = 4;
let cfgConfirmAtrHigh = 5, cfgConfirmAtrLow = 2;
let cfgTrailHigh = 35, cfgTrailLow = 15;
let cfgTrailAtrHigh = 5, cfgTrailAtrLow = 2;
let cfgCooldownSecs = 180;
let confirmTicks = 3;

let isRunning = false;
let currentMode = 'demo';
let lastSkipReason = null, lastSkipTs = 0;

// Local checkbox state
let tradeCE = false, tradePE = false;
let activeSidesFromServer = [];

// ── Resize ────────────────────────────────────────────────────
function resize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  if (!w || !h) return;
  cv.width = w; cv.height = h; draw();
}
new ResizeObserver(resize).observe(wrap);

// ── Side checkbox toggle ──────────────────────────────────────
function toggleSide(side) {
  if (isRunning) return;
  if (side === 'CE') tradeCE = !tradeCE;
  else               tradePE = !tradePE;
  renderSideUI();
}

function renderSideUI() {
  const ceChk     = document.getElementById('ceChk');
  const peChk     = document.getElementById('peChk');
  const ceRow     = document.getElementById('ceRow');
  const peRow     = document.getElementById('peRow');
  const bothBadge = document.getElementById('bothBadge');
  const topBar    = document.getElementById('topSidesBar');

  ceChk.className = tradeCE ? 'token-checkbox ce-checked' : 'token-checkbox';
  peChk.className = tradePE ? 'token-checkbox pe-checked' : 'token-checkbox';
  ceRow.className = 'token-row' + (tradeCE ? ' ce-active' : '');
  peRow.className = 'token-row' + (tradePE ? ' pe-active' : '');
  bothBadge.classList.toggle('show', tradeCE && tradePE);

  topBar.innerHTML = '';
  if (tradeCE && tradePE)      topBar.innerHTML = '<span class="side-pill both">CE + PE</span>';
  else if (tradeCE)            topBar.innerHTML = '<span class="side-pill ce">CE</span>';
  else if (tradePE)            topBar.innerHTML = '<span class="side-pill pe">PE</span>';
}

function syncServerSides(sides) {
  activeSidesFromServer = sides || [];
  const topBar = document.getElementById('topSidesBar');
  if (!isRunning) return;
  topBar.innerHTML = '';
  if (activeSidesFromServer.includes('CE') && activeSidesFromServer.includes('PE'))
    topBar.innerHTML = '<span class="side-pill both">CE + PE</span>';
  else if (activeSidesFromServer.includes('CE'))
    topBar.innerHTML = '<span class="side-pill ce">CE</span>';
  else if (activeSidesFromServer.includes('PE'))
    topBar.innerHTML = '<span class="side-pill pe">PE</span>';
}

// ── Mode ──────────────────────────────────────────────────────
function applyMode(mode) {
  currentMode = mode;
  const isReal = mode === 'real';
  document.getElementById('demoBtnToggle').classList.toggle('active', !isReal);
  document.getElementById('realBtnToggle').classList.toggle('active',  isReal);
  document.getElementById('modeWarning').classList.toggle('show', isReal);
  document.getElementById('modeInfo').textContent = isReal
    ? '⚠ Live orders sent to Zerodha' : 'Paper trading — no real orders';
  const banner = document.getElementById('modeBanner');
  banner.className = isReal ? 'real' : 'demo';
  document.getElementById('bannerIcon').textContent = isReal ? '🔴' : '🔵';
  document.getElementById('bannerText').textContent = isReal
    ? 'REAL TRADING — Live orders being placed on Zerodha!'
    : 'DEMO MODE — Paper trading active, no real orders';
  const pill = document.getElementById('topModePill');
  pill.className = 'mode-pill-top ' + mode;
  pill.textContent = isReal ? '🔴 REAL' : '🔵 DEMO';
  document.getElementById('orderIdRow').classList.toggle('show', isReal);
}

function setMode(mode) {
  if (mode === 'real') {
    const ok = confirm('⚠ Switch to REAL TRADING?\n\nLive orders will be placed on Zerodha.\nConfirm your enctoken is up to date!\n\nClick OK to confirm.');
    if (!ok) return;
  }
  socket.emit('set_trading_mode', { mode });
}

// ── Chart ─────────────────────────────────────────────────────
function setChart(mode) {
  chartMode = mode;
  document.getElementById('ctN').classList.toggle('active', mode === 'nifty');
  document.getElementById('ctO').classList.toggle('active', mode === 'option');
  document.getElementById('chartTitle').textContent = mode === 'nifty' ? 'NIFTY 50' : 'Option Price';
  rebuildAndDraw();
}
function setTF(tf, id) {
  TF = tf;
  document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  rebuildAndDraw();
}
function buildCandles() {
  const hist = chartMode === 'nifty' ? niftyHist : optHist;
  if (!hist.length) { candles = []; return; }
  const b = new Map();
  for (const { t, v } of hist) {
    const k = Math.floor(t / TF) * TF;
    if (!b.has(k)) b.set(k, []);
    b.get(k).push(v);
  }
  candles = [...b.entries()].sort((a, c) => a[0] - c[0])
    .map(([t, a]) => ({ t, o: a[0], h: Math.max(...a), l: Math.min(...a), c: a[a.length-1] }));
}
function rebuildAndDraw() {
  buildCandles();
  lastP = candles.length ? candles[candles.length-1].c : null;
  if (candles.length) {
    const c = candles[candles.length-1];
    set('oO', c.o.toFixed(2)); set('oH', c.h.toFixed(2));
    set('oL', c.l.toFixed(2)); set('oC', c.c.toFixed(2));
  }
  draw();
}
function addNiftyPrice(p) {
  if (!p || isNaN(p)) return;
  if (!cv.width) resize();
  niftyHist.push({ t: Date.now()/1000, v: p });
  if (niftyHist.length > 500000) niftyHist.shift();
  if (chartMode === 'nifty') rebuildAndDraw();
}
function addOptPrice(p) {
  if (!p || isNaN(p)) return;
  optHist.push({ t: Date.now()/1000, v: p });
  if (optHist.length > 500000) optHist.shift();
  if (chartMode === 'option') rebuildAndDraw();
}
function view() {
  const w = cv.width - AX_W;
  const vis   = Math.max(1, Math.floor(w / (candleW + 2)));
  const end   = Math.max(candles.length - viewOff, 0);
  const start = Math.max(end - vis, 0);
  return { start, end };
}
function priceRange(s, e) {
  const sl = candles.slice(s, e);
  if (!sl.length) return { mn: 0, mx: 1 };
  let mn = Infinity, mx = -Infinity;
  for (const c of sl) { mn = Math.min(mn, c.l); mx = Math.max(mx, c.h); }
  const rawRange = mx - mn || 1, thr = rawRange * 2;
  const levels = chartMode === 'nifty' ? [lvl.ref] : [lvl.entry, lvl.sl, lvl.trail];
  for (const v of levels) {
    if (v > 0 && Math.abs(v - (mn+mx)/2) < thr) { mn = Math.min(mn, v); mx = Math.max(mx, v); }
  }
  const pad = Math.max((mx - mn) * 0.08, 2);
  return { mn: mn - pad, mx: mx + pad };
}
function py(p, mn, mx) { return (cv.height - AX_H) * (1 - (p - mn) / (mx - mn)); }
function drawGrid(mn, mx) {
  const h = cv.height - AX_H, w = cv.width - AX_W;
  const range = mx - mn || 1, raw = range / 6;
  const mag  = Math.pow(10, Math.floor(Math.log10(raw)));
  const nice = Math.ceil(raw / mag) * mag;
  const sv   = Math.ceil(mn / nice) * nice;
  ctx.font = '9px IBM Plex Mono'; ctx.textAlign = 'right';
  for (let v = sv; v <= mx + nice; v += nice) {
    if (v < mn) continue;
    const y = py(v, mn, mx);
    ctx.strokeStyle = 'rgba(28,36,56,.9)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    ctx.fillStyle = 'rgba(78,101,133,.5)'; ctx.fillText(v.toFixed(1), cv.width - 3, y + 3);
  }
  const { start: vs } = view();
  const cw = candleW + 2, every = Math.max(1, Math.floor(60 / TF));
  ctx.textAlign = 'center';
  for (let i = vs; i < candles.length; i++) {
    if (!candles[i] || (i - vs) % every !== 0) continue;
    const x = (i - vs) * cw + candleW / 2;
    if (x > w) break;
    ctx.strokeStyle = 'rgba(28,36,56,.6)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    const d = new Date(candles[i].t * 1000);
    ctx.fillStyle = 'rgba(78,101,133,.38)';
    ctx.fillText(d.toLocaleTimeString('en', { hour:'2-digit', minute:'2-digit', hour12:false }), x, cv.height - 4);
  }
}
function drawCandle(c, x, mn, mx, isLast) {
  const up = c.c >= c.o, color = up ? UP : DN, cx2 = x + candleW / 2;
  const bTop = py(Math.max(c.o, c.c), mn, mx);
  const bBot = py(Math.min(c.o, c.c), mn, mx);
  const bH   = Math.max(bBot - bTop, 2);
  ctx.strokeStyle = color; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx2, py(c.h, mn, mx)); ctx.lineTo(cx2, bTop);
  ctx.moveTo(cx2, bBot);            ctx.lineTo(cx2, py(c.l, mn, mx));
  ctx.stroke();
  if (isLast) { ctx.shadowColor = color; ctx.shadowBlur = 8; }
  ctx.fillStyle = color; ctx.globalAlpha = 0.9;
  ctx.fillRect(x + 1, bTop, candleW - 2, bH);
  ctx.globalAlpha = 1; ctx.shadowBlur = 0;
}
function drawLevel(price, color, dash, mn, mx) {
  if (!price || price <= 0) return;
  const y = py(price, mn, mx), w = cv.width - AX_W;
  if (y < 0 || y > cv.height - AX_H) return;
  ctx.save();
  ctx.strokeStyle = color; ctx.lineWidth = 1.5;
  ctx.setLineDash(dash); ctx.globalAlpha = 0.75;
  ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  ctx.restore();
}
function drawMarkers(start, mn, mx) {
  const cw = candleW + 2;
  for (const m of markers) {
    const idx = candles.findIndex(c => c.t >= m.time);
    if (idx < 0 || idx < start) continue;
    const x = (idx - start) * cw + candleW / 2;
    const y = py(m.price, mn, mx);
    ctx.save();
    if (m.type === 'buy') {
      ctx.fillStyle   = m.side === 'CE' ? UP : DN;
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, y-9); ctx.lineTo(x-6, y+3); ctx.lineTo(x+6, y+3);
      ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.fillStyle = '#fff'; ctx.font = 'bold 6px IBM Plex Mono'; ctx.textAlign = 'center';
      ctx.fillText(m.side, x, y + 2);
    } else if (m.type === 'exit') {
      ctx.fillStyle   = m.pnl >= 0 ? UP : DN;
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, y-6); ctx.lineTo(x+6, y); ctx.lineTo(x, y+6); ctx.lineTo(x-6, y);
      ctx.closePath(); ctx.fill(); ctx.stroke();
    }
    ctx.restore();
  }
}
function drawCross(mn, mx) {
  if (mX < 0) return;
  const w = cv.width - AX_W, h = cv.height - AX_H;
  if (mX > w) return;
  ctx.save();
  ctx.strokeStyle = 'rgba(78,101,133,.3)'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
  ctx.beginPath();
  ctx.moveTo(mX, 0); ctx.lineTo(mX, h);
  ctx.moveTo(0, mY); ctx.lineTo(w, mY);
  ctx.stroke();
  const price = mn + (1 - mY/h) * (mx - mn);
  ctx.fillStyle = 'rgba(77,142,255,.9)';
  ctx.fillRect(w, mY-8, AX_W, 16);
  ctx.fillStyle = '#fff'; ctx.font = '9px IBM Plex Mono'; ctx.textAlign = 'center';
  ctx.fillText(price.toFixed(2), w + AX_W/2, mY + 3);
  ctx.restore();
  const { start } = view();
  const idx = start + Math.floor(mX / (candleW + 2));
  const tip = document.getElementById('xTip');
  if (candles[idx]) {
    const c = candles[idx], up = c.c >= c.o;
    tip.style.display = 'block';
    tip.innerHTML =
      `<span style="color:var(--text2);font-size:8px">${new Date(c.t*1000).toLocaleTimeString()}</span><br>` +
      `O <b>${c.o.toFixed(2)}</b> ` +
      `<span style="color:${UP}">H <b>${c.h.toFixed(2)}</b></span> ` +
      `<span style="color:${DN}">L <b>${c.l.toFixed(2)}</b></span> ` +
      `<span style="color:${up?UP:DN}">C <b>${c.c.toFixed(2)}</b></span>`;
    let tx = mX+12, ty = mY-44;
    if (tx + 240 > cv.width) tx = mX - 244;
    if (ty < 0) ty = mY + 8;
    tip.style.left = tx+'px'; tip.style.top = ty+'px';
  } else { tip.style.display = 'none'; }
}
function updateTags(mn, mx) {
  const niftyLvls  = [{ id:'tagRef',   price:lvl.ref   }];
  const optionLvls = [{ id:'tagEntry', price:lvl.entry }, { id:'tagSL', price:lvl.sl }, { id:'tagTrail', price:lvl.trail }];
  const active = chartMode === 'nifty' ? niftyLvls  : optionLvls;
  const hidden = chartMode === 'nifty' ? optionLvls : niftyLvls;
  hidden.forEach(({ id }) => { document.getElementById(id).style.display = 'none'; });
  active.forEach(({ id, price }) => {
    const el = document.getElementById(id);
    if (price > 0 && price >= mn && price <= mx) {
      el.style.display = 'block'; el.style.top = (py(price, mn, mx) - 9) + 'px';
    } else { el.style.display = 'none'; }
  });
  const pt = document.getElementById('priceTag');
  if (lastP) { pt.style.display = 'block'; pt.textContent = lastP.toFixed(2); pt.style.top = (py(lastP, mn, mx) - 9) + 'px'; }
  else { pt.style.display = 'none'; }
}
function drawEmpty() {
  ctx.fillStyle = '#050810'; ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = 'rgba(78,101,133,.15)'; ctx.font = '12px Space Grotesk'; ctx.textAlign = 'center';
  ctx.fillText('Check CE and/or PE boxes, enter tokens, then start', cv.width/2, cv.height/2-8);
  ctx.fillStyle = 'rgba(78,101,133,.08)'; ctx.font = '10px IBM Plex Mono';
  ctx.fillText('Scroll zoom · Drag pan · Toggle Nifty/Option', cv.width/2, cv.height/2+14);
}
function draw() {
  if (!cv.width || !cv.height) return;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#050810'; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!candles.length) { drawEmpty(); return; }
  const { start, end } = view();
  const { mn, mx }     = priceRange(start, end);
  drawGrid(mn, mx);
  if (chartMode === 'nifty') {
    drawLevel(lvl.ref,   'rgba(176,106,255,.8)', [8,4], mn, mx);
  } else {
    drawLevel(lvl.entry, 'rgba(0,212,255,.85)',  [8,4], mn, mx);
    drawLevel(lvl.sl,    'rgba(255,61,92,.85)',  [4,4], mn, mx);
    drawLevel(lvl.trail, 'rgba(255,176,32,.85)', [6,3], mn, mx);
  }
  const cw = candleW + 2;
  for (let i = start; i < end; i++) drawCandle(candles[i], (i-start)*cw, mn, mx, i===candles.length-1);
  drawMarkers(start, mn, mx);
  if (lastP) {
    const y = py(lastP, mn, mx), w = cv.width - AX_W;
    ctx.save();
    ctx.strokeStyle = 'rgba(0,212,255,.35)'; ctx.lineWidth = 1; ctx.setLineDash([2,4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    ctx.restore();
  }
  drawCross(mn, mx);
  updateTags(mn, mx);
}

// ── Mouse ─────────────────────────────────────────────────────
cv.addEventListener('mousemove', e => {
  const r = cv.getBoundingClientRect();
  const sx = cv.width/r.width, sy = cv.height/r.height;
  if (drag) viewOff = Math.max(0, dOff - Math.round((e.clientX-dX)/(candleW+2)));
  mX = (e.clientX-r.left)*sx; mY = (e.clientY-r.top)*sy; draw();
});
cv.addEventListener('mouseleave', () => { mX=-1; mY=-1; drag=false; document.getElementById('xTip').style.display='none'; draw(); });
cv.addEventListener('mousedown', e => { drag=true; dX=e.clientX; dOff=viewOff; cv.style.cursor='grabbing'; });
cv.addEventListener('mouseup',   () => { drag=false; cv.style.cursor='crosshair'; });
cv.addEventListener('wheel', e => {
  e.preventDefault();
  candleW = Math.max(4, Math.min(36, candleW - Math.sign(e.deltaY)*1.5)); draw();
}, { passive:false });

// ── Socket ────────────────────────────────────────────────────
const socket = io();
function set(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; }

// Server sends full state (including logs) on connect — no extra get_state needed
socket.on('connect', () => {});
socket.on('ws_status', d  => {
  document.getElementById('wsLed').classList.toggle('on', d.connected);
  document.getElementById('wsLbl').textContent = d.connected ? 'Live' : 'Disconnected';
});
socket.on('log', appendLog);

socket.on('daily_reset', d => {
  clearLogs();
  bestTrade = null; worstTrade = null;
  const plEl = document.getElementById('sessionPL');
  if (plEl) { plEl.textContent = '₹ --'; plEl.className = 'pnl-total zero'; }
  set('tradeCount', '0 trades · 0W / 0L');
  set('stTrades', '0'); set('stWin', '--');
  set('stBest', '--'); set('stWorst', '--');
  const tkSess = document.getElementById('tkSess');
  if (tkSess) { tkSess.textContent = '+₹0.00'; tkSess.style.color = 'var(--text2)'; }
  const tkPL = document.getElementById('tkPL');
  if (tkPL)   { tkPL.textContent = '--'; tkPL.style.color = 'var(--text2)'; }
  appendLog({
    ts: new Date().toLocaleTimeString('en', { hour12: false }),
    msg: `📅 New trading day ${d.date || ''} — session reset`,
    level: 'info',
  });
});

socket.on('error', d => {
  const msg = (d && d.msg) ? d.msg : String(d);
  appendLog({ ts: new Date().toLocaleTimeString('en',{hour12:false}), msg: '⚠ ' + msg, level: 'error' });
});

socket.on('state', d => {
  isRunning = d.running; updateBtn();
  if (d.trading_mode != null) applyMode(d.trading_mode);
  if (d.active_sides) syncServerSides(d.active_sides);

  // CE / PE live prices
  if (d.ce_price != null) {
    set('tkCE', '₹' + (+d.ce_price).toFixed(2));
    const el = document.getElementById('ceLive');
    el.textContent = '₹' + (+d.ce_price).toFixed(2);
    el.classList.add('has-price');
  }
  if (d.pe_price != null) {
    set('tkPE', '₹' + (+d.pe_price).toFixed(2));
    const el = document.getElementById('peLive');
    el.textContent = '₹' + (+d.pe_price).toFixed(2);
    el.classList.add('has-price');
  }
  if (d.ce_strike) set('ceStrike', d.ce_strike);
  if (d.pe_strike) set('peStrike', d.pe_strike);

  // Order IDs
  if (d.last_order_id) {
    set('oidBuy', '▲ BUY: ' + d.last_order_id);
    document.getElementById('oidBuy').style.color = 'var(--green)';
  }
  if (d.last_exit_order_id) {
    set('oidSell', '▼ SELL: ' + d.last_exit_order_id);
    document.getElementById('oidSell').style.color = 'var(--red)';
  }
  if (d.trading_mode === 'real' && d.last_order_id)
    set('bannerOid', '| Last: ' + d.last_order_id);
  else set('bannerOid', '');

  // ── v8.2: Auto-jump threshold display ────────────────────────
  const autoJumpRow = document.getElementById('autoJumpFilterRow');
  if (d.auto_jump_active != null) {
    if (d.auto_jump_active) {
      // Show dynamic ATR-derived threshold
      if (d.jump_threshold_pts != null) {
        const pts = (+d.jump_threshold_pts).toFixed(2);
        set('fvJump', pts + ' pts (ATR)');
        set('tkJumpThr', pts + 'p');
        document.getElementById('tkJumpThr').style.color = 'var(--cyan)';
        // Update spike meter scale
        jumpPts = +d.jump_threshold_pts;
        set('spikeL', `−${jumpPts.toFixed(1)} pts`);
        set('spikeR', `+${jumpPts.toFixed(1)} pts`);
      }
      if (d.jump_atr != null) {
        set('fvJumpAtr', 'ATR=' + (+d.jump_atr).toFixed(3));
        autoJumpRow.style.display = '';
      } else {
        autoJumpRow.style.display = 'none';
      }
    } else {
      // Fixed pct mode
      autoJumpRow.style.display = 'none';
      if (d.jump_pct != null) set('fvJump', d.jump_pct + '% of Nifty');
      if (d.jump_threshold_pts != null) {
        const pts = (+d.jump_threshold_pts).toFixed(2);
        set('tkJumpThr', pts + 'p');
        document.getElementById('tkJumpThr').style.color = 'var(--purple)';
        jumpPts = +d.jump_threshold_pts;
        set('spikeL', `−${jumpPts.toFixed(1)} pts`);
        set('spikeR', `+${jumpPts.toFixed(1)} pts`);
      }
    }
  } else {
    // Fallback: use dynamic_jump_pts which v8 always broadcasts
    if (d.dynamic_jump_pts != null) {
      jumpPts = +d.dynamic_jump_pts;
      set('spikeL', `−${jumpPts.toFixed(1)} pts`);
      set('spikeR', `+${jumpPts.toFixed(1)} pts`);
      set('tkJumpThr', jumpPts.toFixed(2) + 'p');
    }
    if (d.jump_pct != null) set('fvJump', d.jump_pct + '% of Nifty');
  }

  // SL config
  if (d.sl_pct_p1 != null) cfgSlP1 = d.sl_pct_p1;
  if (d.sl_pct_p2 != null) cfgSlP2 = d.sl_pct_p2;
  if (d.sl_phase1_secs != null) { cfgSlSecs = d.sl_phase1_secs; set('fvSLsecs', d.sl_phase1_secs + 's'); }
  set('fvSL', `${cfgSlP1}% → ${cfgSlP2}%`);

  // Confirm config
  if (d.confirm_ticks_fast != null) cfgConfirmFast    = d.confirm_ticks_fast;
  if (d.confirm_ticks_mid  != null) cfgConfirmMid     = d.confirm_ticks_mid;
  if (d.confirm_ticks_slow != null) cfgConfirmSlow    = d.confirm_ticks_slow;
  if (d.confirm_atr_high   != null) cfgConfirmAtrHigh = d.confirm_atr_high;
  if (d.confirm_atr_low    != null) cfgConfirmAtrLow  = d.confirm_atr_low;
  set('fvConfirm',    `${cfgConfirmFast} / ${cfgConfirmMid} / ${cfgConfirmSlow}`);
  set('fvConfirmAtr', `<${cfgConfirmAtrLow}→slow | >${cfgConfirmAtrHigh}→fast`);

  // Trail config
  if (d.trail_pct_high != null) cfgTrailHigh    = d.trail_pct_high;
  if (d.trail_pct_low  != null) cfgTrailLow     = d.trail_pct_low;
  if (d.trail_atr_high != null) cfgTrailAtrHigh = d.trail_atr_high;
  if (d.trail_atr_low  != null) cfgTrailAtrLow  = d.trail_atr_low;
  set('fvTrail',    `${cfgTrailLow}% → ${cfgTrailHigh}%`);
  set('fvTrailAtr', `<${cfgTrailAtrLow}→${cfgTrailLow}% | >${cfgTrailAtrHigh}→${cfgTrailHigh}%`);
  // profit_trail_threshold removed in v8.3

  // AI Brain state
  if (d.ai_regime != null) {
    const regime = d.ai_regime;
    set('tkRegime', regime === 'unknown' ? '--' : regime.replace('_',' ').toUpperCase().substring(0,4));
    const rb = document.getElementById('aiRegimeBadge');
    if (rb) {
      rb.textContent = regime.replace('_',' ');
      rb.className = 'ai-regime-badge ' + regime;
    }
  }
  if (d.ai_entry_score != null) {
    const sc = +d.ai_entry_score;
    set('tkAiScore', sc.toFixed(2));
    set('aiScoreVal', sc.toFixed(2));
    const fill = document.getElementById('aiScoreFill');
    if (fill) {
      fill.style.width = (sc * 100) + '%';
      fill.style.background = sc >= 0.72 ? 'var(--green)' : sc >= 0.57 ? 'var(--cyan)' : sc >= 0.42 ? 'var(--amber)' : 'var(--red)';
    }
    const tkAi = document.getElementById('tkAiScore');
    if (tkAi) tkAi.style.color = sc >= 0.72 ? 'var(--green)' : sc >= 0.57 ? 'var(--cyan)' : sc >= 0.42 ? 'var(--amber)' : 'var(--red)';
  }
  if (d.ai_brain != null) {
    const b = d.ai_brain;
    set('aiBrainTrades', b.n_trades || 0);
    set('aiBrainWR', b.win_rate != null ? (b.win_rate * 100).toFixed(1) + '%' : '--');
    const readyEl = document.getElementById('aiBrainReady');
    if (readyEl) {
      if (b.model_ready) {
        readyEl.innerHTML = '<span class="ai-ready-dot on"></span> Ready';
        readyEl.className = 'ai-v g';
      } else {
        const rem = Math.max(0, 30 - (b.n_trades || 0));
        readyEl.innerHTML = `<span class="ai-ready-dot off"></span> ${rem} more trades`;
        readyEl.className = 'ai-v y';
      }
    }
  }

  // Exit Brain
  if (d.exit_brain != null) {
    const eb = d.exit_brain;
    set('exitBrainStats', `${eb.n_ticks || 0} / ${eb.n_trades || 0}`);
    if (eb.exit_threshold != null) set('exitBrainThreshold', (+eb.exit_threshold).toFixed(2));
    const ebReady = document.getElementById('exitBrainReady');
    if (ebReady) {
      if (eb.policy_ready) {
        ebReady.innerHTML = '<span class="ai-ready-dot on"></span> Ready';
        ebReady.className = 'ai-v g';
      } else {
        const rem = Math.max(0, 30 - (eb.n_ticks || 0));
        ebReady.innerHTML = `<span class="ai-ready-dot off"></span> ${rem} ticks`;
        ebReady.className = 'ai-v y';
      }
    }
  }
  if (d.exit_score != null) {
    const sc = +d.exit_score;
    const fill = document.getElementById('exitScoreFill');
    if (fill) {
      fill.style.width = (sc * 100) + '%';
      fill.style.background = sc >= 0.55 ? 'var(--red)' : sc >= 0.35 ? 'var(--amber)' : 'var(--green)';
    }
    set('exitScoreLbl', sc.toFixed(2));
  }

  // Self-Tuner
  if (d.tuner != null) {
    const t = d.tuner;
    set('tunerTrades', t.n_trades || 0);
    const trReady = document.getElementById('tunerReady');
    if (trReady) {
      if (t.ready) {
        trReady.innerHTML = '<span class="ai-ready-dot on"></span> Active';
        trReady.className = 'ai-v g';
      } else {
        const rem = Math.max(0, 15 - (t.n_trades || 0));
        trReady.innerHTML = `<span class="ai-ready-dot off"></span> ${rem} more trades`;
        trReady.className = 'ai-v y';
      }
    }
    if (t.learned) {
      set('tunerConfirm', t.learned.confirm_ticks != null ? t.learned.confirm_ticks.toFixed(1) : '--');
      set('tunerJump',    t.learned.jump_floor    != null ? t.learned.jump_floor.toFixed(1) + ' pts' : '--');
      set('tunerSlope',   t.learned.slope_min     != null ? t.learned.slope_min.toFixed(2) : '--');
    }
  }

  // SL hit streak
  if (d.consecutive_sl_hits != null) {
    const hits = d.consecutive_sl_hits;
    const el = document.getElementById('fvSlStreak');
    if (el) {
      el.textContent = hits > 0 ? `${hits} hit${hits > 1 ? 's' : ''} (cooldown ×${Math.pow(2, hits - 1)})` : '0';
      el.className = hits >= 2 ? 'fval fv-warn' : hits === 1 ? 'fval fv-warn' : 'fval fv-ok';
    }
  }

  // Config values
  if (d.breakeven_trigger_pct != null) set('fvBreakeven', d.breakeven_trigger_pct + '%');
  if (d.nifty_reversal_exit != null) {
    const el = document.getElementById('fvNiftyReversal');
    if (el) { el.textContent = d.nifty_reversal_exit ? 'ON' : 'OFF'; el.className = d.nifty_reversal_exit ? 'fval fv-ok' : 'fval fv-warn'; }
  }
  // fast_move_velocity removed in v8.3 — move-type logic replaced by ExitBrain

  if (d.regression_window != null) set('fvReg', `${d.regression_window} ticks`);

  // Time gate (from config — shows actual configured values)
  if (d.trade_start != null && d.trade_end != null) {
    const forceStr = d.force_exit_time ? ` (force ${d.force_exit_time})` : '';
    set('fvTimeGate', `${d.trade_start}–${d.trade_end}${forceStr}`);
  }

  // Cooldown / limits
  if (d.sl_cooldown_secs != null) cfgCooldownSecs = d.sl_cooldown_secs;
  const cdRem = d.cooldown_remaining || 0;
  set('fvCooldown',  cdRem > 0 ? `${cfgCooldownSecs}s (${cdRem}s left)` : `${cfgCooldownSecs}s`);
  if (d.max_daily_loss != null) set('fvLossLim',   `₹${Number(d.max_daily_loss).toLocaleString('en-IN')}`);
  if (d.profit_target  != null) set('fvProfitTgt', `₹${Number(d.profit_target).toLocaleString('en-IN')}`);

  // Trail live
  if (d.trail_pct != null) {
    const tp = +d.trail_pct;
    set('trailLiveVal', tp.toFixed(1) + '%');
    set('tkTrailPct',   tp.toFixed(1) + '%');
    set('tcTrPct', `(${tp.toFixed(1)}%)`);
    const tv = document.getElementById('trailLiveVal');
    if (tv) tv.style.color = tp >= cfgTrailHigh*0.9 ? 'var(--red)' : tp <= cfgTrailLow*1.1 ? 'var(--green)' : 'var(--amber)';
  }

  // Option ATR
  if (d.option_atr != null) {
    const atr = +d.option_atr;
    set('optAtrVal', atr.toFixed(2)); set('tkOptAtr', atr.toFixed(2));
    const badge = document.getElementById('optAtrBadge');
    if (badge) {
      if (atr >= cfgTrailAtrHigh)     { badge.textContent='▲ HIGH'; badge.className='ir-badge atr-high'; }
      else if (atr <= cfgTrailAtrLow) { badge.textContent='▼ SLOW'; badge.className='ir-badge atr-slow'; }
      else                            { badge.textContent='● NRM';  badge.className='ir-badge atr-normal'; }
    }
  } else {
    set('optAtrVal','--'); set('tkOptAtr','--');
    const badge = document.getElementById('optAtrBadge');
    if (badge) { badge.textContent='--'; badge.className='ir-badge atr-none'; }
  }

  // Regression slope
  if (d.nifty_slope != null) {
    const sl  = +d.nifty_slope;
    const str = (sl >= 0 ? '+' : '') + sl.toFixed(3);
    const minSl = 0.3;
    set('slopeVal', str); set('tkSlope', str);
    const trend = document.getElementById('slopeTrend');
    if (trend) {
      trend.textContent = sl >= minSl ? '▲ UP' : sl <= -minSl ? '▼ DN' : '● FLAT';
      trend.className   = 'ir-badge ' + (sl >= minSl ? 'slope-up' : sl <= -minSl ? 'slope-dn' : 'slope-flat');
    }
    document.getElementById('tkSlope').className = 'ti-v ' + (sl >= minSl ? 'up' : sl <= -minSl ? 'dn' : '');
  }

  if (d.confirm_needed != null) { confirmTicks = d.confirm_needed; rebuildConfirmDots(confirmTicks); }

  if (d.ws_connected !== undefined) {
    document.getElementById('wsLed').classList.toggle('on', d.ws_connected);
    document.getElementById('wsLbl').textContent = d.ws_connected ? 'Live' : 'Disconnected';
  }

  // Chart feeds
  if (d.nifty_price != null) {
    const old = chartMode === 'nifty' ? lastP : null;
    const el  = document.getElementById('tkN');
    el.textContent = '₹' + d.nifty_price.toFixed(2);
    el.className   = 'ti-v ' + (old==null ? '' : d.nifty_price>old ? 'up' : 'dn');
    addNiftyPrice(d.nifty_price);
  }
  if (d.opt_price != null && d.trade_open) addOptPrice(d.opt_price);

  // Confirm ticker
  if (d.pending_side) {
    const cnt = d.confirm_count || 0, needed = d.confirm_needed || confirmTicks;
    set('tkConfirm', `${d.pending_side} ${cnt}/${needed}`);
    document.getElementById('tkConfirm').style.color = 'var(--cyan)';
  } else {
    set('tkConfirm', '--');
    document.getElementById('tkConfirm').style.color = 'var(--text2)';
  }

  updateSpikeMeter(
    d.nifty_move, d.trade_open, d.nifty_price != null,
    d.pending_side, d.confirm_count, d.confirm_needed,
    d.cooldown_remaining, d.active_sides || []
  );

  if (d.nifty_move != null) {
    const mv = d.nifty_move, el = document.getElementById('tkMove');
    el.textContent = (mv >= 0 ? '+' : '') + mv.toFixed(2) + ' pts';
    el.className   = 'ti-v ' + (mv >= jumpPts ? 'up' : mv <= -jumpPts ? 'dn' : '');
  }

  updateSkipReason(d.last_skip_reason);

  // P&L
  const sess = d.session_pnl || 0;
  const plEl = document.getElementById('sessionPL');
  plEl.textContent = (sess >= 0 ? '+' : '') + '₹' + sess.toFixed(2);
  plEl.className   = 'pnl-total ' + (sess > 0 ? 'pos' : sess < 0 ? 'neg' : 'zero');
  const tkPL   = document.getElementById('tkPL');
  const tkSess = document.getElementById('tkSess');
  if (d.live_pnl != null) {
    tkPL.textContent = (d.live_pnl >= 0 ? '+' : '') + '₹' + d.live_pnl.toFixed(2);
    tkPL.style.color  = d.live_pnl >= 0 ? 'var(--green)' : 'var(--red)';
  }
  tkSess.textContent = (sess >= 0 ? '+' : '') + '₹' + sess.toFixed(2);
  tkSess.style.color  = sess >= 0 ? 'var(--green)' : 'var(--red)';
  set('tradeCount', (d.trades_today||0) + ' trades · ' + (d.wins||0) + 'W / ' + (d.losses||0) + 'L');
  set('stTrades', d.trades_today || 0);
  const wr = d.trades_today ? Math.round(d.wins/d.trades_today*100)+'%' : '--';
  set('stWin', wr);
  if (d.capital) set('capVal', '₹' + d.capital.toLocaleString('en-IN'));

  document.getElementById('exitBtn').classList.toggle('show', !!d.trade_open);
  updateTradeCard(d);

  // Status bar
  const dot  = document.getElementById('sDot');
  const msg  = document.getElementById('sMsg');
  const fmsg = document.getElementById('sFilterMsg');
  const sidesStr = (d.active_sides || []).join('+') || '?';
  if (d.running) {
    if (d.trade_open) {
      dot.className   = 's-dot trade';
      msg.textContent = `${d.active_side} OPEN [${d.trading_mode==='real'?'🔴 REAL':'🔵 DEMO'}]`;
      fmsg.textContent = '';
    } else if (d.pending_side) {
      dot.className   = 's-dot conf';
      msg.textContent = `Confirming ${d.pending_side} spike — ${d.confirm_count}/${d.confirm_needed||confirmTicks}`;
      fmsg.textContent = '';
    } else {
      dot.className   = 's-dot scan';
      msg.textContent = `Scanning ${sidesStr} ±${jumpPts.toFixed(1)}pt spikes (${d.trades_today||0} trades)`;
      fmsg.textContent = d.last_skip_reason ? `Skip: ${d.last_skip_reason}` : '';
    }
  } else {
    dot.className = 's-dot'; msg.textContent = 'Robot stopped'; fmsg.textContent = '';
  }

  if (d.nifty_ref   != null) lvl.ref   = d.nifty_ref;
  if (d.entry       != null) lvl.entry = d.entry;
  if (d.sl          != null) lvl.sl    = d.sl;
  if (d.trail_price != null && d.phase2) lvl.trail = d.trail_price;

});

socket.on('trade_opened', d => {
  const t = Date.now() / 1000;
  markers.push({ type:'buy', side:d.side, price:d.entry, time:t });
  lvl.entry = d.entry; lvl.sl = d.sl; lvl.trail = 0;
  // v8.2: order_id patched in after real order placed
  if (d.order_id) {
    set('oidBuy', '▲ BUY: ' + d.order_id);
    document.getElementById('oidBuy').style.color = 'var(--green)';
    set('bannerOid', '| Last: ' + d.order_id);
  }
  draw();
});

socket.on('trade_closed', d => {
  const t = Date.now() / 1000;
  markers.push({ type:'exit', price:d.exit_price, pnl:d.pnl, time:t });
  lvl.entry = 0; lvl.sl = 0; lvl.trail = 0;
  if (bestTrade  === null || d.pnl > bestTrade)  bestTrade  = d.pnl;
  if (worstTrade === null || d.pnl < worstTrade) worstTrade = d.pnl;
  set('stBest',  bestTrade  !== null ? (bestTrade  >=0?'+':'')+'₹'+bestTrade.toFixed(2)  : '--');
  set('stWorst', worstTrade !== null ? (worstTrade >=0?'+':'')+'₹'+worstTrade.toFixed(2) : '--');
  // v8.2: exit_order_id patched in after real sell order placed
  if (d.exit_order_id) {
    set('oidSell', '▼ SELL: ' + d.exit_order_id);
    document.getElementById('oidSell').style.color = 'var(--red)';
  }
  document.getElementById('exitBtn').classList.remove('show');
  ['tcBreakevenRow','tcMomentumRow','tcExitScoreRow'].forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
  draw();
});

// ── Spike meter ───────────────────────────────────────────────
function updateSpikeMeter(move, tradeLocked, hasData, pendingSide, confirmCount, confirmNeeded, cooldownRem, activeSides) {
  const fill  = document.getElementById('spikeFill');
  const badge = document.getElementById('spikeBadge');
  const valEl = document.getElementById('spikeVal');
  const subEl = document.getElementById('spikeSub');
  const cWrap = document.getElementById('confirmWrap');

  if (tradeLocked) {
    fill.style.width='0%'; fill.style.left='50%'; fill.style.background='var(--amber)';
    badge.textContent='🔒 LOCKED'; badge.className='spk-badge lock';
    subEl.textContent='Trade open — detector locked';
    cWrap.classList.remove('active');
    if (move!=null) { valEl.textContent=(move>=0?'+':'')+move.toFixed(2)+' pts'; valEl.className='spike-val neu'; }
    return;
  }
  if (pendingSide) {
    const cnt = confirmCount||0, needed = confirmNeeded||confirmTicks;
    fill.style.width='100%'; fill.style.left='0%'; fill.style.background='rgba(0,212,255,.3)';
    badge.textContent=`⚡ ${pendingSide} CONFIRMING`; badge.className='spk-badge conf';
    subEl.textContent=`${needed-cnt} more tick(s) needed…`;
    valEl.textContent=(move!=null?(move>=0?'+':'')+move.toFixed(2):'0.00')+' pts';
    valEl.className='spike-val '+(pendingSide==='CE'?'up':'dn');
    cWrap.classList.add('active');
    updateConfirmDots(cnt, needed);
    return;
  }
  cWrap.classList.remove('active');
  if (!hasData || move==null) {
    badge.textContent='FLAT'; badge.className='spk-badge neu';
    valEl.textContent='0.00 pts'; valEl.className='spike-val neu';
    subEl.textContent='Waiting for ticks…'; return;
  }
  const side     = move > 0 ? 'CE' : 'PE';
  const wrongDir = !activeSides.includes(side);
  const pct      = Math.min(1, Math.max(-1, move/jumpPts));
  if (pct > 0) {
    fill.style.left='50%'; fill.style.width=(pct*50)+'%';
    fill.style.background=wrongDir?'rgba(176,106,255,.5)':`rgba(0,232,122,${0.3+pct*0.65})`;
    badge.textContent=wrongDir?'▲ UP (wrong dir)':'▲ UP';
    badge.className='spk-badge '+(wrongDir?'wrong':'up');
    valEl.className='spike-val '+(wrongDir?'neu':'up');
  } else if (pct < 0) {
    fill.style.left=(50+pct*50)+'%'; fill.style.width=(-pct*50)+'%';
    fill.style.background=wrongDir?'rgba(176,106,255,.5)':`rgba(255,61,92,${0.3+(-pct)*0.65})`;
    badge.textContent=wrongDir?'▼ DN (wrong dir)':'▼ DN';
    badge.className='spk-badge '+(wrongDir?'wrong':'dn');
    valEl.className='spike-val '+(wrongDir?'neu':'dn');
  } else {
    fill.style.width='0%'; fill.style.left='50%'; fill.style.background='var(--text3)';
    badge.textContent='FLAT'; badge.className='spk-badge neu'; valEl.className='spike-val neu';
  }
  valEl.textContent=(move>=0?'+':'')+move.toFixed(2)+' pts';
  if (Math.abs(pct)>=1&&!wrongDir) subEl.textContent='🚀 Spike! Running filters…';
  else if (wrongDir)               subEl.textContent=`${side} not selected — ignored`;
  else { const rem=(jumpPts-Math.abs(move)).toFixed(2); subEl.textContent=rem+' pts to trigger'; }
}

function rebuildConfirmDots(n) {
  const container = document.getElementById('confirmDots');
  container.innerHTML = '';
  for (let i = 0; i < n; i++) {
    const d = document.createElement('div');
    d.className = 'conf-dot'; d.id = `cd${i}`; container.appendChild(d);
  }
  set('confirmCount', `0 / ${n}`);
}
function updateConfirmDots(count, needed) {
  const n = needed || confirmTicks;
  document.getElementById('confirmFill').style.width = Math.min(100, Math.round(count/n*100))+'%';
  set('confirmCount', `${count} / ${n}`);
  for (let i = 0; i < n; i++) {
    const el = document.getElementById(`cd${i}`);
    if (el) el.className = 'conf-dot' + (i < count ? ' filled' : '');
  }
}
function updateSkipReason(reason) {
  const box = document.getElementById('skipBox');
  if (reason && reason !== lastSkipReason) {
    lastSkipReason = reason; lastSkipTs = Date.now();
    document.getElementById('skipReason').textContent = reason;
    box.classList.add('show');
    setTimeout(() => { if (Date.now()-lastSkipTs >= 3900) box.classList.remove('show'); }, 4000);
  } else if (!reason) { box.classList.remove('show'); lastSkipReason = null; }
}

function updateTradeCard(d) {
  const card  = document.getElementById('tradeCard');
  const side  = document.getElementById('tcSide');
  const badge = document.getElementById('tcBadge');

  if (!d.trade_open && !d.active_status) {
    side.textContent='No trade'; side.className='tc-side none';
    badge.textContent='Waiting'; badge.className='tc-badge wait';
    card.className='trade-card';
    ['tcEntry','tcCurrent','tcSL','tcTrail','tcPeak','tcPeakPct','tcPeakP','tcQty','tcHeld','tcLivePL','tcExitScore']
      .forEach(id => set(id,'--'));
    ['tcTrailRow','tcPeakRow','tcPeakPctRow','tcProfRow','tcExitScoreRow']
      .forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
    return;
  }
  const s = d.active_side;
  side.textContent = s ? `Long ${s} Option` : '--';
  side.className   = 'tc-side ' + (s ? s.toLowerCase() : 'none');
  card.className   = 'trade-card ' + (!d.trade_open ? 'closed' : s==='CE' ? 'ce-open' : 'pe-open');

  if (d.trade_open && d.fast_entry) { badge.textContent='⚡ FAST';      badge.className='tc-badge open'; }
  else if (d.trade_open && d.phase2){ badge.textContent='● TRAILING';   badge.className='tc-badge p2';   }
  else if (d.trade_open)            { badge.textContent='● OPEN';       badge.className='tc-badge open'; }
  else {
    const st = d.active_status || '';
    if (st.includes('sl'))          { badge.textContent='SL Hit';    badge.className='tc-badge loss'; }
    else if (st.includes('trail'))  { badge.textContent='Trail Exit'; badge.className='tc-badge profit'; }
    else if (st.includes('timeout')){ badge.textContent='Timeout';   badge.className='tc-badge loss'; }
    else if (st.includes('force'))  { badge.textContent='Force Exit'; badge.className='tc-badge loss'; }
    else if (st.includes('ai_exit'))     { badge.textContent='AI Exit';     badge.className='tc-badge profit'; }
    else if (st.includes('nifty_reversal')) { badge.textContent='Reversal';  badge.className='tc-badge loss'; }
    else                            { badge.textContent='Closed';    badge.className='tc-badge loss'; }
  }

  if (d.entry  != null) set('tcEntry', '₹' + d.entry.toFixed(2));
  if (d.sl     != null) set('tcSL',    '₹' + d.sl.toFixed(2));
  if (d.sl_pct != null) set('tcSlPct', `(${d.sl_pct}%)`);
  if (d.qty    != null) set('tcQty',   d.qty + ' lot' + (d.qty>1?'s':'') + ' (' + (d.qty*65) + ' qty)');

  if (d.held_secs != null) {
    const h = d.held_secs, hEl = document.getElementById('tcHeld');
    hEl.textContent = h + 's';
    hEl.className   = 'lv ' + (h > 45 ? 'y' : 'c');
  }
  if (d.opt_price != null) set('tcCurrent', '₹' + d.opt_price.toFixed(2));
  if (d.live_pnl  != null) {
    const v = d.live_pnl, el = document.getElementById('tcLivePL');
    el.textContent = (v>=0?'+':'')+'₹'+v.toFixed(2);
    el.className   = 'lv '+(v>=0?'g':'r');
  }

  if (d.phase2) {
    ['tcTrailRow','tcPeakRow','tcPeakPctRow','tcProfRow','tcExitScoreRow']
      .forEach(id => { const el=document.getElementById(id); if(el) el.style.display='flex'; });
    if (d.trail_price != null) { set('tcTrail', '₹'+d.trail_price.toFixed(2)); lvl.trail=d.trail_price; }
    if (d.peak_price  != null) set('tcPeak',   '₹'+d.peak_price.toFixed(2));
    if (d.peak_profit_pct != null) set('tcPeakPct', '+'+d.peak_profit_pct.toFixed(1)+'%');
    if (d.peak_profit != null) set('tcPeakP',  '₹'+d.peak_profit.toFixed(2));
    if (d.trail_pct   != null) set('tcTrPct',  `(${(+d.trail_pct).toFixed(1)}%)`);

    // Exit score
    if (d.exit_score != null) {
      const sc = +d.exit_score;
      const esEl = document.getElementById('tcExitScore');
      if (esEl) {
        esEl.textContent = sc.toFixed(3) + (sc >= 0.55 ? ' ← EXIT' : '');
        esEl.className = 'lv ' + (sc >= 0.55 ? 'r' : sc >= 0.35 ? 'y' : 'g');
      }
    }

    // Breakeven
    const beRow = document.getElementById('tcBreakevenRow');
    if (beRow) {
      beRow.style.display = 'flex';
      const beEl = document.getElementById('tcBreakeven');
      if (d.breakeven_moved) {
        beEl.textContent = '✓ Moved to entry'; beEl.className = 'lv g';
      } else {
        beEl.textContent = 'Not yet'; beEl.className = 'lv y';
      }
    }

    // Momentum
    const mmRow = document.getElementById('tcMomentumRow');
    if (mmRow && d.momentum_score != null) {
      mmRow.style.display = 'flex';
      const sc = +d.momentum_score;
      const mmEl = document.getElementById('tcMomentum');
      const mFill = document.getElementById('momentumFill');
      if (mmEl) { mmEl.textContent = sc.toFixed(2); mmEl.className = 'lv ' + (sc >= 0.65 ? 'g' : sc >= 0.35 ? 'y' : 'r'); }
      if (mFill) {
        mFill.style.width = (sc * 100) + '%';
        mFill.style.background = sc >= 0.65 ? 'var(--green)' : sc >= 0.35 ? 'var(--amber)' : 'var(--red)';
        set('momentumLbl', sc.toFixed(2));
      }
    }
  }
}

// ── Controls ──────────────────────────────────────────────────
function toggleRobot() {
  if (isRunning) {
    socket.emit('stop_robot');
    isRunning = false; updateBtn();
  } else {
    if (!tradeCE && !tradePE) { alert('Check at least one side (CE or PE) to trade'); return; }
    const ceVal = document.getElementById('ceInput').value.trim();
    const peVal = document.getElementById('peInput').value.trim();
    if (tradeCE && !ceVal) { alert('CE is checked — please enter CE Symbol/Token'); return; }
    if (tradePE && !peVal) { alert('PE is checked — please enter PE Symbol/Token'); return; }

    const sideStr = (tradeCE && tradePE) ? 'CE + PE (BOTH)' : tradeCE ? 'CE only' : 'PE only';
    if (currentMode === 'real') {
      const ok = confirm(
        `🔴 Starting in REAL TRADING mode!\n\nSides: ${sideStr}\n` +
        (tradeCE ? `CE: ${ceVal}\n` : '') +
        (tradePE ? `PE: ${peVal}\n` : '') +
        '\nLive BUY/SELL orders will be sent to Zerodha.\n\nConfirm to proceed.'
      );
      if (!ok) return;
    }

    // Reset local state
    markers=[]; lastP=null;
    lvl={ref:0,entry:0,sl:0,trail:0};
    bestTrade=null; worstTrade=null;
    niftyHist=[]; optHist=[]; candles=[];
    lastSkipReason=null;
    set('oidBuy','—'); set('oidSell','—');
    set('ceLive','--'); set('peLive','--');
    document.getElementById('ceLive').classList.remove('has-price');
    document.getElementById('peLive').classList.remove('has-price');
    ['tcEntry','tcCurrent','tcSL','tcTrail','tcPeak','tcPeakPct','tcPeakP','tcQty','tcHeld','tcLivePL','tcExitScore']
      .forEach(id => set(id,'--'));
    ['tcTrailRow','tcPeakRow','tcPeakPctRow','tcProfRow','tcExitScoreRow']
      .forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
    ['tcBreakevenRow','tcMomentumRow'].forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
    set('aiScoreVal','--'); set('aiBrainTrades','0'); set('aiBrainWR','--');
    const aiScoreFill = document.getElementById('aiScoreFill');
    if (aiScoreFill) aiScoreFill.style.width = '0%';
    const mFill = document.getElementById('momentumFill');
    if (mFill) mFill.style.width = '0%';
    document.getElementById('exitBtn').classList.remove('show');
    document.getElementById('confirmWrap').classList.remove('active');
    document.getElementById('skipBox').classList.remove('show');
    document.getElementById('autoJumpFilterRow').style.display = 'none';
    set('tkSlope','--'); set('slopeVal','--');
    set('tkOptAtr','--'); set('optAtrVal','--');
    set('tkTrailPct','--'); set('trailLiveVal','--');
    set('tkConfirm','--'); set('tkJumpThr','--');
    set('tkCE','--'); set('tkPE','--');

    socket.emit('start_robot', {
      ce_input: ceVal,
      pe_input: peVal,
      trade_ce: tradeCE,
      trade_pe: tradePE,
    });
    isRunning = true; updateBtn();
  }
}

function manualExit() {
  if (!confirm('Exit trade now at market price?')) return;
  socket.emit('manual_exit_trade');
  document.getElementById('exitBtn').classList.remove('show');
}

function updateBtn() {
  const b = document.getElementById('toggleBtn');
  if (isRunning) {
    b.className = 'btn btn-stop'; b.textContent = '⏹ Stop Robot';
  } else {
    b.className = 'btn btn-go'; b.textContent = '🎯 Start Buy Robot';
    renderSideUI();
  }
}

function appendLog(e) {
  const body = document.getElementById('logBody');
  const el   = document.createElement('div');
  el.className = `le ${e.level||''}`;
  el.innerHTML = `<span class="le-ts">${e.ts}</span><span class="le-msg">${e.msg}</span>`;
  body.insertBefore(el, body.firstChild);
  while (body.children.length > 200) body.removeChild(body.lastChild);
}
function clearLogs() { document.getElementById('logBody').innerHTML = ''; }

// Init
renderSideUI();
requestAnimationFrame(resize);
