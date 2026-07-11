from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from tag_creator.csv_store import CsvStore
from tag_creator.models import EnrichmentResult, MediaFile, MergedMetadata


def _store(**kw) -> CsvStore:
    return CsvStore(Path(tempfile.mkdtemp()), **kw)


def test_cache_roundtrip_and_last_wins_survives_reload():
    store = _store(flush_every=2)
    data_dir = store.data_dir
    store.set_cache("itunes", "k", 200, {"v": 0})
    for i in range(5):
        store.set_cache("itunes", "k", 200, {"v": i})
    assert store.get_cache("itunes", "k", 10_000) == {"v": 4}
    store.close()

    reopened = CsvStore(data_dir)
    assert reopened.get_cache("itunes", "k", 10_000) == {"v": 4}
    reopened.close()


def test_cache_ttl_expiry():
    store = _store()
    store.set_cache("itunes", "k", 200, {"v": 1})
    assert store.get_cache("itunes", "k", 0) is None  # max_age 0 -> expired
    store.close()


def test_resume_skip_matches_size_and_mtime():
    store = _store(flush_every=1)
    media = MediaFile(path=store.data_dir / "a.mp3", extension=".mp3", size_bytes=10, mtime=5.0, tags={})
    merged = MergedMetadata({}, {}, [], [], [])
    store.save_enrichment(EnrichmentResult(media, merged, "dry_run_done", [], []))
    assert store.should_skip(media.path, 10, 5.0) is True
    assert store.should_skip(media.path, 11, 5.0) is False
    assert store.should_skip(media.path, 10, 9.9) is False
    store.close()


def test_compaction_dedups_rows():
    store = _store(flush_every=1, compact_on_close=False)
    for i in range(10):
        store.set_cache("itunes", "k", 200, {"v": i})
    compacted = store.compact(force=True)
    assert "api_cache" in compacted
    rows = list(csv.DictReader((store.data_dir / "api_cache.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 1
    store.close()
