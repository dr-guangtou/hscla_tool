"""Fetch FITS cutouts from the HSCLA DAS image service.

The cutout service is its own little world inside HSCLA:

- Different host path: ``https://hscla.mtk.nao.ac.jp/das_cutout/la2020``,
  with the actual handler at ``/cgi-bin/cutout`` (POST).
- Different authentication: HTTP Basic Auth, *not* the
  ``LAAUTH_SESSION`` cookie used by the SQL catalog API.
- Bulk-first wire format: every POST uploads a multipart form-data
  whose ``list`` part is a whitespace-delimited text file with one row
  per cutout. The response is a streaming TAR archive of multi-extension
  FITS files (one FITS per requested cutout).

This module's v0 surface is **single-region**: `fetch_cutout(ra, dec,
size_arcsec, band, ...)` builds a one-row coordinate list, POSTs it,
unpacks the one FITS that comes back, caches it on disk under
``${HSCLA_TOOL_CACHE}/cutouts/<content-hash>.fits``, and returns a
``Cutout`` dataclass that carries both the path and the parsed HDUs.

Coverage caveat: when the region has no HSCLA data, the server returns
HTTP 200 with an empty TAR (~10 KiB of zero padding). We turn that
into a typed ``NoCoverageError`` so callers can branch cleanly.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import requests
from astropy.io import fits
from astropy.wcs import WCS

from hscla_tool import config, db, mask as _mask

LOGGER = logging.getLogger(__name__)

DEFAULT_RELEASE = "la2020"
DEFAULT_KIND: "CutoutKind" = "coadd"
DEFAULT_TRACT = "any"
DEFAULT_HTTP_TIMEOUT = 180.0  # seconds; cutouts can be large

CutoutKind = Literal["coadd", "warp", "frame"]


# --------------------------------------------------------------------------- #
# Errors and result type
# --------------------------------------------------------------------------- #


class CutoutError(RuntimeError):
    """Base class for HSCLA cutout client failures."""


class NoCoverageError(CutoutError):
    """Raised when HSCLA has no data at the requested region/band/kind."""


@dataclass(frozen=True)
class Cutout:
    """One downloaded HSCLA cutout, with both file path and parsed HDUs.

    Attributes
    ----------
    band, ra, dec, size_arcsec, kind : str / float
        The request that produced this cutout.
    fits_path : Path
        On-disk multi-extension FITS file (cached).
    hdul : fits.HDUList
        Opened HDU list. Caller is responsible for closing it via
        `close()` or `with cutout.open() as hdul: ...`.
    image, mask_hdu, variance : ImageHDU | None
        Convenience references to the relevant HDUs (``mask_hdu`` is
        named with the suffix to avoid shadowing the module-level
        :mod:`hscla_tool.mask`). Any of them may be `None` if the
        corresponding flag was set to False at fetch time.
    """

    band: str
    ra: float
    dec: float
    size_arcsec: float
    kind: str
    fits_path: Path
    hdul: fits.HDUList = field(repr=False)
    image: "fits.ImageHDU | None" = field(repr=False, default=None)
    mask_hdu: "fits.ImageHDU | None" = field(repr=False, default=None)
    variance: "fits.ImageHDU | None" = field(repr=False, default=None)

    def close(self) -> None:
        """Close the underlying FITS file handle. Safe to call twice."""

        try:
            self.hdul.close()
        except Exception:  # noqa: BLE001 - safe to swallow during cleanup
            pass

    def wcs(self) -> WCS:
        """Return the WCS of the image HDU (or the only HDU with WCS)."""

        if self.image is not None:
            return WCS(self.image.header)
        for hdu in self.hdul:
            try:
                return WCS(hdu.header)
            except Exception:  # noqa: BLE001
                continue
        raise CutoutError("no usable WCS found in cutout FITS")

    def mask_planes(self, planes: "list[str] | None" = None) -> dict[str, "object"]:
        """Decode the mask HDU into named boolean arrays. See `hscla_tool.mask`."""

        if self.mask_hdu is None:
            raise CutoutError("cutout was fetched without a mask plane (with_mask=False)")
        return _mask.decode(self.mask_hdu, planes=planes)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class HscLaCutoutClient:
    """Thin POST client for the HSCLA DAS cutout service."""

    def __init__(
        self,
        credentials: "config.Credentials | None" = None,
        *,
        session: "requests.Session | None" = None,
        release: str = DEFAULT_RELEASE,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.credentials = credentials or config.load_credentials()
        self.release = release
        self.timeout = timeout
        self._session = session or requests.Session()
        api = db.get_cutout_api()
        self._base_url = str(api["base_url"]).rstrip("/")
        self._endpoint = str(api["endpoint"])
        self._field_name = str(api["multipart_field"])
        token = base64.standard_b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode("utf-8")
        ).decode("ascii")
        self._auth_header = f"Basic {token}"

    def fetch_cutout(
        self,
        ra: float,
        dec: float,
        *,
        size_arcsec: float,
        band: str,
        kind: CutoutKind = DEFAULT_KIND,
        tract: "int | str" = DEFAULT_TRACT,
        with_variance: bool = True,
        with_mask: bool = True,
        cache: bool = True,
        cache_dir: "Path | None" = None,
    ) -> Cutout:
        """Fetch a single HSCLA cutout. See module docstring for the wire format.

        ``size_arcsec`` is the *full* edge length of the square box.
        Internally the request uses half-widths (``sw`` and ``sh``) in
        degrees, as the HSCLA API expects.
        """

        size_deg = float(size_arcsec) / 3600.0
        half_deg = size_deg / 2.0
        rerun = self.release
        key = _cache_key(ra, dec, size_arcsec, band, kind, tract, with_variance, with_mask, rerun)
        root = Path(cache_dir) if cache_dir is not None else config.cache_dir() / "cutouts"
        fits_path = root / f"{key}.fits"

        if cache and fits_path.is_file():
            LOGGER.info("cutout cache hit: %s", fits_path)
            hdul = fits.open(fits_path)
        else:
            body, boundary = _build_multipart_body(
                rerun=rerun,
                kind=kind,
                band=band,
                tract=tract,
                ra=float(ra),
                dec=float(dec),
                half_deg=half_deg,
                with_image=True,
                with_mask=with_mask,
                with_variance=with_variance,
                multipart_field=self._field_name,
            )
            url = self._base_url + self._endpoint
            resp = self._session.post(
                url,
                data=body,
                headers={
                    "Authorization": self._auth_header,
                    "Content-Type": f'multipart/form-data; boundary="{boundary}"',
                },
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                raise CutoutError(
                    f"cutout POST failed: {resp.status_code} {resp.reason} — "
                    f"{resp.text.strip()[:300]}"
                )

            fits_bytes = _extract_one_fits(resp.content)
            if fits_bytes is None:
                raise NoCoverageError(
                    f"HSCLA has no {band} {kind} coverage at "
                    f"(RA, Dec) = ({ra:.6f}, {dec:.6f}) for a {size_arcsec:g}\" box"
                )
            fits_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = fits_path.with_suffix(".fits.tmp")
            tmp.write_bytes(fits_bytes)
            tmp.replace(fits_path)
            hdul = fits.open(fits_path)

        image_hdu, mask_hdu, variance_hdu = _split_hdul(
            hdul, expect_mask=with_mask, expect_variance=with_variance
        )
        return Cutout(
            band=band,
            ra=float(ra),
            dec=float(dec),
            size_arcsec=float(size_arcsec),
            kind=str(kind),
            fits_path=fits_path,
            hdul=hdul,
            image=image_hdu,
            mask_hdu=mask_hdu,
            variance=variance_hdu,
        )


# --------------------------------------------------------------------------- #
# Module-level shortcut
# --------------------------------------------------------------------------- #


def fetch_cutout(
    ra: float,
    dec: float,
    *,
    size_arcsec: float,
    band: str,
    **kwargs: Any,
) -> Cutout:
    """One-line shortcut: instantiate a client and call `fetch_cutout`."""

    return HscLaCutoutClient().fetch_cutout(
        ra, dec, size_arcsec=size_arcsec, band=band, **kwargs
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cache_key(
    ra: float,
    dec: float,
    size_arcsec: float,
    band: str,
    kind: str,
    tract: "int | str",
    with_variance: bool,
    with_mask: bool,
    rerun: str,
) -> str:
    payload = json.dumps(
        {
            "ra": round(float(ra), 12),
            "dec": round(float(dec), 12),
            "size_arcsec": float(size_arcsec),
            "band": band,
            "kind": kind,
            "tract": str(tract),
            "with_variance": bool(with_variance),
            "with_mask": bool(with_mask),
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
    tract: "int | str",
    ra: float,
    dec: float,
    half_deg: float,
    with_image: bool,
    with_mask: bool,
    with_variance: bool,
    multipart_field: str,
    boundary: str = "HscLaToolBoundary",
) -> "tuple[bytes, str]":
    tract_str = "any" if str(tract).lower() == "any" else str(tract)
    header = (
        "#? rerun type filter tract ra dec sw sh image mask variance\n"
    )
    row = (
        f"{rerun} {kind} {band} {tract_str} "
        f"{ra:.16e}deg {dec:.16e}deg "
        f"{half_deg:.16e}deg {half_deg:.16e}deg "
        f"{'true' if with_image else 'false'} "
        f"{'true' if with_mask else 'false'} "
        f"{'true' if with_variance else 'false'}\n"
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


def _extract_one_fits(tar_bytes: bytes) -> "bytes | None":
    """Pull the (single) FITS file out of an HSCLA cutout TAR response.

    Returns ``None`` if the TAR is empty (the server's no-coverage signal).
    """

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".fits"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            return handle.read()
    return None


_HDU_KIND_RE = re.compile(r"(image|mask|variance)", re.IGNORECASE)


def _split_hdul(
    hdul: fits.HDUList,
    *,
    expect_mask: bool,
    expect_variance: bool,
) -> "tuple[fits.ImageHDU | None, fits.ImageHDU | None, fits.ImageHDU | None]":
    """Identify which HDUs in the cutout file are image / mask / variance.

    HSCLA writes them in the order ``[PRIMARY, image, mask, variance]``
    (with mask and variance only present when requested). We rely on
    that ordering plus a check that the mask HDU is integer-typed.
    """

    image_hdu: "fits.ImageHDU | None" = None
    mask_hdu: "fits.ImageHDU | None" = None
    variance_hdu: "fits.ImageHDU | None" = None

    data_hdus = [hdu for hdu in hdul[1:] if hdu.data is not None]
    expected = 1 + int(expect_mask) + int(expect_variance)
    if len(data_hdus) < expected:
        LOGGER.warning(
            "cutout FITS has %d data HDUs but %d were expected; mapping by position",
            len(data_hdus), expected,
        )

    for hdu in data_hdus:
        is_integer = hdu.data.dtype.kind in ("i", "u")
        # HSCLA orders [image, mask, variance]. Image is always first.
        if image_hdu is None and not is_integer:
            image_hdu = hdu  # type: ignore[assignment]
            continue
        if expect_mask and mask_hdu is None and is_integer:
            mask_hdu = hdu  # type: ignore[assignment]
            continue
        if expect_variance and variance_hdu is None and not is_integer:
            variance_hdu = hdu  # type: ignore[assignment]
            continue

    return image_hdu, mask_hdu, variance_hdu
