"""Tests for `hscla_tool.archive`. Offline + one tiny live HEAD check."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from hscla_tool import archive, config

# --------------------------------------------------------------------------- #
# URL + path helpers
# --------------------------------------------------------------------------- #


def test_patch_file_url_encodes_patch_comma() -> None:
    url = archive.patch_file_url(
        tract=15548, patch="1,6", band="HSC-I", kind="calexp",
    )
    assert url.startswith(archive.DEFAULT_BASE_URL)
    assert "/deepCoadd-results/HSC-I/15548/1%2C6/" in url
    assert url.endswith("calexp-HSC-I-15548-1%2C6.fits")


def test_patch_file_url_rejects_unknown_kind() -> None:
    with pytest.raises(archive.ArchiveError, match="unknown HSCLA per-patch file"):
        archive.patch_file_url(tract=1, patch="0,0", band="HSC-I", kind="nope")


def test_patch_file_url_rejects_bad_patch_format() -> None:
    with pytest.raises(archive.ArchiveError, match="patch must be"):
        archive.patch_file_url(tract=1, patch="0-0", band="HSC-I", kind="calexp")


def test_patch_file_relpath_mirrors_archive_layout() -> None:
    rel = archive.patch_file_relpath(
        tract=15548, patch="1,6", band="HSC-I", kind="calexp",
    )
    assert rel == Path("archive/HSC-I/15548/1,6/calexp-HSC-I-15548-1,6.fits")


def test_supported_kinds_matches_known_set() -> None:
    expected = {
        "calexp", "forced_src", "meas", "deblendedFlux",
        "det", "det_bkgd", "ran", "srcMatch", "srcMatchFull",
    }
    assert set(archive.supported_kinds()) == expected


# --------------------------------------------------------------------------- #
# Fake HTTP transport
# --------------------------------------------------------------------------- #


class _FakeStreamResponse:
    def __init__(self, status_code: int, content: bytes, reason: str = "OK",
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("latin-1", errors="replace")
        self.reason = reason
        self.headers = headers or {"content-type": "application/octet-stream"}

    def __enter__(self) -> _FakeStreamResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def iter_content(self, chunk_size: int = 0):
        yield self.content


class _FakeSession:
    def __init__(self, get_response: _FakeStreamResponse) -> None:
        self._get = get_response
        self.gets: list[dict[str, Any]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float,
            stream: bool = False) -> _FakeStreamResponse:
        self.gets.append({"url": url, "headers": dict(headers), "timeout": timeout,
                          "stream": stream})
        return self._get


@pytest.fixture
def fake_creds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HSCLA_USR", "alice@example.com")
    monkeypatch.setenv("HSCLA_PWD", "hunter2")
    return config.load_credentials()


# --------------------------------------------------------------------------- #
# Client behavior
# --------------------------------------------------------------------------- #


def test_download_patch_file_writes_destination(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    payload = b"FITS-like payload" * 200
    session = _FakeSession(_FakeStreamResponse(200, payload))
    client = archive.HscLaArchiveClient(credentials=fake_creds, session=session)
    dest = tmp_path / "calexp.fits"
    out = client.download_patch_file(
        tract=15548, patch="1,6", band="HSC-I", kind="calexp", dest=dest,
    )
    assert out.path == dest
    assert dest.is_file()
    assert dest.read_bytes() == payload
    sent = session.gets[0]
    assert sent["headers"]["Authorization"].startswith("Basic ")
    assert sent["stream"] is True
    assert "calexp-HSC-I-15548-1%2C6.fits" in sent["url"]


def test_download_patch_file_skips_when_cached(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeStreamResponse(200, b"X"))
    client = archive.HscLaArchiveClient(credentials=fake_creds, session=session)
    dest = tmp_path / "cached.fits"
    dest.write_bytes(b"existing-content")
    out = client.download_patch_file(
        tract=1, patch="0,0", band="HSC-I", kind="calexp", dest=dest,
    )
    assert out.bytes == len(b"existing-content")
    assert session.gets == []   # never hit the server


def test_download_patch_file_resumes_with_range_header(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    payload = b"FITSDATA" * 50
    session = _FakeSession(_FakeStreamResponse(
        206, payload,
        headers={"content-type": "application/octet-stream",
                 "content-range": f"bytes 64-{63+len(payload)}/9999"},
    ))
    client = archive.HscLaArchiveClient(credentials=fake_creds, session=session)
    dest = tmp_path / "resumable.fits"
    tmp = dest.with_suffix(".fits.tmp")
    tmp.write_bytes(b"x" * 64)   # 64 bytes already on disk
    out = client.download_patch_file(
        tract=1, patch="0,0", band="HSC-I", kind="calexp", dest=dest,
    )
    sent = session.gets[0]
    assert sent["headers"].get("Range") == "bytes=64-"
    # The original 64 bytes plus the new payload should be on disk now.
    assert out.bytes == 64 + len(payload)


def test_download_patch_file_raises_on_404(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeStreamResponse(404, b"not here", reason="Not Found"))
    client = archive.HscLaArchiveClient(credentials=fake_creds, session=session)
    with pytest.raises(archive.ArchiveError, match="not found"):
        client.download_patch_file(
            tract=1, patch="0,0", band="HSC-I", kind="calexp",
            dest=tmp_path / "x.fits",
        )


def test_list_patch_files_parses_directory_index(
    fake_creds: config.Credentials,
) -> None:
    html = (
        "<html><body>"
        '<a href="../">../</a>'
        '<a href="calexp-HSC-I-15548-1%2C6.fits">calexp-HSC-I-15548-1%2C6.fits</a>'
        '<a href="forced_src-HSC-I-15548-1%2C6.fits">forced_src-HSC-I-15548-1%2C6.fits</a>'
        "</body></html>"
    )
    resp = _FakeStreamResponse(200, html.encode("utf-8"),
                                headers={"content-type": "text/html"})
    session = _FakeSession(resp)
    client = archive.HscLaArchiveClient(credentials=fake_creds, session=session)
    names = client.list_patch_files(tract=15548, patch="1,6", band="HSC-I")
    assert "calexp-HSC-I-15548-1,6.fits" in names
    assert "forced_src-HSC-I-15548-1,6.fits" in names


# --------------------------------------------------------------------------- #
# Live (opt-in) — only a tiny HEAD-style listing, no big downloads.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_list_patch_files_at_perseus_patch() -> None:
    # Perseus LSBG sits in tract 15548, patch 1,6 in HSC-I.
    client = archive.HscLaArchiveClient()
    names = client.list_patch_files(tract=15548, patch="1,6", band="HSC-I")
    # The patch should expose at least the calexp and forced catalogs.
    assert any(n.startswith("calexp-HSC-I-15548-1,6") for n in names)
    assert any(n.startswith("forced_src-HSC-I-15548-1,6") for n in names)
