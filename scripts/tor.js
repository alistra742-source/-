'use strict';
/*
 * Tor for the no-proxy mode.
 *
 *   npm run tor          start (or confirm) a daemon from the command line
 *   ensureTor({ log })  used by the runner: starts one automatically when no
 *                        proxies were supplied, so the run never has to fail
 *                        just because Tor was not started by hand.
 *
 * Tor runs with a SOCKS port and a control port, so each hit can use a fresh
 * exit circuit. Installs Tor first when running as root.
 */
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const HOST = '127.0.0.1';
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0;
// TOR_BIN overrides the binary (absolute path or name); the tests use it.
const TOR_BIN = process.env.TOR_BIN || 'tor';
const has = (c) => {
  if (c.includes('/')) return fs.existsSync(c);
  return process.platform !== 'win32' &&
    spawnSync('sh', ['-c', `command -v ${c}`], { stdio: 'ignore' }).status === 0;
};

const probe = (port, host = HOST) => new Promise((res) => {
  const s = net.connect({ host, port });
  const done = (ok) => { s.destroy(); res(ok); };
  s.setTimeout(1200);
  s.once('connect', () => done(true));
  s.once('timeout', () => done(false));
  s.once('error', () => done(false));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/*
 * Make sure a SOCKS endpoint is listening. Returns
 *   { socks: boolean, control: boolean, started: boolean }
 * and throws with the exact fix when Tor cannot be brought up.
 */
async function ensureTor({
  socksPort = Number(process.env.TOR_SOCKS_PORT || 9050),
  controlPort = Number(process.env.TOR_PORT || 9051),
  log = () => {},
} = {}) {
  const result = { socks: false, control: false, started: false };
  if (await probe(socksPort)) {
    log(`tor already listening on ${HOST}:${socksPort}`);
    result.socks = true;
    result.control = await probe(controlPort);
    return result;
  }

  if (!has(TOR_BIN)) {
    if (isRoot && has('apt-get')) {
      log('tor not installed — installing (apt-get)…');
      spawnSync('apt-get', ['update'], { stdio: 'ignore' });
      if (spawnSync('apt-get', ['install', '-y', 'tor'], { stdio: 'ignore' }).status !== 0 || !has(TOR_BIN)) {
        throw new Error('tor install failed — run as root: apt-get update && apt-get install -y tor');
      }
    } else {
      throw new Error(
        'no proxies supplied and the tor binary is not installed — ' +
        'as root run: apt-get update && apt-get install -y tor  (then Start again), or paste proxies in the box',
      );
    }
  }

  const dataDir = path.join(os.tmpdir(), 'guns-tor-data');
  const args = [
    '--SocksPort', String(socksPort),
    '--ControlPort', String(controlPort),
    '--CookieAuthentication', '0',      // no password: the runner AUTHENTICATEs with none
    '--DataDirectory', dataDir,
  ];
  // Tor refuses to run as root unless a User is named; in a container that is root.
  if (isRoot) args.push('--User', 'root');
  fs.mkdirSync(dataDir, { recursive: true });

  log(`starting tor (socks ${socksPort}, control ${controlPort})…`);
  const child = spawn(TOR_BIN, args, { detached: true, stdio: 'ignore' });
  child.on('error', () => {});
  child.unref();
  result.started = true;

  for (let i = 0; i < 60; i++) {
    if (await probe(socksPort)) break;
    await sleep(500);
  }
  if (!(await probe(socksPort))) {
    throw new Error(`tor did not come up on socks ${socksPort} — run it in the foreground to see why: tor --SocksPort ${socksPort} --ControlPort ${controlPort}`);
  }
  result.socks = true;
  result.control = await probe(controlPort);
  log(`tor ready — ${result.control ? 'exit circuits rotate per hit' : 'control port closed, every hit shares one exit'}`);
  return result;
}

/* CLI: npm run tor */
if (require.main === module) {
  ensureTor({ log: (m) => console.log(`[tor] ${m}`) })
    .then((r) => {
      if (!r.control) {
        console.log(`[tor] to rotate exits, run: tor --SocksPort 9050 --ControlPort 9051 --CookieAuthentication 0`);
      }
    })
    .catch((e) => { console.error(`[tor] ${e.message}`); process.exit(1); });
}

module.exports = { ensureTor };
