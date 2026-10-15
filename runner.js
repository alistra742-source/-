'use strict';
/*
 * guns.lol view runner — engine
 * Shared by the CLI (bot.js) and the web UI (server.js).
 *
 * 2 chromium instances x 5 isolated contexts by default. Every context egresses
 * on its own proxy. No proxies -> Tor, with a fresh exit circuit per hit.
 */
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { EventEmitter } = require('events');
const { spawnSync } = require('child_process');

/*
 * Runtime bootstrap — must run BEFORE playwright is required.
 *
 * Playwright resolves its browser cache from HOME, so a browser installed as
 * one user is invisible to another: the classic "Executable doesn't exist at
 * /root/.cache/ms-playwright/...". The cache is therefore pinned to this
 * project, unconditionally — never falling back to a home directory, so the
 * lookup cannot disagree with where the browser was installed.
 *
 * .pwlibs is the same idea for the shared objects chromium links against: on
 * hosts where they cannot be installed system-wide (no root), the packages are
 * extracted here and this puts them ahead of chromium's loader. Absolute path,
 * child processes only — the running node process already resolved its own.
 */
const PROJECT_BROWSERS = path.join(__dirname, '.playwright-browsers');
if (!process.env.PLAYWRIGHT_BROWSERS_PATH) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = PROJECT_BROWSERS;
}
const PROJECT_LIBS_ROOT = path.join(__dirname, '.pwlibs');
const BASE_LD_LIBRARY_PATH = process.env.LD_LIBRARY_PATH || '';

/* Every directory under .pwlibs that actually holds shared objects. The layout
 * differs by distro and architecture (usr/lib/x86_64-linux-gnu, lib/aarch64-…),
 * so walk it instead of assuming a path. */
function collectLibDirs(root, maxDepth = 6) {
  const found = [];
  const walk = (dir, depth) => {
    if (depth > maxDepth || found.length > 64) return;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    let hasLib = false;
    for (const e of entries) {
      if (e.isDirectory()) walk(path.join(dir, e.name), depth + 1);
      else if (/\.so(\.|$)/.test(e.name)) hasLib = true;
    }
    if (hasLib) found.push(dir);
  };
  walk(root, 0);
  return found;
}

/* Rebuildable so it can be re-run after libraries are installed at runtime. */
function applyLibPath() {
  if (process.env.PLAYWRIGHT_SKIP_LOCAL_LIBS) return [];
  const dirs = fs.existsSync(PROJECT_LIBS_ROOT) ? collectLibDirs(PROJECT_LIBS_ROOT) : [];
  const combined = [...dirs, BASE_LD_LIBRARY_PATH].filter(Boolean).join(':');
  if (combined) process.env.LD_LIBRARY_PATH = combined;
  else delete process.env.LD_LIBRARY_PATH;
  return dirs;
}
applyLibPath();

const { chromium } = require('playwright');

const chromiumExecutable = () => chromium.executablePath();

/* Other places a usable cache might already live — reused instead of re-downloading. */
function browserCacheCandidates() {
  const out = [];
  const pinned = process.env.PLAYWRIGHT_BROWSERS_PATH;
  if (pinned && pinned !== '0') out.push(pinned);
  try { out.push(path.join(os.homedir(), '.cache', 'ms-playwright')); } catch { /* no home */ }
  return out;
}

const playwrightBin = () => path.join(__dirname, 'node_modules', '.bin', process.platform === 'win32' ? 'playwright.cmd' : 'playwright');

let ensurePromise = null;

/*
 * Guarantee chromium exists, once per process. Idempotent and race-safe.
 * Order: already installed -> reuse a cache found elsewhere -> download it.
 * Only if all three fail does this throw, and when it throws it names the fix.
 */
function ensureChromium(log = () => {}) {
  if (ensurePromise) return ensurePromise;
  ensurePromise = (async () => {
    const exe = chromiumExecutable();
    if (fs.existsSync(exe)) return exe;

    for (const dir of browserCacheCandidates()) {
      if (dir === PROJECT_BROWSERS || !fs.existsSync(dir)) continue;
      try {
        if (fs.existsSync(PROJECT_BROWSERS)) fs.rmSync(PROJECT_BROWSERS, { recursive: true, force: true });
        fs.symlinkSync(dir, PROJECT_BROWSERS, process.platform === 'win32' ? 'junction' : 'dir');
        if (fs.existsSync(exe)) { log(`reusing existing browser cache at ${dir}`); return exe; }
      } catch { /* fall through to a real install */ }
    }

    const bin = playwrightBin();
    if (fs.existsSync(bin)) {
      log('chromium missing — downloading it now (one time, ~120 MB)…');
      const r = spawnSync(bin, ['install', 'chromium'], {
        stdio: 'inherit',
        env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: PROJECT_BROWSERS },
      });
      if (r.status === 0 && fs.existsSync(exe)) { log('chromium ready'); return exe; }
    }

    ensurePromise = null;   // let a later attempt retry once the user has fixed it
    throw new Error(`chromium is not installed and could not be downloaded — run: npm run setup  (looked in ${exe})`);
  })();
  return ensurePromise;
}

/*
 * The OS libraries chromium links against. A slim container usually has few of
 * them, which shows up as "error while loading shared libraries: libglib-2.0.so
 * .0". Package names differ across releases (Ubuntu 24.04 renamed several to
 * -t64), so both spellings are offered and misses are harmless.
 */
const DEB_PACKAGES = [
  'libglib2.0-0t64', 'libglib2.0-0', 'libnss3', 'libnspr4',
  'libatk1.0-0t64', 'libatk1.0-0', 'libatk-bridge2.0-0t64', 'libatk-bridge2.0-0',
  'libatspi2.0-0t64', 'libatspi2.0-0', 'libcups2t64', 'libcups2',
  'libdrm2', 'libxkbcommon0', 'libxcomposite1', 'libxdamage1', 'libxfixes3',
  'libxrandr2', 'libgbm1', 'libpango-1.0-0', 'libcairo2',
  'libasound2t64', 'libasound2', 'libxi6', 'libxrender1', 'libexpat1',
  'libx11-6', 'libxcb1', 'libxext6', 'libwayland-client0',
];

function hasCmd(cmd) {
  if (process.platform === 'win32') return false;
  return spawnSync('sh', ['-c', `command -v ${cmd}`], { stdio: 'ignore' }).status === 0;
}

/* Ask the binary itself, so this matches what a real launch will do. */
function probeChromium(exe) {
  const r = spawnSync(exe, ['--version'], { encoding: 'utf8', timeout: 20000 });
  const text = `${r.stdout || ''}${r.stderr || ''}`;
  const m = text.match(/error while loading shared libraries: ([^:]+):/);
  if (m) return { ok: false, missing: m[1] };
  if (r.error) return { ok: false, missing: null, error: r.error.message };
  return { ok: r.status === 0, missing: null };
}

/* Never unpack the C library locally: prepending a second glibc to
 * LD_LIBRARY_PATH breaks child processes in ways that look like anything. */
const DEB_SKIP = new Set(['libc6', 'libc6-dev', 'libcrypt1', 'libgcc-s1', 'libgcc1', 'libstdc++6', 'dpkg', 'perl-base']);

/* Direct dependency names of a .deb, reduced to plain package names. */
function debDeps(file) {
  const r = spawnSync('dpkg-deb', ['-f', file, 'Depends'], { encoding: 'utf8' });
  if (r.status !== 0 || !r.stdout) return [];
  return r.stdout
    .split(',')
    .map((s) => s.trim().split('|')[0])            // first alternative only
    .map((s) => s.replace(/\(.*?\)/g, '').trim())   // drop version constraints
    .map((s) => s.split(':')[0].trim())            // drop arch qualifier
    .filter((s) => /^[a-z0-9][a-z0-9+.-]*$/.test(s) && !DEB_SKIP.has(s));
}

/* No root: fetch the packages and unpack them into .pwlibs, following each
 * package's own dependencies — libcups2 needs avahi, and so on. */
function installDepsRootless(log) {
  if (!hasCmd('apt-get') || !hasCmd('dpkg-deb')) return false;
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pw-debs-'));
  try {
    log('downloading chromium system libraries into .pwlibs…');
    const wanted = new Set();
    let queue = DEB_PACKAGES;
    for (let round = 0; round < 5 && queue.length; round++) {
      // One name at a time: apt-get download aborts the whole batch on a single
      // unknown package, and half these names do not exist on any given release.
      for (const pkg of queue) {
        if (wanted.has(pkg) || DEB_SKIP.has(pkg)) continue;
        wanted.add(pkg);
        spawnSync('apt-get', ['download', pkg], { cwd: tmp, stdio: 'ignore' });
      }
      const next = new Set();
      for (const f of fs.readdirSync(tmp).filter((n) => n.endsWith('.deb'))) {
        for (const dep of debDeps(path.join(tmp, f))) if (!wanted.has(dep)) next.add(dep);
      }
      queue = [...next];
      if (queue.length) log(`resolving ${queue.length} dependency package(s)…`);
    }
    const debs = fs.readdirSync(tmp).filter((f) => f.endsWith('.deb'));
    if (!debs.length) { log('no packages could be downloaded (is there an apt index — apt-get update)?'); return false; }
    fs.mkdirSync(PROJECT_LIBS_ROOT, { recursive: true });
    let ok = 0;
    for (const f of debs) {
      if (spawnSync('dpkg-deb', ['-x', path.join(tmp, f), PROJECT_LIBS_ROOT], { stdio: 'ignore' }).status === 0) ok++;
    }
    log(`extracted ${ok}/${debs.length} package(s), dependencies included`);
    return ok > 0;
  } catch (e) {
    log(`could not unpack packages (${e.message})`);
    return false;
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

let depsPromise = null;

/*
 * Guarantee chromium can actually start. Root gets the supported path
 * (playwright install-deps); without root the libraries are unpacked locally.
 * Either way it retries the real binary before giving up.
 */
function ensureChromiumDeps(log = () => {}) {
  if (depsPromise) return depsPromise;
  depsPromise = (async () => {
    const exe = chromiumExecutable();
    let probe = probeChromium(exe);
    if (probe.ok) return true;
    if (!probe.missing) { depsPromise = null; throw new Error(probe.error || 'chromium failed to start'); }

    log(`chromium cannot start — missing ${probe.missing}`);

    if (typeof process.getuid === 'function' && process.getuid() === 0 && fs.existsSync(playwrightBin())) {
      log('installing OS dependencies (playwright install-deps)…');
      spawnSync(playwrightBin(), ['install-deps', 'chromium'], { stdio: 'inherit', env: process.env });
      probe = probeChromium(exe);
      if (probe.ok) { log('chromium can start'); return true; }
      log(`install-deps did not resolve ${probe.missing}`);
    }

    if (installDepsRootless(log)) {
      applyLibPath();
      probe = probeChromium(exe);
      if (probe.ok) { log('chromium can start'); return true; }
    }

    depsPromise = null;
    throw new Error(`chromium is missing system libraries (${probe.missing}) — run: npm run deps  (as root: npx playwright install-deps chromium)`);
  })();
  return depsPromise;
}

const UAS = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
];
const TZS = ['America/New_York', 'Europe/London', 'Europe/Berlin', 'America/Los_Angeles', 'Asia/Tokyo'];
const LANGS = ['en-US,en;q=0.9', 'en-GB,en;q=0.8', 'en-US,en;q=0.9,de;q=0.7'];

const rand = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;
const pick = (arr) => arr[rand(0, arr.length - 1)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const { ensureTor } = require('./scripts/tor');
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, Number(n) || lo));

/* ---------- proxy parsing ---------- */
function fmtProxy(scheme, host, port, user, pass) {
  const s = scheme.toLowerCase() === 'socks5h' ? 'socks5' : scheme.toLowerCase();
  return { server: `${s}://${host}:${port}`, username: user || undefined, password: pass || undefined, label: `${host}:${port}` };
}

function parseProxyLine(raw) {
  const line = String(raw).trim();
  if (!line || line.startsWith('#')) return null;

  let m = line.match(/^(?<scheme>https?|socks5h?|socks4):\/\/(?:(?<user>[^:@/]+):(?<pass>[^@/]+)@)?(?<host>[^:/]+):(?<port>\d+)$/i);
  if (m) return fmtProxy(m.groups.scheme, m.groups.host, m.groups.port, m.groups.user, m.groups.pass);

  m = line.match(/^(?<host>[^:]+):(?<port>\d+):(?<user>[^:]+):(?<pass>.+)$/);
  if (m) return fmtProxy('http', m.groups.host, m.groups.port, m.groups.user, m.groups.pass);

  m = line.match(/^(?<user>[^:@/]+):(?<pass>[^@/]+)@(?<host>[^:/]+):(?<port>\d+)$/);
  if (m) return fmtProxy('http', m.groups.host, m.groups.port, m.groups.user, m.groups.pass);

  m = line.match(/^(?<host>[^:]+):(?<port>\d+)$/);
  if (m) return fmtProxy('http', m.groups.host, m.groups.port);

  return null;
}

function parseProxyText(text) {
  const out = [];
  const bad = [];
  for (const line of String(text || '').split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const p = parseProxyLine(line);
    if (!p) bad.push(line.trim());
    else if (/^socks/.test(p.server) && p.username) {
      // Chromium cannot answer SOCKS5 username/password, so this line can never connect.
      bad.push(`${line.trim()} (chromium cannot log in to SOCKS5 — use an http proxy with user:pass)`);
    } else out.push(p);
  }
  return { proxies: out, bad };
}

function loadProxies(file) {
  if (!file || !fs.existsSync(file)) return { proxies: [], bad: [] };
  return parseProxyText(fs.readFileSync(file, 'utf8'));
}

/* host/port out of a socks URL, so the daemon can be probed before use. */
function socksEndpoint(url) {
  try {
    const u = new URL(url);
    return { host: u.hostname || '127.0.0.1', port: Number(u.port) || 9050 };
  } catch {
    return { host: '127.0.0.1', port: 9050 };
  }
}

function probeTcp(host, port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port });
    const done = (ok) => { sock.destroy(); resolve(ok); };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => done(true));
    sock.once('timeout', () => done(false));
    sock.once('error', () => done(false));
  });
}

/* ---------- runner ---------- */
const DEFAULTS = {
  baseUrl: 'https://guns.lol',
  browsers: 2,
  pages: 3,
  headless: true,
  dwell: [9000, 22000],
  roundGap: [20000, 60000],
  torSocks: 'socks5://127.0.0.1:9050',
  torControlHost: '127.0.0.1',
  torControlPort: 9051,
  torControlPass: null,
  navTimeout: 45000,
};

class Runner extends EventEmitter {
  constructor(defaults = {}) {
    super();
    // An explicit `undefined` must not clobber a real default — callers pass
    // env-driven options that are often unset (TOR_SOCKS, TOR_PASSWORD).
    const overrides = Object.fromEntries(
      Object.entries(defaults).filter(([, v]) => v !== undefined),
    );
    this.defaults = { ...DEFAULTS, ...overrides };
    this._clear();
  }

  /* latest cam frame for a worker id like "1.2", or null */
  frame(id) {
    return this.frames.get(id) || null;
  }

  _clear() {
    this.browsers = [];
    this.frames = new Map();   // worker id -> { jpg, at }, the live cam
    this.workerHandles = [];
    this.stopping = false;
    this.running = false;
    this.hits = 0;
    this.errors = 0;
    this.startedAt = null;
    this.lastError = null;
    this.plan = null;
    this.torControlDown = false;
    this.torWarned = false;
  }

  status() {
    return {
      running: this.running,
      url: this.plan ? this.plan.url : null,
      target: this.plan ? this.plan.target : null,
      mode: this.plan ? this.plan.mode : null,          // 'proxies' | 'tor'
      proxyCount: this.plan ? this.plan.proxyCount : 0,
      browsers: this.plan ? this.plan.browsers : this.defaults.browsers,
      pages: this.plan ? this.plan.pages : this.defaults.pages,
      headless: this.plan ? this.plan.headless : this.defaults.headless,
      hits: this.hits,
      errors: this.errors,
      startedAt: this.startedAt,
      uptime: this.startedAt ? Date.now() - this.startedAt : 0,
      lastError: this.lastError,
    };
  }

  log(level, tag, msg) {
    this.emit('log', { t: Date.now(), level, tag, msg, hits: this.hits, errors: this.errors });
  }

  _emitState() {
    this.emit('state', this.status());
  }

  /* Tor control port: force a fresh exit circuit. */
  _torNewCircuit() {
    const o = this.defaults;
    return new Promise((resolve, reject) => {
      const sock = net.connect({ host: o.torControlHost, port: o.torControlPort });
      let buf = '';
      let stage = 0;
      const done = (err) => { sock.destroy(); err ? reject(err) : resolve(); };
      sock.setTimeout(6000);
      sock.on('timeout', () => done(new Error('tor control timeout')));
      sock.on('error', (e) => done(e));
      sock.on('connect', () => {
        sock.write((o.torControlPass ? `AUTHENTICATE "${o.torControlPass}"` : 'AUTHENTICATE') + '\r\n');
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

  async _newIdentity() {
    // Known-dead control port: don't burn ten timed-out connects on it.
    if (this.torControlDown) return;
    try { await this._torNewCircuit(); await sleep(1500); }
    catch (e) {
      if (this.torWarned) return;      // once per run, not once per worker
      this.torWarned = true;
      this.log('warn', 'tor', `cannot rotate exit circuits (${e.message}) — every hit shares one exit`);
    }
  }

  async _dwell(page) {
    const [lo, hi] = this.plan.dwell;
    const end = Date.now() + rand(lo, hi);
    while (Date.now() < end && !this.stopping) {
      await page.mouse.move(rand(0, 900), rand(0, 700), { steps: rand(4, 12) }).catch(() => {});
      await page.evaluate(() => window.scrollBy(0, Math.floor(Math.random() * 400) - 150)).catch(() => {});
      await sleep(rand(700, 2500));
    }
  }

  async _worker(browser, proxy, id) {
    const [gapLo, gapHi] = this.plan.roundGap;
    for (;;) {
      if (this.stopping) return;
      let context;
      let cam = null;
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
            : { server: this.plan.torSocks },
        };

        if (!proxy) await this._newIdentity();

        context = await browser.newContext(opts);
        const page = await context.newPage();
        // Live cam: a small JPEG of this page every second, skipped while one is still in flight.
        let shooting = false;
        cam = setInterval(() => {
          if (shooting || this.stopping) return;
          shooting = true;
          page.screenshot({ type: 'jpeg', quality: 55, scale: 'css', timeout: 5000 })
            .then((jpg) => { if (!this.stopping) this.frames.set(id, { jpg, at: Date.now() }); })
            .catch(() => {})
            .finally(() => { shooting = false; });
        }, 1000);
        await page.addInitScript(() => {
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
          window.chrome = window.chrome || { runtime: {} };
        });

        await page.goto(this.plan.url, { waitUntil: 'load', timeout: this.plan.navTimeout });
        await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {});
        await this._dwell(page);

        if (this.stopping) return;
        this.hits++;
        this.log('hit', `p${id}${proxy ? ' ' + proxy.label : ' tor'}`, 'view sent');
        this.emit('hit', { worker: id, proxy: proxy ? proxy.label : 'tor', hits: this.hits });
        this._emitState();
      } catch (e) {
        if (this.stopping) return;
        this.errors++;
        this.lastError = String(e.message).split('\n')[0];
        this.log('miss', `p${id}`, `miss: ${this.lastError}`);
        this._emitState();
      } finally {
        if (cam) clearInterval(cam);
        if (context) await context.close().catch(() => {});
      }
      await sleep(rand(gapLo, gapHi));
    }
  }

  async start(opts = {}) {
    if (this.running) throw new Error('already running');
    await ensureChromium((m) => this.log('info', 'boot', m));
    await ensureChromiumDeps((m) => this.log('info', 'boot', m));
    const o = { ...this.defaults, ...opts };

    const target = String(opts.target || '').trim().replace(/^\/+|\/+$/g, '');
    if (!target) throw new Error('target required');
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(target)) throw new Error('target has invalid characters');

    // proxies: explicit text wins, else the proxies file, else Tor
    let proxies = [];
    let bad = [];
    if (opts.proxies && String(opts.proxies).trim()) {
      ({ proxies, bad } = parseProxyText(opts.proxies));
    } else if (o.proxyFile) {
      ({ proxies, bad } = loadProxies(o.proxyFile));
    }

    // Pasted text that yielded no usable proxy must not silently become Tor.
    if (!proxies.length && String(opts.proxies || '').trim()) {
      throw new Error(`no usable proxy in the box — ${bad.length} line(s) rejected, e.g. "${bad[0] || ''}". Use host:port, host:port:user:pass, user:pass@host:port or http://user:pass@host:port`);
    }

    // No proxies at all: run over Tor. Start a daemon ourselves when none is up.
    let torControlDown = false;
    if (!proxies.length) {
      const tor = await ensureTor({
        socksPort: Number(socksEndpoint(o.torSocks).port),
        controlPort: o.torControlPort,
        log: (m) => this.log('info', 'tor', m),
      });
      torControlDown = !tor.control;
    }

    // Pasted or file proxies: confirm each host:port answers before any browser
    // launches, so a dead proxy is named once instead of failing every hit.
    if (proxies.length) {
      const endpoints = [...new Map(proxies.map((p) => {
        const u = new URL(p.server);
        return [`${u.hostname}:${u.port}`, { host: u.hostname, port: Number(u.port) }];
      })).values()];
      const results = await Promise.all(endpoints.map((e) => probeTcp(e.host, e.port).then((ok) => ({ ...e, ok }))));
      const up = results.filter((r) => r.ok);
      for (const r of results.filter((x) => !x.ok)) {
        this.log('warn', 'boot', `proxy ${r.host}:${r.port} is not reachable — check the address or that the proxy is online`);
      }
      if (!up.length) {
        throw new Error(`none of the ${endpoints.length} proxy address(es) accept a connection — check host, port and that the proxy is online`);
      }
      const liveKeys = new Set(up.map((r) => `${r.host}:${r.port}`));
      proxies = proxies.filter((p) => liveKeys.has(new URL(p.server).host));
    }

    const browsers = clamp(opts.browsers ?? o.browsers, 1, 8);
    const pages = clamp(opts.pages ?? o.pages, 1, 10);
    const headless = opts.headless === undefined ? o.headless : !!opts.headless;

    this._clear();
    this.plan = {
      target,
      url: `${String(o.baseUrl).replace(/\/+$/, '')}/${target}`,
      mode: proxies.length ? 'proxies' : 'tor',
      proxyCount: proxies.length,
      browsers,
      pages,
      headless,
      dwell: o.dwell,
      roundGap: o.roundGap,
      navTimeout: o.navTimeout,
      torSocks: o.torSocks,
    };
    this.torControlDown = torControlDown;

    this.running = true;
    this.startedAt = Date.now();
    this.log('info', 'boot', `target ${this.plan.url}`);
    this.log('info', 'boot', `${browsers} browsers x ${pages} pages = ${browsers * pages} workers`);
    this.log('info', 'boot', this.plan.mode === 'tor'
      ? `egress TOR ${this.plan.torSocks} (fresh circuit per hit)`
      : `egress ${proxies.length} proxies (round-robin)`);
    if (this.plan.mode === 'tor' && torControlDown) {
      this.log('warn', 'tor', `control port ${o.torControlHost}:${o.torControlPort} not open — every hit shares one exit (add ControlPort to torrc to rotate)`);
    }
    if (bad.length) this.log('warn', 'boot', `${bad.length} proxy line(s) unparsed and ignored`);
    this._emitState();

    let slot = 0;
    try {
      for (let b = 0; b < browsers; b++) {
        const browser = await chromium.launch({
          headless,
          args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage'],
        });
        if (this.stopping) { await browser.close().catch(() => {}); break; }
        this.browsers.push(browser);
        for (let p = 0; p < pages; p++) {
          const proxy = this.plan.mode === 'tor' ? null : proxies[slot++ % proxies.length];
          const handle = this._worker(browser, proxy, `${b + 1}.${p + 1}`)
            .catch((e) => this.log('error', 'worker', e.message));
          this.workerHandles.push(handle);
        }
      }
    } catch (e) {
      // Roll back so the UI never claims a run that never launched, and turn
      // playwright's cache-miss stack trace into the one command that fixes it.
      this.stopping = true;
      await Promise.allSettled(this.browsers.map((x) => x.close()));
      this._clear();
      this.log('error', 'boot', e.message.split('\n')[0]);
      this._emitState();
      if (/Executable doesn't exist|ENOENT/i.test(e.message)) {
        throw new Error(`chromium is not installed — run: npm run setup  (looked in ${chromium.executablePath()})`);
      }
      const lib = e.message.match(/error while loading shared libraries: ([^:]+):/);
      if (lib) {
        throw new Error(`chromium is missing the system library ${lib[1]} — run: npm run deps  (as root: npx playwright install-deps chromium)`);
      }
      throw e;
    }
    return this.status();
  }

  async stop() {
    if (!this.running) return this.status();
    this.stopping = true;
    this.running = false;
    this.log('info', 'stop', `closing ${this.browsers.length} browsers — ${this.hits} hits sent`);
    await Promise.allSettled(this.browsers.map((b) => b.close()));
    this.browsers = [];
    this.workerHandles = [];
    this.stopping = false;
    this.log('info', 'stop', 'all workers down');
    this._emitState();
    return this.status();
  }
}

module.exports = {
  Runner, parseProxyLine, parseProxyText, loadProxies,
  ensureChromium, ensureChromiumDeps, chromiumExecutable, PROJECT_BROWSERS,
};
