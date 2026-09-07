from __future__ import annotations

from types import SimpleNamespace

from tag_creator.missing_artist import MissingArtistResolver, repair_rows


class FakeSession:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params["q"])
        return SimpleNamespace(
            status_code=200,
            text=self.html,
            raise_for_status=lambda: None,
        )


def test_repair_keeps_existing_artist_without_search() -> None:
    session = FakeSession("<html></html>")
    resolver = MissingArtistResolver(session=session)
    rows, headers, stats = repair_rows(
        [{"title": "Carmen", "artist": "Olivia Dean", "filename": "Carmen.mp3"}],
        ["title", "artist", "filename"],
        resolver=resolver,
    )

    assert rows == [{"title": "Carmen", "artist": "Olivia Dean", "filename": "Carmen.mp3"}]
    assert headers == ["title", "artist", "filename"]
    assert stats.unchanged == 1
    assert session.calls == []


def test_repair_fills_missing_artist_from_filename_identity() -> None:
    resolver = MissingArtistResolver(search_enabled=False)
    rows, _headers, stats = repair_rows(
        [{"title": "Diet Pepsi", "artist": "", "filename": "Addison Rae - Diet Pepsi.mp3"}],
        ["title", "artist", "filename"],
        resolver=resolver,
    )

    assert rows[0]["artist"] == "Addison Rae"
    assert stats.filled == 1
    assert stats.local_hits == 1


def test_repair_fills_missing_artist_from_google_result() -> None:
    html = """
    <html><body>
      <div class="g">
        <h3>Year of the Cat - song by Al Stewart</h3>
        <span>Year of the Cat is a song by Al Stewart from 1976.</span>
      </div>
    </body></html>
    """
    resolver = MissingArtistResolver(
        session=FakeSession(html),
        min_confidence=0.80,
        max_results=1,
        search_enabled=True,
    )
    rows, _headers, stats = repair_rows(
        [{"title": "Year of the Cat", "artist": "", "filename": "Year of the Cat.mp3"}],
        ["title", "artist", "filename"],
        resolver=resolver,
    )

    assert rows[0]["artist"] == "Al Stewart"
    assert stats.filled == 1
    assert stats.google_hits == 1


def test_repair_removes_rows_with_missing_identity_or_unresolved_artist() -> None:
    resolver = MissingArtistResolver(search_enabled=False)
    rows, _headers, stats = repair_rows(
        [
            {"title": "", "artist": "", "filename": "empty.mp3"},
            {"title": "Unknown Song", "artist": "", "filename": "Unknown Song.mp3"},
        ],
        ["title", "artist", "filename"],
        resolver=resolver,
    )

    assert rows == []
    assert stats.removed_missing_identity == 1
    assert stats.removed_unresolved_artist == 1
