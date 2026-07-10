/* app.js — dashboard entry point: SocketIO wiring, chart, actions, tabs. */

import { BotChart, TF_LIST } from './chart.js';
import { IND_CATALOG } from './indicators.js';
import * as P from './panels.js';

const socket = io();
const chart = new BotChart(document.getElementById('chartMain'),
                           document.getElementById('chartOsc'));
chart.onOhlc = b => P.setText('ohlc',
  `O ${b.o.toFixed(2)}  H ${b.h.toFixed(2)}  L ${b.l.toFixed(2)}  C ${b.c.toFixed(2)}`);

let lastState = {};
let logInit = false;
let adoptEnabled = false;
let adoptTimer = null;

/* ── SocketIO handlers ─────────────────────────────────────────────────── */
socket.on('connect', () => {
  logInit = false;
  P.setText('sMsg', 'connected');
  chart.loadHistorical();
});

socket.on('disconnect', () => P.setText('sMsg', 'disconnected — reconnecting…'));

socket.on('state', d => {
  lastState = d;

  // chart feeds (client clock; server broadcasts are near-real-time)
  const now = Math.floor(Date.now() / 1000);
  if (d.nifty_price) chart.onTick('nifty', d.nifty_price, now);
  if (d.trade_open && d.opt_price) {
    const optVol = d.active_side === 'PE' ? d.pe_volume : d.ce_volume;
    chart.onTick('option', d.opt_price, now, optVol || 0);
  }

  const ml = d.manual_levels;
  chart.setLevels({
    ref:   d.nifty_ref || 0,
    entry: d.trade_open ? (d.entry || 0) : 0,
    sl:    d.trade_open ? (d.sl || 0) : 0,
    trail: d.trade_open && d.phase2 ? (d.trail_price || 0) : 0,
    tp:    d.trade_open ? (d.scalp_tp || d.target_price || 0) : 0,
    msl:   ml ? ml.sl : 0,      // manual NIFTY levels → NIFTY chart
    mtp:   ml ? ml.tp : 0,
  });
  chart.setPendingOrders(d.pending_orders);   // PND lines on watched charts
  P.updateManualLevels(d);
  P.updatePendingOrders(d);

  P.updateTicker(d);
  P.updateMode(d);
  P.updateAtm(d.nifty_atm);
  P.updateControls(d);
  P.updateSpike(d);
  P.updateSession(d);
  P.updateAI(d);
  P.updateFilters(d);
  P.updateTradeCard(d);
  P.updateStatusBar(d);
  if (d.ce_price) P.setText('atmCePx', Number(d.ce_price).toFixed(2));
  if (d.pe_price) P.setText('atmPePx', Number(d.pe_price).toFixed(2));

  if (!logInit && Array.isArray(d.logs)) {
    logInit = true;
    for (const entry of d.logs.slice(-100)) P.appendLog(entry);
  }
});

socket.on('log', e => P.appendLog(e));
socket.on('error', e => P.appendLog({ ts: '', msg: e.msg || String(e), level: 'error' }));

socket.on('daily_reset', d => {
  P.clearLog();
  P.appendLog({ ts: '', msg: `— new trading day ${d.date} —`, level: 'info' });
});

socket.on('nifty_atm_resolved', atm => P.updateAtm(atm));

socket.on('trade_opened', d => {
  chart.clearOption();
  chart.addMarker('buy', Date.now() / 1000, d.entry, `${d.side} @${d.entry}`);
  P.showPartials(0);
});

socket.on('trade_closed', d => {
  const pnl = d.pnl_total !== undefined ? d.pnl_total : d.pnl;
  chart.addMarker('sell', Date.now() / 1000, d.exit_price,
    `${pnl >= 0 ? '+' : ''}${Number(pnl).toFixed(0)}`);
  if (d.partial_realized_pnl) P.showPartials(d.partial_realized_pnl);
  loadHistory();          // refresh History tab data after each close
});

socket.on('adopt_status', d => {
  if (!adoptEnabled) return;
  P.setText('adoptStatus', d.adopted
    ? `managing ${d.symbol} (${d.side}) ${d.pnl_pct >= 0 ? '+' : ''}${d.pnl_pct}% · ${d.positions} open`
    : `${d.positions ?? 0} open positions — none adopted${d.error ? ' · ' + d.error : ''}`);
});

/* ── Header / left-panel actions ───────────────────────────────────────── */
document.getElementById('modeSeg').addEventListener('click', e => {
  const btn = e.target.closest('.seg-btn'); if (!btn) return;
  const mode = btn.dataset.mode;
  if (mode === lastState.trading_mode) return;
  if (mode === 'real' && !confirm('Switch to REAL trading? Live orders will be placed on Zerodha.')) return;
  socket.emit('set_trading_mode', { mode });
});

document.getElementById('btnRobot').addEventListener('click', () => {
  if (lastState.running) {
    socket.emit('stop_robot');
  } else {
    if (lastState.trading_mode === 'real'
        && !confirm('Start robot in REAL mode? It will place live orders.')) return;
    socket.emit('start_robot', { index: 'NIFTY' });
  }
});

document.getElementById('btnExit').addEventListener('click', () => {
  if (confirm('Exit the open trade now?')) socket.emit('manual_exit_trade');
});

document.getElementById('btnRefreshAtm').addEventListener('click',
  () => socket.emit('refresh_nifty_atm'));

document.getElementById('scalpSeg').addEventListener('click', e => {
  const btn = e.target.closest('.seg-btn'); if (!btn) return;
  socket.emit('toggle_scalp_mode', { mode: btn.dataset.scalp });
});

const lotsOf = id => Math.max(1, parseInt(document.getElementById(id).value, 10) || 1);

for (const side of ['CE', 'PE']) {
  document.getElementById('btnScalp' + side).addEventListener('click', () => {
    const qty = lotsOf('scalpQty');
    if (confirm(`Scalp BUY ${side} × ${qty} lot now? AI manages the exit.`))
      socket.emit('scalp_manual_buy', { side, qty });
  });
}

document.getElementById('mlApply').addEventListener('click', () => {
  socket.emit('set_manual_levels', {
    sl: Number(document.getElementById('mlSl').value),
    tp: Number(document.getElementById('mlTp').value),
  });
});

// Drag a level line on the chart → send the new level on release.
// The server validates and echoes back via state; an invalid drop snaps back.
chart.onLevelDragEnd = (key, price) => {
  if (price == null) return;
  if (key.startsWith('pnd:')) {
    socket.emit('update_pending_order', { id: Number(key.slice(4)), level: price });
  } else {
    socket.emit('set_manual_levels',
      key === 'msl' ? { sl: price } : { tp: price });
  }
};

// ── Pending order controls ─────────────────────────────────────────────
document.getElementById('poArm').addEventListener('click', () => {
  const level = Number(document.getElementById('poLevel').value);
  if (!level) { P.appendLog({ ts: '', msg: 'Enter a trigger level first', level: 'warning' }); return; }
  socket.emit('place_pending_order', {
    side:  document.getElementById('poSide').value,
    watch: document.getElementById('poWatch').value,
    level,
    qty:   lotsOf('poQty'),
  });
  document.getElementById('poLevel').value = '';
});
// per-order ✕ buttons + cancel-all (event delegation on the list)
document.getElementById('poList').addEventListener('click', e => {
  const x = e.target.closest('.po-x');
  if (x) socket.emit('cancel_pending_order', { id: Number(x.dataset.id) });
});
document.getElementById('poCancelAll').addEventListener('click',
  () => socket.emit('cancel_pending_order', {}));

document.getElementById('adoptToggle').addEventListener('change', e => {
  adoptEnabled = e.target.checked;
  if (adoptEnabled) {
    socket.emit('adopt_positions', { enabled: true });
    P.setText('adoptStatus', 'scanning…');
    adoptTimer = setInterval(() =>
      socket.emit('adopt_positions', { enabled: true, poll: true }), 10000);
  } else {
    clearInterval(adoptTimer);
    socket.emit('adopt_positions', { enabled: false });
    P.setText('adoptStatus', 'off');
  }
});

document.getElementById('filtersHead').addEventListener('click', () => {
  const body = document.getElementById('filtersBody');
  const open = body.classList.toggle('hidden');
  P.setText('filtersCaret', open ? '▸' : '▾');
});

/* ── Chart controls ────────────────────────────────────────────────────── */
const tfWrap = document.getElementById('tfPills');
for (const tf of TF_LIST) {
  const b = document.createElement('button');
  b.className = 'pill' + (tf.secs === 60 ? ' active' : '');
  b.textContent = tf.label;
  b.onclick = () => {
    tfWrap.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
    b.classList.add('active');
    chart.setTF(tf.secs);
  };
  tfWrap.appendChild(b);
}

document.getElementById('srcSeg').addEventListener('click', e => {
  const btn = e.target.closest('.seg-btn'); if (!btn) return;
  document.querySelectorAll('#srcSeg .seg-btn').forEach(b =>
    b.classList.toggle('active', b === btn));
  chart.setSource(btn.dataset.src);
});

document.getElementById('chartType').addEventListener('change',
  e => chart.setType(e.target.value));

/* indicator dropdown — toggle + per-indicator param editor */
const indMenu = document.getElementById('indMenu');
const CATS = [['trend', 'Trend'], ['osc', 'Oscillators'],
              ['vol', 'Volatility'], ['volume', 'Volume']];

// Edited params persist across toggle off/on (and localStorage across reloads)
const paramStore = JSON.parse(localStorage.getItem('indParams') || '{}');
const saveParams = () => localStorage.setItem('indParams', JSON.stringify(paramStore));
const paramsFor = ind => ({ ...ind.params, ...(paramStore[ind.id] || {}) });

function buildParamEditor(ind, host) {
  host.innerHTML = '';
  const current = paramsFor(ind);
  for (const [key, defVal] of Object.entries(ind.params)) {
    const row = document.createElement('label');
    row.className = 'pp-row';
    const isColor = key === 'color';
    row.innerHTML = `<span>${key}</span>`;
    const input = document.createElement('input');
    if (isColor) {
      input.type = 'color';
      input.value = current[key] || defVal;
    } else {
      input.type = 'number';
      input.step = Math.abs(defVal) < 1 ? '0.01' : '1';
      input.min = '0';
      input.value = current[key];
    }
    input.addEventListener('change', () => {
      const v = isColor ? input.value : Number(input.value);
      if (!isColor && (!isFinite(v) || v <= 0)) { input.value = current[key]; return; }
      paramStore[ind.id] = { ...(paramStore[ind.id] || {}), [key]: v };
      saveParams();
      chart.setIndicatorParams(ind.id, { [key]: v });   // live if active
    });
    input.addEventListener('click', e => e.stopPropagation());
    row.appendChild(input);
    host.appendChild(row);
  }
}

for (const [cat, label] of CATS) {
  const h = document.createElement('div');
  h.className = 'dd-cat'; h.textContent = label;
  indMenu.appendChild(h);
  for (const ind of IND_CATALOG.filter(i => i.cat === cat)) {
    const item = document.createElement('div');
    item.className = 'dd-item';
    item.innerHTML = `<span class="sw"></span><span class="dd-name">${ind.name}${ind.sub ? ' <small>(pane)</small>' : ''}</span>`;
    if (Object.keys(ind.params).length) {
      const gear = document.createElement('span');
      gear.className = 'dd-gear';
      gear.textContent = '⚙';
      gear.title = 'Edit parameters';
      gear.addEventListener('click', e => {
        e.stopPropagation();
        const open = !editor.classList.contains('hidden');
        indMenu.querySelectorAll('.dd-params').forEach(p => p.classList.add('hidden'));
        if (!open) { buildParamEditor(ind, editor); editor.classList.remove('hidden'); }
      });
      item.appendChild(gear);
    }
    item.onclick = () => {
      if (ind.sub) {
        const on = chart.setOscillator(
          chart.oscillator && chart.oscillator.id === ind.id ? null : ind.id,
          paramsFor(ind));
        indMenu.querySelectorAll('.dd-item.osc-on').forEach(x =>
          x.classList.remove('on', 'osc-on'));
        if (on) item.classList.add('on', 'osc-on');
      } else {
        chart.toggleOverlay(ind.id, paramsFor(ind));
        item.classList.toggle('on', chart.overlays.has(ind.id));
      }
    };
    indMenu.appendChild(item);
    const editor = document.createElement('div');
    editor.className = 'dd-params hidden';
    indMenu.appendChild(editor);
  }
}
document.getElementById('indBtn').addEventListener('click', e => {
  e.stopPropagation();
  indMenu.classList.toggle('hidden');
});
document.addEventListener('click', e => {
  if (!e.target.closest('.dropdown')) indMenu.classList.add('hidden');
});

/* ── Right-panel tabs ──────────────────────────────────────────────────── */
document.querySelector('.rp-tabs').addEventListener('click', e => {
  const tab = e.target.closest('.rp-tab'); if (!tab) return;
  document.querySelectorAll('.rp-tab').forEach(t => t.classList.toggle('active', t === tab));
  document.querySelectorAll('.rp-panel').forEach(p =>
    p.classList.toggle('active', p.id === 'rp-' + tab.dataset.tab));
  if (tab.dataset.tab === 'positions' || tab.dataset.tab === 'orders') loadOrderBook();
  if (tab.dataset.tab === 'history') loadHistory();
});

let orderCache = { orders: [] };
let orderFilter = 'all';

async function loadOrderBook() {
  try {
    const r = await fetch('/api/order_book');
    orderCache = await r.json();
    P.renderPositions(orderCache);
    P.renderOrders(orderCache.orders, orderFilter);
  } catch (e) {
    P.setText('posSummary', 'order book unavailable');
  }
}

document.getElementById('btnPosRefresh').addEventListener('click', loadOrderBook);
document.getElementById('orderFilters').addEventListener('click', e => {
  const pill = e.target.closest('.pill'); if (!pill) return;
  orderFilter = pill.dataset.f;
  document.querySelectorAll('#orderFilters .pill').forEach(p =>
    p.classList.toggle('active', p === pill));
  P.renderOrders(orderCache.orders, orderFilter);
});

async function loadHistory() {
  try {
    const [tr, an] = await Promise.all([
      fetch('/api/trade_history').then(r => r.json()).catch(() => []),
      fetch('/api/analytics').then(r => r.json()).catch(() => ({})),
    ]);
    P.renderHistory(Array.isArray(tr) ? tr : (tr.trades || []), an);
  } catch (e) { /* endpoint unavailable — leave placeholder */ }
}

document.getElementById('btnClearLog').addEventListener('click', P.clearLog);

/* initial history load (populates the tab before first open) */
loadHistory();

/* debug/test handle (used by the headless UI probe) */
window.__bot = { chart, socket, get state() { return lastState; } };
