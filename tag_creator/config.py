from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(Exception):
    """Raised for an invalid or out-of-range configuration value."""


def _load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid value for {name}: {value!r} (expected a boolean like true/false)")


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"Invalid value for {name}: {value!r} (expected a number)") from None


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(f"Invalid value for {name}: {value!r} (expected an integer)") from None


def _int_or_none(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(f"Invalid value for {name}: {value!r} (expected an integer or blank)") from None


def _list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def _mapping(name: str, default: dict[str, float]) -> dict[str, float]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    parsed: dict[str, float] = {}
    for item in value.split(","):
        if ":" not in item:
            continue
        key, raw_number = item.split(":", 1)
        try:
            parsed[key.strip()] = float(raw_number.strip())
        except ValueError:
            raise ConfigError(
                f"Invalid value for {name}: entry {item.strip()!r} is not 'name:number'"
            ) from None
    return parsed


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    input_dir: Path
    output_dir: Path
    data_dir: Path
    report_csv: Path
    final_csv: Path
    final_no_blanks: bool
    final_missing_value: str
    dry_run: bool
    write_tags: bool
    write_cover_art: bool
    backup_dir: Path | None
    verify_after_write: bool
    resume: bool
    limit: int | None
    supported_extensions: list[str]
    required_tags: list[str]
    min_field_confidence: float
    min_write_confidence: float
    fill_unknown_values: bool
    cpu_threads: int
    worker_threads: int
    enabled_providers: list[str]
    free_stage_providers: list[str]
    web_stage_providers: list[str]
    paid_stage_providers: list[str]
    local_ai_stage_providers: list[str]
    hybrid_mode: bool
    paid_only_if_missing: bool
    max_paid_calls: int | None
    web_max_fetches_per_run: int
    log_dir: Path
    log_json: bool
    local_ai_enabled: bool
    local_ai_always_run: bool
    local_ai_cache_ttl_days: int
    local_ai_top_n: int
    local_ai_min_score: float
    local_ai_timeout_seconds: int
    local_ai_models_dir: Path
    clap_model_name: str
    clap_cache_dir: Path
    clap_label_specs: list[str]
    clap_concurrency: int
    clap_max_seconds: int
    essentia_discogs_embedding_model: Path
    essentia_discogs_prediction_model: Path
    essentia_discogs_labels: Path
    musicnn_mtg_jamendo_model: Path
    musicnn_mtg_jamendo_labels: Path
    essentia_extra_heads: list[str]
    web_scraping_enabled: bool
    web_max_results: int
    web_max_queries_per_file: int
    web_allowed_domains: list[str]
    web_search_endpoint: str
    provider_weights: dict[str, float]
    rate_limits: dict[str, float]
    acoustid_api_key: str
    fpcalc_path: str
    spotify_client_id: str
    spotify_client_secret: str
    lastfm_api_key: str
    discogs_token: str
    discogs_consumer_key: str
    discogs_consumer_secret: str
    genius_client_id: str
    genius_client_secret: str
    genius_access_token: str
    sonoteller_rapidapi_key: str
    sonoteller_rapidapi_host: str
    sonoteller_base_url: str
    sonoteller_analyze_endpoint: str
    sonoteller_input_mode: str
    sonoteller_file_url_base: str


def _validate_settings(settings: Settings) -> None:
    """Fail fast on out-of-range values; warn (not crash) on soft issues."""
    problems: list[str] = []

    def _in_unit_range(name: str, value: float) -> None:
        if not 0.0 <= value <= 1.0:
            problems.append(f"{name}={value} must be between 0.0 and 1.0")

    _in_unit_range("MIN_FIELD_CONFIDENCE", settings.min_field_confidence)
    _in_unit_range("MIN_WRITE_CONFIDENCE", settings.min_write_confidence)
    _in_unit_range("LOCAL_AI_MIN_SCORE", settings.local_ai_min_score)

    if settings.worker_threads < 1:
        problems.append("WORKER_THREADS must be >= 1")
    if settings.cpu_threads < 1:
        problems.append("TAG_CREATOR_CPU_THREADS must be >= 1")
    if settings.local_ai_timeout_seconds <= 0:
        problems.append("LOCAL_AI_TIMEOUT_SECONDS must be > 0")
    if settings.web_max_results < 1:
        problems.append("WEB_MAX_RESULTS must be >= 1")
    if settings.web_max_queries_per_file < 1:
        problems.append("WEB_MAX_QUERIES_PER_FILE must be >= 1")
    if settings.local_ai_top_n < 1:
        problems.append("LOCAL_AI_TOP_N must be >= 1")
    if settings.clap_concurrency < 1:
        problems.append("CLAP_CONCURRENCY must be >= 1")
    if settings.clap_max_seconds < 1:
        problems.append("CLAP_MAX_SECONDS must be >= 1")
    if settings.limit is not None and settings.limit < 0:
        problems.append("LIMIT must be >= 0 or blank")
    if settings.max_paid_calls is not None and settings.max_paid_calls < 0:
        problems.append("MAX_PAID_CALLS must be >= 0 or blank")
    if settings.web_max_fetches_per_run < 0:
        problems.append("WEB_MAX_FETCHES_PER_RUN must be >= 0 (0 = unlimited)")

    for label, mapping in (("PROVIDER_WEIGHTS", settings.provider_weights), ("RATE_LIMITS", settings.rate_limits)):
        for key, number in mapping.items():
            if number < 0:
                problems.append(f"{label} value for {key!r} must be >= 0 (got {number})")

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))

    # Soft warnings — do not abort the run.
    # A provider named in a stage list but absent from ENABLED_PROVIDERS is silently
    # dropped from the client map; surface that so a mis-set .env is caught early.
    enabled = set(settings.enabled_providers)
    staged = (
        set(settings.free_stage_providers)
        | set(settings.web_stage_providers)
        | set(settings.paid_stage_providers)
        | set(settings.local_ai_stage_providers)
        | {"local_cleanup"}
    )
    dropped = sorted(name for name in staged if name not in enabled)
    if dropped:
        LOGGER.warning(
            "these stage providers are NOT in ENABLED_PROVIDERS and will be skipped: %s",
            ", ".join(dropped),
        )
    if settings.paid_stage_providers and not any(
        name in enabled for name in settings.free_stage_providers + settings.web_stage_providers
    ):
        LOGGER.warning(
            "paid stage is enabled but no free/web providers are — paid APIs would run without a free-first pass"
        )
    if not settings.input_dir.exists():
        LOGGER.warning("input directory does not exist yet: %s", settings.input_dir)
    if settings.local_ai_enabled:
        for path in (
            settings.essentia_discogs_embedding_model,
            settings.essentia_discogs_prediction_model,
            settings.musicnn_mtg_jamendo_model,
        ):
            if not path.exists():
                LOGGER.warning("LOCAL_AI_ENABLED but model file is missing: %s", path)


def load_settings() -> Settings:
    _load_env()
    default_weights = {
        "acoustid": 0.95,
        "local_cleanup": 0.70,
        "itunes": 0.78,
        "deezer": 0.74,
        "wikidata": 0.52,
        "essentia_features": 0.85,
        "essentia_discogs_effnet": 0.82,
        "musicnn_mtg_jamendo": 0.82,
        "clap_zero_shot": 0.86,
        "web_discovery": 0.58,
        "rules_inference": 0.50,
        "sonoteller": 0.94,
        "musicbrainz": 0.90,
        "spotify": 0.82,
        "lastfm": 0.62,
        "discogs": 0.76,
        "genius": 0.45,
        "cover_art_archive": 0.80,
    }
    default_rates = {
        "musicbrainz": 1.10,
        "local_cleanup": 0.00,
        "itunes": 0.15,
        "deezer": 0.25,
        "wikidata": 0.35,
        "essentia_features": 0.00,
        "essentia_discogs_effnet": 0.00,
        "musicnn_mtg_jamendo": 0.00,
        "clap_zero_shot": 0.00,
        "web_discovery": 2.00,
        "sonoteller": 1.00,
        "cover_art_archive": 0.25,
        "spotify": 0.10,
        "lastfm": 0.25,
        "discogs": 1.10,
        "acoustid": 0.35,
        "genius": 0.25,
    }
    free_stage = _list(
        "FREE_STAGE_PROVIDERS",
        ["local_cleanup", "itunes", "deezer", "wikidata", "acoustid", "musicbrainz", "spotify", "lastfm", "discogs", "genius", "cover_art_archive"],
    )
    web_stage = _list("WEB_STAGE_PROVIDERS", ["web_discovery", "rules_inference"])
    paid_stage = _list("PAID_STAGE_PROVIDERS", [])
    local_ai_stage = _list(
        "LOCAL_AI_STAGE_PROVIDERS",
        ["essentia_features", "essentia_discogs_effnet", "musicnn_mtg_jamendo", "clap_zero_shot"],
    )
    # Default ENABLED_PROVIDERS = every provider that any stage references (+ local_cleanup).
    # This guarantees a blank/minimal .env can never silently starve the free/web stages
    # while leaving the PAID stage enabled — the exact footgun the old fixed list caused.
    # Providers without an API key / model file self-skip via is_configured(), so enabling
    # the full union is safe and cost-free.
    enabled_default: list[str] = []
    for _stage_name in ["local_cleanup", *free_stage, *local_ai_stage, *web_stage, *paid_stage]:
        if _stage_name not in enabled_default:
            enabled_default.append(_stage_name)

    settings = Settings(
        input_dir=resolve_path(os.environ.get("INPUT_DIR", "../ftp_downloads/mp3")),
        output_dir=resolve_path(os.environ.get("OUTPUT_DIR", "output")),
        data_dir=resolve_path(os.environ.get("DATA_DIR", "data")),
        report_csv=resolve_path(os.environ.get("REPORT_CSV", "output/enrichment_report.csv")),
        final_csv=resolve_path(os.environ.get("FINAL_CSV", "output/final_enriched_tags.csv")),
        final_no_blanks=_bool("FINAL_NO_BLANKS", True),
        final_missing_value=os.environ.get("FINAL_MISSING_VALUE", "NEEDS_REVIEW").strip() or "NEEDS_REVIEW",
        dry_run=_bool("DRY_RUN", True),
        write_tags=_bool("WRITE_TAGS", False),
        write_cover_art=_bool("WRITE_COVER_ART", False),
        backup_dir=(resolve_path(os.environ["BACKUP_DIR"]) if os.environ.get("BACKUP_DIR", "").strip() else None),
        verify_after_write=_bool("VERIFY_AFTER_WRITE", False),
        resume=_bool("RESUME", True),
        limit=_int_or_none("LIMIT"),
        supported_extensions=_list("SUPPORTED_EXTENSIONS", [".mp3", ".m4a", ".mp4", ".aac", ".flac"]),
        required_tags=_list("REQUIRED_TAGS", ["title", "artist", "album", "year", "genre", "mood", "cover_art"]),
        min_field_confidence=_float("MIN_FIELD_CONFIDENCE", 0.72),
        min_write_confidence=_float("MIN_WRITE_CONFIDENCE", 0.82),
        fill_unknown_values=_bool("FILL_UNKNOWN_VALUES", False),
        cpu_threads=max(1, _int("TAG_CREATOR_CPU_THREADS", 2)),
        worker_threads=max(1, _int("WORKER_THREADS", min(6, max(1, (os.cpu_count() or 2) + 2)))),
        enabled_providers=_list("ENABLED_PROVIDERS", enabled_default),
        free_stage_providers=free_stage,
        web_stage_providers=web_stage,
        paid_stage_providers=paid_stage,
        local_ai_stage_providers=local_ai_stage,
        hybrid_mode=_bool("HYBRID_MODE", True),
        paid_only_if_missing=_bool("PAID_ONLY_IF_MISSING", True),
        max_paid_calls=_int_or_none("MAX_PAID_CALLS"),
        web_max_fetches_per_run=_int("WEB_MAX_FETCHES_PER_RUN", 0),
        log_dir=resolve_path(os.environ.get("LOG_DIR", "logs")),
        log_json=_bool("LOG_JSON", False),
        local_ai_enabled=_bool("LOCAL_AI_ENABLED", False),
        local_ai_always_run=_bool("LOCAL_AI_ALWAYS_RUN", False),
        local_ai_cache_ttl_days=_int("LOCAL_AI_CACHE_TTL_DAYS", 365),
        local_ai_top_n=_int("LOCAL_AI_TOP_N", 12),
        local_ai_min_score=_float("LOCAL_AI_MIN_SCORE", 0.18),
        local_ai_timeout_seconds=max(30, _int("LOCAL_AI_TIMEOUT_SECONDS", 600)),
        local_ai_models_dir=resolve_path(os.environ.get("LOCAL_AI_MODELS_DIR", "models/local_ai")),
        clap_model_name=os.environ.get("CLAP_MODEL_NAME", "laion/clap-htsat-unfused").strip(),
        clap_cache_dir=resolve_path(os.environ.get("CLAP_CACHE_DIR", "models/local_ai/hf")),
        clap_concurrency=max(1, _int("CLAP_CONCURRENCY", 2)),
        clap_max_seconds=max(1, _int("CLAP_MAX_SECONDS", 30)),
        clap_label_specs=_list(
            "CLAP_LABEL_SPECS",
            [
                "genre: pop",
                "genre: dance",
                "genre: electronic",
                "genre: house",
                "genre: deep house",
                "genre: tech house",
                "genre: techno",
                "genre: trance",
                "genre: drum and bass",
                "genre: disco",
                "genre: funk",
                "genre: soul",
                "genre: rock",
                "genre: indie",
                "genre: r&b",
                "genre: hip hop",
                "genre: rap",
                "genre: latin",
                "genre: afrobeat",
                "genre: reggae",
                "genre: jazz",
                "genre: acoustic",
                "genre: country",
                "genre: folk",
                "genre: edm",
                "subgenre: dance pop",
                "subgenre: eurodance",
                "subgenre: synth-pop",
                "subgenre: techno",
                "subgenre: trance",
                "subgenre: pop rock",
                "subgenre: indie pop",
                "subgenre: latin pop",
                "subgenre: reggaeton",
                "subgenre: afro house",
                "subgenre: slap house",
                "subgenre: tropical house",
                "subgenre: indie dance",
                "subgenre: commercial pop",
                "mood: upbeat",
                "mood: energetic",
                "mood: happy",
                "mood: calm",
                "mood: relaxing",
                "mood: romantic",
                "mood: melancholic",
                "mood: dramatic",
                "mood: confident",
                "mood: playful",
                "mood: elegant",
                "mood: intense",
                "mood: chillout",
                "mood: warm",
                "mood: bright",
                "mood: dark",
                "mood: smooth",
                "valence: positive",
                "valence: neutral",
                "valence: negative",
                "danceability: high",
                "danceability: medium",
                "danceability: low",
                "energy: high",
                "energy: medium",
                "energy: low",
                "occasion: retail background",
                "occasion: busy store",
                "occasion: premium atmosphere",
                "occasion: calm store",
                "occasion: party",
                "occasion: morning",
                "occasion: evening",
                "occasion: weekend",
                "occasion: family friendly",
                "occasion: youth audience",
                "occasion: luxury retail",
                "occasion: grocery store",
                "occasion: fashion store",
                "occasion: restaurant",
                "occasion: workout",
                "weather: sunny",
                "weather: rainy",
                "weather: cloudy",
                "weather: cold",
                "weather: warm",
                "season: summer",
                "season: spring",
                "season: autumn",
                "season: winter",
                "season: christmas",
                "age_group: youth",
                "age_group: adult",
                "age_group: family",
                "age_group: mature",
                "instrument: piano",
                "instrument: guitar",
                "instrument: drums",
                "instrument: bass",
                "instrument: percussion",
                "instrument: electronic beat",
                "instrument: synthesizer",
                "instrument: strings",
                "instrument: acoustic guitar",
                "instrument: electric guitar",
                "instrument: keyboard",
                "instrument: brass",
                "instrument: saxophone",
                "vocals: vocal",
                "vocals: instrumental",
                "vocals: female vocal",
                "vocals: male vocal",
                "language: English",
                "language: Spanish",
                "language: French",
                "language: German",
            ],
        ),
        essentia_discogs_embedding_model=resolve_path(os.environ.get("ESSENTIA_DISCOGS_EMBEDDING_MODEL", "models/local_ai/discogs-effnet-embeddings.pb")),
        essentia_discogs_prediction_model=resolve_path(os.environ.get("ESSENTIA_DISCOGS_PREDICTION_MODEL", "models/local_ai/genre_discogs400-discogs-effnet.pb")),
        essentia_discogs_labels=resolve_path(os.environ.get("ESSENTIA_DISCOGS_LABELS", "models/local_ai/discogs-effnet-labels.txt")),
        musicnn_mtg_jamendo_model=resolve_path(os.environ.get("MUSICNN_MTG_JAMENDO_MODEL", "models/local_ai/mtg_jamendo_musicnn.pb")),
        musicnn_mtg_jamendo_labels=resolve_path(os.environ.get("MUSICNN_MTG_JAMENDO_LABELS", "models/local_ai/mtg_jamendo_labels.txt")),
        essentia_extra_heads=_list("ESSENTIA_EXTRA_HEADS", []),
        web_scraping_enabled=_bool("WEB_SCRAPING_ENABLED", True),
        web_max_results=_int("WEB_MAX_RESULTS", 5),
        web_max_queries_per_file=_int("WEB_MAX_QUERIES_PER_FILE", 4),
        web_allowed_domains=_list(
            "WEB_ALLOWED_DOMAINS",
            [
                "tunebat.com",
                "songbpm.com",
                "musicstax.com",
                "songdata.io",
                "getsongbpm.com",
                "getsongkey.com",
                "chosic.com",
                "wikidata.org",
                "wikipedia.org",
                "musicbrainz.org",
                "discogs.com",
                "last.fm",
                "deezer.com",
                "genius.com",
                "allmusic.com",
                "rateyourmusic.com",
                "officialcharts.com",
                "theaudiodb.com",
                "songwhip.com",
            ],
        ),
        web_search_endpoint=os.environ.get("WEB_SEARCH_ENDPOINT", "https://html.duckduckgo.com/html/").strip(),
        # Merge env overrides ON TOP of defaults so a provider added later always
        # has a sane weight/rate even if the user's .env predates it.
        provider_weights={**default_weights, **_mapping("PROVIDER_WEIGHTS", {})},
        rate_limits={**default_rates, **_mapping("RATE_LIMITS", {})},
        acoustid_api_key=os.environ.get("ACOUSTID_API_KEY", "").strip(),
        fpcalc_path=os.environ.get("FPCALC_PATH", "").strip(),
        spotify_client_id=os.environ.get("SPOTIFY_CLIENT_ID", "").strip(),
        spotify_client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip(),
        lastfm_api_key=os.environ.get("LASTFM_API_KEY", "").strip(),
        discogs_token=os.environ.get("DISCOGS_TOKEN", "").strip(),
        discogs_consumer_key=os.environ.get("DISCOGS_CONSUMER_KEY", "").strip(),
        discogs_consumer_secret=os.environ.get("DISCOGS_CONSUMER_SECRET", "").strip(),
        genius_client_id=os.environ.get("GENIUS_CLIENT_ID", "").strip(),
        genius_client_secret=os.environ.get("GENIUS_CLIENT_SECRET", "").strip(),
        genius_access_token=os.environ.get("GENIUS_ACCESS_TOKEN", "").strip(),
        sonoteller_rapidapi_key=os.environ.get("SONOTELLER_RAPIDAPI_KEY", "").strip(),
        sonoteller_rapidapi_host=os.environ.get("SONOTELLER_RAPIDAPI_HOST", "sonoteller-ai1.p.rapidapi.com").strip(),
        sonoteller_base_url=os.environ.get("SONOTELLER_BASE_URL", "https://sonoteller-ai1.p.rapidapi.com").strip().rstrip("/"),
        sonoteller_analyze_endpoint=os.environ.get("SONOTELLER_ANALYZE_ENDPOINT", "/music").strip(),
        sonoteller_input_mode=os.environ.get("SONOTELLER_INPUT_MODE", "url").strip().lower(),
        sonoteller_file_url_base=os.environ.get("SONOTELLER_FILE_URL_BASE", "").strip().rstrip("/"),
    )
    _validate_settings(settings)
    return settings
