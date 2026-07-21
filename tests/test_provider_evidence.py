from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tag_creator.clients.discogs import DiscogsClient
from tag_creator.clients.local_ai_audio import LocalAIAudioClient
from tag_creator.clients.musicbrainz import MusicBrainzClient
from tag_creator.clients.web_discovery import WebDiscoveryClient
from tag_creator.models import MediaFile
from tag_creator.rate_limit import RateLimiter


def _media(path: Path) -> MediaFile:
    return MediaFile(
        path=path,
        extension=".mp3",
        size_bytes=1,
        mtime=1.0,
        tags={"title": "Evidence Song", "artist": "Actual Artist", "album": "Evidence Album"},
    )


def test_musicbrainz_rich_lookup_adds_only_catalog_relationship_data(tmp_path: Path):
    client = MusicBrainzClient(Mock(), RateLimiter({}))

    def response(url: str, **_kwargs):
        if url.endswith("/recording"):
            return {
                "recordings": [
                    {
                        "id": "recording-id",
                        "score": 100,
                        "title": "Evidence Song",
                        "artist-credit": [{"name": "Actual Artist", "joinphrase": ""}],
                    }
                ]
            }
        if url.endswith("/recording/recording-id"):
            return {
                "id": "recording-id",
                "title": "Evidence Song",
                "artist-credit": [{"name": "Actual Artist", "joinphrase": ""}],
                "isrcs": ["GBABC2400001"],
                "genres": [{"name": "Electronic", "count": 8}],
                "releases": [
                    {
                        "id": "release-id",
                        "title": "Evidence Album",
                        "date": "2024-03-10",
                        "status": "Official",
                    }
                ],
                "relations": [{"type": "performance", "work": {"id": "work-id"}}],
            }
        if url.endswith("/release/release-id"):
            return {
                "title": "Evidence Album",
                "date": "2024-03-10",
                "label-info": [{"label": {"name": "Actual Records"}, "catalog-number": "AR-101"}],
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {"number": "4", "recording": {"id": "recording-id"}},
                        ],
                    }
                ],
            }
        if url.endswith("/work/work-id"):
            return {
                "relations": [
                    {"type": "composer", "artist": {"name": "Real Composer"}},
                    {"type": "lyricist", "artist": {"name": "Real Lyricist"}},
                ]
            }
        raise AssertionError(url)

    client.get_json = response
    result = client.enrich(_media(tmp_path / "song.mp3"))

    assert result is not None
    assert result.fields["isrc"] == "GBABC2400001"
    assert result.fields["label"] == "Actual Records"
    assert result.fields["catalog_number"] == "AR-101"
    assert result.fields["track_number"] == "4"
    assert result.fields["disc_number"] == "1"
    assert result.fields["composer"] == "Real Composer"
    assert result.fields["comment"] == "Lyricist: Real Lyricist"


def test_discogs_release_detail_adds_real_style_label_track_and_composer(make_settings):
    settings = make_settings(discogs_token="token")
    client = DiscogsClient(Mock(), RateLimiter({}), settings)
    client.get_json = Mock(
        return_value={
            "title": "Evidence Album",
            "year": 2024,
            "released": "2024-03-10",
            "genres": ["Electronic"],
            "styles": ["Deep House"],
            "labels": [{"name": "Actual Records", "catno": "AR-101"}],
            "tracklist": [
                {
                    "position": "4",
                    "title": "Evidence Song",
                    "extraartists": [{"name": "Real Composer", "role": "Written-By"}],
                }
            ],
        }
    )

    fields, detail = client._release_detail_fields(101, "Evidence Song")

    assert detail
    assert fields["subgenre"] == "Deep House"
    assert fields["label"] == "Actual Records"
    assert fields["catalog_number"] == "AR-101"
    assert fields["track_number"] == "4"
    assert fields["composer"] == "Real Composer"


def test_web_discovery_requires_page_track_and_artist_identity(tmp_path: Path, make_settings):
    settings = make_settings(
        web_scraping_enabled=True,
        web_allowed_domains=["musicbrainz.org"],
        web_search_endpoint="https://search.invalid",
    )
    client = WebDiscoveryClient(Mock(), RateLimiter({}), settings)
    client._search_many_urls = Mock(return_value=["https://musicbrainz.org/release/example"])
    client._robots_allowed = Mock(return_value=True)
    client.session.get = Mock(
        return_value=SimpleNamespace(
            ok=True,
            headers={"content-type": "text/html"},
            text=(
                '<script type="application/ld+json">'
                '{"@type":"MusicRecording","name":"Evidence Song",'
                '"byArtist":{"name":"Actual Artist"},"inAlbum":{"name":"Evidence Album"},'
                '"datePublished":"2024-03-10","isrcCode":"GBABC2400001"}'
                "</script>"
            ),
        )
    )

    result = client.enrich(_media(tmp_path / "song.mp3"))

    assert result is not None
    assert result.fields["isrc"] == "GBABC2400001"
    assert result.raw["field_evidence"]["isrc"]["domains"] == ["musicbrainz.org"]


def test_web_discovery_rejects_metadata_page_without_artist_identity(tmp_path: Path, make_settings):
    settings = make_settings(
        web_scraping_enabled=True,
        web_allowed_domains=["musicbrainz.org"],
        web_search_endpoint="https://search.invalid",
    )
    client = WebDiscoveryClient(Mock(), RateLimiter({}), settings)
    client._search_many_urls = Mock(return_value=["https://musicbrainz.org/release/wrong"])
    client._robots_allowed = Mock(return_value=True)
    client.session.get = Mock(
        return_value=SimpleNamespace(
            ok=True,
            headers={"content-type": "text/html"},
            text='<meta name="title" content="Evidence Song"><div>ISRC: GBABC2400001</div>',
        )
    )

    result = client.enrich(_media(tmp_path / "song.mp3"))

    assert result is not None
    assert result.fields == {}


def test_local_ai_confidence_tracks_audio_evidence_strength():
    weak = LocalAIAudioClient._evidence_confidence(
        {"tags": [{"label": "pop", "score": 0.20}]},
        {"genre": "Pop"},
    )
    strong = LocalAIAudioClient._evidence_confidence(
        {"tags": [{"label": "pop", "score": 0.90}]},
        {"genre": "Pop"},
    )
    measured = LocalAIAudioClient._evidence_confidence(
        {"features": {"bpm": 124, "key": "A"}},
        {"bpm": "124", "key": "A minor"},
    )

    assert weak < strong
    assert measured >= 0.86
    assert strong <= 0.92
