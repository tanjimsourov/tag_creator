#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings
from tag_creator.csv_store import CsvStore
from tag_creator.providers import provider_status
from tag_creator.rate_limit import RateLimiter


CATALOG_PROVIDERS = ["itunes", "deezer", "musicbrainz", "spotify", "lastfm", "discogs", "cover_art_archive", "wikidata"]
ADVANCED_PROVIDERS = ["essentia_discogs_effnet", "musicnn_mtg_jamendo", "sonoteller", "web_discovery", "rules_inference"]


def main() -> int:
    settings = load_settings()
    store = CsvStore(settings.data_dir)
    rows = provider_status(settings, store, RateLimiter(settings.rate_limits))
    store.close()
    by_name = {row["provider"]: row for row in rows}

    print("Tag Creator Server Readiness")
    print(f"input_dir={settings.input_dir}")
    print(f"data_dir={settings.data_dir}")
    print(f"output_dir={settings.output_dir}")
    print(f"cpu_threads={settings.cpu_threads}")
    print(f"local_ai_enabled={settings.local_ai_enabled}")
    print(f"essentia_installed={'yes' if importlib.util.find_spec('essentia') else 'no'}")
    print()

    missing_catalog = [name for name in CATALOG_PROVIDERS if by_name.get(name, {}).get("will_run") != "yes"]
    missing_advanced = [name for name in ADVANCED_PROVIDERS if by_name.get(name, {}).get("will_run") != "yes"]
    print("catalog_providers_ready=" + ", ".join(name for name in CATALOG_PROVIDERS if name not in missing_catalog))
    print("catalog_providers_missing=" + (", ".join(missing_catalog) or "none"))
    print("advanced_providers_ready=" + ", ".join(name for name in ADVANCED_PROVIDERS if name not in missing_advanced))
    print("advanced_providers_missing=" + (", ".join(missing_advanced) or "none"))
    print()

    errors: list[str] = []
    if not settings.input_dir.exists():
        errors.append(f"input directory does not exist: {settings.input_dir}")
    if not any(by_name.get(name, {}).get("will_run") == "yes" for name in CATALOG_PROVIDERS):
        errors.append("no catalog metadata provider is ready")
    if settings.local_ai_enabled and importlib.util.find_spec("essentia") is None:
        errors.append("LOCAL_AI_ENABLED=true but essentia is not installed")

    if errors:
        print("NOT READY")
        for error in errors:
            print(f"- {error}")
        return 2
    print("READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
