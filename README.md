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

The architecture lives in [`docs/SPEC.md`](docs/SPEC.md); the plan and
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

Still to come (see [`docs/todo.md`](docs/todo.md)): PSF picker /
crossmatch / direct file-tree download / CLI.

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
- Architecture: [`docs/SPEC.md`](docs/SPEC.md).
- Plan and phase status: [`docs/todo.md`](docs/todo.md).
- Gotchas and rationale: [`docs/lessons.md`](docs/lessons.md).
- Agent guide (for Claude Code etc.): [`CLAUDE.md`](CLAUDE.md).

`docs/journal/` is gitignored — per-session development journals stay
local.

## License

See [`LICENSE`](LICENSE).
