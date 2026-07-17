"""Shared, hermetic test fixtures — no network, no .env, no real audio needed."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tag_creator.config import Settings
from tag_creator.models import MediaFile, ProviderResult

# A tiny but VALID MPEG-1 Layer III stream (silent frames). mutagen opens it and
# ID3/MP4 tags round-trip cleanly, so the file-writing path can be tested offline.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * (417 - 4)  # 128 kbps, 44.1 kHz frame
MP3_BYTES = _MP3_FRAME * 24  # ~0.6 s


def make_mp3(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_BYTES)
    return path


@pytest.fixture
def sample_mp3(tmp_path: Path) -> Path:
    return make_mp3(tmp_path / "song.mp3")


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        input_dir=tmp_path / "media",
        output_dir=tmp_path / "output",
        data_dir=tmp_path / "data",
        report_csv=tmp_path / "output" / "report.csv",
        final_csv=tmp_path / "output" / "final.csv",
        final_no_blanks=True,
        final_missing_value="NEEDS_REVIEW",
        dry_run=True,
        write_tags=False,
        write_cover_art=False,
        backup_dir=None,
        verify_after_write=False,
        resume=True,
        limit=None,
        supported_extensions=[".mp3", ".m4a", ".mp4"],
        required_tags=["title", "artist"],
        min_field_confidence=0.40,
        min_write_confidence=0.40,
        fill_unknown_values=False,
        cpu_threads=1,
        worker_threads=1,
        enabled_providers=[],
        free_stage_providers=[],
        web_stage_providers=[],
        paid_stage_providers=[],
        local_ai_stage_providers=[],
        hybrid_mode=False,
        paid_only_if_missing=True,
        max_paid_calls=None,
        web_max_fetches_per_run=0,
        log_dir=tmp_path / "logs",
        log_json=False,
        local_ai_enabled=False,
        local_ai_always_run=False,
        local_ai_cache_ttl_days=365,
        local_ai_top_n=12,
        local_ai_min_score=0.18,
        local_ai_timeout_seconds=600,
        local_ai_models_dir=tmp_path / "models",
        clap_model_name="laion/clap-htsat-unfused",
        clap_cache_dir=tmp_path / "models" / "hf",
        clap_label_specs=[],
        essentia_discogs_embedding_model=tmp_path / "models" / "emb.pb",
        essentia_discogs_prediction_model=tmp_path / "models" / "genre.pb",
        essentia_discogs_labels=tmp_path / "models" / "labels.txt",
        musicnn_mtg_jamendo_model=tmp_path / "models" / "musicnn.pb",
        musicnn_mtg_jamendo_labels=tmp_path / "models" / "musicnn_labels.txt",
        essentia_extra_heads=[],
        web_scraping_enabled=False,
        web_max_results=5,
        web_allowed_domains=[],
        web_search_endpoint="",
        provider_weights={},   # unknown providers default to 0.5 in the merge
        rate_limits={},        # unknown providers default to 0 delay (no sleeping)
        acoustid_api_key="",
        fpcalc_path="",
        spotify_client_id="",
        spotify_client_secret="",
        lastfm_api_key="",
        discogs_token="",
        discogs_consumer_key="",
        discogs_consumer_secret="",
        genius_client_id="",
        genius_client_secret="",
        genius_access_token="",
        sonoteller_rapidapi_key="",
        sonoteller_rapidapi_host="",
        sonoteller_base_url="",
        sonoteller_analyze_endpoint="",
        sonoteller_input_mode="url",
        sonoteller_file_url_base="",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def make_settings(tmp_path: Path):
    """Return a factory: make_settings(**overrides) -> Settings (hermetic)."""
    def _factory(**overrides) -> Settings:
        return _settings(tmp_path, **overrides)

    return _factory


class FakeProvider:
    """Stand-in provider client for pipeline tests (no network)."""

    def __init__(self, name: str, fields: dict[str, str] | None = None, confidence: float = 1.0,
                 raises: bool = False, calls: list | None = None):
        self.provider_name = name
        self._fields = fields or {}
        self._confidence = confidence
        self._raises = raises
        self.calls = calls if calls is not None else []

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        self.calls.append(media.path)
        if self._raises:
            raise RuntimeError(f"{self.provider_name} boom")
        if not self._fields:
            return ProviderResult(self.provider_name, 0, {}, notes="no data")
        return ProviderResult(self.provider_name, self._confidence, dict(self._fields), notes="ok")


def media_for(path: Path, tags: dict[str, str] | None = None) -> MediaFile:
    stat = path.stat()
    return MediaFile(
        path=path,
        extension=path.suffix.lower(),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        tags=tags or {},
    )


# Expose helpers as fixtures so tests never depend on importing conftest directly.
@pytest.fixture
def mp3_factory():
    return make_mp3


@pytest.fixture
def fake_provider_cls():
    return FakeProvider


@pytest.fixture
def media_factory():
    return media_for
