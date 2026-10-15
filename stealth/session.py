"""StealthSession — one Camoufox browser, one context, one worker.

One session == one browser launch == one egress. That mapping is deliberate:
Playwright supports a per-context proxy only on Chromium, so on Firefox (which
Camoufox is) the proxy has to be set at launch. One proxy therefore means one
browser. The fleet fans out across sessions.

Each session wires together the four stealth layers:

    fingerprint  -> Camoufox's own generator (guided by os/locale/screen)
    human motion -> HumanBehavior on the page
    TLS spoof    -> TLSFetch on the request path
    Cloudflare   -> CloudflareGuard around navigation
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from ._compat import ensure_fpgen_network
from .cloudflare import CloudflareGuard
from .config import OS_TARGETS, Proxy, StealthConfig
from .egress import ClearanceBroker, EgressPool
from .fingerprint import FingerprintFactory, Identity
from .human import HumanBehavior
from .tls import TLSFetch

try:
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover - dependency missing at import
    AsyncCamoufox = None  # type: ignore


def _rand(lo: int, hi: int) -> float:
    return random.uniform(lo, hi)


class StealthSession:
    """A single stealth worker. Use as an async context manager."""

    def __init__(
        self,
        config: StealthConfig,
        *,
        proxy: Optional[Proxy] = None,
        worker_id: str = "1.1",
        on_frame: Optional[Callable[[str, bytes], None]] = None,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.config = config
        self.proxy = proxy
        self.worker_id = worker_id
        self.on_frame = on_frame
        self._log = log or (lambda *_a: None)

        self._cm = None
        self.browser = None
        self.context = None
        self.page = None
        self.human: Optional[HumanBehavior] = None
        self.guard: Optional[CloudflareGuard] = None
        self.broker: Optional[ClearanceBroker] = None
        self.tls: Optional[TLSFetch] = None
        self.identity: Optional[Identity] = None
        self._cam_task: Optional[asyncio.Task] = None
        self.hits = 0
        self.misses = 0

    # -- lifecycle -------------------------------------------------------- #

    async def __aenter__(self) -> "StealthSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def _launch_options(self) -> dict:
        cfg = self.config
        factory = FingerprintFactory(cfg.os_target)
        self.identity = factory.build()
        os_claim = cfg.os_target or self.identity.os_target

        opts: dict = {
            "headless": cfg.headless,
            "humanize": cfg.humanize if cfg.humanize is not False else False,
            "os": os_claim,
            "block_images": cfg.block_images,
            "block_webrtc": cfg.block_webrtc,
            "disable_coop": cfg.disable_coop,
            "main_world_eval": cfg.main_world_eval,
            "i_know_what_im_doing": cfg.i_know_what_im_doing,
            "debug": cfg.debug_config,
        }
        if cfg.geoip:
            opts["geoip"] = True
        if cfg.locale:
            opts["locale"] = cfg.locale
        if self.proxy:
            opts["proxy"] = self.proxy.to_playwright()
        return opts

    async def start(self) -> None:
        if AsyncCamoufox is None:
            raise RuntimeError(
                "camoufox is not installed — run: pip install -U 'camoufox[geoip]' "
                "&& camoufox fetch"
            )
        # Camoufox builds its fingerprint with fpgen; undo fpgen's false module
        # detection before it imports the model, or launch dies on NetworkError.
        ensure_fpgen_network()
        opts = self._launch_options()
        self._log("boot", f"{self.worker_id}: launching camoufox os={opts.get('os')} "
                          f"headless={opts.get('headless')} "
                          f"proxy={self.proxy.label if self.proxy else 'direct'}")

        self._cm = AsyncCamoufox(**opts)
        self.browser = await self._cm.__aenter__()

        # Viewport from the generated identity, so screen and window agree.
        vp = self.identity.viewport if self.identity else (1280, 720)
        self.context = await self.browser.new_context(viewport={"width": vp[0], "height": vp[1]})
        self.page = await self.context.new_page()

        self.human = HumanBehavior(self.page, viewport=vp)
        # The broker coordinates with sibling worker processes on the same
        # egress, so two of them never solve one challenge twice.
        self.broker = ClearanceBroker(
            self.config.state_dir,
            shared=self.config.share_clearance,
            log=self._log,
        )
        self.guard = CloudflareGuard(
            self.context,
            proxy_label=self.proxy.label if self.proxy else "direct",
            state_dir=self.config.state_dir,
            timeout_ms=self.config.cf_timeout_ms,
            verbose=self.config.verbose,
            log=self._log,
            broker=self.broker,
            share_clearance=self.config.share_clearance,
            clearance_wait_s=self.config.clearance_wait_s,
            worker_id=self.worker_id,
        )
        self.tls = TLSFetch(
            proxies=[self.proxy.to_curl()] if self.proxy else None,
            impersonate=self.config.tls_impersonate,
        )

        if self.on_frame:
            self._cam_task = asyncio.create_task(self._camera())

    async def stop(self) -> None:
        if self._cam_task:
            self._cam_task.cancel()
            try:
                await self._cam_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cam_task = None
        if self.broker:
            # Drop the solve slot even on a crash path; the kernel would, but
            # releasing explicitly lets a waiting peer proceed immediately.
            try:
                host = urlparse(self.config.url).hostname or ""
                self.broker.release(self.proxy.label if self.proxy else "direct", host)
            except Exception:
                pass
        if self.tls:
            await self.tls.close()
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
        self.context = None
        self.page = None
        self.browser = None

    # -- camera (parity with the Chromium runner's live view) ------------- #

    async def _camera(self) -> None:
        while True:
            try:
                if self.on_frame and self.page:
                    jpg = await self.page.screenshot(type="jpeg", quality=55, timeout=5000)
                    self.on_frame(self.worker_id, jpg)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    # -- the hit ---------------------------------------------------------- #

    async def hit(self) -> bool:
        """Navigate, clear any challenge, loiter like a person. True on a clean view."""
        if not self.page:
            raise RuntimeError("session not started")
        url = self.config.url
        host = urlparse(url).hostname or ""

        try:
            if self.config.warmup:
                await self._warmup()

            # Attach any cached clearance before the first navigation.
            await self.guard.seed_cookies(self.page, host)

            resp = await self.page.goto(
                url, wait_until="domcontentloaded", timeout=self.config.nav_timeout_ms
            )

            if await self.guard.status_code_blocked(resp) or await self.guard.detect(self.page):
                result = await self.guard.handle(self.page, host, human=self.human)
                if not result.solved:
                    self.misses += 1
                    self._log("miss", f"{self.worker_id}: challenge not cleared ({result.reason})")
                    return False
                # After clearance, settle then reload the target for the real view.
                await asyncio.sleep(_rand(400, 1200) / 1000)
                await self.page.goto(url, wait_until="domcontentloaded",
                                     timeout=self.config.nav_timeout_ms)

            await self.page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            # networkidle is best-effort; a page that never goes idle is normal.
            pass

        try:
            if self.human and self.config.dwell[1] > 0:
                await self.human.idle(_rand(*self.config.dwell) / 1000)
        except Exception:
            pass

        # Hand the browser cookies to the fast HTTP path so its TLS-spoofed
        # requests carry the same session identity.
        try:
            if self.tls and self.context:
                cookies = await self.context.cookies()
                self.tls.set_cookies({c["name"]: c["value"] for c in cookies})
        except Exception:
            pass

        self.hits += 1
        self._log("hit", f"{self.worker_id}: view sent "
                         f"({'proxy ' + self.proxy.label if self.proxy else 'direct egress'})")
        return True

    async def _warmup(self) -> None:
        """A benign navigation gives the session a history before the target."""
        try:
            await self.page.goto("about:blank")
            await asyncio.sleep(_rand(200, 600) / 1000)
        except Exception:
            pass

    # -- egress accounting ------------------------------------------------ #

    def egress_stats(self) -> Dict[str, int]:
        """What this session's egress had to do — folded back into the pool."""
        g = self.guard
        return {
            "solves": int(getattr(g, "solves", 0)) if g else 0,
            "blocks": int(getattr(g, "blocks", 0)) if g else 0,
            "reused": int(getattr(g, "reused", 0)) if g else 0,
        }

    # -- fast path -------------------------------------------------------- #

    async def fetch(self, path_or_url: str, **kw):
        """TLS-spoofed HTTP fetch sharing the browser's session cookies."""
        url = path_or_url
        if not url.startswith("http"):
            base = self.config.base_url.rstrip("/")
            url = f"{base}/{url.lstrip('/')}"
        return await self.tls.get(url, **kw)


class StealthFleet:
    """Fan-out across worker PROCESSES — one Camoufox browser per process.

    Why processes and not coroutines? Camoufox (patched Firefox) intermittently
    stalls `browser.new_page()` when two instances share one asyncio loop: the
    juggler handshake contends and one browser's page creation never returns.
    A process per worker gives each its own browser, event loop, proxy and
    identity, which is both the fix and the way browser farms normally scale.

    `StealthSession` stays available for single, in-process use.
    """

    def __init__(
        self,
        config: StealthConfig,
        *,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.config = config
        self._log = log or (lambda *_a: None)
        self._running = False
        self._procs: List[Any] = []
        self.hits = 0
        self.misses = 0
        self._project_root = str(Path(__file__).resolve().parents[1])
        self._pool: Optional[EgressPool] = None

    def _egress_pool(self) -> EgressPool:
        """One pool for the run: proxies, warm-clearance knowledge, outcomes."""
        proxies, bad = self.config.resolved_proxies()
        for line in bad:
            self._log("warn", f"proxy rejected: {line}")
        host = urlparse(self.config.url).hostname or ""
        # The parent only reads the clearance cache (to prefer warm egresses);
        # the workers are the ones that claim solve slots.
        inspector = ClearanceBroker(
            self.config.state_dir,
            shared=self.config.share_clearance,
            log=self._log,
        )
        self._pool = EgressPool(
            proxies,
            state_dir=self.config.state_dir,
            host=host,
            broker=inspector,
            log=self._log,
        )
        if proxies and inspector.shared:
            warm = [p.label for p in proxies if inspector.cached(p.label, host)]
            if warm:
                self._log("boot", f"{len(warm)}/{len(proxies)} egress(es) already hold "
                                   f"clearance for {host} — sending them first")
        return self._pool

    def _assign_proxies(self, count: int) -> List[Optional[Proxy]]:
        pool = self._pool or self._egress_pool()
        return pool.plan(count)  # type: ignore[return-value]

    def _job(self, proxy: Optional[Proxy], worker_id: str, rounds: int) -> Dict[str, Any]:
        cfg = self.config
        return {
            "target": cfg.target,
            "base_url": cfg.base_url,
            "worker_id": worker_id,
            "rounds": rounds,
            "proxy": (
                {"server": proxy.server, "username": proxy.username, "password": proxy.password}
                if proxy else None
            ),
            "headless": cfg.headless,
            "os_target": cfg.os_target,
            "humanize": cfg.humanize,
            "geoip": cfg.geoip,
            "locale": cfg.locale,
            "block_images": cfg.block_images,
            "block_webrtc": cfg.block_webrtc,
            "disable_coop": cfg.disable_coop,
            "main_world_eval": cfg.main_world_eval,
            "tls_impersonate": cfg.tls_impersonate,
            "nav_timeout_ms": cfg.nav_timeout_ms,
            "dwell": list(cfg.dwell),
            "round_gap": list(cfg.round_gap),
            "warmup": cfg.warmup,
            "solve_turnstile": cfg.solve_turnstile,
            "cf_timeout_ms": cfg.cf_timeout_ms,
            "state_dir": cfg.state_dir,
            "share_clearance": cfg.share_clearance,
            "clearance_wait_s": cfg.clearance_wait_s,
            "launch_retries": cfg.launch_retries,
            "launch_retry_delay": list(cfg.launch_retry_delay),
            "debug_config": cfg.debug_config,
            "verbose": cfg.verbose,
        }

    async def run(self, rounds: int = 1) -> dict:
        cfg = self.config
        cfg.validate()
        count = max(1, cfg.contexts * cfg.tabs)
        pool = self._egress_pool()
        pool.begin_run()
        assignments = pool.plan(count)
        self._running = True
        self.hits = 0
        self.misses = 0

        self._log("boot", f"fleet: {count} worker process(es) x {rounds} round(s) "
                          f"-> {cfg.url}")

        procs: List[Any] = []
        pending: List[Any] = []
        for i, px in enumerate(assignments):
            wid = f"{i // max(1, cfg.tabs) + 1}.{i % max(1, cfg.tabs) + 1}"
            job = self._job(px, wid, rounds)
            env = dict(os.environ)
            env["STEALTH_JOB"] = json.dumps(job)
            try:
                p = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "stealth.worker",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=self._project_root,
                )
            except Exception as e:
                self._log("error", f"{wid}: spawn failed — {e}")
                continue
            procs.append((wid, p))
            pending.append(self._watch(wid, p))
            # Stagger cold starts: a fleet of simultaneous Firefox launches
            # spikes memory and CPU on a small host, which is what makes one of
            # them stall in the first place.
            if cfg.spawn_stagger_s > 0 and i + 1 < len(assignments):
                await asyncio.sleep(cfg.spawn_stagger_s)

        self._procs = procs
        results = await asyncio.gather(*pending, return_exceptions=True) if pending else []
        self._merge(pool, results)
        pool.save()
        self._running = False
        return self.status()

    def _merge(self, pool: EgressPool, results: List[Any]) -> None:
        """Fold worker RESULT lines into the run totals and the egress pool."""
        for r in results:
            if not isinstance(r, dict):
                continue
            self.hits += int(r.get("hits", 0))
            self.misses += int(r.get("misses", 0))
            pool.record(
                r.get("egress"),
                hits=int(r.get("hits", 0)),
                misses=int(r.get("misses", 0)),
                solves=int(r.get("solves", 0)),
                blocks=int(r.get("blocks", 0)),
                reused=int(r.get("reused", 0)),
            )

    async def _watch(self, wid: str, proc) -> Dict[str, Any]:
        """Bound a worker so a stalled browser cannot hang the whole run."""
        try:
            return await asyncio.wait_for(
                self._collect(wid, proc), timeout=self.config.worker_timeout_s
            )
        except asyncio.TimeoutError:
            self._log("miss", f"{wid}: no result in {self.config.worker_timeout_s}s — killing")
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                pass
            return {"worker": wid, "hits": 0, "misses": 1, "error": "timeout"}

    async def _collect(self, wid: str, proc) -> Dict[str, Any]:
        """Stream a worker's log live, then read its final RESULT line.

        Read line by line rather than `communicate()`: a browser launch takes
        tens of seconds and the operator wants to watch the fleet work, not see
        every worker's output land at once when it exits.
        """
        result: Dict[str, Any] = {"worker": wid, "hits": 0, "misses": 1}
        stdout = proc.stdout
        while stdout is not None:
            raw = await stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("RESULT "):
                try:
                    result = json.loads(line[len("RESULT "):])
                except Exception:
                    pass
            elif line:
                self._log("worker", line)
        await proc.wait()
        err = result.get("error")
        if err:
            self._log("miss", f"{wid}: {err}")
        else:
            tag = "hit" if result.get("hits") else "miss"
            extra = []
            if result.get("egress"):
                extra.append(f"egress={result['egress']}")
            if result.get("solves"):
                extra.append(f"solves={result['solves']}")
            if result.get("reused"):
                extra.append(f"reused={result['reused']}")
            if result.get("blocks"):
                extra.append(f"blocks={result['blocks']}")
            suffix = (" " + " ".join(extra)) if extra else ""
            self._log(tag, f"{wid}: hits={result.get('hits', 0)} "
                           f"misses={result.get('misses', 0)}{suffix}")
        return result

    async def stop(self) -> None:
        self._running = False
        for wid, p in self._procs:
            if getattr(p, "returncode", 0) is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        self._procs = []

    def status(self) -> dict:
        proxies, _ = self.config.resolved_proxies()
        return {
            "running": self._running,
            "url": self.config.url,
            "target": self.config.target,
            "workers": len(self._procs),
            "hits": self.hits,
            "misses": self.misses,
            "mode": "proxies" if proxies else "direct",
            "egress": self._pool.summary() if self._pool else [],
        }
