#!/usr/bin/env python3
"""Offline checks for the stealth rig — no browser, no network, no proxies.

    ./.venv/bin/python stealth/scripts/check_rig.py

Covers the fleet and egress layers, where the subtle bugs live:

    A  one solver per egress, enforced across real processes
    B  a worker killed mid-solve (the fleet watchdog) does not wedge the egress
    C  a waiting worker picks up the peer's clearance instead of solving
    D  CloudflareGuard reuses, verifies, solves-then-publishes, and releases
    E  EgressPool prefers warm egresses, sinks blockers, persists, and cycles
    F  worker RESULT lines fold into the run totals and the pool
    G  a browser that never launches costs a relaunch, not a worker slot
    H  the native guns.lol protocol: challenge, payload, headers, block detection
    I  the PoW harness and its cache, driven by a worker captured from the site
    J  captcha providers against a local stub: success, refusal, timeout
    K  the Chromium engine's font/bootstrap fix and resource blocking
    L  rotating proxies: a fresh session per view, a per-session clearance key

H-L live in `check_rig_extra.py`; they still need no browser and no network.

An engine check still needs the browser: run
`python -m stealth <user> --contexts 1` for the live path.

Exits with the number of failed checks (0 = all green).
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
import traceback
from queue import Empty
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stealth.cloudflare import CloudflareGuard  # noqa: E402
from stealth.config import Proxy, StealthConfig  # noqa: E402
from stealth.egress import ClearanceBroker, EgressPool  # noqa: E402
from stealth.session import StealthFleet  # noqa: E402

HOST = "guns.lol"
LABEL = "203.0.113.7:8080"

FAILURES: List[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        FAILURES.append(name)
    return ok


# --------------------------------------------------------------------------- #
# child-process helpers (must be module level so `spawn` can pickle them)
# --------------------------------------------------------------------------- #


def _child_claim(state_dir: str, label: str, host: str, q, hold_s: float,
                 publish: bool) -> None:
    """Claim the solve slot, optionally hold it, then report."""
    broker = ClearanceBroker(state_dir, shared=True)
    claimed = broker.try_claim(label, host, "child")
    q.put(claimed)
    if claimed and hold_s > 0:
        time.sleep(hold_s)
        if publish:
            broker.publish(label, host, {"cf_clearance": "peer-token"})
        broker.release(label, host)


def _spawn_claims(state_dir: str, n: int, *, hold_s: float = 0.0,
                  publish: bool = False) -> List[bool]:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_child_claim, args=(state_dir, LABEL, HOST, q, hold_s, publish))
        for _ in range(n)
    ]
    for p in procs:
        p.start()
    out: List[bool] = []
    for _ in procs:
        try:
            out.append(bool(q.get(timeout=45)))
        except Empty:
            out.append(False)
    for p in procs:
        p.join(45)
        if p.is_alive():  # pragma: no cover - defensive
            p.kill()
    return out


# --------------------------------------------------------------------------- #
# fakes for the guard-level checks
# --------------------------------------------------------------------------- #


class FakePage:
    """A page that reports a Cloudflare interstitial for N detect() calls."""

    def __init__(self, challenged_calls: int) -> None:
        self.remaining = challenged_calls

    async def title(self) -> str:
        if self.remaining > 0:
            self.remaining -= 1
            return "Just a moment..."
        return "Home"

    async def query_selector(self, sel: str):
        return object() if self.remaining > 0 else None


class FakeContext:
    def __init__(self) -> None:
        self.added: List[Dict[str, str]] = []

    async def cookies(self):
        return [
            {"name": "cf_clearance", "value": "solved-token"},
            {"name": "session", "value": "abc"},
        ]

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


class FakeStream:
    """A readable stand-in for a subprocess pipe (asyncio StreamReader)."""

    def __init__(self, data: bytes) -> None:
        self._lines = data.splitlines(keepends=True)
        self._i = 0

    async def readline(self) -> bytes:
        if self._i >= len(self._lines):
            return b""
        line = self._lines[self._i]
        self._i += 1
        return line


class FakeProc:
    """Stands in for the worker subprocess in `StealthFleet._collect`."""

    def __init__(self, text: str) -> None:
        self._buf = text.encode()
        self.stdout = FakeStream(self._buf)
        self.returncode = 0

    async def communicate(self):
        return self._buf, None

    async def wait(self):
        return 0


def make_log(sink: List[str]):
    def log(tag: str, msg: str) -> None:
        sink.append(f"[{tag}] {msg}")
    return log


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_broker(tmp: str) -> None:
    print("\nA/B/C. ClearanceBroker — one solver per egress, across processes")
    sd = os.path.join(tmp, "broker")
    sink: List[str] = []
    parent = ClearanceBroker(sd, shared=True, log=make_log(sink))

    # A. exclusion while held, then release
    check("A1 parent takes the solve slot", parent.try_claim(LABEL, HOST, "parent") is True)
    got = _spawn_claims(sd, 3)
    check("A2 three sibling processes are refused while the slot is held",
          got == [False, False, False], f"got {got}")
    parent.release(LABEL, HOST)
    got = _spawn_claims(sd, 1)
    check("A3 the slot is claimable once released", got == [True], f"got {got}")

    # B. a killed holder must not wedge the egress (the watchdog case)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    holder = ctx.Process(target=_child_claim, args=(sd, LABEL, HOST, q, 30.0, False))
    holder.start()
    try:
        claimed = bool(q.get(timeout=45))
    except Empty:
        claimed = False
    check("B1 a child process takes the slot", claimed is True)
    check("B2 the parent is refused while that child holds it",
          parent.try_claim(LABEL, HOST, "parent") is False)
    holder.kill()
    holder.join(20)
    time.sleep(0.3)
    check("B3 killing the holder (watchdog) frees the slot for the next worker",
          parent.try_claim(LABEL, HOST, "parent") is True)
    parent.release(LABEL, HOST)

    # C. a peer's clearance is picked up instead of solving again
    sd2 = os.path.join(tmp, "broker-reuse")
    waiter = ClearanceBroker(sd2, shared=True, log=make_log(sink))
    q2 = ctx.Queue()
    solver = ctx.Process(target=_child_claim, args=(sd2, LABEL, HOST, q2, 1.5, True))
    solver.start()
    t0 = time.time()
    fresh = asyncio.run(waiter.wait_for_peer(LABEL, HOST, timeout_s=20))
    elapsed = time.time() - t0
    solver.join(20)
    check("C1 a waiting worker receives the peer's clearance",
          fresh.get("cf_clearance") == "peer-token", f"got {fresh}")
    check("C2 it arrives promptly, not at the timeout", elapsed < 15,
          f"{elapsed:.1f}s")

    sd3 = os.path.join(tmp, "broker-timeout")
    give_up = ClearanceBroker(sd3, shared=True, log=make_log(sink))
    t0 = time.time()
    empty = asyncio.run(give_up.wait_for_peer(LABEL, HOST, timeout_s=1.0))
    check("C3 nothing published means the wait returns empty (caller solves)",
          empty == {} and time.time() - t0 >= 0.9, f"got {empty}")

    print("\n   broker log:")
    for line in sink:
        print(f"     {line}")


def check_guard(tmp: str) -> None:
    print("\nD. CloudflareGuard — reuse, verify, solve-and-publish, release")
    log: List[str] = []

    # D1: a warm clearance published by a peer is adopted without solving.
    # `challenged_calls=1` means the interstitial is gone by the next look —
    # i.e. the cookie worked.
    sd = os.path.join(tmp, "guard-reuse")
    broker = ClearanceBroker(sd, shared=True, log=make_log(log))
    broker.publish(LABEL, HOST, {"cf_clearance": "warm-token"})
    ctx = FakeContext()
    guard = CloudflareGuard(ctx, proxy_label=LABEL, state_dir=sd, timeout_ms=5000,
                            broker=broker, share_clearance=True, clearance_wait_s=1.0,
                            worker_id="1.1", log=make_log(log))
    res = asyncio.run(guard.handle(FakePage(challenged_calls=1), HOST, human=None))
    check("D1 warm clearance is reused (no solve)", res.solved and res.reused,
          f"solved={res.solved} reused={res.reused} reason={res.reason}")
    check("D2 the clearance landed on the browser context",
          any(c["name"] == "cf_clearance" and c["value"] == "warm-token" for c in ctx.added))
    check("D3 no solve was charged to the egress", guard.solves == 0 and guard.reused == 1,
          f"solves={guard.solves} reused={guard.reused}")
    check("D4 the reuse path left no solve slot held",
          broker.try_claim(LABEL, HOST, "probe") is True)

    # D5: a cached cookie that does NOT clear the page must not buy a pass.
    # `challenged_calls=20` outlasts the 2.5s confirmation window, so the guard
    # has to fall through and work the challenge for real.
    sd = os.path.join(tmp, "guard-stale")
    broker = ClearanceBroker(sd, shared=True, log=make_log(log))
    broker.publish(LABEL, HOST, {"cf_clearance": "stale-token"})
    guard = CloudflareGuard(FakeContext(), proxy_label=LABEL, state_dir=sd,
                            timeout_ms=6000, broker=broker, share_clearance=True,
                            clearance_wait_s=1.0, worker_id="1.2", log=make_log(log))
    res = asyncio.run(guard.handle(FakePage(challenged_calls=20), HOST, human=None))
    check("D5 a stale cached clearance is rejected, not counted as a pass",
          not res.reused and guard.reused == 0,
          f"reused={res.reused} reason={res.reason}")
    check("D6 the guard fell through and worked the challenge",
          (guard.solves + guard.blocks) == 1,
          f"solves={guard.solves} blocks={guard.blocks}")

    # D7: solving publishes the cookie for the rest of the egress
    sd = os.path.join(tmp, "guard-solve")
    broker = ClearanceBroker(sd, shared=True, log=make_log(log))
    guard = CloudflareGuard(FakeContext(), proxy_label=LABEL, state_dir=sd, timeout_ms=9000,
                            broker=broker, share_clearance=True, clearance_wait_s=1.0,
                            worker_id="1.3", log=make_log(log))
    res = asyncio.run(guard.handle(FakePage(challenged_calls=2), HOST, human=None))
    check("D7 a real solve is counted once",
          res.solved and not res.reused and guard.solves == 1,
          f"solved={res.solved} reused={res.reused} solves={guard.solves} reason={res.reason}")
    check("D8 the solve is published for the rest of the egress",
          broker.cached(LABEL, HOST).get("cf_clearance") == "solved-token")
    check("D9 the solve slot is released when the challenge ends",
          broker.try_claim(LABEL, HOST, "probe") is True)

    # D10: a peer that never publishes must not stall this worker
    sd = os.path.join(tmp, "guard-contended")
    broker = ClearanceBroker(sd, shared=True, log=make_log(log))
    peer = ClearanceBroker(sd, shared=True, log=make_log(log))
    check("D10 peer holds the slot", peer.try_claim(LABEL, HOST, "peer") is True)
    guard = CloudflareGuard(FakeContext(), proxy_label=LABEL, state_dir=sd, timeout_ms=9000,
                            broker=broker, share_clearance=True, clearance_wait_s=1.0,
                            worker_id="1.4", log=make_log(log))
    t0 = time.time()
    res = asyncio.run(guard.handle(FakePage(challenged_calls=2), HOST, human=None))
    waited = time.time() - t0
    check("D11 a silent peer delays the worker but does not block it",
          res.solved and waited >= 1.0 and waited < 12,
          f"solved={res.solved} waited={waited:.1f}s reason={res.reason}")
    # `try_claim` is idempotent for the current holder, so observe the slot with
    # a broker that has never claimed it: still held means the guard left the
    # peer's slot alone.
    observer = ClearanceBroker(sd, shared=True, log=make_log(log))
    check("D12 the contended worker did not release a slot it never owned",
          observer.try_claim(LABEL, HOST, "observer") is False)
    observer.release(LABEL, HOST)
    peer.release(LABEL, HOST)
    check("D13 the peer's slot frees once the peer is done",
          observer.try_claim(LABEL, HOST, "observer") is True)
    observer.release(LABEL, HOST)

    # D14: sharing turned off means no coordination at all
    sd = os.path.join(tmp, "guard-unshared")
    broker = ClearanceBroker(sd, shared=False, log=make_log(log))
    guard = CloudflareGuard(FakeContext(), proxy_label=LABEL, state_dir=sd,
                            timeout_ms=9000, broker=broker, share_clearance=False,
                            clearance_wait_s=1.0, worker_id="1.5", log=make_log(log))
    res = asyncio.run(guard.handle(FakePage(challenged_calls=2), HOST, human=None))
    locks = [f for f in os.listdir(sd) if f.endswith(".lock")]
    check("D14 --no-share-clearance still solves, and takes no solve slot",
          res.solved and guard.solves == 1 and not locks,
          f"solved={res.solved} solves={guard.solves} locks={locks}")

    print("\n   guard log:")
    for line in log:
        print(f"     {line}")


def check_pool(tmp: str) -> None:
    print("\nE. EgressPool — warm first, blockers last, persists, cycles")
    proxies = [
        Proxy(server="http://10.0.0.1:8080"),
        Proxy(server="http://10.0.0.2:8080"),
        Proxy(server="http://10.0.0.3:8080"),
    ]
    labels = [p.label for p in proxies]

    # E1: an egress with a warm clearance is spent first
    sd = os.path.join(tmp, "pool-warm")
    broker = ClearanceBroker(sd, shared=True)
    broker.publish(labels[1], HOST, {"cf_clearance": "warm"})
    pool = EgressPool(proxies, state_dir=sd, host=HOST, broker=broker)
    pool.begin_run()
    first = pool.plan(1)[0]
    check("E1 a warm egress is assigned before cold ones",
          first is not None and first.label == labels[1],
          f"got {getattr(first, 'label', first)}")

    # E2: history sinks a blocking egress to the back of the queue
    sd = os.path.join(tmp, "pool-history")
    pool = EgressPool(proxies, state_dir=sd, host=HOST)
    pool.record(labels[0], hits=0, misses=4, solves=1, blocks=4)
    pool.save()
    pool.begin_run()
    order = [p.label for p in pool.plan(3)]
    check("E2 a blocking egress is tried last", order == [labels[1], labels[2], labels[0]],
          f"got {order}")
    reloaded = EgressPool(proxies, state_dir=sd, host=HOST)
    check("E3 outcomes survive into the next run",
          reloaded.stats[labels[0]].blocks == 4 and reloaded.stats[labels[0]].misses == 4,
          f"blocks={reloaded.stats[labels[0]].blocks}")
    pool.begin_run()
    cycled = [p.label for p in pool.plan(5)]
    check("E4 a small pool cycles instead of starving workers",
          cycled == [labels[1], labels[2], labels[0], labels[1], labels[2]],
          f"got {cycled}")
    check("E5 no proxies means direct, one slot per worker",
          EgressPool([], state_dir=sd, host=HOST).plan(3) == [None, None, None])

    # E6: the pool prefers the egress that was idle longest
    sd = os.path.join(tmp, "pool-rotation")
    pool = EgressPool(proxies, state_dir=sd, host=HOST)
    pool.begin_run()
    a = [p.label for p in pool.plan(1)]
    b = [p.label for p in pool.plan(1)]
    c = [p.label for p in pool.plan(1)]
    check("E6 consecutive workers rotate across the pool",
          len({a[0], b[0], c[0]}) == 3, f"got {a + b + c}")


def check_fleet(tmp: str) -> None:
    print("\nF. StealthFleet — worker results fold into totals and the pool")
    sd = os.path.join(tmp, "fleet")
    proxies = [Proxy(server="http://10.0.0.9:8080")]
    cfg = StealthConfig(target="someone", base_url="https://guns.lol", state_dir=sd)
    log: List[str] = []
    fleet = StealthFleet(cfg, log=make_log(log))
    fleet._pool = EgressPool(proxies, state_dir=sd, host=HOST)
    fleet._merge(
        fleet._pool,
        [
            {"hits": 2, "misses": 1, "egress": "10.0.0.9:8080", "solves": 1,
             "blocks": 1, "reused": 1},
            {"hits": 1, "misses": 0, "egress": "10.0.0.9:8080", "solves": 0,
             "blocks": 0, "reused": 1},
        ],
    )
    st = fleet._pool.stats["10.0.0.9:8080"]
    check("F1 run totals add up", fleet.hits == 3 and fleet.misses == 1,
          f"hits={fleet.hits} misses={fleet.misses}")
    check("F2 egress outcomes are attributed",
          (st.hits, st.misses, st.solves, st.blocks, st.reused) == (3, 1, 1, 1, 2),
          f"got {st}")
    check("F3 status() reports the egress table",
          fleet.status().get("egress") and fleet.status()["egress"][0]["egress"] == "10.0.0.9:8080")

    import json
    raw = "worker chatter\nRESULT " + json.dumps(
        {"worker": "1.1", "hits": 1, "misses": 0, "error": None,
         "egress": "10.0.0.9:8080", "solves": 2, "blocks": 0, "reused": 1}
    )
    proc = FakeProc(raw)
    got = asyncio.run(fleet._collect("1.1", proc))
    check("F4 _collect parses the worker's RESULT line",
          got.get("egress") == "10.0.0.9:8080" and got.get("solves") == 2,
          f"got {got}")
    check("F5 _collect streams the worker's log lines as they arrive",
          any("worker chatter" in line for line in log), f"log={log}")


def check_worker_retry(tmp: str) -> None:
    print("\nG. StealthWorker — a dead launch is relaunched, a missed round is not")
    import stealth.worker as W

    class FakeSession:
        """Stand-in for StealthSession: records calls, obeys the script."""

        start_calls = 0
        fail_starts = 0
        hits = 0
        hit_raises = False

        def __init__(self, cfg, proxy=None, worker_id="", on_frame=None, log=None):
            self.worker_id = worker_id
            self.proxy = proxy

        async def start(self):
            type(self).start_calls += 1
            if type(self).start_calls <= type(self).fail_starts:
                raise RuntimeError(
                    "BrowserContext.new_page: Target page, context or browser has been closed"
                )

        async def stop(self):
            pass

        async def hit(self):
            if type(self).hit_raises:
                raise RuntimeError("navigation exploded")
            type(self).hits += 1
            return True

        def egress_stats(self):
            return {"solves": 0, "blocks": 0, "reused": 0}

    original = W.StealthSession
    W.StealthSession = FakeSession  # type: ignore[assignment]
    job = {
        "target": "guns",
        "worker_id": "9.9",
        "rounds": 2,
        "state_dir": os.path.join(tmp, "worker"),
        "launch_retries": 1,
        "launch_retry_delay": [0.05, 0.1],
        "round_gap": [0, 0],
    }
    try:
        def arm(fail_starts: int, hit_raises: bool) -> None:
            FakeSession.start_calls = 0
            FakeSession.hits = 0
            FakeSession.fail_starts = fail_starts
            FakeSession.hit_raises = hit_raises

        arm(fail_starts=1, hit_raises=False)
        res = asyncio.run(W._run(dict(job)))
        check("G1 a browser that dies in launch is relaunched",
              FakeSession.start_calls == 2, f"starts={FakeSession.start_calls}")
        check("G2 the relaunched worker still delivers its rounds",
              res["hits"] == 2 and res["misses"] == 0 and not res["error"], f"got {res}")

        arm(fail_starts=99, hit_raises=False)
        res = asyncio.run(W._run(dict(job)))
        check("G3 a launch that never works is bounded to retries+1 attempts",
              FakeSession.start_calls == 2 and res["hits"] == 0 and res["misses"] == 1,
              f"starts={FakeSession.start_calls} got {res}")
        check("G4 the failure is reported with a reason",
              "has been closed" in str(res.get("error")), f"error={res.get('error')}")

        arm(fail_starts=0, hit_raises=True)
        res = asyncio.run(W._run(dict(job)))
        check("G5 a mid-run failure is charged as a miss, never laundered "
              "into a relaunch",
              FakeSession.start_calls == 1 and res["hits"] == 0 and res["misses"] == 2,
              f"starts={FakeSession.start_calls} got {res}")

        arm(fail_starts=1, hit_raises=False)
        res = asyncio.run(W._run({**job, "launch_retries": 0}))
        check("G6 --launch-retries 0 leaves a dead launch dead",
              FakeSession.start_calls == 1 and res["misses"] == 1,
              f"starts={FakeSession.start_calls} got {res}")
    finally:
        W.StealthSession = original  # type: ignore[assignment]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rig-check-")
    print(f"stealth rig checks — state under {tmp}")
    from check_rig_extra import (check_captcha, check_chromium_env, check_display_deps,
                                 check_guns_protocol, check_pow_harness, check_proxy_session)

    # The suites in check_rig_extra take the reporter as an argument so they can
    # be imported without importing this module's globals back.
    suites = [(fn, ()) for fn in (check_broker, check_guard, check_pool, check_fleet,
                                  check_worker_retry)]
    suites += [(fn, (check,)) for fn in (check_guns_protocol, check_pow_harness,
                                         check_captcha, check_chromium_env,
                                         check_display_deps, check_proxy_session)]
    try:
        for fn, extra in suites:
            try:
                fn(tmp, *extra)
            except Exception:
                FAILURES.append(f"{fn.__name__} raised")
                print(f"  [FAIL] {fn.__name__} raised:")
                traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} checks FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
    else:
        print(f"all {CHECKS} checks passed")
    print("=" * 62)
    return len(FAILURES)


if __name__ == "__main__":
    raise SystemExit(main())
