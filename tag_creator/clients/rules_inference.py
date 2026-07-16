from __future__ import annotations

from ..models import MediaFile, ProviderResult
from .base import ProviderClient


GENRE_RULES = {
    "house": {"mood": "upbeat", "energy": "high", "danceability": "high", "occasion": "retail energy"},
    "deep house": {"mood": "smooth", "energy": "medium", "danceability": "high", "occasion": "background retail"},
    "tech house": {"mood": "driving", "energy": "high", "danceability": "high", "occasion": "busy store"},
    "dance": {"mood": "upbeat", "energy": "high", "danceability": "high", "occasion": "busy store"},
    "edm": {"mood": "energetic", "energy": "high", "danceability": "high", "occasion": "peak hours"},
    "electro": {"mood": "energetic", "energy": "high", "danceability": "high"},
    "pop": {"mood": "mainstream", "energy": "medium", "danceability": "medium"},
    "dance pop": {"mood": "upbeat", "energy": "medium-high", "danceability": "high"},
    "synthpop": {"mood": "bright", "energy": "medium", "danceability": "medium-high"},
    "r&b": {"mood": "smooth", "energy": "medium-low", "danceability": "medium"},
    "soul": {"mood": "warm", "energy": "medium-low", "occasion": "premium atmosphere"},
    "hip hop": {"mood": "confident", "energy": "medium-high", "danceability": "medium-high"},
    "rock": {"mood": "energetic", "energy": "medium-high"},
    "indie": {"mood": "fresh", "energy": "medium", "occasion": "modern retail"},
    "jazz": {"mood": "relaxed", "energy": "low-medium", "occasion": "calm store"},
    "classical": {"mood": "calm", "energy": "low", "occasion": "premium atmosphere"},
    "christmas": {"mood": "festive", "season": "christmas", "occasion": "holiday"},
    "acoustic": {"mood": "warm", "energy": "low-medium", "occasion": "soft background"},
    "ambient": {"mood": "calm", "energy": "low", "weather": "rainy"},
    "latin": {"mood": "sunny", "energy": "medium-high", "danceability": "high", "weather": "sunny"},
    "reggaeton": {"mood": "party", "energy": "high", "danceability": "high"},
    "afro": {"mood": "warm", "energy": "medium-high", "danceability": "high"},
}


class RulesInferenceClient(ProviderClient):
    provider_name = "rules_inference"

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        genre_text = " ".join(
            item
            for item in [
                media.tags.get("genre", ""),
                media.tags.get("subgenre", ""),
                media.tags.get("album", ""),
                media.tags.get("title", ""),
            ]
            if item
        ).lower()
        fields: dict[str, str] = {}
        matched_rules: list[str] = []
        for keyword, rule_fields in GENRE_RULES.items():
            if keyword in genre_text:
                matched_rules.append(keyword)
                for field, value in rule_fields.items():
                    fields.setdefault(field, value)

        bpm = media.tags.get("bpm", "")
        if bpm.isdigit():
            bpm_value = int(bpm)
            fields["bpm"] = bpm
            if bpm_value >= 125:
                fields.setdefault("energy", "high")
                fields.setdefault("mood", "energetic")
            elif bpm_value <= 80:
                fields.setdefault("energy", "low")
                fields.setdefault("mood", "calm")

        if not fields:
            return None
        return ProviderResult(
            "rules_inference",
            0.88,
            fields,
            notes="playlist taxonomy inference from verified/free/local tags; suitable for CSV enrichment, lower priority than direct catalog facts",
            raw={"matched_rules": matched_rules},
        )
