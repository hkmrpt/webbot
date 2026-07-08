/* Visual check: EMA + BB on, zoom to last 60 bars, screenshot chart area. */
import puppeteer from 'puppeteer-core';
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new',
  args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });
await page.goto('http://localhost:5001/', { waitUntil: 'networkidle2' });
await new Promise(r => setTimeout(r, 2500));
await page.evaluate(() => {
  const c = window.__bot.chart;
  c.toggleOverlay('ema');
  c.toggleOverlay('bb');
  const n = c._lastCandles.length;
  c.chart.timeScale().setVisibleLogicalRange({ from: n - 60, to: n });
});
await new Promise(r => setTimeout(r, 800));
const el = await page.$('#chartMain');
await el.screenshot({ path: 'scripts/ui_probe3.png' });
await browser.close();
console.log('done');
