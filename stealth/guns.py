"""The guns.lol view protocol, driven without a page.

A guns.lol profile view is not a page load. The site's own beacon posts a
signed payload to `/api/analytics/view`, and three things have to be true for
that payload to count:

1. **You hold the page's challenge.** The profile's Next.js flight payload
   carries a `_gpp_ch` object — version, challenge id, nonce, seal, the 64-hex
   challenge, a difficulty, and the Cloudflare Turnstile `action`/`data-cdata`
   the view widget will run with.
2. **You solve its proof-of-work.** The challenge id names a directory under
   `/_challenge/pow/<id>/` holding a worker, a wasm-bindgen glue module, and a
   binary. guns.lol *rotates* all three (and re-hashes the export names), so the
   worker's obfuscated glue import is decoded, the glue is parsed for the wasm
   URL and the rotated constructor/solve exports, and the binary is driven
   through the glue — see `stealth/powjs/runner.mjs`. The proof is `_oo`.
3. **You carry a Turnstile token and a `cf_clearance` for the POST path.**
   Cloudflare's Managed Challenge sits on `/api/analytics/view` itself, *not* on
   the profile page: a client with no token and no clearance is answered with a
   403 `cf-mitigated: challenge` interstitial even though the page served it
   200. This is the one part that cannot be computed locally — mint it with a
   captcha service (`stealth/captcha.py`) or hand one in.

The browser is therefore only needed to *obtain* clearances, never to record a
view: one clear pass through `stealth/session.py` mints a clearance for an
egress, and every view on that egress then costs a page GET, a wasm solve, and
one POST.

    client = GunsClient(state_dir=".stealth-state", proxy=proxy)
    await client.open()
    view = await client.record_view("someone", clearances=...)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi.requests import AsyncSession
except Exception:  # pragma: no cover - dependency missing at import time
    AsyncSession = None  # type: ignore

GUNS_ORIGIN = "https://guns.lol"
ANALYTICS_VIEW = f"{GUNS_ORIGIN}/api/analytics/view"
ANALYTICS_RECORD = f"{GUNS_ORIGIN}/api/analytics/record"
CHALLENGE_VERIFY = f"{GUNS_ORIGIN}/_challenge/verify"

# The Turnstile sitekey guns.lol renders the view widget with. It is public (it
# ships in the page) and rotates far more slowly than the PoW module.
TURNSTILE_SITEKEY = "0x4AAAAAAAgU7T2niLQD-TLm"

# `cf_clearance` is bound to the User-Agent that solved the challenge, so the
# whole flow — browser, captcha provider, and every request below — must use one
# value. Chrome 151 on Windows is the identity both the browser engine and the
# TLS-spoofed client present.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# The numeric device enum the endpoint expects, in the order the array uses.
DEVICE_NUM = {"desktop": 0, "mobile": 1, "tablet": 2}

# `_gpp_ch` lives inside a JavaScript string literal in the flight payload, so
# its interior quotes are escaped. The object is flat (no nested braces), which
# is what makes a brace-free body safe to match.
_CHALLENGE_RE = re.compile(r'\\"_gpp_ch\\":(\{[^{}]*\})')
_FLIGHT_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\/": "/"}

# The worker obfuscates every symbol with an inline XOR call: `_f([bytes], key)`.
_XOR_CALL_RE = re.compile(r"\[([0-9,]+)\],(\d+)\)")
# The glue names its binary in the init function.
_GLUE_WASM_RE = re.compile(r'new URL\("([^"]+)"')
# ...and calls every wasm export through `wasm._<hash>(args)`.
_GLUE_CALL_RE = re.compile(r"wasm\.(_[0-9a-f]+)\(([^)]*)\)")

# Response headers that mean "Cloudflare answered, not guns.lol".
_CF_HEADERS = ("cf-mitigated", "cf-ray")


@dataclass
class Challenge:
    """The `_gpp_ch` view challenge as the profile page publishes it."""

    version: int          # v — payload schema version
    id: str               # e — challenge id / worker directory name
    worker_url: str       # u — same-origin path of the PoW worker
    timestamp: int        # t — issue time (unix seconds)
    nonce: str            # n — per-challenge nonce
    seal: str             # s — opaque server seal, replayed in the solution
    difficulty: int       # d — required proof difficulty
    action: str           # a — Turnstile data-action
    cdata: str            # cd — Turnstile data-cdata
    proof_input: str      # c — 64-hex solver input

    @property
    def args(self) -> List[Any]:
        """Constructor arguments, in the worker's own order."""
        return [self.proof_input, self.difficulty, str(self.timestamp),
                self.nonce, self.seal]

    def ctor_like(self) -> Dict[str, Any]:
        """The raw short keys, for logging and diagnostics."""
        return {"v": self.version, "e": self.id, "t": self.timestamp,
                "d": self.difficulty, "a": self.action}


@dataclass
class WorkerScript:
    """What the worker module tells us about the module behind it."""

    glue_path: str        # e.g. "./_001a97d9c40aec62a5e53a2"
    class_name: str       # the solver class the worker instantiates
    solve_name: str       # the method that returns the proof
    proof_key: str        # the field the proof arrives in ("_oo")


@dataclass
class PowModule:
    """A fetched, cached PoW rotation: glue, binary, and export names."""

    id: str
    glue: str
    wasm: str
    class_name: str
    solve_name: str
    proof_key: str = "_oo"
    meta_path: str = ""

    def job(self, args: List[Any]) -> Dict[str, Any]:
        return {"glue": self.glue, "wasm": self.wasm, "args": args,
                "class": self.class_name, "solve": self.solve_name,
                "proofKey": self.proof_key}


@dataclass
class ViewAttempt:
    """The outcome of one record-a-view attempt, with stage timings."""

    ok: bool
    stage: str = ""
    status: int = 0
    detail: str = ""
    proof: str = ""
    seconds: float = 0.0
    stages: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "stage": self.stage, "status": self.status,
                "detail": self.detail, "seconds": round(self.seconds, 3),
                "stages": {k: round(v, 3) for k, v in self.stages.items()}}


# --------------------------------------------------------------------------- #
# Pure parsers — no network, no browser (these are what check_rig.py drives)
# --------------------------------------------------------------------------- #


def unescape_flight(raw: str) -> str:
    """Turn the escaped in-page substring back into JSON."""
    out = raw
    for find, replace in _FLIGHT_UNESCAPE.items():
        out = out.replace(find, replace)
    return out


def parse_challenge(html: str) -> Challenge:
    """Extract the `_gpp_ch` view challenge from a profile page body.

    Raises ValueError when the page has no challenge — an unchallenged page, a
    login wall, or a Cloudflare interstitial all look like that, and pretending
    otherwise would turn a blocked request into a silent miss.
    """
    match = _CHALLENGE_RE.search(html or "")
    if not match:
        raise ValueError("no _gpp_ch challenge in page (blocked, or not a profile)")
    try:
        data = json.loads(unescape_flight(match.group(1)))
    except json.JSONDecodeError as e:
        raise ValueError(f"_gpp_ch is not valid JSON: {e}") from e
    missing = [k for k in ("v", "e", "u", "t", "n", "s", "c", "d") if k not in data]
    if missing:
        raise ValueError(f"_gpp_ch missing fields: {', '.join(missing)}")
    return Challenge(
        version=int(data["v"]),
        id=str(data["e"]),
        worker_url=str(data["u"]),
        timestamp=int(data["t"]),
        nonce=str(data["n"]),
        seal=str(data["s"]),
        difficulty=int(data["d"]),
        action=str(data.get("a", "")),
        cdata=str(data.get("cd", "")),
        proof_input=str(data["c"]),
    )


def decode_worker(js: str) -> WorkerScript:
    """Reverse the worker's XOR obfuscation to recover its symbol names.

    The worker is a tiny shim: it decodes the glue module path, imports it,
    initialises it, constructs the solver with the challenge tuple, calls one
    method, and posts the `_oo` field back. Every one of those names is hidden
    behind `_helper([bytes], key)`, so decoding the byte arrays is enough to
    learn the whole ABI without executing anything.
    """
    strings: List[str] = []
    for match in _XOR_CALL_RE.finditer(js or ""):
        key = int(match.group(2))
        try:
            raw = bytes(int(b) ^ key for b in match.group(1).split(","))
        except ValueError:
            continue
        strings.append(raw.decode("utf-8", "replace"))
    if not strings:
        raise ValueError("worker has no decodable XOR strings")

    glue = next((s for s in strings if s.startswith("./")), "")
    if not glue:
        raise ValueError("worker does not name a glue module")

    # The class and its solve method are the two identifiers the worker uses on
    # the imported module; "default" is the module's init and the rest are
    # postMessage plumbing. Anything else that looks like an identifier and is
    # not a string literal we already know is a candidate — the module itself
    # confirms which is which at instantiation time.
    known = {glue, "onmessage", "postMessage", "data", "default"}
    idents = [s for s in strings if s not in known and re.fullmatch(r"[A-Za-z_$][\w$]*", s)]
    proof_key = next((s for s in idents if s.startswith("_") and len(s) <= 4), "_oo")
    rest = [s for s in idents if s != proof_key]
    if len(rest) < 2:
        raise ValueError(f"worker did not expose a class/solve pair (saw {idents})")
    return WorkerScript(glue_path=glue, class_name=rest[0], solve_name=rest[1],
                        proof_key=proof_key or "_oo")


def parse_glue(js: str) -> str:
    """The binary filename a wasm-bindgen glue module loads."""
    match = _GLUE_WASM_RE.search(js or "")
    if not match:
        raise ValueError("glue does not name a wasm binary")
    return match.group(1)


def glue_exports(js: str) -> Tuple[str, str]:
    """Best-effort (constructor, solve) export names straight from the glue.

    The worker already gives us these; this is the fallback for a rotation that
    renames its exports between the worker fetch and the glue fetch.
    """
    ctor = solve = ""
    for match in _GLUE_CALL_RE.finditer(js or ""):
        args = match.group(2)
        parts = [a.strip() for a in args.split(",")] if args.strip() else []
        if len(parts) == 9:
            ctor = ctor or match.group(1)
        elif len(parts) == 2 and "__wbg_ptr" in parts[1]:
            solve = solve or match.group(1)
    return ctor, solve


def view_payload(challenge: Challenge, proof: str, username: str,
                 device: str = "desktop", referrer: str = "") -> bytes:
    """The positional array the rewritten view beacon posts.

    `[turnstileToken, [v, e, t, n, s, c, proof], username, deviceNum, referrer]`
    """
    return json.dumps([
        "",
        [challenge.version, challenge.id, challenge.timestamp, challenge.nonce,
         challenge.seal, challenge.proof_input, proof],
        username,
        DEVICE_NUM.get(device, 0),
        referrer,
    ]).encode("utf-8")


def with_token(payload: bytes, token: str) -> bytes:
    """Replace the leading Turnstile slot of a built payload."""
    data = json.loads(payload.decode("utf-8"))
    data[0] = token
    return json.dumps(data).encode("utf-8")


def click_payload(username: str, link_id: str, referrer: str = "") -> bytes:
    """The link-click event body (`/api/analytics/record`)."""
    import random

    return json.dumps({
        "username": username,
        "event": "click",
        "linkId": link_id,
        "referrer": referrer or f"{GUNS_ORIGIN}/{username}",
        "deviceType": random.choice(["desktop", "mobile", "tablet"]),
    }).encode("utf-8")


def view_headers(username: str, ua: str = CHROME_UA) -> Dict[str, str]:
    """Browser-shaped headers for the analytics POST.

    The `sec-fetch-*`/`sec-ch-ua*` set and the same-origin `Origin`/`Referer`
    are not decoration: the endpoint is a same-origin fetch from the profile
    page, and a request that does not look like one is scored differently by
    Cloudflare before guns.lol ever sees it.
    """
    return {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        "Origin": GUNS_ORIGIN,
        "Referer": f"{GUNS_ORIGIN}/{username}",
        "sec-ch-ua": '"Chromium";v="151", "Google Chrome";v="151", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def is_cloudflare_challenge(status: int, headers: Dict[str, str], body: str = "") -> bool:
    """True when a response is Cloudflare's interstitial rather than the app."""
    low = {k.lower(): (v or "").lower() for k, v in (headers or {}).items()}
    if any(k in low for k in _CF_HEADERS) and status in (403, 429, 503):
        return True
    return status == 403 and "Please wait a moment" in (body or "")[:4000]


# --------------------------------------------------------------------------- #
# Proof-of-work
# --------------------------------------------------------------------------- #


class PowSolver:
    """Fetch, cache and drive the rotating guns.lol PoW module.

    The module is immutable under a challenge id (the id changes when guns.lol
    rotates), so caching by id is always current — which matters because a fleet
    would otherwise re-download the same three files per view.
    """

    def __init__(self, state_dir: str = ".stealth-state", *, node: str = "node",
                 log=None, timeout_s: float = 180.0) -> None:
        self.root = os.path.join(state_dir, "pow")
        self.node = node
        self.timeout_s = timeout_s
        self._log = log or (lambda *_a: None)
        self._runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "powjs", "runner.mjs")
        self.solves = 0
        self.cache_hits = 0
        os.makedirs(self.root, exist_ok=True)

    # -- availability ----------------------------------------------------- #

    def available(self) -> bool:
        return bool(shutil.which(self.node)) and os.path.exists(self._runner)

    def _dir(self, challenge_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", challenge_id or "")[:96]
        return os.path.join(self.root, safe)

    # -- cache ------------------------------------------------------------ #

    def load(self, challenge_id: str) -> Optional[PowModule]:
        d = self._dir(challenge_id)
        meta_path = os.path.join(d, "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        glue, wasm = os.path.join(d, meta.get("glue", "")), os.path.join(d, meta.get("wasm", ""))
        if not (meta.get("glue") and meta.get("wasm")
                and os.path.exists(glue) and os.path.exists(wasm)):
            return None
        self.cache_hits += 1
        return PowModule(id=challenge_id, glue=glue, wasm=wasm,
                         class_name=meta.get("class", ""), solve_name=meta.get("solve", ""),
                         proof_key=meta.get("proofKey", "_oo"), meta_path=meta_path)

    def store(self, challenge_id: str, worker: WorkerScript, glue_name: str,
              glue_js: str, wasm_name: str, wasm_bytes: bytes) -> PowModule:
        d = self._dir(challenge_id)
        os.makedirs(d, exist_ok=True)
        glue_file = os.path.join(d, "glue.mjs")
        wasm_file = os.path.join(d, "mod.wasm")
        # Write the binary first: a crash mid-download must not leave a compilable
        # glue next to a truncated wasm, which would fail only at solve time.
        with open(wasm_file + ".tmp", "wb") as fh:
            fh.write(wasm_bytes)
        os.replace(wasm_file + ".tmp", wasm_file)
        with open(glue_file + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(glue_js)
        os.replace(glue_file + ".tmp", glue_file)
        meta = {"glue": "glue.mjs", "wasm": "mod.wasm", "glueSource": glue_name,
                "wasmSource": wasm_name, "class": worker.class_name,
                "solve": worker.solve_name, "proofKey": worker.proof_key,
                "fetched": time.time()}
        with open(os.path.join(d, "meta.json") + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(os.path.join(d, "meta.json") + ".tmp", os.path.join(d, "meta.json"))
        return PowModule(id=challenge_id, glue=glue_file, wasm=wasm_file,
                         class_name=worker.class_name, solve_name=worker.solve_name,
                         proof_key=worker.proof_key,
                         meta_path=os.path.join(d, "meta.json"))

    # -- solve ------------------------------------------------------------ #

    async def solve(self, module: PowModule, challenge: Challenge) -> str:
        """Run the module for one challenge and return the `_oo` proof."""
        if not self.available():
            raise RuntimeError(
                f"proof-of-work needs node + {self._runner} (node: {shutil.which(self.node) or 'missing'})"
            )
        job_path = os.path.join(self._dir(challenge.id), f"job-{os.getpid()}.json")
        with open(job_path, "w", encoding="utf-8") as fh:
            json.dump(module.job(challenge.args), fh)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.node, self._runner, job_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"pow solve timed out after {self.timeout_s:.0f}s")
        finally:
            try:
                os.unlink(job_path)
            except OSError:
                pass
        line = (out or b"").decode("utf-8", "replace").strip().splitlines()
        payload: Dict[str, Any] = {}
        for candidate in reversed(line):
            try:
                payload = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if not payload.get("ok"):
            detail = payload.get("error") or (err or b"").decode("utf-8", "replace")[-200:]
            raise RuntimeError(f"pow solve failed: {detail}")
        self.solves += 1
        return str(payload.get("proof", ""))


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class GunsClient:
    """An HTTP client for the guns.lol view API, with its own cookie jar."""

    def __init__(self, *, state_dir: str = ".stealth-state", proxy: Optional[str] = None,
                 cookies: Optional[Dict[str, str]] = None, ua: str = CHROME_UA,
                 impersonate: str = "chrome", timeout: float = 30.0, log=None) -> None:
        self.state_dir = state_dir
        self.proxy = proxy
        self.ua = ua
        self.impersonate = impersonate
        self.timeout = timeout
        self.cookies: Dict[str, str] = dict(cookies or {})
        self.solver = PowSolver(state_dir, log=log)
        self._log = log or (lambda *_a: None)
        self._session = None
        self.last_clearance: Optional[Dict[str, str]] = None

    # -- lifecycle -------------------------------------------------------- #

    async def __aenter__(self) -> "GunsClient":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        if AsyncSession is None:
            raise RuntimeError("curl_cffi is not installed — run: pip install -r stealth/requirements.txt")
        if self._session is None:
            self._session = AsyncSession()
        for name, value in self.cookies.items():
            try:
                self._session.cookies.set(name, value, domain="guns.lol")
            except Exception:
                pass

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _proxies(self) -> Optional[Dict[str, str]]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    async def _request(self, method: str, url: str, *, data: Optional[bytes] = None,
                       headers: Optional[Dict[str, str]] = None):
        await self.open()
        assert self._session is not None
        kwargs: Dict[str, Any] = {
            "headers": headers or {},
            "timeout": self.timeout,
            "impersonate": self.impersonate,
            "allow_redirects": False,
        }
        if data is not None:
            kwargs["data"] = data
        proxies = self._proxies()
        if proxies:
            kwargs["proxies"] = proxies
        return await self._session.request(method, url, **kwargs)

    # -- page + challenge ------------------------------------------------- #

    async def fetch_page(self, username: str) -> Tuple[int, str, Dict[str, str]]:
        """GET a profile page, surviving the guns_clearance 307 handshake.

        guns.lol answers a client with no `guns_clearance` cookie with a 307 back
        to the same path *and* sets the cookie, so one replay is all it takes —
        the PoW interstitial (401) is a different, rarer subsystem and is left to
        the browser engine, which can actually render it.
        """
        headers = {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": GUNS_ORIGIN + "/",
        }
        url = f"{GUNS_ORIGIN}/{username}"
        resp = await self._request("GET", url, headers=headers)
        for attempt in range(3):
            for cookie in resp.cookies.jar:
                if cookie.name == "guns_clearance":
                    self.cookies["guns_clearance"] = cookie.value
                    self._log("guns", "captured guns_clearance cookie")
            if resp.status_code in (301, 302, 307, 308) and attempt < 2:
                location = resp.headers.get("location") or url
                if location.startswith("/"):
                    location = GUNS_ORIGIN + location
                headers["Referer"] = url
                resp = await self._request("GET", location, headers=headers)
                continue
            break
        body = getattr(resp, "text", None) or ""
        return resp.status_code, body, dict(resp.headers)

    async def challenge(self, username: str) -> Tuple[Challenge, int]:
        status, body, _ = await self.fetch_page(username)
        if status != 200:
            raise RuntimeError(f"profile page returned {status}")
        return parse_challenge(body), status

    # -- PoW module ------------------------------------------------------- #

    async def module(self, challenge: Challenge) -> PowModule:
        """Load the PoW module for a challenge, from cache when possible."""
        cached = self.solver.load(challenge.id)
        if cached:
            self._log("guns", f"pow module {challenge.id}: cache hit")
            return cached
        worker_url = challenge.worker_url
        if not worker_url.startswith("http"):
            worker_url = GUNS_ORIGIN + worker_url
        base_dir = worker_url.rsplit("/", 1)[0]

        def page_headers() -> Dict[str, str]:
            return {"User-Agent": self.ua, "Referer": GUNS_ORIGIN + "/"}

        worker_js = await self._get(worker_url, page_headers())
        worker = decode_worker(worker_js)
        glue_name = worker.glue_path[2:] if worker.glue_path.startswith("./") else worker.glue_path
        glue_url = f"{base_dir}/{glue_name}"
        glue_js = await self._get(glue_url, page_headers())
        wasm_name = parse_glue(glue_js)
        resp = await self._request("GET", f"{base_dir}/{wasm_name}", headers=page_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"pow wasm returned {resp.status_code}")
        ctor, solve = glue_exports(glue_js)
        if (not worker.class_name or not worker.solve_name) and ctor and solve:
            worker = WorkerScript(worker.glue_path, ctor or worker.class_name,
                                  solve or worker.solve_name, worker.proof_key)
        self._log("guns", f"pow module {challenge.id}: fetched "
                          f"(class={worker.class_name} solve={worker.solve_name})")
        return self.solver.store(challenge.id, worker, glue_name, glue_js, wasm_name,
                                 bytes(resp.content))

    async def _get(self, url: str, headers: Dict[str, str]) -> str:
        resp = await self._request("GET", url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"GET {url} returned {resp.status_code}")
        return getattr(resp, "text", None) or ""

    # -- the view --------------------------------------------------------- #

    async def record_view(self, username: str, *, token: str = "",
                          challenge: Optional[Challenge] = None,
                          device: str = "desktop", referrer: str = "") -> ViewAttempt:
        """Fetch the challenge, solve its PoW, and post the view.

        Every failure is attributed to the stage it happened in (`page`, `module`,
        `pow`, `post`), because "miss" on its own cannot tell you whether the
        egress was blocked, the rotation changed, or the token was refused.
        """
        started = time.time()
        stages: Dict[str, float] = {}
        try:
            t = time.time()
            if challenge is None:
                challenge, _ = await self.challenge(username)
            stages["page"] = time.time() - t

            t = time.time()
            module = await self.module(challenge)
            stages["module"] = time.time() - t

            t = time.time()
            proof = await self.solver.solve(module, challenge)
            stages["pow"] = time.time() - t
            if not proof:
                return ViewAttempt(False, "pow", 0, "empty proof", "", time.time() - started, stages)

            body = with_token(view_payload(challenge, proof, username, device, referrer), token)
            t = time.time()
            resp = await self._request("POST", ANALYTICS_VIEW, data=body,
                                       headers=view_headers(username, self.ua))
            stages["post"] = time.time() - t
        except Exception as e:
            return ViewAttempt(False, "error", 0, str(e).splitlines()[0],
                               time.time() - started, stages)

        text = getattr(resp, "text", None) or ""
        headers = dict(resp.headers)
        if is_cloudflare_challenge(resp.status_code, headers, text):
            return ViewAttempt(False, "post", resp.status_code,
                               "Cloudflare challenge on /api/analytics/view — this path "
                               "needs a Turnstile token and a cf_clearance minted for it",
                               proof, time.time() - started, stages)
        if resp.status_code != 200:
            return ViewAttempt(False, "post", resp.status_code, text[:200],
                               proof, time.time() - started, stages)
        return ViewAttempt(True, "post", resp.status_code, text[:200] or "ok",
                           proof, time.time() - started, stages)

    async def capture_interstitial(self, username: str) -> Tuple[int, str, Dict[str, str]]:
        """POST a throwaway view to capture the Cloudflare interstitial.

        A captcha provider that replays the challenge needs the *interstitial*
        itself, and the only way to be shown it is to provoke it exactly as the
        real request would: a POST to the analytics path. The response is the
        challenge page; the provider solves that, and the clearance it returns
        is good for every later POST from the same egress.
        """
        probe = json.dumps(["", [0, "", 0, "", "", "", ""] , username, 0, ""]).encode("utf-8")
        resp = await self._request("POST", ANALYTICS_VIEW, data=probe,
                                   headers=view_headers(username, self.ua))
        return resp.status_code, getattr(resp, "text", None) or "", dict(resp.headers)

    async def record_click(self, username: str, link_id: str, referrer: str = "") -> ViewAttempt:
        """Record a link-click event instead of a view."""
        started = time.time()
        try:
            resp = await self._request("POST", ANALYTICS_RECORD,
                                       data=click_payload(username, link_id, referrer),
                                       headers=view_headers(username, self.ua))
        except Exception as e:
            return ViewAttempt(False, "error", 0, str(e).splitlines()[0], "", time.time() - started)
        text = getattr(resp, "text", None) or ""
        if is_cloudflare_challenge(resp.status_code, dict(resp.headers), text):
            return ViewAttempt(False, "post", resp.status_code,
                               "Cloudflare challenge on /api/analytics/record",
                               "", time.time() - started)
        ok = resp.status_code == 200
        return ViewAttempt(ok, "post", resp.status_code, text[:200],
                           "", time.time() - started)


# --------------------------------------------------------------------------- #
# Clearance reuse + the provider-backed view
# --------------------------------------------------------------------------- #


def clearance_file(state_dir: str, egress: str, host: str = "guns.lol") -> str:
    """Where a minted cf_clearance for one egress is remembered between runs."""
    try:
        from .egress import clearance_path

        return clearance_path(state_dir, egress, host)
    except Exception:  # pragma: no cover - egress module unavailable
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", egress or "direct")
        return os.path.join(state_dir, f"clearance-{safe}-{host}.json")


def load_clearance(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if float(data.get("expires", 0)) < time.time():
        return {}
    return {str(k): str(v) for k, v in (data.get("cookies") or {}).items()}


def save_clearance(path: str, cookies: Dict[str, str], ttl_s: float = 1800.0) -> None:
    if not cookies:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cookies": cookies, "expires": time.time() + ttl_s}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


async def record_view_with_provider(client: "GunsClient", provider, username: str, *,
                                    device: str = "desktop",
                                    clearance_cache: Optional[str] = None,
                                    log=None) -> ViewAttempt:
    """Record a view using a captcha provider for the two uncomputable parts.

    Order matters for cost. The clearance is cached per egress and reused, so a
    provider is asked for a `cf_clearance` once per egress rather than once per
    view; the Turnstile token is genuinely per-view (it is single-use), and is
    only asked for after the challenge is known, because the widget's `action`
    and `data-cdata` come from the page's own `_gpp_ch`.
    """
    log = log or (lambda *_a: None)
    started = time.time()
    egress_cookies = dict(client.cookies)

    try:
        challenge, _ = await client.challenge(username)
    except Exception as e:
        return ViewAttempt(False, "page", 0, str(e).splitlines()[0], "", time.time() - started)

    # 1. clearance: cached first, minted only when there is none for this egress
    if clearance_cache:
        cached = load_clearance(clearance_cache)
        if cached.get("cf_clearance"):
            egress_cookies.update(cached)
            log("guns", "reusing cf_clearance for this egress")
    if not egress_cookies.get("cf_clearance"):
        from .captcha import ClearanceRequest

        status, html, _headers = await client.capture_interstitial(username)
        if status != 200:
            t = time.time()
            try:
                clear = await provider.solve_cf_clearance(ClearanceRequest(
                    page_url=f"{GUNS_ORIGIN}/{username}",
                    challenge_url=ANALYTICS_VIEW,
                    challenge_method="POST",
                    html=html.encode("utf-8", "replace"),
                    proxy=client.proxy or "",
                    user_agent=client.ua,
                    site_key=TURNSTILE_SITEKEY,
                ))
            except Exception as e:
                return ViewAttempt(False, "clearance", 0, str(e).splitlines()[0], "",
                                   time.time() - started)
            log("guns", f"cf_clearance minted in {time.time() - t:.1f}s")
            egress_cookies.update(clear.cookies or {"cf_clearance": clear.cookie})
            if clearance_cache:
                save_clearance(clearance_cache, egress_cookies)
            if clear.user_agent and clear.user_agent != client.ua:
                log("warn", "provider solved as a different User-Agent — the clearance may not match")

    client.cookies.update(egress_cookies)
    for name, value in egress_cookies.items():
        try:
            client._session.cookies.set(name, value, domain="guns.lol")
        except Exception:
            pass

    # 2. Turnstile token: per view, and only now that the widget's action is known
    token = ""
    try:
        from .captcha import TurnstileRequest

        t = time.time()
        token = await provider.solve_turnstile(TurnstileRequest(
            page_url=f"{GUNS_ORIGIN}/{username}",
            site_key=TURNSTILE_SITEKEY,
            action=challenge.action,
            cdata=challenge.cdata,
            proxy=client.proxy or "",
        ))
        log("guns", f"turnstile token in {time.time() - t:.1f}s")
    except Exception as e:
        return ViewAttempt(False, "token", 0, str(e).splitlines()[0], "", time.time() - started)

    return await client.record_view(username, token=token, challenge=challenge, device=device)


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #


class GunsRunner:
    """Record many views across egresses, each view on its own proxy session.

    Views are cheap next to a page load, so the unit of work is a view, not a
    browser: `concurrency` clients run at once, each with its own sticky proxy
    session (a `{session}` placeholder in the proxy credentials is re-rendered
    per view) and its own cookie jar.
    """

    def __init__(self, username: str, *, state_dir: str = ".stealth-state",
                 proxies: Optional[List[Any]] = None, tokens: Optional[Any] = None,
                 provider: Any = None, device: str = "desktop", log=None) -> None:
        self.username = username
        self.state_dir = state_dir
        self.proxies = list(proxies or [])
        self.tokens = tokens
        self.provider = provider
        self.device = device
        self._log = log or (lambda *_a: None)
        self.ok = 0
        self.failed = 0
        self.stages: Dict[str, int] = {}
        self.results: List[ViewAttempt] = []
        self._rr = 0

    def _egress(self, index: int) -> Optional[str]:
        """The proxy URL for one view, rendered fresh when the proxy rotates.

        A `{session}` placeholder resolves to a new token here, and therefore to
        a new exit IP — a view per IP is the whole point, since the site counts
        roughly one view per IP per day.
        """
        if not self.proxies:
            return None
        proxy = self.proxies[index % len(self.proxies)]
        try:
            return proxy.to_curl()
        except AttributeError:
            return str(proxy)

    def _clearance_path(self, proxy_url: Optional[str]) -> str:
        """Cache key for a clearance: the rendered egress, never just the host.

        A rotating proxy hands out a different exit IP per view, and
        `cf_clearance` is bound to the IP that solved it — so caching a rotating
        proxy's clearance under its host:port would replay one IP's cookie from
        another IP, which Cloudflare revokes on sight.
        """
        import hashlib

        from urllib.parse import urlparse

        if not proxy_url:
            return clearance_file(self.state_dir, "direct")
        u = urlparse(proxy_url)
        label = f"{u.hostname}:{u.port}"
        digest = hashlib.sha1(proxy_url.encode("utf-8")).hexdigest()[:10]
        return clearance_file(self.state_dir, f"{label}-{digest}")

    async def _one(self, index: int, sem: asyncio.Semaphore) -> ViewAttempt:
        async with sem:
            proxy = self._egress(index)
            cookie, token = await self._token_for(index)
            async with GunsClient(state_dir=self.state_dir, proxy=proxy,
                                  cookies=cookie) as client:
                if self.provider is not None and not token:
                    attempt = await record_view_with_provider(
                        client, self.provider, self.username, device=self.device,
                        clearance_cache=self._clearance_path(proxy), log=self._log)
                else:
                    attempt = await client.record_view(self.username, token=token,
                                                       device=self.device)
            self.result(attempt, index)
            return attempt

    async def _token_for(self, index: int) -> Tuple[Dict[str, str], str]:
        if self.tokens is None:
            return {}, ""
        value = self.tokens.get(index) if hasattr(self.tokens, "get") else await self.tokens(index)
        if isinstance(value, dict):
            return value.get("cookies", {}), value.get("token", "")
        return {}, str(value or "")

    def result(self, attempt: ViewAttempt, index: int) -> None:
        self.results.append(attempt)
        if attempt.ok:
            self.ok += 1
            self._log("hit", f"view {index + 1}: recorded in {attempt.seconds:.1f}s "
                             f"(pow {attempt.stages.get('pow', 0):.1f}s)")
        else:
            self.failed += 1
            self.stages[attempt.stage] = self.stages.get(attempt.stage, 0) + 1
            self._log("miss", f"view {index + 1}: {attempt.stage} — {attempt.detail}")

    async def run(self, count: int = 1, concurrency: int = 4) -> Dict[str, Any]:
        count = max(1, int(count))
        concurrency = max(1, min(int(concurrency), count))
        self._log("guns", f"{count} view(s) across {concurrency} worker(s) "
                          f"-> {GUNS_ORIGIN}/{self.username}")
        sem = asyncio.Semaphore(concurrency)
        await asyncio.gather(*(self._one(i, sem) for i in range(count)))
        return {
            "username": self.username,
            "ok": self.ok,
            "failed": self.failed,
            "stages": dict(self.stages),
        }
