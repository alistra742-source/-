'use strict';
/*
 * Get a local Tor daemon up for the no-proxy mode.
 *
 *   npm run tor
 *
 * Brings up Tor with a SOCKS port and a control port, so each hit can use a
 * fresh exit circuit. Installs Tor first when running as root; otherwise it
 * prints the one command that does.
 */
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const HOST = '127.0.0.1';
const SOCKS = Number(process.env.TOR_SOCKS_PORT || 9050);
const CONTROL = Number(process.env.TOR_PORT || 9051);
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0;
const has = (c) => process.platform !== 'win32' &&
  spawnSync('sh', ['-c', `command -v ${c}`], { stdio: 'ignore' }).status === 0;

const probe = (port) => new Promise((res) => {
  const s = net.connect({ host: HOST, port });
  const done = (ok) => { s.destroy(); res(ok); };
  s.setTimeout(1200);
  s.once('connect', () => done(true));
  s.once('timeout', () => done(false));
  s.once('error', () => done(false));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function report() {
  if (await probe(CONTROL)) {
    console.log(`[tor] control port ${CONTROL} open — exit circuits rotate per hit`);
    return true;
  }
  console.log(`[tor] control port ${CONTROL} not reachable — every hit will share one exit.`);
  console.log(`[tor] to rotate, run: tor --SocksPort ${SOCKS} --ControlPort ${CONTROL} --CookieAuthentication 0`);
  return false;
}

(async () => {
  if (await probe(SOCKS)) {
    console.log(`[tor] socks already listening on ${HOST}:${SOCKS}`);
    await report();
    return;
  }

  if (!has('tor')) {
    if (isRoot && has('apt-get')) {
      console.log('[tor] installing tor…');
      spawnSync('apt-get', ['update'], { stdio: 'inherit' });
      if (spawnSync('apt-get', ['install', '-y', 'tor'], { stdio: 'inherit' }).status !== 0 || !has('tor')) {
        console.error('[tor] install failed — install tor manually, then rerun: npm run tor');
        process.exit(1);
      }
    } else {
      console.error('[tor] the tor binary is not installed.');
      console.error('      as root:  apt-get update && apt-get install -y tor');
      console.error('      then:     npm run tor');
      console.error('      or skip Tor: paste proxies into the UI proxy box.');
      process.exit(1);
    }
  }

  const args = [
    '--SocksPort', String(SOCKS),
    '--ControlPort', String(CONTROL),
    '--CookieAuthentication', '0',      // no password: the runner AUTHENTICATEs with none
    '--DataDirectory', path.join(os.tmpdir(), 'guns-tor-data'),
  ];
  // Tor refuses to run as root unless a User is named; in a container that is root.
  if (isRoot) args.push('--User', 'root');

  fs.mkdirSync(path.join(os.tmpdir(), 'guns-tor-data'), { recursive: true });
  console.log(`[tor] starting tor (socks ${SOCKS}, control ${CONTROL})…`);
  spawn('tor', args, { detached: true, stdio: 'ignore' }).unref();

  for (let i = 0; i < 60; i++) {
    if (await probe(SOCKS)) { console.log('[tor] ready'); await report(); return; }
    await sleep(500);
  }
  console.error('[tor] tor did not come up — run it in the foreground to see why:');
  console.error(`      tor --SocksPort ${SOCKS} --ControlPort ${CONTROL}`);
  process.exit(1);
})();
