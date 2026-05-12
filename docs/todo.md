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

## Phase 2 — SQL client

Goal: get arbitrary SQL queries working against HSCLA2020.

- [ ] Port `hsc_sandbox/step1/python/sql_query.py` into `hscla_tool/sql.py` and adapt the API endpoint to HSCLA (`https://hscla.mtk.nao.ac.jp/datasearch/api/catalog_jobs/`).
- [ ] Add `release="la2020"` as the only supported release in v0.
- [ ] Provide `run_sql(sql, *, fmt="csv") -> pandas.DataFrame` plus a streaming variant for large outputs.
- [ ] Cache job outputs under `${HSCLA_TOOL_CACHE}/sql/<hash>.csv`.
- [ ] Network test (skipped unless `HSCLA_USR` is set): cone search around `covered_lsbg` returns >0 rows from `la2020.forced`; same against `uncovered_blank` returns 0 rows.

**Acceptance.** A two-line script can list `object_id` and band-detection flags for objects in the Perseus fixture box.

---

## Phase 3 — Coverage and provenance (U1, U2)

- [ ] `coverage.region_coverage(ra, dec, *, size_deg)` → queries `la2020.mosaic` via `boxSearch`, returns filters present + overlapping `mosaic_id`s.
- [ ] `coverage.frame_coverage(ra, dec, *, size_deg)` → same against `la2020.frame` (single-CCD provenance).
- [ ] Both functions return a structured `Coverage` dataclass; empty result for uncovered regions.
- [ ] Tests against both fixtures.

**Acceptance.** Perseus fixture returns five filters; uncovered fixture returns `Coverage(filters=[], …)` without raising.

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
