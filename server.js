'use strict';
/*
 * guns.lol view runner — web UI server
 * Serves the single-file front end and drives the shared engine.
 *
 * env:
 *   PORT / PORTS   comma list, default 8080   e.g. PORTS=8080,3002
 *   HOST           default 0.0.0.0
 *   PROXY_FILE     default ./proxies.txt (used when the UI proxy box is empty)
 *   BASE_URL       default https://guns.lol
 *   DWELL_MIN/DWELL_MAX, GAP_MIN/GAP_MAX, TOR_SOCKS, TOR_PORT, TOR_PASSWORD
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { Runner, ensureChromium, chromiumExecutable } = require('./runner');

// Port 0 is valid ("bind any free port") and must survive the filter.
const PORTS = String(process.env.PORT || process.env.PORTS || '8080')
  .split(',').map((s) => Number(s.trim()))
  .filter((n) => Number.isInteger(n) && n >= 0 && n <= 65535);
if (!PORTS.length) PORTS.push(8080);
const HOST = process.env.HOST || '0.0.0.0';
const BASE_URL = process.env.BASE_URL || 'https://guns.lol';
const PROXY_FILE = process.env.PROXY_FILE || path.join(__dirname, 'proxies.txt');
const INDEX = path.join(__dirname, 'public', 'index.html');

const runner = new Runner({
  baseUrl: BASE_URL,
  proxyFile: PROXY_FILE,
  dwell: [Number(process.env.DWELL_MIN || 9000), Number(process.env.DWELL_MAX || 22000)],
  roundGap: [Number(process.env.GAP_MIN || 20000), Number(process.env.GAP_MAX || 60000)],
  torSocks: process.env.TOR_SOCKS || undefined,
  torControlPort: Number(process.env.TOR_PORT || 9051),
  torControlPass: process.env.TOR_PASSWORD || null,
});
const sseClients = new Set();
let shuttingDown = false;

function broadcast(type, data) {
  const payload = `data: ${JSON.stringify({ type, data })}\n\n`;
  for (const res of sseClients) {
    try { res.write(payload); } catch { /* client vanished; cleaned up on close */ }
  }
}
runner.on('log', (e) => broadcast('log', e));
runner.on('state', (s) => broadcast('state', s));

function json(res, code, body) {
  const buf = Buffer.from(JSON.stringify(body));
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': buf.length,
    'cache-control': 'no-store',
  });
  res.end(buf);
}

function readBody(req, limit = 256 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) { reject(new Error('body too large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

// no-store everywhere: a cached shell must never outlive the build that made it.
function serveIndex(res) {
  fs.readFile(INDEX, (err, buf) => {
    if (err) {
      const msg = Buffer.from('index.html missing — run from the project root');
      res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8', 'content-length': msg.length, 'cache-control': 'no-store' });
      res.end(msg);
      return;
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'content-length': buf.length, 'cache-control': 'no-store' });
    res.end(buf);
  });
}

async function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const route = `${req.method} ${url.pathname}`;

  if (route === 'GET /' || route === 'GET /index.html') return serveIndex(res);
  if (route === 'GET /health') return json(res, 200, { ok: true, chromium: fs.existsSync(chromiumExecutable()), ...runner.status() });
  if (route === 'GET /api/status') return json(res, 200, runner.status());

  if (route === 'GET /api/events') {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-store',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    });
    res.write(`data: ${JSON.stringify({ type: 'state', data: runner.status() })}\n\n`);
    sseClients.add(res);
    const ping = setInterval(() => { try { res.write(': ping\n\n'); } catch { /* ignore */ } }, 20000);
    req.on('close', () => { clearInterval(ping); sseClients.delete(res); });
    return;
  }

  if (route === 'POST /api/start') {
    let body;
    try { body = JSON.parse((await readBody(req)) || '{}'); }
    catch { return json(res, 400, { error: 'invalid json body' }); }
    if (runner.status().running) return json(res, 409, { error: 'a run is already in progress' });
    try {
      const status = await runner.start({
        target: body.target,
        proxies: body.proxies,
        browsers: body.browsers,
        pages: body.pages,
        headless: body.headless,
      });
      return json(res, 200, { ok: true, status });
    } catch (e) {
      return json(res, 400, { error: e.message });
    }
  }

  if (route === 'POST /api/stop') {
    const status = await runner.stop();
    return json(res, 200, { ok: true, status });
  }

  res.writeHead(404, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(JSON.stringify({ error: 'not found' }));
}

// Warm the browser at boot so the first Start click is not a two-minute stall.
// Non-fatal: the server still serves the UI, and /api/start reports the fix.
ensureChromium((m) => console.log(`[setup] ${m}`))
  .then(() => console.log('[serve] chromium ready'))
  .catch((e) => console.warn(`[serve] ${e.message}`));

// One http.Server per port: a single Server cannot listen on two ports at once,
// and the second listen() silently leaves the first unbound.
const onRequest = (req, res) => {
  handle(req, res).catch((e) => {
    if (!res.headersSent) json(res, 500, { error: e.message });
  });
};

const servers = [];
let bound = 0;
for (const port of PORTS) {
  const s = http.createServer(onRequest);
  s.on('error', (e) => {
    console.error(`[serve] port ${port} failed: ${e.message}`);
    // only fatal when nothing at all came up
    if (servers.every((x) => !x.listening)) process.exit(1);
  });
  s.listen(port, HOST, () => {
    bound++;
    // report the port actually bound, so PORT=0 is usable
    console.log(`[serve] http://${HOST}:${s.address().port}  (ui)`);
    if (bound === PORTS.length) console.log(`[serve] engine ready — proxy file: ${PROXY_FILE}`);
  });
  servers.push(s);
}

// Teardown order: stop producers (browsers), then the listener.
async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log('\n[serve] shutting down');
  for (const res of sseClients) { try { res.end(); } catch { /* ignore */ } }
  sseClients.clear();
  await runner.stop().catch(() => {});
  let open = servers.length;
  if (!open) process.exit(0);
  for (const s of servers) s.close(() => { if (--open === 0) process.exit(0); });
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
