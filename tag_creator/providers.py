from __future__ import annotations

from .clients import (
    AcoustIDClient,
    CoverArtArchiveClient,
    DiscogsClient,
    GeniusClient,
    ITunesClient,
    DeezerClient,
    EssentiaDiscogsEffnetClient,
    EssentiaFeaturesClient,
    LastFMClient,
    LocalCleanupClient,
    MusicNNMtgJamendoClient,
    MusicBrainzClient,
    SpotifyClient,
    SonotellerClient,
    WikidataClient,
    WebDiscoveryClient,
    RulesInferenceClient,
)
from .config import Settings
from .csv_store import CsvStore
from .rate_limit import RateLimiter


def _all_clients(settings: Settings, store: CsvStore, rate_limiter: RateLimiter) -> dict:
    """Single source of truth for provider wiring.

    Add or remove a provider here exactly once; the client map and the status
    report both derive from this dict.
    """
    return {
        "acoustid": AcoustIDClient(store, rate_limiter, settings),
        "itunes": ITunesClient(store, rate_limiter),
        "deezer": DeezerClient(store, rate_limiter),
        "local_cleanup": LocalCleanupClient(store, rate_limiter),
        "essentia_features": EssentiaFeaturesClient(store, rate_limiter, settings),
        "essentia_discogs_effnet": EssentiaDiscogsEffnetClient(store, rate_limiter, settings),
        "musicnn_mtg_jamendo": MusicNNMtgJamendoClient(store, rate_limiter, settings),
        "wikidata": WikidataClient(store, rate_limiter),
        "web_discovery": WebDiscoveryClient(store, rate_limiter, settings),
        "rules_inference": RulesInferenceClient(store, rate_limiter),
        "sonoteller": SonotellerClient(store, rate_limiter, settings),
        "musicbrainz": MusicBrainzClient(store, rate_limiter),
        "spotify": SpotifyClient(store, rate_limiter, settings),
        "lastfm": LastFMClient(store, rate_limiter, settings),
        "discogs": DiscogsClient(store, rate_limiter, settings),
        "genius": GeniusClient(store, rate_limiter, settings),
        "cover_art_archive": CoverArtArchiveClient(store, rate_limiter),
    }


def build_provider_client_map(settings: Settings, store: CsvStore, rate_limiter: RateLimiter) -> dict:
    return {
        name: client
        for name, client in _all_clients(settings, store, rate_limiter).items()
        if name in settings.enabled_providers and client.is_configured()
    }


def provider_status(settings: Settings, store: CsvStore, rate_limiter: RateLimiter) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name, client in _all_clients(settings, store, rate_limiter).items():
        enabled = name in settings.enabled_providers
        configured = client.is_configured()
        rows.append(
            {
                "provider": name,
                "enabled": "yes" if enabled else "no",
                "configured": "yes" if configured else "no",
                "will_run": "yes" if enabled and configured else "no",
            }
        )
    return rows
