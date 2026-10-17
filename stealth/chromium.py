"""A fast Chromium engine — the volume rail next to the Camoufox rig.

Two facts drive this module.

**Chromium is much faster at loading this target, and a view is mostly loading.**
The profile is a Next.js bundle; `load` + `networkidle` waits for fonts, images
and tracking beacons that change nothing about whether the view counts. Waiting
for `domcontentloaded` with images, fonts and media aborted cuts the wait to the
part that matters, and the page's own challenge still runs because scripts, XHR
and *stylesheets* are left alone — the challenge is CSS- and script-driven, not
image-driven.

**A headless browser on a slim host dies without fonts.** Chromium's Skia font
manager aborts the whole browser process (`FATAL:...SkFontMgr_FontConfigInterface
.cpp Not implemented`) the moment a page renders text, which looks exactly like
"Cloudflare killed my browser" and is not: it happens on any text-heavy page,
proxied or not. `ensure_fontconfig()` points fontconfig at a real font tree
(.foxlibs carries DejaVu from the rootless dependency unpack) and writes the
cache dir, after which the browser is stable.

    async with ChromiumSession(cfg, proxy=p, fast=True) as s:
        report = await s.load(cfg.url)
        print(report.status, report.ms, "ms", report.cleared)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cloudflare import CloudflareGuard

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - dependency missing at import time
    async_playwright = None  # type: ignore

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Resource types that never affect whether a view counts. `stylesheet` is
# deliberately absent: the challenge page is laid out with CSS and blocking it
# changes nothing good.
HEAVY_RESOURCES = ("image", "font", "media")

# Flags that make a headless Chromium both faster and less obviously automated.
# The throttling flags matter in headless: without them Chromium clamps timers
# in backgrounded renderers, which slows page scripts (including the challenge).
LAUNCH_ARGS: Tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
)

INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


# --------------------------------------------------------------------------- #
# Host bootstrap
# --------------------------------------------------------------------------- #


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


FONTCONFIG_TEMPLATE = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>{fonts}</dir>
  <cachedir>{cache}</cachedir>
  <include ignore_missing="yes">{conf}</include>
</fontconfig>
"""


def lib_dirs(root: Path) -> List[str]:
    """Every directory under a rootless dependency unpack that holds .so files."""
    found: List[str] = []
    if not root.exists():
        return found
    stack = [(root, 0)]
    while stack and len(found) < 64:
        path, depth = stack.pop()
        if depth > 6:
            continue
        try:
            entries = list(path.iterdir())
        except OSError:
            continue
        if any(e.is_file() and ".so" in e.name for e in entries):
            found.append(str(path))
        for entry in entries:
            if entry.is_dir():
                stack.append((entry, depth + 1))
    return sorted(found)


def ensure_fontconfig(root: Optional[Path] = None) -> Optional[str]:
    """Guarantee a fontconfig that actually resolves, or return None.

    Prefers a system configuration when one exists; otherwise points at the font
    tree inside the local dependency unpack and writes a cache directory. The
    file goes in the state dir, never in tracked source.
    """
    root = Path(root) if root else project_root()
    if os.environ.get("FONTCONFIG_FILE") and os.path.exists(os.environ["FONTCONFIG_FILE"]):
        return os.environ["FONTCONFIG_FILE"]
    system = "/etc/fonts/fonts.conf"
    if os.path.exists(system):
        os.environ.setdefault("FONTCONFIG_PATH", "/etc/fonts")
        return None

    libs = root / ".foxlibs"
    fonts = libs / "usr" / "share" / "fonts"
    if not fonts.exists():
        for candidate in (root / ".pwlibs", Path("/usr/share/fonts"), Path("/usr/local/share/fonts")):
            if candidate.exists():
                fonts = candidate
                break
    if not fonts.exists():
        return None

    state = Path(os.environ.get("STATE_DIR", root / ".stealth-state"))
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    cache = state / "fontconfig-cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = Path("/tmp/fc-cache")
        cache.mkdir(parents=True, exist_ok=True)
    target = state / "fonts.conf"
    body = FONTCONFIG_TEMPLATE.format(fonts=fonts, cache=cache, conf=libs / "etc" / "fonts" / "conf.d")
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if existing != body:
            target.write_text(body, encoding="utf-8")
    except OSError:
        return None
    os.environ["FONTCONFIG_FILE"] = str(target)
    if (libs / "etc" / "fonts").exists():
        os.environ["FONTCONFIG_PATH"] = str(libs / "etc" / "fonts")
    return str(target)


def ensure_host_env(root: Optional[Path] = None) -> Dict[str, str]:
    """Pin the browser cache, library path and fonts for this process and its children.

    Playwright resolves its browser cache from HOME, which is why a browser
    installed by one user is invisible to another; the unpacked shared objects
    are only visible if they are on the loader path. Both are decided here, once.
    """
    root = Path(root) if root else project_root()
    env: Dict[str, str] = {}
    pinned = root / ".playwright-browsers"
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pinned)
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]

    if not os.environ.get("PLAYWRIGHT_SKIP_LOCAL_LIBS"):
        dirs = lib_dirs(root / ".foxlibs") + lib_dirs(root / ".pwlibs")
        base = os.environ.get("LD_LIBRARY_PATH", "")
        combined = ":".join(dirs + ([base] if base else []))
        if combined:
            os.environ["LD_LIBRARY_PATH"] = combined
            env["LD_LIBRARY_PATH"] = combined

    conf = ensure_fontconfig(root)
    if conf:
        env["FONTCONFIG_FILE"] = conf
    if os.environ.get("FONTCONFIG_PATH"):
        env["FONTCONFIG_PATH"] = os.environ["FONTCONFIG_PATH"]
    return env


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def should_block(resource_type: str, drop: Sequence[str]) -> bool:
    """Whether a request of this type is worth aborting on a view load."""
    return resource_type in drop


async def block_heavy(page, drop: Sequence[str] = HEAVY_RESOURCES,
                      log: Optional[Callable[[str], None]] = None) -> Dict[str, int]:
    """Abort heavy resources for a page. Returns per-type abort counts.

    Counts are returned rather than logged-only so a run can prove the blocker
    actually fired instead of silently matching nothing.
    """
    counts: Dict[str, int] = {}

    async def handler(route: Any, request: Any) -> None:
        if should_block(request.resource_type, drop):
            counts[request.resource_type] = counts.get(request.resource_type, 0) + 1
            try:
                await route.abort()
            except Exception:
                pass
            return
        try:
            await route.continue_()
        except Exception:
            pass

    await page.route("**/*", handler)
    if log:
        log(f"blocking {', '.join(drop)} on {page.url or 'page'}")
    return counts


# The beacon the site fires when it records a view. A page load is not a view:
# measured on the live target, a headless Chromium sits on the profile for over a
# minute, with mouse movement and scrolling, without ever issuing this request.
VIEW_BEACON_PATH = "/api/analytics/"


def is_view_beacon(url: str, method: str = "POST", status: int = 0) -> bool:
    """Whether a response is the site's own view beacon, and it succeeded."""
    return (method.upper() == "POST" and VIEW_BEACON_PATH in (url or "")
            and 200 <= int(status) < 300)


# Why a view did or did not count. Measured on the live target: the page's own
# POST to /api/analytics/view comes back `cf-mitigated: challenge` — Cloudflare
# guards that path separately from the page, even with the page's own
# cf_clearance in the jar. Naming the three outcomes separately is the whole
# point: "no view" and "Cloudflare refused the view" need different fixes.
def classify_view(status: Optional[int], mitigated: str = "") -> str:
    """Map one view POST's response onto counted / challenged / rejected / none."""
    if status is None:
        return "none"
    if 200 <= int(status) < 300:
        return "counted"
    if int(status) == 403 and (mitigated or "").strip().lower() == "challenge":
        return "challenged"
    return "rejected"


def turnstile_params_from(values: Dict[str, Any]) -> Dict[str, str]:
    """Normalise the widget attributes a provider needs into one shape."""
    return {
        "sitekey": str(values.get("sitekey") or ""),
        "action": str(values.get("action") or ""),
        "cdata": str(values.get("cdata") or ""),
    }


@dataclass
class LoadReport:
    """What one fast page load cost, and whether it recorded a view."""

    url: str = ""
    status: int = 0
    ms: float = 0.0
    cleared: bool = False
    detail: str = ""
    blocked: Dict[str, int] = field(default_factory=dict)
    challenge_ms: float = 0.0
    title: str = ""
    beacon_status: Optional[int] = None
    view_status: Optional[int] = None
    view_mitigated: str = ""

    @property
    def view_recorded(self) -> bool:
        """A loaded page is not a recorded view; only a successful beacon is."""
        return self.beacon_status is not None and 200 <= self.beacon_status < 300

    def as_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "status": self.status, "ms": round(self.ms, 1),
                "cleared": self.cleared, "blocked": dict(self.blocked),
                "challenge_ms": round(self.challenge_ms, 1), "title": self.title,
                "beacon_status": self.beacon_status, "view_status": self.view_status,
                "view_mitigated": self.view_mitigated, "view_recorded": self.view_recorded,
                "detail": self.detail}


class ChromiumSession:
    """One Chromium context, launched fast, sharing the rig's clearance cache."""

    def __init__(self, config, *, proxy: Optional[Any] = None, worker_id: str = "1.1",
                 fast: bool = True, drop: Sequence[str] = HEAVY_RESOURCES,
                 wait_until: str = "domcontentloaded", idle_wait_ms: int = 0,
                 block_images_flag: bool = True, reuse_browser: Any = None,
                 pw: Any = None, canary: bool = False,
                 log: Optional[Callable[[str, str], None]] = None) -> None:
        self.config = config
        self.proxy = proxy
        self.worker_id = worker_id
        self.fast = fast
        self.drop = tuple(drop)
        self.wait_until = wait_until
        self.idle_wait_ms = idle_wait_ms
        self.block_images_flag = block_images_flag
        self._reuse = reuse_browser
        self._pw = pw
        self._own_pw = pw is None
        self._canary = canary
        self._log = log or (lambda *_a: None)
        self.hits = 0

        self.browser = None
        self.context = None
        self.page = None
        self.guard: Optional[CloudflareGuard] = None
        self.blocked: Dict[str, int] = {}
        self.last: Optional[LoadReport] = None
        # Every response to the site's analytics endpoints, in order. This is the
        # only evidence that a view counted; a 200 on the page is not.
        self.beacons: List[Tuple[int, str]] = []
        # POSTs to the view endpoint with the headers that explain a refusal.
        self.view_posts: List[Dict[str, Any]] = []

    # -- lifecycle -------------------------------------------------------- #

    async def start(self) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed — run: pip install -r stealth/requirements.txt")
        ensure_host_env()
        if self._pw is None:
            self._pw = await async_playwright().start()
        args = list(LAUNCH_ARGS)
        # Letting the renderer skip image decode entirely is cheaper than
        # aborting each request, so both are used when images are dropped.
        if self.fast and self.block_images_flag and "image" in self.drop:
            args.append("--blink-settings=imagesEnabled=false")
        if self._reuse is not None:
            self.browser = self._reuse
        else:
            self.browser = await self._pw.chromium.launch(headless=bool(self.config.headless),
                                                          channel="chromium", args=args)

        opts: Dict[str, Any] = {
            "user_agent": CHROME_UA,
            "viewport": {"width": 1366, "height": 768},
            "locale": self.config.locale or "en-US",
            "ignore_https_errors": True,
        }
        if self.config.geoip:
            opts["timezone_id"] = "America/New_York"
        if self.proxy:
            opts["proxy"] = self.proxy.to_playwright()
        self.context = await self.browser.new_context(**opts)
        self.page = await self.context.new_page()
        await self.page.add_init_script(INIT_SCRIPT)
        self.page.on("response", self._on_response)
        if self.fast and self.drop:
            self.blocked = await block_heavy(self.page, self.drop)
            # block_heavy returns a live dict that the route handler keeps
            # updating; keep a handle so the counts are readable after the load.
        self.guard = CloudflareGuard(
            self.context,
            proxy_label=self.proxy.label if self.proxy else "direct",
            state_dir=self.config.state_dir,
            timeout_ms=self.config.cf_timeout_ms,
            verbose=self.config.verbose,
            log=self._log,
        )

    def _on_response(self, response: Any) -> None:
        """Record the site's beacon, if it fires. Never raises into the browser."""
        try:
            if VIEW_BEACON_PATH not in response.url:
                return
            self.beacons.append((int(response.status), response.url))
            if response.request.method.upper() != "POST":
                return
            headers: Dict[str, str] = {}
            try:
                headers = dict(response.headers or {})
            except Exception:
                headers = {}
            self.view_posts.append({
                "status": int(response.status),
                "mitigated": str(headers.get("cf-mitigated") or ""),
                "url": response.url,
            })
        except Exception:
            pass

    def last_beacon(self) -> Optional[int]:
        return self.beacons[-1][0] if self.beacons else None

    async def stop(self) -> None:
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        self.context = None
        self.page = None
        if self.browser is not None and self._reuse is None:
            try:
                await self.browser.close()
            except Exception:
                pass
        self.browser = None
        if self._own_pw and self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = None

    async def __aenter__(self) -> "ChromiumSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # -- the load --------------------------------------------------------- #

    async def load(self, url: Optional[str] = None, *, handle_challenge: bool = True) -> LoadReport:
        """Navigate fast, clear a challenge if there is one, and time both."""
        url = url or self.config.url
        report = LoadReport(url=url)
        started = time.time()
        host = ""
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname or ""
        except Exception:
            pass

        if self.guard and host:
            await self.guard.seed_cookies(self.page, host)

        resp = await self.page.goto(url, wait_until=self.wait_until,
                                    timeout=self.config.nav_timeout_ms)
        report.status = getattr(resp, "status", 0) or 0
        report.ms = (time.time() - started) * 1000
        try:
            report.title = await self.page.title()
        except Exception:
            report.title = ""

        if handle_challenge and self.guard:
            blocked = await self.guard.status_code_blocked(resp) or await self.guard.detect(self.page)
            if blocked:
                t = time.time()
                result = await self.guard.handle(self.page, host)
                report.challenge_ms = (time.time() - t) * 1000
                report.cleared = result.solved
                report.detail = result.reason
                if result.solved:
                    await self.page.goto(url, wait_until=self.wait_until,
                                         timeout=self.config.nav_timeout_ms)
                    report.ms = (time.time() - started) * 1000
            else:
                report.cleared = True

        if self.idle_wait_ms:
            try:
                await self.page.wait_for_load_state("networkidle", timeout=self.idle_wait_ms)
            except Exception:
                pass

        if self._canary:
            # The first load retries once: a cold renderer on a loaded host
            # fails more often than a warm one, and a relaunch is cheap next to
            # losing the worker for the whole round.
            try:
                html = await self.page.content()
            except Exception:
                html = ""
            if not html or "<body" not in html:
                report.detail = report.detail or "empty render"
                await asyncio.sleep(1.0)
                resp = await self.page.goto(url, wait_until=self.wait_until,
                                            timeout=self.config.nav_timeout_ms)
                report.status = getattr(resp, "status", report.status) or report.status

        report.beacon_status = self.last_beacon()
        if self.view_posts:
            latest = self.view_posts[-1]
            report.view_status = latest.get("status")
            report.view_mitigated = str(latest.get("mitigated") or "")
        report.blocked = dict(self.blocked)
        self.last = report
        self.hits += 1
        self._log("load", f"{self.worker_id}: {report.status} in {report.ms:.0f}ms"
                          f"{' (cleared ' + format(report.challenge_ms, '.0f') + 'ms)' if report.challenge_ms else ''}"
                          f" blocked={sum(report.blocked.values())}"
                          f" beacon={'none' if report.beacon_status is None else report.beacon_status}")
        return report

    # -- driving the view ------------------------------------------------ #

    async def widget_params(self) -> Dict[str, str]:
        """The Turnstile sitekey/action/cdata the page rendered, for a provider."""
        if self.page is None:
            return {"sitekey": "", "action": "", "cdata": ""}
        try:
            values = await self.page.evaluate("""() => {
                const el = document.querySelector('.cf-turnstile');
                if (!el) return {};
                return {sitekey: el.getAttribute('data-sitekey') || '',
                        action: el.getAttribute('data-action') || '',
                        cdata: el.getAttribute('data-cdata') || ''};
            }""")
        except Exception:
            values = {}
        return turnstile_params_from(values or {})

    async def trigger_view(self, token: str, wait_ms: int = 15_000) -> Dict[str, Any]:
        """Hand the page a Turnstile token and let it post the view itself.

        The page exposes `window.getToken` — the React setter behind the
        widget's `data-callback`. Setting it makes the page's own effect run: it
        computes the proof-of-work in its worker, signs the payload and POSTs to
        /api/analytics/view with its own cookies and fingerprint. That is the
        view request Cloudflare sees from a real browser, which is why this is
        the route worth driving instead of replaying the body from Python.
        """
        page = self.page
        result: Dict[str, Any] = {"status": None, "mitigated": "", "outcome": "none"}
        if page is None:
            result["outcome"] = "no_page"
            return result
        deadline = time.time() + max(1.0, wait_ms / 1000.0)
        while time.time() < deadline:
            try:
                if await page.evaluate("() => typeof window.getToken === 'function'"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        else:
            result["outcome"] = "no_widget"
            return result

        before = len(self.view_posts)
        try:
            await page.evaluate("(t) => { window.getToken(t); }", token)
        except Exception as e:
            result["outcome"] = "inject_failed"
            result["detail"] = str(e).splitlines()[0][:160]
            return result

        while time.time() < deadline:
            if len(self.view_posts) > before:
                post = dict(self.view_posts[before])
                post["outcome"] = classify_view(post.get("status"), post.get("mitigated", ""))
                return post
            await asyncio.sleep(0.25)
        result["outcome"] = "no_post"
        return result

    async def cookies(self) -> Dict[str, str]:
        if not self.context:
            return {}
        try:
            return {c["name"]: c["value"] for c in await self.context.cookies()}
        except Exception:
            return {}


class ChromiumFleet:
    """Fan out across worker CONTEXTS inside one browser process.

    This is the structural difference from the Camoufox rig, and the reason it
    is the volume rail: Playwright supports a per-context proxy on Chromium, so
    N egresses cost one browser launch instead of N — and a cold browser launch
    is the single most expensive thing this tool does. (On Firefox, which
    Camoufox is, the proxy is launch-scoped, so there that mapping has to be one
    browser per egress.)

    Every worker still gets its own cookie jar, its own clearance cache entry
    (keyed by egress) and its own User-Agent-consistent identity.
    """

    def __init__(self, config, *, fast: bool = True, tokens: Optional[Sequence[str]] = None,
                 provider: Any = None, view_wait_ms: int = 15_000,
                 log: Optional[Callable[[str, str], None]] = None) -> None:
        self.config = config
        self.fast = fast
        self._log = log or (lambda *_a: None)
        self._pw = None
        self._browser = None
        self._sessions: List[ChromiumSession] = []
        self._running = False
        self.hits = 0          # views the site actually recorded (beacon confirmed)
        self.loaded = 0        # pages that loaded, but recorded no view
        self.misses = 0
        self.loads: List[float] = []
        self.last_error = ""
        self._explained = False
        # A view needs a Turnstile token. Supplied tokens are reused in order;
        # a provider mints one per view. No token means the page loads and stops
        # there — measured: it never posts a view on its own.
        self._tokens: List[str] = [t for t in (tokens or []) if t]
        self._token_index = 0
        self.provider = provider
        self.view_wait_ms = max(1000, int(view_wait_ms))
        self.challenged = 0    # Cloudflare refused the view POST
        self.rejected = 0      # the view POST reached the app and was refused
        self.untokened = 0     # no token available, so no view was attempted
        self.unposted = 0      # token handed over, page never posted

    def _assign(self, count: int):
        proxies, bad = self.config.resolved_proxies()
        for line in bad:
            self._log("warn", f"proxy rejected: {line}")
        if not proxies:
            return [None] * count
        return [proxies[i % len(proxies)] for i in range(count)]

    def _next_token(self) -> str:
        """The next supplied token, in order. Single tokens are single-use."""
        if not self._tokens:
            return ""
        token = self._tokens[self._token_index % len(self._tokens)]
        self._token_index += 1
        return token

    async def _token_for(self, session: ChromiumSession, proxy) -> str:
        """A token for one view: supplied, or minted by the captcha provider."""
        token = self._next_token()
        if token:
            return token
        if self.provider is None:
            return ""
        params = await session.widget_params()
        if not params.get("sitekey"):
            self._log("warn", f"{session.worker_id}: no Turnstile widget on the page — "
                              f"nothing to mint a token for")
            return ""
        from .captcha import TurnstileRequest

        proxy_url = proxy.to_curl() if proxy is not None else ""
        return await self.provider.solve_turnstile(TurnstileRequest(
            page_url=self.config.url, site_key=params["sitekey"],
            action=params.get("action", ""), cdata=params.get("cdata", ""),
            proxy=proxy_url,
        ))

    async def _worker(self, index: int, total: int, proxy, rounds: int) -> None:
        wid = f"{index // max(1, self.config.tabs) + 1}.{index % max(1, self.config.tabs) + 1}"
        gap = self.config.round_gap
        session = ChromiumSession(
            self.config, proxy=proxy, worker_id=wid, fast=self.fast,
            reuse_browser=self._browser, pw=self._pw, canary=(index == 0),
            log=self._log,
        )
        await session.start()
        self._sessions.append(session)
        try:
            # `rounds <= 0` = keep going until the run is stopped. The context
            # stays warm, which is the point of looping: a page load is cheap, a
            # browser and context are not.
            done = 0
            while self._running and (rounds <= 0 or done < rounds):
                done += 1
                try:
                    report = await session.load()
                    self.loads.append(report.ms)
                    if not (report.status and report.status < 400):
                        self.misses += 1
                        self.last_error = f"status {report.status} {report.detail}"
                        self._log("miss", f"{wid}: {self.last_error}")
                    elif report.view_recorded:
                        self.hits += 1
                        self._log("hit", f"{wid}: view recorded (beacon {report.beacon_status}, "
                                         f"load {report.ms:.0f}ms)")
                    else:
                        # A loaded page is NOT a view. The site posts one when its
                        # widget hands the page a Turnstile token; headless never
                        # solves the widget, so the token has to come from outside.
                        self.loaded += 1
                        await self._attempt_view(session, proxy, wid, report)
                except Exception as e:
                    self.misses += 1
                    self.last_error = str(e).splitlines()[0]
                    self._log("miss", f"{wid}: {self.last_error}")
                if rounds <= 0 or done < rounds:
                    await asyncio.sleep(_rand_range(gap) / 1000.0)
        finally:
            await session.stop()

    async def _attempt_view(self, session: ChromiumSession, proxy, wid: str,
                            report: LoadReport) -> None:
        """One view attempt: get a token, hand it to the page, read the result."""
        try:
            token = await self._token_for(session, proxy)
        except Exception as e:
            self.untokened += 1
            self.last_error = str(e).splitlines()[0]
            self._log("warn", f"{wid}: no token — {self.last_error}")
            return
        if not token:
            self.untokened += 1
            if not self._explained:
                self._explained = True
                self._log("warn", "page loaded but no view was attempted: the widget needs a "
                                  "Turnstile token, and a headless browser never solves it. "
                                  "Supply --turnstile-token, or a provider with "
                                  "--captcha-provider/--captcha-key, and the page will post "
                                  "the view itself.")
            return

        result = await session.trigger_view(token, self.view_wait_ms)
        outcome = str(result.get("outcome") or "none")
        status = result.get("status")
        if outcome == "counted":
            self.hits += 1
            self._log("hit", f"{wid}: view recorded (POST {status}, load {report.ms:.0f}ms)")
        elif outcome == "challenged":
            self.challenged += 1
            self._log("warn", f"{wid}: view POST challenged by Cloudflare "
                              f"(403 cf-mitigated: challenge) — the page's clearance covers "
                              f"the profile, not /api/analytics/view")
        elif outcome == "rejected":
            self.rejected += 1
            self._log("miss", f"{wid}: view POST refused ({status})")
        else:
            self.unposted += 1
            self._log("warn", f"{wid}: token handed to the page but no view POST followed "
                              f"({outcome})")

    async def run(self, rounds: int = 1) -> Dict[str, Any]:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed")
        self.config.validate()
        count = max(1, self.config.contexts * self.config.tabs)
        self._running = True
        self.hits = self.misses = 0
        self.challenged = self.rejected = self.untokened = self.unposted = 0
        self.loads = []
        assignments = self._assign(count)

        ensure_host_env()
        self._log("boot", f"fleet: {count} context(s) in 1 browser x "
                          f"{'∞ (until stopped)' if rounds <= 0 else rounds} round(s) "
                          f"-> {self.config.url}")
        self._log("boot", "egress " + (
            f"{len({p.label for p in assignments if p})} prox(ies) (per-context)"
            if any(assignments) else "direct"))
        if self._tokens:
            self._log("boot", f"tokens: {len(self._tokens)} supplied "
                              f"({'reused' if len(self._tokens) == 1 else 'in order'})")
        elif self.provider is not None:
            self._log("boot", f"tokens: minted per view by {getattr(self.provider, 'name', 'provider')}")
        else:
            self._log("warn", "no token source: pages will load and no views will be attempted "
                              "(--turnstile-token, or --captcha-provider + --captcha-key)")

        self._pw = await async_playwright().start()
        args = list(LAUNCH_ARGS)
        if self.fast and "image" in HEAVY_RESOURCES:
            args.append("--blink-settings=imagesEnabled=false")
        self._browser = await self._pw.chromium.launch(
            headless=bool(self.config.headless), channel="chromium", args=args)

        tasks = []
        for i, px in enumerate(assignments):
            tasks.append(asyncio.create_task(self._worker(i, count, px, rounds)))
            if self.config.spawn_stagger_s:
                await asyncio.sleep(min(0.5, self.config.spawn_stagger_s))
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.stop()
        if self.loaded and not self.hits:
            why = ("no token was supplied" if self.untokened
                   else "Cloudflare challenged every view POST" if self.challenged
                   else "the page never posted a view")
            self._log("warn", f"{self.loaded} page(s) loaded and 0 views recorded — {why}")
        return self.status()

    async def stop(self) -> None:
        self._running = False
        for session in self._sessions:
            try:
                await session.stop()
            except Exception:
                pass
        self._sessions = []
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = None

    def status(self) -> Dict[str, Any]:
        loads = sorted(self.loads)
        median = loads[len(loads) // 2] if loads else 0.0
        blocked: Dict[str, int] = {}
        for session in self._sessions:
            for key, value in session.blocked.items():
                blocked[key] = blocked.get(key, 0) + value
        return {
            "running": self._running,
            "url": self.config.url,
            "target": self.config.target,
            "workers": len(self._sessions),
            "hits": self.hits,            # beacon-confirmed views
            "loaded": self.loaded,        # loaded, view not recorded
            "misses": self.misses,
            "challenged": self.challenged,  # view POST hit a Cloudflare challenge
            "rejected": self.rejected,      # view POST refused by the app
            "untokened": self.untokened,    # no token, no attempt
            "unposted": self.unposted,      # token given, page never posted
            "mode": "chromium",
            "load_ms_median": round(median, 1),
            "load_ms_min": round(loads[0], 1) if loads else 0.0,
            "blocked": blocked,
            "last_error": self.last_error,
        }


def _rand_range(gap) -> float:
    import random

    lo, hi = (gap if isinstance(gap, (tuple, list)) else (gap, gap))
    return random.uniform(float(lo), float(hi))


def chromium_executable_ok() -> bool:
    """Whether a Chromium build is actually present in the pinned cache."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return bool(pw.chromium.executable_path and os.path.exists(pw.chromium.executable_path))
    except Exception:
        return False
