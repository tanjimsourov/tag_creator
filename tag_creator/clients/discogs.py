from __future__ import annotations

import re

from ..config import Settings
from ..matching import similarity
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs
from .base import ProviderClient


class DiscogsClient(ProviderClient):
    provider_name = "discogs"
    base_url = "https://api.discogs.com"

    def __init__(self, db, rate_limiter, settings: Settings) -> None:
        super().__init__(db, rate_limiter)
        self.token = settings.discogs_token
        self.consumer_key = settings.discogs_consumer_key
        self.consumer_secret = settings.discogs_consumer_secret

    def is_configured(self) -> bool:
        return bool(self.token or (self.consumer_key and self.consumer_secret))

    def _auth_params(self) -> dict[str, str]:
        if self.token:
            return {"token": self.token}
        return {"key": self.consumer_key, "secret": self.consumer_secret}

    @staticmethod
    def _credit_names(credits: list[dict], roles: set[str]) -> str:
        names: list[str] = []
        for credit in credits:
            role = str(credit.get("role", "")).lower()
            if not any(expected in role for expected in roles):
                continue
            name = re.sub(r"\s*\(\d+\)$", "", str(credit.get("name", ""))).strip()
            if name and name not in names:
                names.append(name)
        return ", ".join(names)

    def _release_detail_fields(self, release_id: object, expected_title: str) -> tuple[dict[str, str], dict]:
        if not release_id:
            return {}, {}
        detail = self.get_json(
            f"{self.base_url}/releases/{release_id}",
            params=self._auth_params(),
            headers={"User-Agent": "SMC-Tag-Creator/0.1"},
            cache_key_extra="release-detail",
        ) or {}
        if not detail:
            return {}, {}

        fields: dict[str, str] = {}
        if detail.get("title"):
            fields["album"] = str(detail["title"])
        if detail.get("year"):
            fields["year"] = str(detail["year"])
        if detail.get("released"):
            fields["date"] = str(detail["released"])
        if detail.get("genres"):
            fields["genre"] = ", ".join(str(value) for value in detail["genres"] if value)
        if detail.get("styles"):
            fields["subgenre"] = ", ".join(str(value) for value in detail["styles"] if value)

        labels: list[str] = []
        catalog_numbers: list[str] = []
        for label in detail.get("labels", []):
            name = re.sub(r"\s*\(\d+\)$", "", str(label.get("name", ""))).strip()
            catalog_number = str(label.get("catno", "")).strip()
            if name and name not in labels:
                labels.append(name)
            if catalog_number and catalog_number not in catalog_numbers:
                catalog_numbers.append(catalog_number)
        if labels:
            fields["label"] = ", ".join(labels)
        if catalog_numbers:
            fields["catalog_number"] = ", ".join(catalog_numbers)

        matched_track: dict = {}
        best_track_score = 0.0
        for track in detail.get("tracklist", []):
            score = similarity(expected_title, str(track.get("title", "")))
            if score > best_track_score:
                best_track_score = score
                matched_track = track
        if matched_track and best_track_score >= 0.62:
            position = str(matched_track.get("position", "")).strip()
            if position:
                fields["track_number"] = position
            credits = [*detail.get("extraartists", []), *matched_track.get("extraartists", [])]
            composer = self._credit_names(credits, {"composed by", "composer", "written-by", "written by"})
            if composer:
                fields["composer"] = composer
        return fields, detail

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        album = media.tags.get("album", "")
        candidates = candidate_track_pairs(media, limit=3)
        if not self.is_configured() or not candidates:
            return None
        ranked = []
        for artist, title in candidates:
            if not artist and not album:
                continue
            query = " ".join(item for item in [artist, album or title] if item)
            params = {"q": query, "type": "release", "per_page": 8, **self._auth_params()}
            data = self.get_json(
                f"{self.base_url}/database/search",
                params=params,
                headers={"User-Agent": "SMC-Tag-Creator/0.1"},
                cache_key_extra="release-search",
            )
            for candidate in (data.get("results", []) if data else []):
                candidate_title = candidate.get("title", "")
                candidate_artist = candidate_title.split(" - ", 1)[0] if " - " in candidate_title else ""
                candidate_album = candidate_title.split(" - ", 1)[-1]
                album_score = similarity(album or title, candidate_album)
                artist_score = similarity(artist, candidate_artist) if artist and candidate_artist else 0.0
                if album_score >= 0.55 and artist_score >= 0.55:
                    ranked.append(
                        ((album_score * 0.65) + (artist_score * 0.35), album_score, artist_score, query, title, candidate)
                    )
        if not ranked:
            return ProviderResult("discogs", 0, {}, notes="no match")

        _, album_score, artist_score, query, expected_title, release = sorted(
            ranked, key=lambda item: item[0], reverse=True
        )[0]
        fields = {
            "album": release.get("title", "").split(" - ", 1)[-1],
            "year": str(release.get("year") or ""),
            "genre": ", ".join(release.get("genre", []) or release.get("style", []) or []),
            "label": ", ".join(release.get("label", []) or []),
            "catalog_number": ", ".join(release.get("catno", []) if isinstance(release.get("catno"), list) else [str(release.get("catno", ""))]),
            "cover_art_url": release.get("cover_image", ""),
        }
        detail_fields, detail = self._release_detail_fields(release.get("id"), expected_title)
        fields.update({key: value for key, value in detail_fields.items() if value})
        fields = {key: value for key, value in fields.items() if value}
        return ProviderResult(
            "discogs",
            0.72,
            fields,
            source_url=f"https://www.discogs.com{release.get('uri', '')}",
            raw={"discogs_id": release.get("id", ""), "release_detail": bool(detail)},
            notes=(
                f"database release search + verified release detail; query={query}; "
                f"album_similarity={album_score:.2f}; artist_similarity={artist_score:.2f}"
            ),
        )
