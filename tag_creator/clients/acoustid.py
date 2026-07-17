from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..config import Settings
from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class AcoustIDClient(ProviderClient):
    provider_name = "acoustid"
    base_url = "https://api.acoustid.org/v2/lookup"

    def __init__(self, db, rate_limiter, settings: Settings) -> None:
        super().__init__(db, rate_limiter)
        self.api_key = settings.acoustid_api_key
        configured_fpcalc = settings.fpcalc_path.strip()
        if configured_fpcalc:
            resolved = shutil.which(configured_fpcalc) if not Path(configured_fpcalc).is_file() else configured_fpcalc
            self.fpcalc_path = resolved or ""
        else:
            self.fpcalc_path = shutil.which("fpcalc") or ""

    def is_configured(self) -> bool:
        return bool(self.api_key and self.fpcalc_path)

    def fingerprint(self, path: Path) -> tuple[int, str] | None:
        if not self.fpcalc_path:
            return None
        completed = subprocess.run(
            [self.fpcalc_path, "-json", str(path)],
            text=True,
            capture_output=True,
            timeout=90,
        )
        if completed.returncode != 0:
            return None
        data = json.loads(completed.stdout)
        return int(data.get("duration") or 0), data.get("fingerprint", "")

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        if not self.is_configured():
            return None
        fp = self.fingerprint(media.path)
        if not fp:
            return ProviderResult("acoustid", 0, {}, notes="fingerprint failed")
        duration, fingerprint = fp
        if not fingerprint:
            return ProviderResult("acoustid", 0, {}, notes="no fingerprint")
        data = self.get_json(
            self.base_url,
            params={
                "client": self.api_key,
                "duration": duration,
                "fingerprint": fingerprint,
                "meta": "recordings+releasegroups+releases+tracks+compress",
            },
            cache_key_extra="lookup",
        )
        results = data.get("results", []) if data else []
        if not results:
            return ProviderResult("acoustid", 0, {}, notes="no match")
        best = max(results, key=lambda item: float(item.get("score", 0)))
        recordings = best.get("recordings", [])
        fields: dict[str, str] = {}
        release_mbid = ""
        if recordings:
            recording = recordings[0]
            fields["title"] = recording.get("title", "")
            if recording.get("artists"):
                fields["artist"] = ", ".join(artist.get("name", "") for artist in recording.get("artists", []))
            if recording.get("releases"):
                release = recording["releases"][0]
                fields["album"] = release.get("title", "")
                if release.get("date"):
                    fields["date"] = release["date"].get("year", "")
                    fields["year"] = str(release["date"].get("year", ""))
                release_mbid = release.get("id", "")
        fields = {key: str(value) for key, value in fields.items() if value}
        return ProviderResult(
            "acoustid",
            min(0.99, float(best.get("score", 0))),
            fields,
            source_url="https://acoustid.org",
            raw={"release_mbid": release_mbid},
            notes="audio fingerprint",
        )
