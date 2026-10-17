"""olliE's Camoufox stealth rig.

An insane-stealth browser engine that sits beside the Chromium runner in
`runner.js`. Where that one drives stock Chromium through Playwright, this one
drives Camoufox (a patched Firefox build), injects a coherent fingerprint, adds
recorded human motion, spoofs the TLS ClientHello on the fast HTTP path, and
walks Cloudflare Turnstile the way a real Firefox does.

    from stealth import StealthSession, StealthConfig

    cfg = StealthConfig(target="someone")
    async with StealthSession(cfg) as s:
        await s.hit()

Everything is importable from the top level for convenience.
"""

from .captcha import (CaptchaError, CaptchaProvider, ClearanceRequest, ClearanceResult,
                      TurnstileRequest, build_provider)
from .chromium import ChromiumFleet, ChromiumSession, ensure_host_env
from .cloudflare import CloudflareGuard
from .config import StealthConfig, Proxy, parse_proxy_line, parse_proxy_text
from .egress import ClearanceBroker, EgressPool, EgressStat
from .fingerprint import FingerprintFactory
from .guns import (Challenge, GunsClient, GunsRunner, PowSolver, ViewAttempt,
                   parse_challenge, view_payload)
from .human import HumanBehavior
from .session import StealthSession, StealthFleet
from .tls import TLSFetch, IMPERSONATION_TARGETS

__all__ = [
    "StealthConfig",
    "Proxy",
    "parse_proxy_line",
    "parse_proxy_text",
    "ClearanceBroker",
    "EgressPool",
    "EgressStat",
    "FingerprintFactory",
    "HumanBehavior",
    "TLSFetch",
    "IMPERSONATION_TARGETS",
    "CloudflareGuard",
    "StealthSession",
    "StealthFleet",
    # fast Chromium engine
    "ChromiumSession",
    "ChromiumFleet",
    "ensure_host_env",
    # native guns.lol protocol
    "GunsClient",
    "GunsRunner",
    "PowSolver",
    "Challenge",
    "ViewAttempt",
    "parse_challenge",
    "view_payload",
    # captcha services
    "CaptchaProvider",
    "CaptchaError",
    "TurnstileRequest",
    "ClearanceRequest",
    "ClearanceResult",
    "build_provider",
]

__version__ = "1.0.0"
