/* Headless UI probe — drives the dashboard in Edge, reports console errors,
 * exercises the indicator menu, and inspects chart series state.
 * Usage: node scripts/ui_probe.mjs */
import puppeteer from 'puppeteer-core';

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';

const browser = await puppeteer.launch({
  executablePath: EDGE, headless: 'new',
  args: ['--no-sandbox', '--window-size=1600,900'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });

const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

await page.goto('http://localhost:5001/', { waitUntil: 'networkidle2', timeout: 30000 });
await new Promise(r => setTimeout(r, 3000));

const boot = await page.evaluate(() => ({
  hasBot: !!window.__bot,
  stateKeys: window.__bot ? Object.keys(window.__bot.state).length : 0,
  candles: window.__bot ? window.__bot.chart._lastCandles.length : -1,
  histLen: window.__bot ? window.__bot.chart.store.nifty.hist.length : -1,
  tickLen: window.__bot ? window.__bot.chart.store.nifty.ticks.length : -1,
}));
console.log('BOOT:', JSON.stringify(boot));

// open indicator menu and click EMA (overlay)
await page.click('#indBtn');
await new Promise(r => setTimeout(r, 300));
const clicked = await page.evaluate(() => {
  const items = [...document.querySelectorAll('.dd-item')];
  const ema = items.find(i => i.textContent.trim().startsWith('EMA'));
  if (!ema) return 'EMA item not found';
  ema.click();
  return 'clicked EMA';
});
console.log('MENU:', clicked);
await new Promise(r => setTimeout(r, 800));

const afterEma = await page.evaluate(() => {
  const c = window.__bot.chart;
  const ov = c.overlays.get('ema');
  return {
    overlayCount: c.overlays.size,
    emaSeries: ov ? ov.series.length : 0,
    emaParams: ov ? ov.params : null,
    candles: c._lastCandles.length,
  };
});
console.log('AFTER EMA:', JSON.stringify(afterEma));

// click RSI (oscillator)
await page.click('#indBtn');
await new Promise(r => setTimeout(r, 300));
await page.evaluate(() => {
  const items = [...document.querySelectorAll('.dd-item')];
  const rsi = items.find(i => i.textContent.trim().startsWith('RSI'));
  if (rsi) rsi.click();
});
await new Promise(r => setTimeout(r, 800));

const afterRsi = await page.evaluate(() => {
  const c = window.__bot.chart;
  return {
    osc: c.oscillator ? c.oscillator.id : null,
    oscSeries: c.oscillator ? c.oscillator.series.length : 0,
    oscVisible: document.getElementById('chartOsc').style.display,
    oscH: document.getElementById('chartOsc').offsetHeight,
  };
});
console.log('AFTER RSI:', JSON.stringify(afterRsi));

// param editor probe (gear buttons)
const gears = await page.evaluate(() => document.querySelectorAll('.dd-gear').length);
console.log('GEARS:', gears);

console.log('CONSOLE ERRORS:', errors.length ? errors : 'none');
await page.screenshot({ path: 'scripts/ui_probe.png' });
await browser.close();
