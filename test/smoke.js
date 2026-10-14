'use strict';
/*
 * Self-contained smoke test — no network, no Tor daemon needed.
 *
 *   npm test
 *
 * Spins a local origin and a local forward proxy, then drives the real server
 * over HTTP exactly as the browser does: proxy mode, Tor mode, the UI contract,
 * and teardown. Everything it asserts is behaviour a user can see.
 */
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.join(__dirname, '..');
let passed = 0, failed = 0;

function check(name, ok, detail) {
  if (ok) { passed++; console.log(`  ok    ${name}`); }
  else { failed++; console.log(`  FAIL  ${name}${detail ? '  — ' + detail : ''}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function request(port, p, opts = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: p, method: opts.method || 'GET', headers: opts.headers }, (res) => {
      let b = '';
      res.on('data', (d) => { b += d; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: b }));
    });
    req.on('error', reject);
    if (opts.body) req.write(opts.body);
    req.end();
  });
}
const post = (port, p, obj) => request(port, p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(obj) });
const refuses = async (port) => { try { await request(port, '/'); return false; } catch { return true; } };

/* Boot the app and wait until it is actually answering, on whatever port it bound. */
async function launchApp(extraEnv = {}) {
  const child = spawn('node', ['server.js'], {
    cwd: ROOT,
    env: { ...process.env, PORTS: '0', HOST: '127.0.0.1', ...extraEnv },
  });
  const ports = [];
  let out = '';
  child.stdout.on('data', (d) => {
    out += d;
    for (const m of d.toString().matchAll(/http:\/\/[^:]+:(\d+)/g)) ports.push(Number(m[1]));
  });
  child.stderr.on('data', (d) => { out += d; });
  let exited = null;
  child.on('exit', (c) => { exited = c; });

  for (let i = 0; i < 60 && !ports.length; i++) await sleep(150);
  if (!ports.length) throw new Error('server never reported a port:\n' + out);
  return { child, port: ports[0], ports, getOut: () => out, getExit: () => exited };
}

function startSim() {
  const state = { origin: 0, proxy: 0 };
  const origin = http.createServer((req, res) => {
    state.origin++;
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end('<h1>GUNS-LOL-SIM</h1>');
  });
  const proxy = http.createServer((req, res) => {
    state.proxy++;
    const u = new URL(req.url);
    const up = http.request({ host: u.hostname, port: u.port, path: u.pathname + u.search, method: req.method, headers: req.headers }, (ur) => {
      res.writeHead(ur.statusCode, ur.headers);
      ur.pipe(res);
    });
    up.on('error', () => { res.writeHead(502); res.end('bad'); });
    req.pipe(up);
  });
  return { state, origin, proxy };
}

const listen = (srv) => new Promise((r) => srv.listen(0, '127.0.0.1', r));

async function main() {
  const sim = startSim();
  await listen(sim.origin);
  await listen(sim.proxy);
  const originPort = sim.origin.address().port;

  const proxiesFile = path.join(os.tmpdir(), `guns-smoke-proxies-${process.pid}.txt`);
  const emptyFile = path.join(os.tmpdir(), `guns-smoke-empty-${process.pid}.txt`);
  fs.writeFileSync(proxiesFile, `127.0.0.1:${sim.proxy.address().port}\n`);
  fs.writeFileSync(emptyFile, '# force tor mode\n');

  const fast = { DWELL_MIN: '400', DWELL_MAX: '700', GAP_MIN: '300', GAP_MAX: '600' };
  let app;

  try {
    app = await launchApp({ PROXY_FILE: proxiesFile, BASE_URL: `http://127.0.0.1:${originPort}`, ...fast });
  } catch (e) {
    console.log(`  FAIL  server boots  — ${e.message}`);
    process.exit(1);
  }
  const { port } = app;

  console.log('\nUI contract');
  const page = await request(port, '/');
  check('GET / serves html', page.status === 200 && /text\/html/.test(page.headers['content-type'] || ''), `status=${page.status}`);
  check('page contains the control UI', page.body.includes('guns.lol view runner') && page.body.includes('id="target"'));
  check('responses are no-store (no stale shell)', /no-store/.test(page.headers['cache-control'] || ''));
  const stripped = page.body.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<script[\s\S]*?<\/script>/g, '');
  check('no external asset refs (nothing to 404)', !/\ssrc=|\shref=/.test(stripped));
  check('unknown route 404s as json', (await request(port, '/nope')).status === 404);
  check('invalid username rejected', (await post(port, '/api/start', { target: 'bad name!!' })).status === 400);
  check('empty username rejected', (await post(port, '/api/start', { target: '' })).status === 400);

  console.log('\nProxy mode');
  const frames = [];
  const sse = http.get({ host: '127.0.0.1', port, path: '/api/events' }, (res) => {
    check('log stream is event-stream', /text\/event-stream/.test(res.headers['content-type'] || ''));
    res.on('data', (d) => frames.push(d.toString()));
  });
  await sleep(300);

  const started = await post(port, '/api/start', { target: 'smokeuser', browsers: 1, pages: 2 });
  check('start accepted', started.status === 200, started.body.slice(0, 140));
  check('mode reported as proxies', JSON.parse(started.body).status.mode === 'proxies');
  check('a second start is refused', (await post(port, '/api/start', { target: 'smokeuser' })).status === 409);

  let st = null;
  for (let i = 0; i < 80; i++) {
    st = JSON.parse((await request(port, '/api/status')).body);
    if (st.hits >= 2 || st.errors > 0) break;
    await sleep(300);
  }
  check('chromium launched and hits landed', st && st.hits >= 2, `hits=${st && st.hits} errors=${st && st.errors} lastError=${st && st.lastError}`);
  check('traffic went through the proxy', sim.state.proxy >= 2, `proxyRequests=${sim.state.proxy}`);
  check('origin served the loads', sim.state.origin >= 2, `originRequests=${sim.state.origin}`);
  check('log stream delivered frames', frames.join('').includes('"type"'));

  const stopped = await post(port, '/api/stop', {});
  check('stop reports not running', stopped.status === 200 && JSON.parse(stopped.body).status.running === false);
  sse.destroy();

  console.log('\nTeardown');
  app.child.kill('SIGTERM');
  for (let i = 0; i < 30 && app.getExit() === null; i++) await sleep(200);
  check('SIGTERM exits the process', app.getExit() !== null, `exit=${app.getExit()}`);
  check('listener closed after shutdown', await refuses(port));

  console.log('\nTor mode');
  let app2;
  try {
    app2 = await launchApp({ PROXY_FILE: emptyFile, ...fast });
  } catch (e) {
    console.log(`  FAIL  tor-mode server boots  — ${e.message}`);
    process.exit(1);
  }
  const torFrames = [];
  const sse2 = http.get({ host: '127.0.0.1', port: app2.port, path: '/api/events' }, (res) => {
    res.on('data', (d) => torFrames.push(d.toString()));
  });
  await sleep(300);

  const tstarted = await post(app2.port, '/api/start', { target: 'torcheck', browsers: 1, pages: 1 });
  check('start accepted with no proxies', tstarted.status === 200);
  check('mode falls back to tor', JSON.parse(tstarted.body).status.mode === 'tor');

  let tst = null;
  for (let i = 0; i < 50; i++) {
    tst = JSON.parse((await request(app2.port, '/api/status')).body);
    if (tst.errors > 0) break;
    await sleep(300);
  }
  const terr = (tst && tst.lastError) || '';
  check('socks target resolved (not undefined)', !/got undefined/.test(terr), terr);
  check('failure is the socks connect, as expected without a daemon', /ERR_PROXY_CONNECTION_FAILED|ECONNREFUSED|socks/i.test(terr), terr);
  const torStream = torFrames.join('');
  check('exit circuit rotation attempted', /circuit rotate failed/.test(torStream));
  check('boot log names the tor egress', /egress TOR socks5:\/\/127\.0\.0\.1:9050/.test(torStream));

  sse2.destroy();
  await post(app2.port, '/api/stop', {});
  app2.child.kill('SIGTERM');
  await sleep(600);

  fs.unlinkSync(proxiesFile);
  fs.unlinkSync(emptyFile);
  sim.origin.close();
  sim.proxy.close();

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error('smoke test error:', e.message); process.exit(1); });
