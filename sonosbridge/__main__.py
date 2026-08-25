"""Entry point: ``python -m sonosbridge``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from .bridge import Bridge
from .config import BRIDGE_NAME, BRIDGE_VERSION, Config

LOGGER = logging.getLogger("sonosbridge")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # aiohttp logs a warning for every client disconnect mid-NOTIFY, which is
    # normal when a control point goes away.
    logging.getLogger("aiohttp.server").setLevel(logging.ERROR)


async def run() -> int:
    config = Config.from_env()
    configure_logging(config.log_level)
    LOGGER.info("%s %s starting", BRIDGE_NAME, BRIDGE_VERSION)

    bridge = Bridge(config)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, signal_name), stop_event.set)

    try:
        await bridge.start()
    except OSError as exc:
        LOGGER.error("Could not start: %s", exc)
        if getattr(exc, "errno", None) in (98, 48):  # EADDRINUSE
            LOGGER.error(
                "Another program already holds port %d or UDP 1900 on this host. "
                "Stop it, or set HTTP_PORT / SSDP_PORT to something else.",
                config.http_port,
            )
        await bridge.stop()
        return 1

    await stop_event.wait()
    LOGGER.info("Shutting down")
    await bridge.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
