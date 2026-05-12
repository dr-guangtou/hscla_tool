# `hscla_tool` Architecture Spec

This is the source of truth for what `hscla_tool` is, what it must do, and
how its pieces fit together. Code and tests must agree with this document;
when reality and the spec disagree, update the spec first, then the code.

## 1. Mission

`hscla_tool` is two things at once:

1. **A knowledge base** for the HSC Legacy Archive (HSCLA), especially
   HSCLA2020. It exposes the archive's structure (catalogs, file layout,
   endpoints, conventions, example SQL) in a form that both humans and
   coding agents can consume without re-reading raw web pages.
2. **A Python toolkit and CLI** that wraps HSCLA access so a user — or an
   agent acting on the user's behalf — can ask region-based questions
   ("is this RA/Dec covered? in what bands? give me an i-band cutout
   with variance and mask, and a PSF model at this location") and get
   correct, reproducible answers.

It is explicitly **not** a science-analysis pipeline. Catalog modeling,
sample selection, and photometric analysis live downstream (e.g., in
`hsc_sandbox`).

## 2. Non-goals

- We do not re-implement `hscPipe`.
- We do not host or mirror HSCLA data. Outputs are local caches keyed by
  query, not a permanent archive.
- We do not abstract away the difference between HSCLA and HSC-SSP PDR.
  This package targets HSCLA. The same patterns may extend later, but
  multi-release abstraction is out of scope for v0.

## 3. User stories (drive every module)

Given a coordinate or region, a user can:

- **U1 Coverage.** Ask whether HSCLA covers it, and in which bands.
- **U2 Provenance.** List the `mosaic` rows (coadd patches) and `frame`
  rows (single-CCD exposures) that overlap the region.
- **U3 Cutout.** Fetch a FITS cutout in a chosen band, with matching
  variance and mask planes.
- **U4 Mask.** Decode the maskbit plane into named mask bits.
- **U5 PSF.** Fetch a PSF model at a specific (RA, Dec) in a chosen band.
- **U6 Crossmatch.** Cross-match a small object list against HSCLA and
  return matched catalog rows.
- **U7 SQL.** Run an arbitrary SQL query (with cone/box helpers) and
  receive the result as a pandas/`astropy.table` table.
- **U8 Bulk.** Download an entire patch's coadd image or forced catalog
  directly from the archive file tree.

Two regression fixtures cover every user story:

- A covered region in Perseus (low-surface-brightness galaxy) at
  `RA = 49.2657595, Dec = 41.2485927`, box `0.03 deg`.
- An uncovered region at `RA = 198.1261598, Dec = 29.5614297`.

Both are encoded in `data/hscla_db.yaml` under `test_regions`.

## 4. Module layout (target)

```
hscla_tool/
  __init__.py        # public API + __version__
  config.py          # env-var credentials, cache paths, base URLs
  db.py              # load data/hscla_db.yaml + small helper lookups
  coverage.py        # U1, U2: region -> RegionCoverage / FrameCoverage
                     #   server: patch-center proximity (mosaic has no coord
                     #     column the spatial helpers accept);
                     #   local : exact corner-AABB overlap on the Parquet
                     #     mirror, with an RA=0 wrap guard.
  mirror.py          # Build + load local Parquet copies of HSCLA metadata
                     # catalogs (mosaic, frame, mosaicframe, wcs). Used by
                     # the local branch of coverage.
  cutout.py          # U3, U4: FITS cutout (image+variance+mask) via DAS cutout
  mask.py            # U4: maskbit-plane decoding (named planes)
  psf.py             # U5: PSF model retrieval via PSF picker
  sql.py             # U7: HSC SQL client (adapted from hsc_sandbox/step1/sql_query.py)
  crossmatch.py      # U6: server-side crossmatch wrapper
  archive.py         # U8: direct file-tree downloads (coadd image/catalog)
  cli.py             # `hscla` console entry point
data/
  hscla_db.yaml      # knowledge base (URLs, tables, conventions, fixtures)
  schemas/           # cached SQL schema dumps per table (populated later)
docs/
  SPEC.md            # this file
  todo.md            # phased plan and acceptance criteria
  lessons.md         # mistakes and rationale, updated as we go
tests/
  test_<module>.py   # one file per module, exercises both fixture regions
```

The boundary between modules is the **kind of HSCLA endpoint** they talk
to (catalog DB vs DAS cutout vs PSF picker vs file tree). That mirrors
the archive's own division and keeps failure modes isolated.

## 5. Cross-cutting design rules

### 5.1 Credentials
- Read from env vars `HSCLA_USR` and `HSCLA_PWD` (see `~/.zprofile`).
- The check runs **at import time**: `import hscla_tool` fails with
  `MissingCredentialsError` if either variable is unset. This is a
  deliberate choice — we want setup problems caught immediately, even
  at the cost of not being able to import the package on a machine
  with no HSCLA login.
- Never echo to logs. Never persist to disk.
- `config.py` is the only module that reads them; everything else asks
  `config.get_credentials()` and gets a small immutable container.

### 5.2 Where downloaded files go
- For development and tests: default folder is `./outputs/` inside the
  repo. It is in `.gitignore`.
- For larger production runs: there is **no built-in default**. The
  user must either set the env var `HSCLA_TOOL_CACHE` or pass an
  explicit path to the relevant function. We refuse to silently fill
  someone's home directory with gigabytes of FITS files.
- Resolution order inside the tool:
  `explicit argument` > `HSCLA_TOOL_CACHE` env var > `./outputs/`.
- Cache keys are derived from the request, not the timestamp, so
  repeat calls are deterministic.

### 5.2a Where local catalog mirrors go
- Whole-table Parquet mirrors of `mosaic` / `frame` / `mosaicframe` /
  `wcs` live under `config.mirror_root()`.
- Resolution order: `explicit argument` > `HSCLA_MIRROR_ROOT` env var
  > `/Volumes/galaxy/hsc/la2020/` (default external volume).
- One Parquet file per table, named `<table>.parquet`. Built with
  `uv run python -m hscla_tool.mirror build <table>`.
- The bulk photometry tables (`forced`, `meas`, and their detail
  variants) are **not** part of the mirror layer; they need a
  different strategy (per-tract files, partitioned by band) and that
  belongs in a separate module when we get to it.

### 5.3 Failure modes
- Missing credentials -> raise a single typed error, message tells the
  user which env var is missing.
- No HSCLA coverage in the requested region -> return an empty result
  object (`Coverage(filters=[], mosaics=[], frames=[])`), never raise.
  This is what the uncovered fixture exercises.
- HTTP/SQL errors -> wrap into typed exceptions defined alongside the
  module that issued the call.

### 5.4 Units and conventions
- Sky positions: degrees, ICRS, unless explicitly stated.
- Fluxes: nanojansky (the HSCLA native unit).
- Sizes/radii: degrees for boxes, arcsec for cone radii (matches
  `coneSearch`/`boxSearch` server functions).
- All public function parameters use these units; conversions happen at
  the boundary only.

### 5.5 Naming
- snake_case everywhere. No camelCase, even when mirroring upstream
  HSC names — translate at the boundary.

### 5.6 How we read the knowledge base
- `data/hscla_db.yaml` is loaded once, with strict checks: every table
  has a description and a `kind`; every fixture has `ra_deg`, `dec_deg`,
  and a description; every recorded URL parses as `http(s)://...`. A
  bad YAML file is a loud failure, not a silent partial load.
- Code does not poke directly at the raw dictionary. It calls helper
  functions in `db.py` (`get_table`, `get_fixture`, `get_tool_url`,
  `list_tables`, `list_fixtures`). The helpers return plain dicts; we
  do not wrap things in classes unless there's a real reason.

## 6. Data flow examples

### 6.1 Covered region (Perseus LSBG)
```
ra, dec = 49.2657595, 41.2485927
1. coverage.region_coverage(ra, dec, size_deg=0.03)
   -> queries la2020.mosaic via sql.HscSqlClient with boxSearch(...)
   -> returns Coverage with filters=['g','r','i','z','y'] (expected)
2. cutout.fetch_cutout(ra, dec, size_arcsec=108, band='i',
                      with_variance=True, with_mask=True)
   -> POSTs to DAS cutout, caches FITS, returns Cutout(image, var, mask)
3. mask.decode(cutout.mask) -> dict[str, np.ndarray] of named planes
4. psf.fetch_psf(ra, dec, band='i') -> PSF model as 2D array + FITS path
```

### 6.2 Uncovered region
```
ra, dec = 198.1261598, 29.5614297
1. coverage.region_coverage(ra, dec, size_deg=0.03)
   -> Coverage(filters=[], mosaics=[], frames=[])  # well-formed empty
2. cutout.fetch_cutout(...) -> raises NoCoverageError (a typed, expected
   error, easy to catch in user code)
```

## 7. Dependencies (intended minimum)

- `astropy`           — FITS I/O, WCS, units, coordinates
- `numpy`             — arrays
- `pandas`            — tabular SQL results, schema caches
- `pyyaml`            — load `hscla_db.yaml`
- `requests`          — HTTP (cutout, PSF, file-tree downloads)
- `tqdm`              — progress bars for bulk download
- dev: `pytest`, `ruff`, `pre-commit`

Package management: **`uv` only**. No pip/poetry/conda invocations
should appear anywhere in the repo.

## 8. Reference material (do not duplicate, do read first)

- `data/hscla_db.yaml` — every URL, table, and fixture lives here.
- `~/Dropbox/work/project/otters/hsc_sandbox/step1/python/sql_query.py`
  — the modern HSC SQL client we will port and adapt for HSCLA.
- `~/Dropbox/work/project/otters/hsc_sandbox/step1/python/fetch_schema.py`
  — pattern for introspecting `information_schema.columns`.
- `https://github.com/dr-guangtou/unagi` — prior art for PSF/cutout/SQL
  flows in the PDR context; do not copy verbatim.
- Upstream NAOJ scripts under `la2020/` in `data-access-tools` — final
  authority on endpoint payloads.

## 9. Versioning

`hscla_tool` is pre-1.0. Until we hit `1.0.0` the public API is allowed
to change between minor versions. Acceptance for 1.0:

- All eight user stories (U1–U8) work end-to-end against both fixture
  regions.
- `data/hscla_db.yaml` is regenerated from the live archive or
  independently audited at least once.
- CI runs `ruff` and the pytest suite on every push.
