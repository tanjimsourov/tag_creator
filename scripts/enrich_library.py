#!/usr/bin/env python3
"""Thin shim so `python scripts/enrich_library.py ...` keeps working.

The real CLI lives in tag_creator/cli.py (also exposed as the `tag-creator`
console script via pyproject.toml).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
