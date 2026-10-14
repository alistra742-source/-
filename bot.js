'use strict';
/*
 * guns.lol view runner
 * --------------------
 * 2 chromium instances x 5 isolated contexts (=10 concurrent "pages").
 * Every context egresses on its own proxy. No proxy list -> Tor, and each
 * context gets a freshly rotated exit circuit via the Tor control port.
 *
 * usage:
 *   node bot.js <username>          # hits https://guns.lol/<username>
 *   node bot.js                     # prompts for the username
 *
 * env:
 *   HEADFUL=1            show the browsers instead of headless
 *   BROWSERS=n           default 2
 *   PAGES=n              default 5  (per browser)
 *   TOR_SOCKS=socks5://127.0.0.1:9050
 *   TOR_PORT=9051        Tor control port
 *   TOR_PASSWORD=...     control-port password (omit if cookie/none auth)
 *   BASE_URL=...         default https://guns.lol  (swap for a mirror/staging host)
 *   PROXY_FILE=...       default ./proxies.txt
 */

const fs = require('fs');
const net = require('net');
const path = require('path');
const readline = require('readline');
const { chromium } = require('playwright');

const CFG = {
  browsers: Number(process.env.BROWSERS || 2),
  pagesPerBrowser: Number(process.env.PAGES || 5),
  headless: !process.env.HEADFUL,
  navTimeout: 45000,
  dwell: [Number(process.env.DWELL_MIN || 9000), Number(process.env.DWELL_MAX || 22000)],   // ms on-page so the hit registers
  roundGap: [Number(process.env.GAP_MIN || 20000), Number(process.env.GAP_MAX || 60000)],   // ms a worker naps before its next hit
  baseUrl: (process.env.BASE_URL || 'https://guns.lol').replace(/\/+$/, ''),
  proxyFile: process.env.PROXY_FILE || path.join(__dirname, 'proxies.txt'),
  torSocks: process.env.TOR_SOCKS || 'socks5://127.0.0.1:9050',
  torControlHost: '127.0.0.1',
  torControlPort: Number(process.env.TOR_PORT || 9051),
  torControlPass: process.env.TOR_PASSWORD || null,
};

const UAS = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
];
const TZS = ['America/New_York', 'Europe/London', 'Europe/Berlin', 'America/Los_Angeles', 'Asia/Tokyo'];
const LANGS = ['en-US,en;q=0.9', 'en-GB,en;q=0.8', 'en-US,en;q=0.9,de;q=0.7'];

const stats = { hits: 0, errors: 0, started: Date.now() };
let stopping = false;
const rand = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;
const pick = (arr) => arr[rand(0, arr.length - 1)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function log(tag, msg) {
  const up = ((Date.now() - stats.started) / 1000).toFixed(0);
  console.log(`[${up}s][${tag}] ${msg}  (hits=${stats.hits} err=${stats.errors})`);
}

/* ---------- proxy parsing ---------- */
function parseProxyLine(raw) {
  const line = raw.trim();
  if (!line || line.startsWith('#')) return null;

  // scheme://[user:pass@]host:port
  let m = line.match(/^(?<scheme>https?|socks5h?|socks4):\/\/(?:(?<user>[^:@/]+):(?<pass>[^@/]+)@)?(?<host>[^:/]+):(?<port>\d+)$/i);
  if (m) return fmt(m.groups.scheme, m.groups.host, m.groups.port, m.groups.user, m.groups.pass);

  // host:port:user:pass
  m = line.match(/^(?<host>[^:]+):(?<port>\d+):(?<user>[^:]+):(?<pass>.+)$/);
  if (m) return fmt('http', m.groups.host, m.groups.port, m.groups.user, m.groups.pass);

  // host:port
  m = line.match(/^(?<host>[^:]+):(?<port>\d+)$/);
  if (m) return fmt('http', m.groups.host, m.groups.port);

  return null;
}
function fmt(scheme, host, port, user, pass) {
  const s = scheme.toLowerCase() === 'socks5h' ? 'socks5' : scheme.toLowerCase();
  return { server: `${s}://${host}:${port}`, username: user || undefined, password: pass || undefined, label: `${host}:${port}` };
}
function loadProxies() {
  if (!fs.existsSync(CFG.proxyFile)) return [];
  const out = [];
  for (const line of fs.readFileSync(CFG.proxyFile, 'utf8').split(/\r?\n/)) {
    const p = parseProxyLine(line);
    if (p) out.push(p);
    else if (line.trim() && !line.trim().startsWith('#')) console.warn(`[warn] unparsed proxy line: ${line.trim()}`);
  }
  return out;
}

/* ---------- tor control: new exit circuit ---------- */
function torNewCircuit() {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: CFG.torControlHost, port: CFG.torControlPort });
    let buf = '';
    let stage = 0; // 0=awaiting auth reply, 1=awaiting NEWNYM reply
    const done = (err) => {
      sock.destroy();
      err ? reject(err) : resolve();
    };
    sock.setTimeout(6000);
    sock.on('timeout', () => done(new Error('tor control timeout')));
    sock.on('error', (e) => done(e));
    sock.on('connect', () => {
      stage = 0;
      sock.write((CFG.torControlPass ? `AUTHENTICATE "${CFG.torControlPass}"` : 'AUTHENTICATE') + '\r\n');
    });
    sock.on('data', (chunk) => {
      buf += chunk.toString();
      let idx;
      while ((idx = buf.indexOf('\r\n')) !== -1) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (/^5\d\d/.test(line)) return done(new Error(`tor control: ${line}`));
        if (line.startsWith('250')) {
          if (stage === 0) { stage = 1; sock.write('SIGNAL NEWNYM\r\n'); }
          else if (stage === 1) { sock.write('QUIT\r\n'); return done(); }
        }
      }
    });
  });
}
async function newIdentity() {
  try { await torNewCircuit(); await sleep(1500); } // let the circuit come up
  catch (e) { log('tor', `circuit rotate failed (${e.message}) — reusing exit`); }
}

/* ---------- one page worker ---------- */
async function humanDwell(page) {
  const end = Date.now() + rand(CFG.dwell[0], CFG.dwell[1]);
  while (Date.now() < end) {
    await page.mouse.move(rand(0, 900), rand(0, 700), { steps: rand(4, 12) }).catch(() => {});
    await page.evaluate(() => window.scrollBy(0, Math.floor(Math.random() * 400) - 150)).catch(() => {});
    await sleep(rand(700, 2500));
  }
}

async function runWorker(browser, proxy, url, id) {
  for (;;) {
    if (stopping) return;
    let context;
    try {
      const opts = {
        userAgent: pick(UAS),
        viewport: { width: rand(1280, 1920), height: rand(720, 1080) },
        locale: pick(['en-US', 'en-GB']),
        timezoneId: pick(TZS),
        ignoreHTTPSErrors: true,
        bypassCSP: true,
        deviceScaleFactor: 1,
        extraHTTPHeaders: { 'Accept-Language': pick(LANGS) },
        proxy: proxy
          ? { server: proxy.server, username: proxy.username, password: proxy.password }
          : { server: CFG.torSocks },
      };

      if (!proxy) await newIdentity();

      context = await browser.newContext(opts);
      const page = await context.newPage();
      await page.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = window.chrome || { runtime: {} };
      });

      await page.goto(url, { waitUntil: 'load', timeout: CFG.navTimeout });
      // give client-side view counting a beat to fire
      await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {});
      await humanDwell(page);

      stats.hits++;
      log(`p${id}${proxy ? ' ' + proxy.label : ' tor'}`, 'view sent');
    } catch (e) {
      if (stopping) return;
      stats.errors++;
      log(`p${id}`, `miss: ${String(e.message).split('\n')[0]}`);
    } finally {
      if (context) await context.close().catch(() => {});
    }
    await sleep(rand(CFG.roundGap[0], CFG.roundGap[1]));
  }
}

/* ---------- boot ---------- */
function promptTarget() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question('guns.lol username: guns.lol/', (a) => { rl.close(); res(a.trim().replace(/^\/+|\/+$/g, '')); }));
}

async function main() {
  let target = (process.argv[2] || '').trim().replace(/^\/+|\/+$/g, '');
  if (!target) target = await promptTarget();
  if (!target) { console.error('no target — name it: node bot.js <username>'); process.exit(1); }
  const url = `${CFG.baseUrl}/${target}`;

  const proxies = loadProxies();
  const torMode = proxies.length === 0;
  console.log(`target : ${url}`);
  console.log(`layout : ${CFG.browsers} browsers x ${CFG.pagesPerBrowser} pages = ${CFG.browsers * CFG.pagesPerBrowser} workers`);
  console.log(`egress : ${torMode ? `TOR ${CFG.torSocks} (fresh circuit per hit)` : `${proxies.length} proxies (round-robin)`}`);
  console.log(`mode   : ${CFG.headless ? 'headless' : 'headful'}\n`);

  const browsers = [];
  let slot = 0;
  for (let b = 0; b < CFG.browsers; b++) {
    const browser = await chromium.launch({
      headless: CFG.headless,
      args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage'],
    });
    browsers.push(browser);
    for (let p = 0; p < CFG.pagesPerBrowser; p++) {
      const proxy = torMode ? null : proxies[slot++ % proxies.length];
      runWorker(browser, proxy, url, `${b + 1}.${p + 1}`).catch((e) => log('fatal', e.message));
    }
  }

  const shutdown = async () => {
    if (stopping) return;
    stopping = true;
    console.log(`\n[stop] closing ${browsers.length} browsers — ${stats.hits} hits sent`);
    await Promise.allSettled(browsers.map((b) => b.close()));
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((e) => { console.error(e); process.exit(1); });
