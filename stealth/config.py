"""Stealth rig configuration and proxy parsing.

Mirrors the proxy grammar the Chromium runner already accepts in
`runner.js:parseProxyLine`, so a `proxies.txt` written for one engine works in
the other without edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Target OS handling
# --------------------------------------------------------------------------- #

# Camoufox claims one of these at the identity layer. The engine underneath is
# Firefox on the host OS, so "windows" is the safest claim for a Linux/VPS box:
# GPU/renderer and font sets line up with the largest population of real users.
OS_TARGETS = ("windows", "macos", "linux")


@dataclass
class Proxy:
    """One usable egress endpoint in Playwright's dict shape."""

    server: str
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def label(self) -> str:
        try:
            from urllib.parse import urlparse

            u = urlparse(self.server)
            return f"{u.hostname}:{u.port}"
        except Exception:  # pragma: no cover - defensive
            return self.server

    def to_playwright(self) -> dict:
        """The exact dict Playwright/Camoufox wants for `proxy=`."""
        out = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    def to_curl(self) -> str:
        """A single proxy URL for curl_cffi's `proxies=` argument."""
        if self.username and self.password:
            scheme, _, rest = self.server.partition("://")
            return f"{scheme}://{self.username}:{self.password}@{rest}"
        return self.server


def _fmt(scheme: str, host: str, port: str, user: Optional[str], pw: Optional[str]) -> Proxy:
    s = "socks5" if scheme.lower() == "socks5h" else scheme.lower()
    return Proxy(server=f"{s}://{host}:{port}", username=user or None, password=pw or None)


def parse_proxy_line(raw: str) -> Optional[Proxy]:
    """Parse one proxy line. Returns None when the line is not a proxy.

    Accepted (same set as runner.js):
        scheme://host:port
        scheme://user:pass@host:port
        host:port
        host:port:user:pass
        user:pass@host:port
    """
    import re

    line = str(raw).strip()
    if not line or line.startswith("#"):
        return None

    m = re.match(
        r"^(?P<scheme>https?|socks5h?|socks4)://"
        r"(?:(?P<user>[^:@/]+):(?P<pass>[^@/]+)@)?"
        r"(?P<host>[^:/]+):(?P<port>\d+)$",
        line,
        re.IGNORECASE,
    )
    if m:
        return _fmt(m["scheme"], m["host"], m["port"], m["user"], m["pass"])

    m = re.match(r"^(?P<host>[^:]+):(?P<port>\d+):(?P<user>[^:]+):(?P<pass>.+)$", line)
    if m:
        return _fmt("http", m["host"], m["port"], m["user"], m["pass"])

    m = re.match(r"^(?P<user>[^:@/]+):(?P<pass>[^@/]+)@(?P<host>[^:/]+):(?P<port>\d+)$", line)
    if m:
        return _fmt("http", m["host"], m["port"], m["user"], m["pass"])

    m = re.match(r"^(?P<host>[^:]+):(?P<port>\d+)$", line)
    if m:
        return _fmt("http", m["host"], m["port"], None, None)

    return None


def parse_proxy_text(text: str) -> Tuple[List[Proxy], List[str]]:
    """Split a proxy blob into (usable, rejected-with-reason)."""
    good: List[Proxy] = []
    bad: List[str] = []
    for line in str(text or "").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        p = parse_proxy_line(line)
        if not p:
            bad.append(f"{line.strip()} (unrecognised format)")
        else:
            good.append(p)
    return good, bad


def load_proxies(path: str) -> Tuple[List[Proxy], List[str]]:
    if not path or not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8") as fh:
        return parse_proxy_text(fh.read())


# --------------------------------------------------------------------------- #
# Main config
# --------------------------------------------------------------------------- #


@dataclass
class StealthConfig:
    """Everything one stealth run needs. Sensible insane-defaults throughout."""

    target: str = ""
    base_url: str = "https://guns.lol"
    proxies: List[Proxy] = field(default_factory=list)
    proxy_file: str = ""

    # fleet shape
    contexts: int = 2
    tabs: int = 1

    # browser identity
    headless: bool = True            # True | False | "virtual" (headed on Xvfb)
    os_target: Optional[str] = None  # None -> pick per context; else one of OS_TARGETS
    humanize: bool = True            # Camoufox recorded-cursor replay
    geoip: bool = True               # align tz/locale/webrtc with the exit IP
    locale: Optional[str] = None
    block_images: bool = False
    block_webrtc: bool = True
    disable_coop: bool = True        # reach Turnstile widgets inside cross-origin iframes
    main_world_eval: bool = True
    i_know_what_im_doing: bool = True  # silence Camoufox's own config warnings

    # TLS / fast HTTP path
    tls_impersonate: Optional[str] = None  # None -> rotate through the pool

    # timing
    nav_timeout_ms: int = 60_000
    dwell: Tuple[int, int] = (9_000, 22_000)
    round_gap: Tuple[int, int] = (20_000, 60_000)
    warmup: bool = True              # visit about:blank + a benign page first

    # fleet process control. A starved browser launch on a busy host can stall;
    # the watchdog bounds each worker so one stall can never hang the run.
    worker_timeout_s: int = 240      # hard ceiling per worker process
    spawn_stagger_s: float = 1.5     # gap between worker spawns (cold-start RAM spike)
    launch_retries: int = 1          # relaunches when the browser never came up
    launch_retry_delay: Tuple[float, float] = (4.0, 7.0)

    # Cloudflare
    solve_turnstile: bool = True
    cf_timeout_ms: int = 45_000
    state_dir: str = ".stealth-state"
    # Several workers can share one egress (a small proxy pool, or none at all).
    # Sharing a clearance beats each worker solving the same challenge on the
    # same IP — for the egress there is only ever one solve. See egress.py.
    share_clearance: bool = True
    clearance_wait_s: float = 30.0   # how long to wait for a peer's clearance

    # debugging
    debug_config: bool = False
    verbose: bool = False

    # ---- derived ----

    @property
    def url(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/{self.target.strip().strip('/')}"

    def resolved_proxies(self) -> Tuple[List[Proxy], List[str]]:
        """Explicit proxies win; otherwise the proxy file; otherwise empty (direct)."""
        if self.proxies:
            return list(self.proxies), []
        if self.proxy_file:
            return load_proxies(self.proxy_file)
        return [], []

    def validate(self) -> None:
        t = self.target.strip().strip("/")
        if not t:
            raise ValueError("target required")
        if not all(c.isalnum() or c in "._-" for c in t):
            raise ValueError("target has invalid characters")
        if self.os_target and self.os_target not in OS_TARGETS:
            raise ValueError(f"os_target must be one of {OS_TARGETS}")
