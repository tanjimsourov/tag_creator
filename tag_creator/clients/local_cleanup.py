from __future__ import annotations

import re

from ..models import MediaFile, ProviderResult
from .base import ProviderClient


NOISE_WORDS = (
    "official music video",
    "official video",
    "official audio",
    "music video",
    "lyric video",
    "lyrics",
    "visualizer",
    "audio",
    "video",
)


def clean_track_text(value: str) -> str:
    cleaned = re.sub(r"\[[^\]]+\]|\([^\)]+\)", " ", value)
    for word in NOISE_WORDS:
        cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -_")


def split_artist_title(value: str) -> tuple[str, str]:
    cleaned = clean_track_text(value)
    parts = re.split(r"\s+-\s+", cleaned, maxsplit=1)
    if len(parts) != 2:
        return "", cleaned
    artist, title = parts[0].strip(), parts[1].strip()
    if not artist or not title:
        return "", cleaned
    return artist, title


class LocalCleanupClient(ProviderClient):
    provider_name = "local_cleanup"

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        stem_artist, stem_title = split_artist_title(media.path.stem)
        tag_title = media.tags.get("title", "")
        tag_artist = media.tags.get("artist", "")
        title_artist, title_title = split_artist_title(tag_title)

        fields: dict[str, str] = {}
        notes: list[str] = []

        if stem_artist and stem_title:
            fields["artist"] = stem_artist
            fields["title"] = stem_title
            notes.append("parsed artist/title from filename")
        elif title_artist and title_title and not tag_artist:
            fields["artist"] = title_artist
            fields["title"] = title_title
            notes.append("parsed artist/title from embedded title")
        elif tag_title:
            cleaned_title = clean_track_text(tag_title)
            if cleaned_title and cleaned_title != tag_title:
                fields["title"] = cleaned_title
                notes.append("removed video/noise words from embedded title")

        if tag_artist and not fields.get("artist"):
            fields["artist"] = clean_track_text(tag_artist)

        if not fields:
            return None
        return ProviderResult(
            "local_cleanup",
            0.72,
            fields,
            notes="; ".join(notes) or "local metadata cleanup",
        )
