# `hscla_tool` — Agent Context

Read this before doing anything in this repo. It is the per-repo
override on top of `~/.claude/CLAUDE.md`.

## What this repo is

`hscla_tool` is a Python toolkit + knowledge base for the **HSC Legacy
Archive (HSCLA)**, currently targeting the **HSCLA2020** release
(pipeline: `hscPipe v8`, same as HSC-SSP PDR3). See `docs/SPEC.md` for
the full architecture; this file is a short orientation, not a
duplicate.

## Where to look first

Always read these before writing or proposing code:

1. `docs/SPEC.md` — architecture, modules, conventions, fixtures.
2. `docs/USAGE.md` — task-oriented recipes ("how do I X?") for the
   library and CLI. Start here when a user request maps to an
   existing feature; only invent new code paths when no recipe fits.
3. `docs/ARCHIVE_LAYOUT.md` — observed structure of the HSCLA2020
   direct file archive at `/archive/files/la2020/`: per-patch FITS,
   per-visit warps, single-exposure outputs, and the **1 TB session
   rule**. Read before doing bulk-download work.
4. `data/hscla_db.yaml` — machine-readable catalog of HSCLA URLs,
   tables, server-side WHERE functions, and the two regression
   coordinates. **This is the structured projection of `README.md`.
   Update both together.**
5. `docs/todo.md` — the phased plan and current status.
6. `docs/lessons.md` — accumulated mistakes and rationale.

## Reference repos (external, do not modify)

- `/Users/shuang/Dropbox/work/project/otters/hsc_sandbox/step1`
  - `python/sql_query.py` — modern HSC SQL client we will port to `hscla_tool/sql.py`.
  - `python/fetch_schema.py` — pattern for `information_schema` introspection.
  - `hsc_db.yaml` — template for our `data/hscla_db.yaml`.
- `https://github.com/dr-guangtou/unagi` — prior-art Python wrapper for
  HSC SSP. Many of our target features exist there in older form; learn
  from it, do not copy verbatim.
- `https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020`
  — final authority on HSCLA endpoint payloads (cutout, PSF, SQL,
  crossmatch).

## Local data on this machine

The Parquet mirrors of the HSCLA metadata catalogs are already built
at `/Volumes/galaxy/hsc/la2020/`:

- `mosaic.parquet` — ~51 MB, 464,840 rows.
- `frame.parquet`  — ~1.36 GB, 4,163,375 rows.

**Default for basic coverage queries on this machine: use the local
copy.** Concretely:

- Prefer `coverage.region_coverage(..., source='local')` and
  `coverage.frame_coverage(..., source='local')` for any interactive
  or scripted coverage / overlap / patch-listing question. The local
  path is strictly more accurate for `mosaic` (exact 4-corner AABB
  overlap with an RA-wrap guard, instead of the server's patch-center
  proximity rule), just as accurate for `frame`, and roughly a
  one-second Parquet scan instead of a network round trip.
- Use `source='server'` only when you specifically want a fresh read
  from the archive — extremely rare for HSCLA2020, which is a closed
  release.
- If `/Volumes/galaxy` is not mounted, `source='local'` raises
  `MirrorError` with a "run `uv run python -m hscla_tool.mirror build <table>`"
  hint. Rebuild from there.

This is a machine-specific default, not a global one — the public
function signatures still default to `source='server'` so that the
tool behaves correctly on machines without the mirror.

## How to talk with the user

- **Ask questions when you're not sure.** Interview the user
  interactively — one or a few questions at a time — rather than
  guessing or making silent assumptions.
- **Plain language only.** Avoid software-engineering and
  project-management jargon (no "stakeholders", "scope", "acceptance
  criteria", "MVP", "contract", "abstraction layer", etc.). Say what
  you mean in everyday words.

## Expect surprises from the HSCLA server

The NAOJ web services behind HSCLA2020 are not held to a uniform
design. Different services in the same archive use different auth
schemes, different request formats, and different "no result"
signals. We've already hit:

- SQL catalog uses session-cookie auth (`LAAUTH_SESSION`); the DAS
  cutout service in the *same* archive uses HTTP Basic auth.
- `release_version` on the SQL wire is `hscla2020`; everywhere else
  the short release name is `la2020`.
- Server-side spatial helpers (`coneSearch`, `boxSearch`) don't apply
  to `mosaic.areacube` and return zero matches silently.
- `mosaic` corner-envelope wraps incorrectly across RA=0 / 360.
- `preview` SQL endpoint has a ~5 s hard timeout; real catalog
  queries must go through the full `submit + poll + download` path.
- HSCLA CSV downloads always prefix the header line with `# `.
- `frame.object` (yes, that's a column name) holds mixed int + string
  cells that crash plain `to_parquet`.
- DAS cutout signals "no data here" with **HTTP 200 + empty TAR**,
  not a 404. Multi-extension FITS without `EXTNAME`s.

**Rule when adding any new HSCLA endpoint**: probe the live service
end-to-end first (smallest legal request) and confirm the wire format,
auth, success-vs-empty signals, and field/HDU naming. Only then write
the module. Record each surprise in `docs/lessons.md`. Do not
extrapolate from one HSCLA service's behavior to another.

## Hard rules for this repo

- **Never work on `main`.** Cut a feature branch. Do not merge without
  explicit user permission.
- **English only** in code, comments, docstrings, commits.
- **`uv` only** for Python deps and execution. Always invoke
  `uv run python …`, never bare `python` or `.venv/bin/python`.
- **`snake_case` everywhere** in Python. Translate camelCase HSC names
  at the boundary (HTTP/SQL layer); do not let them leak into our API.
- **Credentials** come from env vars `HSCLA_USR` and `HSCLA_PWD` (set
  in `~/.zshenv`). Never log, print, or persist them. Only
  `hscla_tool/config.py` reads them.
- **Caching path**: respect `HSCLA_TOOL_CACHE` env var; default to
  `~/.cache/hscla_tool`. Cache keys are content-derived, never
  timestamped, so repeats are deterministic.
- **No coverage = empty result, not an error**, for query-like calls
  (coverage, SQL). For fetch-like calls (cutout, PSF) raise the typed
  `NoCoverageError`. The `uncovered_blank` fixture exercises both
  branches; new modules must test against it.
- **Don't estimate, measure.** Performance/size claims need a
  benchmark; numerical thresholds need a citation or a script.

## When you change something

- **New endpoint or table:** update `data/hscla_db.yaml` and the
  relevant section in `README.md`. They must stay in sync.
- **New module or significant behavior:** update `docs/SPEC.md`
  *before* the code, then add a checklist entry in `docs/todo.md`.
- **A mistake or surprise:** append a dated entry to `docs/lessons.md`.
- **Each phase complete:** add a Review block at the bottom of the
  phase in `docs/todo.md`.

## Test fixtures (use these in every new module)

| Fixture           | RA (deg)        | Dec (deg)       | Notes                                  |
| ----------------- | --------------- | --------------- | -------------------------------------- |
| `covered_lsbg`    | 49.2657595      | 41.2485927      | Perseus LSBG, multi-band HSCLA cover.  |
| `uncovered_blank` | 198.1261598     | 29.5614297      | No HSCLA coverage; exercise empty path.|

Box size for the covered fixture is 0.03 deg (≈108 arcsec) on a side.

## Style notes specific to this repo

- Public functions take SI-ish HSC-native units: positions in degrees,
  cone radii in arcsec (matches `coneSearch`), fluxes in nJy.
- Module-level constants are UPPER_SNAKE; everything else is lower.
- Docstrings are PEP 257; one short summary line plus paragraphs only
  where there is real information to convey. No restating the name.
