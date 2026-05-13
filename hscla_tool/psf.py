"""Fetch PSF kernels from the HSCLA PSF picker service.

Endpoint: ``https://hscla.mtk.nao.ac.jp/psf/la2020/cgi/getpsf?bulk=on``,
POST with HTTP Basic auth and a multipart-form ``list`` file. Same
auth style as the DAS cutout service, but a different host path, a
different field set (``rerun type filter tract patch ra dec centered``),
and a much smaller payload — one ``PRIMARY`` HDU per FITS, no mask or
variance.

Per-request response shape (confirmed live on 2026-05-12):

- HTTP 200, ``application/x-tar`` with one ``.fits`` member.
- The FITS file has a single ``PRIMARY`` HDU holding a 2-D ``float64``
  PSF kernel normalized so its sum is 1.0. The header uses a pixel-only
  ``LINEAR`` WCS — there is no sky WCS, because a PSF kernel lives in
  pixel space, not on the sky.
- An uncovered (RA, Dec) returns HTTP 200 plus a zero-member TAR
  (~10 KiB of zero padding), exactly like the cutout endpoint. We turn
  that into a typed ``NoCoverageError`` re-used from
  :mod:`hscla_tool.cutout` so callers can write::

      from hscla_tool import cutout
      try:
          ...
      except cutout.NoCoverageError:
          ...

Cache layout matches the cutout module: one FITS per request, keyed by
``sha256(json(...))[:16]`` under ``${HSCLA_TOOL_CACHE}/psfs/``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import requests
from astropy.io import fits
from astropy.wcs import WCS

from hscla_tool import config, db
from hscla_tool.cutout import NoCoverageError  # re-used cross-service "no data here"

LOGGER = logging.getLogger(__name__)

DEFAULT_RELEASE = "la2020"
DEFAULT_KIND: PsfKind = "coadd"
DEFAULT_TRACT = "auto"
DEFAULT_PATCH = "auto"
DEFAULT_HTTP_TIMEOUT = 120.0

PsfKind = Literal["coadd", "calexp"]


class PsfError(RuntimeError):
    """Base class for HSCLA PSF client failures."""


@dataclass(frozen=True)
class Psf:
    """One downloaded HSCLA PSF kernel, with both file path and parsed HDU.

    Attributes
    ----------
    band, ra, dec, kind : str / float
        The request that produced this PSF.
    fits_path : Path
        On-disk single-HDU FITS file (cached).
    hdul : fits.HDUList
        Opened HDU list. Close it with ``.close()`` when you are done.
    psf_hdu : fits.PrimaryHDU
        The single HDU carrying the kernel. Same object as ``hdul[0]``.
    """

    band: str
    ra: float
    dec: float
    kind: str
    fits_path: Path
    hdul: fits.HDUList = field(repr=False)
    psf_hdu: fits.PrimaryHDU = field(repr=False)

    def close(self) -> None:
        """Close the FITS file handle. Safe to call twice."""

        try:
            self.hdul.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def array(self) -> np.ndarray:
        """The 2D PSF kernel as a numpy array."""

        return np.asarray(self.psf_hdu.data)

    def wcs(self) -> WCS:
        """Return the (linear, pixel-space) WCS from the PSF header.

        HSCLA PSF FITS files do not carry a sky WCS; they write a
        linear pixel-coordinate WCS under the alternate key ``A``
        (``CTYPE1A='LINEAR'`` etc.). We try the primary key first and
        fall back to ``A`` so this works for both shapes.
        """

        try:
            return WCS(self.psf_hdu.header)
        except KeyError:
            return WCS(self.psf_hdu.header, key="A")


class HscLaPsfClient:
    """Thin POST client for the HSCLA PSF picker service."""

    def __init__(
        self,
        credentials: config.Credentials | None = None,
        *,
        session: requests.Session | None = None,
        release: str = DEFAULT_RELEASE,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.credentials = credentials or config.load_credentials()
        self.release = release
        self.timeout = timeout
        self._session = session or requests.Session()
        api = db.get_psf_api()
        base = str(api["base_url"]).rstrip("/")
        endpoint = str(api["endpoint"])
        query = str(api.get("query", "")).lstrip("?")
        self._url = f"{base}{endpoint}" + (f"?{query}" if query else "")
        self._field_name = str(api["multipart_field"])
        token = base64.standard_b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode()
        ).decode("ascii")
        self._auth_header = f"Basic {token}"

    def fetch_psf(
        self,
        ra: float,
        dec: float,
        *,
        band: str,
        kind: PsfKind = DEFAULT_KIND,
        tract: int | str = DEFAULT_TRACT,
        patch: str = DEFAULT_PATCH,
        centered: bool = True,
        cache: bool = True,
        cache_dir: Path | None = None,
    ) -> Psf:
        """Fetch one PSF kernel at (ra, dec) in the given band."""

        rerun = self.release
        key = _cache_key(ra, dec, band, kind, tract, patch, centered, rerun)
        root = Path(cache_dir) if cache_dir is not None else config.cache_dir() / "psfs"
        fits_path = root / f"{key}.fits"

        if cache and fits_path.is_file():
            LOGGER.info("psf cache hit: %s", fits_path)
            hdul = fits.open(fits_path)
        else:
            body, boundary = _build_multipart_body(
                rerun=rerun, kind=kind, band=band, tract=tract, patch=patch,
                ra=float(ra), dec=float(dec), centered=centered,
                multipart_field=self._field_name,
            )
            resp = self._session.post(
                self._url,
                data=body,
                headers={
                    "Authorization": self._auth_header,
                    "Content-Type": f'multipart/form-data; boundary="{boundary}"',
                },
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                raise PsfError(
                    f"psf POST failed: {resp.status_code} {resp.reason} — "
                    f"{resp.text.strip()[:300]}"
                )
            fits_bytes = _extract_one_fits(resp.content)
            if fits_bytes is None:
                raise NoCoverageError(
                    f"HSCLA has no {band} {kind} PSF at "
                    f"(RA, Dec) = ({ra:.6f}, {dec:.6f})"
                )
            fits_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = fits_path.with_suffix(".fits.tmp")
            tmp.write_bytes(fits_bytes)
            tmp.replace(fits_path)
            hdul = fits.open(fits_path)

        primary = hdul[0]
        if primary.data is None:
            hdul.close()
            raise PsfError(f"PSF FITS at {fits_path} has empty PRIMARY HDU")
        return Psf(
            band=band,
            ra=float(ra),
            dec=float(dec),
            kind=str(kind),
            fits_path=fits_path,
            hdul=hdul,
            psf_hdu=primary,  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# Module-level shortcut
# --------------------------------------------------------------------------- #


def fetch_psf(
    ra: float,
    dec: float,
    *,
    band: str,
    **kwargs: Any,
) -> Psf:
    """One-line shortcut: construct a client and call `fetch_psf`."""

    return HscLaPsfClient().fetch_psf(ra, dec, band=band, **kwargs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cache_key(
    ra: float,
    dec: float,
    band: str,
    kind: str,
    tract: int | str,
    patch: str,
    centered: bool,
    rerun: str,
) -> str:
    payload = json.dumps(
        {
            "ra": round(float(ra), 12),
            "dec": round(float(dec), 12),
            "band": band,
            "kind": kind,
            "tract": str(tract),
            "patch": str(patch),
            "centered": bool(centered),
            "rerun": rerun,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_multipart_body(
    *,
    rerun: str,
    kind: str,
    band: str,
    tract: int | str,
    patch: str,
    ra: float,
    dec: float,
    centered: bool,
    multipart_field: str,
    boundary: str = "HscLaPsfBoundary",
) -> tuple[bytes, str]:
    tract_str = "auto" if str(tract).lower() == "auto" else str(tract)
    patch_str = "auto" if str(patch).lower() == "auto" else str(patch)
    header = "#? rerun type filter tract patch ra dec centered\n"
    row = (
        f"{rerun} {kind} {band} {tract_str} {patch_str} "
        f"{ra:.16e}deg {dec:.16e}deg "
        f"{'true' if centered else 'false'}\n"
    )
    coord_list = header + row
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{multipart_field}"; '
        f'filename="coordlist.txt"\r\n\r\n'
        + coord_list
        + f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")
    return body, boundary


def _extract_one_fits(tar_bytes: bytes) -> bytes | None:
    """Pull the (single) FITS file out of a PSF TAR response.

    Returns ``None`` when the TAR has zero members — HSCLA's signal for
    "no coverage at this position".
    """

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        for member in tar:
            if member.isfile() and member.name.endswith(".fits"):
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                return handle.read()
    return None
