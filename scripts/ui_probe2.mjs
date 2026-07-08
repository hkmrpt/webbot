/* Deep indicator probe: toggle every indicator, count series data points. */
import puppeteer from 'puppeteer-core';

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
  args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });

await page.goto('http://localhost:5001/', { waitUntil: 'networkidle2' });
await new Promise(r => setTimeout(r, 2500));

const report = await page.evaluate(async () => {
  const c = window.__bot.chart;
  const out = {};
  const overlays = ['ema','sma','wma','dema','tema','psar','ichimoku','bb','kc','dc','vwap'];
  for (const id of overlays) {
    c.toggleOverlay(id);
    const ov = c.overlays.get(id);
    out[id] = ov ? ov.series.map(s => { try { return s.data().length; } catch (e) { return 'ERR:' + e.message; } }) : 'no-series';
    c.toggleOverlay(id);   // off again
  }
  const oscs = ['rsi','stoch','macd','atr','cci','williams','mfi','obv','cmf','vol'];
  for (const id of oscs) {
    c.setOscillator(id);
    out[id] = c.oscillator ? c.oscillator.series.map(s => { try { return s.data().length; } catch (e) { return 'ERR:' + e.message; } }) : 'no-osc';
  }
  c.setOscillator(null);
  out._candles = c._lastCandles.length;
  out._volSum = c._lastCandles.reduce((a, x) => a + (x.v || 0), 0);
  return out;
});
console.log(JSON.stringify(report, null, 1));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
