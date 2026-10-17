#!/usr/bin/env python3
"""Rootless system-library setup for Camoufox on a slim Linux host.

Camoufox is a real Firefox build, so it links the GTK3 stack — `libgtk-3.so.0`,
`libgdk-3.so.0`, and friends. A slim container or CI image has none of them, and
the launch dies with:

    XPCOMGlueLoad error ... libmozgtk.so: libgtk-3.so.0: cannot open shared
    object file ... Couldn't load XPCOM.

This is the same problem `npm run deps` solves for the Chromium runner, with the
same solution: with root, `apt-get install`. Without root, download the `.deb`s,
follow their own `Depends`, unpack them into a project-local directory, and put
that directory on `LD_LIBRARY_PATH`.

    python stealth/scripts/linux_deps.py           # unpack into .foxlibs
    eval "$(python stealth/scripts/linux_deps.py --print-env)"   # export it

Never unpack libc locally — prepending a second glibc breaks the very child
processes this is meant to fix.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBS_ROOT = PROJECT_ROOT / ".foxlibs"

# Seed set: the GTK3 + X11 + NSS stack Firefox needs. Dependency walking pulls
# the transitive closure, so a miss here is filled in by the round loop.
SEED_PACKAGES = [
    "libgtk-3-0t64", "libgtk-3-0",
    "libgdk-3-0t64",
    "libatk1.0-0t64", "libatk1.0-0",
    "libatk-bridge2.0-0t64", "libatk-bridge2.0-0",
    "libatspi2.0-0t64", "libatspi2.0-0",
    "libcairo2", "libcairo-gobject2",
    "libpango-1.0-0", "libpangocairo-1.0-0", "libpangoft2-1.0-0",
    "libgdk-pixbuf-2.0-0", "libharfbuzz0b", "libepoxy0",
    "libx11-6", "libxcb1", "libxext6", "libxrender1", "libxfixes3",
    "libxdamage1", "libxcomposite1", "libxrandr2", "libxi6",
    "libxcursor1", "libxtst6", "libxkbcommon0",
    "libnspr4", "libnss3", "libdbus-1-3", "libdbus-glib-1-2",
    "libasound2t64", "libasound2",
    "libwayland-client0", "libwayland-cursor0", "libwayland-egl1",
    "libglib2.0-0t64", "libglib2.0-0",
    "libfontconfig1", "libfreetype6", "libpng16-16",
    "libcups2t64", "libcups2", "libavahi-client3", "libavahi-common3",
]

SKIP = {
    "libc6", "libc6-dev", "libcrypt1", "libgcc-s1", "libgcc1",
    "libstdc++6", "dpkg", "perl-base",
}


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _deb_deps(path: Path) -> list[str]:
    r = subprocess.run(["dpkg-deb", "-f", str(path), "Depends"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        return []
    out = []
    for part in r.stdout.replace("\n", "").split(","):
        name = part.strip().split("|")[0]
        name = name.split("(")[0].strip().split(":")[0].strip()
        if name and name[0].isalnum() and name not in SKIP and "_" not in name:
            out.append(name)
    return out


def collect_lib_dirs(root: Path, max_depth: int = 6) -> list[str]:
    """Every directory under `root` that actually holds shared objects."""
    found: list[str] = []
    stack = [(root, 0)]
    while stack and len(found) <= 128:
        path, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(path.iterdir())
        except OSError:
            continue
        has_lib = any(e.is_file() and (".so" in e.name) for e in entries)
        if has_lib:
            found.append(str(path))
        for e in entries:
            if e.is_dir():
                stack.append((e, depth + 1))
    return found


def main() -> int:
    if sys.platform != "linux":
        print("linux only", file=sys.stderr)
        return 1

    if "--print-env" in sys.argv:
        dirs = collect_lib_dirs(LIBS_ROOT) if LIBS_ROOT.exists() else []
        base = os.environ.get("LD_LIBRARY_PATH", "")
        joined = ":".join(dirs + ([base] if base else []))
        print(f'export LD_LIBRARY_PATH="{joined}"')
        # Same unpack, second half of the story: Chromium needs a fontconfig
        # that resolves, or its Skia font manager aborts the whole browser the
        # first time a page renders text. The font tree is already here.
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from stealth.chromium import ensure_fontconfig

            conf = ensure_fontconfig(PROJECT_ROOT)
            if conf:
                print(f'export FONTCONFIG_FILE="{conf}"')
                fc_path = LIBS_ROOT / "etc" / "fonts"
                if fc_path.exists():
                    print(f'export FONTCONFIG_PATH="{fc_path}"')
        except Exception as e:  # pragma: no cover - keep the library export usable
            print(f"# fontconfig not set up: {e}", file=sys.stderr)
        return 0

    if not _has("apt-get") or not _has("dpkg-deb"):
        print("need apt-get and dpkg-deb", file=sys.stderr)
        return 2

    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if as_root:
        print("[deps] root detected — apt-get install.")
        r = subprocess.run(["apt-get", "install", "-y"] + SEED_PACKAGES + ["libgtk-3-0"])
        return r.returncode

    tmp = PROJECT_ROOT / ".fxlibs-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        wanted: set[str] = set()
        queue = list(SEED_PACKAGES)
        for rnd in range(6):
            if not queue:
                break
            for pkg in queue:
                if pkg in wanted or pkg in SKIP:
                    continue
                wanted.add(pkg)
                # One at a time: apt-get download aborts a batch on one unknown
                # name, and half of these do not exist on any given release.
                subprocess.run(["apt-get", "download", pkg], cwd=tmp,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nxt: set[str] = set()
            for f in tmp.glob("*.deb"):
                for dep in _deb_deps(f):
                    if dep not in wanted:
                        nxt.add(dep)
            queue = list(nxt)
            if queue:
                print(f"[deps] resolving {len(queue)} dependency package(s)…")

        debs = list(tmp.glob("*.deb"))
        if not debs:
            print("[deps] no packages could be downloaded — run 'apt-get update' first",
                  file=sys.stderr)
            return 3

        LIBS_ROOT.mkdir(parents=True, exist_ok=True)
        ok = 0
        for f in debs:
            if subprocess.run(["dpkg-deb", "-x", str(f), str(LIBS_ROOT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                ok += 1
        print(f"[deps] unpacked {ok}/{len(debs)} packages into {LIBS_ROOT}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    dirs = collect_lib_dirs(LIBS_ROOT)
    print(f"[deps] {len(dirs)} lib dir(s) ready")
    print('[deps] run:  eval "$(python stealth/scripts/linux_deps.py --print-env)"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
