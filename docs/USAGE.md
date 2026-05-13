# Using `hscla_tool`

A task-oriented guide for both **humans** and **coding agents** working
with the HSC Legacy Archive (HSCLA2020) through this toolkit.

This file answers "**how do I X?**" with copy-pasteable recipes. It is
not a tour (see [`README.md`](../README.md)), not the architecture
(see [`docs/SPEC.md`](SPEC.md)), and not the rules of the road (see
[`CLAUDE.md`](../CLAUDE.md) for agent rules). Use this when you have a
specific task and want to know which knob to turn.

Recipes use the two regression fixtures wired into the test suite:

| Fixture           | RA (deg)        | Dec (deg)       | Note                                      |
| ----------------- | --------------- | --------------- | ----------------------------------------- |
| `covered_lsbg`    | `49.2657595`    | `41.2485927`    | Perseus LSBG; deep multi-band cover.      |
| `uncovered_blank` | `198.1261598`   | `29.5614297`    | No HSCLA coverage — exercise empty paths. |

So every snippet below is something you can run unmodified, against
the real archive, to confirm the toolkit works on your machine.

---

## Reading order

1. **This file** — task recipes + a compact reference at the bottom.
2. [`README.md`](../README.md) — narrative tour, endpoint reference,
   table catalog.
3. [`docs/SPEC.md`](SPEC.md) — architecture and contracts.
4. [`docs/ARCHIVE_LAYOUT.md`](ARCHIVE_LAYOUT.md) — observed layout of
   the HSCLA2020 direct file archive (`/archive/files/la2020/`).
   Read this before doing any bulk-download work; it documents the
   per-patch / per-visit file kinds and the 1 TB session rule.
5. [`docs/lessons.md`](lessons.md) — every HSCLA surprise we've hit so far.
6. [`data/hscla_db.yaml`](../data/hscla_db.yaml) — the machine-readable
   knowledge base (URLs, tables, server-side functions, fixtures).

Agents: also read [`CLAUDE.md`](../CLAUDE.md) before writing any code.

---

## Setup

### Install

```bash
git clone git@github.com:dr-guangtou/hscla_tool.git
cd hscla_tool
uv sync
```

This installs the package in editable mode and registers the
`hscla` console script on your `$PATH`. `uv` is mandatory — do not
mix in `pip`, `poetry`, or `conda`.

### Credentials

`hscla_tool` reads your HSCLA login from two environment variables:

| Variable     | What it is                        |
| ------------ | --------------------------------- |
| `HSCLA_USR`  | Your HSCLA / STARS account email. |
| `HSCLA_PWD`  | The matching password.            |

Put both in `~/.zshenv` (not `~/.zprofile`) so non-login shells —
`uv run`, CI, IDE tasks — inherit them. The package verifies them at
import time and fails loudly if either is missing:

```python
>>> import hscla_tool
MissingCredentialsError: HSCLA_USR and HSCLA_PWD not set. ...
```

### Optional: cache and mirror folders

| Variable             | Default                          | What it controls                                       |
| -------------------- | -------------------------------- | ------------------------------------------------------ |
| `HSCLA_TOOL_CACHE`   | `./outputs/`                     | Downloads, FITS cache, SQL CSVs, batch outputs.        |
| `HSCLA_MIRROR_ROOT`  | `/Volumes/galaxy/hsc/la2020/`    | Local Parquet copies of HSCLA metadata catalogs.       |

Set `HSCLA_TOOL_CACHE` for any non-toy workload so cutouts and SQL
results don't accumulate in your repo's `./outputs/`. Set
`HSCLA_MIRROR_ROOT` if your local mirror lives somewhere other than
the default external volume.

---

## How do I … (cookbook)

### …check if a region has HSCLA coverage?

**Library:**
```python
from hscla_tool import coverage

cov = coverage.region_coverage(49.2657595, 41.2485927, size_deg=0.03)
if not cov.covered:
    print("nothing here")
else:
    print("bands:", cov.filters)              # ('HSC-G', 'HSC-I', 'HSC-R', 'HSC-Z')
    print("patches:", len(cov.patches))
    print("mean seeing:", cov.mean_seeing_per_band)
```

**CLI:**
```bash
hscla coverage 49.2657595 41.2485927 --size-deg 0.03
```

**Pitfalls:**
- An **uncovered region returns a well-formed empty `RegionCoverage`**,
  not an exception. Use `cov.covered` (boolean) as the branch
  predicate, never `try/except`.
- Regions wrapping `RA = 0 / 360` are not supported in v0; both
  fixtures are far from the wrap.
- On a machine where you've built the local Parquet mirror, prefer
  `source='local'` — it's both faster and uses the exact patch corners
  with an RA-wrap guard rather than the server's patch-center
  proximity approximation.

### …find which patches and tracts overlap a region?

```python
cov = coverage.region_coverage(49.27, 41.25, size_deg=0.03, source='local')
for p in cov.patches:
    print(p.band, p.tract, p.patch_s, f"seeing={p.seeing:.3f}\"")
# HSC-G 15548 1,6 seeing=0.692"
# HSC-G 15548 2,6 seeing=0.689"
# HSC-I 15548 1,6 seeing=0.644"
# ...
```

Use `p.patch_s` (e.g., `"1,6"`) for archive file-tree URLs; use
`p.patch` (the integer skymap id) for SQL joins against tables that
key on the integer.

### …count visits and single-frame depth at a position?

```python
frames = coverage.frame_coverage(49.2657595, 41.2485927, size_deg=0.03)
for band in frames.filters:
    s = frames.band_summary[band]
    print(f"{band}: {s.n_frames} frames across {s.n_visits} visits")
```

Set `detailed=True` to also get the per-frame rows (one dict each).
The Perseus fixture has ~145 visits across HSC-G/R/I/Z; HSC-G alone
contributes 102.

### …download a single FITS cutout?

**Library:**
```python
from hscla_tool import cutout

c = cutout.fetch_cutout(
    ra=49.2657595, dec=41.2485927,
    size_arcsec=108.0,                # 0.03 deg box
    band="HSC-I",
    with_variance=True, with_mask=True,
)
try:
    image = c.image.data
    var = c.variance.data
    wcs = c.wcs()
finally:
    c.close()
```

**CLI:** (saves to `./outputs/cutouts/<auto>.fits` by default)
```bash
hscla cutout 49.2657595 41.2485927 --size-arcsec 108 --band HSC-I
```

**Pitfalls:**
- `size_arcsec` is the **full edge** of the square box, not a radius.
- An uncovered region raises `cutout.NoCoverageError`. The CLI
  returns exit code `2` (not 1) so loops can `continue` on no-coverage.
- FITS files are cached under `${HSCLA_TOOL_CACHE}/cutouts/<hash>.fits`.
  A re-run of the same request is a free local read. Pass
  `cache=False` to force a refetch.
- The default `kind='coadd'` applies a per-visit *local* background
  subtraction and tends to over-subtract the sky, eating
  low-surface-brightness flux. For LSB galaxy morphology / structure
  analyses, prefer `kind='coadd/bg'` — see the next recipe.

### …pick `coadd` vs `coadd/bg` for LSB morphology?

HSCLA2020 serves two coadd flavors through the same cutout endpoint;
the only difference is the **background-subtraction policy**:

| `kind`        | Background policy                                | When to use it                                            |
| ------------- | ------------------------------------------------ | --------------------------------------------------------- |
| `'coadd'`     | Per-visit local bg subtraction (default).        | Compact sources; quick QA. Tends to over-subtract sky.    |
| `'coadd/bg'`  | Full focal-plane bg correction at coadd time.    | **LSB galaxy morphology and structure** — recommended.    |

```python
c = cutout.fetch_cutout(
    ra=49.2657595, dec=41.2485927,
    size_arcsec=108.0, band="HSC-I",
    kind="coadd/bg",          # was: kind="coadd"
)
```

CLI:
```bash
hscla cutout 49.2657595 41.2485927 --size-arcsec 108 --band HSC-I --kind coadd/bg
```

Both flavors share the same HDU layout (image + mask + variance) and
the same `Cutout` result type. Confirmed live for HSC-I at the
Perseus LSBG fixture on 2026-05-13; the median pixel value differs
between the two by milli-ADU, consistent with the bg-correction
difference. Test: `tests/test_cutout.py::test_live_fetch_cutout_coadd_bg_perseus_i_band`.

> **Note for bulk users:** the `calexp` files in the direct file
> archive (`/archive/files/la2020/deepCoadd-results/<F>/<T>/<P>/`) are
> the **`coadd`** flavor (bit-identical at the pixel level), not
> `coadd/bg`. If you need bg-corrected coadd pixels for LSB work, you
> must go through the DAS cutout service with `kind='coadd/bg'`. See
> [`docs/ARCHIVE_LAYOUT.md`](ARCHIVE_LAYOUT.md#calexp-is-the-coadd-flavor-not-coaddbg)
> for the verification and the residual figure.

The cache key includes `kind`, so a `coadd` cutout and a `coadd/bg`
cutout at the same (ra, dec, band, size) live in different cached
FITS files — no risk of one overwriting the other.

### …decode the mask plane into named bits?

```python
c = cutout.fetch_cutout(49.27, 41.25, size_arcsec=108.0, band="HSC-I")
try:
    planes = c.mask_planes()           # dict[str, np.ndarray of bool]
    bad = planes["BAD"]                # 2D bool array, same shape as image
    saturated = planes["SAT"]
    # HSCLA2020 ships these in every coadd cutout:
    # BAD, SAT, INTRP, CR, EDGE, DETECTED, DETECTED_NEGATIVE, SUSPECT,
    # NO_DATA, BRIGHT_OBJECT, CROSSTALK, NOT_DEBLENDED, UNMASKEDNAN,
    # CLIPPED, REJECTED, SENSOR_EDGE, INEXACT_PSF
finally:
    c.close()
```

If you fetched without `with_mask=True`, `mask_planes()` raises
`CutoutError`. The mask itself comes from the integer HDU; we use
`hdu.data.dtype.kind in ("i","u")` to identify it because HSCLA does
not set `EXTNAME`.

### …fetch a PSF kernel at a position?

**Library:**
```python
from hscla_tool import psf

p = psf.fetch_psf(ra=49.2657595, dec=41.2485927, band="HSC-I")
try:
    kernel = p.array                    # 41x41 float64, normalized to sum 1.0
    assert abs(kernel.sum() - 1.0) < 1e-3
finally:
    p.close()
```

**CLI:**
```bash
hscla psf 49.2657595 41.2485927 --band HSC-I
```

**Pitfalls:**
- The PSF FITS has a **linear pixel WCS under alternate key `'A'`**
  (CTYPE1A, CRPIX1A, …), not under the primary key. `p.wcs()` tries
  primary first and falls back to `'A'`.
- The same `NoCoverageError` from `cutout` is re-exported and raised
  on uncovered regions.

### …download many cutouts in one round-trip?

The DAS cutout multipart form accepts up to **990 rows per POST**. Use
`fetch_cutouts` (plural) for any workflow where you have a list of
(ra, dec, band) targets.

**Library — DataFrame input:**
```python
import pandas as pd
from hscla_tool import cutout

requests = pd.DataFrame({
    "ra":          [49.27, 49.28, 198.0],   # row 2 is uncovered
    "dec":         [41.25, 41.25, 29.5],
    "size_arcsec": [108.0, 108.0, 108.0],
    "band":        ["HSC-I", "HSC-R", "HSC-I"],
})
result = cutout.fetch_cutouts(requests)
print(f"{result.n_success}/{len(result)} ok; {result.n_failure} no coverage")
for c in result.cutouts:                    # parallel to input rows
    if c is None:
        continue
    image = c.image.data
result.close()
```

**Library — list input:**
```python
reqs = [
    cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I"),
    cutout.CutoutRequest(ra=49.28, dec=41.25, size_arcsec=108.0, band="HSC-R"),
]
result = cutout.fetch_cutouts(reqs)
```

**CLI:**
```bash
hscla cutouts inputs.csv                    # columns: ra, dec, size_arcsec, band
```

**Pitfalls:**
- `result.cutouts` is **parallel to the input rows**: `None` in a slot
  means that row had no coverage (or some other per-row failure).
  `result.failures` lists `(input_row_index, exception)` for every
  `None` slot.
- Whole-batch failures (HTTP error, oversized batch, malformed
  request) raise `CutoutError`. Only no-coverage rows go in
  `failures`; everything else aborts the batch.
- The cache is shared with the single-row path: rows that were
  already cached from prior `fetch_cutout(...)` calls cost zero HTTP
  traffic. Re-running the same batch sends zero POSTs.
- Batch sizes over `cutout.MAX_BATCH_ROWS` (990) raise before any
  HTTP. Split bigger inputs into chunks.

### …download a whole patch FITS (coadd image or forced catalog)?

> **!!! 1 TB session limit.** The direct file archive enforces an
> upstream policy: any download session over **1 TB** must be
> coordinated with the NAOJ team at `hscla-contact@ml.nao.ac.jp`
> first. Plan budgets per filter × patch × file-kind before pulling.
> [`docs/ARCHIVE_LAYOUT.md`](ARCHIVE_LAYOUT.md) carries the typical
> file sizes.


```python
from hscla_tool import archive

# Coadd image (~136 MB for HSC-I tract 15548 patch 1,6):
out = archive.download_coadd_image(tract=15548, patch="1,6", band="HSC-I")
print(out.path, out.bytes)

# Forced catalog FITS for the same patch:
forced = archive.download_forced_catalog(tract=15548, patch="1,6", band="HSC-I")

# Any of the nine per-patch file kinds:
ran = archive.download_patch_file(
    tract=15548, patch="1,6", band="HSC-I", kind="ran",
)
```

**CLI:**
```bash
hscla archive list 15548 1,6 HSC-I            # what files exist?
hscla archive download 15548 1,6 HSC-I --kind calexp
```

Files cache under `${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/`
mirroring the upstream layout. **Downloads are resumable** via HTTP
`Range:` requests — an interrupted run leaves a `.tmp` file and the
next call picks up where it stopped.

The nine supported `kind` values are `calexp`, `forced_src`, `meas`,
`deblendedFlux`, `det`, `det_bkgd`, `ran`, `srcMatch`, `srcMatchFull`.

### …run an arbitrary SQL query?

Two flavors — pick by query size, not by syntax:

**Fast lookups (≤ 5 s server timeout):**
```python
from hscla_tool import sql

result = sql.preview_sql("SELECT 1 AS one")
print(result["fields"], result["rows"])

# Useful for information_schema, COUNT(*), small joins:
schema = sql.preview_sql(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='la2020' ORDER BY table_name"
)
```

**Real catalog queries (full submit / poll / download):**
```python
df = sql.run_sql(
    "SELECT object_id, ra, dec, i_cmodel_mag "
    "FROM la2020.forced "
    "WHERE coneSearch(coord, 49.27, 41.25, 60.0) "
    "  AND isprimary LIMIT 1000"
)
print(df.shape, df.head())
```

**CLI:**
```bash
hscla sql "SELECT COUNT(*) FROM la2020.mosaic" --preview
hscla sql --file my_query.sql                       # full pipeline
```

**Pitfalls:**
- `preview_sql` enforces a **~5 s server-side timeout**. Anything
  touching `forced` cone searches will time out via preview. Use it
  only for schema lookups and counts.
- `run_sql` results are cached by **content hash of the SQL text**
  under `${HSCLA_TOOL_CACHE}/sql/<hash>.csv`. The same query re-run
  is a free local read.
- The HSCLA CSV format **always prefixes its header line with `# `**,
  even with `include_metainfo_to_body=False`. The parser handles
  both shapes automatically.

### …crossmatch a small catalog against HSCLA?

> **HSCLA crossmatch is slow on the server side — currently
> 30–45 minutes for a few-row input.** The infrastructure is correct
> (the live test passes), but in practice you should treat the module
> as a placeholder until NAOJ improves the service or until we ship a
> local-mirror crossmatch.

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
    nearest_only=False,
)
# Columns: match_input_id, match_ra, match_dec, object_id, match_distance,
#          i_cmodel_mag, i_cmodel_magerr.
```

**CLI:**
```bash
hscla crossmatch inputs.csv --radius-arcsec 1.0 --nearest-only
```

The CLI prints a slow-server warning banner to stderr before submitting.

### …make coverage queries fast (and exact) by mirroring locally?

`coverage.region_coverage` / `frame_coverage` both accept
`source='local'`, which reads a Parquet copy of the small HSCLA
metadata tables instead of round-tripping through SQL. The local path
is faster *and* more accurate — it uses the real patch corners (with
an RA-wrap guard) instead of the server's patch-center proximity
filter.

**One-time setup:**
```bash
uv run python -m hscla_tool.mirror status            # what's already on disk?
uv run python -m hscla_tool.mirror build mosaic      # ~1 min
uv run python -m hscla_tool.mirror build frame       # ~5–15 min, ~1.4 GB
```

**Then in code:**
```python
cov = coverage.region_coverage(49.27, 41.25, size_deg=0.03, source='local')
```

**CLI:** the `--source` flag flips it for any region call.
```bash
hscla coverage 49.27 41.25 --size-deg 0.03 --source local
```

Mirrors live at `${HSCLA_MIRROR_ROOT}/<table>.parquet`. If the volume
isn't mounted, `source='local'` raises `MirrorRootMissing` with a
clear hint; the public defaults stay `source='server'` so the toolkit
works on machines without a local mirror.

### …handle "no coverage" cleanly across the toolkit?

The contract is uniform:

- **Query-like calls** (coverage, SQL) return a well-formed empty
  result and **never raise** on absence of data.
  - `coverage.region_coverage` → `RegionCoverage` with
    `filters=()`, `covered=False`.
  - `coverage.frame_coverage` → analogous `FrameCoverage`.
  - `sql.run_sql` → empty DataFrame.
- **Fetch-like calls** (cutout, PSF) raise the typed
  `cutout.NoCoverageError` (also re-exported from `psf`).
  - `cutout.fetch_cutout` → raises.
  - `psf.fetch_psf` → raises.
- **Batch fetch** (`fetch_cutouts`) **does not raise** for individual
  no-coverage rows. Instead, the per-row slot in `result.cutouts` is
  `None` and `result.failures` carries `(idx, NoCoverageError)`.

Recommended branching:

```python
try:
    c = cutout.fetch_cutout(ra, dec, size_arcsec=108.0, band="HSC-I")
except cutout.NoCoverageError:
    # Expected outcome for some regions; not a bug.
    continue
```

For the CLI: exit code **2** means "no coverage" specifically.
Anything else is a real failure (3 = missing credentials, 4 = mirror
missing, 5 = fetch failure, 6 = bad args).

### …make a long script cheap to re-run?

Every fetch in this toolkit caches its result by a SHA-256 hash of the
request tuple. Concretely:

| Module       | Cache directory                                  | Key                                                       |
| ------------ | ------------------------------------------------ | --------------------------------------------------------- |
| `sql`        | `${HSCLA_TOOL_CACHE}/sql/<hash>.{csv,csv.gz}`    | SQL text + release + format                               |
| `cutout`     | `${HSCLA_TOOL_CACHE}/cutouts/<hash>.fits`        | (ra, dec, size, band, kind, tract, with_mask, with_var, rerun) |
| `psf`        | `${HSCLA_TOOL_CACHE}/psfs/<hash>.fits`           | (ra, dec, band, kind, tract, patch, centered, rerun)      |
| `archive`    | `${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/<kind>-...fits` | layout-mirrored (not hashed)                              |
| `crossmatch` | `${HSCLA_TOOL_CACHE}/sql/<hash>.csv`             | same as `sql.run_sql` (built on top of it)                |

Side effects:
- Re-running the same script reads from the cache.
- The `fetch_cutouts` batch path shares the **same content-hash cache**
  as `fetch_cutout` — rows already cached from prior single-row calls
  are free.
- Pass `cache=False` to force a fresh fetch.

### …troubleshoot a credentials problem?

`import hscla_tool` fails with `MissingCredentialsError`:

1. Check both vars are present in the current shell:
   ```bash
   echo "$HSCLA_USR" "$HSCLA_PWD" | wc -c       # > 1 if both set
   ```
2. If unset, edit `~/.zshenv` (not `~/.zprofile`) so non-login shells
   inherit them, then open a new terminal or `source ~/.zshenv`.
3. CI: set the variables in the workflow env (placeholders are fine
   for offline tests; real secrets only for the live-tests job).

`hscla` CLI returns exit code **3** for this case, with a friendly
hint at `~/.zshenv` rather than a Python traceback.

### …troubleshoot a missing local-mirror volume?

`coverage.region_coverage(..., source='local')` (or any other local
read) raises `config.MirrorRootMissing`:

1. Check the volume is mounted: `ls /Volumes/galaxy/hsc/la2020/`
2. If you keep mirrors elsewhere, set `HSCLA_MIRROR_ROOT` to that path.
3. If you have not built the mirror yet:
   ```bash
   uv run python -m hscla_tool.mirror build mosaic    # ~1 min
   uv run python -m hscla_tool.mirror build frame     # ~5–15 min
   ```

`hscla` CLI returns exit code **4** for this case.

---

## For coding agents

If you are an agent picking this codebase up cold:

1. **First read** [`CLAUDE.md`](../CLAUDE.md). It is the contract:
   never work on `main`, English only, `uv` only, `snake_case` only,
   credentials never logged, **probe every new HSCLA endpoint live
   before writing code**, log every surprise to
   [`docs/lessons.md`](lessons.md).
2. **Find the structured truth** in
   [`data/hscla_db.yaml`](../data/hscla_db.yaml) before scraping any
   web docs. URLs, table inventory, server-side `WHERE` helpers, the
   `release_version_token` mapping, test fixtures — all there.
3. **Use these recipes** as the default starting point. If a recipe
   doesn't fit, prefer modifying the closest existing module to
   inventing a new one. Each module's failure modes are
   well-understood; new code paths cost lessons-learned cycles.

A few decision rules that came up repeatedly while building this
toolkit:

| Question | Answer |
| --- | --- |
| One position or many? | One → `fetch_cutout`/`fetch_psf`. Many → `fetch_cutouts` (batch). |
| Fast query or real query? | `preview_sql` for `information_schema`/`COUNT(*)` (≤ 5 s timeout). `run_sql` for everything else. |
| Server or local coverage? | On a machine with the Parquet mirror, default `source='local'`; it is strictly more accurate. Otherwise `source='server'`. |
| Empty result or error? | Coverage / SQL → empty result, never raise. Cutout / PSF → raise `NoCoverageError`. Batch cutouts → `None` slot + entry in `failures`. |
| Where do downloaded files go? | `${HSCLA_TOOL_CACHE}` (default `./outputs/`) for cutouts / PSFs / SQL CSVs. `${HSCLA_MIRROR_ROOT}` (default `/Volumes/galaxy/hsc/la2020/`) for the metadata Parquet mirrors. |

The "Expect surprises" meta-rule from `CLAUDE.md` is non-negotiable:
**before writing any module that talks to a new HSCLA endpoint, probe
the live service with the smallest legal request** (curl is fine,
`requests` is fine, a Python one-liner is fine — record the auth
shape, the wire format, the "no result" signal, and any field/HDU
naming). Then write the module. Then log any surprise in
[`docs/lessons.md`](lessons.md) with a dated entry. The eight
recorded inconsistencies in HSCLA's services are catalogued in
[`CLAUDE.md`](../CLAUDE.md).

---

## Reference appendix

### Library modules

Importing the package verifies credentials at import time. All public
APIs that hit the server accept a `client=` keyword to reuse an
existing logged-in session.

| Module                     | Public entry points                                                                                                          | Default cache                          |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `hscla_tool.config`        | `load_credentials`, `cache_dir`, `mirror_root`                                                                               | —                                      |
| `hscla_tool.db`            | `get_table`, `list_tables`, `get_fixture`, `list_fixtures`, `get_release_version_token`, `get_sql_api`, `get_cutout_api`, `get_psf_api`, `get_where_clause_functions` | —                                      |
| `hscla_tool.sql`           | `preview_sql`, `run_sql`, `HscLaClient`, `Job`, `JobError`, `JobTimeout`, `SqlError`                                          | `${HSCLA_TOOL_CACHE}/sql/`             |
| `hscla_tool.coverage`      | `region_coverage`, `frame_coverage`, `RegionCoverage`, `FrameCoverage`, `PatchInfo`, `BandFrameSummary`                       | reads `mirror.load_mirror(...)` when `source='local'` |
| `hscla_tool.mirror`        | `build_mirror`, `load_mirror`, `is_mirrored`, `mirror_path`, `SUPPORTED_TABLES`, `MirrorError`                                | `${HSCLA_MIRROR_ROOT}/<table>.parquet` |
| `hscla_tool.cutout`        | `fetch_cutout`, `fetch_cutouts`, `HscLaCutoutClient`, `Cutout`, `CutoutRequest`, `BatchResult`, `NoCoverageError`, `CutoutError`, `MAX_BATCH_ROWS` | `${HSCLA_TOOL_CACHE}/cutouts/`         |
| `hscla_tool.mask`          | `decode`, `parse_mask_planes`                                                                                                | —                                      |
| `hscla_tool.psf`           | `fetch_psf`, `HscLaPsfClient`, `Psf`, `PsfError`                                                                              | `${HSCLA_TOOL_CACHE}/psfs/`            |
| `hscla_tool.crossmatch`    | `match`, `CrossmatchError`                                                                                                   | inherits `sql.run_sql` cache           |
| `hscla_tool.archive`       | `download_patch_file`, `download_coadd_image`, `download_forced_catalog`, `HscLaArchiveClient`, `SUPPORTED_KINDS`, `ArchiveError`, `ArchiveFile` | `${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/` |

### CLI subcommands

Run `hscla <command> --help` for the full options. Friendly progress
lines go to **stderr** so stdout stays pipe-friendly; pass `--quiet`
to drop the progress chatter.

| Subcommand            | What it does                                                                            | Default output                                                  |
| --------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `hscla coverage`      | Bands and patches overlapping a region.                                                 | Stdout summary.                                                 |
| `hscla frames`        | Per-band single-CCD frame and visit counts.                                             | Stdout summary; `--detailed` for per-frame rows.                |
| `hscla cutout`        | Fetch one FITS cutout.                                                                  | `./outputs/cutouts/<auto>.fits`; `--out` to override.           |
| `hscla cutouts`       | Bulk: fetch many cutouts from a CSV/Parquet of `(ra, dec, size_arcsec, band)` rows.     | `./outputs/cutouts/` (one named file per row).                  |
| `hscla psf`           | Fetch one PSF kernel.                                                                   | `./outputs/psfs/<auto>.fits`; `--out` to override.              |
| `hscla sql`           | Run a query (`--preview` for fast, default for full submit/poll/download).              | `./outputs/sql/<auto>.csv`; stdout for `--preview`.             |
| `hscla crossmatch`    | Crossmatch a small CSV input. Prints slow-server warning.                                | `./outputs/crossmatch/<auto>.csv`.                              |
| `hscla mirror build`  | Build the local Parquet mirror for one of `mosaic`, `frame`, `mosaicframe`, `wcs`.       | `${HSCLA_MIRROR_ROOT}/<table>.parquet`.                         |
| `hscla mirror status` | List which mirrors exist on disk and their sizes.                                       | Stdout.                                                         |
| `hscla archive list`  | List file names available for one `(tract, patch, band)`.                               | Stdout, one name per line.                                      |
| `hscla archive download` | Download one per-patch FITS by `--kind` (default `calexp`).                          | Layout-mirrored cache under `${HSCLA_TOOL_CACHE}/archive/`.     |

### CLI exit codes

| Code | Meaning                                                                |
| ---- | ---------------------------------------------------------------------- |
| `0`  | Success.                                                               |
| `2`  | No HSCLA coverage at the requested region (use this to `continue` past empty rows). |
| `3`  | Missing or invalid credentials.                                        |
| `4`  | Missing local mirror (volume not mounted, mirror not built).           |
| `5`  | Fetch failure (HTTP error, malformed response).                        |
| `6`  | Bad command-line arguments.                                            |
| `130`| Ctrl-C / interrupted.                                                  |

### Environment variables

| Variable             | Default                          | Notes                                                  |
| -------------------- | -------------------------------- | ------------------------------------------------------ |
| `HSCLA_USR`          | *(required)*                     | HSCLA account email.                                   |
| `HSCLA_PWD`          | *(required)*                     | HSCLA account password.                                |
| `HSCLA_TOOL_CACHE`   | `./outputs/`                     | Where cutouts, PSFs, SQL CSVs, batch outputs go.       |
| `HSCLA_MIRROR_ROOT`  | `/Volumes/galaxy/hsc/la2020/`    | Where local Parquet mirrors live.                      |
| `HSCLA_LIVE_TESTS`   | *(unset)*                        | Set to `1` to enable the network-touching tests.       |

### Test fixtures

Defined in [`data/hscla_db.yaml`](../data/hscla_db.yaml) under
`test_regions`. Every new module's tests must exercise both.

| Fixture           | RA (deg)        | Dec (deg)       | Box size (deg) | Purpose                          |
| ----------------- | --------------- | --------------- | -------------- | -------------------------------- |
| `covered_lsbg`    | `49.2657595`    | `41.2485927`    | `0.03`         | Multi-band Perseus coverage.     |
| `uncovered_blank` | `198.1261598`   | `29.5614297`    | `0.03`         | Exercise empty / no-coverage path. |

Load programmatically:
```python
from hscla_tool import db
covered = db.get_fixture("covered_lsbg")
print(covered["ra_deg"], covered["dec_deg"], covered["box_size_deg"])
```

### Where each surprise is recorded

The HSCLA endpoints disagree with each other in eight documented ways
(auth schemes, success-vs-empty signals, header quirks, WCS keys,
etc.). The canonical list lives in [`CLAUDE.md`](../CLAUDE.md) under
"Expect surprises from the HSCLA server"; the per-incident detail
lives in [`docs/lessons.md`](lessons.md). If you hit a new surprise,
add a dated entry to `docs/lessons.md` before fixing the code.
