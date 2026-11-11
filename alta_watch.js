#!/usr/bin/env node
// Alta 交換器即時狀態收集器(常駐)
// Alta 沒有公開 API:用 headless 瀏覽器登入 manage.alta.inc,讀網頁版解碼好的 MQTT 資料
// (window._mqttdb.devMap),每 20 秒寫一次 alta.json 給 fetch_stats.py 讀。
// 憑證:YC_ALTA_USER / YC_ALTA_PASS(放 ~/.config/zonetech/yc.env),站點 YC_ALTA_SITE。
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const USER = process.env.YC_ALTA_USER, PASS = process.env.YC_ALTA_PASS;
const SITE = process.env.YC_ALTA_SITE || 'YC 松菸';
const OUT = path.join(__dirname, 'alta.json');
const EVERY_MS = 20000;
if (!USER || !PASS) { console.error('缺少 YC_ALTA_USER / YC_ALTA_PASS'); process.exit(1); }

function findChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const base = path.join(os.homedir(), 'Library/Caches/ms-playwright');
  const dirs = fs.readdirSync(base).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of dirs) {
    const p = path.join(base, d, 'chrome-headless-shell-mac-arm64/chrome-headless-shell');
    if (fs.existsSync(p)) return p;
  }
  throw new Error('找不到 chrome-headless-shell,請設定 CHROME_PATH');
}

const log = (...a) => console.log(new Date().toISOString(), ...a);

// 在頁面內執行:把 devMap 裡的交換器整理成精簡結構(BigInt 位元圖在瀏覽器內解)
function extract() {
  const db = window._mqttdb;
  if (!db || !db.devMap) return null;
  const bit = (v, i) => (BigInt(v || 0) >> BigInt(i)) & 1n ? true : false;
  const speed = (d, i) => bit(d.spd10000, i) ? 10000 : bit(d.spd5000, i) ? 5000 : bit(d.spd2500, i) ? 2500
    : bit(d.spd1000, i) ? 1000 : bit(d.spd100, i) ? 100 : bit(d.spd10, i) ? 10 : 0;
  const out = [];
  for (const d of Object.values(db.devMap)) {
    if (d.type !== 3 || !d.nports) continue;   // type 3 = 交換器
    const ports = [];
    for (let i = 0; i < d.nports; i++) {
      const p = (d.ports || {})[i] || {};
      const n = ((d.lldp || {}).neighs || []).find(x => x.p === i);
      ports.push({
        idx: i + 1, speed: speed(d, i), up: speed(d, i) > 0,
        full_duplex: bit(d.dupfull, i), poe: i < (d.poeports || 0),
        poe_w: typeof p.p === 'number' ? p.p : null, poe_v: typeof p.v === 'number' ? p.v : null,
        uplink: d.uplink === i,
        neighbor: n ? { mac: n.mac, name: n.name, descr: n.descr, port: n.port } : null,
      });
    }
    out.push({
      mac: d.id, name: d.name, model: d.model, version: d.version, ip: d.lanip || (d.net || {}).lanip || '',
      ts: d.ts || null, uptime: d.uptime || null, temp: (d.temp || {}).a ?? null,
      load: d.load ?? null, mem: d.mem ?? null, nports: d.nports, poeports: d.poeports || 0,
      blocked: d.blocked || 0, ports,
    });
  }
  return { connected: !!db.connected, switches: out };
}

async function login(page) {
  await page.goto('https://manage.alta.inc/', { waitUntil: 'networkidle', timeout: 60000 });
  if (await page.$('input[name=email]')) {
    await page.fill('input[name=email]', USER);
    await page.fill('input[name=password]', PASS);
    await page.keyboard.press('Enter');
    await page.waitForURL(/dashboard|site/, { timeout: 30000 });
  }
  await page.goto('https://manage.alta.inc/site/' + encodeURIComponent(SITE).replace(/%20/g, '+'),
                  { waitUntil: 'domcontentloaded', timeout: 60000 });
  log('已登入並開啟站點', SITE);
}

(async () => {
  const browser = await chromium.launch({ executablePath: findChrome() });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  await login(page);
  let lastFresh = Date.now();
  for (;;) {
    await new Promise(r => setTimeout(r, EVERY_MS));
    try {
      if (/manage\.alta\.inc\/?$|login/.test(page.url())) await login(page);
      const data = await page.evaluate(extract);
      const newest = Math.max(0, ...((data && data.switches) || []).map(s => s.ts || 0));
      if (data && data.switches.length) {
        const tmp = OUT + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify({ written: Math.floor(Date.now() / 1000), site: SITE, ...data }));
        fs.renameSync(tmp, OUT);
      }
      if (newest && Date.now() / 1000 - newest < 120) lastFresh = Date.now();
      // 超過 3 分鐘沒有新資料(MQTT 斷線、token 過期):重新整理,必要時重新登入
      if (Date.now() - lastFresh > 180000) {
        log('資料停滯,重新載入');
        await login(page);
        lastFresh = Date.now();
      }
    } catch (e) {
      log('錯誤', e.message);
      try { await login(page); } catch (e2) { log('重新登入失敗', e2.message); }
    }
  }
})().catch(e => { console.error(e); process.exit(1); });
