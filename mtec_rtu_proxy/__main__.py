"""Entry point: ``python -m mtec_rtu_proxy`` (config from environment)."""
from __future__ import annotations

import asyncio
import logging

from .config import Config
from .proxy import run


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(Config.from_env()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
