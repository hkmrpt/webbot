// ── Canvas ────────────────────────────────────────────────────
const cv   = document.getElementById('cv');
const ctx  = cv.getContext('2d');
const wrap = document.getElementById('chartWrap');
const UP = '#00e87a', DN = '#ff3d5c';
const AX_W = 60, AX_H = 20;

let TF = 5, chartMode = 'nifty';
let chartType = 'candle';  // 'candle' | 'ha' | 'line' | 'area'
let _mainH = 0;            // main chart height, set at top of draw()
let niftyHist = [], optHist = [];
let stockSpotHist = [], stockCeHist = [], stockPeHist = [];
// Pre-built OHLC candles from Zerodha historical API (1-min resolution)
let stockSpotCandles = [], stockCeCandles = [], stockPeCandles = [];
let _activeStockSym = null;   // which stock is currently shown in the chart
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

let isRunning = false, logInit = false;
let currentMode  = 'demo';
let _lastCapital = 100000;
let lastSkipReason = null, lastSkipTs = 0;

let activeSidesFromServer = [];

// ── Resize ────────────────────────────────────────────────────
function resize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  if (!w || !h) return;
  cv.width = w; cv.height = h; draw();
}
new ResizeObserver(resize).observe(wrap);

// ── Watchlist ─────────────────────────────────────────────────
let _watchlistStocks = [];
let _wlSearchTimer   = null;
let _selectedStocks  = new Set();   // symbols ticked for trading

async function loadWatchlist() {
  try {
    const data = await fetch('/api/watchlist').then(r => r.json());
    _watchlistStocks = Array.isArray(data) ? data : [];
    renderWatchlist();
  } catch(e) { /* silent */ }
}

function renderWatchlist() {
  const list = document.getElementById('wlList');
  const cnt  = document.getElementById('wlCount');
  if (!list) return;
  if (cnt) cnt.textContent = _watchlistStocks.length ? `(${_watchlistStocks.length})` : '';
  if (!_watchlistStocks.length) {
    list.innerHTML = '<div class="wl-empty">No stocks — search above to add</div>';
    return;
  }
  list.innerHTML = _watchlistStocks.map(s => {
    const exp  = (s.expiry || '').substring(5);   // "2025-06-26" → "06-26"
    const stk  = s.strike ? (+s.strike).toFixed(0) : '--';
    const sel  = _selectedStocks.has(s.symbol);
    return `<div class="wl-item${sel ? ' selected' : ''}" id="wl-${s.symbol}" data-symbol="${s.symbol}" data-score="0">
      <button class="wl-sel${sel ? ' on' : ''}" onclick="toggleStockSelect('${s.symbol}')" title="${sel ? 'Click to deselect (will not trade)' : 'Click to select for trading'}">
        ${sel ? '✓' : '○'}
      </button>
      <div class="wl-item-body" onclick="openStockDetail('${s.symbol}')" style="cursor:pointer">
        <div class="wl-dir flat" id="wl-dir-${s.symbol}">—</div>
        <div class="wl-item-info">
          <div class="wl-item-row1">
            <span class="wl-sym">${s.symbol}</span>
            <span class="wl-chg flat" id="wl-chg-${s.symbol}">+0.00%</span>
          </div>
          <div class="wl-item-row2">
            <span class="wl-spot" id="wl-spot-${s.symbol}">₹--</span>
            <span class="wl-sep">·</span>
            <span class="wl-strike-info">${stk} · ${exp}</span>
          </div>
          <div class="wl-item-row3">
            <span class="wl-opt-price ce" id="wl-ce-${s.symbol}">${s.ce_symbol ? 'CE --' : ''}</span>
            <span class="wl-opt-price pe" id="wl-pe-${s.symbol}">${s.pe_symbol ? 'PE --' : ''}</span>
            <span class="wl-score" id="wl-score-${s.symbol}" title="Composite score: ATR · Momentum · Range · Week position · Premium">◆ --</span>
            <span class="wl-pending" id="wl-pend-${s.symbol}" style="display:none"></span>
          </div>
          <div class="wl-item-row4">
            <span class="wl-spike-thr" id="wl-spike-${s.symbol}" title="Spike threshold = 7-day avg daily range × 18%">⚡ --</span>
          </div>
        </div>
      </div>
      <button class="wl-del" onclick="removeWatchlistStock('${s.symbol}')" title="Remove">✕</button>
    </div>`;
  }).join('');
  _updateWlSelCount();
}

function toggleStockSelect(symbol) {
  if (_selectedStocks.has(symbol)) {
    _selectedStocks.delete(symbol);
  } else {
    _selectedStocks.add(symbol);
  }
  const item = document.getElementById('wl-' + symbol);
  const btn  = item ? item.querySelector('.wl-sel') : null;
  const sel  = _selectedStocks.has(symbol);
  if (item) item.classList.toggle('selected', sel);
  if (btn)  { btn.classList.toggle('on', sel); btn.textContent = sel ? '✓' : '○'; btn.title = sel ? 'Click to deselect (will not trade)' : 'Click to select for trading'; }
  _updateWlSelCount();
  _sortWatchlistByScore();
}

function _updateWlSelCount() {
  const cnt = document.getElementById('wlCount');
  const total = _watchlistStocks.length;
  const sel   = _selectedStocks.size;
  if (cnt) cnt.textContent = total ? `(${sel}/${total})` : '';
  // Update Select All button label
  const btn = document.getElementById('wlSelectAllBtn');
  if (btn && total > 0) btn.textContent = (sel === total) ? 'Unselect All' : 'Select All';
}

function toggleSelectAll() {
  const total = _watchlistStocks.length;
  if (!total) return;
  const allSelected = _selectedStocks.size === total;
  if (allSelected) {
    // Unselect all
    _watchlistStocks.forEach(s => {
      _selectedStocks.delete(s.symbol);
      const item = document.getElementById('wl-' + s.symbol);
      const btn  = item ? item.querySelector('.wl-sel') : null;
      if (item) item.classList.remove('selected');
      if (btn)  { btn.classList.remove('on'); btn.textContent = '○'; btn.title = 'Click to select for trading'; }
    });
  } else {
    // Select all
    _watchlistStocks.forEach(s => {
      _selectedStocks.add(s.symbol);
      const item = document.getElementById('wl-' + s.symbol);
      const btn  = item ? item.querySelector('.wl-sel') : null;
      if (item) item.classList.add('selected');
      if (btn)  { btn.classList.add('on'); btn.textContent = '✓'; btn.title = 'Click to deselect (will not trade)'; }
    });
  }
  _updateWlSelCount();
  _sortWatchlistByScore();
}

async function addWatchlistStock(symbol) {
  const spin = document.getElementById('wlSpin');
  const dd   = document.getElementById('wlDropdown');
  const inp  = document.getElementById('wlSearchInput');
  if (spin) spin.style.display = 'inline';
  if (dd)  dd.innerHTML = `<div class="wl-dd-hint">Resolving ATM options for ${symbol}…</div>`;
  try {
    const resp = await fetch('/api/add_watchlist_stock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol }),
    });
    const data = await resp.json();
    if (data.error) { alert('Error: ' + data.error); if (dd) dd.innerHTML = ''; return; }
    if (dd)  dd.innerHTML = '';
    if (inp) inp.value = '';
    _selectedStocks.add(symbol.toUpperCase());   // auto-select on add
    await loadWatchlist();
  } catch(e) {
    alert('Failed to add ' + symbol + ': ' + e.message);
    if (dd) dd.innerHTML = '';
  } finally {
    if (spin) spin.style.display = 'none';
  }
}

async function removeWatchlistStock(symbol) {
  try {
    await fetch('/api/remove_watchlist_stock/' + symbol, { method: 'DELETE' });
    _selectedStocks.delete(symbol);
    _watchlistStocks = _watchlistStocks.filter(s => s.symbol !== symbol);
    renderWatchlist();
  } catch(e) { alert('Failed to remove: ' + e.message); }
}

// Watchlist search input handler (set up after DOM ready)
(function setupWatchlistSearch() {
  const inp = document.getElementById('wlSearchInput');
  const dd  = document.getElementById('wlDropdown');
  if (!inp) return;
  inp.addEventListener('input', () => {
    clearTimeout(_wlSearchTimer);
    const q = inp.value.trim();
    if (!q) { dd.innerHTML = ''; return; }
    dd.innerHTML = '<div class="wl-dd-hint">Searching…</div>';
    _wlSearchTimer = setTimeout(() => _fetchFoStocks(q), 260);
  });
  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape') { dd.innerHTML = ''; inp.value = ''; }
    if (e.key === 'Enter') {
      const first = dd.querySelector('.wl-dd-row');
      if (first) first.click();
    }
  });
  document.addEventListener('click', e => {
    if (!inp.contains(e.target) && !dd.contains(e.target)) dd.innerHTML = '';
  });
})();

async function _fetchFoStocks(q) {
  const dd = document.getElementById('wlDropdown');
  try {
    const data = await fetch('/api/search_fo_stocks?q=' + encodeURIComponent(q)).then(r => r.json());
    if (data.error) { dd.innerHTML = `<div class="wl-dd-hint">${data.error}</div>`; return; }
    if (!data.length) { dd.innerHTML = '<div class="wl-dd-hint">No F&amp;O stocks found</div>'; return; }
    dd.innerHTML = data.map(s =>
      `<div class="wl-dd-row" onclick="addWatchlistStock('${s.symbol}')">
        <span class="wl-dd-sym">${s.symbol}</span>
      </div>`
    ).join('');
  } catch(e) {
    dd.innerHTML = `<div class="wl-dd-hint">Error: ${e.message}</div>`;
  }
}

// ── Watchlist live data ───────────────────────────────────────
function updateWatchlistLive(multiStocks) {
  if (!multiStocks) return;
  for (const [sym, d] of Object.entries(multiStocks)) {
    const item = document.getElementById('wl-' + sym);
    if (!item) continue;

    // Prefer composite_score (multi-factor) for sorting; fall back to live tick score
    const sortScore = d.composite_score != null && d.composite_score > 0
                    ? d.composite_score : (d.score || 0);
    item.dataset.score = sortScore;

    // Direction badge
    const dir = document.getElementById('wl-dir-' + sym);
    if (dir) {
      dir.className = 'wl-dir ' + (d.direction || 'flat');
      dir.textContent = d.direction === 'bull' ? '▲' : d.direction === 'bear' ? '▼' : '—';
    }
    // Spot price
    const spot = document.getElementById('wl-spot-' + sym);
    if (spot && d.price != null) spot.textContent = '₹' + (+d.price).toFixed(1);
    // % change
    const chg = document.getElementById('wl-chg-' + sym);
    if (chg && d.change_pct != null) {
      const p = d.change_pct;
      chg.textContent = (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
      chg.className = 'wl-chg ' + (p > 0.1 ? 'bull' : p < -0.1 ? 'bear' : 'flat');
    }
    // CE price
    const ce = document.getElementById('wl-ce-' + sym);
    if (ce) ce.textContent = d.ce_price != null ? 'CE ₹' + (+d.ce_price).toFixed(1) : (d.ce_symbol ? 'CE --' : '');
    // PE price
    const pe = document.getElementById('wl-pe-' + sym);
    if (pe) pe.textContent = d.pe_price != null ? 'PE ₹' + (+d.pe_price).toFixed(1) : (d.pe_symbol ? 'PE --' : '');
    // Composite score badge + AI strategy state
    const scoreEl = document.getElementById('wl-score-' + sym);
    if (scoreEl) {
      const cs  = d.composite_score || 0;
      const ai  = d.ai_strategy || {};
      const p   = ai.last_params || {};
      if (cs > 0) {
        scoreEl.textContent = '◆ ' + cs.toFixed(0);
        scoreEl.className   = 'wl-score ' + (cs >= 70 ? 'hot' : cs >= 45 ? 'warm' : 'cool');
        // Rich tooltip: score breakdown + AI params
        const bd  = d.score_breakdown || {};
        const lines = [
          `Score ${cs.toFixed(0)}/100`,
          d.hist_atr ? `7d ATR: ${(+d.hist_atr).toFixed(1)}pts  →  Spike threshold: ${(+d.hist_threshold).toFixed(1)}pts` : `Spike threshold: ${(+d.jump_threshold).toFixed(1)}pts (live ATR)`,
          bd.atr      ? `ATR:${bd.atr}  Mom:${bd.mom_3d}  Rng:${bd.intraday}` : '',
          bd.week_pos ? `Wk:${bd.week_pos}  Tr30:${bd.trend_30}  Prem:${bd.premium}` : '',
          ai.n_trades != null
            ? `AI: ${ai.n_trades} trades  WR:${(ai.win_rate*100).toFixed(0)}%  ${ai.model_ready?'✓ ready':'warming'}`
            : '',
          p.sl_pct_p1 ? `SL:${p.sl_pct_p1}%→${p.sl_pct_p2}%  trail:${p.trail_pct}%  t/o:${p.timeout_secs}s` : '',
          p.side_bias ? `Bias: ${p.side_bias}` : '',
        ].filter(Boolean).join('\n');
        scoreEl.title = lines;
      }
    }

    // Pending spike indicator
    const pend = document.getElementById('wl-pend-' + sym);
    if (pend) {
      if (d.pending_side) {
        pend.textContent = '⚡ ' + d.pending_side;
        pend.style.display = '';
      } else {
        pend.style.display = 'none';
      }
    }

    // Spike threshold badge — shows per-stock historical ATR-based threshold
    const spikeEl = document.getElementById('wl-spike-' + sym);
    if (spikeEl) {
      if (d.hist_threshold != null && d.hist_atr != null) {
        spikeEl.textContent = `⚡ ${(+d.hist_threshold).toFixed(1)}pts`;
        spikeEl.title = `7-day ATR: ${(+d.hist_atr).toFixed(1)}pts  →  Spike threshold: ${(+d.hist_threshold).toFixed(1)}pts  (7d avg range × 18%)`;
        spikeEl.className = 'wl-spike-thr ready';
      } else {
        const live = d.jump_threshold || d.threshold;
        spikeEl.textContent = live ? `⚡ ${(+live).toFixed(1)}pts` : '⚡ --';
        spikeEl.title = live ? `Live ATR threshold: ${(+live).toFixed(1)}pts (historical ATR loading…)` : 'Calculating…';
        spikeEl.className = 'wl-spike-thr loading';
      }
    }
  }
  // Only re-sort every 30 seconds to prevent constant position swapping
  const now = Date.now();
  if ((now - _lastSortTs) >= 30000) {
    _lastSortTs = now;
    _sortWatchlistByScore();
  }
}

let _lastSortTs = 0;

function _sortWatchlistByScore() {
  const list = document.getElementById('wlList');
  if (!list) return;
  const items = Array.from(list.querySelectorAll('.wl-item'));
  items.sort((a, b) => {
    // Selected stocks always on top; within group sort by score descending
    const aSel = a.classList.contains('selected') ? 1 : 0;
    const bSel = b.classList.contains('selected') ? 1 : 0;
    if (bSel !== aSel) return bSel - aSel;
    return parseFloat(b.dataset.score || 0) - parseFloat(a.dataset.score || 0);
  });
  items.forEach(el => list.appendChild(el));
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
  if (isReal) {
    fetchRealBalance();
  } else {
    // Restore demo capital from last known state
    const capEl = document.getElementById('capVal');
    if (capEl && _lastCapital) capEl.textContent = '₹' + _lastCapital.toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }
}

function setMode(mode) {
  if (mode === 'real') {
    const ok = confirm('⚠ Switch to REAL TRADING?\n\nLive orders will be placed on Zerodha.\nConfirm your enctoken is up to date!\n\nClick OK to confirm.');
    if (!ok) return;
  }
  socket.emit('set_trading_mode', { mode });
}

async function fetchRealBalance() {
  const capEl = document.getElementById('capVal');
  if (capEl) capEl.textContent = '₹…';
  try {
    const data = await fetch('/api/account_balance').then(r => r.json());
    if (data.error) { if (capEl) capEl.textContent = 'ERR'; return; }
    if (capEl) capEl.textContent = '₹' + (+data.available).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  } catch(e) {
    if (capEl) capEl.textContent = '₹--';
  }
}


// ── Chart ─────────────────────────────────────────────────────
function setChart(mode) {
  chartMode = mode;
  document.getElementById('ctN').classList.toggle('active', mode === 'nifty');
  document.getElementById('ctO').classList.toggle('active', mode === 'option');
  document.getElementById('ctSS') && document.getElementById('ctSS').classList.toggle('active', mode === 'stock_spot');
  document.getElementById('ctSC') && document.getElementById('ctSC').classList.toggle('active', mode === 'stock_ce');
  document.getElementById('ctSP') && document.getElementById('ctSP').classList.toggle('active', mode === 'stock_pe');
  const sym = _activeStockSym;
  if (mode === 'nifty')      document.getElementById('chartTitle').textContent = 'NIFTY 50';
  else if (mode === 'option')document.getElementById('chartTitle').textContent = 'Option Price';
  else if (mode === 'stock_spot' && sym) document.getElementById('chartTitle').textContent = sym + ' · SPOT';
  else if (mode === 'stock_ce'   && sym) document.getElementById('chartTitle').textContent = sym + ' · CE Premium';
  else if (mode === 'stock_pe'   && sym) document.getElementById('chartTitle').textContent = sym + ' · PE Premium';
  rebuildAndDraw();
}
// Map TF (seconds) → Zerodha interval string for historical API
function _tfToZerodhaInterval(tf) {
  if (tf <= 60)   return 'minute';
  if (tf <= 180)  return '3minute';
  if (tf <= 300)  return '5minute';
  if (tf <= 900)  return '15minute';
  if (tf <= 1800) return '30minute';
  return '60minute';
}

function setTF(tf, id) {
  TF = tf;
  document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  // If a stock chart is open, reload historical candles at the right interval
  if (_activeStockSym) {
    _reloadStockHistorical(_activeStockSym);
  } else {
    rebuildAndDraw();
  }
}

async function _reloadStockHistorical(symbol) {
  const interval = _tfToZerodhaInterval(TF);
  try {
    const data = await fetch(
      '/api/stock_detail/' + encodeURIComponent(symbol) + '?interval=' + interval
    ).then(r => r.json());
    stockSpotCandles = data.spot_candles || [];
    stockCeCandles   = data.ce_candles   || [];
    stockPeCandles   = data.pe_candles   || [];
    rebuildAndDraw();
  } catch(e) {
    console.error('Historical reload failed:', e);
    rebuildAndDraw();
  }
}
function buildCandles() {
  if (chartMode.startsWith('stock_')) {
    // Pre-built OHLC from Zerodha historical API (minute candles)
    const pre  = chartMode === 'stock_spot' ? stockSpotCandles
               : chartMode === 'stock_ce'   ? stockCeCandles
               : stockPeCandles;
    // Live ticks accumulated since page load (from WebSocket state events)
    const live = chartMode === 'stock_spot' ? stockSpotHist
               : chartMode === 'stock_ce'   ? stockCeHist
               : stockPeHist;

    if (!pre.length && !live.length) { candles = []; return; }

    // Start with historical candles as immutable base
    candles = pre.map(c => ({ t: c.t, o: c.o, h: c.h, l: c.l, c: c.c }));

    // Bucket live ticks using current TF, append after historical data
    if (live.length) {
      const lastPreT = candles.length ? candles[candles.length - 1].t : 0;
      const liveMap  = new Map();
      for (const { t, v } of live) {
        const k = Math.floor(t / TF) * TF;
        if (!liveMap.has(k)) liveMap.set(k, []);
        liveMap.get(k).push(v);
      }
      for (const [t, arr] of [...liveMap.entries()].sort((a, b) => a[0] - b[0])) {
        if (t > lastPreT) {
          candles.push({ t, o: arr[0], h: Math.max(...arr), l: Math.min(...arr), c: arr[arr.length - 1] });
        }
      }
    }
    return;
  }

  // Nifty / Option — original tick-based logic
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
  const isStock = chartMode.startsWith('stock_');
  const levels = chartMode === 'nifty' ? [lvl.ref] : isStock ? [] : [lvl.entry, lvl.sl, lvl.trail];
  for (const v of levels) {
    if (v > 0 && Math.abs(v - (mn+mx)/2) < thr) { mn = Math.min(mn, v); mx = Math.max(mx, v); }
  }
  const pad = Math.max((mx - mn) * 0.08, 2);
  return { mn: mn - pad, mx: mx + pad };
}
function getMainH() { return Math.max(80, cv.height - AX_H); }
function py(p, mn, mx) { return _mainH * (1 - (p - mn) / (mx - mn)); }
function drawGrid(mn, mx) {
  const h = _mainH, w = cv.width - AX_W;
  const range = mx - mn || 1, raw = range / 6;
  const mag  = Math.pow(10, Math.floor(Math.log10(raw)));
  const nice = Math.ceil(raw / mag) * mag;
  const sv   = Math.ceil(mn / nice) * nice;
  ctx.font = '9px IBM Plex Mono'; ctx.textAlign = 'right';
  for (let v = sv; v <= mx + nice; v += nice) {
    if (v < mn) continue;
    const y = py(v, mn, mx);
    if (y < 0 || y > h) continue;
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
    ctx.fillText(d.toLocaleTimeString('en', { hour:'2-digit', minute:'2-digit', hour12:false }), x, _mainH + AX_H - 4);
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
  if (y < 0 || y > _mainH) return;
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
  const w = cv.width - AX_W, h = _mainH;
  if (mX > w || mY > h) return;
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
  const active = (chartMode === 'nifty' && !chartMode.startsWith('stock_')) ? niftyLvls
               : chartMode.startsWith('stock_') ? []
               : optionLvls;
  const hidden = chartMode.startsWith('stock_') ? [...niftyLvls, ...optionLvls]
               : chartMode === 'nifty' ? optionLvls : niftyLvls;
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
  ctx.fillText('Add stocks to watchlist and click Start Buy Robot', cv.width/2, cv.height/2-8);
  ctx.fillStyle = 'rgba(78,101,133,.08)'; ctx.font = '10px IBM Plex Mono';
  ctx.fillText('Scroll zoom · Drag pan · Toggle Nifty/Option', cv.width/2, cv.height/2+14);
}
// ── Heikin-Ashi transform ─────────────────────────────────────
function toHeikinAshi(src) {
  const ha = [];
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    const haC = (c.o + c.h + c.l + c.c) / 4;
    const haO = i === 0 ? (c.o + c.c) / 2 : (ha[i-1].o + ha[i-1].c) / 2;
    ha.push({ t: c.t, o: haO, h: Math.max(c.h, haO, haC), l: Math.min(c.l, haO, haC), c: haC });
  }
  return ha;
}

// ── Chart type helpers ────────────────────────────────────────
function setChartType(type) {
  chartType = type;
  ['ctypeCandle','ctypeHA','ctypeLine','ctypeArea'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  });
  const map = { candle:'ctypeCandle', ha:'ctypeHA', line:'ctypeLine', area:'ctypeArea' };
  const el = document.getElementById(map[type]);
  if (el) el.classList.add('active');
  draw();
}

function _drawLineChart(src, start, end, cw, mn, mx) {
  ctx.save(); ctx.strokeStyle = UP; ctx.lineWidth = 1.5; ctx.setLineDash([]);
  ctx.beginPath(); let s = false;
  for (let i = start; i < end; i++) {
    const x = (i-start)*cw + candleW/2, y = py(src[i].c, mn, mx);
    if (!s) { ctx.moveTo(x,y); s=true; } else ctx.lineTo(x,y);
  }
  ctx.stroke(); ctx.restore();
}
function _drawAreaChart(src, start, end, cw, mn, mx) {
  const baseY = _mainH;
  ctx.save();
  const grad = ctx.createLinearGradient(0, 0, 0, baseY);
  grad.addColorStop(0, 'rgba(0,232,122,0.28)'); grad.addColorStop(1, 'rgba(0,232,122,0.0)');
  ctx.beginPath();
  const sx = start*cw + candleW/2 - start*cw;
  ctx.moveTo(0, baseY);
  for (let i = start; i < end; i++) {
    const x = (i-start)*cw + candleW/2, y = py(src[i].c, mn, mx);
    if (i === start) { ctx.lineTo(x, baseY); ctx.lineTo(x, y); }
    else ctx.lineTo(x, y);
  }
  ctx.lineTo((end-1-start)*cw + candleW/2, baseY);
  ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  for (let i = start; i < end; i++) {
    const x = (i-start)*cw + candleW/2, y = py(src[i].c, mn, mx);
    if (i === start) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.strokeStyle = UP; ctx.lineWidth = 1.5; ctx.setLineDash([]); ctx.stroke();
  ctx.restore();
}
// ── Main draw ─────────────────────────────────────────────────
function draw() {
  if (!cv.width || !cv.height) return;
  _mainH = getMainH();
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#050810'; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!candles.length) { drawEmpty(); return; }

  const dispCandles = chartType === 'ha' ? toHeikinAshi(candles) : candles;
  const { start, end } = view();
  const { mn, mx }     = priceRange(start, end);
  drawGrid(mn, mx);

  if (chartMode.startsWith('stock_')) {
    // no level overlays
  } else if (chartMode === 'nifty') {
    drawLevel(lvl.ref, 'rgba(176,106,255,.8)', [8,4], mn, mx);
  } else {
    drawLevel(lvl.entry, 'rgba(0,212,255,.85)',  [8,4], mn, mx);
    drawLevel(lvl.sl,    'rgba(255,61,92,.85)',  [4,4], mn, mx);
    drawLevel(lvl.trail, 'rgba(255,176,32,.85)', [6,3], mn, mx);
  }

  const cw = candleW + 2;
  if (chartType === 'line') {
    _drawLineChart(dispCandles, start, end, cw, mn, mx);
  } else if (chartType === 'area') {
    _drawAreaChart(dispCandles, start, end, cw, mn, mx);
  } else {
    for (let i = start; i < end; i++) drawCandle(dispCandles[i], (i-start)*cw, mn, mx, i===candles.length-1);
  }

  drawMarkers(start, mn, mx);
  if (lastP) {
    const y = py(lastP, mn, mx), w = cv.width - AX_W;
    ctx.save();
    ctx.strokeStyle = 'rgba(0,212,255,.35)'; ctx.lineWidth = 1; ctx.setLineDash([2,4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    ctx.restore();
  }
  drawUserDrawings(mn, mx);
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
cv.addEventListener('mousedown', e => {
  if (activeTool !== 'cursor') { handleDrawClick(e); return; }
  drag=true; dX=e.clientX; dOff=viewOff; cv.style.cursor='grabbing';
});
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
  logInit = false;
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
  if (d.index_name) {
    const lbl = document.getElementById('tkNLabel');
    if (lbl) lbl.textContent = d.index_name;
  }

  // CE / PE live prices (topbar only)
  if (d.ce_price != null) set('tkCE', '₹' + (+d.ce_price).toFixed(2));
  if (d.pe_price != null) set('tkPE', '₹' + (+d.pe_price).toFixed(2));

  // Live watchlist momentum update
  if (d.multi_stocks) updateWatchlistLive(d.multi_stocks);
  // Live-update stock detail modal if open
  if (d.multi_stocks) _sdLiveUpdate(d.multi_stocks);

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
  if (d.profit_trail_threshold != null) set('fvProfitThreshold', `≥${d.profit_trail_threshold}%`);

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
    updateAiRing(sc);
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
        const rem = Math.max(0, 15 - (b.n_trades || 0));
        readyEl.innerHTML = `<span class="ai-ready-dot off"></span> ${rem} more trades`;
        readyEl.className = 'ai-v y';
      }
    }
  }

  if (d.adv_vol_atr != null) {
    const atr = (+d.adv_vol_atr).toFixed(2);
    const mult = d.adv_vol_mult || 1.0;
    const revRate = d.adv_vol_rev_rate != null ? (+d.adv_vol_rev_rate * 100).toFixed(0) + '%' : '--';
    set('tkVolAtr', atr + 'p');
    document.getElementById('tkVolAtr').style.color =
      mult >= 1.2 ? 'var(--green)' : mult <= 0.8 ? 'var(--amber)' : 'var(--cyan)';
    const gateEl = document.getElementById('fvVolGate');
    if (gateEl) {
      if (!d.adv_vol_gate_enabled) {
        gateEl.textContent = 'OFF'; gateEl.className = 'fval fv-off';
      } else if (mult >= 1.2) {
        gateEl.textContent = 'LOW-VOL ✓'; gateEl.className = 'fval fv-boost';
      } else if (mult <= 0.0) {
        gateEl.textContent = 'CHOPPY ✗'; gateEl.className = 'fval fv-block';
      } else if (mult <= 0.8) {
        gateEl.textContent = 'HIGH-VOL'; gateEl.className = 'fval fv-warn';
      } else {
        gateEl.textContent = 'NORMAL'; gateEl.className = 'fval fv-ok';
      }
    }
    set('fvVolAtr', atr + ' pts');
    set('fvChopRate', revRate);
    document.getElementById('fvChopRate').className =
      'fval ' + (+d.adv_vol_rev_rate >= 0.65 ? 'fv-block' : +d.adv_vol_rev_rate >= 0.45 ? 'fv-warn' : 'fv-ok');
  }
  if (d.adv_breakout_range != null) {
    const br = d.adv_breakout_range;
    const boEl = document.getElementById('fvBreakout');
    if (boEl) {
      if (!d.adv_breakout_enabled) { boEl.textContent = 'OFF'; boEl.className = 'fval fv-off'; }
      else { boEl.textContent = 'ON'; boEl.className = 'fval fv-ok'; }
    }
    const brEl = document.getElementById('fvBreakoutRange');
    if (brEl && br.rng_low != null) {
      brEl.textContent = br.rng_low.toFixed(1) + '–' + br.rng_high.toFixed(1) + ' (' + br.rng_width.toFixed(1) + 'p)';
    }
  }

  // Config values
  if (d.breakeven_trigger_pct != null) set('fvBreakeven', d.breakeven_trigger_pct + '%');
  if (d.nifty_reversal_exit != null) {
    const el = document.getElementById('fvNiftyReversal');
    if (el) { el.textContent = d.nifty_reversal_exit ? 'ON' : 'OFF'; el.className = d.nifty_reversal_exit ? 'fval fv-ok' : 'fval fv-warn'; }
  }
  if (d.fast_move_velocity != null) set('fvMoveVelocity', d.fast_move_velocity + ' pts/tick');

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
  if (d.capital) {
    _lastCapital = d.capital;
    if (currentMode !== 'real') set('capVal', '₹' + d.capital.toLocaleString('en-IN'));
  }

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

  if (d.logs && !logInit) { d.logs.forEach(appendLog); logInit = true; }
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
  document.getElementById('tcProfitTrailRow').classList.remove('show');
  document.getElementById('tcRatchetRow').classList.remove('show');
  ['tcBreakevenRow','tcMoveTypeRow','tcMomentumRow'].forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
  const timerRow = document.getElementById('tcTimerRow');
  if (timerRow) timerRow.style.display = 'none';
  const fill = document.getElementById('tcTimerFill');
  if (fill) fill.style.width = '0%';
  const hero = document.getElementById('livePnlHero');
  if (hero) { hero.textContent = '--'; hero.className = 'live-pnl-hero'; }
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
  if (Math.abs(pct)>=1&&!wrongDir) { subEl.textContent='🚀 Spike! Running filters…'; flashSpikeGauge(); }
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
  const card           = document.getElementById('tradeCard');
  const side           = document.getElementById('tcSide');
  const badge          = document.getElementById('tcBadge');
  const profitTrailRow = document.getElementById('tcProfitTrailRow');
  const ratchetRow     = document.getElementById('tcRatchetRow');

  if (!d.trade_open && !d.active_status) {
    side.textContent='No trade'; side.className='tc-side none';
    badge.textContent='Waiting'; badge.className='tc-badge wait';
    card.className='trade-card';
    ['tcEntry','tcCurrent','tcSL','tcTrail','tcPeak','tcPeakPct','tcPeakP','tcQty','tcHeld','tcLivePL','tcAtrTrail','tcMinTrail']
      .forEach(id => set(id,'--'));
    ['tcTrailRow','tcPeakRow','tcPeakPctRow','tcProfRow','tcAtrRow']
      .forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
    profitTrailRow.classList.remove('show');
    ratchetRow.classList.remove('show');
    const heroEl = document.getElementById('livePnlHero');
    if (heroEl) { heroEl.textContent = '--'; heroEl.className = 'live-pnl-hero'; }
    const timerRowEl = document.getElementById('tcTimerRow');
    if (timerRowEl) timerRowEl.style.display = 'none';
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
    const timerRow = document.getElementById('tcTimerRow');
    if (timerRow) timerRow.style.display = 'flex';
    updateTradeTimer(h);
  }
  if (d.opt_price != null) set('tcCurrent', '₹' + d.opt_price.toFixed(2));
  if (d.live_pnl  != null) {
    const v = d.live_pnl, el = document.getElementById('tcLivePL');
    el.textContent = (v>=0?'+':'')+'₹'+v.toFixed(2);
    el.className   = 'lv '+(v>=0?'g':'r');
    const hero = document.getElementById('livePnlHero');
    if (hero) {
      hero.textContent = (v>=0?'+':'')+'₹'+v.toFixed(2);
      hero.className = 'live-pnl-hero ' + (v>=0?'profit':'loss');
    }
  }

  if (d.phase2) {
    ['tcTrailRow','tcPeakRow','tcPeakPctRow','tcProfRow','tcAtrRow']
      .forEach(id => { const el=document.getElementById(id); if(el) el.style.display='flex'; });
    if (d.trail_price != null) { set('tcTrail', '₹'+d.trail_price.toFixed(2)); lvl.trail=d.trail_price; }
    if (d.peak_price  != null) set('tcPeak',   '₹'+d.peak_price.toFixed(2));
    if (d.peak_profit_pct != null) set('tcPeakPct', '+'+d.peak_profit_pct.toFixed(1)+'%');
    if (d.peak_profit != null) set('tcPeakP',  '₹'+d.peak_profit.toFixed(2));
    if (d.trail_pct   != null) set('tcTrPct',  `(${(+d.trail_pct).toFixed(1)}%)`);
    if (d.atr_trail_pct != null) {
      const atrTp = +d.atr_trail_pct;
      const atr   = d.option_atr != null ? +d.option_atr : null;
      const atrEl = document.getElementById('tcAtrTrail');
      atrEl.textContent = (atr!=null?`ATR=${atr.toFixed(2)} → `:'') + `${atrTp.toFixed(1)}%`;
      atrEl.className   = 'lv '+(atrTp>=cfgTrailHigh*0.9?'r':atrTp<=cfgTrailLow*1.1?'g':'y');
    }
    if (d.profit_trail_pct != null) {
      set('tcProfitTrail', d.profit_trail_pct.toFixed(1)+'%  ← ACTIVE');
      profitTrailRow.classList.add('show');
    } else { profitTrailRow.classList.remove('show'); }
    if (d.min_trail_pct_reached != null) {
      set('tcMinTrail', d.min_trail_pct_reached.toFixed(1)+'%');
      ratchetRow.classList.add('show');
    } else { ratchetRow.classList.remove('show'); }

    // Breakeven, move type, momentum
    const beRow = document.getElementById('tcBreakevenRow');
    if (beRow) {
      beRow.style.display = 'flex';
      const beEl = document.getElementById('tcBreakeven');
      if (d.breakeven_moved) {
        beEl.textContent = '✓ Moved to entry';
        beEl.className = 'lv g';
      } else {
        beEl.textContent = 'Not yet';
        beEl.className = 'lv y';
      }
    }
    const mtRow = document.getElementById('tcMoveTypeRow');
    if (mtRow && d.move_type) {
      mtRow.style.display = 'flex';
      const mtEl = document.getElementById('tcMoveType');
      if (mtEl) {
        mtEl.textContent = d.move_type === 'fast' ? '⚡ FAST' : '🐢 SLOW';
        mtEl.className = 'lv ' + (d.move_type === 'fast' ? 'g' : 'y');
      }
    }
    const mmRow = document.getElementById('tcMomentumRow');
    if (mmRow && d.momentum_score != null) {
      mmRow.style.display = 'flex';
      const sc = +d.momentum_score;
      const mmEl = document.getElementById('tcMomentum');
      const mFill = document.getElementById('momentumFill');
      if (mmEl) {
        mmEl.textContent = sc.toFixed(2);
        mmEl.className = 'lv ' + (sc >= 0.65 ? 'g' : sc >= 0.35 ? 'y' : 'r');
      }
      if (mFill) {
        mFill.style.width = (sc * 100) + '%';
        mFill.style.background = sc >= 0.65 ? 'var(--green)' : sc >= 0.35 ? 'var(--amber)' : 'var(--red)';
        set('momentumLbl', sc.toFixed(2));
      }
    }
  } else {
    profitTrailRow.classList.remove('show');
    ratchetRow.classList.remove('show');
  }
}

// ── Advanced UI helpers ───────────────────────────────────────
function updateAiRing(score) {
  const el = document.getElementById('aiRingProg');
  if (!el) return;
  const circumference = 163.4;
  el.style.strokeDashoffset = circumference * (1 - Math.max(0, Math.min(1, score)));
  el.style.stroke = score >= 0.72 ? 'var(--green)' : score >= 0.57 ? 'var(--cyan)' : score >= 0.42 ? 'var(--amber)' : 'var(--red)';
}

function updateTradeTimer(heldSecs) {
  const fill = document.getElementById('tcTimerFill');
  if (!fill) return;
  const pct = Math.min(100, (heldSecs / 300) * 100);
  fill.style.width = pct + '%';
  fill.style.background = pct >= 80 ? 'var(--red)' : pct >= 50 ? 'var(--amber)' : 'var(--cyan)';
}

let _flashTimer = null;
function flashSpikeGauge() {
  const gauge = document.querySelector('.spike-gauge');
  if (!gauge) return;
  gauge.classList.add('spiking');
  clearTimeout(_flashTimer);
  _flashTimer = setTimeout(() => gauge.classList.remove('spiking'), 800);
}

function toggleFilters() {
  const sec = document.getElementById('filterSec');
  const chev = document.getElementById('filterChevron');
  if (!sec) return;
  sec.classList.toggle('open');
  if (chev) chev.style.transform = sec.classList.contains('open') ? 'rotate(180deg)' : '';
}

// ── Controls ──────────────────────────────────────────────────
function toggleRobot() {
  if (isRunning) {
    socket.emit('stop_robot');
    isRunning = false; updateBtn();
  } else {
    if (!_watchlistStocks.length) {
      alert('Add at least one stock to the watchlist before starting'); return;
    }
    const activeSyms = [..._selectedStocks].filter(s => _watchlistStocks.some(w => w.symbol === s));
    if (!activeSyms.length) {
      alert('Tick at least one stock (✓) to enable trading'); return;
    }
    if (currentMode === 'real') {
      const ok = confirm(
        `🔴 Starting in REAL TRADING mode!\n\nActive stocks: ${activeSyms.join(', ')}\n\n` +
        'Live BUY/SELL orders will be sent to Zerodha.\n\nConfirm to proceed.'
      );
      if (!ok) return;
    }

    // Reset local state
    markers=[]; lastP=null; logInit=false;
    lvl={ref:0,entry:0,sl:0,trail:0};
    bestTrade=null; worstTrade=null;
    niftyHist=[]; optHist=[]; candles=[];
    lastSkipReason=null;
    set('oidBuy','—'); set('oidSell','—');
    ['tcEntry','tcCurrent','tcSL','tcTrail','tcPeak','tcPeakPct','tcPeakP','tcQty','tcHeld','tcLivePL','tcAtrTrail','tcMinTrail']
      .forEach(id => set(id,'--'));
    ['tcTrailRow','tcPeakRow','tcPeakPctRow','tcProfRow','tcAtrRow']
      .forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
    document.getElementById('tcProfitTrailRow').classList.remove('show');
    document.getElementById('tcRatchetRow').classList.remove('show');
    ['tcBreakevenRow','tcMoveTypeRow','tcMomentumRow'].forEach(id => { const el=document.getElementById(id); if(el) el.style.display='none'; });
    set('aiScoreVal','--'); set('aiBrainTrades','0'); set('aiBrainWR','--');
    const aiScoreFill = document.getElementById('aiScoreFill');
    if (aiScoreFill) aiScoreFill.style.width = '0%';
    updateAiRing(0);
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
    set('tkVolAtr','--');

    socket.emit('start_robot', { symbols: activeSyms });
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
  if (!b) return;
  if (isRunning) {
    b.className = 'btn btn-stop'; b.textContent = '⏹ Stop Robot';
  } else {
    b.className = 'btn btn-go'; b.textContent = '🎯 Start Buy Robot';
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

// ── Drawing Tools ─────────────────────────────────────────────
let activeTool = 'cursor';
let drawings   = [];   // {type:'hline', price} | {type:'tline', idx1,p1,idx2,p2} | {type:'rect', idx1,p1,idx2,p2}
let drawState  = null; // in-progress drawing

function setDrawTool(tool) {
  activeTool = tool;
  document.querySelectorAll('.dt-btn').forEach(b => b.classList.remove('active'));
  const idMap = { cursor:'dtCursor', hline:'dtHline', tline:'dtTline', rect:'dtRect', vline:'dtVline' };
  const el = document.getElementById(idMap[tool]);
  if (el) el.classList.add('active');
  drawState = null;
  cv.style.cursor = tool === 'cursor' ? 'crosshair' : 'cell';
}

function clearDrawings() { drawings = []; drawState = null; draw(); }

function _canvasToData(e) {
  const r   = cv.getBoundingClientRect();
  const sx  = cv.width  / r.width;
  const sy  = cv.height / r.height;
  const x   = (e.clientX - r.left) * sx;
  const y   = (e.clientY - r.top)  * sy;
  const { start } = view();
  const { mn, mx } = priceRange(start, start + Math.floor((cv.width - AX_W) / (candleW + 2)));
  const h     = _mainH;
  const price = mn + (1 - Math.max(0, Math.min(h, y)) / h) * (mx - mn);
  const idx   = start + Math.floor(x / (candleW + 2));
  return { x, y, price, idx };
}

function handleDrawClick(e) {
  if (activeTool === 'cursor') return;
  const d = _canvasToData(e);
  if (activeTool === 'hline') {
    drawings.push({ type: 'hline', price: d.price });
    draw();
  } else if (activeTool === 'tline') {
    if (!drawState) {
      drawState = { type: 'tline', idx1: d.idx, p1: d.price };
    } else {
      drawings.push({ type: 'tline', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price });
      drawState = null;
      draw();
    }
  } else if (activeTool === 'rect') {
    if (!drawState) {
      drawState = { type: 'rect', idx1: d.idx, p1: d.price };
    } else {
      drawings.push({ type: 'rect', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price });
      drawState = null;
      draw();
    }
  } else if (activeTool === 'vline') {
    drawings.push({ type: 'vline', idx: d.idx });
    draw();
  }
}

function drawUserDrawings(mn, mx) {
  const { start } = view();
  const cw = candleW + 2;
  const h  = _mainH;
  const w  = cv.width  - AX_W;

  for (const d of drawings) {
    ctx.save();
    if (d.type === 'hline') {
      const y = py(d.price, mn, mx);
      if (y < 0 || y > h) { ctx.restore(); continue; }
      ctx.strokeStyle = 'rgba(255,255,255,.55)';
      ctx.lineWidth   = 1; ctx.setLineDash([6,4]); ctx.globalAlpha = 0.85;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      ctx.font = '9px IBM Plex Mono'; ctx.textAlign = 'right';
      ctx.fillStyle = 'rgba(255,255,255,.6)'; ctx.globalAlpha = 1; ctx.setLineDash([]);
      ctx.fillText(d.price.toFixed(2), w - 3, y - 3);
    } else if (d.type === 'tline') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(d.p1, mn, mx);
      const y2 = py(d.p2, mn, mx);
      ctx.strokeStyle = 'rgba(255,200,50,.75)';
      ctx.lineWidth   = 1.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.fillStyle = 'rgba(255,200,50,.9)';
      [x1, x2].forEach((x, i) => {
        ctx.beginPath(); ctx.arc(x, i === 0 ? y1 : y2, 3, 0, Math.PI * 2); ctx.fill();
      });
    } else if (d.type === 'rect') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(Math.max(d.p1, d.p2), mn, mx);
      const y2 = py(Math.min(d.p1, d.p2), mn, mx);
      ctx.strokeStyle = 'rgba(77,142,255,.7)';
      ctx.fillStyle   = 'rgba(77,142,255,.07)';
      ctx.lineWidth   = 1.5; ctx.setLineDash([]);
      ctx.fillRect(Math.min(x1,x2), y1, Math.abs(x2-x1), y2-y1);
      ctx.strokeRect(Math.min(x1,x2), y1, Math.abs(x2-x1), y2-y1);
    } else if (d.type === 'vline') {
      const x = (d.idx - start) * cw + candleW / 2;
      if (x < 0 || x > w) { ctx.restore(); continue; }
      ctx.strokeStyle = 'rgba(255,255,255,.4)';
      ctx.lineWidth   = 1; ctx.setLineDash([4,4]); ctx.globalAlpha = 0.75;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      if (candles[d.idx]) {
        const t = new Date(candles[d.idx].t * 1000);
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'center';
        ctx.fillStyle = 'rgba(255,255,255,.5)'; ctx.globalAlpha = 1; ctx.setLineDash([]);
        ctx.fillText(t.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',hour12:false}), x, 12);
      }
    }
    ctx.restore();
  }

  // In-progress drawing preview
  if (drawState && mX >= 0) {
    const { mn: mn2, mx: mx2 } = priceRange(start, start + Math.floor(w / cw));
    const y1 = py(drawState.p1, mn2, mx2);
    const x1 = (drawState.idx1 - start) * cw + candleW / 2;
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,.3)';
    ctx.lineWidth   = 1; ctx.setLineDash([4,4]);
    if (drawState.type === 'tline') {
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(mX, mY); ctx.stroke();
    } else if (drawState.type === 'rect') {
      const y2p = Math.min(y1, mY), y2h = Math.abs(mY - y1);
      const x2p = Math.min(x1, mX), x2w = Math.abs(mX - x1);
      ctx.strokeRect(x2p, y2p, x2w, y2h);
      ctx.fillStyle = 'rgba(255,255,255,.04)'; ctx.fillRect(x2p, y2p, x2w, y2h);
    }
    ctx.restore();
  }
}

// Right-click to delete nearest drawing
cv.addEventListener('contextmenu', e => {
  e.preventDefault();
  if (!drawings.length) return;
  if (drawState) { drawState = null; draw(); return; }
  const r  = cv.getBoundingClientRect();
  const sy = cv.height / r.height;
  const y  = (e.clientY - r.top) * sy;
  const { start } = view();
  const { mn, mx } = priceRange(start, start + Math.floor((cv.width - AX_W) / (candleW + 2)));
  const h  = cv.height - AX_H;
  const price = mn + (1 - Math.max(0, Math.min(h, y)) / h) * (mx - mn);
  const pxPerPt = h / (mx - mn || 1);
  let minDist = Infinity, minIdx = -1;
  drawings.forEach((d, i) => {
    let dist = Infinity;
    if (d.type === 'hline') dist = Math.abs(d.price - price) * pxPerPt;
    else if (d.type === 'tline' || d.type === 'rect') dist = 12; // coarse hit
    if (dist < minDist) { minDist = dist; minIdx = i; }
  });
  if (minIdx >= 0 && minDist < 14) { drawings.splice(minIdx, 1); draw(); }
});


// Escape to cancel in-progress drawing
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (drawState) { drawState = null; draw(); }
    return;
  }
  if (e.key === 'h' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT') setDrawTool('hline');
  if (e.key === 't' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT') setDrawTool('tline');
  if (e.key === 'r' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT') setDrawTool('rect');
  if (e.key === 'v' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT') setDrawTool('vline');
});


// ── Right Panel Tabs (Activity / History) ─────────────────────
let _histLoaded = false;

function switchRpTab(tab) {
  const actBtn  = document.getElementById('tabActivity');
  const histBtn = document.getElementById('tabHistory');
  const logBody  = document.getElementById('logBody');
  const histBody = document.getElementById('historyBody');
  const clrBtn   = document.getElementById('logClrBtn');
  if (tab === 'activity') {
    actBtn.classList.add('active');   histBtn.classList.remove('active');
    logBody.style.display  = '';      histBody.style.display = 'none';
    if (clrBtn) clrBtn.style.display = '';
  } else {
    histBtn.classList.add('active');  actBtn.classList.remove('active');
    histBody.style.display = '';      logBody.style.display  = 'none';
    if (clrBtn) clrBtn.style.display = 'none';
    if (!_histLoaded) _loadHistory();
  }
}

async function _loadHistory() {
  const body = document.getElementById('historyBody');
  body.innerHTML = '<div class="hist-loading">Loading trades…</div>';
  try {
    const data = await fetch('/api/trade_history').then(r => r.json());
    _histLoaded = true;
    if (!data.length) { body.innerHTML = '<div class="hist-empty">No trades logged yet.</div>'; return; }
    body.innerHTML = data.map(t => {
      const pnl     = parseFloat(t.pnl  || 0);
      const entry   = parseFloat(t.entry || 0);
      const exitP   = parseFloat(t.exit_price || 0);
      const cls     = pnl >= 0 ? 'pos' : 'neg';
      const sideCls = (t.side || '').toUpperCase() === 'CE' ? 'hist-side-ce' : 'hist-side-pe';
      return `<div class="hist-row">
        <div class="hist-top">
          <span class="hist-sym"><span class="${sideCls}">${t.side || '--'}</span> ${t.date || ''} ${t.time || ''}</span>
          <span class="hist-pnl ${cls}">${pnl >= 0 ? '+' : ''}₹${pnl.toFixed(2)}</span>
        </div>
        <div class="hist-meta">
          <span>Entry ₹${entry.toFixed(2)}</span>
          <span>→ ₹${exitP.toFixed(2)}</span>
          <span>${t.held_secs || '--'}s</span>
          <span class="hist-reason">${t.reason || '--'}</span>
        </div>
      </div>`;
    }).join('');
  } catch(err) {
    body.innerHTML = `<div class="hist-err">Error: ${err.message}</div>`;
  }
}

// Re-load history on new trade close
socket.on('trade_closed', () => { _histLoaded = false; });

// ── Stock chart in main chart area ───────────────────────────
async function openStockDetail(symbol) {
  _activeStockSym = symbol;

  // Show stock toggle buttons
  ['ctStockSep','ctSS','ctSC','ctSP','ctSX'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = '';
  });
  // Label the buttons (filled with real names after fetch)
  const ssBtn = document.getElementById('ctSS');
  const scBtn = document.getElementById('ctSC');
  const spBtn = document.getElementById('ctSP');
  if (ssBtn) ssBtn.textContent = symbol;
  if (scBtn) scBtn.textContent = 'CE…';
  if (spBtn) spBtn.textContent = 'PE…';

  // Clear all history arrays, auto-switch to 1-min TF (matches historical candle resolution)
  stockSpotHist = []; stockCeHist = []; stockPeHist = [];
  stockSpotCandles = []; stockCeCandles = []; stockPeCandles = [];

  // Switch to 1m TF for historical + live continuity, then show spot
  setTF(60, 'tf60');
  setChart('stock_spot');

  try {
    const data = await fetch('/api/stock_detail/' + encodeURIComponent(symbol)).then(r => r.json());

    // Pre-built historical candles from Zerodha API
    stockSpotCandles = data.spot_candles || [];
    stockCeCandles   = data.ce_candles   || [];
    stockPeCandles   = data.pe_candles   || [];

    // Live ticks accumulated by the bot since it started
    stockSpotHist = data.spot_ticks || [];
    stockCeHist   = data.ce_ticks   || [];
    stockPeHist   = data.pe_ticks   || [];

    // Update button labels with real option symbols
    if (scBtn && data.ce_symbol) scBtn.textContent = data.ce_symbol.slice(-8);
    if (spBtn && data.pe_symbol) spBtn.textContent = data.pe_symbol.slice(-8);

    rebuildAndDraw();
  } catch(e) {
    console.error('Stock detail fetch failed:', e);
  }
}

function clearStockChart() {
  _activeStockSym = null;
  stockSpotHist = []; stockCeHist = []; stockPeHist = [];
  stockSpotCandles = []; stockCeCandles = []; stockPeCandles = [];
  ['ctStockSep','ctSS','ctSC','ctSP','ctSX'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  setChart('nifty');
}

function closeStockDetail() { clearStockChart(); }

// Live-update stock chart ticks from SocketIO state
function _sdLiveUpdate(multiStocks) {
  if (!_activeStockSym || !multiStocks) return;
  const d = multiStocks[_activeStockSym];
  if (!d) return;
  const now = Date.now() / 1000;
  if (d.price    != null) { stockSpotHist.push({t:now, v:d.price});    if(stockSpotHist.length>200000)stockSpotHist.shift(); }
  if (d.ce_price != null) { stockCeHist.push({t:now, v:d.ce_price});   if(stockCeHist.length>200000)stockCeHist.shift();   }
  if (d.pe_price != null) { stockPeHist.push({t:now, v:d.pe_price});   if(stockPeHist.length>200000)stockPeHist.shift();   }
  if (chartMode.startsWith('stock_')) rebuildAndDraw();
}

// ── MiniChart — self-contained candlestick chart for modal ────
class MiniChart {
  constructor(canvas) {
    this.cv      = canvas;
    this.ticks   = [];
    this.candles = [];
    this.TF      = 5;       // candle timeframe seconds
    this.candleW = 8;
    this.viewOff = 0;
    this.label   = '';
    this._mX = -1; this._mY = -1;
    this._drag = false; this._dX = 0; this._dOff = 0;
    this._bindEvents();
    new ResizeObserver(() => this._onResize()).observe(canvas.parentElement);
  }

  setData(ticks, label) {
    this.ticks = ticks || [];
    this.label = label || '';
    this.viewOff = 0;
    this._buildCandles();
    this._onResize();
  }

  addTick(t, v) {
    if (v == null) return;
    this.ticks.push({t, v});
    if (this.ticks.length > 3000) this.ticks.shift();
    this._buildCandles();
    this.draw();
  }

  setTF(tf) { this.TF = tf; this._buildCandles(); this.draw(); }

  _buildCandles() {
    const b = new Map();
    for (const {t, v} of this.ticks) {
      const k = Math.floor(t / this.TF) * this.TF;
      if (!b.has(k)) b.set(k, []);
      b.get(k).push(v);
    }
    this.candles = [...b.entries()].sort((a,c) => a[0]-c[0])
      .map(([t,a]) => ({t, o:a[0], h:Math.max(...a), l:Math.min(...a), c:a[a.length-1]}));
  }

  _onResize() {
    const wrap = this.cv.parentElement;
    if (!wrap) return;
    this.cv.width  = wrap.clientWidth  || 500;
    this.cv.height = wrap.clientHeight || 280;
    this.draw();
  }

  draw() {
    const cv = this.cv;
    if (!cv.width || !cv.height) return;
    const x  = cv.getContext('2d');
    const W  = cv.width, H = cv.height;
    const AW = 58, AH = 18;
    const UP = '#00e87a', DN = '#ff3d5c';

    x.clearRect(0,0,W,H);
    x.fillStyle = '#050810'; x.fillRect(0,0,W,H);

    if (!this.candles.length) {
      x.fillStyle = 'rgba(78,101,133,.3)';
      x.font = '11px IBM Plex Mono'; x.textAlign = 'center';
      x.fillText('Waiting for ticks…', W/2, H/2);
      return;
    }

    const cw  = this.candleW + 2;
    const vis = Math.max(1, Math.floor((W-AW)/cw));
    const end = Math.max(this.candles.length - this.viewOff, 0);
    const st  = Math.max(end - vis, 0);
    const sl  = this.candles.slice(st, end);

    let mn = Infinity, mx = -Infinity;
    for (const c of sl) { mn = Math.min(mn,c.l); mx = Math.max(mx,c.h); }
    const pad = Math.max((mx-mn)*0.08, 1);
    mn -= pad; mx += pad;
    const rng = mx - mn || 1;
    const py = v => (H-AH)*(1-(v-mn)/rng);

    // Grid
    for (let i=0; i<=4; i++) {
      const v = mn+rng*(i/4), y = py(v);
      x.strokeStyle='rgba(28,36,56,.9)'; x.lineWidth=1;
      x.beginPath(); x.moveTo(0,y); x.lineTo(W-AW,y); x.stroke();
      x.fillStyle='rgba(78,101,133,.5)';
      x.font='8px IBM Plex Mono'; x.textAlign='right';
      x.fillText(v.toFixed(1), W-3, y+3);
    }

    // Time axis
    const every = Math.max(1, Math.floor(60/this.TF));
    x.font='8px IBM Plex Mono'; x.textAlign='center';
    for (let i=st; i<end; i++) {
      if ((i-st)%every!==0) continue;
      const cx = (i-st)*cw + this.candleW/2;
      if (cx > W-AW) break;
      x.strokeStyle='rgba(28,36,56,.6)'; x.lineWidth=1;
      x.beginPath(); x.moveTo(cx,0); x.lineTo(cx,H-AH); x.stroke();
      const d = new Date(this.candles[i].t*1000);
      x.fillStyle='rgba(78,101,133,.38)';
      x.fillText(d.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',hour12:false}),cx,H-4);
    }

    // Candles
    for (let i=st; i<end; i++) {
      const c = this.candles[i];
      const cx = (i-st)*cw;
      const up = c.c>=c.o, col = up?UP:DN, cx2 = cx+this.candleW/2;
      const bT = py(Math.max(c.o,c.c)), bB = py(Math.min(c.o,c.c));
      const bH = Math.max(bB-bT, 1);
      x.strokeStyle=col; x.lineWidth=1;
      x.beginPath(); x.moveTo(cx2,py(c.h)); x.lineTo(cx2,bT);
      x.moveTo(cx2,bB); x.lineTo(cx2,py(c.l)); x.stroke();
      const isLast = i===end-1;
      if (isLast) { x.shadowColor=col; x.shadowBlur=8; }
      x.fillStyle=col; x.globalAlpha=0.9;
      x.fillRect(cx+1,bT,this.candleW-2,bH);
      x.globalAlpha=1; x.shadowBlur=0;
    }

    // Last price dashed line + tag
    const lc = this.candles[end-1];
    if (lc) {
      const lp=lc.c, ly=py(lp);
      x.save();
      x.strokeStyle='rgba(0,212,255,.4)'; x.lineWidth=1; x.setLineDash([2,4]);
      x.beginPath(); x.moveTo(0,ly); x.lineTo(W-AW,ly); x.stroke();
      x.restore();
      x.fillStyle='rgba(0,212,255,.9)';
      x.fillRect(W-AW+1,ly-8,AW-2,16);
      x.fillStyle='#030709'; x.font='bold 8px IBM Plex Mono'; x.textAlign='center';
      x.fillText(lp.toFixed(1), W-AW+1+(AW-2)/2, ly+3);
    }

    // Label + OHLC
    const ohlcEl = document.getElementById('sdOhlc');
    if (lc && ohlcEl) {
      const up = lc.c>=lc.o;
      ohlcEl.innerHTML =
        `<span style="color:var(--text3);font-size:9px">${this.label}</span> ` +
        `O <b>${lc.o.toFixed(2)}</b> ` +
        `<span style="color:${UP}">H <b>${lc.h.toFixed(2)}</b></span> ` +
        `<span style="color:${DN}">L <b>${lc.l.toFixed(2)}</b></span> ` +
        `<span style="color:${up?UP:DN}">C <b>${lc.c.toFixed(2)}</b></span>`;
    }

    // Crosshair
    if (this._mX>=0 && this._mX<W-AW) {
      x.save();
      x.strokeStyle='rgba(78,101,133,.3)'; x.lineWidth=1; x.setLineDash([4,4]);
      x.beginPath(); x.moveTo(this._mX,0); x.lineTo(this._mX,H-AH);
      x.moveTo(0,this._mY); x.lineTo(W-AW,this._mY); x.stroke();
      const price = mn+(1-this._mY/(H-AH))*rng;
      x.fillStyle='rgba(77,142,255,.9)'; x.fillRect(W-AW,this._mY-8,AW,16);
      x.fillStyle='#fff'; x.font='8px IBM Plex Mono'; x.textAlign='center';
      x.fillText(price.toFixed(2),W-AW+AW/2,this._mY+3);
      x.restore();
      // Hovered candle OHLC
      const idx = st + Math.floor(this._mX/cw);
      const hc = this.candles[idx];
      if (hc && ohlcEl) {
        const up2 = hc.c>=hc.o;
        ohlcEl.innerHTML =
          `<span style="color:var(--text3);font-size:9px">${new Date(hc.t*1000).toLocaleTimeString()}</span> ` +
          `O <b>${hc.o.toFixed(2)}</b> ` +
          `<span style="color:${UP}">H <b>${hc.h.toFixed(2)}</b></span> ` +
          `<span style="color:${DN}">L <b>${hc.l.toFixed(2)}</b></span> ` +
          `<span style="color:${up2?UP:DN}">C <b>${hc.c.toFixed(2)}</b></span>`;
      }
    }
  }

  _bindEvents() {
    const cv = this.cv;
    cv.addEventListener('mousemove', e => {
      const r=cv.getBoundingClientRect(), sx=cv.width/r.width, sy=cv.height/r.height;
      if (this._drag) this.viewOff = Math.max(0, this._dOff - Math.round((e.clientX-this._dX)/(this.candleW+2)));
      this._mX=(e.clientX-r.left)*sx; this._mY=(e.clientY-r.top)*sy; this.draw();
    });
    cv.addEventListener('mouseleave', () => { this._mX=-1; this._mY=-1; this._drag=false; this.draw(); });
    cv.addEventListener('mousedown', e => { this._drag=true; this._dX=e.clientX; this._dOff=this.viewOff; cv.style.cursor='grabbing'; });
    cv.addEventListener('mouseup', () => { this._drag=false; cv.style.cursor='crosshair'; });
    cv.addEventListener('wheel', e => {
      e.preventDefault();
      this.candleW=Math.max(4,Math.min(32,this.candleW-Math.sign(e.deltaY)*1.5));
      this.draw();
    }, {passive:false});
  }
}

// Init
loadWatchlist();
requestAnimationFrame(resize);
