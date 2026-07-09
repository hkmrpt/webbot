/* chart.js — Lightweight Charts wrapper.
 * Candle store (historical seed + live tick bucketing), timeframe switching,
 * NIFTY/Option source toggle, chart types (candle/HA/line/area), level
 * price-lines (ref/entry/SL/trail/TP), trade markers, indicator overlays and
 * a single oscillator sub-pane. Library: static/vendor/lightweight-charts. */

import * as IND from './indicators.js';

const LWC = window.LightweightCharts;

// Lightweight Charts renders epoch times as UTC and has no timezone setting.
// Shift every timestamp by the IST offset so the axis shows exchange time.
const IST_OFF = 5.5 * 3600;

const TF_LIST = [
  { label: '5s',  secs: 5 },   { label: '15s', secs: 15 },
  { label: '1m',  secs: 60 },  { label: '3m',  secs: 180 },
  { label: '5m',  secs: 300 }, { label: '15m', secs: 900 },
  { label: '30m', secs: 1800 },{ label: '1H',  secs: 3600 },
];

function tfToInterval(tf) {
  if (tf <= 60)   return 'minute';
  if (tf <= 180)  return '3minute';
  if (tf <= 300)  return '5minute';
  if (tf <= 900)  return '15minute';
  if (tf <= 1800) return '30minute';
  return '60minute';
}

// Days fetched per back-fill request when scrolling into the past
function chunkDays(tf) {
  if (tf <= 60)   return 3;
  if (tf <= 300)  return 10;
  if (tf <= 900)  return 30;
  if (tf <= 1800) return 45;
  return 90;
}

const HIST_MAX_DAYS_BACK = 120;   // hard stop for lazy back-loading

function dstr(d) {
  return d.toISOString().slice(0, 10);
}

const CHART_OPTS = {
  layout: { background: { color: '#070b14' }, textColor: '#4e6585',
            fontFamily: "'IBM Plex Mono', monospace", fontSize: 10 },
  grid: { vertLines: { color: 'rgba(24,32,52,.5)' },
          horzLines: { color: 'rgba(24,32,52,.5)' } },
  crosshair: { mode: 0,
    vertLine: { color: 'rgba(0,212,255,.35)', labelBackgroundColor: '#0a0e1a' },
    horzLine: { color: 'rgba(0,212,255,.35)', labelBackgroundColor: '#0a0e1a' } },
  rightPriceScale: { borderColor: '#182034' },
  timeScale: { borderColor: '#182034', timeVisible: true, secondsVisible: true },
};

const UP = '#00e87a', DOWN = '#ff3d5c';

const LEVEL_STYLE = {
  ref:   { color: '#b06aff', title: 'REF'   },
  entry: { color: '#00d4ff', title: 'ENTRY' },
  sl:    { color: '#ff3d5c', title: 'SL'    },
  trail: { color: '#ffb020', title: 'TRAIL' },
  tp:    { color: '#00e87a', title: 'TP'    },
  // Manual-trade NIFTY levels — drawn on the NIFTY chart
  msl:   { color: '#ff3d5c', title: 'N-SL'  },
  mtp:   { color: '#00e87a', title: 'N-TP'  },
  // Pending-order trigger — drawn on whichever chart it watches
  pnd:   { color: '#4d8eff', title: 'PND'   },
};

export class BotChart {

  constructor(mainEl, oscEl) {
    this.mainEl = mainEl;
    this.oscEl  = oscEl;

    this.tf       = 60;          // seconds
    this.source   = 'nifty';     // 'nifty' | 'option'
    this.type     = 'candle';    // candle | ha | line | area

    // per-source stores: closed candles + live ticks (capped)
    this.store = {
      nifty:  { hist: [], ticks: [] },
      option: { hist: [], ticks: [] },
    };
    this.markers  = [];
    this.levels   = { ref: 0, entry: 0, sl: 0, trail: 0, tp: 0, msl: 0, mtp: 0, pnd: 0 };
    this.pendingWatch = null;    // 'nifty' | 'premium' — where the PND line lives
    this._priceLines = {};
    this.overlays = new Map();   // catalogId -> {series:[...], params}
    this.oscillator = null;      // {id, series:[...], params}

    // history paging (scroll-left back-fill)
    this._histOldest   = null;    // YYYY-MM-DD of the earliest loaded day
    this._histLoading  = false;
    this._histDone     = false;
    this._histEmptyRuns = 0;

    this.chart = LWC.createChart(mainEl, { ...CHART_OPTS, autoSize: true });
    this.osc   = LWC.createChart(oscEl,  { ...CHART_OPTS, autoSize: true });
    this._buildPriceSeries();
    this._syncTimeScales();

    // Lazy back-fill: nearing the left edge of loaded data loads older days
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(r => {
      if (r && r.from < 15) this._fetchOlder();
    });

    // Drag-and-drop for the manual NIFTY level lines (N-SL / N-TP)
    this.onLevelDragEnd = null;      // callback(levelKey, newPrice)
    this._dragKey  = null;
    this._dragHold = 0;              // suppress server overwrites just after a drag
    this._initLevelDrag();
    this.onOhlc = null;          // callback({o,h,l,c}) for the readout
    this._lastCandles = [];

    this.chart.subscribeCrosshairMove(p => {
      if (!this.onOhlc) return;
      let bar = null;
      if (p && p.time && this._series && p.seriesData) {
        const d = p.seriesData.get(this._series);
        if (d && d.open !== undefined) bar = { o: d.open, h: d.high, l: d.low, c: d.close };
      }
      if (!bar && this._lastCandles.length) {
        const lc = this._lastCandles[this._lastCandles.length - 1];
        bar = { o: lc.o, h: lc.h, l: lc.l, c: lc.c };
      }
      if (bar) this.onOhlc(bar);
    });
  }

  // ── series lifecycle ────────────────────────────────────────────────────
  _buildPriceSeries() {
    if (this._series) this.chart.removeSeries(this._series);
    this._priceLines = {};
    if (this.type === 'line') {
      this._series = this.chart.addLineSeries({ color: '#00d4ff', lineWidth: 2,
        priceLineVisible: false });
    } else if (this.type === 'area') {
      this._series = this.chart.addAreaSeries({ lineColor: '#00d4ff', lineWidth: 2,
        topColor: 'rgba(0,212,255,.25)', bottomColor: 'rgba(0,212,255,.02)',
        priceLineVisible: false });
    } else {
      this._series = this.chart.addCandlestickSeries({
        upColor: UP, downColor: DOWN, borderUpColor: UP, borderDownColor: DOWN,
        wickUpColor: UP, wickDownColor: DOWN });
    }
  }

  _syncTimeScales() {
    let guard = false;
    const link = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange(r => {
      if (guard || !r) return;
      guard = true;
      b.timeScale().setVisibleLogicalRange(r);
      guard = false;
    });
    link(this.chart, this.osc);
    link(this.osc, this.chart);
  }

  // ── data ────────────────────────────────────────────────────────────────
  async loadHistorical() {
    if (this.source !== 'nifty') { this.render(); return; }
    // reset paging state — a TF switch reloads at the new interval
    this._histOldest = dstr(new Date());
    this._histDone = false;
    this._histEmptyRuns = 0;
    try {
      const r = await fetch('/api/nifty_historical?interval=' + tfToInterval(this.tf));
      const data = await r.json();
      if (Array.isArray(data)) {
        this.store.nifty.hist = data.map(c => (
          { t: c.t + IST_OFF, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v || 0 }));
      }
    } catch (e) { /* no history (bad token / off-hours) — tick-only chart */ }
    this.render();
  }

  /** Scroll-left back-fill: fetch the chunk of days before the earliest
   *  loaded day and prepend it, preserving the visible time range. */
  async _fetchOlder() {
    if (this.source !== 'nifty' || this._histLoading || this._histDone
        || !this._histOldest) return;
    this._histLoading = true;
    try {
      const to = new Date(this._histOldest + 'T00:00:00Z');
      to.setUTCDate(to.getUTCDate() - 1);
      const from = new Date(to);
      from.setUTCDate(from.getUTCDate() - (chunkDays(this.tf) - 1));

      const ageDays = (Date.now() - to.getTime()) / 86400000;
      if (ageDays > HIST_MAX_DAYS_BACK) { this._histDone = true; return; }

      const r = await fetch(`/api/nifty_historical?interval=${tfToInterval(this.tf)}`
                            + `&from=${dstr(from)}&to=${dstr(to)}`);
      const data = await r.json();
      this._histOldest = dstr(from);

      if (!Array.isArray(data) || !data.length) {
        // holidays/weekends return empty — give up only after several dry chunks
        if (++this._histEmptyRuns >= 4) this._histDone = true;
        return;
      }
      this._histEmptyRuns = 0;

      const hist = this.store.nifty.hist;
      const firstT = hist.length ? hist[0].t : Infinity;
      const older = data.map(c => (
        { t: c.t + IST_OFF, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v || 0 }))
        .filter(c => c.t < firstT);
      if (!older.length) return;
      this.store.nifty.hist = older.concat(hist);

      // Re-render without yanking the viewport: setData shifts logical
      // indices, so save/restore the TIME range instead.
      const view = this.chart.timeScale().getVisibleRange();
      this.render();
      if (view) this.chart.timeScale().setVisibleRange(view);
    } catch (e) { /* transient fetch error — retry on next scroll event */ }
    finally { this._histLoading = false; }
  }

  onTick(source, price, tsSec, cumVol = 0) {
    const st = this.store[source];
    // cumVol is the exchange's cumulative daily volume — candle volume is
    // derived from the delta within each bucket in _buildCandles.
    st.ticks.push({ t: tsSec + IST_OFF, v: price, cv: cumVol || 0 });
    if (st.ticks.length > 100000) st.ticks.splice(0, 50000);
    if (source === this.source) this._updateForming();
  }

  clearOption() {
    this.store.option = { hist: [], ticks: [] };
  }

  _buildCandles() {
    const st = this.store[this.source];
    const tf = this.tf;
    const candles = st.hist.slice();
    const lastHistEnd = candles.length
      ? candles[candles.length - 1].t + tf : 0;
    let cur = null;
    for (const tick of st.ticks) {
      const bucket = Math.floor(tick.t / tf) * tf;
      if (candles.length && bucket <= candles[candles.length - 1].t) {
        // tick belongs to the last (possibly historical) candle — update it
        const lc = candles[candles.length - 1];
        lc.h = Math.max(lc.h, tick.v); lc.l = Math.min(lc.l, tick.v); lc.c = tick.v;
        continue;
      }
      if (cur && bucket === cur.t) {
        cur.h = Math.max(cur.h, tick.v); cur.l = Math.min(cur.l, tick.v); cur.c = tick.v;
        cur.v = Math.max(0, (tick.cv || 0) - cur._cv0);   // cumulative-vol delta
      } else {
        if (cur) { delete cur._cv0; candles.push(cur); }
        cur = { t: bucket, o: tick.v, h: tick.v, l: tick.v, c: tick.v,
                v: 0, _cv0: tick.cv || 0 };
      }
    }
    if (cur) { delete cur._cv0; candles.push(cur); }
    return candles;
  }

  // full rebuild (TF switch / source switch / type switch / indicator change)
  render() {
    const candles = this._buildCandles();
    this._lastCandles = candles;
    const src = this.type === 'ha' ? IND.heikinAshi(candles) : candles;
    if (this.type === 'line' || this.type === 'area') {
      this._series.setData(src.map(c => ({ time: c.t, value: c.c })));
    } else {
      this._series.setData(src.map(c => (
        { time: c.t, open: c.o, high: c.h, low: c.l, close: c.c })));
    }
    this._applyMarkers();
    this._applyLevels();
    this._renderIndicators(candles);
    if (this.onOhlc && candles.length) {
      const lc = candles[candles.length - 1];
      this.onOhlc({ o: lc.o, h: lc.h, l: lc.l, c: lc.c });
    }
  }

  // cheap per-tick update of the forming candle only
  _updateForming() {
    const candles = this._buildCandles();
    this._lastCandles = candles;
    if (!candles.length) return;
    const src = this.type === 'ha' ? IND.heikinAshi(candles) : candles;
    const lc = src[src.length - 1];
    if (this.type === 'line' || this.type === 'area') {
      this._series.update({ time: lc.t, value: lc.c });
    } else {
      this._series.update({ time: lc.t, open: lc.o, high: lc.h, low: lc.l, close: lc.c });
    }
    this._indTick++;
    if (this._indTick % 5 === 0) this._renderIndicators(candles);
    if (this.onOhlc) {
      const raw = candles[candles.length - 1];
      this.onOhlc({ o: raw.o, h: raw.h, l: raw.l, c: raw.c });
    }
  }
  _indTick = 0;

  // ── controls ────────────────────────────────────────────────────────────
  setTF(secs)      { this.tf = secs; this.loadHistorical(); }
  setSource(src)   { this.source = src; this.render(); if (src === 'nifty') this.loadHistorical(); }
  setType(type)    { this.type = type; this._buildPriceSeries(); this.render(); }

  // ── levels & markers ────────────────────────────────────────────────────
  setLevels(levels) {
    let changed = false;
    const holding = Date.now() < this._dragHold;
    for (const k of Object.keys(LEVEL_STYLE)) {
      // Never let a state broadcast yank a line the user is dragging
      // (or just dropped — the server echo takes a moment to round-trip)
      if (this._dragKey === k
          || (holding && (k === 'msl' || k === 'mtp' || k === 'pnd'))) continue;
      const v = levels[k] || 0;
      if (this.levels[k] !== v) { this.levels[k] = v; changed = true; }
    }
    if (changed) this._applyLevels();
  }

  // ── drag-and-drop level lines ───────────────────────────────────────────
  HIT_PX = 7;

  _initLevelDrag() {
    const el = this.mainEl;

    // capture phase so we beat the library's own pan handler
    el.addEventListener('mousedown', e => {
      const k = this._hitLevel(e);
      if (!k) return;
      this._dragKey = k;
      this.chart.applyOptions({ handleScroll: false, handleScale: false });
      e.preventDefault();
      e.stopPropagation();
    }, true);

    window.addEventListener('mousemove', e => {
      if (this._dragKey) {
        const price = this._yToPrice(e);
        if (price != null) {
          this.levels[this._dragKey] = Math.round(price * 10) / 10;
          const line = this._priceLines[this._dragKey];
          if (line) line.applyOptions({ price: this.levels[this._dragKey] });
        }
        e.preventDefault();
        return;
      }
      // hover feedback
      el.style.cursor = this._hitLevel(e) ? 'ns-resize' : '';
    });

    window.addEventListener('mouseup', () => {
      if (!this._dragKey) return;
      const k = this._dragKey;
      this._dragKey  = null;
      this._dragHold = Date.now() + 2000;   // ignore stale broadcasts briefly
      this.chart.applyOptions({ handleScroll: true, handleScale: true });
      if (this.onLevelDragEnd) this.onLevelDragEnd(k, this.levels[k]);
    });
  }

  _draggableKeys() {
    const keys = [];
    if (this.source === 'nifty') keys.push('msl', 'mtp');
    if ((this.pendingWatch === 'nifty' && this.source === 'nifty')
        || (this.pendingWatch === 'premium' && this.source === 'option')) {
      keys.push('pnd');
    }
    return keys;
  }

  _hitLevel(e) {
    const rect = this.mainEl.getBoundingClientRect();
    const y = e.clientY - rect.top;
    for (const k of this._draggableKeys()) {
      if (!this._priceLines[k] || !this.levels[k]) continue;
      const ly = this._series.priceToCoordinate(this.levels[k]);
      if (ly != null && Math.abs(ly - y) <= this.HIT_PX) return k;
    }
    return null;
  }

  _yToPrice(e) {
    const rect = this.mainEl.getBoundingClientRect();
    const p = this._series.coordinateToPrice(e.clientY - rect.top);
    return (p == null || !isFinite(p)) ? null : p;
  }

  _applyLevels() {
    for (const [k, line] of Object.entries(this._priceLines)) {
      this._series.removePriceLine(line);
      delete this._priceLines[k];
    }
    // NIFTY chart: reference + manual NIFTY levels · option chart: trade levels
    const wanted = this.source === 'nifty' ? ['ref', 'msl', 'mtp']
                                           : ['entry', 'sl', 'trail', 'tp'];
    // pending trigger line lives on whichever chart it watches
    if ((this.pendingWatch === 'nifty' && this.source === 'nifty')
        || (this.pendingWatch === 'premium' && this.source === 'option')) {
      wanted.push('pnd');
    }
    for (const k of wanted) {
      const price = this.levels[k];
      if (!price) continue;
      const s = LEVEL_STYLE[k];
      this._priceLines[k] = this._series.createPriceLine({
        price, color: s.color, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed,
        axisLabelVisible: true, title: s.title });
    }
  }

  addMarker(kind, tsSec, price, text) {
    this.markers.push({
      time: Math.floor((tsSec + IST_OFF) / this.tf) * this.tf,
      position: kind === 'buy' ? 'belowBar' : 'aboveBar',
      color: kind === 'buy' ? UP : DOWN,
      shape: kind === 'buy' ? 'arrowUp' : 'arrowDown',
      text,
    });
    if (this.markers.length > 60) this.markers.splice(0, 20);
    this._applyMarkers();
  }

  _applyMarkers() {
    try { this._series.setMarkers(this.markers.slice().sort((a, b) => a.time - b.time)); }
    catch (e) { /* markers outside data range — ignore */ }
  }

  // ── indicators ──────────────────────────────────────────────────────────
  toggleOverlay(catalogId, params = null) {
    if (this.overlays.has(catalogId)) {
      for (const s of this.overlays.get(catalogId).series) this.chart.removeSeries(s);
      this.overlays.delete(catalogId);
    } else {
      const cat = IND.IND_CATALOG.find(c => c.id === catalogId);
      if (!cat || cat.sub) return;
      this.overlays.set(catalogId, { series: [], params: { ...cat.params, ...(params || {}) } });
    }
    this.render();
  }

  setOscillator(catalogId, params = null) {
    if (this.oscillator) {
      for (const s of this.oscillator.series) this.osc.removeSeries(s);
      this.oscillator = null;
    }
    if (catalogId) {
      const cat = IND.IND_CATALOG.find(c => c.id === catalogId);
      if (cat) this.oscillator = { id: catalogId, series: [],
                                   params: { ...cat.params, ...(params || {}) } };
    }
    this.oscEl.style.display = this.oscillator ? 'block' : 'none';
    this.render();
    return !!this.oscillator;
  }

  /** Live param update (period/mult/color…): rebuild the indicator's series
   *  with the merged params and re-render. No-op if the indicator is off. */
  setIndicatorParams(catalogId, params) {
    const ov = this.overlays.get(catalogId);
    if (ov) {
      ov.params = { ...ov.params, ...params };
      for (const s of ov.series) this.chart.removeSeries(s);
      ov.series = [];
      this.render();
      return true;
    }
    if (this.oscillator && this.oscillator.id === catalogId) {
      this.oscillator.params = { ...this.oscillator.params, ...params };
      for (const s of this.oscillator.series) this.osc.removeSeries(s);
      this.oscillator.series = [];
      this.render();
      return true;
    }
    return false;
  }

  _line(chart, color, width = 1) {
    return chart.addLineSeries({ color, lineWidth: width, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false });
  }

  _setLineData(series, candles, values) {
    series.setData(candles.map((c, i) => ({ time: c.t, value: values[i] }))
                          .filter(p => p.value !== null && p.value !== undefined
                                    && isFinite(p.value)));
  }

  _renderIndicators(candles) {
    const closes = candles.map(c => c.c);

    for (const [id, ov] of this.overlays) {
      const p = ov.params;
      const need = n => {
        // width 2 — 1px lines vanish among dense candles at full zoom-out
        while (ov.series.length < n) ov.series.push(this._line(this.chart, p.color, 2));
        return ov.series;
      };
      switch (id) {
        case 'ema':  this._setLineData(need(1)[0], candles, IND.ema(closes, p.period)); break;
        case 'sma':  this._setLineData(need(1)[0], candles, IND.sma(closes, p.period)); break;
        case 'wma':  this._setLineData(need(1)[0], candles, IND.wma(closes, p.period)); break;
        case 'dema': this._setLineData(need(1)[0], candles, IND.dema(closes, p.period)); break;
        case 'tema': this._setLineData(need(1)[0], candles, IND.tema(closes, p.period)); break;
        case 'vwap': this._setLineData(need(1)[0], candles, IND.vwap(candles)); break;
        case 'psar': {
          const vals = IND.psar(candles, p.step, p.max)
            .map(v => v && typeof v === 'object' ? v.val : v);
          const s = need(1)[0];
          s.applyOptions({ lineStyle: LWC.LineStyle.Dotted, lineWidth: 1 });
          this._setLineData(s, candles, vals);
          break;
        }
        case 'bb': {
          const b = IND.bollinger(closes, p.period, p.mult);
          const s = need(3);
          this._setLineData(s[0], candles, b.upper);
          this._setLineData(s[1], candles, b.mid);
          this._setLineData(s[2], candles, b.lower);
          break;
        }
        case 'kc': {
          const k = IND.keltner(candles, p.period, p.mult);
          const s = need(3);
          this._setLineData(s[0], candles, k.upper);
          this._setLineData(s[1], candles, k.mid);
          this._setLineData(s[2], candles, k.lower);
          break;
        }
        case 'dc': {
          const d = IND.donchian(candles, p.period);
          const s = need(3);
          this._setLineData(s[0], candles, d.upper);
          this._setLineData(s[1], candles, d.mid);
          this._setLineData(s[2], candles, d.lower);
          break;
        }
        case 'ichimoku': {
          const ich = IND.ichimoku(candles, p.tenkan, p.kijun, p.senkou);
          const s = need(4);
          this._setLineData(s[0], candles, ich.tenkan);
          this._setLineData(s[1], candles, ich.kijun);
          this._setLineData(s[2], candles, ich.senkouA);
          this._setLineData(s[3], candles, ich.senkouB);
          s[1].applyOptions({ color: '#ff6e40' });
          s[2].applyOptions({ color: 'rgba(0,232,122,.6)' });
          s[3].applyOptions({ color: 'rgba(255,61,92,.6)' });
          break;
        }
      }
    }

    if (this.oscillator) {
      const o = this.oscillator, p = o.params;
      const need = (n, mk) => {
        while (o.series.length < n) o.series.push(mk(o.series.length));
        return o.series;
      };
      const mkLine = () => this._line(this.osc, p.color, 1);
      switch (o.id) {
        case 'rsi':     this._setLineData(need(1, mkLine)[0], candles, IND.rsi(closes, p.period)); break;
        case 'atr':     this._setLineData(need(1, mkLine)[0], candles, IND.atr(candles, p.period)); break;
        case 'cci':     this._setLineData(need(1, mkLine)[0], candles, IND.cci(candles, p.period)); break;
        case 'williams':this._setLineData(need(1, mkLine)[0], candles, IND.williamsR(candles, p.period)); break;
        case 'mfi':     this._setLineData(need(1, mkLine)[0], candles, IND.mfi(candles, p.period)); break;
        case 'obv':     this._setLineData(need(1, mkLine)[0], candles, IND.obv(candles)); break;
        case 'cmf':     this._setLineData(need(1, mkLine)[0], candles, IND.cmf(candles, p.period)); break;
        case 'stoch': {
          const st = IND.stochastic(candles, p.k, p.d, p.smooth);
          const s = need(2, i => this._line(this.osc, i === 0 ? p.color : '#ffb020'));
          this._setLineData(s[0], candles, st.k);
          this._setLineData(s[1], candles, st.d);
          break;
        }
        case 'macd': {
          const m = IND.macd(closes, p.fast, p.slow, p.signal);
          const s = need(3, i => i === 2
            ? this.osc.addHistogramSeries({ color: 'rgba(77,142,255,.5)',
                priceLineVisible: false, lastValueVisible: false })
            : this._line(this.osc, i === 0 ? p.color : '#ffb020'));
          this._setLineData(s[0], candles, m.macd);
          this._setLineData(s[1], candles, m.signal);
          s[2].setData(candles.map((c, i) => ({ time: c.t, value: m.histogram[i],
            color: (m.histogram[i] || 0) >= 0 ? 'rgba(0,232,122,.5)' : 'rgba(255,61,92,.5)' }))
            .filter(pt => pt.value !== null && isFinite(pt.value)));
          break;
        }
        case 'vol': {
          const s = need(1, () => this.osc.addHistogramSeries({
            priceLineVisible: false, lastValueVisible: false }));
          s[0].setData(candles.map(c => ({ time: c.t, value: c.v || 0,
            color: c.c >= c.o ? 'rgba(0,232,122,.45)' : 'rgba(255,61,92,.45)' })));
          break;
        }
      }
    }
  }
}

export { TF_LIST };
