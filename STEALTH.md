# stealth/ — the Python rig: stealth, speed and the native protocol

Three engines share one CLI, because a guns.lol view has three different costs
depending on what the site is doing to you:

* **`--engine camoufox`** (default) — the stealth rig, built to get *through*:
past Cloudflare, past JS fingerprinting, past the behavioural checks that flag
stock automation.
* **`--engine chromium`** — a fast Chromium fleet: one browser, many contexts,
heavy resources aborted. Built for volume.
* **`--engine guns`** — the native guns.lol protocol: challenge, WASM
proof-of-work, signed POST. No page load, no browser, per view.

Details of the last two are in [Three engines, one CLI](#three-engines-one-cli);
the rest of this document is the stealth rig.

Four layers per Camoufox browser, stacked, plus the glue that runs many of them:

| layer | module | what it does |
|---|---|---|
| identity | `fingerprint.py` | coherent Firefox fingerprint (fpgen → browserforge → builtin) |
| motion | `human.py` | bezier pointer paths, typo-aware typing, eased scroll, idle drift |
| transport | `tls.py` | curl_cffi ClientHello impersonation (JA3/JA4) on the fast HTTP path |
| gating | `cloudflare.py` | detect interstitial, click Turnstile, persist `cf_clearance` |
| fleet | `egress.py` | one solver per egress, warm-first proxy scheduling, learned outcomes |

The browser itself is **Camoufox** — a patched Firefox build. Firefox, not
Chromium, on purpose: Chromium's automation surface is the most heavily
fingerprinted target on the web, and Camoufox patches the tells at the C++ layer
rather than shimming them with JS.

## Install

```bash
pip install -r stealth/requirements.txt
camoufox fetch                       # download the patched Firefox (one time)
```

`camoufox fetch` installs the browser binary, the GeoIP database, and the fpgen
fingerprint model. Without it the first launch fails; the rig says so instead of
hanging.

### Linux: the GTK runtime

Camoufox is a real Firefox, so it links `libgtk-3.so.0` and the X11/NSS stack. A
slim container has none of them and the launch dies with
`XPCOMGlueLoad error ... libgtk-3.so.0 ... Couldn't load XPCOM`. With root:

```bash
apt-get install -y libgtk-3-0
```

Without root, unpack the `.deb`s into the project and point the loader at them —
the same approach the Chromium runner's `npm run deps` takes:

```bash
python stealth/scripts/linux_deps.py
 eval "$(python stealth/scripts/linux_deps.py --print-env)"   # per shell
```

The first command resolves dependencies and extracts into `.foxlibs/`; the second
exports `LD_LIBRARY_PATH` for the current shell. Both are safe to re-run.

## Run

```bash
python -m stealth <username>
python -m stealth <username> --contexts 2 --tabs 3 --proxy-file proxies.txt
python -m stealth <username> --headful --os windows --rounds 5
python -m stealth <username> --virtual        # headed on a private Xvfb display
python -m stealth <username> --launch-retries 2 --no-share-clearance
```

Environment mirrors `bot.js`: `BASE_URL`, `BROWSERS`, `PAGES`, `HEADFUL`,
`DWELL_MIN/MAX`, `GAP_MIN/MAX`, `PROXY_FILE`, `STATE_DIR`, plus
`STEALTH_OS` for the claimed identity OS.

Proxy grammar is identical to the Chromium runner, so a `proxies.txt` works in
both: `host:port`, `host:port:user:pass`, `user:pass@host:port`,
`http(s)://user:pass@host:port`, `socks5://…`.

## Library use

```python
import asyncio

# one browser, in-process — full control
from stealth import StealthConfig, StealthSession

# N browsers, one process each — the fleet
from stealth import StealthFleet

async def single():
    cfg = StealthConfig(target="get", base_url="https://httpbin.org")
    async with StealthSession(cfg) as s:
        await s.hit()
        print(await s.fetch("https://httpbin.org/user-agent"))

async def fleet():
    cfg = StealthConfig(target="someone", contexts=2, tabs=2)
    print(await StealthFleet(cfg, log=lambda t, m: print(t, m)).run(rounds=3))

asyncio.run(fleet())
```

> **`python -m` caveat, handled for you:** fpgen disables itself when it believes
> it is being run as `python -m fpgen`, which is also true of any
> `python -m yourpackage` run. Camoufox builds its fingerprint with fpgen, so the
> rig repairs that false positive (`stealth/_compat.py`) before every launch.

`StealthSession` is one Camoufox browser, one context, one egress, usable
directly in-process. `StealthFleet` runs N of them as **child processes** — not
coroutines — because two Camoufox browsers in one asyncio loop intermittently
stall `new_page()` (the juggler handshake contends and one browser never
returns). A process per worker gives each its own browser, event loop, proxy and
identity; isolation is the fix, and it is how browser farms scale. The proxy is
a launch-time option on Firefox, so a worker cannot share a browser across
proxies anyway.

```
fleet
 ├─ worker process 1.1  → camoufox → proxy A
 ├─ worker process 1.2  → camoufox → proxy B
 └─ worker process 2.1  → camoufox → proxy A (cycled)
```

The parent streams each worker's log lines live (not when the worker exits), so
a launch that takes forty seconds looks like a launch that takes forty seconds.

### Fleet and egress

Two problems only appear once the fleet is bigger than one worker, and both are
in `egress.py`.

**One solver per egress.** When several workers share an egress — a small proxy
pool, or no proxies at all, where every worker is on your own IP — they must not
all work the same challenge at once. Two simultaneous Turnstiles from one IP is a
signal, not a shortcut, and one of the two solves is wasted. `ClearanceBroker`
keeps exactly one solver per *(egress, host)* using an `flock` on
`.stealth-state/cf_<key>.lock`; the rest wait on the clearance file and adopt the
cookie the moment it is published. A file lock is released by the kernel when the
solving process dies, so a worker killed by the fleet watchdog cannot wedge the
egress behind a stale lock. Not sharing is a flag: `--no-share-clearance`.

A cached cookie is never taken on faith. Attaching a stale `cf_clearance`
succeeds silently and clears nothing, so after adopting one the guard watches the
page for a moment; if the interstitial is still there it drops the cookie and
works the challenge for real.

**Choosing an egress.** Egress reputation is the real ceiling, so
`EgressPool` spends what it knows first: egresses already holding a warm
clearance, then the ones idle longest, then ones that have blocked before. A
blocker is used last but never dropped — a small pool has to cycle, and
reputation drifts. Outcomes accumulate in `.stealth-state/egress.json` and the
run ends with a per-egress table:

```
[   63s][done] hits=2 misses=0 mode=direct
[   63s][egress] direct: hits=2 misses=0 solves=0 reused=0 blocks=0
```

**A dead launch is not a dead worker.** On a loaded host Camoufox's launch
dies on its own now and then (`Target page, context or browser has been
closed`) — the slowest, most memory-hungry moment in the whole run. The worker
relaunches once (`--launch-retries`, default 1) after a pause, and the failed
attempt is cleaned up first so a relaunch is never two browsers. A failure
*inside* a round is different: a navigation was genuinely attempted, so it is
counted as a miss and never laundered into a retry. The watchdog still bounds
every worker, so even a browser that hangs rather than dies cannot hang the run.

### Checks

The fleet and egress layers have offline checks — no browser, no network, no
proxies:

```bash
python stealth/scripts/check_rig.py     # 101 checks, exits non-zero on failure
```

They drive the real broker from real child processes (including killing a holder
mid-solve) and the real guard against a scripted page, so the coordination claims
above are verified rather than asserted. Sections H–L (in `check_rig_extra.py`)
cover the fast rails described below, with fixtures captured from the live site:
a real `_gpp_ch` object, a real worker module. The live engine still needs the
browser: run `python -m stealth <user> --contexts 1` for that path.

## Three engines, one CLI

`python -m stealth <user> --engine …`

| engine | what it is | when |
|---|---|---|
| `camoufox` (default) | the stealth rig above, one Firefox per worker | the site fights back and you need to get through |
| `chromium` | a fast Chromium fleet: **one browser, many contexts**, heavy resources aborted | volume — many egresses, few launches |
| `guns` | the native guns.lol protocol: no page load, no browser, per view | the view is the goal and you have tokens |

### chromium — the volume rail

```bash
python -m stealth <user> --engine chromium --contexts 4
python -m stealth <user> --engine chromium --no-fast        # full load, for comparison
```

**A loaded page is not a view, and this engine counts only views.** The site
records one when *its own* beacon POSTs to `/api/analytics/…`, so the session
watches for that response and a hit means a 2xx on it. Measured on the live
target here: a headless Chromium sits on the profile for **67 s**, moving the
mouse and scrolling, and **never issues the beacon** — the view widget needs a
Turnstile solve it cannot finish without a display. The run therefore reports
`views=0 loaded_no_view=1` and says so out loud, instead of calling the load a
view. On a host with a display (or Xvfb) the same engine records views for real,
and the count will say so.

Two things make it fast, and both are measured rather than assumed. A view is
mostly a page load, and most of that load is payload a view cannot use, so
images, webfonts and media are aborted (scripts, XHR and stylesheets are left
alone — the challenge needs those), and navigation resolves at
`domcontentloaded` instead of `load` plus `networkidle`. On the live target from
this box that is **1.7 s vs 1.9 s** per load — the honest number is small,
because this page is only 4–5 heavy requests, and the win grows with the
payload. The bigger win is structural: Chromium accepts a **per-context proxy**,
which Firefox does not, so four egresses cost **one browser launch** instead of
four. Four workers, one browser, one round: ~10 s wall, ~2.5 s/view amortised,
versus a browser launch each.

### Headless Chromium needs a fontconfig (or it aborts)

On a slim host Chromium's Skia font manager kills the whole browser the first
time a page renders text:

    FATAL:third_party/skia/src/ports/SkFontMgr_FontConfigInterface.cpp:163] Not implemented.

That looks exactly like "Cloudflare killed my browser" and is not — it happens on
any text-heavy page. `stealth/chromium.py:ensure_host_env()` pins the browser
cache, puts the locally unpacked shared objects on the loader path, and points
`FONTCONFIG_FILE` at a generated config naming the DejaVu tree inside
`.foxlibs`. The same three exports come out of the dependency helper:

```bash
eval "$(python stealth/scripts/linux_deps.py --print-env)"
```

### guns — the native protocol, no page load

A guns.lol view is not a page load. The profile carries a `_gpp_ch` challenge;
the challenge id names a WASM proof-of-work module under `/_challenge/pow/<id>/`;
and the view is a signed POST to `/api/analytics/view`. `stealth/guns.py` does
all three without a browser, and it *solves the real module*: guns.lol rotates
the worker, the wasm-bindgen glue and the hashed export names, so the worker's
obfuscated glue import is decoded, the glue is parsed for the binary and the
rotated constructor/solve exports, and the binary is driven in `node` by
`stealth/powjs/runner.mjs`. The module is cached by challenge id.

```bash
python -m stealth <user> --engine guns --count 20 --concurrency 4
```

Measured live: page fetch 0.5 s, module fetch 0.9 s (cache hit after the first
view), proof-of-work 0.9–5 s at difficulty 5, POST 0.2 s — **a view in ~3.7 s
with no browser at all**, and it fails with the stage it failed *in*
(`page`/`module`/`pow`/`post`) instead of a bare "miss".

### The one thing that cannot be computed: the POST's Cloudflare gate

Cloudflare's Managed Challenge sits on `/api/analytics/view` itself, **not** on
the profile page. This is measured, not inferred: the page serves 200 to a plain
client, and the very next POST is answered with `403` +
`cf-mitigated: challenge` — from curl_cffi, and from the page's own `fetch`, and
from the browser's request context, with a valid `cf_clearance` present. Clearing
the page does not clear the endpoint.

So a view needs two artifacts from a captcha service (or handed in):

| field | what it is | why it cannot be local |
|---|---|---|
| `cf_clearance` (for the POST path) | a clearance minted for `/api/analytics/view` | Cloudflare only issues it when it actually challenges; it is bound to the exit IP **and** User-Agent |
| Turnstile token | the widget's response token | it is the widget's signature; single-use, per view |

```bash
python -m stealth <user> --engine guns --count 20 --concurrency 4 \
  --captcha-provider capmonster --captcha-key $CAPTCHA_KEY

# tokens minted elsewhere (a browser extension, a different service):
python -m stealth <user> --engine guns --turnstile-token <tok> --cf-clearance <cookie>
```

Three providers are wired, each with its own task shape and its own documented
quirks: `capmonster` (replays the captured interstitial, solves with the UA it is
handed), `solverify` (drives the challenged URL itself, http proxies only,
returns a cookie **jar** plus the UA you must then present) and `uncaptcha`
(single call, `X-Api-Key`, `waf` task). The clearance is cached **per egress**,
so a provider is paid once per egress, not once per view; the Turnstile token is
asked for per view, after the challenge is known, because the widget's `action`
and `data-cdata` come from `_gpp_ch`.

Set `CAPTCHA_PROVIDER` / `CAPTCHA_KEY` to keep the key out of the command line.

### Rotating proxies

A `{session}` placeholder in a proxy's credentials is re-rendered **per view**, so
one proxy line becomes a fresh exit IP per view — the site counts roughly one
view per IP per day, so without it the fleet's second view is wasted. Each
rendered session also gets its own clearance cache key, because a clearance is
bound to the IP that solved it and replaying one across two IPs gets it revoked:

```bash
python -m stealth <user> --engine guns --count 40 \
  --proxies 'http://user:pass_session-{session}_lifetime-30s@geo.iproyal.com:12345'
```

## What each layer actually does

**Identity.** Camoufox generates the fingerprint itself (guided by `os`,
`locale`, `geoip`), which is the most coherent path — it keeps the UA, JS
engine, TLS stack and platform in agreement because they *are* in agreement. The
factory adds a generator tier on top: `fpgen` if installed, then `browserforge`,
then a hand-built coherent identity. It never lets a Chrome UA ride on a Firefox
engine. Pin an identity explicitly with `Identity.as_injection()` when you need
reproducible runs.

**Motion.** Camoufox's `humanize=True` replays recorded *cursor* trajectories.
That is the pointer, and only the pointer. `HumanBehavior` adds bezier mouse
paths with overshoot and tremor, typing with per-key dwell and the odd
typo-then-backspace, eased wheel scrolling, and an idle loop that drifts,
nudges, and blips focus like someone reading. Timings are drawn from ranges, so
no two runs share a timing fingerprint.

**Transport.** The browser path already speaks real Firefox TLS. The leak is the
*fast* path — any `requests`/`httpx` call in the stack emits Python's OpenSSL
ClientHello, and a bot score that sees that next to a Firefox page load flags
the session. `TLSFetch` uses `curl_cffi` to re-issue a real browser's
ClientHello and HTTP/2 frame order, rotates presets per call, and carries the
browser's cookies (including `cf_clearance`) so both paths present one identity.

**Gating.** `CloudflareGuard` detects the interstitial (title markers, header
status, selectors), waits for a managed challenge to self-clear before touching
anything, clicks the Turnstile checkbox inside the cross-origin iframe when it
does not, and persists `cf_clearance` keyed by **proxy + host** in
`.stealth-state/`. The key includes the proxy because `cf_clearance` is bound to
the exit IP — reuse across a different IP is the fastest way to get it revoked.

## Honest limits

- Cloudflare Bot Management scores TLS, ASN, IP reputation, header order,
  cadence and pointer events together. No library guarantees passage. This rig
  removes the automation tells and behaves like a person; a burned IP still
  loses. Pair it with residential/mobile egress.
- `cf_clearance` is short-lived and IP-bound. The cache is a convenience for
  repeated hits through the *same* proxy in a session, not a long-term pass.
- Camoufox's `virtual` headless (headed on Xvfb) is stealthier than headless
  mode on a VPS, where there is no real display to render to.
- Keep Playwright below 1.63 — Camoufox pins the driver interface.
- Concurrency is bounded by RAM, not by the code. Each worker is a real Firefox
  (a few hundred MB, on top of a 1.3 GB install), so ~2 workers on a 4-core/8 GB
  box is the honest number. More workers need more memory; on a small host use
  `--contexts 1`, or plan the fleet for the machine you actually have.
- `--engine guns` was verified **up to** the Cloudflare gate: challenge scrape,
  module fetch, rotation parsing, native proof-of-work and the POST are all
  exercised live, and the POST is refused with an honest stage (`post`). The
  provider-backed path (`--captcha-provider …`) is verified against a local stub
  for every request shape, error path and timeout (checks J1–J19) — it was **not**
  run against a live provider, because no key was available. Treat the first live
  run as the acceptance test for that half.
- The proof-of-work is CPU work: ~1–5 s per view at difficulty 5 on one core.
  Concurrency multiplies it, which is why the native engine is measured in views
  per second per core, not per page load.
- `--engine chromium` on a host **without a display records no views** (measured:
  0 beacons in 67 s). It is a fast, honest page-load engine there — useful for
  proving an egress works, not for counting. Give it Xvfb/headed to count.
- `--engine guns` is the only engine that was verified to reach the analytics
  endpoint with a well-formed, proof-of-work-signed request. It stops at
  Cloudflare's challenge on that path by design; what clears it is a captcha
  token, not more stealth.

## Pyright/type note

The curl_cffi and browserforge imports are guarded; the rig runs with none of
the optional generators installed and says exactly which package is missing when
`camoufox` or `curl_cffi` are absent.
