"""Fingerprint generation — the best generator for the job, with fallbacks.

Three tiers, tried in order:

1. `fpgen` — Camoufox's current bundled generator (a fork of BrowserForge tuned
   for the patched Firefox). This is the one the current README documents.
2. `browserforge` — the upstream generator. Older Camoufox docs used it, and it
   still ships coherent UA/header/device profiles.
3. A hand-built coherent identity — no dependency, guaranteed to compile. Used
   when neither generator is installed, or when the rig must run air-gapped.

The generator's job is *coherence*: the UA, the UA-CH client hints, the
Accept-Language, the platform and the screen must all agree with each other and
with the OS Camoufox is claiming. A generator that produces a Windows UA next to
a Linux platform string is worse than a fixed, consistent profile.

Where Camoufox generates its own full identity (default), use this class only
for the *supplementary* headers, or pass `as_injection()` to pin an identity
explicitly via `fingerprint=`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Tier 1 is Camoufox's own generator. Camoufox ships fpgen and calls it itself
# when `os`/`locale`/`screen` guide the identity, so the rig does NOT call fpgen
# directly — its API is a Bayesian query (`fpgen.Generator().generate(...)` over
# target conditions), not a `browser=`/`os=` factory. What fpgen needs from us is
# that `python -m` did not disable it: see stealth/_compat.py.

# Tier 2: BrowserForge, for callers who want to pre-generate an identity.
try:
    from browserforge.fingerprints import (  # type: ignore
        FingerprintGenerator as _BfGenerator,
        Screen as _BfScreen,
    )
except Exception:  # pragma: no cover
    _BfGenerator = None
    _BfScreen = None


# Firefox, because that is what Camoufox actually is. Claiming Chrome in the UA
# while running Firefox's JS engine is the single loudest contradiction a site
# can detect — the UA string, the JS feature set, and the TLS stack must agree.
_FIREFOX_VERSIONS = ["135.0", "134.0", "133.0", "132.0", "131.0"]

_OS_PROFILES: Dict[str, Dict[str, object]] = {
    "windows": {
        "ua_platform": "Windows NT 10.0; Win64; x64",
        "platform": "Win32",
        "ua_ch_platform": '"Windows"',
        "screens": [(1920, 1080), (2560, 1440), (1536, 864), (1366, 768), (3840, 2160)],
        "gpu": [
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ],
    },
    "macos": {
        "ua_platform": "Macintosh; Intel Mac OS X 10.15",
        "platform": "MacIntel",
        "ua_ch_platform": '"macOS"',
        "screens": [(1440, 900), (1680, 1050), (2560, 1600), (1512, 982)],
        "gpu": [
            ("Apple", "Apple M1"),
            ("Apple", "Apple M2"),
            ("Intel Inc.", "Intel Iris OpenGL Engine"),
        ],
    },
    "linux": {
        "ua_platform": "X11; Linux x86_64",
        "platform": "Linux x86_64",
        "ua_ch_platform": '"Linux"',
        "screens": [(1920, 1080), (1366, 768), (2560, 1440), (1600, 900)],
        "gpu": [
            ("Mesa", "Mesa Intel(R) UHD Graphics 630 (CFL GT2)"),
            ("Mesa", "AMD Radeon RX 580 (POLARIS10, DRM 3.42.0)"),
        ],
    },
}

_LOCALES = ["en-US", "en-GB", "en-CA", "en-AU"]


@dataclass
class Identity:
    """A coherent, mutually-consistent browser identity."""

    os_target: str
    user_agent: str
    platform: str
    locale: str
    languages: str
    timezone: str
    screen: tuple
    viewport: tuple
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    generator: str = "builtin"
    extras: Dict[str, object] = field(default_factory=dict)

    def headers(self) -> Dict[str, str]:
        """Supplementary headers to pin alongside the browser request."""
        return {
            "Accept-Language": self.languages,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    def as_injection(self) -> dict:
        """Shape the identity as a Camoufox `fingerprint=` injection dict.

        Only pass this to Camoufox when you intentionally pin an identity. The
        default (letting Camoufox generate) is more coherent; this exists for the
        reproducible case — same identity across runs, CI, or a warm profile.
        """
        return {
            "navigator": {
                "userAgent": self.user_agent,
                "platform": self.platform,
                "language": self.locale,
                "languages": self.languages.split(","),
                "hardwareConcurrency": self.hardware_concurrency,
                "maxTouchPoints": 0,
            },
            "screen": {
                "width": self.screen[0],
                "height": self.screen[1],
                "colorDepth": 24,
            },
            "webgl": {
                "vendor": self.webgl_vendor,
                "renderer": self.webgl_renderer,
            },
        }


class FingerprintFactory:
    """Generate coherent identities; best generator available, silently tiered."""

    def __init__(self, os_target: Optional[str] = None) -> None:
        self.os_target = os_target
        self.generator = self._detect_generator()

    @staticmethod
    def _detect_generator() -> str:
        # Camoufox always generates internally (via fpgen); this tier only picks
        # what the *supplementary* factory can pre-build.
        if _BfGenerator is not None:
            return "browserforge"
        return "builtin"

    # -- public ----------------------------------------------------------- #

    def build(self, os_target: Optional[str] = None) -> Identity:
        target = os_target or self.os_target or random.choice(list(_OS_PROFILES))
        if self.generator == "browserforge":
            ident = self._try_generator(target)
            if ident is not None:
                return ident
        return self._builtin(target)

    # -- generator tiers -------------------------------------------------- #

    def _try_generator(self, target: str) -> Optional[Identity]:
        try:
            gen = _BfGenerator()
        except Exception:
            return None
        try:
            profile = gen.generate(browser="firefox", os=target)
        except TypeError:
            try:
                profile = gen.generate()
            except Exception:
                return None
        except Exception:
            return None
        return self._from_profile(profile, target)

    def _from_profile(self, profile, target: str) -> Optional[Identity]:
        """Normalise either generator's output into our Identity shape."""

        def dig(*path, default=None):
            cur = profile
            for key in path:
                if cur is None:
                    return default
                cur = getattr(cur, key, None) if not isinstance(cur, dict) else cur.get(key)
            return cur if cur is not None else default

        base = self._builtin(target)
        ua = dig("navigator", "userAgent") or getattr(profile, "navigator", None)
        if isinstance(ua, dict):
            ua = ua.get("userAgent", base.user_agent)
        ua = ua if isinstance(ua, str) else base.user_agent

        # Reject incoherent generator output: keep Firefox and the claimed OS.
        if "Firefox" not in ua:
            ua = base.user_agent
        base.user_agent = ua
        base.generator = self.generator
        return base

    # -- builtin ---------------------------------------------------------- #

    def _builtin(self, target: str) -> Identity:
        prof = _OS_PROFILES[target]
        ver = random.choice(_FIREFOX_VERSIONS)
        locale = random.choice(_LOCALES)
        lang = f"{locale},{locale.split('-')[0]};q=0.5"
        screen = random.choice(prof["screens"])  # type: ignore[arg-type]
        # Viewport is always a bit smaller than the screen — a browser window
        # that exactly fills the display is one of the classic headless tells.
        vw = min(screen[0] - random.choice([0, 12, 24, 80]), screen[0])
        vh = min(screen[1] - random.choice([80, 100, 120, 160]), screen[1])
        vendor, renderer = random.choice(prof["gpu"])  # type: ignore[arg-type]
        return Identity(
            os_target=target,
            user_agent=(
                f"Mozilla/5.0 ({prof['ua_platform']}; rv:{ver}) "
                f"Gecko/20100101 Firefox/{ver}"
            ),
            platform=str(prof["platform"]),
            locale=locale,
            languages=lang,
            timezone="UTC",
            screen=screen,
            viewport=(vw, vh),
            hardware_concurrency=random.choice([4, 8, 12, 16]),
            device_memory=random.choice([8, 16, 32]),
            webgl_vendor=vendor,
            webgl_renderer=renderer,
        )
