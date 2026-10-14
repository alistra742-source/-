'use strict';
/*
 * guns.lol view runner — CLI
 * Same engine as the web UI (runner.js); this just drives it from a terminal.
 *
 * usage:
 *   node bot.js <username>          # hits https://guns.lol/<username>
 *   node bot.js                     # prompts for the username
 *
 * env:
 *   HEADFUL=1            show the browsers instead of headless
 *   BROWSERS=n           default 2
 *   PAGES=n              default 5  (per browser)
 *   DWELL_MIN / DWELL_MAX / GAP_MIN / GAP_MAX   timing overrides (ms)
 *   TOR_SOCKS / TOR_PORT / TOR_PASSWORD
 *   BASE_URL=...         default https://guns.lol
 *   PROXY_FILE=...       default ./proxies.txt
 */
const path = require('path');
const readline = require('readline');
const { Runner } = require('./runner');

const CFG = {
  browsers: Number(process.env.BROWSERS || 2),
  pages: Number(process.env.PAGES || 5),
  headless: !process.env.HEADFUL,
  dwell: [Number(process.env.DWELL_MIN || 9000), Number(process.env.DWELL_MAX || 22000)],
  roundGap: [Number(process.env.GAP_MIN || 20000), Number(process.env.GAP_MAX || 60000)],
  baseUrl: process.env.BASE_URL || 'https://guns.lol',
  proxyFile: process.env.PROXY_FILE || path.join(__dirname, 'proxies.txt'),
  torSocks: process.env.TOR_SOCKS || 'socks5://127.0.0.1:9050',
  torControlPort: Number(process.env.TOR_PORT || 9051),
  torControlPass: process.env.TOR_PASSWORD || null,
};

function promptTarget() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question('guns.lol username: guns.lol/', (a) => { rl.close(); res(a.trim()); }));
}

async function main() {
  let target = (process.argv[2] || '').trim();
  if (!target) target = await promptTarget();
  if (!target) { console.error('no target — name it: node bot.js <username>'); process.exit(1); }

  const runner = new Runner({
    baseUrl: CFG.baseUrl,
    browsers: CFG.browsers,
    pages: CFG.pages,
    headless: CFG.headless,
    dwell: CFG.dwell,
    roundGap: CFG.roundGap,
    torSocks: CFG.torSocks,
    torControlPort: CFG.torControlPort,
    torControlPass: CFG.torControlPass,
  });
  const started = Date.now();
  runner.on('log', (e) => {
    const up = ((e.t - started) / 1000).toFixed(0);
    console.log(`[${up}s][${e.tag}] ${e.msg}  (hits=${e.hits} err=${e.errors})`);
  });

  await runner.start({ target, proxyFile: CFG.proxyFile });

  const shutdown = async () => { await runner.stop(); process.exit(0); };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((e) => { console.error(e.message); process.exit(1); });
