"""Egress management: who solves a challenge, and which proxy each worker gets.

Two problems live here, and they only appear once the fleet is bigger than one
worker.

**1. Stampeding one egress.** Two Camoufox workers behind the same proxy both hit
a Cloudflare interstitial at the same time and both start clicking a Turnstile.
That is the opposite of stealth: the egress suddenly produces two near-identical
challenges at once, which is itself a signal, and it wastes one of the two
solves. The correct behaviour is what two people behind one NAT do — one solves,
the other waits and reuses the `cf_clearance` cookie. `ClearanceBroker` enforces
exactly one solver per (egress, host) using an `flock` on a state file. A file
lock is released by the kernel when the solving process dies, so a worker killed
by the fleet watchdog can never wedge the egress behind a stale lock.

**2. Choosing an egress.** Egress reputation is the hard ceiling on this whole
rig: a clean residential IP sails through a managed challenge, a flagged
datacenter IP gets an interactive widget no amount of human motion will clear.
`EgressPool` therefore spends the egresses it knows something about first: ones
that already hold a warm clearance, then ones that were used longest ago, then
ones that have been blocking. Outcomes are persisted to the state dir, so a
proxy that blocked all of last run starts at the back of the queue this run
instead of being re-discovered at the cost of a worker.

Neither class promises a pass. They promise that no worker is spent twice on the
same question.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

try:  # POSIX only; the rig targets Linux/VPS and macOS
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

# `cf_clearance` is short-lived (Cloudflare's own TTL is ~30 min). We cache for
# less than that: a stale cookie is worse than none, because a request that
# presents one and gets bounced looks like a replay.
CLEARANCE_TTL_S = 1500


def egress_label(proxy_label: Optional[str]) -> str:
    """Normalise an egress key. A direct connection is its own egress."""
    return proxy_label or "direct"


def clearance_key(proxy_label: Optional[str], host: str) -> str:
    """Cache key for (egress, host) — IP- and target-bound, by design."""
    raw = f"{egress_label(proxy_label)}|{host}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def clearance_path(state_dir: str, proxy_label: Optional[str], host: str) -> str:
    return os.path.join(state_dir, f"cf_{clearance_key(proxy_label, host)}.json")


def lock_path(state_dir: str, proxy_label: Optional[str], host: str) -> str:
    return os.path.join(state_dir, f"cf_{clearance_key(proxy_label, host)}.lock")


def read_clearance(path: str) -> Dict[str, str]:
    """Fresh cookies from a clearance file, or {} (expired files are reaped)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if float(blob.get("expires", 0)) < time.time():
            try:
                os.remove(path)
            except OSError:
                pass
            return {}
        cookies = blob.get("cookies") or {}
        return {str(k): str(v) for k, v in cookies.items()}
    except (OSError, ValueError, TypeError):
        return {}


class ClearanceBroker:
    """One solver per (egress, host), shared across worker processes.

    The lock is an `flock` on `<state>/cf_<key>.lock`, held for the whole solve.
    Contenders poll the clearance file while they wait; if the holder dies or
    takes too long, the next contender takes the lock and solves itself.
    """

    def __init__(
        self,
        state_dir: str = ".stealth-state",
        *,
        shared: bool = True,
        poll_s: float = 0.5,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.state_dir = state_dir
        self.shared = shared and fcntl is not None
        self.poll_s = max(0.05, poll_s)
        self._log = log or (lambda *_a: None)
        self._held: Dict[str, int] = {}
        os.makedirs(self.state_dir, exist_ok=True)
        if shared and fcntl is None:  # pragma: no cover - Windows
            self._log("warn", "fcntl unavailable — clearance sharing disabled")

    # -- cache ------------------------------------------------------------ #

    def path(self, proxy_label: Optional[str], host: str) -> str:
        return clearance_path(self.state_dir, proxy_label, host)

    def cached(self, proxy_label: Optional[str], host: str) -> Dict[str, str]:
        """A clearance this egress already holds, from this worker or a peer."""
        return read_clearance(self.path(proxy_label, host))

    def publish(
        self,
        proxy_label: Optional[str],
        host: str,
        cookies: Dict[str, str],
        ttl_s: int = CLEARANCE_TTL_S,
    ) -> None:
        """Make clearance visible to every worker on this egress."""
        if not cookies:
            return
        path = self.path(proxy_label, host)
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"cookies": cookies, "expires": time.time() + ttl_s}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            # Atomic: a waiting peer must never read a half-written cookie jar.
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # -- solve slot ------------------------------------------------------- #

    def try_claim(self, proxy_label: Optional[str], host: str, worker: str = "") -> bool:
        """Become the designated solver. False means a peer already is one."""
        if not self.shared:
            return True
        key = clearance_key(proxy_label, host)
        if key in self._held:
            return True
        path = lock_path(self.state_dir, proxy_label, host)
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return True  # no lock file, no coordination — fail open
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:  # annotate the holder for debugging; the lock itself is the truth
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "worker": worker,
                                     "ts": time.time()}).encode())
            self._held[key] = fd
        except OSError:
            os.close(fd)
            return False
        return True

    def release(self, proxy_label: Optional[str], host: str) -> None:
        """Drop the solve slot. The kernel also does this if the worker dies."""
        fd = self._held.pop(clearance_key(proxy_label, host), None)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    async def wait_for_peer(
        self,
        proxy_label: Optional[str],
        host: str,
        timeout_s: float,
        *,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, str]:
        """Wait for the current solver to publish clearance. {} on timeout."""
        deadline = time.time() + max(0.0, timeout_s)
        while time.time() < deadline:
            await asyncio.sleep(self.poll_s)
            cookies = self.cached(proxy_label, host)
            if cookies:
                return cookies
            if should_abort and should_abort():
                return {}
        return {}

    def close(self) -> None:
        for key in list(self._held):
            fd = self._held.pop(key)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Egress selection
# --------------------------------------------------------------------------- #


@dataclass
class EgressStat:
    """What this rig has learned about one egress over time."""

    attempts: int = 0
    hits: int = 0
    misses: int = 0
    solves: int = 0      # challenges this egress had to clear
    blocks: int = 0      # challenges it failed to clear
    reused: int = 0      # clearances picked up from a peer
    last_used: float = 0.0
    last_hit: float = 0.0

    # per-run scratch, never persisted
    uses: int = field(default=0, compare=False)


class EgressPool:
    """Assigns one egress per worker and remembers how each one behaved.

    Selection order (highest priority first):

    1. egresses already holding a warm `cf_clearance` for this host — a worker
       that starts with clearance skips the challenge entirely
    2. egresses used longest ago (and least in this run) — rotate, don't hammer
    3. egresses that have blocked before — used last, but never excluded, since
       a small pool has to cycle and reputation drifts
    """

    def __init__(
        self,
        proxies: Sequence,
        *,
        state_dir: str = ".stealth-state",
        host: str = "",
        broker: Optional[ClearanceBroker] = None,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.proxies = list(proxies)
        self.state_dir = state_dir
        self.host = host
        self.broker = broker
        self._log = log or (lambda *_a: None)
        self.stats: Dict[str, EgressStat] = {}
        self._path = os.path.join(state_dir, "egress.json")
        os.makedirs(self.state_dir, exist_ok=True)
        self._load()

    # -- persistence ------------------------------------------------------ #

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        for label, row in (blob.get("egress") or {}).items():
            if not isinstance(row, dict):
                continue
            known = {f for f in EgressStat.__dataclass_fields__ if f != "uses"}
            self.stats[str(label)] = EgressStat(
                **{k: v for k, v in row.items() if k in known}
            )

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated": time.time(),
            "host": self.host,
            "egress": {
                label: {k: v for k, v in asdict(st).items() if k != "uses"}
                for label, st in self.stats.items()
            },
        }
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as e:  # stats are a nicety, never a reason to fail a run
            self._log("warn", f"could not persist egress stats: {e}")

    # -- selection -------------------------------------------------------- #

    def begin_run(self) -> None:
        for st in self.stats.values():
            st.uses = 0

    def _stat(self, label: str) -> EgressStat:
        return self.stats.setdefault(label, EgressStat())

    def plan(self, count: int) -> List[Optional[object]]:
        """Return one proxy (or None for direct) per worker."""
        if not self.proxies:
            return [None] * count

        def warm(p) -> bool:
            if not self.broker or not self.host:
                return False
            return bool(self.broker.cached(p.label, self.host))

        ranked = sorted(
            self.proxies,
            key=lambda p: (
                0 if warm(p) else 1,          # 1. warm clearance first
                self._stat(p.label).uses,     # 2. least used in this run
                self._stat(p.label).blocks,   # 3. blockers last
                self._stat(p.label).last_used,  # 4. longest idle first
                p.label,                      # 5. stable tiebreak
            ),
        )
        out: List[Optional[object]] = []
        for i in range(count):
            proxy = ranked[i % len(ranked)]
            st = self._stat(proxy.label)
            st.uses += 1
            st.last_used = time.time()
            out.append(proxy)
        if len(self.proxies) < count:
            shared = {p.label for p in ranked}
            self._log(
                "warn",
                f"egress pool has {len(self.proxies)} endpoint(s) for {count} "
                f"worker(s) — workers will share {', '.join(sorted(shared))}; "
                f"the clearance broker keeps them from solving twice",
            )
        return out

    # -- outcomes --------------------------------------------------------- #

    def record(
        self,
        proxy_label: Optional[str],
        *,
        hits: int = 0,
        misses: int = 0,
        solves: int = 0,
        blocks: int = 0,
        reused: int = 0,
    ) -> None:
        st = self._stat(egress_label(proxy_label))
        st.attempts += max(0, hits) + max(0, misses)
        st.hits += max(0, hits)
        st.misses += max(0, misses)
        st.solves += max(0, solves)
        st.blocks += max(0, blocks)
        st.reused += max(0, reused)
        st.last_used = time.time()
        if hits:
            st.last_hit = time.time()

    def summary(self) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        labels = [p.label for p in self.proxies] or sorted(self.stats)
        for label in labels:
            st = self.stats.get(label)
            if st is None:
                continue
            rows.append(
                {
                    "egress": label,
                    "attempts": st.attempts,
                    "hits": st.hits,
                    "misses": st.misses,
                    "solves": st.solves,
                    "blocks": st.blocks,
                    "reused": st.reused,
                }
            )
        rows.sort(key=lambda r: (-int(r["hits"]), str(r["egress"])))
        return rows
