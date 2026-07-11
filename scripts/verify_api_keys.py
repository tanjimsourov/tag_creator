#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings
from tag_creator.csv_store import CsvStore
from tag_creator.providers import provider_status
from tag_creator.rate_limit import RateLimiter


def main() -> int:
    settings = load_settings()
    store = CsvStore(settings.data_dir)
    rows = provider_status(settings, store, RateLimiter(settings.rate_limits))
    for row in rows:
        print(
            f"{row['provider']}: enabled={row['enabled']} configured={row['configured']} will_run={row['will_run']}"
        )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
