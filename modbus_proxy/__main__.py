"""Entry point: ``python -m modbus_proxy`` (config from environment)."""
from __future__ import annotations

import asyncio
import logging

from .config import Config
from .proxy import run


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
