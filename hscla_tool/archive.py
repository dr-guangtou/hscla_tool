"""Direct file-tree downloads from the HSCLA archive.

For per-patch products (coadd FITS, forced catalog FITS, etc.) the
HSCLA archive exposes a simple Apache-style directory tree at
``https://hscla.mtk.nao.ac.jp/archive/files/la2020/``, protected by
HTTP Basic auth. We hit those URLs directly rather than going through
the cutout / catalog services.

Layout confirmed on 2026-05-12::

    /archive/files/la2020/deepCoadd-results/<filter>/<tract>/<patch>/<kind>-<filter>-<tract>-<patch>.fits

Nine ``<kind>`` values per patch:

- ``calexp``        the coadd image (FITS, ~100 MB)
- ``forced_src``    forced-photometry source table
- ``meas``          unforced measurement source table
- ``deblendedFlux`` deblended-flux measurements
- ``det``           detection catalog
- ``det_bkgd``      detection-step background
- ``ran``           per-patch random points
- ``srcMatch``      source-matching table
- ``srcMatchFull``  source-matching table (full version)

The server advertises ``Accept-Ranges: bytes``, so we can resume
partial downloads with a ``Range: bytes=<offset>-`` header.
``download_patch_file(...)`` does that automatically when a temp file
from a previous interrupted run is found.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import requests

from hscla_tool import config

LOGGER = logging.getLogger(__name__)

DEFAULT_RELEASE = "la2020"
DEFAULT_BASE_URL = "https://hscla.mtk.nao.ac.jp/archive/files/la2020"
DEFAULT_HTTP_TIMEOUT = 600.0  # one big coadd FITS can take a while

# The nine per-patch file kinds advertised by the HSCLA file tree.
SUPPORTED_KINDS: tuple[str, ...] = (
    "calexp",
    "forced_src",
    "meas",
    "deblendedFlux",
    "det",
    "det_bkgd",
    "ran",
    "srcMatch",
    "srcMatchFull",
)

# Block size for streaming downloads.
_CHUNK_BYTES = 1024 * 1024  # 1 MiB


class ArchiveError(RuntimeError):
    """Raised when an HSCLA file-tree download fails or has bad arguments."""


@dataclass(frozen=True)
class ArchiveFile:
    """Metadata about one downloaded archive file."""

    url: str
    path: Path
    kind: str
    band: str
    tract: int
    patch: str
    bytes: int


# --------------------------------------------------------------------------- #
# URL + path helpers
# --------------------------------------------------------------------------- #


def patch_file_url(
    *,
    tract: int,
    patch: str,
    band: str,
    kind: str,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Build the archive URL for one per-patch file.

    ``patch`` is the HSC patch-cell coordinate as a string ``"x,y"``
    (e.g., ``"1,6"``). The comma is URL-encoded as ``%2C``.
    """

    if kind not in SUPPORTED_KINDS:
        raise ArchiveError(
            f"unknown HSCLA per-patch file kind {kind!r}. "
            f"Supported: {SUPPORTED_KINDS}"
        )
    if not _is_patch_string(patch):
        raise ArchiveError(f"patch must be 'x,y' (got {patch!r})")
    safe_patch = urllib.parse.quote(patch, safe="")
    name = f"{kind}-{band}-{tract}-{patch}.fits"
    safe_name = urllib.parse.quote(name, safe="")
    base = base_url.rstrip("/")
    return f"{base}/deepCoadd-results/{band}/{tract}/{safe_patch}/{safe_name}"


def patch_file_relpath(
    *,
    tract: int,
    patch: str,
    band: str,
    kind: str,
) -> Path:
    """Local-cache relative path for one per-patch file.

    Mirrors the archive layout under ``<cache>/archive/<band>/<tract>/<patch>/``.
    """

    return Path("archive") / band / str(tract) / patch / f"{kind}-{band}-{tract}-{patch}.fits"


def _is_patch_string(patch: str) -> bool:
    if not isinstance(patch, str) or "," not in patch:
        return False
    parts = patch.split(",")
    return len(parts) == 2 and all(p.strip().isdigit() for p in parts)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


class HscLaArchiveClient:
    """Thin HTTP client for the HSCLA per-patch file tree."""

    def __init__(
        self,
        credentials: config.Credentials | None = None,
        *,
        session: requests.Session | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.credentials = credentials or config.load_credentials()
        self._session = session or requests.Session()
        self._base_url = base_url
        self.timeout = timeout
        token = base64.standard_b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode()
        ).decode("ascii")
        self._auth_header = f"Basic {token}"

    def download_patch_file(
        self,
        tract: int,
        patch: str,
        band: str,
        kind: str,
        *,
        dest: Path | None = None,
        force: bool = False,
        resume: bool = True,
    ) -> ArchiveFile:
        """Fetch one ``<kind>`` file for a given patch and band.

        Stored on disk under ``${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/``
        unless ``dest`` is given. Skipped silently when the destination
        already exists, unless ``force=True``. If an interrupted ``.tmp``
        file is present, the download resumes with a ``Range:`` request
        (turn this off with ``resume=False``).
        """

        url = patch_file_url(tract=tract, patch=patch, band=band, kind=kind,
                              base_url=self._base_url)
        target: Path
        if dest is None:
            target = config.cache_dir() / patch_file_relpath(
                tract=tract, patch=patch, band=band, kind=kind,
            )
        else:
            target = Path(dest)

        if target.is_file() and not force:
            LOGGER.info("archive cache hit: %s", target)
            return ArchiveFile(
                url=url, path=target, kind=kind, band=band,
                tract=tract, patch=patch, bytes=target.stat().st_size,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        offset = tmp.stat().st_size if (resume and tmp.is_file()) else 0
        if offset:
            LOGGER.info("resuming %s at byte %d", url, offset)

        headers = {"Authorization": self._auth_header}
        if offset:
            headers["Range"] = f"bytes={offset}-"

        with self._session.get(url, headers=headers, timeout=self.timeout,
                               stream=True) as resp:
            if resp.status_code == 404:
                raise ArchiveError(f"archive file not found: {url}")
            if resp.status_code >= 400:
                raise ArchiveError(
                    f"archive GET failed: {resp.status_code} {resp.reason} — "
                    f"{resp.text.strip()[:300]}"
                )
            # 206 = partial content; 200 = full content (server might ignore Range).
            mode = "ab" if (offset and resp.status_code == 206) else "wb"
            with tmp.open(mode) as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK_BYTES):
                    if chunk:
                        fh.write(chunk)
        tmp.replace(target)
        size = target.stat().st_size
        LOGGER.info("downloaded %s (%d bytes) -> %s", url, size, target)
        return ArchiveFile(
            url=url, path=target, kind=kind, band=band,
            tract=tract, patch=patch, bytes=size,
        )

    def list_patch_files(
        self,
        tract: int,
        patch: str,
        band: str,
    ) -> tuple[str, ...]:
        """Return the file names HSCLA exposes for one (band, tract, patch).

        Talks to the directory index. Useful when you want to know
        which `kind` values are actually present (the answer should be
        :data:`SUPPORTED_KINDS`, but we trust the server, not the
        constant).
        """

        safe_patch = urllib.parse.quote(patch, safe="")
        url = (
            f"{self._base_url.rstrip('/')}/deepCoadd-results/"
            f"{band}/{tract}/{safe_patch}/"
        )
        resp = self._session.get(
            url, headers={"Authorization": self._auth_header}, timeout=self.timeout,
        )
        if resp.status_code == 404:
            raise ArchiveError(f"patch dir not found: {url}")
        if resp.status_code >= 400:
            raise ArchiveError(
                f"list patch dir failed: {resp.status_code} {resp.reason}"
            )
        import re
        hrefs = re.findall(r'<a href="([^"]+)"', resp.text)
        return tuple(urllib.parse.unquote(h) for h in hrefs if h.endswith(".fits"))


# --------------------------------------------------------------------------- #
# Module-level shortcuts
# --------------------------------------------------------------------------- #


def download_patch_file(
    tract: int,
    patch: str,
    band: str,
    kind: str,
    **kwargs,
) -> ArchiveFile:
    """One-line shortcut: build a default client and download one patch file."""

    return HscLaArchiveClient().download_patch_file(
        tract, patch, band, kind, **kwargs
    )


def download_coadd_image(
    tract: int,
    patch: str,
    band: str,
    **kwargs,
) -> ArchiveFile:
    """Convenience: download the per-patch coadd image (the ``calexp`` FITS)."""

    return download_patch_file(tract, patch, band, "calexp", **kwargs)


def download_forced_catalog(
    tract: int,
    patch: str,
    band: str,
    **kwargs,
) -> ArchiveFile:
    """Convenience: download the per-patch forced-photometry catalog."""

    return download_patch_file(tract, patch, band, "forced_src", **kwargs)


# --------------------------------------------------------------------------- #
# Helpers exposed for testability
# --------------------------------------------------------------------------- #


def supported_kinds() -> Iterable[str]:
    """Return the per-patch file kinds the archive exposes."""

    return SUPPORTED_KINDS
