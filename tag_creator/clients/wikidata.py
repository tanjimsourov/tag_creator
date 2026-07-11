from __future__ import annotations

from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class WikidataClient(ProviderClient):
    provider_name = "wikidata"
    base_url = "https://www.wikidata.org/w/api.php"

    def __init__(self, store, rate_limiter) -> None:
        super().__init__(store, rate_limiter)
        self.session.headers.update({"User-Agent": "SMC-Tag-Creator/0.1 (metadata enrichment; contact: tanjim@advikon.eu)"})

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        if not title:
            return None
        query = " ".join(item for item in [title, artist] if item)
        data = self.get_json(
            self.base_url,
            params={
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "format": "json",
                "type": "item",
                "limit": 5,
            },
            cache_key_extra="entity-search",
        )
        results = data.get("search", []) if data else []
        if not results:
            return ProviderResult("wikidata", 0, {}, notes="no match")
        item = results[0]
        fields = {
            "analysis_summary": item.get("description", ""),
        }
        return ProviderResult(
            "wikidata",
            0.45,
            {key: value for key, value in fields.items() if value},
            source_url=item.get("concepturi", ""),
            raw={"wikidata_id": item.get("id", "")},
            notes="Wikidata entity search metadata only",
        )
