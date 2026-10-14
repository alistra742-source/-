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
- `PLAYWRIGHT_SKIP_LOCAL_LIBS` — ignore `.pwlibs`

## If something breaks

```bash
npm test        # tells you whether it is the environment or the code
npm run deps    # repairs missing chromium libraries
npm run tor     # no proxies? this is the missing piece
```
