# stealth/ — the Camoufox rig

An insane-stealth browser engine that sits beside the Chromium runner in
`runner.js`. The Chromium runner is built for volume through cheap proxies; this
one is built to get *through* — past Cloudflare, past JS fingerprinting, past
the behavioural checks that flag stock automation.

Four layers per browser, stacked, plus the glue that runs many of them:

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
python stealth/scripts/check_rig.py
```

They drive the real broker from real child processes (including killing a holder
mid-solve) and the real guard against a scripted page, so the coordination claims
above are verified rather than asserted. The live engine still needs the browser:
run `python -m stealth <user> --contexts 1` for that path.

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

## Pyright/type note

The curl_cffi and browserforge imports are guarded; the rig runs with none of
the optional generators installed and says exactly which package is missing when
`camoufox` or `curl_cffi` are absent.
