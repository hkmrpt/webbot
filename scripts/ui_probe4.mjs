/* Verify: overlay visible, param editor exists and live-updates the series,
 * IST time axis, VWAP now draws, option-volume plumbing intact. */
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

// 1. open menu, enable EMA via UI click
await page.click('#indBtn');
await page.evaluate(() => {
  [...document.querySelectorAll('.dd-item')]
    .find(i => i.textContent.trim().startsWith('EMA')).click();
});
await new Promise(r => setTimeout(r, 400));

// 2. open EMA gear, change period 9 → 21 via the input
await page.click('#indBtn');
const gearResult = await page.evaluate(() => {
  const item = [...document.querySelectorAll('.dd-item')]
    .find(i => i.textContent.trim().startsWith('EMA'));
  const gear = item.querySelector('.dd-gear');
  if (!gear) return 'NO GEAR';
  gear.click();
  const editor = item.nextElementSibling;
  const num = editor.querySelector('input[type=number]');
  if (!num) return 'NO NUMBER INPUT';
  num.value = '21';
  num.dispatchEvent(new Event('change'));
  return 'edited period=21';
});
await new Promise(r => setTimeout(r, 400));

const check = await page.evaluate(() => {
  const c = window.__bot.chart;
  const ov = c.overlays.get('ema');
  // VWAP should now have data (v||1 fallback)
  c.toggleOverlay('vwap');
  const vwapPts = c.overlays.get('vwap').series[0].data().length;
  c.toggleOverlay('vwap');
  const first = c._lastCandles[0];
  const istHour = new Date(first.t * 1000).getUTCHours();  // displayed hour
  return {
    emaParams: ov ? ov.params : null,
    emaPts: ov ? ov.series[0].data().length : 0,
    vwapPts,
    firstCandleDisplayedHour: istHour,   // should be 9 (09:15 IST), not 3 (UTC)
    stored: localStorage.getItem('indParams'),
  };
});
console.log(JSON.stringify({ gearResult, ...check }, null, 1));

// 3. zoomed screenshot with EMA-21 on
await page.evaluate(() => {
  const c = window.__bot.chart;
  const n = c._lastCandles.length;
  c.chart.timeScale().setVisibleLogicalRange({ from: n - 60, to: n });
});
await new Promise(r => setTimeout(r, 500));
const el = await page.$('#chartMain');
await el.screenshot({ path: 'scripts/ui_probe4.png' });
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
