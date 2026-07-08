/* Verify scroll-left history back-fill: pan to the left edge repeatedly,
 * assert older candles get prepended and the viewport doesn't jump. */
import puppeteer from 'puppeteer-core';
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
  args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });
const errors = [];
page.on('pageerror', e => errors.push(e.message));

await page.goto('http://localhost:5001/', { waitUntil: 'networkidle2' });
await new Promise(r => setTimeout(r, 2500));

const before = await page.evaluate(() => {
  const c = window.__bot.chart;
  return { hist: c.store.nifty.hist.length,
           oldest: c.store.nifty.hist[0] ? new Date(c.store.nifty.hist[0].t * 1000).toISOString() : null };
});
console.log('BEFORE:', JSON.stringify(before));

// Simulate scrolling to the left edge 3 times (each triggers a back-fill)
for (let i = 0; i < 3; i++) {
  await page.evaluate(() => {
    window.__bot.chart.chart.timeScale().setVisibleLogicalRange({ from: -5, to: 80 });
  });
  await new Promise(r => setTimeout(r, 1500));
}

const after = await page.evaluate(() => {
  const c = window.__bot.chart;
  const vr = c.chart.timeScale().getVisibleRange();
  return {
    hist: c.store.nifty.hist.length,
    oldest: new Date(c.store.nifty.hist[0].t * 1000).toISOString(),
    histOldestDay: c._histOldest,
    done: c._histDone,
    viewFrom: vr ? new Date(vr.from * 1000).toISOString() : null,
  };
});
console.log('AFTER 3 SCROLLS:', JSON.stringify(after));
console.log('GREW:', after.hist > before.hist ? `yes (+${after.hist - before.hist} candles)` : 'NO — BUG');
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
