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
const net = require('net');
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

  // No daemon at all: refuse up front rather than launch ten browsers that cannot connect.
  // TOR_BIN points at a missing binary so this is deterministic: the runner tries to
  // start Tor itself, finds none, and must refuse with the install fix.
  const appDown = await launchApp({ PROXY_FILE: emptyFile, TOR_BIN: '/nonexistent/tor', ...fast });
  const refused = await post(appDown.port, '/api/start', { target: 'torcheck', browsers: 2, pages: 5 });
  const refusedBody = JSON.parse(refused.body);
  check('no daemon: run refused with 400', refused.status === 400, `${refused.status} ${refused.body.slice(0, 90)}`);
  check('no daemon: message says tor is missing and offers the paste fix',
    /tor binary is not installed/.test(refusedBody.error || '') && /paste proxies/.test(refusedBody.error || ''),
    refusedBody.error);
  check('no daemon: nothing was launched', !/"running":true/.test(refused.body));
  appDown.child.kill('SIGTERM');
  await sleep(600);

  // With a daemon present, tor mode engages and exits rotate over the control port.
  const controlSeen = [];
  const fakeControl = net.createServer((sock) => {
    sock.on('error', () => {});
    let buf = '';
    sock.on('data', (d) => {
      buf += d.toString();
      let i;
      while ((i = buf.indexOf('\r\n')) !== -1) {
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line) continue;
        controlSeen.push(line.split(' ')[0]);
        sock.write('250 OK\r\n');
        if (line.startsWith('QUIT')) sock.end();
      }
    });
  });
  // Accepts the TCP connect (enough for the preflight) then drops it, so the
  // navigation fails promptly instead of waiting out a SOCKS handshake.
  const fakeSocks = net.createServer((sock) => { sock.on('error', () => {}); sock.destroy(); });
  await new Promise((r) => fakeControl.listen(0, '127.0.0.1', r));
  await new Promise((r) => fakeSocks.listen(0, '127.0.0.1', r));
  const socksUrl = `socks5://127.0.0.1:${fakeSocks.address().port}`;

  const app2 = await launchApp({
    PROXY_FILE: emptyFile,
    TOR_SOCKS: socksUrl,
    TOR_PORT: String(fakeControl.address().port),
    ...fast,
  });
  const torFrames = [];
  const sse2 = http.get({ host: '127.0.0.1', port: app2.port, path: '/api/events' }, (res) => {
    res.on('data', (d) => torFrames.push(d.toString()));
  });
  await sleep(300);

  const tstarted = await post(app2.port, '/api/start', { target: 'torcheck', browsers: 1, pages: 1 });
  check('daemon present: start accepted', tstarted.status === 200, tstarted.body.slice(0, 110));
  check('mode falls back to tor', tstarted.status === 200 && JSON.parse(tstarted.body).status.mode === 'tor');

  let tst = null;
  for (let i = 0; i < 50; i++) {
    tst = JSON.parse((await request(app2.port, '/api/status')).body);
    if (tst.errors > 0) break;
    await sleep(300);
  }
  const terr = (tst && tst.lastError) || '';
  check('socks target resolved (not undefined)', !/got undefined/.test(terr), terr);
  // Which connection error chromium reports varies (proxy refused vs reset);
  // what matters is that it is a real connection failure, not a bad proxy option.
  check('navigation fails at the stand-in socks, as expected',
    /ERR_(PROXY_|SOCKS_)?CONNECTION_(RESET|FAILED|CLOSED)|ECONNREFUSED|ERR_PROXY|ERR_SOCKS/i.test(terr), terr);
  check('exit circuits rotated over the control port',
    controlSeen.includes('AUTHENTICATE') && controlSeen.includes('SIGNAL'), JSON.stringify(controlSeen.slice(0, 6)));
  check('boot log names the tor egress', /egress TOR socks5:\/\/127\.0\.0\.1:/.test(torFrames.join('')));

  sse2.destroy();
  await post(app2.port, '/api/stop', {});
  app2.child.kill('SIGTERM');
  await sleep(600);
  fakeControl.close();
  fakeSocks.close();

  // Pasted proxies that nothing answers: refused before any browser launches.
  console.log('\nPasted proxies');
  const deadApp = await launchApp({ PROXY_FILE: emptyFile, ...fast });
  const dead = await post(deadApp.port, '/api/start', { target: 'deadproxy', proxies: '127.0.0.1:1' });
  check('dead pasted proxy: refused with 400', dead.status === 400, `${dead.status} ${dead.body.slice(0, 90)}`);
  check('dead pasted proxy: message names the reachability fault',
    /accept a connection/.test(JSON.parse(dead.body).error || ''), dead.body.slice(0, 160));
  // An unparseable paste must not silently become Tor.
  const junk = await post(deadApp.port, '/api/start', { target: 'deadproxy', proxies: 'not a proxy at all' });
  check('unparseable paste: refused, not routed to tor', junk.status === 400 && /no usable proxy/.test(JSON.parse(junk.body).error || ''),
    junk.body.slice(0, 160));
  deadApp.child.kill('SIGTERM');
  await sleep(600);

  fs.unlinkSync(proxiesFile);
  fs.unlinkSync(emptyFile);
  sim.origin.close();
  sim.proxy.close();

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error('smoke test error:', e.message); process.exit(1); });
