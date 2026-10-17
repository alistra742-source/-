#!/usr/bin/env python3
"""Offline checks for the fast rails: guns.lol protocol, PoW, captcha, Chromium.

Split out of `check_rig.py` and imported by it, so the fleet/egress suite stays
readable. Same contract: no browser, no network, exit non-zero on any failure.

The fixtures are *captured from the live site* — a real `_gpp_ch` object, a real
worker module, a real wasm filename and export pair — because the whole point of
this code is surviving guns.lol rotating them. A hand-written fixture would pass
against a parser that no longer matches the site.

    H  the native guns.lol protocol: challenge, payload, headers, block detection
    I  the PoW harness (real captured worker) + the Node runner's own selftest
    J  captcha providers against a local stub: success, refusal, timeout, bad proxy
    K  the Chromium engine's host bootstrap and resource blocking
    L  rotating-proxy sessions: fresh IP per view, never a shared clearance
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.dom.minidom

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stealth import captcha as cap  # noqa: E402
from stealth import chromium as chrom  # noqa: E402
from stealth import guns  # noqa: E402
from stealth.config import Proxy, new_session_id  # noqa: E402

# --- captured fixtures ----------------------------------------------------- #
# A profile page's flight payload, exactly as the escaping appears in the HTML.
CHALLENGE_HTML = (
    'self.__next_f.push([1,"13:\\"_gpp_ch\\":{\\"v\\":3,\\"e\\":\\"vuzm9wbsBHbIki9HxfnIIA\\",'
    '\\"u\\":\\"/_challenge/pow/vuzm9wbsBHbIki9HxfnIIA/_51915aee78384c31cd9125f\\",'
    '\\"t\\":1792222178,\\"n\\":\\"cZNX7vuXbCE6HAoC3LxSWhEEgHrHiIPW\\",'
    '\\"s\\":\\"zkAFHCY1AAgBAwIABAOc_KYt\\",'
    '\\"c\\":\\"1672db63701ed2bf08c8cc5f680c2155c1aa4b6f4cd6bf1316ee59800cdefa30\\",'
    '\\"d\\":5,\\"cd\\":\\"1672db63701ed2bf08c8cc5f680c2155c1aa4b6f4cd6bf1316ee59800cdefa30\\",'
    '\\"a\\":\\"guns_view\\"}"])'
)

# The worker module, verbatim: every symbol is XOR-obfuscated inline.
WORKER_JS = (
    "const _df15d757=(e,t)=>String.fromCharCode(...e.map(e=>e^t));"
    "self[_df15d757([94,95,92,84,66,66,80,86,84],49)]=async function(e){try{"
    "const n=await import(_df15d757([31,30,110,1,1,0,80,8,6,85,8,82,5,1,80,84,82,7,3,80,4,84,4,2,80,3],49)),"
    "t=e[_df15d757([85,80,69,80],49)];"
    "await n[_df15d757([85,84,87,80,68,93,69],49)]();"
    "const s=new n[_df15d757([110,3,5,9,4,0,83,9,5,87,87,5],49)](t[0],Number(t[1]),String(t[2]),t[3],t[4]),"
    "o=await s[_df15d757([110,9,82,83,7,4,83,0,5,9,5,0],49)]();"
    "self[_df15d757([65,94,66,69,124,84,66,66,80,86,84],49)](o[_df15d757([110,94,94],49)])}catch{}}"
)

# The glue's shape: `new URL("<binary>", ...)` plus hashed wasm export calls.
GLUE_JS = (
    'export{initSync,__wbg_init as default};'
    'async function __wbg_init(){const t=new URL("_fe4203b9bd16800ee24ce58",import.meta.url);}'
    'class _24851b84ff4{constructor(){wasm._aa39262298338(this,0);}'
    '_8cb65b14841(){return wasm._b3197fcf54d30996cf3(1,this.__wbg_ptr);}}'
)


# --------------------------------------------------------------------------- #
# H — the native guns.lol protocol
# --------------------------------------------------------------------------- #


def check_guns_protocol(_tmp: str, check) -> None:
    ch = guns.parse_challenge(CHALLENGE_HTML)
    check("H1 the page's escaped _gpp_ch parses", ch.id == "vuzm9wbsBHbIki9HxfnIIA", f"id={ch.id!r}")
    check("H2 every challenge field is expanded from its short key",
          (ch.version, ch.difficulty, ch.timestamp, ch.action) == (3, 5, 1792222178, "guns_view")
          and ch.worker_url.startswith("/_challenge/pow/")
          and len(ch.proof_input) == 64,
          f"got {ch.ctor_like()} {ch.proof_input[:12]}")
    check("H3 the solver arguments match the worker's own order",
          ch.args == [ch.proof_input, 5, "1792222178", ch.nonce, ch.seal], f"args={ch.args[:1]}")

    bad = 0
    for sample in ("", "<html>no challenge here</html>",
                   '\\"_gpp_ch\\":{\\"v\\":3,\\"e\\":\\"x\\"}'):
        try:
            guns.parse_challenge(sample)
        except ValueError:
            bad += 1
    check("H4 a page without a usable challenge raises instead of inventing one", bad == 3, f"raised {bad}/3")

    worker = guns.decode_worker(WORKER_JS)
    check("H5 the worker's XOR strings decode to the glue, class and solve names",
          worker.glue_path == "./_001a97d9c40aec62a5e53a2"
          and worker.class_name == "_24851b84ff4"
          and worker.solve_name == "_8cb65b14841"
          and worker.proof_key == "_oo",
          f"got {worker}")
    check("H6 the glue names its binary and its own exports",
          guns.parse_glue(GLUE_JS) == "_fe4203b9bd16800ee24ce58"
          and guns.glue_exports(GLUE_JS)[1] == "_b3197fcf54d30996cf3",
          f"got {guns.glue_exports(GLUE_JS)}")

    payload = json.loads(guns.view_payload(ch, "PROOF", "someone").decode())
    check("H7 the view payload is the positional array the beacon posts",
          payload[0] == "" and payload[1] == [3, ch.id, ch.timestamp, ch.nonce, ch.seal,
                                              ch.proof_input, "PROOF"]
          and payload[2] == "someone" and payload[3] == 0 and payload[4] == "",
          f"got {payload}")
    check("H8 the device enum is numeric, and mobile is not desktop",
          json.loads(guns.view_payload(ch, "p", "u", "mobile").decode())[3] == 1)
    check("H9 a token can be dropped into a built payload without rebuilding it",
          json.loads(guns.with_token(guns.view_payload(ch, "p", "u"), "tok").decode())[0] == "tok")

    headers = guns.view_headers("someone")
    needed = {"Origin", "Referer", "sec-fetch-site", "sec-fetch-mode", "Content-Type", "User-Agent"}
    check("H10 the POST carries the same-origin browser headers",
          needed.issubset(headers) and headers["Referer"] == "https://guns.lol/someone"
          and headers["Origin"] == "https://guns.lol",
          f"missing {needed - set(headers)}")
    check("H11 a Cloudflare interstitial is recognised, and a normal 200 is not",
          guns.is_cloudflare_challenge(403, {"cf-mitigated": "challenge"}, "")
          and guns.is_cloudflare_challenge(403, {}, 'x<html><head>Please wait a moment</head>')
          and not guns.is_cloudflare_challenge(200, {}, "ok")
          and not guns.is_cloudflare_challenge(403, {}, "guns.lol says no"))

    click = json.loads(guns.click_payload("someone", "link-uuid").decode())
    check("H12 a click event posts the documented record shape",
          click["event"] == "click" and click["linkId"] == "link-uuid"
          and click["deviceType"] in ("desktop", "mobile", "tablet")
          and click["referrer"].endswith("/someone"),
          f"got {click}")


# --------------------------------------------------------------------------- #
# I — the PoW harness
# --------------------------------------------------------------------------- #


def check_pow_harness(tmp: str, check) -> None:
    node = shutil.which("node")
    runner = os.path.join(ROOT, "stealth", "powjs", "runner.mjs")
    check("I0 the PoW runner file is where the solver looks for it", os.path.exists(runner))
    if not node:
        check("I1 the Node harness discovers the solver class and drives it", False,
              "node is not installed — cannot run the harness")
        return

    proc = subprocess.run([node, runner, "--selftest"], capture_output=True, text=True, timeout=60)
    out: dict = {}
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        out = {}
    check("I1 the harness discovers the solver class and drives it",
          proc.returncode == 0 and out.get("ok") is True
          and out.get("discovered", {}).get("method") == "_solve123",
          f"rc={proc.returncode} out={proc.stdout[:160]} stderr={proc.stderr[-160:]}")

    # A job whose glue rejects the arguments must fail loudly, not return a proof.
    glue = os.path.join(tmp, "bad-glue.mjs")
    with open(glue, "w", encoding="utf-8") as fh:
        fh.write("export function initSync(){}\n"
                 "export class _c { constructor(){ throw new Error('bad challenge args'); } _s(){} }\n")
    empty_wasm = os.path.join(tmp, "empty.wasm")
    with open(empty_wasm, "wb") as fh:
        fh.write(b"\x00asm\x01\x00\x00\x00")   # a valid, empty module
    job = {"glue": glue, "wasm": empty_wasm, "args": [1],
           "class": "_c", "solve": "_s"}
    job_path = os.path.join(tmp, "job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    proc = subprocess.run([node, runner, job_path], capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        out = {}
    check("I2 a module that rejects the challenge reports an error and a non-zero exit",
          proc.returncode != 0 and out.get("ok") is False and "constructor" in str(out.get("error", "")),
          f"rc={proc.returncode} out={proc.stdout[:160]}")

    # The cache must be keyed by challenge id, and must not hand back a partial
    # module: a truncated download that still "loads" fails only at solve time.
    solver = guns.PowSolver(state_dir=tmp, node=node)
    cid = "vuzm9wbsBHbIki9HxfnIIA"
    check("I3 the solver is available when node and the runner are present", solver.available())
    check("I4 a challenge with nothing cached loads as a miss", solver.load(cid) is None)

    worker = guns.decode_worker(WORKER_JS)
    module = solver.store(cid, worker, "glue.mjs", GLUE_JS, "mod.wasm", b"\x00asm")
    cached = solver.load(cid)
    check("I5 a fetched module round-trips through the cache",
          cached is not None and cached.class_name == worker.class_name
          and cached.solve_name == worker.solve_name and os.path.exists(cached.wasm),
          f"got {cached}")
    check("I6 the cached module is keyed by challenge id, not a fixed path",
          module.meta_path != solver._dir("another-id") and cid in module.meta_path,
          f"meta={module.meta_path}")
    check("I7 a half-written module is not treated as a cache hit",
          (os.remove(os.path.join(solver._dir(cid), "mod.wasm")),
           solver.load(cid) is None)[1])
    os.makedirs(solver._dir("../escape"), exist_ok=True)
    check("I8 a hostile challenge id cannot escape the cache directory",
          os.path.abspath(solver._dir("../../etc/passwd")).startswith(os.path.abspath(solver.root)),
          f"path={solver._dir('../../etc/passwd')}")


# --------------------------------------------------------------------------- #
# J — captcha providers, against a local stub
# --------------------------------------------------------------------------- #


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """A stand-in for a createTask/getTaskResult service. Scripted, per mode."""

    mode = "ready"
    calls = 0
    seen: list = []          # one entry per request body, in order
    types: list = []         # the task type of each request ("" when it has none)

    def log_message(self, *_a):  # keep the check output clean
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls += 1
        type(self).seen.append(body)
        type(self).types.append(str((body.get("task") or {}).get("type", ""))
                                or str(body.get("task_type", "")))

        # The mode-specific result branches come first: a generic
        # "any getTaskResult" branch above them would shadow every mode.
        if self.mode == "refuse":
            payload = {"errorId": 1, "errorCode": "ERROR_KEY_DENIED",
                       "errorDescription": "bad key"}
        elif self.mode == "uncaptcha":
            payload = {"success": True, "data": {"solution": {"token": "unc-token",
                                                              "clearance": "unc-clearance"}}}
        elif self.mode == "solverify" and self.path.endswith("getTaskResult"):
            payload = {"errorId": 0, "status": "completed",
                       "solution": {"value": "v", "useragent": "UA-from-solver",
                                    "cookies": {"cf_clearance": "solverify-clearance"}}}
        elif self.mode == "working" and self.path.endswith("getTaskResult"):
            payload = {"errorId": 0, "status": "processing"}
        elif self.path.endswith("getTaskResult"):
            payload = {"errorId": 0, "status": "ready",
                       "solution": {"token": "sol-token", "cf_clearance": "sol-clearance"}}
        else:
            payload = {"errorId": 0, "taskId": 4242}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _Stub:
    """A local HTTP server standing in for a captcha service."""

    def __init__(self, mode: str = "ready") -> None:
        self.handler = type("H", (_StubHandler,), {"mode": mode, "calls": 0,
                                                   "seen": [], "types": []})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def check_captcha(_tmp: str, check) -> None:
    check("J1 an unknown provider name is refused, and names the supported set",
          _raises(lambda: cap.build_provider("nope", "k")) and
          all(p in str(_raises(lambda: cap.build_provider("nope", "k"), True))
              for p in cap.SUPPORTED_PROVIDERS))

    check("J2 a provider with no key is refused up front",
          _raises(lambda: cap.build_provider("capmonster", "")))

    ok = _raises(lambda: cap.proxy_parts("http://hostonly"))
    check("J3 a proxy without a port is rejected rather than half-parsed", ok)
    parts = cap.proxy_parts("http://user:pw@1.2.3.4:8080")
    check("J4 an authenticated proxy splits into the fields services want",
          parts == {"scheme": "http", "host": "1.2.3.4", "port": 8080,
                    "login": "user", "password": "pw"}, f"got {parts}")

    # Every scenario runs inside ONE event loop: a provider keeps one
    # AsyncSession, and an AsyncSession cannot be reused from a second loop.
    # (That is a real constraint of the client, not a quirk of this test.)

    # -- CapMonster: create → poll → token, and the clearance task shape -- #
    stub = _Stub("ready")
    try:
        async def scenario():
            p = cap.CapMonster("key", base_url=stub.url, poll_interval_s=0.01)
            token = await p.solve_turnstile(cap.TurnstileRequest(
                page_url="https://guns.lol/someone", site_key="0x4AAAA",
                action="guns_view", cdata="cd"))
            clear = await p.solve_cf_clearance(cap.ClearanceRequest(
                page_url="https://guns.lol/someone", challenge_url=guns.ANALYTICS_VIEW,
                challenge_method="POST", html=b"<html>interstitial</html>",
                proxy="http://u:p@1.2.3.4:8080", user_agent=guns.CHROME_UA))
            await p.close()
            return token, clear

        token, clear = asyncio.run(scenario())
        check("J5 capmonster returns the turnstile token from the polled result",
              token == "sol-token", f"got {token!r}")
        sent = stub.handler.seen[0]
        check("J6 capmonster's turnstile task is proxyless and carries the widget's action/cdata",
              sent["clientKey"] == "key" and stub.handler.types[0] == "TurnstileTaskProxyless"
              and sent["task"]["pageAction"] == "guns_view" and sent["task"]["data"] == "cd",
              f"got {json.dumps(sent)[:200]}")
        task = stub.handler.seen[2]["task"]
        check("J7 capmonster's clearance task targets the challenged POST, not the page",
              task["cloudflareTaskType"] == "cf_clearance"
              and task["websiteURL"] == guns.ANALYTICS_VIEW
              and task["proxyAddress"] == "1.2.3.4" and task["proxyPort"] == 8080
              and task["proxyLogin"] == "u"
              and __import__("base64").b64decode(task["htmlPageBase64"]) == b"<html>interstitial</html>",
              f"got {json.dumps(task)[:220]}")
        check("J8 the clearance comes back as the cookie, bound to our identity",
              clear.cookie == "sol-clearance" and clear.cookies["cf_clearance"] == "sol-clearance")
        check("J9 the UA we send is the UA the rig will present",
              task["userAgent"] == guns.CHROME_UA)
    finally:
        stub.close()

    # -- a provider that refuses the task -- #
    stub = _Stub("refuse")
    try:
        err = _raises(lambda: asyncio.run(_one_call(
            lambda p: p.solve_turnstile(cap.TurnstileRequest(page_url="u", site_key="k")),
            stub.url)), True)
        check("J10 a refused task surfaces the provider's own error code",
              "ERROR_KEY_DENIED" in str(err), f"got {err}")
    finally:
        stub.close()

    # -- a task that never finishes must time out, not hang -- #
    stub = _Stub("working")
    try:
        started = time.time()
        err = _raises(lambda: asyncio.run(_one_call(
            lambda p: p.solve_turnstile(cap.TurnstileRequest(page_url="u", site_key="k")),
            stub.url, deadline_s=0.4)), True)
        check("J11 a task that never completes times out with a reason",
              "timed out" in str(err) and time.time() - started < 5, f"got {err}")
    finally:
        stub.close()

    # -- a clearance task with no proxy is refused before spending money -- #
    err = _raises(lambda: asyncio.run(_one_call(
        lambda p: p.solve_cf_clearance(cap.ClearanceRequest(
            page_url="u", challenge_url="c", challenge_method="POST", html=b"",
            proxy="", user_agent="ua")),
        "http://127.0.0.1:1")), True)
    check("J12 a clearance task without a proxy is refused locally (cf_clearance is IP-bound)",
          "requires a proxy" in str(err), f"got {err}")

    # -- Solverify: the interstitial task, and the UA it reports back -- #
    stub = _Stub("solverify")
    try:
        clear = asyncio.run(_one_call(
            lambda p: p.solve_cf_clearance(cap.ClearanceRequest(
                page_url="https://guns.lol/someone", challenge_url=guns.ANALYTICS_VIEW,
                challenge_method="POST", html=b"<html>i</html>",
                proxy="http://u:p@1.2.3.4:8080", user_agent=guns.CHROME_UA,
                site_key=guns.TURNSTILE_SITEKEY)),
            stub.url, provider=cap.Solverify))
        task = stub.handler.seen[0]["task"]
        check("J13 solverify drives the challenged POST itself and replays our proxy",
              task["type"] == "interstitial" and task["method"] == "POST"
              and task["proxyAddress"] == "1.2.3.4" and task["useragent"] == guns.CHROME_UA,
              f"got {json.dumps(task)[:220]}")
        check("J14 solverify's sitekey enables the widget fast path",
              task["siteKey"] == guns.TURNSTILE_SITEKEY)
        check("J15 solverify's whole cookie jar is returned, with the UA to replay",
              clear.cookie == "solverify-clearance" and clear.user_agent == "UA-from-solver",
              f"got {clear}")
    finally:
        stub.close()

    err = _raises(lambda: asyncio.run(_one_call(
        lambda p: p.solve_cf_clearance(cap.ClearanceRequest(
            page_url="u", challenge_url="c", challenge_method="POST", html=b"",
            proxy="socks5://1.2.3.4:1080", user_agent="ua")),
        "http://127.0.0.1:1", provider=cap.Solverify)), True)
    check("J16 solverify's http-only proxy rule is enforced locally, not by the server",
          "http proxies" in str(err), f"got {err}")

    # -- uncaptcha.io: single call, header auth -- #
    stub = _Stub("uncaptcha")
    try:
        async def unc_scenario():
            p = cap.Uncaptcha("key", base_url=stub.url)
            token = await p.solve_turnstile(cap.TurnstileRequest(
                page_url="https://guns.lol/someone", site_key="0x4AAAA",
                action="a", cdata="cd"))
            clear = await p.solve_cf_clearance(cap.ClearanceRequest(
                page_url="u", challenge_url=guns.ANALYTICS_VIEW, challenge_method="POST",
                html=b"<html>x</html>", proxy="http://1.2.3.4:8080", user_agent="ua"))
            await p.close()
            return token, clear

        token, clear = asyncio.run(unc_scenario())
        body = stub.handler.seen[0]
        check("J17 uncaptcha posts one execute call keyed by header",
              stub.handler.calls == 2 and body["task_type"] == "turnstile"
              and body["task_data"] == {"sitekey": "0x4AAAA", "url": "https://guns.lol/someone",
                                        "action": "a", "cdata": "cd"},
              f"got {json.dumps(body)[:200]}")
        check("J18 uncaptcha returns the token straight out of the envelope", token == "unc-token")
        check("J19 uncaptcha's waf task returns a clearance, not a token",
              clear.cookie == "unc-clearance"
              and stub.handler.seen[1]["task_type"] == "waf", f"got {clear}")
    finally:
        stub.close()


async def _one_call(call, base_url: str, *, provider=None, **kwargs):
    """Run one provider call in its own loop, then close the session."""
    cls = provider or cap.CapMonster
    p = cls("key", base_url=base_url, poll_interval_s=0.01, **kwargs)
    try:
        return await call(p)
    finally:
        await p.close()


def _raises(fn, want_text: bool = False):
    """Run fn; return True (or the exception text) when it raises.

    Catches `SystemExit` too: argparse refuses a bad choice by calling `sys.exit`,
    and that refusal is the behaviour under test — not a reason to end the suite.
    """
    try:
        fn()
    except SystemExit as e:  # argparse refuses by exiting
        return str(e) if want_text else True
    except Exception as e:
        return str(e) if want_text else True
    return False if not want_text else "did not raise"


# --------------------------------------------------------------------------- #
# K — the Chromium engine's host bootstrap
# --------------------------------------------------------------------------- #


def check_chromium_env(tmp: str, check) -> None:
    check("K1 heavy resources never include the ones the challenge needs",
          not (set(chrom.HEAVY_RESOURCES) & {"script", "stylesheet", "xhr", "document", "fetch"}),
          f"got {chrom.HEAVY_RESOURCES}")
    check("K2 the blocker drops exactly the configured types",
          chrom.should_block("image", chrom.HEAVY_RESOURCES)
          and chrom.should_block("font", ("font",))
          and not chrom.should_block("image", ("font",))
          and not chrom.should_block("script", chrom.HEAVY_RESOURCES))
    check("K3 the automation tells are suppressed at launch",
          any("AutomationControlled" in a for a in chrom.LAUNCH_ARGS)
          and any("disable-background-timer-throttling" in a for a in chrom.LAUNCH_ARGS),
          f"args={chrom.LAUNCH_ARGS}")

    # A loaded page is not a view. Only the site's own beacon proves one, and
    # these are the three ways a run could lie about it.
    check("K9 only a successful POST to the site's analytics path is a view",
          chrom.is_view_beacon("https://guns.lol/api/analytics/view", "POST", 200)
          and not chrom.is_view_beacon("https://guns.lol/api/analytics/view", "GET", 200)
          and not chrom.is_view_beacon("https://guns.lol/api/analytics/view", "POST", 403)
          and not chrom.is_view_beacon("https://guns.lol/guns.lol", "POST", 200))
    loaded = chrom.LoadReport(url="https://guns.lol/x", status=200)
    check("K10 a page that loaded with no beacon is NOT a recorded view",
          loaded.view_recorded is False and loaded.as_dict()["view_recorded"] is False,
          f"got {loaded.as_dict()}")
    loaded.beacon_status = 200
    check("K11 the site's own beacon is what makes it a view",
          loaded.view_recorded and loaded.as_dict()["beacon_status"] == 200)
    loaded.beacon_status = 403
    check("K12 a refused beacon is not a view either, and is reported as refused",
          not loaded.view_recorded and loaded.as_dict()["beacon_status"] == 403)

    # A view POST has three distinguishable endings, and the run has to name the
    # right one: Cloudflare refusing the path is not the same failure as the app
    # refusing the payload, and neither is the same as no attempt at all.
    check("K13 a view POST is classified by what actually came back",
          chrom.classify_view(200) == "counted"
          and chrom.classify_view(204, "") == "counted"
          and chrom.classify_view(403, "challenge") == "challenged"
          and chrom.classify_view(403, "CHALLENGE") == "challenged"
          and chrom.classify_view(403, "") == "rejected"
          and chrom.classify_view(500) == "rejected"
          and chrom.classify_view(None) == "none",
          f"got {[chrom.classify_view(s, m) for s, m in ((200, ''), (403, 'challenge'), (403, ''), (None, ''))]}")
    check("K14 widget attributes are normalised, absent ones included",
          chrom.turnstile_params_from({"sitekey": "0xabc", "action": "view"})
          == {"sitekey": "0xabc", "action": "view", "cdata": ""}
          and chrom.turnstile_params_from({}) == {"sitekey": "", "action": "", "cdata": ""})

    _check_token_drive(check)
    check_chromium_fonts(tmp, check)


def _check_token_drive(check) -> None:
    """The token hand-off: the page's own setter is what triggers the view."""

    class FakePage:
        def __init__(self, widget: bool, posts: list, status: int, mitigated: str) -> None:
            self.widget = widget
            self.posts = posts
            self.status = status
            self.mitigated = mitigated
            self.injected: list = []

        async def evaluate(self, js: str, arg=None):
            if "typeof window.getToken" in js:
                return self.widget
            if "window.getToken(t)" in js:
                self.injected.append(arg)
                self.posts.append({"status": self.status, "mitigated": self.mitigated,
                                   "url": "https://guns.lol/api/analytics/view"})
                return None
            return None

    from stealth.config import StealthConfig

    session = chrom.ChromiumSession(StealthConfig(target="cat"), fast=False)

    session.page = FakePage(True, session.view_posts, 403, "challenge")
    out = asyncio.run(session.trigger_view("tok-1", wait_ms=2000))
    check("K15 handing a token to the page fires its own view POST and reports the outcome",
          session.page.injected == ["tok-1"] and out.get("outcome") == "challenged"
          and out.get("status") == 403,
          f"got {out} injected={session.page.injected}")

    session.view_posts.clear()
    session.page = FakePage(True, session.view_posts, 200, "")
    out2 = asyncio.run(session.trigger_view("tok-2", wait_ms=2000))
    check("K16 a 200 on the page's own POST is a counted view",
          out2.get("outcome") == "counted" and out2.get("status") == 200, f"got {out2}")

    session.page = FakePage(False, [], 0, "")
    idle = asyncio.run(session.trigger_view("tok-3", wait_ms=1200))
    check("K17 no widget on the page is reported as such, not as a silent zero",
          idle.get("outcome") == "no_widget", f"got {idle}")

    # Fleet accounting: a challenged view, a counted view, and "no token" must
    # land in three different buckets, or the summary line lies about the run.
    class StubSession:
        worker_id = "9.9"

        def __init__(self, outcome):
            self.outcome = outcome
            self.given = []

        async def widget_params(self):
            return {"sitekey": "0xkey", "action": "", "cdata": ""}

        async def trigger_view(self, token, wait_ms=0):
            self.given.append(token)
            return self.outcome

    logs: list = []
    fleet = chrom.ChromiumFleet(StealthConfig(target="cat"), tokens=["supplied-token"],
                                log=lambda tag, msg: logs.append((tag, msg)))
    stub = StubSession({"status": 403, "mitigated": "challenge", "outcome": "challenged"})
    asyncio.run(fleet._attempt_view(stub, None, "9.9", chrom.LoadReport(ms=100.0)))
    stub_ok = StubSession({"status": 200, "mitigated": "", "outcome": "counted"})
    asyncio.run(fleet._attempt_view(stub_ok, None, "9.9", chrom.LoadReport(ms=100.0)))
    bare = chrom.ChromiumFleet(StealthConfig(target="cat"), log=lambda *a: None)
    asyncio.run(bare._attempt_view(stub_ok, None, "9.9", chrom.LoadReport(ms=100.0)))
    check("K18 the fleet counts a refused view, a counted view and a missing token apart",
          fleet.challenged == 1 and fleet.hits == 1 and fleet.untokened == 0
          and stub.given == ["supplied-token"] and bare.untokened == 1
          and bare.hits == 0, f"got hits={fleet.hits} challenged={fleet.challenged} "
                              f"bare={bare.untokened}/{bare.hits}")
    # Minting: the provider must be told *this* page's widget — sitekey, action
    # and cdata come off the rendered div, because a token minted for another
    # widget is worthless here.
    class StubProvider:
        name = "stub"

        def __init__(self) -> None:
            self.req = None

        async def solve_turnstile(self, req):
            self.req = req
            return "minted-token"

    prov = StubProvider()
    minting = chrom.ChromiumFleet(StealthConfig(target="cat"), provider=prov,
                                  log=lambda *a: None)
    minted = asyncio.run(minting._token_for(stub, None))
    check("K20 a provider is asked for a token for this page's widget",
          minted == "minted-token" and prov.req is not None
          and prov.req.site_key == "0xkey" and prov.req.page_url.endswith("/cat"),
          f"got {minted} req={getattr(prov.req, '__dict__', None)}")

    detail = bare.status()
    check("K19 the summary exposes why a view did not count",
          detail.get("challenged") == 0 and detail.get("untokened") == 1
          and detail.get("rejected") == 0 and detail.get("unposted") == 0,
          f"got {detail}")

    pass


# --------------------------------------------------------------------------- #
# K — the font bootstrap (kept apart: it mutates the process environment)
# --------------------------------------------------------------------------- #


def check_chromium_fonts(tmp: str, check) -> None:
    # The font bootstrap is what keeps headless Chromium alive on a slim host:
    # without a resolvable fontconfig its Skia font manager aborts the process.
    libs = os.path.join(ROOT, ".foxlibs", "usr", "share", "fonts")
    if os.path.isdir(libs):
        state = os.path.join(tmp, "state")
        old = {k: os.environ.get(k) for k in ("FONTCONFIG_FILE", "FONTCONFIG_PATH", "STATE_DIR")}
        try:
            for k in old:
                os.environ.pop(k, None)
            os.environ["STATE_DIR"] = state
            conf = chrom.ensure_fontconfig()
            check("K4 a fontconfig pointing at a real font tree is written", bool(conf), f"got {conf}")
            body = open(conf, encoding="utf-8").read() if conf else ""
            xml.dom.minidom.parseString(body)  # raises if the file is malformed
            check("K5 the generated fontconfig is valid XML and names the local fonts",
                  "fontconfig" in body and str(libs).replace("\\", "/") in body, body[:120])
            check("K6 its cache directory exists and is writable",
                  os.path.isdir(os.path.join(state, "fontconfig-cache")))
            check("K7 an existing FONTCONFIG_FILE is respected, not overwritten",
                  os.environ.get("FONTCONFIG_FILE") == conf)
            env = chrom.ensure_host_env(ROOT)
            check("K8 the host bootstrap exports the browser cache, loader path and fonts",
                  env.get("PLAYWRIGHT_BROWSERS_PATH", "").endswith(".playwright-browsers")
                  and env.get("FONTCONFIG_FILE") == conf,
                  f"got {sorted(env)}")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    else:
        check("K4 a fontconfig is only written when a font tree exists", True, "no .foxlibs in this checkout")
        for name in ("K5 the generated fontconfig is valid XML and names the local fonts",
                     "K6 its cache directory exists and is writable",
                     "K7 an existing FONTCONFIG_FILE is respected, not overwritten",
                     "K8 the host bootstrap exports the browser cache, loader path and fonts"):
            check(name, True, "skipped: no local font tree")


# --------------------------------------------------------------------------- #
# L — rotating-proxy sessions
# --------------------------------------------------------------------------- #


def check_display_deps(_tmp: str, check) -> None:
    """The dependency helper only drags Xvfb in when a display is asked for."""
    sys.path.insert(0, os.path.join(ROOT, "stealth", "scripts"))
    import linux_deps as ld  # noqa: E402

    plain = ld.seed_packages([])
    asked = ld.seed_packages(["--with-display"])
    check("M1 the default install set does not pull in an X server",
          not any("xvfb" in p for p in plain), f"got {[p for p in plain if 'xvfb' in p]}")
    check("M2 --with-display adds Xvfb, the XKB compiler and the keymap data",
          all(p in asked for p in ld.DISPLAY_PACKAGES) and "xkb-data" in asked,
          f"got {[p for p in ld.DISPLAY_PACKAGES if p not in asked]}")
    check("M3 asking for a display never removes the browser stack",
          all(p in asked for p in ld.SEED_PACKAGES),
          f"lost {[p for p in ld.SEED_PACKAGES if p not in asked]}")
    check("M4 the unpack refuses to ship a second glibc",
          {"libc6", "libc6-dev", "libstdc++6"}.issubset(ld.SKIP), f"SKIP={ld.SKIP}")


def check_engine_names(_tmp: str, check) -> None:
    """The engine names say what they do, and the old name still resolves."""
    import argparse

    sys.path.insert(0, os.path.join(ROOT, "stealth"))
    from stealth.cli import build_parser, config_from_args
    from stealth.config import StealthConfig

    parser = build_parser()
    args = parser.parse_args(["someone", "--engine", "guns"])
    cfg = config_from_args(args)
    check("N1 the old 'guns' name still selects the API engine",
          cfg.engine == "api", f"got {cfg.engine!r}")
    args = parser.parse_args(["someone", "--engine", "api"])
    check("N2 'api' is accepted by name", config_from_args(args).engine == "api")
    for name in ("camoufox", "chromium"):
        a = parser.parse_args(["someone", "--engine", name])
        check(f"N3 --engine {name} is a browser engine, and needs no key",
              config_from_args(a).engine == name
              and not config_from_args(a).captcha_key)
    check("N4 an unknown engine is refused at parse time",
          _raises(lambda: parser.parse_args(["someone", "--engine", "browser"])))
    check("N5 config.validate refuses an engine it cannot drive",
          _raises(lambda: StealthConfig(target="someone", engine="playwright").validate(), True)
          != "did not raise")

    # --rounds 0 must survive parsing as the loop sentinel, not become 1.
    a = parser.parse_args(["someone", "--rounds", "0"])
    check("N6 --rounds 0 parses as the loop-until-stopped sentinel", a.rounds == 0, f"got {a.rounds}")
    check("N7 a positive round count is unchanged", parser.parse_args(["someone", "--rounds", "7"]).rounds == 7)

    # The help is the interface: it has to name the engines by mechanism, say
    # which ones need a key, and say what 0 rounds means.
    actions = {a.dest: a for a in parser._actions}
    engines = tuple(actions["engine"].choices)
    help_text = actions["engine"].help + actions["rounds"].help
    check("N8 the engine choices are the canonical names plus the legacy alias",
          set(engines) == {"camoufox", "chromium", "api", "guns"}, f"got {engines}")
    # The browser engines are keyless to *run* but a view needs a token, so the
    # help has to say both — "no key" alone was the misleading half.
    check("N9 the help says which engines need no key, that a view needs a token, "
          "and what 0 rounds means",
          "no key" in help_text and "turnstile-token" in help_text.lower()
          and "loops until" in help_text,
          f"got {help_text!r}")


def check_proxy_session(tmp: str, check) -> None:
    rotating = Proxy(server="http://geo.iproyal.com:12345",
                     username="user",
                     password="pw_session-{session}_lifetime-30s")
    check("L1 a proxy with a {session} placeholder is recognised as rotating", rotating.rotating)
    check("L2 a plain proxy is not", not Proxy(server="http://1.2.3.4:8080").rotating)

    first = rotating.render("aaaa1111")
    check("L3 an explicit token renders deterministically",
          first.to_curl() == "http://user:pw_session-aaaa1111_lifetime-30s@geo.iproyal.com:12345",
          f"got {first.to_curl()}")
    a, b = rotating.to_curl(), rotating.to_curl()
    check("L4 two renders hand out two different sessions (two different IPs)", a != b, f"{a} == {b}")
    check("L5 an unrotating proxy renders to itself, token or not",
          Proxy(server="http://1.2.3.4:8080").render("x").to_curl() == "http://1.2.3.4:8080")

    pw = rotating.to_playwright("tok1234")
    check("L6 Playwright receives the rendered credentials, not the placeholder",
          pw["username"] == "user" and pw["password"] == "pw_session-tok1234_lifetime-30s",
          f"got {pw}")
    check("L7 the label stays the endpoint, so logs group by provider host",
          rotating.label == "geo.iproyal.com:12345", f"got {rotating.label}")

    runner = guns.GunsRunner("someone", state_dir=tmp, proxies=[rotating])
    paths = {runner._clearance_path(runner._egress(0)) for _ in range(6)}
    check("L8 a rotating egress gets a per-session clearance key, never one shared cookie",
          len(paths) == 6, f"got {len(paths)} distinct keys from 6 views")
    fixed = guns.GunsRunner("someone", state_dir=tmp,
                            proxies=[Proxy(server="http://1.2.3.4:8080")])
    same = {fixed._clearance_path(fixed._egress(0)) for _ in range(3)}
    check("L9 a sticky egress reuses one clearance key, so its cookie is reused",
          len(same) == 1, f"got {len(same)}")

    # Clearance files must round-trip and expire.
    path = os.path.join(tmp, "clr.json")
    guns.save_clearance(path, {"cf_clearance": "abc"}, ttl_s=60)
    check("L10 a minted clearance is remembered for the egress",
          guns.load_clearance(path).get("cf_clearance") == "abc")
    guns.save_clearance(path, {"cf_clearance": "abc"}, ttl_s=-1)
    check("L11 an expired clearance is not replayed", guns.load_clearance(path) == {})
    check("L12 an unreadable clearance file is a miss, not a crash",
          guns.load_clearance(os.path.join(tmp, "nope.json")) == {})
    check("L13 a session token is 16 hex characters", len(new_session_id()) == 16)
