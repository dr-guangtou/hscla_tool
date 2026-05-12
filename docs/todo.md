# `hscla_tool` Plan

Phased roadmap. Each phase has explicit acceptance criteria tied to the
two fixture regions defined in `data/hscla_db.yaml` (`test_regions`):

- `covered_lsbg`  : RA=49.2657595, Dec=41.2485927, box=0.03 deg
- `uncovered_blank`: RA=198.1261598, Dec=29.5614297

Mark items with `[x]` when complete. Add a brief "Review" block at the
end of each phase summarizing what we learned.

---

## Phase 0 — Repo scaffold *(in progress)*

- [x] Cut `scaffold/init` branch.
- [x] Author `data/hscla_db.yaml` (machine-readable knowledge base).
- [x] Author `docs/SPEC.md` (architecture source of truth).
- [x] Author `docs/todo.md` (this file).
- [ ] Author `CLAUDE.md` (repo-level agent context).
- [ ] Author `pyproject.toml` (`uv` managed) + empty `hscla_tool/` package.
- [ ] Seed `docs/lessons.md`.
- [ ] Commit scaffold (await user permission per global rule).

**Acceptance.** `uv sync` succeeds on a clean checkout, `python -c "import hscla_tool; print(hscla_tool.__version__)"` works, and a new agent dropped into the repo can answer "where do I read the archive's structure?" from `data/hscla_db.yaml` alone.

---

## Phase 1 — Login, downloads folder, knowledge-base loader

Goal: a tiny, testable foundation that everything else builds on.

- [x] `hscla_tool/config.py`: read `HSCLA_USR`/`HSCLA_PWD` from env, expose `Credentials` dataclass, raise `MissingCredentialsError` when missing. Also resolves the downloads folder (explicit arg > `HSCLA_TOOL_CACHE` env > `./outputs/`).
- [x] Wire the credential check into `hscla_tool/__init__.py` so `import hscla_tool` fails loudly when the env vars are missing.
- [x] `hscla_tool/db.py`: load `data/hscla_db.yaml` once, strict validation on load, helper lookups (`get_table`, `list_tables`, `get_fixture`, `list_fixtures`, `get_tool_url`, `get_command_line_tool`, `get_where_clause_functions`).
- [x] Unit tests for both modules (24 tests, all offline).
- [x] `uv sync` clean, `uv run pytest -q` green.

**Done check.**
- Tests pass with no network access (24/24).
- `db.get_fixture('covered_lsbg')` returns RA, Dec, box size.
- Missing env vars give one clear error: tells you which var is missing and points at `~/.zprofile`.

### Review (2026-05-12)
- The "fail at import time" rule means the user's shell must already
  have `HSCLA_USR` / `HSCLA_PWD` set. They used to live in `~/.zprofile`,
  which only loads for login shells. They are now in `~/.zshenv`, which
  every zsh shell sources — so `uv run`, CI, and IDE tasks all see them.
  Details in `docs/lessons.md`.
- Pyright complaints about `from hscla_tool import config` disappear
  after `uv sync` populates the editable install; not a real issue.

---

## Phase 2 — SQL client *(done)*

Goal: get arbitrary SQL queries working against HSCLA2020.

- [x] Live-probed HSCLA endpoints: login at `/account/api/session` (POST `{email,password}`, returns `LAAUTH_SESSION` cookie), API at `/datasearch/api/catalog_jobs/` with `submit`/`status`/`download`/`delete`/`cancel`/`preview` suffixes.
- [x] Confirmed the wire-format `release_version` token is `hscla2020` (not the short `la2020`); recorded in the knowledge base as `releases.la2020.release_version_token` and exposed via `db.get_release_version_token()`.
- [x] Wrote `hscla_tool/sql.py` with `HscLaClient` (session-cookie login, `preview_sql`, `submit_sql`, `job_status`, `wait_for_job`, `download_job`, `delete_job`, `cancel_job`, `run_sql`) plus module-level `run_sql` and `preview_sql` shortcuts.
- [x] `run_sql` defaults to `csv`, caches results under `${HSCLA_TOOL_CACHE}/sql/<hash>.csv`, deletes the job server-side after download.
- [x] CSV parser handles both metainfo modes (off → one `#`-prefixed header line; on → several `#` lines, last one is the header).
- [x] Tests: 13 offline unit tests with a fake `requests.Session`, plus one live test gated by `HSCLA_LIVE_TESTS=1`. Full suite: 37 passed, 1 skipped.
- [x] End-to-end smoke test against the real server (submit → poll → download → DataFrame) succeeded.

### Review (2026-05-12)
- The HSCLA SQL service uses **session-cookie auth** (login → `LAAUTH_SESSION` → reuse), which is a hard break from the HSC-SSP PDR pattern that the `hsc_sandbox/step1/sql_query.py` was built around. Catalog-job endpoints still want the credential in the body too, and a `clientVersion` float. Recorded in `sql_api` in the YAML and in lessons.
- `preview` enforces a ~5 s server-side timeout — anything touching `forced` table cone searches will time out via preview. So `preview_sql` is *only* for fast metadata lookups, and the full submit/poll path is needed for real catalog queries.
- The live schema has **28 tables**, including some not in `README.md`: `forced_conv_flag`, `meas_conv_flag`, `mosaicframe`, `warped`, `wcs`, `frame_hpx11`, `mosaic_hpx11`, `warped_hpx11`, `random`. Added to `hscla_db.yaml` with short descriptions.

---

## Phase 3 — Coverage and provenance (U1, U2) *(done)*

- [x] `coverage.region_coverage(ra, dec, *, size_deg=0.0)` queries `la2020.mosaic` and returns a `RegionCoverage` with sorted filters, per-patch `PatchInfo` rows (band / tract / patch / patch_s / skymap_id / ra2000 / dec2000 / seeing), and per-band mean seeing.
- [x] `coverage.frame_coverage(ra, dec, *, size_deg=0.0, detailed=False)` queries `la2020.frame` and returns a `FrameCoverage` with band-level summary `(n_frames, n_visits)`; `detailed=True` also returns one dict per frame.
- [x] Both functions return well-formed empty results for uncovered regions; `Coverage.covered` is a simple bool flag.
- [x] Eight offline tests (stubbed `preview_sql`) + three live tests gated by `HSCLA_LIVE_TESTS=1`. Full suite: 45 passed + 4 gated-live (also passed when run with the flag).

### Review (2026-05-12)
- The originally planned `boxSearch` approach **doesn't work for `mosaic`**: the server's `coneSearch` / `boxSearch` operate on `coord`-typed columns (present in `forced` / `meas`), and on `mosaic.areacube` they return 0 every time. The naive `LEAST/GREATEST` corner envelope is wrong near the RA=0 wrap: it returned three "matches" on the antipodal side of the sky for the uncovered fixture.
- Switched to **patch-center / frame-center proximity** (`ra2000` / `dec2000` BETWEEN ± margin). Margin = `0.12 deg` for `mosaic` and `0.20 deg` for `frame`, plus half the query box. This is a tiny over-approximation (a patch whose center is one arcsecond past the margin and tilted toward the box could be missed) but it produces correct, deterministic, RA-wrap-free results everywhere we'll actually use this tool.
- Live results on Perseus fixture: 4 bands (HSC-G / HSC-I / HSC-R / HSC-Z), 2 patches each, sub-arcsecond seeing throughout. 145 visits across all four bands (102 in HSC-G alone) — confirms "aggregate by default" was the right call for `frame_coverage`.
- Live results on the uncovered fixture: empty `Coverage` for both `mosaic` and `frame` queries, no exceptions raised.
- **Limitation logged in module docstring:** regions wrapping RA=0/360 are not supported. The two shipped fixtures are nowhere near the wrap.

---

## Bonus — Local Parquet mirrors *(done)*

Local copies of the small HSCLA metadata catalogs, so coverage / overlap / lookup queries can run offline and use the *real* per-patch corner coordinates instead of the server-side proximity test.

- [x] `pyarrow` added to runtime deps; `config.MirrorRootMissing` + `config.mirror_root()` resolver (env: `HSCLA_MIRROR_ROOT`, default `/Volumes/galaxy/hsc/la2020/`).
- [x] `hscla_tool/mirror.py`: `build_mirror(table)` submits `SELECT * FROM la2020.<table>` as `csv.gz`, streams to a Parquet file at `mirror_path(table)`; `load_mirror(table)` reads it back; `is_mirrored(table)` is a cheap check. CLI: `uv run python -m hscla_tool.mirror build {mosaic|frame|mosaicframe|wcs}` and `... status`.
- [x] `coverage.region_coverage(..., source='local')` reads the mosaic Parquet, runs a true per-patch corner-AABB overlap with an RA-wrap guard (patches whose corner span exceeds 180° are dropped — they would otherwise match the whole sky).
- [x] `coverage.frame_coverage(..., source='local', detailed=False)` reads the frame Parquet and applies the same frame-center proximity rule as the server query (frame has no usable CCD corners).
- [x] **`mosaic` mirror built live**: 464,840 rows × 41 cols, 47 s end-to-end, 72.7 MB gzipped CSV, **51.5 MB Parquet on disk** at `/Volumes/galaxy/hsc/la2020/mosaic.parquet`.
- [x] **`frame` mirror built live**: 4,163,375 rows × 97 cols, ~13 min end-to-end (9.2 min SQL job + 2.7 min download of 1.4 GB gzipped CSV + 1.2 min Parquet write), **1.36 GB Parquet on disk** at `/Volumes/galaxy/hsc/la2020/frame.parquet`. First attempt failed at the Parquet write with a `pyarrow.ArrowTypeError` because `frame.object` has mixed int / string cells; fixed by `_coerce_object_columns_to_string` in `mirror.py`.
- [x] **Local `frame_coverage` query at Perseus**: 1.0 s Parquet load + 1.0 s scan → 1,049 frames across 145 visits (`HSC-G` 738/102, `HSC-I` 71/10, `HSC-R` 145/20, `HSC-Z` 95/13). Visit counts match the server query exactly.
- [x] Live local-mirror query: Perseus returns 8 patches across `HSC-G/I/R/Z`, mean seeing 0.53–0.69″; the uncovered fixture returns empty (no antipodal-wrap false positives).

### Review (2026-05-12)
- The local-mirror path is *more accurate* than the server query, not just faster: it uses the actual patch corners (with a wrap guard) rather than the patch-center proximity approximation. For Perseus, both paths return 4 bands but the local path keeps 8 patches vs the server's 4 (server margin happened to fall short of the second patch row).
- `mirror.py` is deliberately scoped to the small metadata tables. The big photometry tables (`forced` / `meas` and detail variants) are tens of millions of rows and need a different strategy (per-tract files, partition by band) — out of scope here.

## Phase 4 — DAS cutout + mask (U3, U4) *(done)*

- [x] `hscla_tool/cutout.py` with `HscLaCutoutClient` (HTTP Basic auth) and `fetch_cutout(ra, dec, *, size_arcsec, band, kind='coadd', tract='any', with_variance=True, with_mask=True)`. Builds the multipart-form coordinate list, POSTs to `/cgi-bin/cutout`, stream-extracts the single FITS from the returned TAR, caches as a multi-extension FITS under `${HSCLA_TOOL_CACHE}/cutouts/<content-hash>.fits`, and returns a `Cutout` dataclass with `fits_path`, `hdul`, `image`, `mask_hdu`, `variance`, plus `.wcs()` and `.mask_planes()` helpers.
- [x] Empty TAR (the server's no-coverage signal) → typed `NoCoverageError`.
- [x] `hscla_tool/mask.py` with `parse_mask_planes(header)` (reads `MP_*` cards) and `decode(mask_hdu, planes=...)`. Vendored fallback bit map for older / hand-edited HDUs.
- [x] 17 offline tests (6 mask, 11 cutout, including a synthetic multi-extension FITS round-trip through a fake `requests.Session`) plus 2 live tests gated by `HSCLA_LIVE_TESTS=1`. Full suite: **76 passed + 6 gated-live, all pass when the flag is on**.
- [x] Live Perseus fetch (RA=49.27, Dec=41.24, 108″ box, HSC-I): cutout FITS opens, image / mask / variance HDUs all present, WCS is celestial, 17 mask planes decoded correctly (`BAD`, `SAT`, `INTRP`, `CR`, `EDGE`, `DETECTED`, ... `INEXACT_PSF`).
- [x] Live uncovered fetch: `NoCoverageError` raised as expected, no exceptions in the TAR-parsing path.

### Review (2026-05-12)
- **DAS cutout uses HTTP Basic auth**, not the `LAAUTH_SESSION` cookie of the SQL API. Two different auth schemes for two services in the same archive.
- **Bulk-first wire format, single-first API.** The upstream multipart form carries up to 990 cutouts; our v0 API exposes one-region calls and constructs a 1-row coord list internally. A `fetch_cutouts(list[...])` batch entry point can land later with no change to the existing call sites.
- **Empty TAR = no coverage.** The server is too polite about uncovered regions: HTTP 200 + 10 KiB of zero-padding TAR. We detect "zero TAR members" and raise `NoCoverageError`, which the live test confirms is the actual behavior.
- **Multi-extension FITS, not three separate files.** Image is the first non-integer HDU, mask is the integer HDU, variance is the second non-integer HDU. `_split_hdul` keys off `dtype.kind` so we never need to rely on HDU names (which HSCLA does not set).

---

## Phase 5 — PSF picker (U5) *(done)*

- [x] `hscla_tool/psf.py` with `HscLaPsfClient` (HTTP Basic auth, multipart-form upload to `/psf/la2020/cgi/getpsf?bulk=on`) and `fetch_psf(ra, dec, *, band, kind='coadd', tract='auto', patch='auto', centered=True)`.
- [x] `Psf` frozen dataclass with `fits_path`, `hdul`, `psf_hdu`, `.array` property, `.wcs()` helper (handles HSCLA's alternate `A` WCS key), and `.close()`.
- [x] `NoCoverageError` re-used from `cutout` so callers can catch one exception for either fetch path.
- [x] Content-hash FITS cache under `${HSCLA_TOOL_CACHE}/psfs/`.
- [x] `psf_api` block added to `data/hscla_db.yaml` and validated by `db.py`.
- [x] 9 offline tests + 2 live tests gated by `HSCLA_LIVE_TESTS=1`. Full suite: **85 passed + 8 gated-live (all pass when the flag is on)**.
- [x] Live Perseus PSF at HSC-I returns a 41×41 float64 kernel normalized to sum 1.0 with the peak within 2 pixels of the center; uncovered fixture raises `NoCoverageError`.

### Review (2026-05-12)
- The PSF picker is the same auth + wire-format pattern as the cutout service (HTTP Basic, multipart-form coord list, TAR-of-FITS, empty-TAR = no data), but with a different field set (`rerun type filter tract patch ra dec centered`) and a much smaller payload (one single-HDU FITS per request, no mask/variance).
- The server accepts both short (`i`) and long (`HSC-I`) filter names and normalizes to the long form in the response filename.
- HSCLA writes the PSF's pixel WCS under the **alternate WCS key `A`** (`CTYPE1A`, `CRVAL1A`, etc.), not under the primary key. `Psf.wcs()` tries the primary first and falls back to `'A'`.
- The PSF kernel header carries no sky WCS by design — these are kernel images in pixel space, not on-sky cutouts.

---

## Phase 6 — Crossmatch + bulk archive (U6, U8) *(done)*

- [x] `hscla_tool/crossmatch.py` — `match(table, *, ra_col='ra', dec_col='dec', id_col=None, radius_arcsec=1.0, extra_columns=(), nearest_only=False)`. Builds a `WITH user_catalog AS (VALUES ...) ... JOIN la2020.forced ON coneSearch(...)` query (modeled after upstream `hscSspCrossMatch.py`), pushes it through `sql.run_sql`, returns a pandas DataFrame with `match_input_id / match_ra / match_dec / object_id / match_distance` plus any requested HSCLA columns. Validates inputs and rejects non-identifier `extra_columns` (no SQL injection).
- [x] `hscla_tool/archive.py` — `HscLaArchiveClient` and module-level shortcuts `download_patch_file(tract, patch, band, kind)`, `download_coadd_image(...)` (= `kind='calexp'`), `download_forced_catalog(...)` (= `kind='forced_src'`). Files cached under `${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/`, skip-if-cached, with resumable downloads using HTTP `Range:` requests against the server's `accept-ranges: bytes` support. `list_patch_files(...)` parses the Apache autoindex for one (band, tract, patch).
- [x] 9 file kinds supported per patch (`calexp / forced_src / meas / deblendedFlux / det / det_bkgd / ran / srcMatch / srcMatchFull`), confirmed by the live probe.
- [x] 23 new offline tests (13 crossmatch + 10 archive) + 2 live tests gated by `HSCLA_LIVE_TESTS=1` (crossmatch against Perseus + uncovered fixtures; archive listing at Perseus tract 15548 patch 1,6).

### Review (2026-05-13)
- **Crossmatch turned out to be SQL, not a separate web service.** Upstream `hscSspCrossMatch.py` is a SQL *generator*; it produces a `WITH user_catalog AS (VALUES ...) ... JOIN la2020.forced ON coneSearch(...)` query and hands it to `hscReleaseQuery.py`. We reuse our own `sql.run_sql` (cookie auth + full submit/poll/download cycle), which gives crossmatch results the same content-hash caching as any other SQL query for free.
- **Two SQL surprises** (each logged in `docs/lessons.md`):
  1. `earth_distance(coord, ll_to_earth(...))` — the upstream pattern — trips a postgres `value for domain earth violates check constraint "on_surface"` against `la2020.forced.coord`. We compute the match distance via a plain great-circle trig formula on `forced.ra`/`forced.dec` instead.
  2. CTE/JOIN-shape queries that pass coordinates as column references kept the planner from using whatever spatial index `forced.coord` has; even the literal-only `UNION ALL` form still takes 40+ minutes for a 3-row input. The infrastructure is correct (live test passes); HSCLA crossmatch performance is what the server gives us.
- **The file tree is a plain Apache autoindex** at `/archive/files/la2020/deepCoadd-results/<filter>/<tract>/<patch>/`. HTTP Basic auth (third HSCLA service to use it). Patches are URL-encoded as `x%2Cy`. The server advertises `Accept-Ranges: bytes` so `Range:`-header download resumption Just Works.

---

## Phase 7 — CLI and docs polish

- [ ] `hscla` console script (entry point in `pyproject.toml`) with subcommands mirroring the U1–U8 stories.
- [ ] README rewrite: short narrative pointing into `docs/SPEC.md` and `data/hscla_db.yaml`.
- [ ] CI: GitHub Actions running `ruff` and `pytest` (network tests gated by a secret).

**Acceptance.** `hscla coverage 49.2658 41.2486 --size-deg 0.03` prints the list of covered bands.

---

## Open questions (resolve before Phase 2)

- Does the HSCLA SQL API endpoint match the PDR one byte-for-byte aside from the host, or does it accept different `release_version` values? Verify by hitting `/datasearch/api/catalog_jobs/preview` with the smallest legal query.
- Is the maskbit→name mapping in HSCLA2020 identical to PDR3? If yes, we can vendor the PDR3 map; if not, we read it from each cutout's FITS header.
- Is `hscSspCrossMatch.py` actually wired to `la2020` server-side, or does the `--rerun=la2020` flag only relabel the input? Test before Phase 6.

## Review

(Add a short retrospective after each phase: what worked, what we
changed in the spec, what got added to `lessons.md`.)
