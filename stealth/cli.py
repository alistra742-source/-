"""CLI for the stealth rig — the Camoufox counterpart to `node bot.js`.

    python -m stealth <username>
    python -m stealth <username> --contexts 2 --tabs 3 --proxy-file proxies.txt
    python -m stealth <username> --headful --os windows --rounds 5

Environment mirrors bot.js where it makes sense:

    BASE_URL, BROWSERS, PAGES, HEADFUL, DWELL_MIN/DWELL_MAX, GAP_MIN/GAP_MAX,
    PROXY_FILE, STEALTH_OS, STATE_DIR
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Optional

from .chromium import ChromiumFleet
from .config import OS_TARGETS, StealthConfig, parse_proxy_text
from .session import StealthFleet


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m stealth",
        description="Insane-stealth Camoufox view runner (TLS spoof + humanizer + CF bypass).",
    )
    p.add_argument("target", nargs="?", default="", help="username -> <base-url>/<username>")
    p.add_argument("--base-url", default=os.environ.get("BASE_URL", "https://guns.lol"))
    p.add_argument("--proxies", default="", help="inline proxy list (one per line)")
    p.add_argument("--proxy-file", default=os.environ.get("PROXY_FILE", "proxies.txt"))
    p.add_argument("--contexts", type=int, default=_env_int("BROWSERS", 2),
                   help="browser instances (each its own proxy) [env BROWSERS]")
    p.add_argument("--tabs", type=int, default=_env_int("PAGES", 1),
                   help="workers per browser [env PAGES]")
    p.add_argument("--rounds", type=int, default=1, help="navigation rounds per worker")
    p.add_argument("--headful", action="store_true", default=bool(os.environ.get("HEADFUL")),
                   help="show the browser window")
    p.add_argument("--virtual", action="store_true",
                   help="headed browser on a private Xvfb display (best stealth on a VPS)")
    p.add_argument("--os", dest="os_target", choices=OS_TARGETS,
                   default=os.environ.get("STEALTH_OS") or None,
                   help="identity OS to claim (default: pick per session)")
    p.add_argument("--locale", default=None, help="force a locale, e.g. en-GB")
    p.add_argument("--tls-impersonate", default=None,
                   help="pin a curl_cffi preset (default: rotate)")
    p.add_argument("--no-humanize", action="store_true", help="disable cursor humanizer")
    p.add_argument("--no-geoip", action="store_true", help="do not align tz/locale with exit IP")
    p.add_argument("--no-turnstile", action="store_true", help="do not attempt CF challenges")
    p.add_argument("--block-images", action="store_true", help="block image requests")
    p.add_argument("--timeout", type=int, default=60_000, help="nav timeout ms")
    p.add_argument("--dwell-min", type=int, default=_env_int("DWELL_MIN", 9_000))
    p.add_argument("--dwell-max", type=int, default=_env_int("DWELL_MAX", 22_000))
    p.add_argument("--gap-min", type=int, default=_env_int("GAP_MIN", 20_000))
    p.add_argument("--gap-max", type=int, default=_env_int("GAP_MAX", 60_000))
    p.add_argument("--state-dir", default=os.environ.get("STATE_DIR", ".stealth-state"))
    p.add_argument("--worker-timeout", type=int, default=240,
                   help="hard ceiling (s) per worker process; a stalled browser is killed")
    p.add_argument("--stagger", type=float, default=1.5,
                   help="seconds between worker spawns (cold-start RAM spike)")
    p.add_argument("--launch-retries", type=int, default=1,
                   help="relaunch a worker whose browser never came up (default 1)")
    p.add_argument("--no-share-clearance", action="store_true",
                   help="let every worker solve its own challenge (workers sharing "
                        "one egress will then solve it once each)")
    p.add_argument("--clearance-wait", type=int, default=30,
                   help="seconds to wait for a sibling worker's solve on the same egress")
    p.add_argument("--debug-config", action="store_true",
                   help="print the Camoufox config sent to the browser (leak check)")

    eng = p.add_argument_group("engines")
    eng.add_argument("--engine", choices=("camoufox", "chromium", "guns"),
                     default=os.environ.get("STEALTH_ENGINE", "camoufox"),
                     help="camoufox = stealth rig (default); chromium = fast Chromium "
                          "fleet, one browser many contexts; guns = the native guns.lol "
                          "protocol, no page load at all")
    eng.add_argument("--no-fast", action="store_true",
                     help="chromium: load the full page (stylesheets, images, fonts) "
                          "and wait for 'load' instead of dropping heavy resources")
    eng.add_argument("--drop", default="image,font,media",
                     help="chromium: resource types to abort (default image,font,media)")
    eng.add_argument("--wait-until", default="domcontentloaded",
                     choices=("domcontentloaded", "load", "commit"),
                     help="chromium: navigation wait (default domcontentloaded)")
    eng.add_argument("--idle-wait", type=int, default=0,
                     help="chromium: also wait up to N ms for networkidle (default 0)")

    guns = p.add_argument_group("native guns.lol protocol (--engine guns)")
    guns.add_argument("--count", type=int, default=1, help="views to record (default 1)")
    guns.add_argument("--concurrency", type=int, default=4,
                      help="views recorded at once (default 4)")
    guns.add_argument("--device", choices=("desktop", "mobile", "tablet"), default="desktop",
                      help="device enum sent with the view")
    guns.add_argument("--captcha-provider", default=os.environ.get("CAPTCHA_PROVIDER", ""),
                      help="capmonster | solverify | uncaptcha — mints the Turnstile "
                           "token and the cf_clearance the analytics POST needs")
    guns.add_argument("--captcha-key", default=os.environ.get("CAPTCHA_KEY", ""),
                      help="API key for the captcha provider")
    guns.add_argument("--cf-clearance", default="",
                      help="a cf_clearance minted elsewhere (skips the provider's clearance task)")
    guns.add_argument("--turnstile-token", default="",
                      help="a Turnstile token minted elsewhere (skips the provider's token task)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> StealthConfig:
    proxies = []
    if args.proxies.strip():
        proxies, bad = parse_proxy_text(args.proxies)
        for line in bad:
            print(f"[warn] proxy rejected: {line}", file=sys.stderr)
    headless: object = True
    if args.virtual:
        headless = "virtual"
    elif args.headful:
        headless = False

    return StealthConfig(
        target=args.target,
        base_url=args.base_url,
        proxies=proxies,
        proxy_file=args.proxy_file,
        contexts=args.contexts,
        tabs=args.tabs,
        headless=headless,  # type: ignore[arg-type]
        os_target=args.os_target,
        humanize=not args.no_humanize,
        geoip=not args.no_geoip,
        locale=args.locale,
        block_images=args.block_images,
        tls_impersonate=args.tls_impersonate,
        nav_timeout_ms=args.timeout,
        dwell=(args.dwell_min, args.dwell_max),
        round_gap=(args.gap_min, args.gap_max),
        solve_turnstile=not args.no_turnstile,
        state_dir=args.state_dir,
        worker_timeout_s=args.worker_timeout,
        spawn_stagger_s=args.stagger,
        launch_retries=max(0, args.launch_retries),
        share_clearance=not args.no_share_clearance,
        clearance_wait_s=float(args.clearance_wait),
        engine=args.engine,
        fast_browser=not args.no_fast,
        drop_resources=tuple(x.strip() for x in str(args.drop).split(",") if x.strip()),
        wait_until=args.wait_until,
        idle_wait_ms=max(0, args.idle_wait),
        view_count=max(1, args.count),
        view_concurrency=max(1, args.concurrency),
        device=args.device,
        captcha_provider=args.captcha_provider,
        captcha_key=args.captcha_key,
        cf_clearance=args.cf_clearance,
        turnstile_token=args.turnstile_token,
        debug_config=args.debug_config,
        verbose=args.verbose,
    )


async def _run(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    if not cfg.target.strip():
        print("no target — name it:  python -m stealth <username>", file=sys.stderr)
        return 1
    try:
        cfg.validate()
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    started = time.time()

    def log(tag: str, msg: str) -> None:
        up = f"{(time.time() - started):5.0f}s"
        print(f"[{up}][{tag}] {msg}", flush=True)

    if cfg.engine == "guns":
        return await _run_guns(cfg, args, log)

    fleet = ChromiumFleet(cfg, fast=cfg.fast_browser, log=log) if cfg.engine == "chromium" \
        else StealthFleet(cfg, log=log)
    try:
        status = await fleet.run(rounds=max(1, args.rounds))
    except KeyboardInterrupt:
        await fleet.stop()
        log("stop", "interrupted")
        return 130
    except Exception as e:
        log("error", str(e).splitlines()[0])
        return 3

    log("done", f"hits={status['hits']} misses={status['misses']} mode={status['mode']}")
    if status.get("load_ms_median"):
        log("speed", f"median page load {status['load_ms_median']:.0f}ms "
                     f"(min {status['load_ms_min']:.0f}ms)"
                     + (f" · aborted {sum(status['blocked'].values())} heavy request(s)"
                        if status.get("blocked") else ""))
    for row in status.get("egress") or []:
        log(
            "egress",
            f"{row['egress']}: hits={row['hits']} misses={row['misses']} "
            f"solves={row['solves']} reused={row['reused']} blocks={row['blocks']}",
        )
    return 0


async def _run_guns(cfg: StealthConfig, args: argparse.Namespace, log) -> int:
    """Record views over the native guns.lol protocol — no page load per view."""
    from .guns import GunsRunner
    from .captcha import CaptchaError, build_provider

    provider = None
    if cfg.captcha_provider:
        try:
            provider = build_provider(cfg.captcha_provider, cfg.captcha_key, log=log)
        except CaptchaError as e:
            log("error", str(e))
            return 2
        log("guns", f"captcha provider: {provider.name}")
    elif not cfg.turnstile_token:
        # Say it up front: the POST path is Cloudflare-gated, and without a token
        # and a clearance the run can only report the block, never a view.
        log("warn", "no --captcha-provider and no --turnstile-token: the analytics POST "
                    "is behind a Cloudflare Managed Challenge, so views will be refused")

    tokens = None  # type: ignore[assignment]
    if cfg.turnstile_token or cfg.cf_clearance:
        cookies = {"cf_clearance": cfg.cf_clearance} if cfg.cf_clearance else {}
        tokens = {i: {"token": cfg.turnstile_token, "cookies": cookies} for i in range(cfg.view_count)}

    proxies, bad = cfg.resolved_proxies()
    for line in bad:
        log("warn", f"proxy rejected: {line}")
    if proxies and not any(p.rotating for p in proxies):
        log("warn", f"none of the {len(proxies)} proxy line(s) contain "
                     f"{'{session}'} — every view will egress on the same IP, and the "
                     f"site counts about one view per IP per day")

    runner = GunsRunner(cfg.target.strip().strip("/"), state_dir=cfg.state_dir,
                        proxies=proxies, tokens=tokens, provider=provider,
                        device=cfg.device, log=log)
    try:
        status = await runner.run(count=cfg.view_count, concurrency=cfg.view_concurrency)
    except KeyboardInterrupt:
        log("stop", "interrupted")
        return 130
    except Exception as e:
        log("error", str(e).splitlines()[0])
        return 3
    if provider is not None:
        await provider.close()

    log("done", f"ok={status['ok']} failed={status['failed']}"
                + (f" stages={status['stages']}" if status["stages"] else ""))
    return 0 if status["failed"] == 0 else 1


def main(argv: Optional[list] = None) -> int:
    # `python -m stealth` puts runpy frames on the stack, which fpgen misreads as
    # "python -m fpgen" and disables itself. Repair before anything imports it.
    from ._compat import ensure_fpgen_network

    ensure_fpgen_network()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
