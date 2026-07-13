"""Pipeline orchestration: merging, error isolation, resume, streaming report,
paid-call cap. All offline — providers are fakes, no network."""
from __future__ import annotations

import json
from pathlib import Path

from tag_creator import pipeline
from tag_creator.csv_store import CsvStore
from tag_creator.pipeline import enrich_library, enrich_one


def test_enrich_one_merges_provider_fields(make_settings, sample_mp3, media_factory, fake_provider_cls):
    settings = make_settings()
    media = media_factory(sample_mp3)
    client_map = {"free": fake_provider_cls("free", {"title": "Song", "artist": "Band"})}
    result = enrich_one(media, client_map, settings)
    assert result.merged.fields.get("title") == "Song"
    assert result.merged.fields.get("artist") == "Band"
    assert result.status == "dry_run_done"


def test_enrich_one_isolates_a_failing_provider(make_settings, sample_mp3, media_factory, fake_provider_cls):
    settings = make_settings()
    media = media_factory(sample_mp3)
    client_map = {
        "good": fake_provider_cls("good", {"title": "OK", "artist": "A"}),
        "bad": fake_provider_cls("bad", raises=True),
    }
    result = enrich_one(media, client_map, settings)  # must not raise
    assert result.merged.fields.get("title") == "OK"
    assert any("error:" in (p.notes or "") for p in result.provider_results)


def _seed_media_dir(mp3_factory, root: Path, count: int) -> Path:
    media_dir = root / "lib"
    for i in range(count):
        mp3_factory(media_dir / f"track_{i}.mp3")
    return media_dir


def test_enrich_library_writes_report_and_summary(tmp_path, make_settings, mp3_factory, fake_provider_cls, monkeypatch):
    media_dir = _seed_media_dir(mp3_factory, tmp_path, 3)
    settings = make_settings(input_dir=media_dir)
    fake_map = {"free": fake_provider_cls("free", {"title": "T", "artist": "A"})}
    monkeypatch.setattr(pipeline, "build_provider_client_map", lambda *a, **k: fake_map)

    store = CsvStore(settings.data_dir)
    try:
        summary = enrich_library(settings, store, input_dir=media_dir, report_csv=settings.report_csv)
    finally:
        store.close()

    assert summary.total_files == 3
    assert summary.written_rows == 3
    assert settings.report_csv.exists()
    assert settings.report_csv.with_suffix(".jsonl").exists()
    run_summary = json.loads((settings.output_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert run_summary["total_files"] == 3


def test_resume_skips_already_processed_files(tmp_path, make_settings, mp3_factory, fake_provider_cls, monkeypatch):
    media_dir = _seed_media_dir(mp3_factory, tmp_path, 3)
    settings = make_settings(input_dir=media_dir, resume=True)
    fake_map = {"free": fake_provider_cls("free", {"title": "T", "artist": "A"})}
    monkeypatch.setattr(pipeline, "build_provider_client_map", lambda *a, **k: fake_map)

    store = CsvStore(settings.data_dir)
    try:
        enrich_library(settings, store, input_dir=media_dir, report_csv=settings.report_csv)
    finally:
        store.close()

    # Second run on the same state must skip everything.
    store2 = CsvStore(settings.data_dir)
    try:
        summary2 = enrich_library(settings, store2, input_dir=media_dir, report_csv=settings.report_csv)
    finally:
        store2.close()
    assert summary2.skipped == 3
    assert summary2.written_rows == 0


def test_paid_guard_caps_paid_calls(tmp_path, make_settings, mp3_factory, fake_provider_cls, monkeypatch):
    media_dir = _seed_media_dir(mp3_factory, tmp_path, 3)
    settings = make_settings(
        input_dir=media_dir,
        hybrid_mode=True,
        paid_only_if_missing=False,   # always attempt the paid stage
        paid_stage_providers=["paid"],
        max_paid_calls=1,             # ...but cap it at one call for the whole run
        resume=False,
    )
    paid = fake_provider_cls("paid", {"mood": "happy"})
    monkeypatch.setattr(pipeline, "build_provider_client_map", lambda *a, **k: {"paid": paid})

    store = CsvStore(settings.data_dir)
    try:
        summary = enrich_library(settings, store, input_dir=media_dir, report_csv=settings.report_csv)
    finally:
        store.close()

    assert summary.paid_calls == 1
    assert len(paid.calls) == 1  # cap actually prevented the other two paid calls
