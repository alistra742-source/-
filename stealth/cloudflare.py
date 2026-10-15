"""Cloudflare guard — detect challenges, walk Turnstile, persist clearance.

The honest mechanics first, because the approach follows from them:

* Cloudflare Bot Management scores a session on signals far beyond the JS
  challenge: TLS/JA3, header order, IP reputation, ASN, request cadence, and
  whether the click that solved the widget was a real pointer event.
* A managed Turnstile often solves itself: a real browser with a real click and
  a clean-ish IP gets a `cf_clearance` cookie with no checkbox interaction. The
  job of this module is to *not get in the way* — behave like a person, wait for
  the interstitial to clear, and reuse the clearance across requests.
* An interactive Turnstile needs an actual click inside a cross-origin iframe.
  That is what `disable_coop=True` on the browser is for: it lets Playwright
  reach into the challenges.cloudflare.com frame.

`cf_clearance` is bound to the exit IP, the UA, and a short TTL. It is
persisted per-proxy (keyed by proxy + target host) and re-attached for later
requests through the same egress. Reusing it across a different IP is the
fastest way to get it revoked, so the cache key includes the proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .egress import CLEARANCE_TTL_S, ClearanceBroker, clearance_path, read_clearance

# Selectors that mean "a Cloudflare interstitial is on screen right now".
_CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "security check",
)
_CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#cf-challenge-running",
    "#cf-please-wait",
    "div#turnstile-wrapper",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='Widget containing']",
)
_TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com'], iframe[title*='Turnstile']"


@dataclass
class ChallengeResult:
    blocked: bool
    solved: bool
    reason: str
    waited_s: float = 0.0
    reused: bool = False   # clearance came from a peer on the same egress


async def _sleep_ms(ms: float) -> None:
    await asyncio.sleep(max(0.0, ms) / 1000.0)


class CloudflareGuard:
    """Per-context Cloudflare challenge handler with on-disk clearance cache."""

    def __init__(
        self,
        context,
        *,
        proxy_label: str = "direct",
        state_dir: str = ".stealth-state",
        timeout_ms: int = 45_000,
        verbose: bool = False,
        log=None,
        broker: Optional[ClearanceBroker] = None,
        share_clearance: bool = True,
        clearance_wait_s: float = 30.0,
        worker_id: str = "",
    ) -> None:
        self.context = context
        self.proxy_label = proxy_label or "direct"
        self.state_dir = state_dir
        self.timeout_ms = timeout_ms
        self.verbose = verbose
        self.worker_id = worker_id
        self._log = log or (lambda *_a, **_k: None)
        self.broker = broker
        self.share_clearance = share_clearance and broker is not None
        self.clearance_wait_s = max(0.0, clearance_wait_s)
        os.makedirs(self.state_dir, exist_ok=True)

        # per-session counters the fleet folds back into the egress pool
        self.solves = 0
        self.blocks = 0
        self.reused = 0

    # -- cache ------------------------------------------------------------ #

    def _cache_path(self, host: str) -> str:
        return clearance_path(self.state_dir, self.proxy_label, host)

    def load_clearance(self, host: str) -> Dict[str, str]:
        return read_clearance(self._cache_path(host))

    def save_clearance(self, host: str, cookies: Dict[str, str], ttl_s: int = CLEARANCE_TTL_S) -> None:
        """Publish clearance where every worker on this egress can pick it up."""
        if not cookies:
            return
        if self.broker is not None:
            self.broker.publish(self.proxy_label, host, cookies, ttl_s)
            return
        path = self._cache_path(host)
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"cookies": cookies, "expires": time.time() + ttl_s}, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    # -- detection -------------------------------------------------------- #

    async def detect(self, page) -> bool:
        """True when the current page is a Cloudflare interstitial or block page."""
        try:
            title = (await page.title() or "").lower()
        except Exception:
            title = ""
        if any(marker in title for marker in _CHALLENGE_TITLE_MARKERS):
            return True
        for sel in _CHALLENGE_SELECTORS:
            try:
                if await page.query_selector(sel) is not None:
                    return True
            except Exception:
                continue
        return False

    async def status_code_blocked(self, response) -> bool:
        """A 403/503 with Cloudflare mitigation headers is a bot block."""
        if response is None:
            return False
        try:
            headers = {k.lower(): v for k, v in response.headers.items()}
        except Exception:
            return False
        if response.status in (403, 429) and (
            "cf-mitigated" in headers
            or "cloudflare" in headers.get("server", "").lower()
        ):
            return True
        if response.status == 503 and "cf-ray" in headers:
            return True
        return False

    # -- solving ---------------------------------------------------------- #

    async def handle(self, page, host: str, human=None) -> ChallengeResult:
        """Wait out / click through a challenge. Returns whether we cleared it."""
        start = time.time()
        if not await self.detect(page):
            # Maybe the block happened on the response, not the DOM.
            await _sleep_ms(random.uniform(150, 500))
            if not await self.detect(page):
                return ChallengeResult(False, True, "no challenge", 0.0)

        # A peer on the same egress may have solved this host while we were
        # navigating. Take its cookie instead of paying for a second solve.
        if self.share_clearance:
            peer = await self._reuse_peer_clearance(page, host, stage="before solving")
            if peer is not None:
                return peer

        # One solver per egress: two workers clicking the same egress at once is
        # a signal, not a shortcut. If a peer owns the slot, wait for its result.
        claimed = True
        if self.share_clearance:
            claimed = self.broker.try_claim(  # type: ignore[union-attr]
                self.proxy_label, host, self.worker_id
            )
            if not claimed:
                self._log("cf", f"{host}: another worker is solving this egress — waiting")
                fresh = await self.broker.wait_for_peer(  # type: ignore[union-attr]
                    self.proxy_label, host, self.clearance_wait_s
                )
                if fresh:
                    self.reused += 1
                    await self._attach_cookies(host, fresh)
                    self._log("cf", f"{host}: reused peer clearance — no solve needed")
                    return ChallengeResult(False, True, "reused peer clearance",
                                           time.time() - start, reused=True)
                claimed = self.broker.try_claim(  # type: ignore[union-attr]
                    self.proxy_label, host, self.worker_id
                )
                if not claimed:
                    # The peer is still working and we are out of patience; the
                    # egress is contended either way, so solve locally and let
                    # the coordinator sort it out next run.
                    self._log("cf", f"{host}: peer still solving — proceeding alone")

        try:
            return await self._solve(page, host, human, start)
        finally:
            if self.share_clearance and claimed:
                self.broker.release(self.proxy_label, host)  # type: ignore[union-attr]

    async def _solve(self, page, host: str, human, start: float) -> ChallengeResult:
        """Stage 1 (let the managed widget resolve) then stage 2 (click it)."""
        # Stage 1: the managed widget frequently resolves on its own once a real
        # browser with a real pointer is present. Give it that time before
        # touching anything — premature clicks are themselves a signal.
        deadline = time.time() + self.timeout_ms / 1000
        clicked = False
        while time.time() < deadline:
            if not await self.detect(page):
                await self._persist_cf_cookie(host)
                self.solves += 1
                return ChallengeResult(False, True, "cleared by managed challenge",
                                       time.time() - start)

            # Stage 2: interactive widget. Click the checkbox inside the frame.
            if not clicked and human is not None:
                if await self._click_turnstile(page, human):
                    clicked = True

            # Nudge the page like a waiting person would, sparingly.
            if random.random() < 0.15:
                await _sleep_ms(random.uniform(600, 1800))
            else:
                await _sleep_ms(random.uniform(300, 900))

        await self._persist_cf_cookie(host)
        still = await self.detect(page)
        if still:
            self.blocks += 1
        else:
            self.solves += 1
        return ChallengeResult(still, not still, "timeout" if still else "cleared late",
                               time.time() - start)

    async def _reuse_peer_clearance(
        self,
        page,
        host: str,
        stage: str,
        confirm_s: float = 2.5,
    ) -> Optional[ChallengeResult]:
        """Adopt clearance already published for this egress, if it really works.

        Attaching the cookie is not proof: a stale `cf_clearance` (wrong UA,
        expired, revoked) attaches perfectly and clears nothing. So the page is
        watched for a moment after the cookie lands. If the interstitial is
        still there, this returns None and the caller solves for real — a
        cached cookie is never allowed to buy an unverified pass.
        """
        cached = self.load_clearance(host)
        if not cached:
            return None
        if not await self._attach_cookies(host, cached):
            return None

        deadline = time.time() + max(0.0, confirm_s)
        while time.time() < deadline:
            await _sleep_ms(300)
            if not await self.detect(page):
                self.reused += 1
                self._log("cf", f"{host}: clearance worked ({stage}) — no solve needed")
                return ChallengeResult(False, True, f"clearance reused {stage}", 0.0,
                                       reused=True)
        self._log("cf", f"{host}: cached clearance did not clear the page — solving")
        return None

    async def _click_turnstile(self, page, human) -> bool:
        """Click the Turnstile checkbox in the cross-origin iframe, if present."""
        try:
            frame_el = await page.query_selector(_TURNSTILE_IFRAME)
        except Exception:
            frame_el = None
        if frame_el is None:
            # Older interstitial: a plain checked checkbox in the page itself.
            for sel in ("input[type=checkbox]", "#challenge-stage input", "label.cb-lb"):
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        box = await el.bounding_box()
                        if box:
                            await _sleep_ms(random.uniform(400, 1400))
                            await human.click(
                                box["x"] + box["width"] * random.uniform(0.3, 0.7),
                                box["y"] + box["height"] * random.uniform(0.3, 0.7),
                            )
                            return True
                except Exception:
                    continue
            return False

        # Locate the widget inside its frame and click the checkbox region.
        try:
            box = await frame_el.bounding_box()
        except Exception:
            box = None
        if not box:
            return False
        # The Turnstile checkbox sits at the left, vertically centred.
        target_x = box["x"] + min(30, box["width"] * 0.12)
        target_y = box["y"] + box["height"] * 0.5
        await _sleep_ms(random.uniform(500, 1600))
        await human.click(target_x + random.uniform(-4, 4), target_y + random.uniform(-4, 4))
        self._log("cf", "clicked Turnstile widget")
        return True

    async def _persist_cf_cookie(self, host: str) -> None:
        try:
            cookies = await self.context.cookies()
        except Exception:
            return
        keep = {c["name"]: c["value"] for c in cookies if c["name"] in ("cf_clearance", "__cf_bm")}
        if keep.get("cf_clearance"):
            self.save_clearance(host, keep)
            self._log("cf", f"cf_clearance acquired ({host})")

    async def _attach_cookies(self, host: str, cookies: Dict[str, str]) -> bool:
        """Put a clearance cookie jar on the context. False if the browser refused."""
        if not cookies:
            return False
        try:
            await self.context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": host,
                        "path": "/",
                        "secure": name == "cf_clearance",
                    }
                    for name, value in cookies.items()
                ]
            )
            return True
        except Exception:
            return False

    async def seed_cookies(self, page, host: str) -> bool:
        """Attach a cached clearance to the context before the first navigation."""
        cached = self.load_clearance(host)
        if not cached:
            return False
        ok = await self._attach_cookies(host, cached)
        if ok:
            self.reused += 1
            self._log("cf", f"seeded cached cf_clearance for {host}")
        return ok
