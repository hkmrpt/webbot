// ── Canvas ────────────────────────────────────────────────────
const cv   = document.getElementById('cv');
const ctx  = cv.getContext('2d');
const wrap = document.getElementById('chartWrap');
const UP = '#2ecc71', DN = '#e74c3c';
const AX_W = 60, AX_H = 20;

let TF = 5, chartMode = 'nifty';
let chartType = 'candle';  // 'candle' | 'ha' | 'line' | 'area'
// ── IQ Option-style Indicator System ─────────────────────────
const IND_CATALOG = [
  // Trend
  { id: 'ema',   name: 'EMA',   cat: 'trend', params: { period: 9, color: '#00d2ff' } },
  { id: 'sma',   name: 'SMA',   cat: 'trend', params: { period: 20, color: '#e8b730' } },
  { id: 'wma',   name: 'WMA',   cat: 'trend', params: { period: 14, color: '#76ff03' } },
  { id: 'dema',  name: 'DEMA',  cat: 'trend', params: { period: 21, color: '#ff6e40' } },
  { id: 'tema',  name: 'TEMA',  cat: 'trend', params: { period: 21, color: '#ea80fc' } },
  { id: 'psar',  name: 'Parabolic SAR', cat: 'trend', params: { step: 0.02, max: 0.2, color: '#ffeb3b' } },
  { id: 'ichimoku', name: 'Ichimoku Cloud', cat: 'trend', params: { tenkan: 9, kijun: 26, senkou: 52, color: '#26a69a' } },
  // Oscillators
  { id: 'rsi',   name: 'RSI',   cat: 'osc', params: { period: 14, color: '#e040fb' }, sub: true },
  { id: 'stoch', name: 'Stochastic', cat: 'osc', params: { k: 14, d: 3, smooth: 3, color: '#00bcd4' }, sub: true },
  { id: 'macd',  name: 'MACD',  cat: 'osc', params: { fast: 12, slow: 26, signal: 9, color: '#2196f3' }, sub: true },
  { id: 'cci',   name: 'CCI',   cat: 'osc', params: { period: 20, color: '#ff7043' }, sub: true },
  { id: 'williams', name: 'Williams %R', cat: 'osc', params: { period: 14, color: '#ab47bc' }, sub: true },
  { id: 'mfi',   name: 'MFI',   cat: 'osc', params: { period: 14, color: '#66bb6a' }, sub: true },
  // Volatility
  { id: 'bb',    name: 'Bollinger Bands', cat: 'vol', params: { period: 20, mult: 2, color: '#8c64dc' } },
  { id: 'atr',   name: 'ATR',   cat: 'vol', params: { period: 14, color: '#ff9800' }, sub: true },
  { id: 'kc',    name: 'Keltner Channel', cat: 'vol', params: { period: 20, mult: 1.5, color: '#4dd0e1' } },
  { id: 'dc',    name: 'Donchian Channel', cat: 'vol', params: { period: 20, color: '#8d6e63' } },
  // Volume
  { id: 'vol',   name: 'Volume', cat: 'volume', params: {} },
  { id: 'vwap',  name: 'VWAP',  cat: 'volume', params: { color: '#ffffff' } },
  { id: 'obv',   name: 'OBV',   cat: 'volume', params: { color: '#42a5f5' }, sub: true },
  { id: 'cmf',   name: 'CMF',   cat: 'volume', params: { period: 20, color: '#26c6da' }, sub: true },
];
let activeIndicators = [];
let _indIdCounter = 0;

// Legacy compat shim for priceRange check
const indicators = {
  get bb() {
    const bbInd = activeIndicators.find(i => i.catalogId === 'bb');
    return { on: !!bbInd, period: bbInd ? bbInd.params.period : 20, mult: bbInd ? bbInd.params.mult : 2 };
  }
};
let _mainH = 0;            // main chart height, set at top of draw()
let niftyHist = [], optHist = [];
let niftyHistCandles = [];  // Pre-built OHLC from Zerodha historical API
let _niftyHistLoaded = false;
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

// ── Watchlist (stubbed — NIFTY-only mode) ─────────────────────
let _watchlistStocks = [];
let _wlSearchTimer   = null;
let _selectedStocks  = new Set();

function loadWatchlist() { /* no-op: NIFTY-only mode */ }
function renderWatchlist() { /* no-op */ }

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
let _balanceFetched = false;
function applyMode(mode) {
  const changed = mode !== currentMode;
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
  document.getElementById('orderIdRow').classList.toggle('show', isReal);
  // Adopt toggle — only visible in real mode
  const adoptRow = document.getElementById('adoptRow');
  const adoptStatus = document.getElementById('adoptStatus');
  if (adoptRow) adoptRow.style.display = isReal ? 'flex' : 'none';
  if (!isReal) {
    if (adoptStatus) adoptStatus.style.display = 'none';
    const adoptToggle = document.getElementById('adoptToggle');
    if (adoptToggle && adoptToggle.checked) { adoptToggle.checked = false; toggleAdoptPositions(false); }
  }
  // Positions/Orders tabs — only useful in real mode
  const posTab = document.getElementById('rpTabPositions');
  const ordTab = document.getElementById('rpTabOrders');
  if (posTab) posTab.style.display = isReal ? '' : 'none';
  if (ordTab) ordTab.style.display = isReal ? '' : 'none';
  // If on a hidden tab, switch to trade tab
  if (!isReal && (document.getElementById('rpPanelPositions')?.style.display !== 'none' ||
                  document.getElementById('rpPanelOrders')?.style.display !== 'none')) {
    switchRpPanel('trade');
  }
  // Only fetch balance once per mode switch (not on every state broadcast)
  if (isReal && !_balanceFetched) {
    _balanceFetched = true;
    fetchRealBalance();
  }
  if (changed && !isReal) {
    _balanceFetched = false;
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
    const bal = +data.available;
    _lastCapital = bal;
    if (capEl) capEl.textContent = '₹' + bal.toLocaleString('en-IN', { maximumFractionDigits: 0 });
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
    _loadNiftyHistorical();
  }
}

async function _loadNiftyHistorical() {
  const interval = _tfToZerodhaInterval(TF);
  try {
    const data = await fetch('/api/nifty_historical?interval=' + interval).then(r => r.json());
    if (!data.error && Array.isArray(data)) {
      niftyHistCandles = data;
      _niftyHistLoaded = true;
    }
  } catch(e) {
    console.error('NIFTY historical load failed:', e);
  }
  rebuildAndDraw();
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
    candles = pre.map(c => ({ t: c.t, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v || 0 }));

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

  // Nifty / Option chart
  if (chartMode === 'nifty' && niftyHistCandles.length) {
    // Start with historical candles from Zerodha API
    candles = niftyHistCandles.map(c => ({ t: c.t, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v || 0 }));
    // Append live ticks after the last historical candle
    if (niftyHist.length) {
      const lastHistT = candles.length ? candles[candles.length - 1].t : 0;
      const liveBuckets = new Map();
      for (const { t, v } of niftyHist) {
        if (t <= lastHistT) continue;
        const k = Math.floor(t / TF) * TF;
        if (!liveBuckets.has(k)) liveBuckets.set(k, []);
        liveBuckets.get(k).push(v);
      }
      for (const [t, a] of [...liveBuckets.entries()].sort((x, y) => x[0] - y[0])) {
        candles.push({ t, o: a[0], h: Math.max(...a), l: Math.min(...a), c: a[a.length - 1] });
      }
    }
    return;
  }

  // Fallback: pure tick-based candles (option chart or no historical data)
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
  // Include Bollinger bands in range when enabled
  const _bbShim = indicators.bb;
  if (_bbShim.on) {
    const closes = sl.map(c => c.c);
    const bb = _computeBB(closes, _bbShim.period, _bbShim.mult);
    for (let i = 0; i < bb.upper.length; i++) {
      if (bb.upper[i] !== null) { mn = Math.min(mn, bb.lower[i]); mx = Math.max(mx, bb.upper[i]); }
    }
  }
  const rawRange = mx - mn || 1, thr = rawRange * 2;
  const isStock = chartMode.startsWith('stock_');
  const levels = chartMode === 'nifty' ? [lvl.ref] : isStock ? [] : [lvl.entry, lvl.sl, lvl.trail];
  for (const v of levels) {
    if (v > 0 && Math.abs(v - (mn+mx)/2) < thr) { mn = Math.min(mn, v); mx = Math.max(mx, v); }
  }
  const pad = Math.max((mx - mn) * 0.08, 2);
  return { mn: mn - pad, mx: mx + pad };
}
function getMainH() {
  const subCount = activeIndicators.filter(i => {
    const cat = IND_CATALOG.find(c => c.id === i.catalogId);
    return cat && cat.sub;
  }).length;
  const volActive = activeIndicators.some(i => i.catalogId === 'vol');
  const subPanels = subCount + (volActive ? 1 : 0);
  if (subPanels === 0) return Math.max(80, cv.height - AX_H);
  const subH = Math.min(subPanels * 60, Math.floor((cv.height - AX_H) * 0.4));
  return Math.max(80, cv.height - AX_H - subH);
}
function getVolH() { return Math.max(20, Math.floor((cv.height - AX_H) * 0.18)); }
function _getSubPanelLayout() {
  const subs = [];
  if (activeIndicators.some(i => i.catalogId === 'vol')) subs.push('vol');
  for (const ind of activeIndicators) {
    const cat = IND_CATALOG.find(c => c.id === ind.catalogId);
    if (cat && cat.sub) subs.push(ind.catalogId + '-' + ind.iid);
  }
  if (!subs.length) return [];
  const totalH = cv.height - AX_H - _mainH;
  const each = Math.floor(totalH / subs.length);
  let y = _mainH;
  return subs.map(id => {
    const layout = { id, top: y, height: each };
    y += each;
    return layout;
  });
}
function py(p, mn, mx) { return _mainH * (1 - (p - mn) / (mx - mn)); }
function _fmtPrice(v) {
  const s = v.toFixed(1);
  if (Math.abs(v) >= 1000) {
    const parts = s.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
  }
  return s;
}
function drawGrid(mn, mx) {
  const h = _mainH, w = cv.width - AX_W;
  const range = mx - mn || 1, raw = range / 5;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const nice = Math.ceil(raw / mag) * mag;
  const sv = Math.ceil(mn / nice) * nice;
  // Ultra-subtle horizontal grid only
  ctx.strokeStyle = 'rgba(255,255,255,0.035)';
  ctx.lineWidth = 0.5;
  for (let v = sv; v <= mx + nice; v += nice) {
    if (v < mn) continue;
    const y = py(v, mn, mx);
    if (y < 0 || y > h) continue;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  // Right-side price axis labels
  ctx.font = '10px IBM Plex Mono'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  ctx.fillStyle = 'rgba(255,255,255,0.3)';
  for (let v = sv; v <= mx + nice; v += nice) {
    if (v < mn) continue;
    const y = py(v, mn, mx);
    if (y < 4 || y > h - 4) continue;
    ctx.fillText(_fmtPrice(v), cv.width - 6, y);
  }
  // Axis separator line
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(w, 0); ctx.lineTo(w, h); ctx.stroke();
  // Time labels — no vertical lines
  const { start: vs } = view();
  const cw = candleW + 2;
  const minLabelPx = 60;
  const every = Math.max(1, Math.ceil(minLabelPx / cw));
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  ctx.fillStyle = 'rgba(255,255,255,0.22)';
  ctx.font = '9px IBM Plex Mono';
  for (let i = vs; i < candles.length; i++) {
    if ((i - vs) % every !== 0) continue;
    const x = (i - vs) * cw + candleW / 2;
    if (x > w - 20) break;
    const d = new Date(candles[i].t * 1000);
    ctx.fillText(d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false }), x, _mainH + 4);
  }
  ctx.textBaseline = 'alphabetic';
}
function drawCandle(c, x, mn, mx, isLast) {
  const up = c.c >= c.o;
  const color = up ? '#2ecc71' : '#e74c3c';
  const cx2 = x + candleW / 2;
  const bTop = py(Math.max(c.o, c.c), mn, mx);
  const bBot = py(Math.min(c.o, c.c), mn, mx);
  const bH = Math.max(bBot - bTop, 1);
  const gap = Math.max(1, Math.floor(candleW * 0.15));
  const bx = x + gap;
  const bw = candleW - gap * 2;
  // Wick
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx2, py(c.h, mn, mx));
  ctx.lineTo(cx2, bTop);
  ctx.moveTo(cx2, bBot);
  ctx.lineTo(cx2, py(c.l, mn, mx));
  ctx.stroke();
  // Body — solid filled with subtle gradient
  if (bH >= 3 && bw >= 3) {
    const grad = ctx.createLinearGradient(0, bTop, 0, bBot);
    if (up) {
      grad.addColorStop(0, '#3ae882');
      grad.addColorStop(1, '#25b65e');
    } else {
      grad.addColorStop(0, '#f25a4a');
      grad.addColorStop(1, '#c0392b');
    }
    ctx.fillStyle = grad;
    // Rounded rect
    const r = Math.min(2, bw / 3, bH / 3);
    ctx.beginPath();
    ctx.moveTo(bx + r, bTop);
    ctx.lineTo(bx + bw - r, bTop);
    ctx.quadraticCurveTo(bx + bw, bTop, bx + bw, bTop + r);
    ctx.lineTo(bx + bw, bBot - r);
    ctx.quadraticCurveTo(bx + bw, bBot, bx + bw - r, bBot);
    ctx.lineTo(bx + r, bBot);
    ctx.quadraticCurveTo(bx, bBot, bx, bBot - r);
    ctx.lineTo(bx, bTop + r);
    ctx.quadraticCurveTo(bx, bTop, bx + r, bTop);
    ctx.closePath();
    ctx.fill();
  } else {
    ctx.fillStyle = color;
    ctx.fillRect(bx, bTop, bw, Math.max(bH, 1));
  }
}
function drawLevel(price, color, dash, mn, mx) {
  if (!price || price <= 0) return;
  const y = py(price, mn, mx), w = cv.width - AX_W;
  if (y < 0 || y > _mainH) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.55;
  ctx.setLineDash(dash);
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
  // Thin solid crosshair lines
  ctx.strokeStyle = 'rgba(255,255,255,0.12)';
  ctx.lineWidth = 0.5;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(mX, 0); ctx.lineTo(mX, h);
  ctx.moveTo(0, mY); ctx.lineTo(w, mY);
  ctx.stroke();
  // Price label — dark pill on right axis
  const price = mn + (1 - mY / h) * (mx - mn);
  const plblW = AX_W - 4;
  const plblH = 18;
  const plblX = w + 2;
  const plblY = mY - plblH / 2;
  ctx.fillStyle = 'rgba(40,45,70,0.92)';
  ctx.beginPath();
  ctx.roundRect(plblX, plblY, plblW, plblH, 4);
  ctx.fill();
  ctx.fillStyle = '#fff';
  ctx.font = '9px IBM Plex Mono';
  ctx.textAlign = 'center';
  ctx.fillText(price.toFixed(2), w + AX_W / 2, mY + 3);
  // Time label — dark pill at bottom
  const { start } = view();
  const idx = start + Math.floor(mX / (candleW + 2));
  if (candles[idx]) {
    const d = new Date(candles[idx].t * 1000);
    const timeStr = d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const tw = ctx.measureText(timeStr).width + 12;
    ctx.fillStyle = 'rgba(40,45,70,0.92)';
    ctx.beginPath();
    ctx.roundRect(mX - tw / 2, _mainH + 1, tw, 16, 4);
    ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.font = '9px IBM Plex Mono';
    ctx.textAlign = 'center';
    ctx.fillText(timeStr, mX, _mainH + 12);
  }
  ctx.restore();
  // Tooltip — dark floating card
  const tip = document.getElementById('xTip');
  if (candles[idx]) {
    const c = candles[idx], up = c.c >= c.o;
    tip.style.display = 'block';
    tip.innerHTML =
      `<span style="color:rgba(255,255,255,.4);font-size:8px">${new Date(c.t * 1000).toLocaleTimeString()}</span><br>` +
      `<span style="color:rgba(255,255,255,.5)">O</span> <b>${c.o.toFixed(2)}</b>  ` +
      `<span style="color:#2ecc71">H</span> <b>${c.h.toFixed(2)}</b>  ` +
      `<span style="color:#e74c3c">L</span> <b>${c.l.toFixed(2)}</b>  ` +
      `<span style="color:${up ? '#2ecc71' : '#e74c3c'}">C</span> <b>${c.c.toFixed(2)}</b>`;
    let tx = mX + 14, ty = mY - 40;
    if (tx + 260 > cv.width) tx = mX - 270;
    if (ty < 0) ty = mY + 10;
    tip.style.left = tx + 'px';
    tip.style.top = ty + 'px';
  } else {
    tip.style.display = 'none';
  }
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
  ctx.fillStyle = '#0b0e18';
  ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = 'rgba(255,255,255,0.08)';
  ctx.font = '13px Space Grotesk';
  ctx.textAlign = 'center';
  ctx.fillText('Start the robot to see live chart', cv.width / 2, cv.height / 2 - 6);
  ctx.fillStyle = 'rgba(255,255,255,0.04)';
  ctx.font = '10px IBM Plex Mono';
  ctx.fillText('Scroll to zoom \u00b7 Drag to pan', cv.width / 2, cv.height / 2 + 16);
}
// ── Heikin-Ashi transform ─────────────────────────────────────
function toHeikinAshi(src) {
  const ha = [];
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    const haC = (c.o + c.h + c.l + c.c) / 4;
    const haO = i === 0 ? (c.o + c.c) / 2 : (ha[i-1].o + ha[i-1].c) / 2;
    ha.push({ t: c.t, o: haO, h: Math.max(c.h, haO, haC), l: Math.min(c.l, haO, haC), c: haC, v: c.v || 0 });
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

// ── IQ Option-style Indicator Functions ──────────────────────
function openIndPanel() {
  const panel = document.getElementById('indPanel');
  panel.classList.toggle('open');
  const btn = document.getElementById('indPanelBtn');
  if (btn) btn.classList.toggle('active', panel.classList.contains('open'));
  _updateIndPanelActive();
}

function _updateIndPanelActive() {
  document.querySelectorAll('.ind-cat-item').forEach(item => {
    item.classList.remove('active-ind');
  });
  for (const ind of activeIndicators) {
    const items = document.querySelectorAll('.ind-cat-item');
    items.forEach(item => {
      const onclick = item.getAttribute('onclick') || '';
      if (onclick.includes(`'${ind.catalogId}'`)) {
        item.classList.add('active-ind');
      }
    });
  }
  // Update toolbar button dot
  const btn = document.getElementById('indPanelBtn');
  if (btn) btn.classList.toggle('has-active', activeIndicators.length > 0);
}

function addIndicator(catalogId) {
  const cat = IND_CATALOG.find(c => c.id === catalogId);
  if (!cat) return;
  // Non-stackable: vol, vwap, rsi, stoch, atr, bb — only one instance
  const nonStackable = ['vol', 'vwap', 'rsi', 'stoch', 'atr', 'bb', 'macd', 'cci', 'williams', 'mfi', 'obv', 'cmf', 'psar', 'ichimoku', 'kc', 'dc'];
  if (nonStackable.includes(catalogId)) {
    const existing = activeIndicators.find(i => i.catalogId === catalogId);
    if (existing) return; // already active
  }
  const instance = {
    iid: ++_indIdCounter,
    catalogId: catalogId,
    name: cat.name,
    sub: !!cat.sub,
    params: JSON.parse(JSON.stringify(cat.params)),
  };
  activeIndicators.push(instance);
  renderIndPills();
  _updateIndPanelActive();
  draw();
}

function removeActiveInd(instanceId) {
  activeIndicators = activeIndicators.filter(i => i.iid !== instanceId);
  renderIndPills();
  _updateIndPanelActive();
  draw();
}

function openIndSettings(instanceId) {
  // Close all other popups first
  document.querySelectorAll('.ind-pill-popup.show').forEach(p => p.classList.remove('show'));
  const popup = document.getElementById('indPop-' + instanceId);
  if (popup) popup.classList.toggle('show');
}

function updateActiveIndParam(instanceId, param, value) {
  const ind = activeIndicators.find(i => i.iid === instanceId);
  if (!ind) return;
  if (param === 'color') {
    ind.params[param] = value;
  } else {
    const v = parseFloat(value);
    if (!isNaN(v) && v > 0) ind.params[param] = v;
  }
  renderIndPills();
  draw();
}

function renderIndPills() {
  const container = document.getElementById('indPills');
  if (!container) return;
  container.innerHTML = '';
  for (const ind of activeIndicators) {
    const color = ind.params.color || '#4caf50';
    const pill = document.createElement('div');
    pill.className = 'ind-pill';
    pill.dataset.iid = ind.iid;
    pill.style.borderColor = color + '44';

    // Build name label
    let nameStr = ind.name;
    if (ind.catalogId === 'ema' || ind.catalogId === 'sma' || ind.catalogId === 'wma' || ind.catalogId === 'dema' || ind.catalogId === 'tema') nameStr += ' ' + ind.params.period;
    else if (ind.catalogId === 'bb' || ind.catalogId === 'kc') nameStr = ind.name.split(' ')[0] + ' ' + ind.params.period + ',' + ind.params.mult;
    else if (ind.catalogId === 'rsi' || ind.catalogId === 'atr' || ind.catalogId === 'cci' || ind.catalogId === 'williams' || ind.catalogId === 'mfi' || ind.catalogId === 'cmf' || ind.catalogId === 'dc') nameStr += ' ' + ind.params.period;
    else if (ind.catalogId === 'stoch') nameStr = 'Stoch ' + ind.params.k;
    else if (ind.catalogId === 'macd') nameStr = 'MACD ' + ind.params.fast + ',' + ind.params.slow + ',' + ind.params.signal;
    else if (ind.catalogId === 'psar') nameStr = 'SAR ' + ind.params.step;
    else if (ind.catalogId === 'ichimoku') nameStr = 'Ichi ' + ind.params.tenkan + ',' + ind.params.kijun;

    pill.innerHTML =
      `<span class="ind-pill-dot" style="background:${color}"></span>` +
      `<span class="ind-pill-name">${nameStr}</span>` +
      `<span class="ind-pill-val" id="indPillVal-${ind.iid}"></span>` +
      (ind.catalogId !== 'vol' ?
        `<button class="ind-pill-settings" onclick="event.stopPropagation();openIndSettings(${ind.iid})">&#9881;</button>` : '') +
      `<button class="ind-pill-close" onclick="event.stopPropagation();removeActiveInd(${ind.iid})">&times;</button>`;

    // Build settings popup
    if (ind.catalogId !== 'vol') {
      const popup = document.createElement('div');
      popup.className = 'ind-pill-popup';
      popup.id = 'indPop-' + ind.iid;
      let rows = '';
      if (ind.params.period !== undefined) {
        rows += `<div class="ind-pp-row"><label>Period</label><input type="number" value="${ind.params.period}" min="2" max="200" onchange="updateActiveIndParam(${ind.iid},'period',this.value)"></div>`;
      }
      if (ind.params.k !== undefined) {
        rows += `<div class="ind-pp-row"><label>%K</label><input type="number" value="${ind.params.k}" min="2" max="100" onchange="updateActiveIndParam(${ind.iid},'k',this.value)"></div>`;
        rows += `<div class="ind-pp-row"><label>%D</label><input type="number" value="${ind.params.d}" min="1" max="50" onchange="updateActiveIndParam(${ind.iid},'d',this.value)"></div>`;
        rows += `<div class="ind-pp-row"><label>Smooth</label><input type="number" value="${ind.params.smooth}" min="1" max="50" onchange="updateActiveIndParam(${ind.iid},'smooth',this.value)"></div>`;
      }
      if (ind.params.mult !== undefined) {
        rows += `<div class="ind-pp-row"><label>Std Dev</label><input type="number" value="${ind.params.mult}" min="0.5" max="5" step="0.5" onchange="updateActiveIndParam(${ind.iid},'mult',this.value)"></div>`;
      }
      if (ind.params.color !== undefined) {
        rows += `<div class="ind-pp-row"><label>Color</label><input type="color" value="${ind.params.color}" onchange="updateActiveIndParam(${ind.iid},'color',this.value)"></div>`;
      }
      popup.innerHTML = rows;
      pill.appendChild(popup);
    }

    container.appendChild(pill);
  }
}

// Close popups and panel on outside click
document.addEventListener('click', e => {
  // Close pill popups
  if (!e.target.closest('.ind-pill') && !e.target.closest('.ind-pill-popup')) {
    document.querySelectorAll('.ind-pill-popup.show').forEach(p => p.classList.remove('show'));
  }
  // Close indicator panel when clicking outside
  if (!e.target.closest('.ind-panel') && !e.target.closest('.dt-ind-btn')) {
    const panel = document.getElementById('indPanel');
    if (panel && panel.classList.contains('open')) {
      panel.classList.remove('open');
      const btn = document.getElementById('indPanelBtn');
      if (btn) btn.classList.remove('active');
    }
  }
});

function _drawLineChart(src, start, end, cw, mn, mx) {
  ctx.save();
  ctx.strokeStyle = '#2ecc71';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.setLineDash([]);
  ctx.beginPath();
  let started = false;
  for (let i = start; i < end; i++) {
    const x = (i - start) * cw + candleW / 2;
    const y = py(src[i].c, mn, mx);
    if (!started) { ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.restore();
}
function _drawAreaChart(src, start, end, cw, mn, mx) {
  ctx.save();
  const baseY = _mainH;
  // Gradient fill
  const grad = ctx.createLinearGradient(0, 0, 0, baseY);
  grad.addColorStop(0, 'rgba(46,204,113,0.30)');
  grad.addColorStop(0.5, 'rgba(46,204,113,0.08)');
  grad.addColorStop(1, 'rgba(46,204,113,0.0)');
  ctx.beginPath();
  let started = false;
  let firstX = 0;
  for (let i = start; i < end; i++) {
    const x = (i - start) * cw + candleW / 2;
    const y = py(src[i].c, mn, mx);
    if (!started) { firstX = x; ctx.moveTo(x, baseY); ctx.lineTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  }
  const lastX = (end - 1 - start) * cw + candleW / 2;
  ctx.lineTo(lastX, baseY);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  // Top edge line
  ctx.strokeStyle = '#2ecc71';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.setLineDash([]);
  ctx.beginPath();
  started = false;
  for (let i = start; i < end; i++) {
    const x = (i - start) * cw + candleW / 2;
    const y = py(src[i].c, mn, mx);
    if (!started) { ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.restore();
}
// ── Technical indicator helpers ───────────────────────────────
function _computeEMA(data, period) {
  const k = 2 / (period + 1);
  const ema = [];
  let sum = 0;
  for (let i = 0; i < data.length; i++) {
    if (i < period) {
      sum += data[i];
      ema.push(i === period - 1 ? sum / period : null);
    } else {
      ema.push(data[i] * k + ema[i-1] * (1 - k));
    }
  }
  return ema;
}
function _computeBB(closes, period, mult) {
  const mid = [], upper = [], lower = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) { mid.push(null); upper.push(null); lower.push(null); continue; }
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += closes[j];
    const avg = sum / period;
    let sqSum = 0;
    for (let j = i - period + 1; j <= i; j++) sqSum += (closes[j] - avg) ** 2;
    const std = Math.sqrt(sqSum / period);
    mid.push(avg); upper.push(avg + mult * std); lower.push(avg - mult * std);
  }
  return { mid, upper, lower };
}
function _computeVWAP(src) {
  const vwap = [];
  let cumTPV = 0, cumVol = 0;
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (!c.v) { vwap.push(null); continue; }
    const tp = (c.h + c.l + c.c) / 3;
    cumTPV += tp * c.v; cumVol += c.v;
    vwap.push(cumVol > 0 ? cumTPV / cumVol : null);
  }
  return vwap;
}

function _computeSMA(data, period) {
  const result = new Array(data.length).fill(null);
  for (let i = period - 1; i < data.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += data[j];
    result[i] = sum / period;
  }
  return result;
}

function _computeRSI(data, period) {
  const result = new Array(data.length).fill(null);
  if (data.length < period + 1) return result;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = data[i] - data[i-1];
    if (d > 0) avgGain += d; else avgLoss -= d;
  }
  avgGain /= period; avgLoss /= period;
  result[period] = avgLoss === 0 ? 100 : 100 - 100/(1 + avgGain/avgLoss);
  for (let i = period + 1; i < data.length; i++) {
    const d = data[i] - data[i-1];
    avgGain = (avgGain * (period-1) + (d > 0 ? d : 0)) / period;
    avgLoss = (avgLoss * (period-1) + (d < 0 ? -d : 0)) / period;
    result[i] = avgLoss === 0 ? 100 : 100 - 100/(1 + avgGain/avgLoss);
  }
  return result;
}

function _computeStochastic(cndls, kPeriod, dPeriod, smooth) {
  const len = cndls.length;
  const rawK = new Array(len).fill(null);
  for (let i = kPeriod - 1; i < len; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - kPeriod + 1; j <= i; j++) {
      hh = Math.max(hh, cndls[j].h);
      ll = Math.min(ll, cndls[j].l);
    }
    rawK[i] = hh === ll ? 50 : ((cndls[i].c - ll) / (hh - ll)) * 100;
  }
  // Smooth %K
  const kLine = _computeSMA(rawK.map(v => v === null ? 0 : v), smooth);
  for (let i = 0; i < kPeriod - 1 + smooth - 1; i++) kLine[i] = null;
  // %D = SMA of %K
  const dLine = _computeSMA(kLine.map(v => v === null ? 0 : v), dPeriod);
  for (let i = 0; i < kPeriod - 1 + smooth - 1 + dPeriod - 1; i++) dLine[i] = null;
  return { k: kLine, d: dLine };
}

function _computeATR(cndls, period) {
  const result = new Array(cndls.length).fill(null);
  if (cndls.length < 2) return result;
  const trs = [cndls[0].h - cndls[0].l];
  for (let i = 1; i < cndls.length; i++) {
    const c = cndls[i], pc = cndls[i-1].c;
    trs.push(Math.max(c.h - c.l, Math.abs(c.h - pc), Math.abs(c.l - pc)));
  }
  if (trs.length < period) return result;
  let sum = 0;
  for (let i = 0; i < period; i++) sum += trs[i];
  result[period - 1] = sum / period;
  for (let i = period; i < trs.length; i++) {
    result[i] = (result[i-1] * (period - 1) + trs[i]) / period;
  }
  return result;
}

function _computeWMA(data, period) {
  const r = new Array(data.length).fill(null);
  const denom = period * (period + 1) / 2;
  for (let i = period - 1; i < data.length; i++) {
    let s = 0;
    for (let j = 0; j < period; j++) s += data[i - period + 1 + j] * (j + 1);
    r[i] = s / denom;
  }
  return r;
}

function _computeDEMA(data, period) {
  const ema1 = _computeEMA(data, period);
  const ema1clean = ema1.map(v => v === null ? 0 : v);
  const ema2 = _computeEMA(ema1clean, period);
  return data.map((_, i) => (ema1[i] === null || ema2[i] === null) ? null : 2 * ema1[i] - ema2[i]);
}

function _computeTEMA(data, period) {
  const ema1 = _computeEMA(data, period);
  const ema1c = ema1.map(v => v === null ? 0 : v);
  const ema2 = _computeEMA(ema1c, period);
  const ema2c = ema2.map(v => v === null ? 0 : v);
  const ema3 = _computeEMA(ema2c, period);
  return data.map((_, i) => (ema1[i] === null || ema2[i] === null || ema3[i] === null) ? null : 3 * ema1[i] - 3 * ema2[i] + ema3[i]);
}

function _computeMACD(data, fast, slow, signal) {
  const emaF = _computeEMA(data, fast);
  const emaS = _computeEMA(data, slow);
  const macdLine = data.map((_, i) => (emaF[i] === null || emaS[i] === null) ? null : emaF[i] - emaS[i]);
  const macdClean = macdLine.map(v => v === null ? 0 : v);
  const sigLine = _computeEMA(macdClean, signal);
  for (let i = 0; i < slow - 1; i++) sigLine[i] = null;
  const hist = data.map((_, i) => (macdLine[i] === null || sigLine[i] === null) ? null : macdLine[i] - sigLine[i]);
  return { macd: macdLine, signal: sigLine, histogram: hist };
}

function _computeCCI(cndls, period) {
  const r = new Array(cndls.length).fill(null);
  const tp = cndls.map(c => (c.h + c.l + c.c) / 3);
  for (let i = period - 1; i < cndls.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += tp[j];
    const mean = sum / period;
    let md = 0;
    for (let j = i - period + 1; j <= i; j++) md += Math.abs(tp[j] - mean);
    md /= period;
    r[i] = md === 0 ? 0 : (tp[i] - mean) / (0.015 * md);
  }
  return r;
}

function _computeWilliamsR(cndls, period) {
  const r = new Array(cndls.length).fill(null);
  for (let i = period - 1; i < cndls.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) { hh = Math.max(hh, cndls[j].h); ll = Math.min(ll, cndls[j].l); }
    r[i] = hh === ll ? -50 : ((hh - cndls[i].c) / (hh - ll)) * -100;
  }
  return r;
}

function _computeMFI(cndls, period) {
  const r = new Array(cndls.length).fill(null);
  if (cndls.length < period + 1) return r;
  const tp = cndls.map(c => (c.h + c.l + c.c) / 3);
  const mf = cndls.map((c, i) => tp[i] * (c.v || 1));
  for (let i = period; i < cndls.length; i++) {
    let posF = 0, negF = 0;
    for (let j = i - period + 1; j <= i; j++) {
      if (tp[j] > tp[j - 1]) posF += mf[j];
      else negF += mf[j];
    }
    r[i] = negF === 0 ? 100 : 100 - 100 / (1 + posF / negF);
  }
  return r;
}

function _computeOBV(cndls) {
  const r = new Array(cndls.length).fill(null);
  if (!cndls.length) return r;
  r[0] = cndls[0].v || 0;
  for (let i = 1; i < cndls.length; i++) {
    const vol = cndls[i].v || 0;
    if (cndls[i].c > cndls[i - 1].c) r[i] = r[i - 1] + vol;
    else if (cndls[i].c < cndls[i - 1].c) r[i] = r[i - 1] - vol;
    else r[i] = r[i - 1];
  }
  return r;
}

function _computeCMF(cndls, period) {
  const r = new Array(cndls.length).fill(null);
  for (let i = period - 1; i < cndls.length; i++) {
    let mfvSum = 0, volSum = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const hl = cndls[j].h - cndls[j].l;
      const clv = hl === 0 ? 0 : ((cndls[j].c - cndls[j].l) - (cndls[j].h - cndls[j].c)) / hl;
      const vol = cndls[j].v || 0;
      mfvSum += clv * vol;
      volSum += vol;
    }
    r[i] = volSum === 0 ? 0 : mfvSum / volSum;
  }
  return r;
}

function _computePSAR(cndls, step, max) {
  const r = new Array(cndls.length).fill(null);
  if (cndls.length < 2) return r;
  let bull = cndls[1].c > cndls[0].c;
  let af = step;
  let ep = bull ? cndls[0].h : cndls[0].l;
  let sar = bull ? cndls[0].l : cndls[0].h;
  r[0] = sar;
  for (let i = 1; i < cndls.length; i++) {
    const prev = sar;
    sar = prev + af * (ep - prev);
    if (bull) {
      if (i >= 2) sar = Math.min(sar, cndls[i - 1].l, cndls[i - 2].l);
      if (cndls[i].l < sar) { bull = false; sar = ep; ep = cndls[i].l; af = step; }
      else { if (cndls[i].h > ep) { ep = cndls[i].h; af = Math.min(af + step, max); } }
    } else {
      if (i >= 2) sar = Math.max(sar, cndls[i - 1].h, cndls[i - 2].h);
      if (cndls[i].h > sar) { bull = true; sar = ep; ep = cndls[i].h; af = step; }
      else { if (cndls[i].l < ep) { ep = cndls[i].l; af = Math.min(af + step, max); } }
    }
    r[i] = { val: sar, bull };
  }
  return r;
}

function _computeIchimoku(cndls, tenkan, kijun, senkou) {
  const len = cndls.length;
  const hl = (s, e) => {
    let h = -Infinity, l = Infinity;
    for (let i = s; i <= e; i++) { h = Math.max(h, cndls[i].h); l = Math.min(l, cndls[i].l); }
    return (h + l) / 2;
  };
  const tenkanSen = new Array(len).fill(null);
  const kijunSen = new Array(len).fill(null);
  const senkouA = new Array(len + kijun).fill(null);
  const senkouB = new Array(len + kijun).fill(null);
  for (let i = tenkan - 1; i < len; i++) tenkanSen[i] = hl(i - tenkan + 1, i);
  for (let i = kijun - 1; i < len; i++) kijunSen[i] = hl(i - kijun + 1, i);
  for (let i = kijun - 1; i < len; i++) {
    if (tenkanSen[i] !== null && kijunSen[i] !== null) senkouA[i + kijun] = (tenkanSen[i] + kijunSen[i]) / 2;
  }
  for (let i = senkou - 1; i < len; i++) senkouB[i + kijun] = hl(i - senkou + 1, i);
  return { tenkan: tenkanSen, kijun: kijunSen, senkouA: senkouA.slice(0, len), senkouB: senkouB.slice(0, len) };
}

function _computeKeltner(cndls, period, mult) {
  const closes = cndls.map(c => c.c);
  const ema = _computeEMA(closes, period);
  const atr = _computeATR(cndls, period);
  const upper = cndls.map((_, i) => (ema[i] === null || atr[i] === null) ? null : ema[i] + mult * atr[i]);
  const lower = cndls.map((_, i) => (ema[i] === null || atr[i] === null) ? null : ema[i] - mult * atr[i]);
  return { mid: ema, upper, lower };
}

function _computeDonchian(cndls, period) {
  const upper = new Array(cndls.length).fill(null);
  const lower = new Array(cndls.length).fill(null);
  const mid = new Array(cndls.length).fill(null);
  for (let i = period - 1; i < cndls.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) { hh = Math.max(hh, cndls[j].h); ll = Math.min(ll, cndls[j].l); }
    upper[i] = hh; lower[i] = ll; mid[i] = (hh + ll) / 2;
  }
  return { upper, lower, mid };
}

function _drawVolumePanel(src, start, end, cw) {
  const volH = getVolH();
  const volTop = _mainH + Math.floor((cv.height - AX_H) * 0.04);
  const w = cv.width - AX_W;
  // Separator line
  ctx.save();
  ctx.strokeStyle = 'rgba(28,36,56,.8)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, volTop - 2); ctx.lineTo(w, volTop - 2); ctx.stroke();
  // Find max volume in visible range
  let maxVol = 0;
  for (let i = start; i < end; i++) {
    if (src[i] && src[i].v) maxVol = Math.max(maxVol, src[i].v);
  }
  if (maxVol === 0) { ctx.restore(); return; }
  // Draw volume bars
  ctx.globalAlpha = 0.4;
  for (let i = start; i < end; i++) {
    const c = src[i];
    if (!c || !c.v) continue;
    const x = (i - start) * cw;
    const barH = (c.v / maxVol) * volH;
    const up = c.c >= c.o;
    ctx.fillStyle = up ? UP : DN;
    ctx.fillRect(x + 1, volTop + volH - barH, candleW - 2, barH);
  }
  ctx.globalAlpha = 1;
  // Volume axis labels (right side)
  ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'right'; ctx.fillStyle = 'rgba(78,101,133,.4)';
  const fmtVol = v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'K' : v.toString();
  ctx.fillText(fmtVol(maxVol), cv.width - 5, volTop + 8);
  ctx.fillText('0', cv.width - 5, volTop + volH - 2);
  ctx.restore();
}

// ── Main draw ─────────────────────────────────────────────────
function draw() {
  if (!cv.width || !cv.height) return;
  _mainH = getMainH();
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#0b0e18'; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!candles.length) { drawEmpty(); return; }

  const dispCandles = chartType === 'ha' ? toHeikinAshi(candles) : candles;
  const { start, end } = view();
  const { mn, mx }     = priceRange(start, end);
  drawGrid(mn, mx);

  const allCloses = candles.map(c => c.c);
  const cw = candleW + 2;

  // ── Draw overlay indicators (BB, EMA, SMA, VWAP — before candles for BB) ──
  for (const ind of activeIndicators) {
    if (ind.catalogId === 'bb') {
      const bb = _computeBB(allCloses, ind.params.period, ind.params.mult);
      ctx.save();
      ctx.beginPath();
      let bbStarted = false;
      for (let i = start; i < end; i++) {
        if (bb.upper[i] === null) continue;
        const x = (i - start) * cw + candleW / 2;
        if (!bbStarted) { ctx.moveTo(x, py(bb.upper[i], mn, mx)); bbStarted = true; }
        else ctx.lineTo(x, py(bb.upper[i], mn, mx));
      }
      for (let i = end - 1; i >= start; i--) {
        if (bb.lower[i] === null) continue;
        const x = (i - start) * cw + candleW / 2;
        ctx.lineTo(x, py(bb.lower[i], mn, mx));
      }
      ctx.closePath();
      ctx.fillStyle = (ind.params.color || '#8c64dc') + '0f'; ctx.fill();
      ctx.strokeStyle = (ind.params.color || '#8c64dc') + '59'; ctx.lineWidth = 1; ctx.setLineDash([3,4]);
      ctx.beginPath(); bbStarted = false;
      for (let i = start; i < end; i++) {
        if (bb.mid[i] === null) continue;
        const x = (i - start) * cw + candleW / 2, y = py(bb.mid[i], mn, mx);
        if (!bbStarted) { ctx.moveTo(x, y); bbStarted = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.strokeStyle = (ind.params.color || '#8c64dc') + '40'; ctx.lineWidth = 1; ctx.setLineDash([]);
      for (const band of [bb.upper, bb.lower]) {
        ctx.beginPath(); let started = false;
        for (let i = start; i < end; i++) {
          if (band[i] === null) continue;
          const x = (i - start) * cw + candleW / 2, y = py(band[i], mn, mx);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
      ctx.restore();
      // Update pill value
      const bbValEl = document.getElementById('indPillVal-' + ind.iid);
      if (bbValEl && bb.mid[end-1] !== null) bbValEl.textContent = bb.mid[end-1].toFixed(1);
    }
  }

  // Level overlays
  if (chartMode.startsWith('stock_')) {
    // no level overlays
  } else if (chartMode === 'nifty') {
    drawLevel(lvl.ref, 'rgba(176,106,255,.8)', [8,4], mn, mx);
  } else {
    drawLevel(lvl.entry, 'rgba(0,212,255,.85)',  [8,4], mn, mx);
    drawLevel(lvl.sl,    'rgba(255,61,92,.85)',  [4,4], mn, mx);
    drawLevel(lvl.trail, 'rgba(255,176,32,.85)', [6,3], mn, mx);
  }

  // Main chart (candles/line/area)
  if (chartType === 'line') {
    _drawLineChart(dispCandles, start, end, cw, mn, mx);
  } else if (chartType === 'area') {
    _drawAreaChart(dispCandles, start, end, cw, mn, mx);
  } else {
    for (let i = start; i < end; i++) drawCandle(dispCandles[i], (i-start)*cw, mn, mx, i===candles.length-1);
  }

  // ── Draw EMA / SMA overlays ────────────────────────────────
  const emaInds = activeIndicators.filter(i => i.catalogId === 'ema');
  const smaInds = activeIndicators.filter(i => i.catalogId === 'sma');
  if (emaInds.length || smaInds.length) {
    ctx.save(); ctx.setLineDash([]);
    const computed = [];
    for (const ind of emaInds) {
      const data = _computeEMA(allCloses, ind.params.period);
      computed.push({ ind, data });
      ctx.strokeStyle = ind.params.color + 'b3'; ctx.lineWidth = 1;
      ctx.beginPath(); let s = false;
      for (let i = start; i < end; i++) {
        if (data[i] === null) continue;
        const x = (i - start) * cw + candleW / 2, y = py(data[i], mn, mx);
        if (!s) { ctx.moveTo(x, y); s = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
      const valEl = document.getElementById('indPillVal-' + ind.iid);
      if (valEl && data[end-1] !== null) valEl.textContent = _fmtPrice(data[end-1]);
    }
    for (const ind of smaInds) {
      const data = _computeSMA(allCloses, ind.params.period);
      computed.push({ ind, data });
      ctx.strokeStyle = ind.params.color + 'b3'; ctx.lineWidth = 1;
      ctx.beginPath(); let s = false;
      for (let i = start; i < end; i++) {
        if (data[i] === null) continue;
        const x = (i - start) * cw + candleW / 2, y = py(data[i], mn, mx);
        if (!s) { ctx.moveTo(x, y); s = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
      const valEl = document.getElementById('indPillVal-' + ind.iid);
      if (valEl && data[end-1] !== null) valEl.textContent = _fmtPrice(data[end-1]);
    }
    // Crossover dots (when exactly 2 EMA instances)
    if (emaInds.length === 2) {
      const d1 = computed.find(c => c.ind.iid === emaInds[0].iid).data;
      const d2 = computed.find(c => c.ind.iid === emaInds[1].iid).data;
      for (let i = start; i < end; i++) {
        if (i < 1 || d1[i] === null || d2[i] === null || d1[i-1] === null || d2[i-1] === null) continue;
        const prevAbove = d1[i-1] > d2[i-1];
        const currAbove = d1[i] > d2[i];
        if (prevAbove !== currAbove) {
          const x = (i - start) * cw + candleW / 2;
          const y = py((d1[i] + d2[i]) / 2, mn, mx);
          ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2);
          ctx.fillStyle = currAbove ? UP : DN; ctx.fill();
          ctx.strokeStyle = '#fff'; ctx.lineWidth = 0.8; ctx.stroke();
        }
      }
    }
    ctx.restore();
  }

  // ── VWAP overlay ───────────────────────────────────────────
  for (const ind of activeIndicators) {
    if (ind.catalogId !== 'vwap') continue;
    const vwap = _computeVWAP(candles);
    const hasVwap = vwap.some(v => v !== null);
    if (hasVwap) {
      ctx.save();
      ctx.strokeStyle = (ind.params.color || '#ffffff') + '8c'; ctx.lineWidth = 1.5; ctx.setLineDash([4,3]);
      ctx.beginPath(); let vwapStarted = false;
      for (let i = start; i < end; i++) {
        if (vwap[i] === null) continue;
        const x = (i - start) * cw + candleW / 2, y = py(vwap[i], mn, mx);
        if (!vwapStarted) { ctx.moveTo(x, y); vwapStarted = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke(); ctx.restore();
      const valEl = document.getElementById('indPillVal-' + ind.iid);
      if (valEl && vwap[end-1] !== null) valEl.textContent = _fmtPrice(vwap[end-1]);
    }
  }

  // ── WMA / DEMA / TEMA overlays ────────────────────────────────
  for (const ind of activeIndicators) {
    let data = null;
    if (ind.catalogId === 'wma')  data = _computeWMA(allCloses, ind.params.period);
    if (ind.catalogId === 'dema') data = _computeDEMA(allCloses, ind.params.period);
    if (ind.catalogId === 'tema') data = _computeTEMA(allCloses, ind.params.period);
    if (!data) continue;
    ctx.save(); ctx.strokeStyle = ind.params.color + 'b3'; ctx.lineWidth = 1.2; ctx.setLineDash([]);
    ctx.beginPath(); let s = false;
    for (let i = start; i < end; i++) {
      if (data[i] === null) continue;
      const x = (i - start) * cw + candleW / 2, y = py(data[i], mn, mx);
      if (!s) { ctx.moveTo(x, y); s = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke(); ctx.restore();
    const ve = document.getElementById('indPillVal-' + ind.iid);
    if (ve && data[end-1] !== null) ve.textContent = _fmtPrice(data[end-1]);
  }

  // ── Parabolic SAR overlay ─────────────────────────────────────
  for (const ind of activeIndicators) {
    if (ind.catalogId !== 'psar') continue;
    const psar = _computePSAR(candles, ind.params.step, ind.params.max);
    ctx.save();
    for (let i = start; i < end; i++) {
      if (!psar[i] || typeof psar[i] !== 'object') continue;
      const x = (i - start) * cw + candleW / 2;
      const y = py(psar[i].val, mn, mx);
      ctx.fillStyle = psar[i].bull ? '#2ecc71' : '#e74c3c';
      ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
    const ve = document.getElementById('indPillVal-' + ind.iid);
    if (ve && psar[end-1] && typeof psar[end-1] === 'object') ve.textContent = _fmtPrice(psar[end-1].val);
  }

  // ── Ichimoku Cloud overlay ────────────────────────────────────
  for (const ind of activeIndicators) {
    if (ind.catalogId !== 'ichimoku') continue;
    const ich = _computeIchimoku(candles, ind.params.tenkan, ind.params.kijun, ind.params.senkou);
    ctx.save();
    // Cloud fill
    ctx.globalAlpha = 0.08;
    ctx.beginPath(); let cs = false;
    for (let i = start; i < end; i++) {
      if (ich.senkouA[i] === null || ich.senkouB[i] === null) continue;
      const x = (i - start) * cw + candleW / 2;
      if (!cs) { ctx.moveTo(x, py(ich.senkouA[i], mn, mx)); cs = true; }
      else ctx.lineTo(x, py(ich.senkouA[i], mn, mx));
    }
    for (let i = end - 1; i >= start; i--) {
      if (ich.senkouB[i] === null) continue;
      ctx.lineTo((i - start) * cw + candleW / 2, py(ich.senkouB[i], mn, mx));
    }
    ctx.closePath(); ctx.fillStyle = ind.params.color; ctx.fill();
    ctx.globalAlpha = 1;
    // Lines
    const drawLine = (arr, clr) => { ctx.strokeStyle = clr + '99'; ctx.lineWidth = 1; ctx.beginPath(); let s = false; for (let i = start; i < end; i++) { if (arr[i] === null) continue; const x = (i-start)*cw+candleW/2, y = py(arr[i],mn,mx); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); } ctx.stroke(); };
    drawLine(ich.tenkan, '#e74c3c');
    drawLine(ich.kijun, '#2980b9');
    drawLine(ich.senkouA, '#2ecc71');
    drawLine(ich.senkouB, '#e74c3c');
    ctx.restore();
    const ve = document.getElementById('indPillVal-' + ind.iid);
    if (ve && ich.tenkan[end-1] !== null) ve.textContent = _fmtPrice(ich.tenkan[end-1]);
  }

  // ── Keltner Channel overlay ───────────────────────────────────
  for (const ind of activeIndicators) {
    if (ind.catalogId !== 'kc') continue;
    const kc = _computeKeltner(candles, ind.params.period, ind.params.mult);
    ctx.save();
    // Fill between bands
    ctx.beginPath(); let ks = false;
    for (let i = start; i < end; i++) { if (kc.upper[i] === null) continue; const x=(i-start)*cw+candleW/2; if (!ks){ctx.moveTo(x,py(kc.upper[i],mn,mx));ks=true;}else ctx.lineTo(x,py(kc.upper[i],mn,mx)); }
    for (let i = end-1; i >= start; i--) { if (kc.lower[i] === null) continue; ctx.lineTo((i-start)*cw+candleW/2, py(kc.lower[i],mn,mx)); }
    ctx.closePath(); ctx.fillStyle = ind.params.color + '0d'; ctx.fill();
    // Lines
    ctx.setLineDash([]); ctx.lineWidth = 1;
    for (const arr of [kc.upper, kc.mid, kc.lower]) {
      ctx.strokeStyle = ind.params.color + (arr === kc.mid ? '66' : '40'); ctx.setLineDash(arr === kc.mid ? [3,4] : []);
      ctx.beginPath(); let s = false;
      for (let i = start; i < end; i++) { if (arr[i] === null) continue; const x=(i-start)*cw+candleW/2, y=py(arr[i],mn,mx); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
      ctx.stroke();
    }
    ctx.restore();
    const ve = document.getElementById('indPillVal-' + ind.iid);
    if (ve && kc.mid[end-1] !== null) ve.textContent = _fmtPrice(kc.mid[end-1]);
  }

  // ── Donchian Channel overlay ──────────────────────────────────
  for (const ind of activeIndicators) {
    if (ind.catalogId !== 'dc') continue;
    const dc = _computeDonchian(candles, ind.params.period);
    ctx.save();
    ctx.beginPath(); let ds = false;
    for (let i = start; i < end; i++) { if (dc.upper[i] === null) continue; const x=(i-start)*cw+candleW/2; if (!ds){ctx.moveTo(x,py(dc.upper[i],mn,mx));ds=true;}else ctx.lineTo(x,py(dc.upper[i],mn,mx)); }
    for (let i = end-1; i >= start; i--) { if (dc.lower[i] === null) continue; ctx.lineTo((i-start)*cw+candleW/2, py(dc.lower[i],mn,mx)); }
    ctx.closePath(); ctx.fillStyle = ind.params.color + '0d'; ctx.fill();
    ctx.setLineDash([]); ctx.lineWidth = 1;
    for (const arr of [dc.upper, dc.mid, dc.lower]) {
      ctx.strokeStyle = ind.params.color + (arr === dc.mid ? '55' : '40');
      ctx.setLineDash(arr === dc.mid ? [3,3] : []);
      ctx.beginPath(); let s = false;
      for (let i = start; i < end; i++) { if (arr[i] === null) continue; const x=(i-start)*cw+candleW/2, y=py(arr[i],mn,mx); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
      ctx.stroke();
    }
    ctx.restore();
    const ve = document.getElementById('indPillVal-' + ind.iid);
    if (ve && dc.mid[end-1] !== null) ve.textContent = _fmtPrice(dc.mid[end-1]);
  }

  drawMarkers(start, mn, mx);
  if (lastP) {
    const y = py(lastP, mn, mx), w = cv.width - AX_W;
    // Thin dotted last price line
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.1)';
    ctx.lineWidth = 0.5;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    ctx.restore();
    // Price tag on right axis
    const up = candles.length > 1 && candles[candles.length - 1].c >= candles[candles.length - 1].o;
    const tagColor = up ? '#2ecc71' : '#e74c3c';
    const tagW = AX_W - 2;
    const tagH = 20;
    const tagX = w + 1;
    const tagY = y - tagH / 2;
    ctx.fillStyle = tagColor;
    ctx.beginPath();
    ctx.roundRect(tagX, tagY, tagW, tagH, 3);
    ctx.fill();
    // Small arrow pointing left
    ctx.beginPath();
    ctx.moveTo(tagX, y);
    ctx.lineTo(tagX - 4, y - 4);
    ctx.lineTo(tagX - 4, y + 4);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 10px IBM Plex Mono';
    ctx.textAlign = 'center';
    ctx.fillText(lastP.toFixed(2), w + AX_W / 2, y + 3.5);
  }

  // ── Sub-panel indicators (Volume, RSI, Stochastic, ATR) ────
  const subLayout = _getSubPanelLayout();
  for (const layout of subLayout) {
    const w = cv.width - AX_W;
    // Separator line
    ctx.save();
    ctx.strokeStyle = 'rgba(28,36,56,.8)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, layout.top - 1); ctx.lineTo(w, layout.top - 1); ctx.stroke();
    ctx.restore();

    if (layout.id === 'vol') {
      // Volume panel
      let maxVol = 0;
      for (let i = start; i < end; i++) {
        if (candles[i] && candles[i].v) maxVol = Math.max(maxVol, candles[i].v);
      }
      if (maxVol > 0) {
        ctx.save(); ctx.globalAlpha = 0.4;
        for (let i = start; i < end; i++) {
          const c = candles[i];
          if (!c || !c.v) continue;
          const x = (i - start) * cw;
          const barH = (c.v / maxVol) * layout.height;
          const up = c.c >= c.o;
          ctx.fillStyle = up ? UP : DN;
          ctx.fillRect(x + 1, layout.top + layout.height - barH, candleW - 2, barH);
        }
        ctx.globalAlpha = 1;
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'right'; ctx.fillStyle = 'rgba(78,101,133,.4)';
        const fmtVol = v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'K' : v.toString();
        ctx.fillText(fmtVol(maxVol), cv.width - 5, layout.top + 8);
        ctx.fillText('0', cv.width - 5, layout.top + layout.height - 2);
        // Label
        ctx.fillStyle = 'rgba(78,101,133,.3)'; ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left';
        ctx.fillText('VOL', 4, layout.top + 10);
        ctx.restore();
      }
    } else {
      // RSI / Stochastic / ATR sub-panels
      const parts = layout.id.split('-');
      const catId = parts[0];
      const iid = parseInt(parts[1]);
      const ind = activeIndicators.find(i => i.iid === iid);
      if (!ind) continue;

      if (catId === 'rsi') {
        const rsi = _computeRSI(allCloses, ind.params.period);
        const subMn = 0, subMx = 100;
        const subPy = (v) => layout.top + layout.height * (1 - (v - subMn) / (subMx - subMn));
        ctx.save();
        // OB/OS lines
        ctx.strokeStyle = 'rgba(78,101,133,.25)'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
        for (const lv of [30, 70]) {
          const y = subPy(lv);
          ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
        }
        // 50 line
        ctx.strokeStyle = 'rgba(78,101,133,.15)'; ctx.setLineDash([2,4]);
        ctx.beginPath(); ctx.moveTo(0, subPy(50)); ctx.lineTo(w, subPy(50)); ctx.stroke();
        // RSI line
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let started = false;
        for (let i = start; i < end; i++) {
          if (rsi[i] === null) continue;
          const x = (i - start) * cw + candleW / 2, y = subPy(rsi[i]);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        // Labels
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('RSI ' + ind.params.period, 4, layout.top + 10);
        ctx.textAlign = 'right'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('70', cv.width - 5, subPy(70) + 3);
        ctx.fillText('30', cv.width - 5, subPy(30) + 3);
        ctx.restore();
        // Update pill value
        const valEl = document.getElementById('indPillVal-' + ind.iid);
        if (valEl && rsi[end-1] !== null) valEl.textContent = rsi[end-1].toFixed(1);
      } else if (catId === 'stoch') {
        const stoch = _computeStochastic(candles, ind.params.k, ind.params.d, ind.params.smooth);
        const subMn = 0, subMx = 100;
        const subPy = (v) => layout.top + layout.height * (1 - (v - subMn) / (subMx - subMn));
        ctx.save();
        // OB/OS lines
        ctx.strokeStyle = 'rgba(78,101,133,.25)'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
        for (const lv of [20, 80]) {
          const y = subPy(lv);
          ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
        }
        // %K line
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s1 = false;
        for (let i = start; i < end; i++) {
          if (stoch.k[i] === null) continue;
          const x = (i - start) * cw + candleW / 2, y = subPy(stoch.k[i]);
          if (!s1) { ctx.moveTo(x, y); s1 = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        // %D line (signal)
        ctx.strokeStyle = '#ff9800cc'; ctx.lineWidth = 1; ctx.setLineDash([3,2]);
        ctx.beginPath(); let s2 = false;
        for (let i = start; i < end; i++) {
          if (stoch.d[i] === null) continue;
          const x = (i - start) * cw + candleW / 2, y = subPy(stoch.d[i]);
          if (!s2) { ctx.moveTo(x, y); s2 = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('STOCH ' + ind.params.k, 4, layout.top + 10);
        ctx.restore();
        const valEl = document.getElementById('indPillVal-' + ind.iid);
        if (valEl && stoch.k[end-1] !== null) valEl.textContent = stoch.k[end-1].toFixed(1);
      } else if (catId === 'atr') {
        const atrData = _computeATR(candles, ind.params.period);
        // Find range in visible area
        let atrMn = Infinity, atrMx = -Infinity;
        for (let i = start; i < end; i++) {
          if (atrData[i] !== null) { atrMn = Math.min(atrMn, atrData[i]); atrMx = Math.max(atrMx, atrData[i]); }
        }
        if (atrMn === Infinity) { atrMn = 0; atrMx = 1; }
        const pad = (atrMx - atrMn) * 0.1 || 0.5;
        atrMn -= pad; atrMx += pad;
        const subPy = (v) => layout.top + layout.height * (1 - (v - atrMn) / (atrMx - atrMn));
        ctx.save();
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let started = false;
        for (let i = start; i < end; i++) {
          if (atrData[i] === null) continue;
          const x = (i - start) * cw + candleW / 2, y = subPy(atrData[i]);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('ATR ' + ind.params.period, 4, layout.top + 10);
        ctx.textAlign = 'right';
        if (atrData[end-1] !== null) ctx.fillText(atrData[end-1].toFixed(2), cv.width - 5, layout.top + 10);
        ctx.restore();
        const valEl = document.getElementById('indPillVal-' + ind.iid);
        if (valEl && atrData[end-1] !== null) valEl.textContent = atrData[end-1].toFixed(2);

      } else if (catId === 'macd') {
        const macd = _computeMACD(allCloses, ind.params.fast, ind.params.slow, ind.params.signal);
        let mn2 = Infinity, mx2 = -Infinity;
        for (let i = start; i < end; i++) { for (const v of [macd.macd[i], macd.signal[i], macd.histogram[i]]) { if (v !== null) { mn2 = Math.min(mn2, v); mx2 = Math.max(mx2, v); } } }
        if (mn2 === Infinity) { mn2 = -1; mx2 = 1; }
        const pd = (mx2 - mn2) * 0.1 || 0.5; mn2 -= pd; mx2 += pd;
        const subPy = (v) => layout.top + layout.height * (1 - (v - mn2) / (mx2 - mn2));
        ctx.save();
        // Zero line
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 0.5; ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(0, subPy(0)); ctx.lineTo(w, subPy(0)); ctx.stroke();
        // Histogram bars
        ctx.globalAlpha = 0.5;
        for (let i = start; i < end; i++) { if (macd.histogram[i] === null) continue; const x = (i-start)*cw; const barH = Math.abs(subPy(macd.histogram[i]) - subPy(0)); ctx.fillStyle = macd.histogram[i] >= 0 ? UP : DN; const yt = macd.histogram[i] >= 0 ? subPy(macd.histogram[i]) : subPy(0); ctx.fillRect(x+1, yt, candleW-2, barH); }
        ctx.globalAlpha = 1;
        // MACD line
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s1 = false;
        for (let i = start; i < end; i++) { if (macd.macd[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(macd.macd[i]); if (!s1){ctx.moveTo(x,y);s1=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        // Signal line
        ctx.strokeStyle = '#ff5252aa'; ctx.lineWidth = 1;
        ctx.beginPath(); let s2 = false;
        for (let i = start; i < end; i++) { if (macd.signal[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(macd.signal[i]); if (!s2){ctx.moveTo(x,y);s2=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('MACD ' + ind.params.fast + ',' + ind.params.slow + ',' + ind.params.signal, 4, layout.top + 10);
        ctx.restore();
        const ve = document.getElementById('indPillVal-' + ind.iid);
        if (ve && macd.macd[end-1] !== null) ve.textContent = macd.macd[end-1].toFixed(2);

      } else if (catId === 'cci') {
        const cci = _computeCCI(candles, ind.params.period);
        const subPy = (v) => layout.top + layout.height * (1 - (v - (-200)) / 400);
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 0.5; ctx.setLineDash([3,3]);
        for (const lv of [-100, 0, 100]) { ctx.beginPath(); ctx.moveTo(0, subPy(lv)); ctx.lineTo(w, subPy(lv)); ctx.stroke(); }
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s = false;
        for (let i = start; i < end; i++) { if (cci[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(Math.max(-200, Math.min(200, cci[i]))); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('CCI ' + ind.params.period, 4, layout.top + 10);
        ctx.restore();
        const ve = document.getElementById('indPillVal-' + ind.iid);
        if (ve && cci[end-1] !== null) ve.textContent = cci[end-1].toFixed(1);

      } else if (catId === 'williams') {
        const wr = _computeWilliamsR(candles, ind.params.period);
        const subPy = (v) => layout.top + layout.height * (1 - (v - (-100)) / 100);
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 0.5; ctx.setLineDash([3,3]);
        for (const lv of [-80, -20]) { ctx.beginPath(); ctx.moveTo(0, subPy(lv)); ctx.lineTo(w, subPy(lv)); ctx.stroke(); }
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s = false;
        for (let i = start; i < end; i++) { if (wr[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(wr[i]); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('%R ' + ind.params.period, 4, layout.top + 10);
        ctx.textAlign = 'right'; ctx.fillText('-20', cv.width - 5, subPy(-20) + 3); ctx.fillText('-80', cv.width - 5, subPy(-80) + 3);
        ctx.restore();
        const ve = document.getElementById('indPillVal-' + ind.iid);
        if (ve && wr[end-1] !== null) ve.textContent = wr[end-1].toFixed(1);

      } else if (catId === 'mfi') {
        const mfi = _computeMFI(candles, ind.params.period);
        const subPy = (v) => layout.top + layout.height * (1 - v / 100);
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 0.5; ctx.setLineDash([3,3]);
        for (const lv of [20, 80]) { ctx.beginPath(); ctx.moveTo(0, subPy(lv)); ctx.lineTo(w, subPy(lv)); ctx.stroke(); }
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s = false;
        for (let i = start; i < end; i++) { if (mfi[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(mfi[i]); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('MFI ' + ind.params.period, 4, layout.top + 10);
        ctx.restore();
        const ve = document.getElementById('indPillVal-' + ind.iid);
        if (ve && mfi[end-1] !== null) ve.textContent = mfi[end-1].toFixed(1);

      } else if (catId === 'obv') {
        const obv = _computeOBV(candles);
        let mn2 = Infinity, mx2 = -Infinity;
        for (let i = start; i < end; i++) { if (obv[i] !== null) { mn2 = Math.min(mn2, obv[i]); mx2 = Math.max(mx2, obv[i]); } }
        if (mn2 === Infinity) { mn2 = 0; mx2 = 1; }
        const pd = (mx2 - mn2) * 0.1 || 1; mn2 -= pd; mx2 += pd;
        const subPy = (v) => layout.top + layout.height * (1 - (v - mn2) / (mx2 - mn2));
        ctx.save();
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s = false;
        for (let i = start; i < end; i++) { if (obv[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(obv[i]); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('OBV', 4, layout.top + 10);
        ctx.restore();
        const ve = document.getElementById('indPillVal-' + ind.iid);
        const fmtVol = v => Math.abs(v) >= 1e6 ? (v/1e6).toFixed(1)+'M' : Math.abs(v) >= 1e3 ? (v/1e3).toFixed(0)+'K' : v.toFixed(0);
        if (ve && obv[end-1] !== null) ve.textContent = fmtVol(obv[end-1]);

      } else if (catId === 'cmf') {
        const cmf = _computeCMF(candles, ind.params.period);
        const subPy = (v) => layout.top + layout.height * (1 - (v - (-0.5)) / 1);
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 0.5; ctx.setLineDash([3,3]);
        ctx.beginPath(); ctx.moveTo(0, subPy(0)); ctx.lineTo(w, subPy(0)); ctx.stroke();
        ctx.strokeStyle = ind.params.color + 'cc'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.beginPath(); let s = false;
        for (let i = start; i < end; i++) { if (cmf[i] === null) continue; const x = (i-start)*cw+candleW/2, y = subPy(cmf[i]); if (!s){ctx.moveTo(x,y);s=true;}else ctx.lineTo(x,y); }
        ctx.stroke();
        ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(78,101,133,.3)';
        ctx.fillText('CMF ' + ind.params.period, 4, layout.top + 10);
        ctx.restore();
        const ve = document.getElementById('indPillVal-' + ind.iid);
        if (ve && cmf[end-1] !== null) ve.textContent = cmf[end-1].toFixed(3);
      }
    }
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
socket.on('connect', () => {
  logInit = false; clearLogs();
  if (!_niftyHistLoaded) _loadNiftyHistorical();
});
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
  if (d.nifty_atm) updateAtmPanel(d.nifty_atm);
  if (d.ce_price != null) set('atmCePrice', '₹' + (d.ce_price || 0).toFixed(2));
  if (d.pe_price != null) set('atmPePrice', '₹' + (d.pe_price || 0).toFixed(2));
  if (d.index_name) {
    const lbl = document.getElementById('tkNLabel');
    if (lbl) lbl.textContent = d.index_name;
  }

  // CE / PE live prices (topbar only)
  if (d.ce_price != null) set('tkCE', '₹' + (+d.ce_price).toFixed(2));
  if (d.pe_price != null) set('tkPE', '₹' + (+d.pe_price).toFixed(2));

  // Order IDs
  if (d.last_order_id) {
    set('oidBuy', '▲ BUY: ' + d.last_order_id);
    document.getElementById('oidBuy').style.color = 'var(--green)';
  }
  if (d.target_order_id) {
    set('oidTarget', '🎯 TGT: ' + d.target_order_id);
  } else {
    set('oidTarget', '—');
  }
  // Show/hide target price row in trade card
  const tgtRow = document.getElementById('tcTargetRow');
  if (tgtRow) tgtRow.style.display = d.target_price ? 'flex' : 'none';
  if (d.target_price) set('tcTargetPrice', '₹' + d.target_price.toFixed(2));
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

  // ── RSI indicator ──────────────────────────────────────────────
  if (d.rsi != null) {
    const rsi = +d.rsi;
    const rsiEl = document.getElementById('tkRsi');
    if (rsiEl) {
      rsiEl.textContent = rsi.toFixed(1);
      rsiEl.style.color = rsi >= 70 ? 'var(--red)' : rsi <= 30 ? 'var(--green)' : 'var(--cyan)';
    }
    const fvRsi = document.getElementById('fvRsi');
    if (fvRsi) {
      fvRsi.textContent = rsi.toFixed(1);
      fvRsi.className = 'fval ' + (rsi >= 70 ? 'fv-warn' : rsi <= 30 ? 'fv-boost' : 'fv-ok');
    }
  }

  // ── Range position ─────────────────────────────────────────────
  if (d.range_position != null) {
    const rp = +d.range_position;
    const rpEl = document.getElementById('tkRangePos');
    if (rpEl) {
      rpEl.textContent = (rp * 100).toFixed(0) + '%';
      rpEl.style.color = rp >= 0.85 ? 'var(--green)' : rp <= 0.15 ? 'var(--red)' : 'var(--text2)';
    }
  }

  // ── Volatility detector (range spread) ─────────────────────────
  if (d.vol_detector != null) {
    const vd = d.vol_detector;
    const rsEl = document.getElementById('fvRangeSpread');
    if (rsEl) {
      if (vd.ready) {
        rsEl.textContent = vd.spread.toFixed(1) + ' pts' + (vd.ranging ? ' (ranging)' : '');
        rsEl.className = 'fval ' + (vd.ranging ? 'fv-ok' : 'fv-warn');
      } else {
        rsEl.textContent = vd.ticks + ' ticks (building)';
        rsEl.className = 'fval fv-off';
      }
    }
  }

  // ── New filter labels ──────────────────────────────────────────
  const fvOpt = document.getElementById('fvOptPrice');
  if (fvOpt) fvOpt.textContent = '₹8–₹700';
  const fvSpd = document.getElementById('fvSpikeSpeed');
  if (fvSpd) { fvSpd.textContent = 'ON'; fvSpd.className = 'fval fv-ok'; }
  const fvTrn = document.getElementById('fvTrend');
  if (fvTrn) { fvTrn.textContent = 'ON'; fvTrn.className = 'fval fv-ok'; }
  const fvBf = document.getElementById('fvBreakoutFilter');
  if (fvBf) { fvBf.textContent = 'ON'; fvBf.className = 'fval fv-ok'; }

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

  // Update right panel session stats
  const rpSess = document.getElementById('rpSessPnl');
  if (rpSess) {
    rpSess.textContent = (sess >= 0 ? '+' : '') + sess.toFixed(2);
    rpSess.style.color = sess >= 0 ? 'var(--green)' : 'var(--red)';
  }
  const rpTrades = document.getElementById('rpSessTrades');
  if (rpTrades) rpTrades.textContent = (d.wins||0) + 'W / ' + (d.losses||0) + 'L';

  // Trade budget display
  if (d.trade_budget) {
    const rpBudget = document.getElementById('rpBudget');
    if (rpBudget) {
      const b = d.trade_budget;
      rpBudget.textContent = b.remaining + ' left [' + b.confidence + ']';
      rpBudget.style.color = b.confidence === 'aggressive' ? 'var(--green)' : b.confidence === 'halted' ? 'var(--red)' : 'var(--text2)';
    }
  }

  // Market intelligence display
  if (d.market_intel) {
    const rpIntel = document.getElementById('rpIntel');
    if (rpIntel) {
      const mi = d.market_intel;
      const di = mi.day_info || {};
      rpIntel.textContent = (di.day_type || '?').toUpperCase() + ' DTE=' + (di.dte || '?');
    }
  }

  set('tradeCount', (d.trades_today||0) + ' trades · ' + (d.wins||0) + 'W / ' + (d.losses||0) + 'L');
  set('stTrades', d.trades_today || 0);
  const wr = d.trades_today ? Math.round(d.wins/d.trades_today*100)+'%' : '--';
  set('stWin', wr);
  if (d.capital) {
    _lastCapital = d.capital;
    set('capVal', '₹' + d.capital.toLocaleString('en-IN', { maximumFractionDigits: 0 }));
  }

  document.getElementById('exitBtn').classList.toggle('show', !!d.trade_open);
  updateTradeCard(d);
  _updateAdoptUI(d);

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

socket.on('nifty_atm_resolved', function(atm) {
  updateAtmPanel(atm);
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
  const _tgtRowC = document.getElementById('tcTargetRow'); if (_tgtRowC) _tgtRowC.style.display = 'none';
  set('oidTarget', '—');
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
    if (currentMode === 'real') {
      const ok = confirm(
        '🔴 Starting in REAL TRADING mode!\n\nNIFTY options will be auto-selected.\n\nLive BUY/SELL orders will be sent to Zerodha.\n\nConfirm to proceed.'
      );
      if (!ok) return;
    }

    // Reset local state
    markers=[]; lastP=null; logInit=false;
    lvl={ref:0,entry:0,sl:0,trail:0};
    bestTrade=null; worstTrade=null;
    niftyHist=[]; optHist=[]; candles=[];
    lastSkipReason=null;
    set('oidBuy','—'); set('oidTarget','—'); set('oidSell','—');
    const _tgtR = document.getElementById('tcTargetRow'); if (_tgtR) _tgtR.style.display = 'none';
    ['tcEntry','tcCurrent','tcSL','tcTrail','tcTargetPrice','tcPeak','tcPeakPct','tcPeakP','tcQty','tcHeld','tcLivePL','tcAtrTrail','tcMinTrail']
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

    socket.emit('start_robot', {});
    isRunning = true; updateBtn();
  }
}

function refreshAtm() {
  socket.emit('refresh_nifty_atm');
  const btn = document.getElementById('atmRefreshBtn');
  if (btn) { btn.textContent = '⟳ Resolving…'; btn.disabled = true; }
}

function updateAtmPanel(atm) {
  if (!atm) return;
  set('atmExpiry',  atm.expiry  || '—');
  set('atmStrike',  atm.strike  ? atm.strike.toLocaleString('en-IN') : '—');
  set('atmCeSymbol', atm.ce_symbol || '—');
  set('atmPeSymbol', atm.pe_symbol || '—');
  const atmStatus = document.getElementById('atmStatus');
  if (atmStatus) atmStatus.textContent = '✅ ATM resolved — watching CE & PE';
  const btn = document.getElementById('atmRefreshBtn');
  if (btn) { btn.textContent = '↺ Refresh ATM'; btn.disabled = false; }
}


function manualExit() {
  if (!confirm('Exit trade now at market price?')) return;
  socket.emit('manual_exit_trade');
  document.getElementById('exitBtn').classList.remove('show');
}

// ── Adopt Open Positions Toggle ──────────────────────────────
let _adoptEnabled = false;
let _adoptPolling = null;

function toggleAdoptPositions(enabled) {
  _adoptEnabled = enabled;
  const status = document.getElementById('adoptStatus');
  const dot = document.getElementById('adoptDot');
  const msg = document.getElementById('adoptMsg');
  const sub = document.getElementById('adoptSub');

  if (enabled) {
    if (status) status.style.display = 'flex';
    if (dot) dot.className = 'adopt-dot scanning';
    if (msg) msg.textContent = 'Scanning for open positions...';
    socket.emit('adopt_positions', { enabled: true });
    // Poll every 10s for status updates
    _adoptPolling = setInterval(() => {
      socket.emit('adopt_positions', { enabled: true, poll: true });
    }, 10000);
  } else {
    if (status) status.style.display = 'none';
    if (dot) dot.className = 'adopt-dot off';
    socket.emit('adopt_positions', { enabled: false });
    if (_adoptPolling) { clearInterval(_adoptPolling); _adoptPolling = null; }
  }
}

socket.on('adopt_status', d => {
  const dot = document.getElementById('adoptDot');
  const msg = document.getElementById('adoptMsg');
  const status = document.getElementById('adoptStatus');
  if (!status || !_adoptEnabled) return;
  status.style.display = 'flex';

  if (d.adopted) {
    if (dot) dot.className = 'adopt-dot active';
    if (msg) msg.textContent = 'Managing: ' + d.symbol + ' (' + d.side + ') P&L: ' + (d.pnl_pct >= 0 ? '+' : '') + d.pnl_pct.toFixed(1) + '%';
  } else if (d.positions > 0) {
    if (dot) dot.className = 'adopt-dot scanning';
    if (msg) msg.textContent = d.positions + ' open position(s) found — adopting...';
  } else {
    if (dot) dot.className = 'adopt-dot scanning';
    if (msg) msg.textContent = 'No open positions — watching...';
  }
});

// Update adopt status from state broadcasts
function _updateAdoptUI(d) {
  if (!_adoptEnabled) return;
  const dot = document.getElementById('adoptDot');
  const msg = document.getElementById('adoptMsg');
  if (d.adopted_position && d.trade_open) {
    if (dot) dot.className = 'adopt-dot active';
    const ap = d.adopted_position;
    if (msg) msg.textContent = 'Managing: ' + (ap.symbol || '?') + ' (' + (ap.side || '?') + ')';
  }
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
  const idMap = { cursor:'dtCursor', hline:'dtHline', tline:'dtTline', rect:'dtRect', vline:'dtVline', ray:'dtRay', channel:'dtChannel', fib:'dtFib', arrow:'dtArrow', ellipse:'dtEllipse', measure:'dtMeasure', text:'dtText' };
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
  } else if (activeTool === 'ray') {
    if (!drawState) { drawState = { type: 'ray', idx1: d.idx, p1: d.price }; }
    else { drawings.push({ type: 'ray', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price }); drawState = null; draw(); }
  } else if (activeTool === 'channel') {
    if (!drawState) { drawState = { type: 'channel', step: 1, idx1: d.idx, p1: d.price }; }
    else if (drawState.step === 1) { drawState.idx2 = d.idx; drawState.p2 = d.price; drawState.step = 2; }
    else { const offset = d.price - drawState.p1; drawings.push({ type: 'channel', idx1: drawState.idx1, p1: drawState.p1, idx2: drawState.idx2, p2: drawState.p2, offset }); drawState = null; draw(); }
  } else if (activeTool === 'fib') {
    if (!drawState) { drawState = { type: 'fib', idx1: d.idx, p1: d.price }; }
    else { drawings.push({ type: 'fib', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price }); drawState = null; draw(); }
  } else if (activeTool === 'arrow') {
    if (!drawState) { drawState = { type: 'arrow', idx1: d.idx, p1: d.price }; }
    else { drawings.push({ type: 'arrow', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price }); drawState = null; draw(); }
  } else if (activeTool === 'ellipse') {
    if (!drawState) { drawState = { type: 'ellipse', idx1: d.idx, p1: d.price }; }
    else { drawings.push({ type: 'ellipse', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price }); drawState = null; draw(); }
  } else if (activeTool === 'measure') {
    if (!drawState) { drawState = { type: 'measure', idx1: d.idx, p1: d.price }; }
    else { drawings.push({ type: 'measure', idx1: drawState.idx1, p1: drawState.p1, idx2: d.idx, p2: d.price }); drawState = null; draw(); }
  } else if (activeTool === 'text') {
    const label = prompt('Enter text label:');
    if (label) { drawings.push({ type: 'text', idx: d.idx, price: d.price, label }); draw(); }
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
    } else if (d.type === 'ray') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(d.p1, mn, mx), y2 = py(d.p2, mn, mx);
      const dx = x2 - x1, dy = y2 - y1;
      const ext = dx === 0 ? 0 : (w - x1) / dx;
      const ex = x1 + dx * Math.max(ext, 2), ey = y1 + dy * Math.max(ext, 2);
      ctx.strokeStyle = 'rgba(255,200,50,.7)'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(ex, ey); ctx.stroke();
      ctx.fillStyle = 'rgba(255,200,50,.9)'; ctx.beginPath(); ctx.arc(x1, y1, 3, 0, Math.PI*2); ctx.fill();
    } else if (d.type === 'channel') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(d.p1, mn, mx), y2 = py(d.p2, mn, mx);
      const oy = py(d.p1 + d.offset, mn, mx) - y1;
      ctx.strokeStyle = 'rgba(100,180,255,.7)'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.strokeStyle = 'rgba(100,180,255,.45)'; ctx.setLineDash([4,3]);
      ctx.beginPath(); ctx.moveTo(x1, y1 + oy); ctx.lineTo(x2, y2 + oy); ctx.stroke();
      ctx.fillStyle = 'rgba(100,180,255,.04)';
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.lineTo(x2, y2+oy); ctx.lineTo(x1, y1+oy); ctx.closePath(); ctx.fill();
    } else if (d.type === 'fib') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const range = d.p2 - d.p1;
      const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
      const colors = ['#e74c3c','#e67e22','#f1c40f','#2ecc71','#3498db','#9b59b6','#e74c3c'];
      ctx.font = '8px IBM Plex Mono'; ctx.textAlign = 'left';
      levels.forEach((lv, li) => {
        const price = d.p1 + range * (1 - lv);
        const y = py(price, mn, mx);
        if (y < 0 || y > h) return;
        ctx.strokeStyle = colors[li] + '66'; ctx.lineWidth = 1; ctx.setLineDash(lv === 0 || lv === 1 ? [] : [4,3]);
        ctx.beginPath(); ctx.moveTo(Math.min(x1,x2), y); ctx.lineTo(Math.max(x1,x2,w), y); ctx.stroke();
        ctx.fillStyle = colors[li] + 'aa';
        ctx.fillText((lv*100).toFixed(1) + '% — ' + price.toFixed(2), Math.min(x1,x2) + 4, y - 3);
      });
      // Shaded zones
      ctx.fillStyle = 'rgba(46,204,113,.03)';
      const y382 = py(d.p1 + range * (1-0.382), mn, mx);
      const y618 = py(d.p1 + range * (1-0.618), mn, mx);
      ctx.fillRect(Math.min(x1,x2), Math.min(y382,y618), Math.max(x1,x2,w)-Math.min(x1,x2), Math.abs(y618-y382));
    } else if (d.type === 'arrow') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(d.p1, mn, mx), y2 = py(d.p2, mn, mx);
      ctx.strokeStyle = 'rgba(255,200,50,.75)'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      // Arrowhead
      const angle = Math.atan2(y2-y1, x2-x1);
      const sz = 8;
      ctx.fillStyle = 'rgba(255,200,50,.9)';
      ctx.beginPath();
      ctx.moveTo(x2, y2);
      ctx.lineTo(x2 - sz*Math.cos(angle-0.4), y2 - sz*Math.sin(angle-0.4));
      ctx.lineTo(x2 - sz*Math.cos(angle+0.4), y2 - sz*Math.sin(angle+0.4));
      ctx.closePath(); ctx.fill();
    } else if (d.type === 'ellipse') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(d.p1, mn, mx), y2 = py(d.p2, mn, mx);
      const cx2 = (x1+x2)/2, cy2 = (y1+y2)/2, rx = Math.abs(x2-x1)/2, ry = Math.abs(y2-y1)/2;
      ctx.strokeStyle = 'rgba(77,142,255,.6)'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(77,142,255,.05)';
      ctx.beginPath(); ctx.ellipse(cx2, cy2, Math.max(rx,1), Math.max(ry,1), 0, 0, Math.PI*2);
      ctx.fill(); ctx.stroke();
    } else if (d.type === 'measure') {
      const x1 = (d.idx1 - start) * cw + candleW / 2;
      const x2 = (d.idx2 - start) * cw + candleW / 2;
      const y1 = py(d.p1, mn, mx), y2 = py(d.p2, mn, mx);
      ctx.strokeStyle = 'rgba(255,255,255,.35)'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y1); ctx.lineTo(x2,y2); ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
      // Label
      const priceDiff = d.p2 - d.p1;
      const pct = d.p1 !== 0 ? ((d.p2 - d.p1) / d.p1 * 100).toFixed(2) : '0';
      const bars = Math.abs(d.idx2 - d.idx1);
      ctx.fillStyle = 'rgba(20,22,40,.9)';
      ctx.beginPath(); ctx.roundRect((x1+x2)/2 - 50, (y1+y2)/2 - 18, 100, 32, 5); ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,.15)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect((x1+x2)/2 - 50, (y1+y2)/2 - 18, 100, 32, 5); ctx.stroke();
      ctx.fillStyle = priceDiff >= 0 ? UP : DN; ctx.font = 'bold 9px IBM Plex Mono'; ctx.textAlign = 'center';
      ctx.fillText((priceDiff>=0?'+':'') + priceDiff.toFixed(2) + ' (' + pct + '%)', (x1+x2)/2, (y1+y2)/2 - 4);
      ctx.fillStyle = 'rgba(255,255,255,.5)'; ctx.font = '8px IBM Plex Mono';
      ctx.fillText(bars + ' bars', (x1+x2)/2, (y1+y2)/2 + 9);
    } else if (d.type === 'text') {
      const x = (d.idx - start) * cw + candleW / 2;
      const y = py(d.price, mn, mx);
      ctx.fillStyle = 'rgba(255,255,255,.75)'; ctx.font = '11px Space Grotesk'; ctx.textAlign = 'left';
      ctx.fillText(d.label, x + 4, y - 2);
      ctx.fillStyle = 'rgba(255,255,255,.4)';
      ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI*2); ctx.fill();
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
    if (drawState.type === 'tline' || drawState.type === 'ray' || drawState.type === 'arrow' || drawState.type === 'measure' || drawState.type === 'fib') {
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(mX, mY); ctx.stroke();
    } else if (drawState.type === 'rect' || drawState.type === 'ellipse') {
      const y2p = Math.min(y1, mY), y2h = Math.abs(mY - y1);
      const x2p = Math.min(x1, mX), x2w = Math.abs(mX - x1);
      if (drawState.type === 'ellipse') {
        ctx.beginPath(); ctx.ellipse((x1+mX)/2,(y1+mY)/2,Math.max(x2w/2,1),Math.max(y2h/2,1),0,0,Math.PI*2); ctx.stroke();
      } else {
        ctx.strokeRect(x2p, y2p, x2w, y2h);
      }
      ctx.fillStyle = 'rgba(255,255,255,.04)'; ctx.fillRect(x2p, y2p, x2w, y2h);
    } else if (drawState.type === 'channel' && drawState.step === 2) {
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo((drawState.idx2-start)*cw+candleW/2, py(drawState.p2,mn2,mx2)); ctx.stroke();
      const oy = mY - y1;
      ctx.setLineDash([4,3]);
      ctx.beginPath(); ctx.moveTo(x1, y1+oy); ctx.lineTo((drawState.idx2-start)*cw+candleW/2, py(drawState.p2,mn2,mx2)+oy); ctx.stroke();
    } else if (drawState.type === 'channel' && drawState.step === 1) {
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(mX, mY); ctx.stroke();
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
  const noMod = !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT';
  if (e.key === 'h' && noMod) setDrawTool('hline');
  if (e.key === 't' && noMod) setDrawTool('tline');
  if (e.key === 'r' && noMod) setDrawTool('rect');
  if (e.key === 'v' && noMod) setDrawTool('vline');
  if (e.key === 'y' && noMod) setDrawTool('ray');
  if (e.key === 'c' && noMod) setDrawTool('channel');
  if (e.key === 'f' && noMod) setDrawTool('fib');
  if (e.key === 'a' && noMod) setDrawTool('arrow');
  if (e.key === 'e' && noMod) setDrawTool('ellipse');
  if (e.key === 'm' && noMod) setDrawTool('measure');
  if (e.key === 'x' && noMod) setDrawTool('text');
});


// ── Right Panel Tab Switching ────────────────────────────────
function switchRpPanel(tab) {
  ['trade','positions','orders','activity'].forEach(t => {
    const panel = document.getElementById('rpPanel' + t.charAt(0).toUpperCase() + t.slice(1));
    const btn = document.getElementById('rpTab' + t.charAt(0).toUpperCase() + t.slice(1));
    if (panel) panel.style.display = t === tab ? '' : 'none';
    if (btn) btn.classList.toggle('active', t === tab);
  });
  if (tab === 'positions' || tab === 'orders') loadOrderBook();
}

let _orderBookLoaded = false;
let _orderBookData = null;
let _orderFilter = 'all';

async function loadOrderBook() {
  try {
    const data = await fetch('/api/order_book').then(r => r.json());
    if (data.error) return;
    _orderBookData = data;
    _orderBookLoaded = true;
    renderPositions(data.positions || [], data.summary || {});
    renderOrders(data.orders || []);
  } catch(e) {
    console.error('Order book load failed:', e);
  }
}

function renderPositions(positions, summary) {
  const list = document.getElementById('rpPosList');
  const count = document.getElementById('rpPosCount');
  const pnl = document.getElementById('rpPosPnl');
  if (!list) return;

  const openPos = positions.filter(p => p.quantity !== 0);
  if (count) count.textContent = openPos.length + ' open';
  if (pnl) {
    const totalPnl = summary.total_pnl || 0;
    pnl.textContent = 'P&L: ' + (totalPnl >= 0 ? '+' : '') + totalPnl.toFixed(2);
    pnl.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
  }

  if (!positions.length) {
    list.innerHTML = '<div class="rp-empty">No positions</div>';
    return;
  }

  list.innerHTML = positions.map(p => {
    const qty = p.quantity || 0;
    const pnlVal = p.pnl || 0;
    const m2m = p.m2m || 0;
    const avgPrice = p.average_price || 0;
    const lastPrice = p.last_price || 0;
    const cls = pnlVal >= 0 ? 'pos' : 'neg';
    const qtyStr = qty > 0 ? '+' + qty : qty.toString();
    const side = qty > 0 ? 'LONG' : qty < 0 ? 'SHORT' : 'CLOSED';
    const sideCls = qty > 0 ? 'pos-long' : qty < 0 ? 'pos-short' : 'pos-flat';
    return '<div class="rp-pos-card">' +
      '<div class="rp-pos-top">' +
        '<span class="rp-pos-sym">' + (p.tradingsymbol || '--') + '</span>' +
        '<span class="rp-pos-pnl-val ' + cls + '">' + (pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(2) + '</span>' +
      '</div>' +
      '<div class="rp-pos-mid">' +
        '<span class="rp-pos-side ' + sideCls + '">' + side + ' ' + Math.abs(qty) + '</span>' +
        '<span>Avg: ' + avgPrice.toFixed(2) + '</span>' +
        '<span>LTP: ' + lastPrice.toFixed(2) + '</span>' +
      '</div>' +
      '<div class="rp-pos-bot">' +
        '<span>M2M: ' + (m2m >= 0 ? '+' : '') + m2m.toFixed(2) + '</span>' +
        '<span>' + (p.product || '') + '</span>' +
        '<span>' + (p.exchange || '') + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

function filterOrders(filter) {
  _orderFilter = filter;
  ['all','open','done','rejected'].forEach(f => {
    const btn = document.getElementById('otab' + f.charAt(0).toUpperCase() + f.slice(1));
    if (btn) btn.classList.toggle('active', f === filter);
  });
  if (_orderBookData) renderOrders(_orderBookData.orders || []);
}

function renderOrders(orders) {
  const list = document.getElementById('rpOrderList');
  if (!list) return;

  let filtered = orders;
  if (_orderFilter === 'open') filtered = orders.filter(o => ['OPEN','TRIGGER PENDING'].includes(o.status));
  else if (_orderFilter === 'done') filtered = orders.filter(o => o.status === 'COMPLETE');
  else if (_orderFilter === 'rejected') filtered = orders.filter(o => o.status === 'REJECTED');

  if (!filtered.length) {
    list.innerHTML = '<div class="rp-empty">No ' + (_orderFilter === 'all' ? '' : _orderFilter + ' ') + 'orders</div>';
    return;
  }

  list.innerHTML = filtered.map(o => {
    const txn = o.transaction_type || '--';
    const status = o.status || '--';
    const qty = o.quantity || 0;
    const filled = o.filled_quantity || 0;
    const avgP = parseFloat(o.average_price || 0);
    const ts = o.order_timestamp || '';
    const time = ts.includes(' ') ? ts.split(' ')[1].substring(0,8) : ts.substring(11,19);
    const isBuy = txn === 'BUY';
    const stsCls = status === 'COMPLETE' ? 'sts-done' : status === 'REJECTED' ? 'sts-rej' : status === 'OPEN' ? 'sts-open' : 'sts-other';
    return '<div class="rp-order-card">' +
      '<div class="rp-ord-top">' +
        '<span class="rp-ord-txn ' + (isBuy ? 'txn-buy' : 'txn-sell') + '">' + txn + '</span>' +
        '<span class="rp-ord-sym">' + (o.tradingsymbol || '--') + '</span>' +
        '<span class="rp-ord-sts ' + stsCls + '">' + status + '</span>' +
      '</div>' +
      '<div class="rp-ord-bot">' +
        '<span>Qty: ' + filled + '/' + qty + '</span>' +
        '<span>Avg: ' + avgP.toFixed(2) + '</span>' +
        '<span>' + (o.order_type || '') + '</span>' +
        '<span>' + time + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

// Re-load order book on new trade close
socket.on('trade_closed', () => { _orderBookLoaded = false; });

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
    const UP = '#2ecc71', DN = '#e74c3c';

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
requestAnimationFrame(resize);
