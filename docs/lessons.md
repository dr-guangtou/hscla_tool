# Lessons

Append-only log of mistakes, surprises, and the rationale behind
non-obvious decisions. One entry per lesson. Newest at the top.

Format:

```
## YYYY-MM-DD — short title
- Context: what we were doing.
- Mistake / surprise: what went wrong or what was unexpected.
- Resolution: what we did about it.
- Rule: the one-line takeaway, if any (also persist to CLAUDE.md if it's a hard rule).
```

## 2026-05-13 — Import-time credential check breaks `hscla --help` on cold installs
- Context: Phase 7 added the `hscla` console script. Loading the entry point pulls in `hscla_tool/__init__.py`, which calls `load_credentials()` at import time.
- Surprise: a fresh CI runner without `HSCLA_USR` / `HSCLA_PWD` set crashes before `hscla --help` can even show its usage. Same problem on a user's first install before they have set the env vars.
- Resolution: the GitHub Actions offline-test job exports placeholder values (`ci-placeholder@example.com` / `ci-placeholder-password`) so the package can import. Live tests use the real secrets via `HSCLA_USR_SECRET` / `HSCLA_PWD_SECRET`. The behavior is documented in the CI workflow and the README's CLI section.
- Rule: when a Python package fails at import time on a missing precondition, any CI / packaging environment that lifts it must satisfy that precondition or the entire CLI becomes unreachable.

## 2026-05-13 — `ruff format` carries a large unintended diff for legacy code
- Context: planning CI for Phase 7. The first draft of the workflow included a `ruff format --check` step alongside `ruff check`.
- Surprise: `ruff format --check` would reformat 22 of 24 source / test files in the repo (older modules pre-date the use of `ruff format`). Including the step in CI would block every PR until those 22 files were reformatted — a stylistic 22-file diff to ship in the same PR as the CLI is too noisy.
- Resolution: kept `ruff check` in CI, dropped `ruff format --check`. Logged the 22-file reformat as an explicit follow-up in `docs/todo.md`'s v1.0 section so the user can opt in deliberately later.
- Rule: do not silently enable a linter rule (or formatter) whose first action is to touch most of the repo — surface the diff, decide explicitly, and ship the reformat as its own change.

---

## 2026-05-13 — HSCLA crossmatch is slow regardless of SQL shape
- Context: even the most index-friendly crossmatch SQL — a `UNION ALL` of per-row `coneSearch(forced.coord, <lit RA>, <lit DEC>, R)` branches with literal bounding boxes on `forced.ra`/`forced.dec` — takes 40+ minutes on the live server for a *three-row* input.
- What we ruled out:
  - `coord` column type incompatibility (would error fast).
  - CTE-vs-column-reference planner blindness (the literal-only `UNION ALL` shape still doesn't help).
  - Our wait/poll loop (the server itself reports `running` for 40+ minutes).
- What this means: HSCLA's planner either does not use a spatial index on `la2020.forced.coord`, or the index is missing, or there's persistent server load. Functional correctness is confirmed by the live test passing; performance is whatever the archive gives us.
- Rule: for HSCLA crossmatch, plan on multi-minute end-to-end times even for tiny inputs. For batch work, consider running queries in parallel from different sessions, or stage matches by tract using the local Parquet mirror instead.

## 2026-05-13 — `earth_distance(coord, ll_to_earth(...))` trips `on_surface` on HSCLA
- Context: implementing `crossmatch.match` following the upstream pattern `earth_distance(coord, ll_to_earth(dec, ra))` to record per-row match separations.
- Surprise: live submission failed with `value for domain earth violates check constraint "on_surface"`. The postgres `earth` domain checks that any value is on a sphere of radius `earth()` (Earth's mean radius). One or both of the two arguments here violates that. `ll_to_earth` itself is fine in isolation (probed). HSCLA's `la2020.forced.coord` is opaque (the SQL whitelist blocks `pg_typeof`), but the failure proves it is *not* a clean member of the `earth` domain — either a different scaling or values with accumulated floating-point error that exceed the `1e-12` tolerance.
- Resolution: dropped `earth_distance` entirely. Compute the match separation with a plain great-circle formula on `forced.ra` and `forced.dec`, clamped against floating-point `acos` domain errors:
  ```sql
  degrees(acos(GREATEST(-1.0, LEAST(1.0,
    sin(radians(target.dec)) * sin(radians(user_catalog.match_dec)) +
    cos(radians(target.dec)) * cos(radians(user_catalog.match_dec)) *
    cos(radians(target.ra - user_catalog.match_ra))
  )))) * 3600.0 AS match_distance
  ```
  Result is in arcseconds, which is what callers actually want for a crossmatch.
- Rule: when an upstream SQL relies on a postgres extension domain (`earth`, `cube`, ...), do not assume the target archive's columns satisfy the domain's check constraints. Always have a portable-trig fallback in your back pocket.

## 2026-05-13 — Crossmatch is SQL, not a separate web service
- Context: I expected the HSCLA crossmatch endpoint to look like cutout / PSF — HTTP Basic + multipart-form + TAR.
- Surprise: the upstream NAOJ tool `pdr2/hscSspCrossMatch/hscSspCrossMatch.py` is a SQL *generator*. It produces a query like:
  ```sql
  WITH user_catalog AS (VALUES ...), match AS (
    SELECT object_id,
           earth_distance(coord, ll_to_earth(user_catalog.dec, user_catalog.ra)),
           user_catalog.*
    FROM user_catalog JOIN la2020.forced ON coneSearch(coord, ...)
    OFFSET 0  -- suppress planner shortcut
  )
  SELECT match.* FROM match LEFT JOIN la2020.forced USING(object_id)
  WHERE isprimary
  ```
  and hands the text to the regular SQL submission tool. There is no upload-and-match HTTP service.
- Resolution: `hscla_tool/crossmatch.py` builds the same SQL and runs it through our existing `sql.run_sql`. Inputs are validated to prevent SQL injection (extra columns must be plain identifiers; embedded apostrophes in IDs are doubled).
- Rule: when an upstream "tool" looks like a standalone client, check whether it is actually a SQL generator wrapping a service we already have.

## 2026-05-13 — The HSCLA file tree is a plain Apache autoindex
- Context: implementing `archive.py` for per-patch FITS downloads.
- Surprise (good kind): the file tree is a vanilla Apache directory listing at `https://hscla.mtk.nao.ac.jp/archive/files/la2020/`. Same HTTP Basic auth as cutout / PSF. Patch directories are URL-encoded `x%2Cy`. The server advertises `Accept-Ranges: bytes`, so partial downloads can be resumed cleanly with a `Range:` header.
- Resolution: `HscLaArchiveClient.download_patch_file(...)` keeps a `<dest>.tmp` for in-progress downloads, sends `Range: bytes=<offset>-` when one is found, and atomically renames on success. `list_patch_files(...)` parses the autoindex HTML with a small regex.
- Rule: when an archive uses `accept-ranges`, you get free resumable downloads — pre-write a `.tmp` partial and send `Range:` for the rest.

## 2026-05-12 — HSCLA PSFs use the alternate WCS key `A`
- Context: writing `Psf.wcs()` for the Phase 5 PSF picker.
- Surprise: `WCS(header)` raised `KeyError("No WCS with key ' ' was found in the given header")` even though the FITS clearly had `CTYPE1A='LINEAR'`, `CRPIX1A=1`, `CRVAL1A=-20` etc. HSCLA writes the PSF kernel's pixel WCS under the alternate key `A`, not the primary key.
- Resolution: `Psf.wcs()` tries `WCS(header)` first and falls back to `WCS(header, key='A')` on `KeyError`. Works for both shapes.
- Rule: when reading any HSCLA FITS, do not assume the WCS lives under the primary key. Probe with `astropy.io.fits.Header.cards`, look for `CTYPE*<key>`, and pass the right key to `astropy.wcs.WCS`.

## 2026-05-12 — Two HSCLA services, two different auth schemes
- Context: Phase 4 cutout client.
- Surprise: the DAS cutout service uses **HTTP Basic auth** (`Authorization: Basic base64(user:password)`), not the `LAAUTH_SESSION` session-cookie flow the catalog SQL service requires. Sending the cookie does nothing here; sending the basic-auth header to the SQL service does nothing there. The two services are siblings under the same archive but they speak different languages.
- Resolution: separate clients — `hscla_tool.sql.HscLaClient` (cookie auth) and `hscla_tool.cutout.HscLaCutoutClient` (basic auth). Both consume the same `config.Credentials` so the user only sets `HSCLA_USR` / `HSCLA_PWD` once.
- Rule: when adding any new HSCLA endpoint, probe its auth requirements before reusing an existing client's pattern.

## 2026-05-12 — DAS cutout signals "no coverage" with an empty TAR (HTTP 200)
- Context: probing the uncovered fixture at the cutout endpoint.
- Surprise: server returned HTTP 200, `application/x-tar`, and ~10 KiB of zero padding — a valid but empty TAR archive. No FITS members. Nothing in the response body says "no coverage"; you have to iterate the TAR and notice it's empty.
- Resolution: `cutout._extract_one_fits` returns `None` when the TAR has zero `.fits` members; `fetch_cutout` then raises a typed `NoCoverageError`.
- Rule: never assume an HTTP error code is your "nothing here" signal. Image services often answer "no" with a successful empty payload.

## 2026-05-12 — HSCLA cutout = one multi-extension FITS, not three files
- Context: I expected the cutout TAR to contain separate `image.fits`, `mask.fits`, `variance.fits` members (`unagi` does it that way).
- Surprise: the live response packs a single multi-extension FITS per cutout request: HDU 0 is empty `PRIMARY`, HDU 1 is the float32 image, HDU 2 is the int32 mask, HDU 3 is the float32 variance. HSCLA does **not** set `EXTNAME` on the data HDUs, so you cannot identify them by header name.
- Resolution: `cutout._split_hdul` keys off `dtype.kind` — first non-integer HDU is the image, first integer HDU is the mask, remaining non-integer HDU is the variance. Order-based but robust to absent flags.
- Rule: when the upstream sends a multi-extension FITS without `EXTNAME`s, infer kind from dtype, not header position alone.

## 2026-05-12 — HSCLA `frame.object` has mixed int + string cells
- Context: building the local Parquet mirror of `la2020.frame` (4.16 M rows × 97 cols).
- Surprise: the SQL job and download both succeeded; `df.to_parquet(...)` then exploded with `ArrowTypeError("Expected bytes, got a 'int' object")`. The column at fault: literally named `object` — the observing-log target name — whose CSV values mix purely numeric strings (parsed as `int`) and proper names. `pd.read_csv` inferred it as plain object-dtype with truly mixed Python types.
- Resolution: added `_coerce_object_columns_to_string(df)` in `mirror.py` that casts every plain object-dtype column to pandas' nullable string dtype before writing. Real null values are preserved (no `"None"` cells). Regression test in `tests/test_mirror.py`.
- Aside: `df.select_dtypes(include="object")` emits a pandas-3 deprecation warning because it also catches `str`/`string` extension dtypes; switched to a direct `df[col].dtype == object` check.
- Rule: when mirroring a remote table, always normalize plain-`object` columns to a uniform string dtype before writing to Parquet. CSV type inference plus columns with mixed numeric/text cells is the canonical pyarrow-write footgun.

## 2026-05-12 — `coneSearch` / `boxSearch` don't apply to `mosaic.areacube`
- Context: writing Phase 3 coverage. I assumed `coneSearch(areacube, ra, dec, r)` or `boxSearch(areacube, ...)` would let us ask "does this patch overlap my region?".
- Surprise: both functions return zero matches on `mosaic` even when the region clearly has coverage (verified by querying with corner-coord ranges, by Perseus showing 4 bands of data in the file tree). On the live archive, the server-side spatial helpers seem to require a real `coord` column, which exists on `forced` / `meas` but not on `mosaic` / `frame`.
- Resolution: switched `coverage.region_coverage` / `frame_coverage` to a patch-center / frame-center proximity test (`ra2000`/`dec2000` BETWEEN center ± margin, with margins of 0.12 deg for patches and 0.20 deg for frames plus half the query box). Records the right answer for the two shipped fixtures.
- Rule: never assume a server-side spatial function works on every coordinate-like column; probe it before designing a module around it.

## 2026-05-12 — Corner-envelope spatial filter is wrong near RA=0 wrap
- Context: my first cut at `region_coverage` used `LEAST(llcra, ulcra, urcra, lrcra) <= max_ra AND GREATEST(...) >= min_ra` (and the same for dec). Worked on Perseus.
- Surprise: at the uncovered fixture (RA≈198, Dec≈29.6) the same query returned three patches — all of them centered at `ra2000 ≈ 359.99997`, on the *antipodal* side of the sky, ~8500 arcmin away. The corner-envelope of a patch whose corners straddle RA=0 wraps around to fill `[0, 360]`, so any query box matches.
- Resolution: dropped the corner-envelope test. Use patch-center proximity instead. Documented "this module does not handle regions that wrap RA=0/360" prominently in the docstring; both shipped fixtures are far from the wrap.
- Rule: when filtering on lat/lon corners, never trust naive `LEAST/GREATEST` on RA — always think about the wrap.

## 2026-05-12 — Float subtraction breaks "string equals" tests on SQL
- Context: I asserted `"BETWEEN 49.15 AND 49.39" in sql_text`.
- Surprise: `49.27 - 0.12` is `49.150000000000006`, so the SQL string contained `49.150000000000006` and the substring match failed.
- Resolution: tests now extract the float bounds from the SQL with a small regex and compare with `math.isclose`.
- Rule: never assert exact float-string contents from a query you built by subtraction. Parse, then compare with tolerance.

## 2026-05-12 — HSCLA SQL auth differs from HSC-SSP PDR
- Context: Phase 2 needed a SQL client; I assumed it could mirror `hsc_sandbox/step1/python/sql_query.py`, which authenticates by including the credential in every catalog-job request body.
- Surprise: HSCLA uses **session-cookie auth** instead. The client must POST `{"email": user, "password": pwd}` to `https://hscla.mtk.nao.ac.jp/account/api/session`, capture the `LAAUTH_SESSION` cookie from the response, and send it on every subsequent call. The catalog-job endpoints still want the credential repeated in the JSON body AND a `clientVersion` float field — but without the cookie the requests are rejected.
- Resolution: live-probed the endpoint before writing any code, recorded the exact request shapes in `data/hscla_db.yaml` under a new `sql_api` block, and built `hscla_tool/sql.py` around `requests.Session` so cookie handling is automatic.
- Rule: never reuse another HSC service's auth scheme without probing the target service first. The cost of a 10-line probe is much smaller than the cost of debugging a wrong auth pattern.

## 2026-05-12 — `preview` endpoint has a ~5 s server timeout
- Context: I tried `coneSearch(coord, ...)` on `la2020.forced` via the `preview` endpoint as a smoke test.
- Surprise: server returned HTTP 406 with `"canceling statement due to statement timeout"` even for a 1-arcsec cone — `preview` is reserved for fast metadata queries, not data queries.
- Resolution: documented in `docs/todo.md` Phase-2 review and in the module docstring; `preview_sql` is now explicitly described as suitable only for `information_schema` lookups, `COUNT(*)`, and similarly cheap queries. Real catalog queries go through the full submit / poll / download path in `run_sql`.

## 2026-05-12 — HSCLA CSV header is `#`-prefixed
- Context: testing `_read_sql_csv`.
- Surprise: the server always prefixes the CSV header line with `# `, even when `include_metainfo_to_body=False`. With metainfo on, it emits *several* `#`-prefixed lines and the *last* one is the actual header.
- Resolution: rewrote the parser to find the contiguous run of `#`-prefixed lines at the top of the file, treat the last entry as the column header, and pass the rest verbatim to `pandas.read_csv`. Test cases cover both modes.

## 2026-05-12 — Moved HSCLA env vars from `~/.zprofile` to `~/.zshenv`
- Context: Phase 1 added a check that fails the import of `hscla_tool` if `HSCLA_USR` / `HSCLA_PWD` are not set. The env vars used to live in `~/.zprofile`.
- Surprise: child shells, `uv run`, CI, and the Bash tool inside Claude Code are non-login by default. `~/.zprofile` is not sourced, so the env vars were absent and `import hscla_tool` failed.
- Resolution: moved both exports to `~/.zshenv` (loaded by every zsh shell, login or not). The original `~/.zprofile` is preserved with the HSCLA lines stripped; a backup lives at `~/.zprofile.bak.2026-05-12`. New `~/.zshenv` is `chmod 600`.
- Side note: while making this change, the password value briefly appeared in the assistant's working transcript. Rotate the HSCLA password when convenient.
- Rule: when a tool depends on env vars, document *which shell file* sets them; for vars that must be visible to non-login shells, prefer `~/.zshenv`.

## 2026-05-12 — Repo seeded with knowledge-base-first scaffold
- Context: starting `hscla_tool` from a near-empty repo (README + LICENSE only).
- Decision: build the machine-readable knowledge base (`data/hscla_db.yaml`) and the architecture spec (`docs/SPEC.md`) **before** any code, because the project's first value proposition is "agent-readable HSCLA knowledge", and the tool API should be designed against the structured catalog, not the prose README.
- Rule: README and `data/hscla_db.yaml` must stay in sync — never update one without the other.
