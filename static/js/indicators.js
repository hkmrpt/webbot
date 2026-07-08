/* indicators.js — pure indicator math (ported from the v8 custom chart engine).
 * All functions take candle arrays [{t,o,h,l,c,v}] or plain number arrays and
 * return aligned arrays with null for warmup slots. No DOM, no chart deps. */

export const IND_CATALOG = [
  // Trend (overlays)
  { id: 'ema',   name: 'EMA',   cat: 'trend', params: { period: 9,  color: '#00d2ff' } },
  { id: 'sma',   name: 'SMA',   cat: 'trend', params: { period: 20, color: '#e8b730' } },
  { id: 'wma',   name: 'WMA',   cat: 'trend', params: { period: 14, color: '#76ff03' } },
  { id: 'dema',  name: 'DEMA',  cat: 'trend', params: { period: 21, color: '#ff6e40' } },
  { id: 'tema',  name: 'TEMA',  cat: 'trend', params: { period: 21, color: '#ea80fc' } },
  { id: 'psar',  name: 'PSAR',  cat: 'trend', params: { step: 0.02, max: 0.2, color: '#ffeb3b' } },
  { id: 'ichimoku', name: 'Ichimoku', cat: 'trend', params: { tenkan: 9, kijun: 26, senkou: 52, color: '#26a69a' } },
  // Oscillators (sub-pane)
  { id: 'rsi',   name: 'RSI',   cat: 'osc', params: { period: 14, color: '#e040fb' }, sub: true },
  { id: 'stoch', name: 'Stoch', cat: 'osc', params: { k: 14, d: 3, smooth: 3, color: '#00bcd4' }, sub: true },
  { id: 'macd',  name: 'MACD',  cat: 'osc', params: { fast: 12, slow: 26, signal: 9, color: '#2196f3' }, sub: true },
  { id: 'cci',   name: 'CCI',   cat: 'osc', params: { period: 20, color: '#ff7043' }, sub: true },
  { id: 'williams', name: 'W%R', cat: 'osc', params: { period: 14, color: '#ab47bc' }, sub: true },
  { id: 'mfi',   name: 'MFI',   cat: 'osc', params: { period: 14, color: '#66bb6a' }, sub: true },
  // Volatility
  { id: 'bb',    name: 'Bollinger', cat: 'vol', params: { period: 20, mult: 2, color: '#8c64dc' } },
  { id: 'atr',   name: 'ATR',   cat: 'vol', params: { period: 14, color: '#ff9800' }, sub: true },
  { id: 'kc',    name: 'Keltner', cat: 'vol', params: { period: 20, mult: 1.5, color: '#4dd0e1' } },
  { id: 'dc',    name: 'Donchian', cat: 'vol', params: { period: 20, color: '#8d6e63' } },
  // Volume
  { id: 'vol',   name: 'Volume', cat: 'volume', params: {}, sub: true },
  { id: 'vwap',  name: 'VWAP',  cat: 'volume', params: { color: '#ffffff' } },
  { id: 'obv',   name: 'OBV',   cat: 'volume', params: { color: '#42a5f5' }, sub: true },
  { id: 'cmf',   name: 'CMF',   cat: 'volume', params: { period: 20, color: '#26c6da' }, sub: true },
];

export function ema(data, period) {
  const k = 2 / (period + 1);
  const out = [];
  let sum = 0;
  for (let i = 0; i < data.length; i++) {
    if (i < period) {
      sum += data[i];
      out.push(i === period - 1 ? sum / period : null);
    } else {
      out.push(data[i] * k + out[i - 1] * (1 - k));
    }
  }
  return out;
}

export function sma(data, period) {
  const out = new Array(data.length).fill(null);
  for (let i = period - 1; i < data.length; i++) {
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) s += data[j];
    out[i] = s / period;
  }
  return out;
}

export function wma(data, period) {
  const out = new Array(data.length).fill(null);
  const denom = period * (period + 1) / 2;
  for (let i = period - 1; i < data.length; i++) {
    let s = 0;
    for (let j = 0; j < period; j++) s += data[i - period + 1 + j] * (j + 1);
    out[i] = s / denom;
  }
  return out;
}

export function dema(data, period) {
  const e1 = ema(data, period);
  const e2 = ema(e1.map(v => v === null ? 0 : v), period);
  return data.map((_, i) => (e1[i] === null || e2[i] === null) ? null : 2 * e1[i] - e2[i]);
}

export function tema(data, period) {
  const e1 = ema(data, period);
  const e2 = ema(e1.map(v => v === null ? 0 : v), period);
  const e3 = ema(e2.map(v => v === null ? 0 : v), period);
  return data.map((_, i) =>
    (e1[i] === null || e2[i] === null || e3[i] === null) ? null : 3 * e1[i] - 3 * e2[i] + e3[i]);
}

export function bollinger(closes, period, mult) {
  const mid = [], upper = [], lower = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) { mid.push(null); upper.push(null); lower.push(null); continue; }
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += closes[j];
    const avg = sum / period;
    let sq = 0;
    for (let j = i - period + 1; j <= i; j++) sq += (closes[j] - avg) ** 2;
    const std = Math.sqrt(sq / period);
    mid.push(avg); upper.push(avg + mult * std); lower.push(avg - mult * std);
  }
  return { mid, upper, lower };
}

export function vwap(cndls) {
  // v||1 fallback: index charts (NIFTY) have no volume — degrade to the
  // cumulative typical-price mean instead of drawing nothing.
  const out = [];
  let cumTPV = 0, cumVol = 0;
  for (const c of cndls) {
    const v = c.v || 1;
    const tp = (c.h + c.l + c.c) / 3;
    cumTPV += tp * v; cumVol += v;
    out.push(cumVol > 0 ? cumTPV / cumVol : null);
  }
  return out;
}

export function rsi(data, period) {
  const out = new Array(data.length).fill(null);
  if (data.length < period + 1) return out;
  let avgG = 0, avgL = 0;
  for (let i = 1; i <= period; i++) {
    const d = data[i] - data[i - 1];
    if (d > 0) avgG += d; else avgL -= d;
  }
  avgG /= period; avgL /= period;
  out[period] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
  for (let i = period + 1; i < data.length; i++) {
    const d = data[i] - data[i - 1];
    avgG = (avgG * (period - 1) + (d > 0 ? d : 0)) / period;
    avgL = (avgL * (period - 1) + (d < 0 ? -d : 0)) / period;
    out[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
  }
  return out;
}

export function stochastic(cndls, kPeriod, dPeriod, smooth) {
  const len = cndls.length;
  const rawK = new Array(len).fill(null);
  for (let i = kPeriod - 1; i < len; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - kPeriod + 1; j <= i; j++) {
      hh = Math.max(hh, cndls[j].h); ll = Math.min(ll, cndls[j].l);
    }
    rawK[i] = hh === ll ? 50 : ((cndls[i].c - ll) / (hh - ll)) * 100;
  }
  const kLine = sma(rawK.map(v => v === null ? 0 : v), smooth);
  for (let i = 0; i < kPeriod - 1 + smooth - 1; i++) kLine[i] = null;
  const dLine = sma(kLine.map(v => v === null ? 0 : v), dPeriod);
  for (let i = 0; i < kPeriod - 1 + smooth - 1 + dPeriod - 1; i++) dLine[i] = null;
  return { k: kLine, d: dLine };
}

export function atr(cndls, period) {
  const out = new Array(cndls.length).fill(null);
  if (cndls.length < 2) return out;
  const trs = [cndls[0].h - cndls[0].l];
  for (let i = 1; i < cndls.length; i++) {
    const c = cndls[i], pc = cndls[i - 1].c;
    trs.push(Math.max(c.h - c.l, Math.abs(c.h - pc), Math.abs(c.l - pc)));
  }
  if (trs.length < period) return out;
  let sum = 0;
  for (let i = 0; i < period; i++) sum += trs[i];
  out[period - 1] = sum / period;
  for (let i = period; i < trs.length; i++) {
    out[i] = (out[i - 1] * (period - 1) + trs[i]) / period;
  }
  return out;
}

export function macd(data, fast, slow, signal) {
  const eF = ema(data, fast), eS = ema(data, slow);
  const line = data.map((_, i) => (eF[i] === null || eS[i] === null) ? null : eF[i] - eS[i]);
  const sig = ema(line.map(v => v === null ? 0 : v), signal);
  for (let i = 0; i < slow - 1; i++) sig[i] = null;
  const hist = data.map((_, i) => (line[i] === null || sig[i] === null) ? null : line[i] - sig[i]);
  return { macd: line, signal: sig, histogram: hist };
}

export function cci(cndls, period) {
  const out = new Array(cndls.length).fill(null);
  const tp = cndls.map(c => (c.h + c.l + c.c) / 3);
  for (let i = period - 1; i < cndls.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += tp[j];
    const mean = sum / period;
    let md = 0;
    for (let j = i - period + 1; j <= i; j++) md += Math.abs(tp[j] - mean);
    md /= period;
    out[i] = md === 0 ? 0 : (tp[i] - mean) / (0.015 * md);
  }
  return out;
}

export function williamsR(cndls, period) {
  const out = new Array(cndls.length).fill(null);
  for (let i = period - 1; i < cndls.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) { hh = Math.max(hh, cndls[j].h); ll = Math.min(ll, cndls[j].l); }
    out[i] = hh === ll ? -50 : ((hh - cndls[i].c) / (hh - ll)) * -100;
  }
  return out;
}

export function mfi(cndls, period) {
  const out = new Array(cndls.length).fill(null);
  if (cndls.length < period + 1) return out;
  const tp = cndls.map(c => (c.h + c.l + c.c) / 3);
  const mf = cndls.map((c, i) => tp[i] * (c.v || 1));
  for (let i = period; i < cndls.length; i++) {
    let pos = 0, neg = 0;
    for (let j = i - period + 1; j <= i; j++) {
      if (tp[j] > tp[j - 1]) pos += mf[j]; else neg += mf[j];
    }
    out[i] = neg === 0 ? 100 : 100 - 100 / (1 + pos / neg);
  }
  return out;
}

export function obv(cndls) {
  // v||1 fallback: with no volume OBV degrades to a cumulative tick-direction
  // line (still directional) instead of a dead flat zero.
  const out = new Array(cndls.length).fill(null);
  if (!cndls.length) return out;
  out[0] = cndls[0].v || 1;
  for (let i = 1; i < cndls.length; i++) {
    const vol = cndls[i].v || 1;
    if (cndls[i].c > cndls[i - 1].c)      out[i] = out[i - 1] + vol;
    else if (cndls[i].c < cndls[i - 1].c) out[i] = out[i - 1] - vol;
    else                                   out[i] = out[i - 1];
  }
  return out;
}

export function cmf(cndls, period) {
  const out = new Array(cndls.length).fill(null);
  for (let i = period - 1; i < cndls.length; i++) {
    let mfv = 0, vol = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const hl = cndls[j].h - cndls[j].l;
      const clv = hl === 0 ? 0 : ((cndls[j].c - cndls[j].l) - (cndls[j].h - cndls[j].c)) / hl;
      const v = cndls[j].v || 0;
      mfv += clv * v; vol += v;
    }
    out[i] = vol === 0 ? 0 : mfv / vol;
  }
  return out;
}

export function psar(cndls, step, max) {
  const out = new Array(cndls.length).fill(null);
  if (cndls.length < 2) return out;
  let bull = cndls[1].c > cndls[0].c;
  let af = step;
  let ep = bull ? cndls[0].h : cndls[0].l;
  let sar = bull ? cndls[0].l : cndls[0].h;
  out[0] = { val: sar, bull };
  for (let i = 1; i < cndls.length; i++) {
    sar = sar + af * (ep - sar);
    if (bull) {
      if (i >= 2) sar = Math.min(sar, cndls[i - 1].l, cndls[i - 2].l);
      if (cndls[i].l < sar) { bull = false; sar = ep; ep = cndls[i].l; af = step; }
      else if (cndls[i].h > ep) { ep = cndls[i].h; af = Math.min(af + step, max); }
    } else {
      if (i >= 2) sar = Math.max(sar, cndls[i - 1].h, cndls[i - 2].h);
      if (cndls[i].h > sar) { bull = true; sar = ep; ep = cndls[i].h; af = step; }
      else if (cndls[i].l < ep) { ep = cndls[i].l; af = Math.min(af + step, max); }
    }
    out[i] = { val: sar, bull };
  }
  return out;
}

export function ichimoku(cndls, tenkan, kijun, senkou) {
  const len = cndls.length;
  const hl = (s, e) => {
    let h = -Infinity, l = Infinity;
    for (let i = s; i <= e; i++) { h = Math.max(h, cndls[i].h); l = Math.min(l, cndls[i].l); }
    return (h + l) / 2;
  };
  const tenkanSen = new Array(len).fill(null);
  const kijunSen  = new Array(len).fill(null);
  const senkouA   = new Array(len + kijun).fill(null);
  const senkouB   = new Array(len + kijun).fill(null);
  for (let i = tenkan - 1; i < len; i++) tenkanSen[i] = hl(i - tenkan + 1, i);
  for (let i = kijun - 1; i < len; i++)  kijunSen[i]  = hl(i - kijun + 1, i);
  for (let i = kijun - 1; i < len; i++) {
    if (tenkanSen[i] !== null && kijunSen[i] !== null)
      senkouA[i + kijun] = (tenkanSen[i] + kijunSen[i]) / 2;
  }
  for (let i = senkou - 1; i < len; i++) senkouB[i + kijun] = hl(i - senkou + 1, i);
  return { tenkan: tenkanSen, kijun: kijunSen,
           senkouA: senkouA.slice(0, len), senkouB: senkouB.slice(0, len) };
}

export function keltner(cndls, period, mult) {
  const closes = cndls.map(c => c.c);
  const mid = ema(closes, period);
  const a = atr(cndls, period);
  const upper = cndls.map((_, i) => (mid[i] === null || a[i] === null) ? null : mid[i] + mult * a[i]);
  const lower = cndls.map((_, i) => (mid[i] === null || a[i] === null) ? null : mid[i] - mult * a[i]);
  return { mid, upper, lower };
}

export function donchian(cndls, period) {
  const upper = new Array(cndls.length).fill(null);
  const lower = new Array(cndls.length).fill(null);
  const mid   = new Array(cndls.length).fill(null);
  for (let i = period - 1; i < cndls.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) { hh = Math.max(hh, cndls[j].h); ll = Math.min(ll, cndls[j].l); }
    upper[i] = hh; lower[i] = ll; mid[i] = (hh + ll) / 2;
  }
  return { upper, lower, mid };
}

export function heikinAshi(cndls) {
  const out = [];
  for (let i = 0; i < cndls.length; i++) {
    const c = cndls[i];
    const haC = (c.o + c.h + c.l + c.c) / 4;
    const haO = i === 0 ? (c.o + c.c) / 2 : (out[i - 1].o + out[i - 1].c) / 2;
    out.push({ t: c.t, o: haO, h: Math.max(c.h, haO, haC),
               l: Math.min(c.l, haO, haC), c: haC, v: c.v });
  }
  return out;
}
