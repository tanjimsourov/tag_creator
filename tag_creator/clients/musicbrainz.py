from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import quote

from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class MusicBrainzClient(ProviderClient):
    provider_name = "musicbrainz"
    base_url = "https://musicbrainz.org/ws/2"

    def __init__(self, db, rate_limiter) -> None:
        super().__init__(db, rate_limiter)
        self.headers = {"User-Agent": "SMC-Tag-Creator/0.1 (metadata-enrichment)"}

    @staticmethod
    def _clean_title(title: str) -> str:
        title = re.sub(r"\[[^\]]*(official|video|audio|lyrics?|visualizer)[^\]]*\]", "", title, flags=re.I)
        title = re.sub(r"\([^\)]*(official|video|audio|lyrics?|visualizer)[^\)]*\)", "", title, flags=re.I)
        return re.sub(r"\s+", " ", title).strip()

    def _search_recordings(self, queries: list[str]) -> list[dict]:
        for query in queries:
            if not query.strip():
                continue
            data = self.get_json(
                f"{self.base_url}/recording",
                params={"fmt": "json", "limit": 5, "query": query},
                headers=self.headers,
                cache_key_extra="recording-search",
            )
            recordings = data.get("recordings", []) if data else []
            if recordings:
                return recordings
        return []

    @staticmethod
    def _norm(value: str) -> str:
        value = value.lower()
        value = re.sub(r"\[[^\]]+\]|\([^\)]+\)", " ", value)
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _similarity(self, left: str, right: str) -> float:
        left_norm = self._norm(self._clean_title(left))
        right_norm = self._norm(self._clean_title(right))
        if not left_norm or not right_norm:
            return 0.0
        if left_norm in right_norm or right_norm in left_norm:
            shorter = min(len(left_norm), len(right_norm))
            longer = max(len(left_norm), len(right_norm))
            return max(0.70, shorter / longer)
        return SequenceMatcher(None, left_norm, right_norm).ratio()

    def _recording_artist(self, recording: dict) -> str:
        if not recording.get("artist-credit"):
            return ""
        return "".join(part.get("name", "") + part.get("joinphrase", "") for part in recording["artist-credit"]).strip()

    @staticmethod
    def _ranked_name(items: list[dict]) -> str:
        if not items:
            return ""
        ranked = sorted(items, key=lambda item: int(item.get("count", 0)), reverse=True)
        return str(ranked[0].get("name", "")).strip()

    def _recording_lookup(self, recording_id: str) -> dict:
        if not recording_id:
            return {}
        return self.get_json(
            f"{self.base_url}/recording/{quote(recording_id)}",
            params={
                "fmt": "json",
                "inc": "artist-credits+releases+release-groups+isrcs+genres+tags+work-rels",
            },
            headers=self.headers,
            cache_key_extra="recording-lookup-rich",
        ) or {}

    def _release_lookup(self, release_id: str) -> dict:
        if not release_id:
            return {}
        return self.get_json(
            f"{self.base_url}/release/{quote(release_id)}",
            params={
                "fmt": "json",
                "inc": "labels+recordings+artist-credits+release-groups",
            },
            headers=self.headers,
            cache_key_extra="release-lookup-rich",
        ) or {}

    def _select_release(self, releases: list[dict], expected_album: str) -> dict:
        if not releases:
            return {}

        def rank(release: dict) -> tuple[float, int, int]:
            title_score = self._similarity(expected_album, str(release.get("title", ""))) if expected_album else 0.70
            official = 1 if str(release.get("status", "")).lower() == "official" else 0
            dated = 1 if release.get("date") else 0
            return title_score, official, dated

        return max(releases, key=rank)

    def _work_contributors(self, recording: dict) -> tuple[str, str]:
        composer_names: list[str] = []
        lyricist_names: list[str] = []
        work_ids: list[str] = []
        for relation in recording.get("relations", []):
            work = relation.get("work") or {}
            work_id = str(work.get("id", "")).strip()
            if work_id and work_id not in work_ids:
                work_ids.append(work_id)

        for work_id in work_ids[:2]:
            work = self.get_json(
                f"{self.base_url}/work/{quote(work_id)}",
                params={"fmt": "json", "inc": "artist-rels"},
                headers=self.headers,
                cache_key_extra="work-contributors",
            ) or {}
            for relation in work.get("relations", []):
                relation_type = str(relation.get("type", "")).lower()
                name = str((relation.get("artist") or {}).get("name", "")).strip()
                if not name:
                    continue
                if relation_type in {"composer", "writer", "music"} and name not in composer_names:
                    composer_names.append(name)
                if relation_type in {"lyricist", "lyrics", "librettist"} and name not in lyricist_names:
                    lyricist_names.append(name)
        return ", ".join(composer_names), ", ".join(lyricist_names)

    @staticmethod
    def _release_fields(release: dict, recording_id: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        if release.get("title"):
            fields["album"] = str(release["title"])
        if release.get("date"):
            fields["date"] = str(release["date"])
            fields["year"] = str(release["date"])[:4]

        label_names: list[str] = []
        catalog_numbers: list[str] = []
        for label_info in release.get("label-info", []):
            label_name = str((label_info.get("label") or {}).get("name", "")).strip()
            catalog_number = str(label_info.get("catalog-number", "")).strip()
            if label_name and label_name not in label_names:
                label_names.append(label_name)
            if catalog_number and catalog_number not in catalog_numbers:
                catalog_numbers.append(catalog_number)
        if label_names:
            fields["label"] = ", ".join(label_names)
        if catalog_numbers:
            fields["catalog_number"] = ", ".join(catalog_numbers)

        for disc_index, medium in enumerate(release.get("media", []), start=1):
            for track in medium.get("tracks", []):
                track_recording_id = str((track.get("recording") or {}).get("id", ""))
                if recording_id and track_recording_id != recording_id:
                    continue
                number = str(track.get("number") or track.get("position") or "").strip()
                disc_number = str(medium.get("position") or disc_index).strip()
                if number:
                    fields["track_number"] = number
                if disc_number:
                    fields["disc_number"] = disc_number
                return fields
        return fields

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        album = media.tags.get("album", "")
        isrc = re.sub(r"[^A-Za-z0-9]", "", media.tags.get("isrc", "")).upper()
        if isrc and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", isrc):
            isrc = ""
        if not title and not isrc:
            return None

        recordings = []
        if isrc:
            data = self.get_json(
                f"{self.base_url}/isrc/{quote(isrc)}",
                # The ISRC resource already returns its recordings. `recordings`
                # is not a valid `inc` value for this endpoint and caused one
                # guaranteed HTTP 400 for every tagged file.
                params={"fmt": "json"},
                headers=self.headers,
                cache_key_extra="isrc",
            )
            recordings = data.get("recordings", []) if data else []

        if not recordings and artist:
            cleaned_title = self._clean_title(title)
            strict_parts = []
            if title:
                strict_parts.append(f'recording:"{title}"')
            if artist:
                strict_parts.append(f'artist:"{artist}"')
            if album:
                strict_parts.append(f'release:"{album}"')
            cleaned_parts = []
            if cleaned_title:
                cleaned_parts.append(f'recording:"{cleaned_title}"')
            if artist:
                cleaned_parts.append(f'artist:"{artist}"')
            loose_query = " ".join(item for item in [cleaned_title or title, artist] if item)
            recordings = self._search_recordings(
                [
                    " AND ".join(strict_parts),
                    " AND ".join(cleaned_parts),
                    loose_query,
                ]
            )

        if not recordings:
            return ProviderResult("musicbrainz", 0, {}, notes="no match")

        ranked = []
        for recording in recordings:
            api_score = int(recording.get("score", 80)) / 100
            title_similarity = self._similarity(title, recording.get("title", ""))
            artist_similarity = self._similarity(artist, self._recording_artist(recording)) if artist else 0.65
            local_score = (title_similarity * 0.60) + (artist_similarity * 0.40)
            combined = (api_score * 0.45) + (local_score * 0.55)
            ranked.append((combined, title_similarity, artist_similarity, api_score, recording))

        combined, title_similarity, artist_similarity, api_score, best = sorted(
            ranked, key=lambda item: item[0], reverse=True
        )[0]
        if title_similarity < 0.45 or (artist and artist_similarity < 0.45):
            return ProviderResult(
                "musicbrainz",
                0,
                {},
                notes=(
                    "rejected low local similarity "
                    f"title={title_similarity:.2f}, artist={artist_similarity:.2f}, api={api_score:.2f}"
                ),
            )

        score = min(0.98, combined)
        recording_id = str(best.get("id", "")).strip()
        rich_recording = self._recording_lookup(recording_id)
        if rich_recording:
            best = {**best, **rich_recording}
        fields: dict[str, str] = {}
        fields["title"] = best.get("title", "")
        if best.get("artist-credit"):
            fields["artist"] = self._recording_artist(best)
            fields["album_artist"] = fields["artist"]
        if best.get("isrcs"):
            fields["isrc"] = best["isrcs"][0]

        genre = self._ranked_name(best.get("genres") or best.get("tags") or [])
        if genre:
            fields["genre"] = genre

        releases = best.get("releases", [])
        release_mbid = ""
        if releases:
            release = self._select_release(releases, album)
            fields["album"] = release.get("title", "")
            if release.get("date"):
                fields["date"] = release["date"]
                fields["year"] = release["date"][:4]
            release_mbid = release.get("id", "")
        elif best.get("first-release-date"):
            fields["date"] = best["first-release-date"]
            fields["year"] = best["first-release-date"][:4]

        if release_mbid:
            fields.update(
                {
                    key: value
                    for key, value in self._release_fields(
                        self._release_lookup(release_mbid), recording_id
                    ).items()
                    if value
                }
            )
        composer, lyricist = self._work_contributors(best)
        if composer:
            fields["composer"] = composer
        if lyricist:
            fields["comment"] = f"Lyricist: {lyricist}"

        fields = {key: value for key, value in fields.items() if value}
        raw = {
            "recording_id": recording_id,
            "release_mbid": release_mbid,
            "rich_lookup": bool(rich_recording),
        }
        source = f"https://musicbrainz.org/recording/{best.get('id')}" if best.get("id") else ""
        return ProviderResult(
            "musicbrainz",
            max(0.40, min(score, 0.98)),
            fields,
            source_url=source,
            raw=raw,
            notes=f"score {score:.2f}, title_similarity {title_similarity:.2f}, artist_similarity {artist_similarity:.2f}",
        )
