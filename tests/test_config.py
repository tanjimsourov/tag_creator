from __future__ import annotations

import pytest

from tag_creator.config import ConfigError, load_settings


def test_invalid_number_raises_config_error(monkeypatch):
    monkeypatch.setenv("MIN_WRITE_CONFIDENCE", "not-a-number")
    with pytest.raises(ConfigError):
        load_settings()


def test_out_of_range_confidence_raises(monkeypatch):
    monkeypatch.setenv("MIN_FIELD_CONFIDENCE", "2.0")
    with pytest.raises(ConfigError):
        load_settings()


def test_invalid_bool_raises(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "maybe")
    with pytest.raises(ConfigError):
        load_settings()


def test_negative_workers_rejected(monkeypatch):
    monkeypatch.setenv("WORKER_THREADS", "-1")
    # -1 is coerced by max(1, ...) at load, so it never goes below 1
    settings = load_settings()
    assert settings.worker_threads >= 1


def test_provider_weights_merge_over_defaults(monkeypatch):
    monkeypatch.setenv("PROVIDER_WEIGHTS", "itunes:0.5")
    settings = load_settings()
    assert settings.provider_weights["itunes"] == 0.5
    # a provider not listed in the override keeps its built-in default
    assert "musicbrainz" in settings.provider_weights
    assert settings.provider_weights["essentia_features"] == 0.85


def test_good_config_loads(monkeypatch):
    settings = load_settings()
    assert 0.0 <= settings.min_field_confidence <= 1.0
    assert settings.worker_threads >= 1
