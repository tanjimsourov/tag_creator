from __future__ import annotations

from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class CoverArtArchiveClient(ProviderClient):
    provider_name = "cover_art_archive"
    base_url = "https://coverartarchive.org"

    def enrich_by_release(self, release_mbid: str) -> ProviderResult | None:
        if not release_mbid:
            return None
        data = self.get_json(f"{self.base_url}/release/{release_mbid}", cache_key_extra="release-art")
        if not data:
            return ProviderResult("cover_art_archive", 0, {}, notes="no cover art")
        images = data.get("images", [])
        if not images:
            return ProviderResult("cover_art_archive", 0, {}, notes="no images")
        front = next((image for image in images if image.get("front")), images[0])
        image_url = front.get("image") or front.get("thumbnails", {}).get("large") or ""
        if not image_url:
            return ProviderResult("cover_art_archive", 0, {}, notes="no usable image URL")
        return ProviderResult(
            "cover_art_archive",
            0.90,
            {"cover_art_url": image_url},
            source_url=f"https://coverartarchive.org/release/{release_mbid}",
            raw={"release_mbid": release_mbid},
            notes="front cover found",
        )

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        return None

