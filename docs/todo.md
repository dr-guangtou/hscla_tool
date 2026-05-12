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

## Phase 4 — DAS cutout + mask (U3, U4)

- [ ] `cutout.fetch_cutout(ra, dec, *, size_arcsec, band, kind="coadd", with_variance=True, with_mask=True)`.
- [ ] Cache downloaded FITS under `${HSCLA_TOOL_CACHE}/cutouts/`.
- [ ] `mask.decode(mask_hdu)` → `dict[str, ndarray]` keyed by named planes (BAD, SAT, INTRP, CR, EDGE, DETECTED, …). Source the bit→name map from the FITS header, fallback to a vendored mapping.
- [ ] Raise `NoCoverageError` for the uncovered fixture.

**Acceptance.** From the Perseus fixture, we can produce an RGB three-color preview using `i/r/g` cutouts; mask decoding correctly identifies SAT/BAD pixels.

---

## Phase 5 — PSF picker (U5)

- [ ] `psf.fetch_psf(ra, dec, *, band, kind="coadd")` returns a PSF as a 2D `numpy` array plus a cached FITS path.
- [ ] Handle the "no coverage" case symmetrically with `cutout`.

**Acceptance.** Perseus fixture yields a finite, normalized PSF in each available band; uncovered fixture raises `NoCoverageError`.

---

## Phase 6 — Crossmatch + bulk archive (U6, U8)

- [ ] `crossmatch.match(table, *, ra_col, dec_col, radius_arcsec)` — submit an upload-based match against HSCLA (`--rerun=la2020` equivalent).
- [ ] `archive.download_patch(tract, patch, *, band, kind)` and `archive.download_forced_catalog(tract, patch)` — direct file-tree downloads.
- [ ] Both operations are resumable / skip-if-cached.

**Acceptance.** A 10-row input list crossmatches in <60 s with all rows in the Perseus footprint matched; a patch-level coadd download completes and opens cleanly with `astropy.io.fits`.

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
