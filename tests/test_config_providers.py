"""Provider-enable config: the default must never silently starve the free/web
stages while leaving the paid stage on, and a mis-set stage list must warn."""
from __future__ import annotations

import logging

from tag_creator import config


def _isolate_env(monkeypatch):
    # Ignore the real .env so we test the code defaults / explicit env only.
    monkeypatch.setattr(config, "_load_env", lambda: None)
    for var in (
        "ENABLED_PROVIDERS",
        "FREE_STAGE_PROVIDERS",
        "WEB_STAGE_PROVIDERS",
        "PAID_STAGE_PROVIDERS",
        "LOCAL_AI_STAGE_PROVIDERS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_enabled_defaults_to_union_of_all_stages(monkeypatch):
    _isolate_env(monkeypatch)
    settings = config.load_settings()
    enabled = set(settings.enabled_providers)
    every_stage = (
        settings.free_stage_providers
        + settings.web_stage_providers
        + settings.paid_stage_providers
        + settings.local_ai_stage_providers
    )
    # Every provider any stage references must be enabled -> nothing silently dropped.
    for name in every_stage:
        assert name in enabled, f"{name} would be silently skipped"
    # The exact free/web providers the old default omitted must now be enabled.
    # Paid providers stay paused unless PAID_STAGE_PROVIDERS explicitly names them.
    assert {"itunes", "deezer", "wikidata", "web_discovery", "rules_inference"} <= enabled
    assert "sonoteller" not in enabled


def test_explicit_enabled_env_is_respected(monkeypatch):
    _isolate_env(monkeypatch)
    monkeypatch.setenv("ENABLED_PROVIDERS", "itunes,musicbrainz")
    settings = config.load_settings()
    assert set(settings.enabled_providers) == {"itunes", "musicbrainz"}


def test_warns_when_a_stage_provider_is_not_enabled(monkeypatch, caplog):
    _isolate_env(monkeypatch)
    monkeypatch.setenv("ENABLED_PROVIDERS", "itunes")
    monkeypatch.setenv("FREE_STAGE_PROVIDERS", "itunes,deezer")
    with caplog.at_level(logging.WARNING, logger="tag_creator.config"):
        config.load_settings()
    assert any("deezer" in record.getMessage() for record in caplog.records)
