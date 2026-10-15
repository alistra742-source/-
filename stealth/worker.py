"""One worker process: a single Camoufox browser, driven from a JSON job.

Why a process per worker? Camoufox (patched Firefox) intermittently stalls
`browser.new_page()` when two of its instances share one asyncio loop — the
juggler handshake contends and one browser's page creation never returns. Rather
than serialize the fleet, each worker gets its own process, its own event loop
and its own browser. Isolation is the fix, and it is also how browser farms are
normally scaled.

The job arrives in the `STEALTH_JOB` environment variable (JSON). The worker
prints its own log lines, then one final `RESULT {json}` line the parent parses.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from typing import Any, Dict

from .config import Proxy, StealthConfig
from .session import StealthSession


def _config_from_job(job: Dict[str, Any]) -> StealthConfig:
    px = job.get("proxy")
    proxy = Proxy(**px) if px else None
    cfg = StealthConfig(
        target=job.get("target", ""),
        base_url=job.get("base_url", "https://guns.lol"),
        headless=job.get("headless", True),
        os_target=job.get("os_target"),
        humanize=job.get("humanize", True),
        geoip=job.get("geoip", False),
        locale=job.get("locale"),
        block_images=job.get("block_images", False),
        block_webrtc=job.get("block_webrtc", True),
        disable_coop=job.get("disable_coop", True),
        main_world_eval=job.get("main_world_eval", True),
        i_know_what_im_doing=True,
        tls_impersonate=job.get("tls_impersonate"),
        nav_timeout_ms=job.get("nav_timeout_ms", 60_000),
        dwell=tuple(job.get("dwell", (9_000, 22_000))),
        round_gap=tuple(job.get("round_gap", (20_000, 60_000))),
        warmup=job.get("warmup", True),
        solve_turnstile=job.get("solve_turnstile", True),
        cf_timeout_ms=job.get("cf_timeout_ms", 45_000),
        state_dir=job.get("state_dir", ".stealth-state"),
        share_clearance=job.get("share_clearance", True),
        clearance_wait_s=job.get("clearance_wait_s", 30.0),
        launch_retries=job.get("launch_retries", 1),
        launch_retry_delay=tuple(job.get("launch_retry_delay", (4.0, 7.0))),
        debug_config=job.get("debug_config", False),
        verbose=job.get("verbose", False),
    )
    return cfg, proxy  # type: ignore[return-value]


def _egress_label(proxy) -> str:
    return proxy.label if proxy else "direct"


async def _rounds(cfg: StealthConfig, proxy, wid: str, rounds: int, log) -> Dict[str, Any]:
    """One browser lifecycle: launch it, run every round, report.

    A launch failure propagates (nothing ran, nothing is counted). A failure
    *inside* a round is caught and counted as a miss, because a navigation was
    genuinely attempted — that worker must not be retried and double-counted.
    """
    session = StealthSession(cfg, proxy=proxy, worker_id=wid, log=log)
    hits = 0
    misses = 0
    error = None
    try:
        # A launch failure may leave a half-open browser behind (Camoufox dies
        # in new_page often enough to matter); stop() releases whatever came up
        # before the failure propagates, so a relaunch is not two browsers.
        await session.start()
        for r in range(rounds):
            try:
                ok = await session.hit()
                hits += 1 if ok else 0
                misses += 0 if ok else 1
            except Exception as e:
                misses += 1
                error = str(e).splitlines()[0] if str(e) else type(e).__name__
                log("miss", error)
            if r + 1 < rounds:
                await asyncio.sleep(random.uniform(*cfg.round_gap) / 1000)
    finally:
        stats = session.egress_stats()
        await session.stop()

    return {
        "worker": wid,
        "hits": hits,
        "misses": misses,
        "error": error,
        "egress": _egress_label(proxy),
        **stats,
    }


async def _run(job: Dict[str, Any]) -> Dict[str, Any]:
    cfg, proxy = _config_from_job(job)
    wid = job.get("worker_id", "1.1")
    rounds = max(1, int(job.get("rounds", 1)))
    max_retries = max(0, int(cfg.launch_retries))

    # Messages from StealthSession already carry the worker id; don't repeat it.
    def log(tag: str, msg: str) -> None:
        print(f"[{tag}] {msg}", flush=True)

    for attempt in range(max_retries + 1):
        try:
            return await _rounds(cfg, proxy, wid, rounds, log)
        except Exception as e:
            err = str(e).splitlines()[0] if str(e) else type(e).__name__
            if attempt >= max_retries:
                log("miss", f"browser failed to launch: {err}")
                return {
                    "worker": wid,
                    "hits": 0,
                    "misses": 1,
                    "error": err,
                    "egress": _egress_label(proxy),
                    "solves": 0,
                    "blocks": 0,
                    "reused": 0,
                }
            # A cold launch on a loaded host dies on its own now and then
            # (Camoufox's juggler handshake, or the OOM killer). That costs a
            # browser, not a worker slot: relaunch once, after a pause that
            # lets whatever spiked subside.
            delay = random.uniform(*cfg.launch_retry_delay)
            log("retry", f"browser never came up ({err}) — relaunching in {delay:.1f}s")
            await asyncio.sleep(max(0.0, delay))


def main() -> int:
    raw = os.environ.get("STEALTH_JOB")
    if not raw:
        print("STEALTH_JOB not set", file=sys.stderr)
        return 2
    try:
        job = json.loads(raw)
    except Exception as e:
        print(f"bad STEALTH_JOB: {e}", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(_run(job))
        rc = 0 if result["hits"] else 1
    except Exception as e:
        result = {
            "worker": job.get("worker_id", "?"),
            "hits": 0,
            "misses": 1,
            "error": (str(e).splitlines()[0] if str(e) else type(e).__name__),
        }
        rc = 1

    print("RESULT " + json.dumps(result), flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
