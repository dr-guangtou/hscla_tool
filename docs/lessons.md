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

---

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
