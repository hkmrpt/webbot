/* panels.js — DOM update layer. Every setter is diff-aware: it caches the
 * last value written per element and skips the DOM write when unchanged,
 * so 1–3 Hz state broadcasts cost almost nothing between real changes. */

const els = new Map();
const last = new Map();

export function $(id) {
  let e = els.get(id);
  if (!e) { e = document.getElementById(id); els.set(id, e); }
  return e;
}

export function setText(id, val) {
  const v = val === null || val === undefined ? '—' : String(val);
  if (last.get('t:' + id) === v) return;
  last.set('t:' + id, v);
  const e = $(id);
  if (e) e.textContent = v;
}

export function setClass(id, cls, on) {
  const key = 'c:' + id + ':' + cls;
  if (last.get(key) === on) return;
  last.set(key, on);
  const e = $(id);
  if (e) e.classList.toggle(cls, on);
}

export function setPnlText(id, val, prefix = '₹') {
  const num = Number(val) || 0;
  setText(id, `${prefix}${num >= 0 ? '+' : ''}${num.toFixed(2)}`);
  setClass(id, 'pos', num > 0);
  setClass(id, 'neg', num < 0);
}

export function show(id, on) { setClass(id, 'hidden', !on); }

const fmt  = (v, d = 2) => (v === null || v === undefined) ? '—' : Number(v).toFixed(d);
const fmtI = v => (v === null || v === undefined) ? '—' : String(v);

/* ── Topbar ticker ─────────────────────────────────────────────────────── */
export function updateTicker(d) {
  setText('tiNiftyLbl', d.index_name || 'NIFTY');
  setText('tiNifty', fmt(d.nifty_price));
  setText('tiCe', fmt(d.ce_price));
  setText('tiPe', fmt(d.pe_price));
  const mv = d.nifty_move;
  setText('tiMove', mv === null || mv === undefined ? '—' : (mv >= 0 ? '+' : '') + fmt(mv));
  setClass('tiMove', 'pos', mv > 0); setClass('tiMove', 'neg', mv < 0);
  setText('tiThr', fmt(d.jump_threshold_pts, 1));
  const sl = d.nifty_slope;
  setText('tiSlope', sl === null || sl === undefined ? '—' : (sl >= 0 ? '+' : '') + fmt(sl, 3));
  setText('tiRsi', fmt(d.rsi, 0));
  setText('tiAi', d.ai_entry_score === null || d.ai_entry_score === undefined ? '—' : fmt(d.ai_entry_score, 2));
  setText('tiRegime', (d.ai_regime || '—').replace('trending_', '↗').replace('unknown', '—'));
  setPnlText('tiPnl', d.live_pnl);
  setPnlText('tiSession', d.session_pnl);
  setText('capVal', d.capital === null || d.capital === undefined ? '—'
    : '₹' + Number(d.capital).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
}

/* ── Banner + mode ─────────────────────────────────────────────────────── */
export function updateMode(d) {
  const real = d.trading_mode === 'real';
  setClass('modeBanner', 'real', real);
  setClass('modeBanner', 'demo', !real);
  setText('bannerText', real ? 'REAL MODE — live orders on Zerodha!'
                             : 'DEMO MODE — paper trading, no real orders');
  setText('bannerOid', d.last_order_id ? 'order: ' + d.last_order_id : '');
  setClass('modeDemo', 'active', !real);
  setClass('modeReal', 'active', real);
  setText('modeHint', real ? 'Live capital at risk — orders hit the exchange'
                           : 'Paper trading — fills simulated at tick price');
  show('adoptSec', real);
  show('tabPositions', real);
  show('tabOrders', real);
}

/* ── ATM panel ─────────────────────────────────────────────────────────── */
export function updateAtm(atm) {
  if (!atm) return;
  setText('atmExpiry', atm.expiry || '—');
  setText('atmStrike', atm.strike || '—');
  setText('atmCeSym', atm.ce_symbol || '—');
  setText('atmPeSym', atm.pe_symbol || '—');
}

/* ── Robot / scalp controls ────────────────────────────────────────────── */
export function updateControls(d) {
  const btn = $('btnRobot');
  const running = !!d.running;
  if (last.get('running') !== running) {
    last.set('running', running);
    btn.textContent = running ? '■ STOP ROBOT' : '▶ START ROBOT';
    btn.classList.toggle('running', running);
  }
  show('btnExit', !!d.trade_open);
  setClass('statusLed', 'on', running && !d.trade_open);
  setClass('statusLed', 'trade', !!d.trade_open);

  const mode = d.scalp_mode || 'off';
  document.querySelectorAll('#scalpSeg .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.scalp === mode));
  show('scalpBtns', mode === 'manual');
  setText('scalpHint',
    mode === 'auto'   ? 'SCALP AUTO — AI micro-move entries, tight SL/trail' :
    mode === 'manual' ? 'SCALP MANUAL — Buy CE/PE; exits on YOUR NIFTY levels (lines on chart)' :
                        'Tight SL/trail for capturing small moves');
}

/* ── Manual NIFTY-level editor ─────────────────────────────────────────── */
export function updateManualLevels(d) {
  const ml = d.manual_levels;
  const active = !!(ml && d.trade_open);
  show('manualLevels', active);
  if (!active) return;
  setText('mlSide', ml.side || '');
  // Don't clobber the input while the user is typing in it
  for (const [id, val] of [['mlSl', ml.sl], ['mlTp', ml.tp]]) {
    const e = $(id);
    if (e && document.activeElement !== e && Number(e.value) !== val) {
      e.value = val;
    }
  }
}

/* ── Spike detector ────────────────────────────────────────────────────── */
export function updateSpike(d) {
  const thr = d.jump_threshold_pts || 1;
  const mv  = d.nifty_move || 0;
  const pct = Math.max(-1, Math.min(1, mv / thr));
  const fill = $('gaugeFill');
  const key = 'gauge:' + pct.toFixed(3);
  if (last.get('gauge') !== key) {
    last.set('gauge', key);
    if (pct >= 0) {
      fill.style.left = '50%'; fill.style.width = (pct * 50) + '%';
      fill.style.background = 'var(--green)';
    } else {
      fill.style.width = (-pct * 50) + '%'; fill.style.left = (50 + pct * 50) + '%';
      fill.style.background = 'var(--red)';
    }
  }
  setText('gaugeHero', (mv >= 0 ? '+' : '') + fmt(mv));
  setText('gaugeL', '-' + fmt(thr, 1));
  setText('gaugeR', '+' + fmt(thr, 1));

  const badge = $('spikeBadge');
  const lbl = d.pending_side ? 'CONFIRM ' + d.pending_side
    : Math.abs(pct) >= 1 ? (mv > 0 ? 'LONG' : 'SHORT') : 'FLAT';
  if (last.get('spikeBadge') !== lbl) {
    last.set('spikeBadge', lbl);
    badge.textContent = lbl;
    badge.classList.toggle('long', lbl === 'LONG');
    badge.classList.toggle('short', lbl === 'SHORT');
    badge.classList.toggle('live', lbl.startsWith('CONFIRM'));
  }

  const need = d.confirm_needed || 0, got = d.pending_side ? (d.confirm_count || 0) : 0;
  const dotsKey = need + ':' + got;
  if (last.get('cdots') !== dotsKey) {
    last.set('cdots', dotsKey);
    const dots = $('confirmDots');
    dots.innerHTML = '';
    for (let i = 0; i < need; i++) {
      const s = document.createElement('span');
      s.className = 'dot' + (i < got ? ' on' : '');
      dots.appendChild(s);
    }
  }
  setText('confirmTxt', d.pending_side ? `${got}/${need} ${d.pending_side}` : `${need} ticks`);
  setText('skipBox', d.last_skip_reason || '—');
}

/* ── Session stats ─────────────────────────────────────────────────────── */
export function updateSession(d) {
  setPnlText('sessPnl', d.session_pnl);
  const w = d.wins || 0, l = d.losses || 0, n = d.trades_today || 0;
  setText('stTrades', `${n} (${w}W/${l}L)`);
  setText('stWinRate', (w + l) > 0 ? Math.round(w / (w + l) * 100) + '%' : '—');
  const b = d.trade_budget || {};
  setText('stBudget', b.remaining !== undefined ? `${b.remaining} left` : '—');
  setClass('stBudget', 'warn', (b.remaining || 99) <= 2);
  const cd = d.cooldown_remaining || 0;
  setText('stCooldown', cd > 0 ? cd + 's' : '—');
  setClass('stCooldown', 'warn', cd > 0);
}

/* ── AI card ───────────────────────────────────────────────────────────── */
const RING_LEN = 163.4;
export function updateAI(d) {
  const brain = d.ai_brain || {};
  const score = d.ai_entry_score;
  setText('aiScore', score === null || score === undefined ? '—' : fmt(score, 2));
  const ring = $('aiRing');
  const off = RING_LEN * (1 - Math.max(0, Math.min(1, score || 0)));
  if (last.get('ring') !== off) { last.set('ring', off); ring.style.strokeDashoffset = off; }
  setText('aiRegime', brain.last_regime || d.ai_regime || '—');
  setText('aiTrades', fmtI(brain.n_trades ?? 0));
  setText('aiWr', brain.n_trades ? Math.round((brain.win_rate || 0) * 100) + '%' : '—');
  setText('aiModeBadge', brain.mode || d.trading_mode || 'demo');

  // Warmup gates (v9.5): AI exits unlock at 10 trades, trained entry gate at 20
  const n = brain.n_trades || 0;
  const exitPct  = Math.min(1, n / 10), entryPct = Math.min(1, n / 20);
  const we = $('warmExit'), wn = $('warmEntry');
  if (last.get('we') !== exitPct)  { last.set('we', exitPct);
    we.style.width = exitPct * 100 + '%'; we.classList.toggle('full', exitPct >= 1); }
  if (last.get('wn') !== entryPct) { last.set('wn', entryPct);
    wn.style.width = entryPct * 100 + '%'; wn.classList.toggle('full', entryPct >= 1); }
  setText('warmExitTxt',  exitPct >= 1 ? 'LIVE' : `${n}/10`);
  setText('warmEntryTxt', entryPct >= 1 ? 'LIVE' : `${n}/20`);
}

/* ── Active filters (config mirrors) ───────────────────────────────────── */
let filtersBuilt = false;
const FILTERS = [
  ['Stop loss',        d => `${fmt(d.sl_pct_p1, 0)}%→${fmt(d.sl_pct_p2, 0)}% @${fmtI(d.sl_phase1_secs)}s`],
  ['Trail range',      d => `${fmt(d.trail_pct_low, 0)}–${fmt(d.trail_pct_high, 0)}%`],
  ['Trail ATR',        d => `${fmt(d.trail_atr_low, 1)}–${fmt(d.trail_atr_high, 1)}`],
  ['Profit trail from',d => fmt(d.profit_trail_threshold, 1) + '%'],
  ['Breakeven at',     d => fmt(d.breakeven_trigger_pct, 1) + '%'],
  ['Confirm ticks',    d => `${fmtI(d.confirm_ticks_fast)}/${fmtI(d.confirm_ticks_mid)}/${fmtI(d.confirm_ticks_slow)}`],
  ['Confirm ATR',      d => `${fmt(d.confirm_atr_low, 1)}–${fmt(d.confirm_atr_high, 1)}`],
  ['Regression win',   d => fmtI(d.regression_window)],
  ['Jump threshold',   d => fmt(d.jump_threshold_pts, 1) + ' pts'],
  ['Hours',            d => `${d.trade_start || '—'}–${d.trade_end || '—'}`],
  ['Force exit',       d => d.force_exit_time || '—'],
  ['Daily loss cap',   d => '₹' + fmtI(d.max_daily_loss)],
  ['NIFTY reversal',   d => d.nifty_reversal_exit ? 'on' : 'off'],
  ['SL cooldown',      d => fmtI(d.sl_cooldown_secs) + 's'],
];
export function updateFilters(d) {
  const body = $('filtersBody');
  if (!filtersBuilt) {
    filtersBuilt = true;
    body.innerHTML = FILTERS.map((f, i) =>
      `<div class="fv"><label>${f[0]}</label><b id="fv${i}">—</b></div>`).join('');
  }
  FILTERS.forEach((f, i) => setText('fv' + i, f[1](d)));
}

/* ── Trade card ────────────────────────────────────────────────────────── */
let tcBuilt = false;
const TC_ROWS = [
  ['Entry',       d => fmt(d.entry)],
  ['Current',     d => fmt(d.opt_price)],
  ['Stop loss',   d => `${fmt(d.sl)} (${fmt(d.sl_pct, 1)}%)`],
  ['Target',      d => d.target_price ? fmt(d.target_price) : '—'],
  ['Trail price', d => d.trail_price ? fmt(d.trail_price) : '—'],
  ['Trail %',     d => fmt(d.trail_pct, 1) + '%'],
  ['ATR trail',   d => fmt(d.atr_trail_pct, 1) + '%'],
  ['Profit trail',d => d.profit_trail_pct ? fmt(d.profit_trail_pct, 1) + '%' : '—'],
  ['Min trail ⚙', d => d.min_trail_pct_reached ? fmt(d.min_trail_pct_reached, 1) + '%' : '—'],
  ['Breakeven',   d => d.breakeven_moved ? 'moved ✓' : 'not yet'],
  ['Momentum',    d => d.momentum_score !== undefined && d.momentum_score !== null ? fmt(d.momentum_score, 2) : '—'],
  ['Move type',   d => d.move_type || '—'],
  ['Peak',        d => `${fmt(d.peak_price)} (${fmt(d.peak_profit_pct, 1)}%)`],
  ['Qty (lots)',  d => fmtI(d.qty)],
  ['Option ATR',  d => fmt(d.option_atr, 2)],
];
export function updateTradeCard(d) {
  if (!tcBuilt) {
    tcBuilt = true;
    $('tcRows').innerHTML = TC_ROWS.map((r, i) =>
      `<div class="row"><label>${r[0]}</label><b id="tc${i}">—</b></div>`).join('');
  }
  const open = !!d.trade_open;
  setText('tcSide', open ? `${d.active_side || ''} ${d.fast_entry ? '· FAST' : ''}` : 'NO TRADE');
  const badge = open ? (d.phase2 ? 'TRAILING' : 'OPEN')
    : (d.active_status && d.active_status.startsWith('closed_')
        ? d.active_status.replace('closed_', '').toUpperCase() : 'idle');
  setText('tcBadge', badge);
  setClass('tcBadge', 'live', open);

  if (open) {
    TC_ROWS.forEach((r, i) => setText('tc' + i, r[1](d)));
    const held = d.held_secs || 0;
    setText('tcHeld', held + 's');
    const bar = $('tcTimerBar');
    const pct = Math.min(100, held / 90 * 100);
    if (last.get('tbar') !== pct) { last.set('tbar', pct); bar.style.width = pct + '%'; }
  } else {
    setText('tcHeld', '—');
  }
  setPnlText('livePnl', open ? d.live_pnl : (d.trade_pnl || 0));
  setText('oidBuy',    d.last_order_id || '—');
  setText('oidTarget', d.target_order_id || '—');
  setText('oidSell',   d.last_exit_order_id || '—');
}

export function showPartials(partialPnl) {
  const e = $('livePartials');
  if (partialPnl) {
    e.textContent = `incl. partials ₹${Number(partialPnl) >= 0 ? '+' : ''}${Number(partialPnl).toFixed(2)}`;
    e.classList.remove('hidden');
  } else {
    e.classList.add('hidden');
  }
}

/* ── Status bar ────────────────────────────────────────────────────────── */
export function updateStatusBar(d) {
  const dot = $('sDot');
  const cls = d.trade_open ? 'trade' : d.pending_side ? 'conf' : d.running ? 'scan' : '';
  if (last.get('sdot') !== cls) {
    last.set('sdot', cls);
    dot.className = 'sdot' + (cls ? ' ' + cls : '');
  }
  setText('sMsg', d.trade_open ? `TRADE OPEN — ${d.active_side}`
    : d.pending_side ? `confirming ${d.pending_side} ${d.confirm_count}/${d.confirm_needed}`
    : d.running ? 'scanning for spikes…' : 'stopped');
  setText('sSkip', d.last_skip_reason || '');
}

/* ── Log ───────────────────────────────────────────────────────────────── */
const MAX_LOG = 200;
export function appendLog(entry) {
  const body = $('logBody');
  const div = document.createElement('div');
  div.className = 'log-line ' + (entry.level || 'info');
  div.innerHTML = `<span class="lt">${entry.ts || ''}</span><span>${escapeHtml(entry.msg || '')}</span>`;
  body.prepend(div);
  while (body.childElementCount > MAX_LOG) body.lastElementChild.remove();
}
export function clearLog() { $('logBody').innerHTML = ''; }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, m =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

/* ── Positions / Orders / History cards ────────────────────────────────── */
export function renderPositions(data) {
  const sum = data.summary || {};
  setText('posSummary', `${sum.open_positions ?? 0} open · P&L ₹${fmt(sum.total_pnl)}`);
  const wrap = $('posCards');
  wrap.innerHTML = (data.positions || []).map(p => `
    <div class="card">
      <div class="c-head"><b>${p.symbol || p.tradingsymbol || ''}</b>
        <b class="${(p.pnl || 0) >= 0 ? 'pos' : 'neg'}">₹${fmt(p.pnl)}</b></div>
      <div class="c-sub"><span>${(p.qty ?? p.quantity ?? 0) > 0 ? 'LONG' : 'SHORT'} ${Math.abs(p.qty ?? p.quantity ?? 0)}</span>
        <span>avg ${fmt(p.avg_price ?? p.average_price)}</span>
        <span>ltp ${fmt(p.last_price)}</span></div>
    </div>`).join('') || '<div class="hint">no open positions</div>';
}

export function renderOrders(orders, filter) {
  const match = o => {
    const st = (o.status || '').toUpperCase();
    if (filter === 'open')     return st.includes('OPEN') || st.includes('TRIGGER');
    if (filter === 'executed') return st.includes('COMPLETE');
    if (filter === 'rejected') return st.includes('REJECT') || st.includes('CANCEL');
    return true;
  };
  const cls = o => {
    const st = (o.status || '').toUpperCase();
    return st.includes('COMPLETE') ? 'executed' : st.includes('REJECT') ? 'rejected'
      : st.includes('OPEN') ? 'open' : '';
  };
  $('orderCards').innerHTML = (orders || []).filter(match).map(o => `
    <div class="card ${cls(o)}">
      <div class="c-head"><b>${o.transaction_type || ''} ${o.tradingsymbol || ''}</b>
        <span class="badge">${o.status || ''}</span></div>
      <div class="c-sub"><span>${o.filled_quantity ?? 0}/${o.quantity ?? 0}</span>
        <span>avg ${fmt(o.average_price)}</span>
        <span>${(o.order_timestamp || '').slice(11, 19)}</span></div>
    </div>`).join('') || '<div class="hint">no orders</div>';
}

export function renderHistory(trades, analytics) {
  const a = analytics || {};
  $('histStats').innerHTML = [
    ['WIN RATE', a.win_rate !== undefined ? Math.round(a.win_rate * 100) + '%' : '—'],
    ['PF', a.profit_factor ?? '—'],
    ['MAX DD', a.max_drawdown !== undefined ? '₹' + fmt(a.max_drawdown, 0) : '—'],
  ].map(s => `<div class="stat"><label>${s[0]}</label><b>${s[1]}</b></div>`).join('');
  // /api/trade_history already returns newest-first
  $('histCards').innerHTML = (trades || []).slice(0, 40).map(t => {
    const pnl = Number(t.pnl_total || t.pnl || 0);
    return `
    <div class="card">
      <div class="c-head"><b>${t.side || ''} ${t.option_symbol || ''}</b>
        <b class="${pnl >= 0 ? 'pos' : 'neg'}">₹${pnl.toFixed(2)}</b></div>
      <div class="c-sub"><span>${t.reason || ''}</span>
        <span>${fmt(t.entry)}→${fmt(t.exit_price)}</span>
        <span>${t.time || ''}</span></div>
    </div>`;
  }).join('') || '<div class="hint">no trades yet</div>';
}
