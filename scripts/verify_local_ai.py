#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings


def _status(path: Path) -> str:
    if path.exists() and path.is_file():
        size_mb = path.stat().st_size / (1024 * 1024)
        return f"ok ({size_mb:.1f} MB)"
    return "missing"


def main() -> int:
    settings = load_settings()
    print(f"LOCAL_AI_ENABLED={settings.local_ai_enabled}")
    print(f"essentia installed={'yes' if importlib.util.find_spec('essentia') else 'no'}")
    print(f"torch installed={'yes' if importlib.util.find_spec('torch') else 'no'}")
    print(f"transformers installed={'yes' if importlib.util.find_spec('transformers') else 'no'}")
    print(f"models_dir={settings.local_ai_models_dir}")
    print(f"clap_model={settings.clap_model_name}")
    print(f"clap_cache_dir={settings.clap_cache_dir}")
    print()
    print("essentia_discogs_effnet:")
    print(f"  embedding_model={_status(settings.essentia_discogs_embedding_model)} {settings.essentia_discogs_embedding_model}")
    print(f"  prediction_model={_status(settings.essentia_discogs_prediction_model)} {settings.essentia_discogs_prediction_model}")
    print(f"  labels={_status(settings.essentia_discogs_labels)} {settings.essentia_discogs_labels}")
    print()
    print("musicnn_mtg_jamendo:")
    print(f"  prediction_model={_status(settings.musicnn_mtg_jamendo_model)} {settings.musicnn_mtg_jamendo_model}")
    print(f"  labels={_status(settings.musicnn_mtg_jamendo_labels)} {settings.musicnn_mtg_jamendo_labels}")
    print()
    print("clap_zero_shot:")
    print(f"  dependencies={'yes' if importlib.util.find_spec('torch') and importlib.util.find_spec('transformers') else 'no'}")
    print(f"  label_specs={len(settings.clap_label_specs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
