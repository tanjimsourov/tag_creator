from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tag_creator.clients.artist_search import ArtistSearchClient
from tag_creator.clients.deezer import DeezerClient
from tag_creator.clients.discogs import DiscogsClient
from tag_creator.clients.itunes import ITunesClient
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


def test_catalog_clients_find_artist_from_title_only(tmp_path: Path):
    media = MediaFile(
        path=tmp_path / "Is There Someone Else.mp4",
        extension=".mp4",
        size_bytes=1,
        mtime=1.0,
        tags={"title": "Is There Someone Else?", "artist": ""},
    )

    itunes = ITunesClient(Mock(), RateLimiter({}))
    itunes.get_json = Mock(
        return_value={
            "results": [
                {
                    "trackName": "Is There Someone Else?",
                    "artistName": "The Weeknd",
                    "collectionName": "Dawn FM",
                    "primaryGenreName": "R&B/Soul",
                    "releaseDate": "2022-01-07T12:00:00Z",
                    "trackViewUrl": "https://music.apple.com/example",
                    "trackId": 1,
                }
            ]
        }
    )

    deezer = DeezerClient(Mock(), RateLimiter({}))

    def deezer_response(url: str, **_kwargs):
        if url.endswith("/search"):
            return {
                "data": [
                    {
                        "id": 2,
                        "title": "Is There Someone Else?",
                        "artist": {"name": "The Weeknd"},
                        "album": {"id": 3, "title": "Dawn FM"},
                        "link": "https://www.deezer.com/track/2",
                    }
                ]
            }
        if url.endswith("/album/3"):
            return {
                "artist": {"name": "The Weeknd"},
                "release_date": "2022-01-07",
                "genres": {"data": [{"name": "R&B"}]},
            }
        raise AssertionError(url)

    deezer.get_json = deezer_response

    musicbrainz = MusicBrainzClient(Mock(), RateLimiter({}))

    def musicbrainz_response(url: str, **_kwargs):
        if url.endswith("/recording"):
            return {
                "recordings": [
                    {
                        "id": "recording-id",
                        "score": 100,
                        "title": "Is There Someone Else?",
                        "artist-credit": [{"name": "The Weeknd", "joinphrase": ""}],
                        "first-release-date": "2022-01-07",
                    }
                ]
            }
        if url.endswith("/recording/recording-id"):
            return {
                "id": "recording-id",
                "title": "Is There Someone Else?",
                "artist-credit": [{"name": "The Weeknd", "joinphrase": ""}],
                "first-release-date": "2022-01-07",
                "releases": [],
                "relations": [],
            }
        raise AssertionError(url)

    musicbrainz.get_json = musicbrainz_response

    assert itunes.enrich(media).fields["artist"] == "The Weeknd"
    assert deezer.enrich(media).fields["artist"] == "The Weeknd"
    assert musicbrainz.enrich(media).fields["artist"] == "The Weeknd"


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


def test_artist_search_extracts_google_answer_artist(tmp_path: Path, make_settings):
    settings = make_settings(
        artist_search_enabled=True,
        artist_search_endpoint="https://search.invalid",
        artist_search_min_confidence=0.86,
    )
    client = ArtistSearchClient(Mock(), RateLimiter({}), settings)
    client.session.get = Mock(
        return_value=SimpleNamespace(
            ok=True,
            text=(
                '<div class="g">The artist for the song "Is There Someone Else?" '
                "is The Weeknd. Song Details Artist: The Weeknd Album: Dawn FM (2022)</div>"
            ),
        )
    )
    media = MediaFile(
        path=tmp_path / "Is There Someone Else.mp4",
        extension=".mp4",
        size_bytes=1,
        mtime=1.0,
        tags={"title": "Is There Someone Else?", "artist": ""},
    )

    result = client.enrich(media)

    assert result is not None
    assert result.fields["artist"] == "The Weeknd"
    assert result.confidence >= 0.86


def test_artist_search_extracts_karaoke_result_artist(tmp_path: Path, make_settings):
    settings = make_settings(
        artist_search_enabled=True,
        artist_search_endpoint="https://search.invalid",
        artist_search_min_confidence=0.86,
    )
    client = ArtistSearchClient(Mock(), RateLimiter({}), settings)
    client.session.get = Mock(
        return_value=SimpleNamespace(
            ok=True,
            text=(
                '<div class="g">Karaoke Is There Someone Else? - The Weeknd '
                "[No Guide Melody] YouTube EdKara</div>"
            ),
        )
    )
    media = MediaFile(
        path=tmp_path / "karaoke.mp4",
        extension=".mp4",
        size_bytes=1,
        mtime=1.0,
        tags={"title": "Is There Someone Else?", "artist": "unknown"},
    )

    result = client.enrich(media)

    assert result is not None
    assert result.fields["artist"] == "The Weeknd"


def test_artist_search_extracts_artist_colon_song_context():
    block = '4*TOWN: Recorded the hit song "Nobody Like U" written for Turning Red. YouTube Spotify'

    candidates = ArtistSearchClient._artists_from_block(block, "Nobody Like U")

    assert ("4*TOWN", 0.91, "artist_colon_title_context") in candidates


def test_artist_search_rejects_ambiguous_multi_artist_title(tmp_path: Path, make_settings):
    settings = make_settings(
        artist_search_enabled=True,
        artist_search_endpoint="https://search.invalid",
        artist_search_min_confidence=0.86,
    )
    client = ArtistSearchClient(Mock(), RateLimiter({}), settings)
    client.session.get = Mock(
        return_value=SimpleNamespace(
            ok=True,
            text=(
                "<div>"
                'Jordan Ramble: Released a song titled "Nobody Like You". '
                'Yung Bleu: Released a track named "Nobody Like You". '
                'Little Mix: Has a song titled "Nobody Like You".'
                "</div>"
            ),
        )
    )
    media = MediaFile(
        path=tmp_path / "Nobody Like You.mp4",
        extension=".mp4",
        size_bytes=1,
        mtime=1.0,
        tags={"title": "Nobody Like You", "artist": ""},
    )

    result = client.enrich(media)

    assert result is not None
    assert result.fields == {}
    assert "below confidence threshold" in result.notes


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
