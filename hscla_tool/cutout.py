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

Two entry points, both built on the same wire format:

- ``fetch_cutout(ra, dec, ...)`` — single region. Builds a 1-row
  coordlist, POSTs, unpacks the one FITS, caches it, returns a
  ``Cutout`` dataclass. Raises ``NoCoverageError`` when the region
  has no data.
- ``fetch_cutouts(requests, ...)`` — bulk. Accepts a
  ``pandas.DataFrame`` or a ``list[CutoutRequest]``, sends one POST
  for the cache-miss rows, parses the multi-FITS TAR, and returns a
  ``BatchResult`` whose ``.cutouts`` list runs parallel to the input
  (``None`` for no-coverage rows) and whose ``.failures`` list carries
  the per-row exceptions.

Cached FITS live at ``${HSCLA_TOOL_CACHE}/cutouts/<content-hash>.fits``;
the cache key is the SHA-256 hash of the request tuple, so re-running
the same request — single or batch — is a free local read.

Coverage caveats:

- Single region with no coverage → HTTP 200 + empty TAR → typed
  ``NoCoverageError``.
- In a batch, the server simply omits the no-coverage rows from the
  TAR (no zero-byte placeholder, no sentinel). Each TAR member is
  named ``<N>-cutout-<band>-<tract>-<release>.fits`` where ``N`` is
  the 1-indexed coordlist line number (the ``#?`` header is line 1),
  so input row ``i`` maps to prefix ``i + 2``. Missing prefixes are
  recorded as per-row ``NoCoverageError`` entries in
  ``BatchResult.failures``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import requests
from astropy.io import fits
from astropy.wcs import WCS

from hscla_tool import config, db
from hscla_tool import mask as _mask

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    import pandas as pd

LOGGER = logging.getLogger(__name__)

DEFAULT_RELEASE = "la2020"
DEFAULT_KIND: CutoutKind = "coadd"
DEFAULT_TRACT = "any"
DEFAULT_HTTP_TIMEOUT = 180.0  # seconds; cutouts can be large

CutoutKind = Literal["coadd", "warp", "frame"]

# Hard cap on how many rows a single POST may carry. The upstream
# manual documents 990 as the per-request limit on the multipart
# coordlist; anything larger is rejected server-side.
MAX_BATCH_ROWS = 990


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
    image: fits.ImageHDU | None = field(repr=False, default=None)
    mask_hdu: fits.ImageHDU | None = field(repr=False, default=None)
    variance: fits.ImageHDU | None = field(repr=False, default=None)

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

    def mask_planes(self, planes: list[str] | None = None) -> dict[str, object]:
        """Decode the mask HDU into named boolean arrays. See `hscla_tool.mask`."""

        if self.mask_hdu is None:
            raise CutoutError("cutout was fetched without a mask plane (with_mask=False)")
        return _mask.decode(self.mask_hdu, planes=planes)


@dataclass(frozen=True)
class CutoutRequest:
    """One row of a bulk cutout request.

    Field names match the keyword arguments of :func:`fetch_cutout` so
    a single-row call and a one-element batch produce byte-identical
    wire requests and share the same cache key.

    ``tract`` accepts ``"any"`` (default) to let the server pick the
    overlapping tract, or an explicit integer tract id.
    """

    ra: float
    dec: float
    size_arcsec: float
    band: str
    kind: str = DEFAULT_KIND
    tract: int | str = DEFAULT_TRACT
    with_variance: bool = True
    with_mask: bool = True


@dataclass(frozen=True)
class BatchResult:
    """Result of :func:`fetch_cutouts`, parallel to the input rows.

    Attributes
    ----------
    cutouts : tuple[Cutout | None, ...]
        One slot per input row, in the original order. Slot ``i`` is
        the :class:`Cutout` for input row ``i``, or ``None`` if that
        row had no HSCLA coverage (or any other per-row failure).
    failures : tuple[tuple[int, Exception], ...]
        ``(input_row_index, exception)`` for every row whose slot is
        ``None``. Ordered by ``input_row_index``. Typically the
        exception is a :class:`NoCoverageError`.
    """

    cutouts: tuple[Cutout | None, ...]
    failures: tuple[tuple[int, Exception], ...]

    @property
    def n_success(self) -> int:
        """Count of input rows that produced a Cutout."""

        return sum(1 for c in self.cutouts if c is not None)

    @property
    def n_failure(self) -> int:
        """Count of input rows that did not produce a Cutout."""

        return len(self.failures)

    def __len__(self) -> int:
        return len(self.cutouts)

    def __iter__(self):
        return iter(self.cutouts)

    def close(self) -> None:
        """Close every underlying FITS handle. Safe to call twice."""

        for c in self.cutouts:
            if c is not None:
                c.close()


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class HscLaCutoutClient:
    """Thin POST client for the HSCLA DAS cutout service."""

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
        api = db.get_cutout_api()
        self._base_url = str(api["base_url"]).rstrip("/")
        self._endpoint = str(api["endpoint"])
        self._field_name = str(api["multipart_field"])
        token = base64.standard_b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode()
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
        tract: int | str = DEFAULT_TRACT,
        with_variance: bool = True,
        with_mask: bool = True,
        cache: bool = True,
        cache_dir: Path | None = None,
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

    def fetch_cutouts(
        self,
        requests_input: pd.DataFrame | Iterable[CutoutRequest],
        *,
        cache: bool = True,
        cache_dir: Path | None = None,
    ) -> BatchResult:
        """Fetch many HSCLA cutouts in a single round-trip.

        See the module docstring and `docs/SPEC.md` §6.3 for the wire
        format and TAR-prefix mapping rules. Rows are validated before
        any HTTP call.
        """

        requests_list = _normalize_requests(requests_input)
        if not requests_list:
            return BatchResult(cutouts=(), failures=())
        if len(requests_list) > MAX_BATCH_ROWS:
            raise CutoutError(
                f"batch size {len(requests_list)} exceeds the server-side "
                f"limit of {MAX_BATCH_ROWS} rows per POST; split the input."
            )
        for idx, req in enumerate(requests_list):
            _validate_request(req, idx)

        rerun = self.release
        root = Path(cache_dir) if cache_dir is not None else config.cache_dir() / "cutouts"
        root.mkdir(parents=True, exist_ok=True)

        # Partition input rows into already-cached and to-be-fetched.
        per_row_path: list[Path] = []
        miss_indices: list[int] = []
        for idx, req in enumerate(requests_list):
            key = _request_cache_key(req, rerun)
            per_row_path.append(root / f"{key}.fits")
            if not (cache and per_row_path[idx].is_file()):
                miss_indices.append(idx)

        if miss_indices:
            body, boundary = _build_batch_multipart_body(
                rerun=rerun,
                rows=[requests_list[i] for i in miss_indices],
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
                    f"cutout batch POST failed: {resp.status_code} {resp.reason} — "
                    f"{resp.text.strip()[:300]}"
                )
            # Map: 0-based position-within-misses -> bytes (only for covered rows).
            fits_by_miss_pos = _extract_tar_by_prefix(resp.content)
            for miss_pos, idx in enumerate(miss_indices):
                fits_bytes = fits_by_miss_pos.get(miss_pos)
                if fits_bytes is None:
                    # Server omitted this row -> no coverage. Defer the
                    # NoCoverageError to the assembly loop below so we
                    # also surface it for fully-cached + uncovered re-runs.
                    continue
                tmp = per_row_path[idx].with_suffix(".fits.tmp")
                tmp.write_bytes(fits_bytes)
                tmp.replace(per_row_path[idx])

        cutouts: list[Cutout | None] = []
        failures: list[tuple[int, Exception]] = []
        for idx, req in enumerate(requests_list):
            path = per_row_path[idx]
            if not path.is_file():
                exc = NoCoverageError(
                    f"HSCLA has no {req.band} {req.kind} coverage at "
                    f"(RA, Dec) = ({req.ra:.6f}, {req.dec:.6f}) for a "
                    f"{req.size_arcsec:g}\" box"
                )
                cutouts.append(None)
                failures.append((idx, exc))
                continue
            hdul = fits.open(path)
            image_hdu, mask_hdu, variance_hdu = _split_hdul(
                hdul, expect_mask=req.with_mask, expect_variance=req.with_variance,
            )
            cutouts.append(Cutout(
                band=req.band,
                ra=float(req.ra),
                dec=float(req.dec),
                size_arcsec=float(req.size_arcsec),
                kind=str(req.kind),
                fits_path=path,
                hdul=hdul,
                image=image_hdu,
                mask_hdu=mask_hdu,
                variance=variance_hdu,
            ))
        return BatchResult(cutouts=tuple(cutouts), failures=tuple(failures))


# --------------------------------------------------------------------------- #
# Module-level shortcuts
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


def fetch_cutouts(
    requests_input: pd.DataFrame | Iterable[CutoutRequest],
    **kwargs: Any,
) -> BatchResult:
    """One-line shortcut: instantiate a client and call `fetch_cutouts`."""

    return HscLaCutoutClient().fetch_cutouts(requests_input, **kwargs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cache_key(
    ra: float,
    dec: float,
    size_arcsec: float,
    band: str,
    kind: str,
    tract: int | str,
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
    tract: int | str,
    ra: float,
    dec: float,
    half_deg: float,
    with_image: bool,
    with_mask: bool,
    with_variance: bool,
    multipart_field: str,
    boundary: str = "HscLaToolBoundary",
) -> tuple[bytes, str]:
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


def _extract_one_fits(tar_bytes: bytes) -> bytes | None:
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


# Filename pattern for one TAR member: "<prefix>-cutout-<band>-<tract>-<release>.fits".
# The leading integer prefix is the 1-indexed line number of the source
# coordlist (header is line 1), confirmed by live probe on 2026-05-13.
_TAR_PREFIX_RE = re.compile(r"^(\d+)-cutout-")


def _extract_tar_by_prefix(tar_bytes: bytes) -> dict[int, bytes]:
    """Map TAR members back to input rows via the integer filename prefix.

    Returns a dict keyed by **0-indexed input row** (prefix minus 2,
    since the ``#?`` header occupies line 1 and the first data row is
    line 2). Members whose basename does not match the expected pattern
    are logged and skipped — the caller treats anything missing from
    this dict as a no-coverage row.
    """

    out: dict[int, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".fits"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            basename = member.name.rsplit("/", 1)[-1]
            match = _TAR_PREFIX_RE.match(basename)
            if match is None:
                LOGGER.warning(
                    "cutout TAR member %r has no integer prefix; skipping", member.name,
                )
                continue
            prefix = int(match.group(1))
            row_idx = prefix - 2
            if row_idx < 0:
                LOGGER.warning(
                    "cutout TAR member %r maps to negative row index %d; skipping",
                    member.name, row_idx,
                )
                continue
            out[row_idx] = handle.read()
    return out


def _normalize_requests(
    requests_input: pd.DataFrame | Iterable[CutoutRequest],
) -> list[CutoutRequest]:
    """Coerce DataFrame or list-of-CutoutRequest into a list of CutoutRequest.

    DataFrame inputs must carry the four required columns
    (``ra``, ``dec``, ``size_arcsec``, ``band``); optional columns fall
    back to the same defaults as :class:`CutoutRequest`.
    """

    # Avoid importing pandas at module level. We only need it when the
    # caller actually hands us a DataFrame.
    try:
        import pandas as _pd
    except ImportError:  # pragma: no cover - pandas is a hard dep, but be defensive
        _pd = None  # type: ignore[assignment]

    if _pd is not None and isinstance(requests_input, _pd.DataFrame):
        df = requests_input
        required = ("ra", "dec", "size_arcsec", "band")
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise CutoutError(
                f"DataFrame input missing required column(s): {', '.join(missing)}"
            )
        out: list[CutoutRequest] = []
        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            out.append(CutoutRequest(
                ra=float(row_dict["ra"]),
                dec=float(row_dict["dec"]),
                size_arcsec=float(row_dict["size_arcsec"]),
                band=str(row_dict["band"]),
                kind=str(row_dict.get("kind", DEFAULT_KIND)),
                tract=row_dict.get("tract", DEFAULT_TRACT),
                with_variance=bool(row_dict.get("with_variance", True)),
                with_mask=bool(row_dict.get("with_mask", True)),
            ))
        return out

    # Iterable of CutoutRequest (or anything that quacks like it).
    out = []
    for idx, item in enumerate(requests_input):
        if not isinstance(item, CutoutRequest):
            raise CutoutError(
                f"requests[{idx}] must be a CutoutRequest "
                f"(got {type(item).__name__}); pass a DataFrame or a list of "
                f"CutoutRequest instances."
            )
        out.append(item)
    return out


def _validate_request(req: CutoutRequest, idx: int) -> None:
    """Reject invalid CutoutRequest values before any HTTP call."""

    if not (0.0 <= float(req.ra) <= 360.0):
        raise CutoutError(f"requests[{idx}].ra out of [0, 360]: {req.ra!r}")
    if not (-90.0 <= float(req.dec) <= 90.0):
        raise CutoutError(f"requests[{idx}].dec out of [-90, 90]: {req.dec!r}")
    if not (float(req.size_arcsec) > 0.0):
        raise CutoutError(f"requests[{idx}].size_arcsec must be > 0: {req.size_arcsec!r}")
    if not (isinstance(req.band, str) and req.band.strip()):
        raise CutoutError(f"requests[{idx}].band must be a non-empty string")
    if not (isinstance(req.kind, str) and req.kind.strip()):
        raise CutoutError(f"requests[{idx}].kind must be a non-empty string")


def _request_cache_key(req: CutoutRequest, rerun: str) -> str:
    """Cache key for a CutoutRequest; byte-identical to the single-row key."""

    return _cache_key(
        ra=req.ra, dec=req.dec, size_arcsec=req.size_arcsec,
        band=req.band, kind=req.kind, tract=req.tract,
        with_variance=req.with_variance, with_mask=req.with_mask,
        rerun=rerun,
    )


def _build_batch_multipart_body(
    *,
    rerun: str,
    rows: list[CutoutRequest],
    multipart_field: str,
    boundary: str = "HscLaToolBoundary",
) -> tuple[bytes, str]:
    """Build the multipart body for an N-row cutout POST."""

    lines = ["#? rerun type filter tract ra dec sw sh image mask variance\n"]
    for req in rows:
        half_deg = float(req.size_arcsec) / 7200.0  # full size / 2 / 3600
        tract_str = "any" if str(req.tract).lower() == "any" else str(req.tract)
        lines.append(
            f"{rerun} {req.kind} {req.band} {tract_str} "
            f"{float(req.ra):.16e}deg {float(req.dec):.16e}deg "
            f"{half_deg:.16e}deg {half_deg:.16e}deg "
            f"true "
            f"{'true' if req.with_mask else 'false'} "
            f"{'true' if req.with_variance else 'false'}\n"
        )
    coord_list = "".join(lines)
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{multipart_field}"; '
        f'filename="coordlist.txt"\r\n\r\n'
        + coord_list
        + f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")
    return body, boundary


_HDU_KIND_RE = re.compile(r"(image|mask|variance)", re.IGNORECASE)


def _split_hdul(
    hdul: fits.HDUList,
    *,
    expect_mask: bool,
    expect_variance: bool,
) -> tuple[fits.ImageHDU | None, fits.ImageHDU | None, fits.ImageHDU | None]:
    """Identify which HDUs in the cutout file are image / mask / variance.

    HSCLA writes them in the order ``[PRIMARY, image, mask, variance]``
    (with mask and variance only present when requested). We rely on
    that ordering plus a check that the mask HDU is integer-typed.
    """

    image_hdu: fits.ImageHDU | None = None
    mask_hdu: fits.ImageHDU | None = None
    variance_hdu: fits.ImageHDU | None = None

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
