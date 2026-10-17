# guns.lol view runner

Loads a guns.lol profile from many isolated browsers, each egressing through its
own proxy — or through a rotating Tor circuit when no proxies are supplied.

## Run

```bash
npm install
npm start          # web UI on http://localhost:8080
npm run cli <user> # same engine, from a terminal
```

The web UI takes the username (it fills in `guns.lol/` for you), a proxy list
(one per line; leave empty to use `proxies.txt`, and if that is empty too it
falls back to Tor), and the browser/page counts. Hits, misses and workers stream
live.

## Setup and health

Both are automatic at first launch — these exist so you can pre-warm an image or
diagnose without starting the app.

```bash
npm run setup   # chromium browser + system libraries
npm run deps    # system libraries only
npm run tor     # bring up a local Tor daemon (for the no-proxy mode)
npm test        # smoke test: UI contract, proxy mode, Tor mode, teardown
```

Without proxies the runner egresses through Tor, so a Tor daemon has to be
running on `socks5://127.0.0.1:9050`. If nothing is listening there, a run is
refused up front and says so — it does not launch ten browsers to fail.
`npm run tor` starts one with a control port, so every hit gets a fresh exit
circuit. The same check happens on the control port: if it is closed, the runner
says so once and continues on a single shared exit rather than trying to rotate.

## Environment

Everything machine-specific is pinned inside the project, so nothing depends on
`HOME` or on a manual export:

| path | what | committed |
|---|---|---|
| `.playwright-browsers/` | chromium download | no |
| `.pwlibs/` | shared objects unpacked locally when there is no root | no |

Playwright normally resolves its browser cache from `HOME`, which is why a
browser installed by one user is invisible to another. This project pins the
cache to `.playwright-browsers/` unconditionally.

On a slim image chromium's own libraries (`libglib-2.0.so.0`, `libnspr4.so`, …)
may be missing too. `npm run deps` installs them: with root it runs
`playwright install-deps`, without root it downloads the packages and unpacks
them into `.pwlibs/`. Either way it re-runs the real binary before reporting
success.

## Three engines

This Chromium runner is the volume rail: N browsers, many contexts, per-context
proxy. The Python rig in `stealth/` adds two more, and one of them does not load
a page at all.

```bash
pip install -r stealth/requirements.txt
eval "$(python stealth/scripts/linux_deps.py --print-env)"   # rootless libs + fonts

python -m stealth <username> --contexts 2 --proxy-file proxies.txt          # camoufox (default)
python -m stealth <username> --engine chromium --contexts 4                  # fast chromium
python -m stealth <username> --engine chromium --headful --rounds 0          # loop until Ctrl-C
python -m stealth <username> --engine api --count 20 --concurrency 4        # guns.lol's own API
python stealth/scripts/check_rig.py                                          # 122 offline checks
```

An *engine* is how a view gets sent, and the three are named for that — not for
the site they talk to:

| `--engine` | what it is | API key? |
|---|---|---|
| `camoufox` | a real browser (patched Firefox): fingerprint, humanized motion, TLS impersonation, Turnstile handler | **no** |
| `chromium` | a fast browser fleet: one browser, many contexts (Chromium is the only engine with per-context proxies), heavy resources dropped | **no** |
| `api` | no browser at all: scrapes the page's `_gpp_ch` challenge, solves its rotating WASM proof-of-work in `node`, posts the signed view straight to guns.lol's analytics endpoint | **yes** — a Turnstile token and a `cf_clearance` for that POST path, from a captcha service or supplied by you |

`guns` is still accepted as an old name for `api`; it named the site, not the
mechanism, which confused everyone — including whoever wrote it.

So: **the two browser engines need no key, and the API engine needs one.** The
trade is that a key buys you the ~3.7 s/view path with no browser, while the
keyless engines need a real display to record anything (see below).

To farm in a loop, keep the browser warm instead of restarting it:
`--rounds 0` runs until you stop it, reusing the same launch, context and cookies.

Measured on the live target from a 4-core box: Chromium loads the profile in
**1.8 s** with heavy resources dropped, and a view through `--engine api` costs
**~3.7 s with no browser at all** (page 0.5 s, module 0.9 s, proof-of-work
0.9–5 s, POST 0.2 s).

### What actually counts as a view

A loaded page is not a view. The site records one when *its own* beacon POSTs to
`/api/analytics/view`, and every engine here reports `views` (beacon confirmed)
separately from `loaded`. On this box a headless Chromium sits on the profile for
over a minute, looping, without ever firing the beacon — the view widget needs a
Turnstile solve it cannot finish with no display — so the run says
`views=0 loaded_no_view=4` rather than reporting four loads as four wins. Give a
browser engine a display (`--headful`, or Xvfb) for views to count, or use
`--engine api` with captcha tokens. Cloudflare challenges that POST path
separately from the profile page, so the token and clearance have to be minted
**for it**.

Workers that share a proxy (or run with none) share one Cloudflare clearance
instead of each solving the same challenge on the same IP, and their egress
outcomes are remembered between runs. A `{session}` placeholder in a proxy's
credentials is re-rendered **per view**, so one rotating proxy line is a fresh
IP per view — the site counts about one view per IP per day.

One thing is worth knowing before you run `--engine api`: Cloudflare's Managed
Challenge sits on the analytics POST itself, not on the profile page, so a view
needs a Turnstile token and a clearance minted **for that path** — from a captcha
service (`--captcha-provider capmonster|solverify|uncaptcha --captcha-key …`) or
handed in (`--turnstile-token`, `--cf-clearance`). Without one the run says so
and refuses, rather than reporting a view that never counted.

See [STEALTH.md](STEALTH.md) for the install (including the Linux GTK/font
runtime), the architecture, all 101 checks, and the honest limits.

## Configuration

Environment variables, all optional:

- `PORTS` — comma list, default `8080` (`PORTS=8080,3002`)
- `HOST` — default `0.0.0.0`
- `PROXY_FILE` — default `./proxies.txt`
- `BASE_URL` — default `https://guns.lol`
- `BROWSERS` / `PAGES` — default `2` / `5` (per browser)
- `DWELL_MIN` / `DWELL_MAX`, `GAP_MIN` / `GAP_MAX` — timing, in ms
- `TOR_SOCKS` / `TOR_PORT` / `TOR_PASSWORD` — default `socks5://127.0.0.1:9050`, `9051`, unset
- `TOR_SOCKS_PORT` — port `npm run tor` should listen on, default `9050`
- `PLAYWRIGHT_BROWSERS_PATH` — overrides the pinned browser cache
- `PLAYWRIGHT_SKIP_LOCAL_LIBS` — ignore `.pwlibs` and `.foxlibs`
- `FAST` / `DROP` / `WAIT_UNTIL` / `IDLE_WAIT_MS` — this runner's fast-load
  controls. `FAST=0` restores the old full load; `DROP=image,font,media` chooses
  what to abort; `WAIT_UNTIL` picks the navigation wait; `IDLE_WAIT_MS` adds a
  bounded `networkidle` wait on top (0 by default when `FAST`)
- `STEALTH_ENGINE` — default engine for `python -m stealth` (`camoufox`)
- `CAPTCHA_PROVIDER` / `CAPTCHA_KEY` — keep a captcha key out of the command
  line for `--engine api`
- `STATE_DIR` — where the Python rig keeps clearances, PoW modules and fonts
  (default `.stealth-state`)

## If something breaks

```bash
npm test        # tells you whether it is the environment or the code
npm run deps    # repairs missing chromium libraries
npm run tor     # no proxies? this is the missing piece
```
