"""Compatibility shims for third-party quirks the rig has to work around.

Currently one: `fpgen`'s false "run as module" detection.

`fpgen/__init__.py` sets `FPGEN_NO_INIT=1` when the two OLDEST frames on the
stack are CPython's runpy frames — `_run_module_as_main` and
`_get_module_details`. That is meant to catch `python -m fpgen`, so the CLI can
load without pulling its model. But the same two frames sit at the bottom of the
stack for ANY `python -m ourpackage` run, including ours when Camoufox imports
fpgen to build a fingerprint. fpgen then sets its Bayesian network to `None`,
and Camoufox dies at launch with:

    NetworkError: FPGEN_NO_INIT is enabled. Cannot access the network.

The fix is to recognise the false positive, clear the flag, and rebuild the
network before Camoufox imports it.
"""

from __future__ import annotations

import importlib
import os


def ensure_fpgen_network() -> bool:
    """Rebuild fpgen's network if `python -m` falsely disabled it.

    Returns True when a repair was applied. Safe to call repeatedly and cheap
    when nothing is wrong.
    """
    try:
        import fpgen.pkgman as pkgman
        import fpgen.utils as utils
    except Exception:
        return False  # fpgen absent or already exploded — nothing to repair

    already_ok = (
        getattr(utils, "NETWORK", None) is not None
        and not os.environ.get("FPGEN_NO_INIT")
    )
    if already_ok:
        return False

    # Clear the flag and permanently disable the heuristic for this process, so
    # the rebuild below is not itself detected as a module run.
    os.environ.pop("FPGEN_NO_INIT", None)
    try:
        pkgman.__is_module__ = lambda: False  # type: ignore[assignment]
        # Re-execute the modules that captured the network at import time.
        importlib.reload(utils)
        try:
            import fpgen.generator as generator

            importlib.reload(generator)
        except Exception:
            pass
    except Exception:
        return False

    return getattr(utils, "NETWORK", None) is not None


def fpgen_is_ready() -> bool:
    """True when fpgen has a usable network (after any repair)."""
    try:
        import fpgen.utils as utils
    except Exception:
        return False
    return getattr(utils, "NETWORK", None) is not None
