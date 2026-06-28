"""Dedicated process entrypoint for TradeBot background workers."""
from __future__ import annotations

import asyncio
import signal

from loguru import logger

from app.core.database import init_db
from app.core.logging import configure_logging
from app.workers.runtime import start_background_workers, stop_background_workers


async def run_worker_process() -> None:
    """Run workers until process termination signal."""
    configure_logging()
    logger.info("Worker process starting...")

    await init_db()
    started = start_background_workers(allow_in_api=True)
    logger.info(f"Workers started: {started}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()

    logger.info("Worker shutdown signal received")
    stop_background_workers()


def main() -> None:
    asyncio.run(run_worker_process())


if __name__ == "__main__":
    main()
