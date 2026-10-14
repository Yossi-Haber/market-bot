"""Best-effort Windows execution-state management for long local jobs."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import sys
from collections.abc import Iterator

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def prevent_system_sleep(*, logger: logging.Logger | None = None) -> Iterator[None]:
    """Keep Windows awake for the context lifetime without forcing the display on.

    This is intentionally best-effort: power management is protective and must
    never make a historical data job fail.  Non-Windows platforms are safe
    no-ops.
    """
    logger = logger or logging.getLogger("market_bot.power")
    if sys.platform != "win32":
        logger.info("PREVENT_SLEEP_UNAVAILABLE platform=%s", sys.platform)
        yield
        return

    setter = None
    try:
        setter = ctypes.windll.kernel32.SetThreadExecutionState
        setter.argtypes = [ctypes.c_uint]
        setter.restype = ctypes.c_uint
        result = setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if not result:
            logger.warning("PREVENT_SLEEP_FAILED platform=windows mode=batch pid=%s", os.getpid())
        else:
            logger.info("PREVENT_SLEEP_ENABLED platform=windows mode=batch pid=%s", os.getpid())
    except Exception as exc:  # pragma: no cover - exact Windows failures vary
        logger.warning("PREVENT_SLEEP_FAILED platform=windows error=%s", type(exc).__name__)

    try:
        yield
    finally:
        if setter is not None:
            try:
                result = setter(ES_CONTINUOUS)
                if not result:
                    logger.warning("PREVENT_SLEEP_FAILED platform=windows phase=release pid=%s", os.getpid())
                else:
                    logger.info("PREVENT_SLEEP_DISABLED platform=windows mode=batch pid=%s", os.getpid())
            except Exception as exc:  # pragma: no cover - exact Windows failures vary
                logger.warning("PREVENT_SLEEP_FAILED platform=windows phase=release error=%s", type(exc).__name__)
