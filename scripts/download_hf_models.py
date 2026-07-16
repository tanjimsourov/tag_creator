#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings


def main() -> int:
    settings = load_settings()
    cache_dir = settings.clap_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache_dir))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(cache_dir))

    try:
        from transformers import ClapModel, ClapProcessor
    except ImportError as exc:
        print(f"missing dependency: {exc}", file=sys.stderr)
        print("Rebuild Docker with INSTALL_LOCAL_AI=true after pulling requirements-ai.lock.", file=sys.stderr)
        return 2

    print(f"Downloading CLAP model: {settings.clap_model_name}")
    print(f"Cache dir: {cache_dir}")
    ClapProcessor.from_pretrained(settings.clap_model_name, cache_dir=str(cache_dir))
    ClapModel.from_pretrained(settings.clap_model_name, cache_dir=str(cache_dir))
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
