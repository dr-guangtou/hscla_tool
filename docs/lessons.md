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
