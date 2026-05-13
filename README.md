# hscla_tool

A Python toolkit and machine-readable knowledge base for the **HSC Legacy
Archive (HSCLA)** — currently targeting HSCLA2020, which is reduced with
`hscPipe v8` (same pipeline as HSC-SSP PDR3).

`hscla_tool` does two things:

1. Gives humans **and coding agents** a structured catalog of the
   archive — what tables exist, what they mean, where the cutout / PSF
   / SQL endpoints live, and what the column conventions are. The
   primary artifact for this is [`data/hscla_db.yaml`](data/hscla_db.yaml).
2. Provides a small Python API (and, soon, CLI) so you can query the
   archive without re-reading the upstream HTTP docs every time.

Task-oriented recipes are in [`docs/USAGE.md`](docs/USAGE.md) — go
there when you have a specific "how do I X?" question. The
architecture lives in [`docs/SPEC.md`](docs/SPEC.md); the plan and
phase-by-phase status in [`docs/todo.md`](docs/todo.md); accumulated
gotchas in [`docs/lessons.md`](docs/lessons.md).

## What works right now

- A validated YAML knowledge base of all HSCLA2020 catalog tables,
  interactive-tool URLs, photometry conventions, and server-side
  WHERE-clause helpers.
- `import hscla_tool` — verifies your HSCLA login at import time.
- `hscla_tool.sql` — a session-cookie-aware SQL client with
  `preview_sql` (fast metadata lookups) and `run_sql` (full
  submit / poll / download → `pandas.DataFrame`, results cached by
  content hash).
- `hscla_tool.coverage` — `region_coverage(ra, dec, size_deg=...)`
  and `frame_coverage(...)` answer "is there HSC data here, in which
  bands, in which patches / how many visits?". Returns frozen
  dataclasses with a `.covered` flag; uncovered regions return
  empty results without raising. Pass `source='local'` to use the
  local Parquet mirror (faster + uses exact patch corners).
- `hscla_tool.mirror` — `build_mirror(table)` materializes one
  whole HSCLA metadata catalog as a single Parquet file under
  `/Volumes/galaxy/hsc/la2020/` (override with `HSCLA_MIRROR_ROOT`).
- `hscla_tool.cutout` — `fetch_cutout(ra, dec, size_arcsec, band, ...)`
  downloads an HSCLA coadd cutout (multi-extension FITS with image,
  mask, variance), caches by content hash, returns a `Cutout`
  dataclass with `.wcs()` and `.mask_planes()` helpers.
- `hscla_tool.mask` — `decode(mask_hdu)` turns the integer mask plane
  into a dict of named boolean arrays (`BAD`, `SAT`, `CR`, ...).
- `hscla_tool.psf` — `fetch_psf(ra, dec, band=...)` downloads a coadd
  PSF kernel as a single-HDU FITS (sum-normalized to 1.0), cached by
  content hash. Returns a `Psf` dataclass with `.array` and `.wcs()`
  helpers. `cutout.NoCoverageError` is raised when there is no data.
- `hscla_tool.crossmatch` — `match(table, ra_col=..., dec_col=...,
  radius_arcsec=1.0)` crossmatches a pandas DataFrame against
  `la2020.forced` and returns matched rows with `object_id` and
  `match_distance` (built on top of `sql.run_sql`, no separate
  upload service).
- `hscla_tool.archive` — `download_coadd_image(tract, patch, band)`
  and `download_forced_catalog(...)` (plus a generic
  `download_patch_file(..., kind=)`) hit the HSCLA file tree
  directly, resumable via HTTP `Range:` requests.
- `hscla` console script — every module above is also reachable as a
  subcommand (`hscla coverage`, `hscla cutout`, `hscla psf`, …). See
  the [CLI quick tour](#cli-quick-tour) below.

## Credentials

The toolkit reads HSCLA credentials from two environment variables:

| Variable     | What it is                       |
| ------------ | -------------------------------- |
| `HSCLA_USR`  | Your HSCLA / STARS account email |
| `HSCLA_PWD`  | The matching password            |

Put them in `~/.zshenv` (not `~/.zprofile`) so non-login shells —
including `uv run`, CI workers, and editor tooling — inherit them. If
either variable is missing, `import hscla_tool` fails immediately with a
clear error.

## Install and first query

```bash
git clone git@github.com:dr-guangtou/hscla_tool.git
cd hscla_tool
uv sync
```

Smallest possible end-to-end check:

```python
from hscla_tool import sql

# Fast inline lookup (server-side ~5 s timeout — metadata queries only):
result = sql.preview_sql("SELECT 1 AS one")
print(result)  # {'count': 1, 'fields': ['one'], 'rows': [['1']]}

# Full job pipeline → DataFrame, cached locally:
df = sql.run_sql(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='la2020' ORDER BY table_name LIMIT 5"
)
print(df)
```

Repeated calls with the same SQL are served from the local cache
(`${HSCLA_TOOL_CACHE}` if set, otherwise `./outputs/sql/`).

### Coverage query

```python
from hscla_tool import coverage

cov = coverage.region_coverage(
    ra=49.265759499639465, dec=41.24859266109193, size_deg=0.03
)
print(cov.filters)              # ('HSC-G', 'HSC-I', 'HSC-R', 'HSC-Z')
print(cov.mean_seeing_per_band) # {'HSC-G': 0.69, 'HSC-I': 0.64, ...}
for p in cov.patches:
    print(p.band, p.tract, p.patch_s, p.skymap_id, p.seeing)

frames = coverage.frame_coverage(49.27, 41.24, size_deg=0.03)
print({b: (s.n_frames, s.n_visits) for b, s in frames.band_summary.items()})
```

### Local catalog mirror (faster and more precise)

Build a Parquet copy of the small metadata tables once, then run
coverage queries entirely offline:

```bash
uv run python -m hscla_tool.mirror status            # what's already on disk?
uv run python -m hscla_tool.mirror build mosaic      # ~1 min
uv run python -m hscla_tool.mirror build frame       # ~5–15 min
```

```python
cov = coverage.region_coverage(49.27, 41.24, size_deg=0.03, source="local")
```

Mirrors live at `/Volumes/galaxy/hsc/la2020/<table>.parquet` by
default; override the directory by setting `HSCLA_MIRROR_ROOT`. The
local path uses the *real* patch corners with an RA=0 wrap guard,
which is strictly tighter than the server's patch-center proximity
filter.

### Cutout + mask

```python
from hscla_tool import cutout, mask

c = cutout.fetch_cutout(
    ra=49.265759499639465, dec=41.24859266109193,
    size_arcsec=108.0,            # 0.03 deg box
    band="HSC-I",
    with_variance=True, with_mask=True,
)
try:
    image = c.image.data           # numpy float32
    var = c.variance.data
    planes = c.mask_planes()       # {'BAD': ndarray, 'SAT': ndarray, ...}
    sat_pixels = planes["SAT"]
    bad_pixels = planes["BAD"]
    wcs = c.wcs()
finally:
    c.close()
```

`fetch_cutout` returns a typed `NoCoverageError` when HSCLA has no
data at the requested (RA, Dec, band, kind). Cutout FITS files are
cached under `${HSCLA_TOOL_CACHE}/cutouts/` by a SHA-256 hash of the
request, so a re-run of the same script reads the local file instead
of refetching.

### Bulk cutouts (one POST, up to 990 rows)

For many cutouts at once, use `fetch_cutouts`. The underlying
multipart form accepts up to 990 rows per request; we send one POST
for every cache-miss row in the input, partitioning around already-
cached rows so re-runs do less work each time.

```python
import pandas as pd
from hscla_tool import cutout

requests = pd.DataFrame({
    "ra":          [49.27, 49.28, 198.0],  # last row is uncovered
    "dec":         [41.25, 41.25, 29.5],
    "size_arcsec": [108.0, 108.0, 108.0],
    "band":        ["HSC-I", "HSC-R", "HSC-I"],
})
# A list of cutout.CutoutRequest works too.

result = cutout.fetch_cutouts(requests)
result.cutouts   # tuple[Cutout | None, ...] parallel to input rows
result.failures  # ((2, NoCoverageError(...)),)
print(f"{result.n_success}/{len(result)} succeeded, "
      f"{result.n_failure} with no coverage")

for c in result.cutouts:
    if c is None:
        continue
    image = c.image.data
    ...
result.close()
```

`fetch_cutouts` raises `CutoutError` for whole-batch failures
(HTTP error, oversized batch, invalid request fields). Per-row
"no coverage" lands in `result.failures`, not as an exception.

### PSF kernel

```python
from hscla_tool import psf

p = psf.fetch_psf(
    ra=49.265759499639465, dec=41.24859266109193,
    band="HSC-I",                # 'i' also works; server normalizes
    centered=True,               # peak lands at the requested RA/Dec
)
try:
    kernel = p.array              # 2D numpy.float64, normalized to sum 1
    assert abs(kernel.sum() - 1.0) < 1e-3
finally:
    p.close()
```

PSFs share the same `NoCoverageError` (re-exported from `cutout`)
when the region has no HSCLA data, and the same content-hash cache
layout under `${HSCLA_TOOL_CACHE}/psfs/`.

### Crossmatch *(placeholder — currently very slow)*

> **HSCLA's SQL crossmatch is slow.** A three-row input takes
> **30–45 minutes** on the server side even with the most
> index-friendly query shape we have found. The module below is
> correct and tested live, but in practice you'll usually want one of
> the alternatives below until NAOJ improves the service.

Alternatives that are usually faster on this archive:

- Crossmatch locally against the Parquet mirror of `la2020.forced`
  (planned, not yet implemented).
- Pull the patch-level forced catalogs via
  [`hscla_tool.archive`](#bulk-archive-download) and crossmatch
  inside pandas.

API for when you do want to use it:

```python
import pandas as pd
from hscla_tool import crossmatch

inputs = pd.DataFrame({
    "name": ["a", "b"],
    "ra":   [49.265, 49.270],
    "dec":  [41.248, 41.250],
})
matches = crossmatch.match(
    inputs, id_col="name",
    radius_arcsec=1.0,
    extra_columns=("i_cmodel_mag", "i_cmodel_magerr"),
)
```

`crossmatch.match` emits a `UNION ALL` of per-row `coneSearch` calls
with literal coordinates and runs the result through `sql.run_sql`,
so the cache, login, and poll-loop machinery are shared with every
other SQL query. Returns one row per (input row, matched HSCLA
object) inside the radius; pass `nearest_only=True` to keep only the
closest match per input.

### Bulk archive download

```python
from hscla_tool import archive

# Coadd image for Perseus, tract 15548, patch 1,6 in HSC-I (~136 MB).
out = archive.download_coadd_image(tract=15548, patch="1,6", band="HSC-I")
print(out.path, out.bytes)

# Or any of the nine per-patch file kinds:
forced = archive.download_patch_file(
    tract=15548, patch="1,6", band="HSC-I", kind="forced_src",
)
```

Files land under `${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/`
mirroring the upstream layout. Downloads are resumable: an
interrupted download leaves a `.tmp` next to the destination and the
next call sends `Range: bytes=<offset>-` to pick up where it stopped.

## CLI quick tour

After `uv sync`, the `hscla` command is on your `$PATH` (via the
`hscla = hscla_tool.cli:main` entry point in `pyproject.toml`). Every
file-producing subcommand auto-names its output under
`./outputs/<subkind>/` unless you pass `--out`. Friendly progress lines
go to **stderr**; the final result path goes to **stdout** so the
command composes with shell pipelines. Pass `--quiet` / `-q` to drop
the progress chatter.

```bash
# Coverage and provenance
hscla coverage 49.2658 41.2486 --size-deg 0.03
hscla coverage 49.2658 41.2486 --size-deg 0.03 --source local
hscla frames   49.2658 41.2486 --size-deg 0.03 --detailed

# Cutouts and PSFs (saved under ./outputs/cutouts/ and ./outputs/psfs/)
hscla cutout 49.2658 41.2486 --size-arcsec 108 --band HSC-I
hscla psf    49.2658 41.2486 --band HSC-I

# Bulk cutouts from a CSV/Parquet of (ra, dec, size_arcsec, band) rows
hscla cutouts inputs.csv                       # one POST for the whole batch

# SQL
hscla sql "SELECT COUNT(*) FROM la2020.mosaic" --preview
hscla sql --file my_query.sql                    # full submit/poll/download

# Crossmatch (prints a slow-server warning; expect 30-45 min)
hscla crossmatch inputs.csv --radius-arcsec 1.0 --nearest-only

# Local Parquet mirrors of the small metadata catalogs
hscla mirror status
hscla mirror build mosaic

# Direct file-tree download (one per-patch FITS)
hscla archive list 15548 1,6 HSC-I
hscla archive download 15548 1,6 HSC-I --kind calexp
```

Exit codes: `0` on success, `2` for "no HSCLA coverage", `3` for
missing credentials, `4` for a missing local mirror, `5` for a fetch
failure, `6` for bad arguments, `130` on Ctrl-C.

## Reference: HSCLA endpoints and data

- HSC Legacy Archive: https://hscla.mtk.nao.ac.jp/doc/home/
- Updated document for HSCLA_2020: https://hscla.mtk.nao.ac.jp/doc/
    - HSCLA2020 includes data taken up through 2020 and is the latest release.
    - The data reduction pipeline is `hscPipe v.8`, the same with HSC PDR3.
    - HSCLA2020 data overview: https://hscla.mtk.nao.ac.jp/doc/available-data-hscla2020/

### Interactive tools

- Data access portal: https://hscla.mtk.nao.ac.jp/doc/data-access-hscla2020/
- SQL search (web UI): https://hscla.mtk.nao.ac.jp/datasearch/
- Schema browser: https://hscla.mtk.nao.ac.jp/schema/
- DAS image cutout: https://hscla.mtk.nao.ac.jp/das_cutout/la2020/
  (manual: https://hscla.mtk.nao.ac.jp/das_cutout/la2020/manual.html)
- PSF picker: https://hscla.mtk.nao.ac.jp/psf/la2020/
  (manual: https://hscla.mtk.nao.ac.jp/psf/la2020/manual.html)
- DAS search form: https://hscla.mtk.nao.ac.jp/das_search/la2020/
  (manual: https://hscla.mtk.nao.ac.jp/das_search/la2020/usage.html)
- Direct file tree: https://hscla.mtk.nao.ac.jp/archive/files/la2020/

### SQL API (HTTP)

Confirmed by live probe on 2026-05-12 and recorded under `sql_api` in
[`data/hscla_db.yaml`](data/hscla_db.yaml):

- Login: POST `https://hscla.mtk.nao.ac.jp/account/api/session` with
  `{"email": "...", "password": "..."}`. The response sets a
  `LAAUTH_SESSION` cookie that must be sent on subsequent requests.
- Catalog jobs base: `https://hscla.mtk.nao.ac.jp/datasearch/api/catalog_jobs/`
  with suffixes `preview`, `submit`, `status`, `download`, `delete`,
  `cancel` (all POST).
- The catalog-job request body must include the credential **again**
  plus a `clientVersion` field (currently `20190924.1`).
- The literal `release_version` string the server expects is
  `hscla2020` (and `hscla2014` / `hscla2016` for older releases). We
  use the short name `la2020` everywhere else; the mapping is in the
  knowledge base.

### Official NAOJ command-line scripts (reference)

We do not depend on these, but they are the upstream authority on
payload shapes. All under
https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020:

- `catalogQuery/hscSspQuery.py` — submit SQL jobs.
- `colorPostage/colorPostage.py` — render color postage stamps.
- `downloadCutout/downloadCutout.py` — FITS cutouts (image, variance, mask).
- `downloadPsf/downloadPsf.py` — PSF model at (RA, Dec) in a chosen band.
- `pdr2/hscSspCrossMatch/hscSspCrossMatch.py` — crossmatch against
  HSCLA when invoked with `--rerun=la2020`.

## HSCLA 2020 catalog tables

Database schema name: `la2020`. Full descriptions and join keys live in
[`data/hscla_db.yaml`](data/hscla_db.yaml); use
`hscla_tool.db.get_table('<name>')` for programmatic access. The
authoritative live schema is the browser at
https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.

**Metadata / geometry**

- `mosaic` — coadd patch metadata (zeropoints from `jointcal`, corner coords, seeing).
- `frame` — single-CCD exposure metadata.
- `photocalib` — per-CCD photometric calibration (mosaic-derived zeropoints).
- `mosaicframe` — join table linking coadd patches to their input frames.
- `warped` — per-CCD warped-frame metadata (after PSF homogenization).
- `wcs` — WCS solutions and footprint vertices for coadd patches.
- `frame_hpx11`, `mosaic_hpx11`, `warped_hpx11` — HEALPix-level-11 spatial indexes.
- `random` — uniform random points within the HSCLA footprint.

**Forced photometry** (`object_id`, positions fixed by the reference band)

- `forced` — summary table: footprint membership, isPrimary, Milky Way
  extinction, `merge_footprint_*` / `merge_peak_*` per filter, all
  pixel flags, CModel photometry.
- `forced_aper` — aperture fluxes.
- `forced_conv` / `forced_conv_flag` — PSF-convolved aperture fluxes + flags.
- `forced_flux` — Gaussian / PSF / Kron / undeblended PSF / undeblended Kron fluxes.
- `forced_other` — input counts, variance, local background, SDSS shape, double-Shapelet PSF, SDSS centroid.
- `forced_undeb_aper` — undeblended aperture photometry (no PSF homogenization).
- `forced_undeb_conv` — **best photometry for photo-z**: undeblended aperture after PSF homogenization.
- `forced_undeb_conv_flag` — flags for `forced_undeb_conv`.

**Unforced ("meas") photometry**

- `meas` — summary table of per-band unforced measurements.
- `meas_aper` — aperture photometry.
- `meas_centroid` — naive and SDSS centroids.
- `meas_cmodel` — unforced CModel.
- `meas_conv` / `meas_conv_flag` — PSF-convolved aperture + flags.
- `meas_flux` — Gaussian / PSF / Kron fluxes.
- `meas_hsm` — HSM PSF measurements (shape / shear).
- `meas_other` — input count, variance, local background, footprint area, SDSS shape, blendedness, double-Shapelet PSF.

### Photometric conventions

- Fluxes are in **nanojansky**; positions in **degrees** (ICRS).
- Shapes and ellipticities are re-projected into the tangent plane at
  the object's own position (first axis ∥ RA, second axis ∥ DEC; the
  tangent plane is flipped relative to the coadd image).

### Server-side WHERE-clause helpers

| Function                                                      | Description                                                                                                                                                                                                                                  |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `coneSearch(coord, RA_deg, DEC_deg, RADIUS_arcsec) -> bool`   | True if `coord` is inside a circle of radius RADIUS arcsec around (RA, DEC) in degrees.                                                                                                                                                       |
| `boxSearch(coord, RA1, RA2, DEC1, DEC2) -> bool`              | True if `coord` is inside the box `[RA1, RA2] × [DEC1, DEC2]` in degrees. RA-wrap matters: `(350, 370, …)` means `ra in [350, 360] ∪ [0, 10]`; `(350, 10, …)` means `ra in [10, 350]`.                                                        |
| `tractSearch(object_id, TRACT) -> bool`                       | True if `tract == TRACT`.                                                                                                                                                                                                                    |
| `tractSearch(object_id, TRACT1, TRACT2) -> bool`              | True if `tract` is in `[TRACT1, TRACT2]`.                                                                                                                                                                                                    |

The `coord` argument is a server-side type. On `forced` and `meas` it
is the `coord` column directly; on `mosaic` use `ra2000` / `dec2000`
plus standard SQL ranges instead (the `areacube` footprint type does
not match the cone center).

## Test regions

Two coordinates exercise the "covered" and "uncovered" code paths and
appear in every new module's tests. They live in
[`data/hscla_db.yaml`](data/hscla_db.yaml) under `test_regions`:

| Name              | RA (deg)    | Dec (deg)   | Note                                       |
| ----------------- | ----------- | ----------- | ------------------------------------------ |
| `covered_lsbg`    | 49.2657595  | 41.2485927  | Perseus LSBG; deep multi-band HSCLA cover. |
| `uncovered_blank` | 198.1261598 | 29.5614297  | No HSCLA coverage.                         |

## Development

- `uv sync` — install runtime + dev dependencies.
- `uv run pytest -q` — run the offline test suite.
- `HSCLA_LIVE_TESTS=1 uv run pytest -q` — also run the opt-in live tests.
- `uv run ruff check` — lint the codebase (same check CI runs).
- GitHub Actions in `.github/workflows/ci.yml` run `ruff check` and the
  offline `pytest` suite on every push and PR. A separate
  `workflow_dispatch` job runs the live tests when triggered with
  `run_live=true` and the `HSCLA_USR_SECRET` / `HSCLA_PWD_SECRET`
  repository secrets are configured.
- Architecture: [`docs/SPEC.md`](docs/SPEC.md).
- Plan and phase status: [`docs/todo.md`](docs/todo.md).
- Gotchas and rationale: [`docs/lessons.md`](docs/lessons.md).
- Agent guide (for Claude Code etc.): [`CLAUDE.md`](CLAUDE.md).

`docs/journal/` is gitignored — per-session development journals stay
local.

## License

See [`LICENSE`](LICENSE).
