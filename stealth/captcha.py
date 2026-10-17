"""Captcha-service providers: the two things a view cannot compute locally.

Everything else in a guns.lol view is arithmetic or HTTP — the challenge, the
proof-of-work, the payload. Two artifacts are not:

* a **Turnstile token** for the widget on the view, and
* a **`cf_clearance` for the POST path**. Cloudflare's Managed Challenge sits on
  `/api/analytics/view` itself, so the clearance a browser earns on the profile
  page does not carry over; the challenged request has to be replayed from the
  same IP, as the same User-Agent, after a service solves it.

Both are minted here, through whichever service the operator has a key for.
Every provider speaks the same two-step shape (`createTask` then poll), so they
share one base class and one polling loop, and each maps its own task payload
onto the same two requests.

    provider = build_provider("capmonster", key)
    clear = await provider.solve_cf_clearance(ClearanceRequest(...))
    token = await provider.solve_turnstile(TurnstileRequest(...))

A `cf_clearance` is bound to the IP *and* the User-Agent that solved it, so the
proxy handed to the provider must be the proxy this rig egresses through, and
the UA handed over must be the UA every later request presents — both are
plumbed through from `stealth/guns.py` (`CHROME_UA`).
"""

from __future__ import annotations

import asyncio
import base64
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

try:
    from curl_cffi.requests import AsyncSession
except Exception:  # pragma: no cover - dependency missing at import time
    AsyncSession = None  # type: ignore

# Providers poll on a 2-3s cadence; both CapMonster and Solverify document that,
# and throttle callers who poll tighter.
POLL_INTERVAL_S = 3.0
# Cloudflare interstitial tasks can run ~120s before timing out internally.
DEFAULT_DEADLINE_S = 180.0

SUPPORTED_PROVIDERS = ("capmonster", "solverify", "uncaptcha")


class CaptchaError(RuntimeError):
    """A provider refused the task or returned no solution."""


@dataclass
class TurnstileRequest:
    """The widget guarding a profile view."""

    page_url: str
    site_key: str
    action: str = ""      # data-action, from `_gpp_ch`
    cdata: str = ""       # data-cdata, from `_gpp_ch`
    proxy: str = ""


@dataclass
class ClearanceRequest:
    """A Cloudflare Managed Challenge that has to be cleared for a path."""

    page_url: str          # the profile page (the cookie's site)
    challenge_url: str     # the exact URL that was challenged
    challenge_method: str  # the method that drew the challenge (POST)
    html: bytes            # the "Just a moment" body captured from the 403
    proxy: str             # must be the proxy the rig egresses through
    user_agent: str        # the identity the cookie will be bound to
    site_key: str = ""     # enables the widget fast path where supported
    cookies: Dict[str, str] = field(default_factory=dict)


@dataclass
class ClearanceResult:
    cookie: str                                   # the cf_clearance value
    user_agent: str = ""                          # provider's UA, when it reports one
    cookies: Dict[str, str] = field(default_factory=dict)


def proxy_parts(url: str) -> Dict[str, Any]:
    """Split a proxy URL into the discrete fields captcha services expect.

    None of the providers accept inline credentials in one URL string, so this
    is the one place that reassembles `scheme://user:pass@host:port`.
    """
    if not url:
        return {}
    u = urlparse(url)
    if not u.scheme or not u.hostname:
        raise ValueError(f"proxy url {url!r} must include scheme and host")
    port = u.port
    if not port:
        raise ValueError(f"proxy url {url!r} must include a port")
    out: Dict[str, Any] = {"scheme": u.scheme, "host": u.hostname, "port": port}
    if u.username:
        out["login"] = u.username
        out["password"] = u.password or ""
    return out


class CaptchaProvider:
    """Base for the two-step task APIs. Subclasses supply the task payloads."""

    name = "base"
    base_url = ""

    def __init__(self, api_key: str, *, base_url: Optional[str] = None,
                 deadline_s: float = DEFAULT_DEADLINE_S,
                 poll_interval_s: float = POLL_INTERVAL_S, log=None) -> None:
        if not api_key:
            raise CaptchaError(f"no API key given for captcha provider {self.name!r}")
        self.api_key = api_key
        if base_url:
            self.base_url = base_url
        self.deadline_s = deadline_s
        self.poll_interval_s = poll_interval_s
        self._log = log or (lambda *_a: None)
        self._session = None
        self.tasks = 0

    async def __aenter__(self) -> "CaptchaProvider":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, path: str, body: Dict[str, Any],
                    headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if AsyncSession is None:
            raise CaptchaError("curl_cffi is not installed")
        if self._session is None:
            self._session = AsyncSession()
        url = self.base_url.rstrip("/") + path
        try:
            resp = await self._session.post(url, json=body, headers=headers or {},
                                            timeout=60.0, impersonate="chrome")
        except Exception as e:  # transport failures are transient, not fatal
            raise CaptchaError(f"{self.name} request to {url} failed: {e}") from e
        try:
            data = resp.json()
        except Exception as e:
            raise CaptchaError(f"{self.name} returned non-JSON ({resp.status_code}): "
                               f"{(getattr(resp, 'text', '') or '')[:200]}") from e
        if not isinstance(data, dict):
            raise CaptchaError(f"{self.name} returned an unexpected body")
        return data

    async def _poll(self, fetch, describe: str = "task") -> Dict[str, Any]:
        """Poll until the provider reports a finished task, or the clock runs out."""
        deadline = time.time() + self.deadline_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CaptchaError(f"{self.name}: {describe} timed out after {self.deadline_s:.0f}s")
            # Jitter keeps a fleet of workers from polling in lockstep.
            await asyncio.sleep(min(self.poll_interval_s, remaining) * random.uniform(0.85, 1.15))
            done, payload = await fetch()
            if done:
                return payload

    # -- the interface ---------------------------------------------------- #

    async def solve_turnstile(self, req: TurnstileRequest) -> str:
        raise NotImplementedError

    async def solve_cf_clearance(self, req: ClearanceRequest) -> ClearanceResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# CapMonster  (https://docs.capmonster.cloud)
# --------------------------------------------------------------------------- #


class CapMonster(CaptchaProvider):
    """CapMonster Cloud: `TurnstileTaskProxyless` and the `cf_clearance` job.

    CapMonster replays the captured interstitial it is handed, and it solves
    with the User-Agent it was given (it reports none back), so our own UA must
    already be the one the clearance will be bound to.
    """

    name = "capmonster"
    base_url = "https://api.capmonster.cloud"

    def _check(self, data: Dict[str, Any], op: str) -> None:
        if int(data.get("errorId", 0) or 0) != 0:
            code = data.get("errorCode") or ""
            desc = data.get("errorDescription") or ""
            detail = f"{code}: {desc}" if desc else (code or "unknown error")
            raise CaptchaError(f"capmonster {op} failed ({detail})")

    async def _create(self, task: Dict[str, Any]) -> int:
        data = await self._post("/createTask", {"clientKey": self.api_key, "task": task})
        self._check(data, "createTask")
        task_id = data.get("taskId")
        if not task_id:
            raise CaptchaError("capmonster createTask returned no taskId")
        self.tasks += 1
        return int(task_id)

    async def _result(self, task_id: int) -> Dict[str, Any]:
        data = await self._post("/getTaskResult", {"clientKey": self.api_key, "taskId": task_id})
        self._check(data, "getTaskResult")
        return data

    def _solution(self, data: Dict[str, Any], field_name: str, describe: str) -> str:
        solution = data.get("solution") or {}
        value = solution.get(field_name)
        if not value:
            raise CaptchaError(f"capmonster returned an empty {describe}")
        return str(value)

    async def solve_turnstile(self, req: TurnstileRequest) -> str:
        task: Dict[str, Any] = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": req.page_url,
            "websiteKey": req.site_key,
        }
        if req.action:
            task["pageAction"] = req.action
        if req.cdata:
            task["data"] = req.cdata
        task_id = await self._create(task)

        async def fetch() -> Tuple[bool, Dict[str, Any]]:
            data = await self._result(task_id)
            return (str(data.get("status")) == "ready"), data

        data = await self._poll(fetch, "turnstile token")
        return self._solution(data, "token", "turnstile token")

    async def solve_cf_clearance(self, req: ClearanceRequest) -> ClearanceResult:
        if not req.proxy:
            raise CaptchaError("a cf_clearance task requires a proxy (cf_clearance is IP-bound)")
        parts = proxy_parts(req.proxy)
        if parts.get("scheme") not in ("http", "https", "socks4", "socks5"):
            raise CaptchaError(f"unsupported proxy scheme {parts.get('scheme')!r}")
        task: Dict[str, Any] = {
            "type": "TurnstileTask",
            "websiteURL": req.challenge_url or req.page_url,
            # CapMonster requires the key field even when the challenge is not a
            # widget; the placeholder is what its own docs use.
            "websiteKey": req.site_key or "xxxxxx",
            "cloudflareTaskType": "cf_clearance",
            "htmlPageBase64": base64.b64encode(req.html or b"").decode("ascii"),
            "userAgent": req.user_agent,
            "proxyType": parts.get("scheme", "http"),
            "proxyAddress": parts.get("host", ""),
            "proxyPort": int(parts.get("port", 0)),
        }
        if parts.get("login"):
            task["proxyLogin"] = parts["login"]
            task["proxyPassword"] = parts.get("password", "")
        task_id = await self._create(task)

        async def fetch() -> Tuple[bool, Dict[str, Any]]:
            data = await self._result(task_id)
            return (str(data.get("status")) == "ready"), data

        data = await self._poll(fetch, "cf_clearance")
        cookie = self._solution(data, "cf_clearance", "cf_clearance")
        return ClearanceResult(cookie=cookie, cookies={"cf_clearance": cookie})


# --------------------------------------------------------------------------- #
# Solverify  (https://solverify.net/docs)
# --------------------------------------------------------------------------- #


class Solverify(CaptchaProvider):
    """Solverify: the `interstitial` task, which drives the challenge itself.

    It navigates the challenged request in its own browser — Cloudflare only
    issues `cf_clearance` when it actually challenges, and the profile page is
    served unchallenged — so it is handed the *challenged URL and method*, not
    the page URL. Its solution returns the whole cookie jar plus the UA it used,
    which is why the rig must then present that exact UA.
    """

    name = "solverify"
    base_url = "https://solver.solverify.net"

    def _check(self, data: Dict[str, Any], op: str) -> None:
        if int(data.get("errorId", 0) or 0) != 0:
            code = data.get("errorCode") or ""
            desc = data.get("errorDescription") or ""
            raise CaptchaError(f"solverify {op} failed ({code or 'error'}"
                               + (f": {desc}" if desc else "") + ")")

    async def _create(self, task: Dict[str, Any]) -> Any:
        data = await self._post("/createTask", {"clientKey": self.api_key, "task": task})
        self._check(data, "createTask")
        task_id = data.get("taskId")
        if not task_id:
            raise CaptchaError("solverify createTask returned no taskId")
        self.tasks += 1
        return task_id

    async def _result(self, task_id: Any) -> Tuple[bool, Dict[str, Any]]:
        data = await self._post("/getTaskResult", {"clientKey": self.api_key, "taskId": task_id})
        self._check(data, "getTaskResult")
        status = str(data.get("status") or "").lower()
        if status in ("failed", "error"):
            raise CaptchaError(f"solverify task failed ({data.get('errorCode') or status})")
        return status == "completed", data

    def _proxy_task(self, req: ClearanceRequest) -> Dict[str, Any]:
        if not req.proxy:
            raise CaptchaError("solverify's interstitial task requires an http proxy")
        parts = proxy_parts(req.proxy)
        if parts.get("scheme") not in ("http", "https"):
            # Documented: only http is accepted for this task type.
            raise CaptchaError("solverify accepts only http proxies for interstitial tasks")
        task = {
            "websiteURL": req.challenge_url or req.page_url,
            "proxyType": "http",
            "proxyAddress": parts.get("host", ""),
            "proxyPort": str(parts.get("port", "")),
        }
        if req.challenge_method:
            task["method"] = req.challenge_method.upper()
        if req.user_agent:
            task["useragent"] = req.user_agent
        if req.site_key:
            # Widget fast path: answer the Turnstile on a synthetic page at the
            # target origin instead of loading the whole bundle.
            task["siteKey"] = req.site_key
        if parts.get("login"):
            task["proxyLogin"] = parts["login"]
            task["proxyPassword"] = parts.get("password", "")
        return task

    async def solve_turnstile(self, req: TurnstileRequest) -> str:
        # The documented Turnstile path is the interstitial task with the widget
        # sitekey; the solution's `value` field carries the token.
        clear = await self.solve_cf_clearance(ClearanceRequest(
            page_url=req.page_url, challenge_url=req.page_url, challenge_method="GET",
            html=b"", proxy=req.proxy, user_agent="", site_key=req.site_key,
        ))
        if not clear.cookie:
            raise CaptchaError("solverify returned no token")
        return clear.cookie

    async def solve_cf_clearance(self, req: ClearanceRequest) -> ClearanceResult:
        task = {"type": "interstitial", **self._proxy_task(req)}
        if req.html:
            task["source"] = True
        task_id = await self._create(task)
        data = await self._poll(lambda: self._result(task_id), "cf_clearance")
        solution = data.get("solution") or {}
        cookies = {str(k): str(v) for k, v in (solution.get("cookies") or {}).items()}
        cookie = cookies.get("cf_clearance") or str(solution.get("value") or "")
        if not cookie:
            raise CaptchaError("solverify returned no cf_clearance")
        return ClearanceResult(cookie=cookie, user_agent=str(solution.get("useragent") or ""),
                               cookies=cookies or {"cf_clearance": cookie})


# --------------------------------------------------------------------------- #
# uncaptcha.io  (https://docs.uncaptcha.io)
# --------------------------------------------------------------------------- #


class Uncaptcha(CaptchaProvider):
    """uncaptcha.io: a single `/task/execute` call, keyed by header.

    The synchronous shape means there is no task id to poll: the response
    carries the solution, so the base polling loop is not used here.
    """

    name = "uncaptcha"
    base_url = "https://api.uncaptcha.io/v1"

    async def _execute(self, task_type: str, task_data: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._post("/task/execute",
                                {"task_type": task_type, "task_data": task_data},
                                headers={"X-Api-Key": self.api_key})
        if not data.get("success"):
            raise CaptchaError(f"uncaptcha {task_type} failed: {data.get('message') or 'unknown error'}")
        self.tasks += 1
        payload = data.get("data") or {}
        return payload.get("solution") or {}

    async def solve_turnstile(self, req: TurnstileRequest) -> str:
        task_data: Dict[str, Any] = {"sitekey": req.site_key, "url": req.page_url}
        if req.proxy:
            task_data["proxy"] = req.proxy
        if req.action:
            task_data["action"] = req.action
        if req.cdata:
            task_data["cdata"] = req.cdata
        solution = await self._execute("turnstile", task_data)
        token = solution.get("token")
        if not token:
            raise CaptchaError("uncaptcha returned no turnstile token")
        return str(token)

    async def solve_cf_clearance(self, req: ClearanceRequest) -> ClearanceResult:
        # `waf` replays the captured interstitial; `wafauto` makes the follow-up
        # request itself, which we cannot use because the retry has to carry our
        # own signed view payload.
        task_data: Dict[str, Any] = {"url": req.challenge_url or req.page_url}
        if req.proxy:
            task_data["proxy"] = req.proxy
        if req.user_agent:
            task_data["user_agent"] = req.user_agent
        if req.html:
            task_data["block_html"] = (req.html or b"").decode("utf-8", "replace")
        solution = await self._execute("waf", task_data)
        cookie = solution.get("clearance") or solution.get("token") or ""
        if not cookie:
            raise CaptchaError("uncaptcha returned no cf_clearance")
        return ClearanceResult(cookie=str(cookie), cookies={"cf_clearance": str(cookie)})


PROVIDERS = {
    CapMonster.name: CapMonster,
    Solverify.name: Solverify,
    Uncaptcha.name: Uncaptcha,
}


def build_provider(name: str, api_key: str, **kwargs: Any) -> CaptchaProvider:
    """Build the provider named on the command line."""
    key = (name or "").strip().lower()
    if key not in PROVIDERS:
        raise CaptchaError(f"unknown captcha provider {name!r} "
                           f"(supported: {', '.join(SUPPORTED_PROVIDERS)})")
    return PROVIDERS[key](api_key, **kwargs)
