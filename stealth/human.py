"""Human behaviour layer — the "crazy stealth human".

Camoufox's own `humanize=True` replays *recorded cursor* trajectories for
`page.mouse`-style moves. That covers pointer motion and nothing else. This layer
adds the rest of what separates a person from a script:

* Bezier pointer paths with overshoot, jitter and per-segment easing — a mouse
  never travels in a straight line, and it never arrives exactly on the first
  try.
* Typing with dwell between keystrokes, word-boundary pauses, and the odd typo
  followed by backspace — nobody types 400wpm perfectly.
* Wheel scrolling with eased ramps and reading pauses, not `scrollTo` jumps.
* Idle micro-drift while "reading": small pointer moves, occasional scroll nudge,
  and a brief tab-blur/refocus so the page sees a real foreground/background
  transition.

All timings are drawn from ranges, not fixed constants, so two runs never share
a timing fingerprint.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import List, Optional, Tuple

Point = Tuple[float, float]


def _rand(a: float, b: float) -> float:
    return random.uniform(a, b)


async def _sleep_ms(ms: float) -> None:
    await asyncio.sleep(max(0.0, ms) / 1000.0)


def _bezier(control: List[Point], t: float) -> Point:
    """De Casteljau evaluation of a Bezier curve at t in [0, 1]."""
    pts = list(control)
    while len(pts) > 1:
        pts = [
            ((1 - t) * pts[i][0] + t * pts[i + 1][0],
             (1 - t) * pts[i][1] + t * pts[i + 1][1])
            for i in range(len(pts) - 1)
        ]
    return pts[0]


def _ease_out(t: float) -> float:
    """Fitts'-law-ish ease: fast launch, slow approach, slight sticky landing."""
    return 1 - (1 - t) ** 3


class HumanBehavior:
    """Attach to a Playwright page; every motion routes through the paths below."""

    def __init__(self, page, viewport: Tuple[int, int] = (1280, 720), seed: Optional[int] = None) -> None:
        self.page = page
        self.viewport = viewport
        self.rng = random.Random(seed)
        self._x, self._y = _rand(0, viewport[0] * 0.4), _rand(0, viewport[1] * 0.4)

    # -- pointer ---------------------------------------------------------- #

    def _path(self, start: Point, end: Point, curve: float = 0.35) -> List[Point]:
        """Build a Bezier control set between two points with a lateral bulge."""
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        dist = math.hypot(dx, dy) or 1.0
        # Perpendicular offset — the bow in the arc.
        nx, ny = -dy / dist, dx / dist
        bow = dist * curve * _rand(-1.0, 1.0)
        # Two interior control points, jittered, plus an overshoot target.
        c1 = (sx + dx * _rand(0.2, 0.4) + nx * bow * 0.6,
              sy + dy * _rand(0.2, 0.4) + ny * bow * 0.6)
        c2 = (sx + dx * _rand(0.6, 0.8) + nx * bow * 0.3,
              sy + dy * _rand(0.6, 0.8) + ny * bow * 0.3)
        return [(sx, sy), c1, c2, (ex, ey)]

    async def move(self, x: float, y: float, *, steps: Optional[int] = None) -> None:
        x = max(1.0, min(x, self.viewport[0] - 1))
        y = max(1.0, min(y, self.viewport[1] - 1))
        start = (self._x, self._y)
        # Overshoot then correct — the natural landing on a target.
        if self.rng.random() < 0.6:
            ox = x + _rand(-14, 14)
            oy = y + _rand(-14, 14)
            await self._travel(start, (ox, oy))
            start = (ox, oy)
        await self._travel(start, (x, y))
        self._x, self._y = x, y

    async def _travel(self, start: Point, end: Point) -> None:
        dist = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = int(max(8, min(60, dist / 8)))
        # Fitts: longer/farther motions take proportionally longer, capped.
        duration = _rand(180, 420) + min(dist * 0.9, 700)
        control = self._path(start, end)
        prev = start
        for i in range(1, steps + 1):
            t = _ease_out(i / steps)
            px, py = _bezier(control, t)
            # Sub-pixel tremor on the way.
            px += self.rng.gauss(0, 0.6)
            py += self.rng.gauss(0, 0.6)
            dt = duration / steps * _rand(0.6, 1.5)
            step_delay = max(3, dt * math.hypot(px - prev[0], py - prev[1]) / max(1.0, dist / steps))
            try:
                await self.page.mouse.move(px, py)
            except Exception:
                return
            await _sleep_ms(min(step_delay, 45))
            prev = (px, py)

    async def click(self, x: float, y: float) -> None:
        await self.move(x, y)
        await _sleep_ms(_rand(60, 220))
        try:
            await self.page.mouse.down()
            # Human press duration varies; a 0ms down/up is a bot tell.
            await _sleep_ms(_rand(45, 140))
            await self.page.mouse.up()
        except Exception:
            return

    async def click_element(self, selector: str, timeout: int = 5000) -> bool:
        try:
            el = await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            box = await el.bounding_box()
        except Exception:
            return False
        if not box:
            return False
        # Aim at the visual centre with a small offset, not the exact corner.
        cx = box["x"] + box["width"] * _rand(0.35, 0.65)
        cy = box["y"] + box["height"] * _rand(0.35, 0.65)
        await self.click(cx, cy)
        return True

    # -- typing ----------------------------------------------------------- #

    async def type_text(self, text: str, *, typos: bool = True) -> None:
        """Type like a person: variable dwell, word pauses, occasional typos."""
        for i, ch in enumerate(text):
            if typos and ch.isalpha() and self.rng.random() < 0.04:
                wrong = chr(ord(ch) + self.rng.choice([-1, 1]))
                await self._key(wrong)
                await _sleep_ms(_rand(60, 180))
                await self._key("Backspace")
                await _sleep_ms(_rand(40, 130))
            await self._key(ch)
            # Dwell after a keystroke; longer after spaces (the "think" beat).
            if ch == " ":
                await _sleep_ms(_rand(90, 340))
            else:
                await _sleep_ms(_rand(35, 165))
            if i and i % self.rng.randint(7, 14) == 0:
                await _sleep_ms(_rand(200, 600))

    async def _key(self, ch: str) -> None:
        try:
            if ch == " ":
                await self.page.keyboard.press("Space")
            elif ch == "Backspace":
                await self.page.keyboard.press("Backspace")
            elif len(ch) == 1 and ch.isprintable():
                await self.page.keyboard.type(ch)
            else:
                await self.page.keyboard.press(ch)
        except Exception:
            return

    # -- scroll ----------------------------------------------------------- #

    async def scroll(self, amount: Optional[int] = None, *, pauses: bool = True) -> None:
        """Wheel-scroll in eased increments, pausing to 'read'."""
        remaining = amount if amount is not None else self.rng.randint(300, 1400)
        direction = 1 if remaining >= 0 else -1
        remaining = abs(remaining)
        increments = max(1, remaining // self.rng.randint(90, 180))
        for _ in range(increments):
            step = min(remaining, self.rng.randint(70, 190))
            try:
                await self.page.mouse.wheel(0, step * direction)
            except Exception:
                return
            remaining -= step
            if pauses and self.rng.random() < 0.45:
                await _sleep_ms(_rand(400, 2200))
            else:
                await _sleep_ms(_rand(20, 90))

    # -- idle ------------------------------------------------------------- #

    async def idle(self, seconds: float) -> None:
        """Loiter like someone reading: micro-drift, scroll nudges, focus blips."""
        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            r = self.rng.random()
            try:
                if r < 0.55:
                    await self.move(
                        self._x + _rand(-120, 120),
                        self._y + _rand(-90, 90),
                    )
                elif r < 0.8:
                    await self.scroll(self.rng.randint(-200, 260))
                elif r < 0.92:
                    await self.page.keyboard.press("PageDown")
                    await _sleep_ms(_rand(500, 1600))
                else:
                    # Blur then refocus: a backgrounded tab that pops back.
                    try:
                        await self.page.evaluate("() => window.dispatchEvent(new Event('blur'))")
                        await _sleep_ms(_rand(300, 1200))
                        await self.page.evaluate("() => window.dispatchEvent(new Event('focus'))")
                    except Exception:
                        pass
            except Exception:
                return
            await _sleep_ms(_rand(500, 2600))
