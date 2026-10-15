"""TLS / JA3-JA4 fingerprint spoofing on the fast HTTP path.

Camoufox handles the browser path and presents Firefox's *native* TLS
fingerprint — which is correct, because it IS Firefox. The problem is the fast
path: every `requests`/`httpx` call in a stack leaks Python's OpenSSL
ClientHello, and a bot score that sees that right after a Firefox page load
flags the session. `curl_cffi` re-issues the ClientHello (and HTTP/2 frame
ordering) of a real browser, so a scrape looks like the same browser that just
loaded the page.

This module wraps curl_cffi with rotation, retries, cookie sharing from the
browser context, and per-proxy egress.
"""

from __future__ import annotations

import asyncio
import random
from typing import Dict, Iterable, List, Optional  # noqa: F401

try:
    from curl_cffi.requests import AsyncSession
    from curl_cffi import CurlError
except Exception:  # pragma: no cover - dependency missing at import time
    AsyncSession = None  # type: ignore

    class CurlError(Exception):  # type: ignore
        ...


# Newest-first. curl_cffi resolves a bare family name ("chrome") to the newest
# available preset, so the pinned entries are a fallback ladder, not the norm.
# The list is only attempted when curl_cffi is installed; unknown names raise
# CurlError and get skipped, so an older build degrades instead of dying.
IMPERSONATION_TARGETS: List[str] = [
    "chrome",
    "chrome136",
    "chrome131",
    "chrome124",
    "chrome120",
    "firefox",
    "firefox135",
    "firefox133",
    "safari",
    "safari184",
    "edge101",
]

# Presets grouped by the browser they make you look like. The rig drives
# Camoufox (Firefox), so the fast path must impersonate *Firefox* — a Chrome
# ClientHello next to a Firefox page load is a louder tell than Python's own
# OpenSSL hello, because the two paths disagree about what the client is.
BROWSER_MATCHED: Dict[str, List[str]] = {
    "firefox": ["firefox", "firefox135", "firefox133"],
    "chrome": ["chrome", "chrome136", "chrome131", "chrome124", "chrome120"],
    "safari": ["safari", "safari184"],
}


class TLSFetch:
    """A rotating, impersonating HTTP client that shares the browser's cookies.

    Usage:
        async with TLSFetch(proxies, impersonate="chrome") as f:
            r = await f.get("https://example.com/api")
            print(r.status_code, r.json())

    Every call re-picks an impersonation target (unless pinned) so JA3/JA4 is
    stable per-context but diverse across the fleet.
    """

    def __init__(
        self,
        proxies: Optional[Iterable[str]] = None,
        impersonate: Optional[str] = None,
        family: str = "firefox",
        timeout: float = 30.0,
        retries: int = 2,
    ) -> None:
        self._proxy_urls: List[str] = list(proxies or [])
        self._pinned: Optional[str] = impersonate
        self._family = family if family in BROWSER_MATCHED else "firefox"
        self._timeout = timeout
        self._retries = retries
        self._session = None
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------- #

    async def __aenter__(self) -> "TLSFetch":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        if AsyncSession is None:
            raise RuntimeError(
                "curl_cffi is not installed — run: pip install 'curl_cffi>=0.7'"
            )
        if self._session is None:
            # impersonate is applied per-request so rotation works on one session.
            self._session = AsyncSession()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- helpers ---------------------------------------------------------- #

    def _pick_target(self) -> str:
        if self._pinned:
            return self._pinned
        # Rotate within the family that matches the browser, not across all
        # browsers — a coherent ClientHello beats a random one.
        return random.choice(BROWSER_MATCHED.get(self._family, IMPERSONATION_TARGETS))

    def _pick_proxy(self) -> Optional[str]:
        return random.choice(self._proxy_urls) if self._proxy_urls else None

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        """Seed the client with a browser context's cookies (cf_clearance included)."""
        if self._session is not None and cookies:
            try:
                self._session.cookies.update(cookies)
            except Exception:
                pass

    async def cookies(self) -> Dict[str, str]:
        if self._session is None:
            return {}
        try:
            return {c.name: c.value for c in self._session.cookies.jar}
        except Exception:
            return {}

    # -- core request ----------------------------------------------------- #

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        data: Optional[dict] = None,
        impersonate: Optional[str] = None,
        proxy: Optional[str] = None,
        allow_redirects: bool = True,
    ):
        await self.open()
        assert self._session is not None

        target = impersonate or self._pick_target()
        proxy_url = proxy or self._pick_proxy()

        last_exc: Optional[Exception] = None
        # Attempt the pinned target first; if curl_cffi does not know that
        # preset (older build), fall back to the generic family name once.
        ladder = [target]
        if target not in ("chrome", "firefox", "safari"):
            ladder.append(target.split("1")[0].rstrip("0123456789") or "chrome")

        for attempt in range(self._retries + 1):
            imp = ladder[min(attempt, len(ladder) - 1)]
            kwargs = dict(
                headers=headers,
                params=params,
                json=json,
                data=data,
                timeout=self._timeout,
                impersonate=imp,
                allow_redirects=allow_redirects,
            )
            if proxy_url:
                kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
            try:
                resp = await self._session.request(method, url, **kwargs)
                resp.impersonated_as = imp  # type: ignore[attr-defined]
                return resp
            except CurlError as e:  # preset unknown or transport error
                last_exc = e
                if "impersonate" in str(e).lower() or "not found" in str(e).lower():
                    continue
                if attempt < self._retries:
                    await asyncio.sleep(0.4 * (attempt + 1) + random.random() * 0.3)
                    continue
                raise
            except Exception as e:
                last_exc = e
                if attempt < self._retries:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise
        raise last_exc or RuntimeError("request failed")

    async def get(self, url: str, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw):
        return await self.request("POST", url, **kw)

    # -- full-page shortcut ---------------------------------------------- #

    async def get_text(self, url: str, **kw) -> str:
        """Fetch and return decoded body text, trying a couple of charsets."""
        r = await self.get(url, **kw)
        text = getattr(r, "text", None)
        if text:
            return text
        content = getattr(r, "content", b"") or b""
        for enc in ("utf-8", "latin-1"):
            try:
                return content.decode(enc)
            except Exception:
                continue
        return content.decode("utf-8", "replace")
