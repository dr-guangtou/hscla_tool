"""Load and look up the HSCLA knowledge base (`data/hscla_db.yaml`).

The YAML file is the structured projection of `README.md`. This module:

  1. Finds and loads the YAML once per process.
  2. Checks it carefully on load. Anything missing or malformed makes
     us fail loudly, so a typo cannot silently turn into wrong science.
  3. Offers small helper functions for the lookups we actually do —
     tables, fixtures, tool URLs.

All helpers return plain dicts (or lists / strings). No special
classes. If you find yourself reaching past the helpers into the raw
mapping, add a new helper here instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
DEFAULT_YAML_PATH = REPO_ROOT / "data" / "hscla_db.yaml"


class KnowledgeBaseError(RuntimeError):
    """Raised when the HSCLA knowledge-base YAML is missing fields or malformed."""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Return the validated knowledge-base contents as a dict.

    Repeated calls with the same path are cached. Pass `path` only for
    tests or when the file lives somewhere unusual.
    """

    resolved = Path(path).resolve() if path is not None else DEFAULT_YAML_PATH
    return _cached_load(str(resolved))


@lru_cache(maxsize=4)
def _cached_load(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_file():
        raise KnowledgeBaseError(f"HSCLA knowledge-base YAML not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise KnowledgeBaseError(f"Failed to parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise KnowledgeBaseError(
            f"Top level of {path} must be a mapping, got {type(data).__name__}"
        )
    _validate(data, source=path)
    return data


# --------------------------------------------------------------------------- #
# Strict validation
# --------------------------------------------------------------------------- #


def _validate(data: dict[str, Any], *, source: Path) -> None:
    """Loud, opinionated check that the YAML has everything we rely on."""

    required_top_keys = {
        "archive",
        "releases",
        "sql_api",
        "cutout_api",
        "psf_api",
        "command_line_tools",
        "catalogs",
        "photometry",
        "where_clause_functions",
        "test_regions",
    }
    missing = required_top_keys - set(data)
    if missing:
        raise KnowledgeBaseError(
            f"{source}: top-level keys missing: {sorted(missing)}"
        )

    _require_keys(data["archive"], ("name", "home", "credentials"), source, "archive")
    creds = data["archive"]["credentials"]
    _require_keys(creds, ("username_env", "password_env"), source, "archive.credentials")

    releases = data["releases"]
    if not isinstance(releases, dict) or not releases:
        raise KnowledgeBaseError(f"{source}: releases must be a non-empty mapping")
    for name, body in releases.items():
        ctx = f"releases.{name}"
        _require_keys(body, ("name", "tools"), source, ctx)
        tools = body["tools"]
        if not isinstance(tools, dict) or not tools:
            raise KnowledgeBaseError(f"{source}: {ctx}.tools must be a non-empty mapping")
        for tool_key, url in tools.items():
            _check_url(url, source, f"{ctx}.tools.{tool_key}")

    catalogs = data["catalogs"]
    _require_keys(catalogs, ("schema", "tables"), source, "catalogs")
    tables = catalogs["tables"]
    if not isinstance(tables, dict) or not tables:
        raise KnowledgeBaseError(f"{source}: catalogs.tables must be a non-empty mapping")
    for tname, tbody in tables.items():
        ctx = f"catalogs.tables.{tname}"
        if not isinstance(tbody, dict):
            raise KnowledgeBaseError(f"{source}: {ctx} must be a mapping")
        _require_keys(tbody, ("kind", "description"), source, ctx)
        if not isinstance(tbody["description"], str) or not tbody["description"].strip():
            raise KnowledgeBaseError(f"{source}: {ctx}.description must be a non-empty string")

    sql_api = data["sql_api"]
    _require_keys(
        sql_api,
        ("login_url", "base_url", "endpoints", "client_version", "status_values"),
        source,
        "sql_api",
    )
    _check_url(sql_api["login_url"], source, "sql_api.login_url")
    _check_url(sql_api["base_url"], source, "sql_api.base_url")
    endpoints = sql_api["endpoints"]
    if not isinstance(endpoints, dict) or not endpoints:
        raise KnowledgeBaseError(f"{source}: sql_api.endpoints must be a non-empty mapping")
    for key in ("submit", "status", "download", "delete", "preview"):
        if key not in endpoints:
            raise KnowledgeBaseError(f"{source}: sql_api.endpoints missing required key {key!r}")
    status_values = sql_api["status_values"]
    _require_keys(status_values, ("in_progress", "terminal"), source, "sql_api.status_values")

    cutout_api = data["cutout_api"]
    _require_keys(
        cutout_api,
        ("base_url", "endpoint", "auth", "multipart_field", "coord_list_format"),
        source,
        "cutout_api",
    )
    _check_url(cutout_api["base_url"], source, "cutout_api.base_url")

    psf_api = data["psf_api"]
    _require_keys(
        psf_api,
        ("base_url", "endpoint", "auth", "multipart_field", "coord_list_format"),
        source,
        "psf_api",
    )
    _check_url(psf_api["base_url"], source, "psf_api.base_url")

    fixtures = data["test_regions"]
    if not isinstance(fixtures, dict) or not fixtures:
        raise KnowledgeBaseError(f"{source}: test_regions must be a non-empty mapping")
    for fname, fbody in fixtures.items():
        ctx = f"test_regions.{fname}"
        _require_keys(fbody, ("description", "ra_deg", "dec_deg"), source, ctx)
        for coord in ("ra_deg", "dec_deg"):
            if not isinstance(fbody[coord], (int, float)):
                raise KnowledgeBaseError(f"{source}: {ctx}.{coord} must be a number")


def _require_keys(
    body: Any,
    keys: tuple[str, ...],
    source: Path,
    context: str,
) -> None:
    if not isinstance(body, dict):
        raise KnowledgeBaseError(f"{source}: {context} must be a mapping")
    missing = [k for k in keys if k not in body]
    if missing:
        raise KnowledgeBaseError(f"{source}: {context} missing keys {missing}")


def _check_url(value: Any, source: Path, context: str) -> None:
    if not isinstance(value, str):
        raise KnowledgeBaseError(f"{source}: {context} must be a string URL")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise KnowledgeBaseError(f"{source}: {context} is not a valid http(s) URL: {value!r}")


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #


def get_table(name: str, *, path: str | Path | None = None) -> dict[str, Any]:
    """Return the info dict for one HSCLA catalog table (e.g. 'forced')."""

    tables = load(path)["catalogs"]["tables"]
    if name not in tables:
        raise KeyError(f"Unknown HSCLA table {name!r}. Known: {sorted(tables)}")
    return dict(tables[name])


def list_tables(
    *,
    kind: str | None = None,
    path: str | Path | None = None,
) -> list[str]:
    """List HSCLA table names, optionally filtered by `kind` (metadata/forced/meas)."""

    tables = load(path)["catalogs"]["tables"]
    if kind is None:
        return sorted(tables)
    return sorted(name for name, body in tables.items() if body.get("kind") == kind)


def get_fixture(name: str, *, path: str | Path | None = None) -> dict[str, Any]:
    """Return the info dict for one test region (e.g. 'covered_lsbg')."""

    fixtures = load(path)["test_regions"]
    if name not in fixtures:
        raise KeyError(f"Unknown HSCLA test region {name!r}. Known: {sorted(fixtures)}")
    return dict(fixtures[name])


def list_fixtures(*, path: str | Path | None = None) -> list[str]:
    """List the names of all defined test regions."""

    return sorted(load(path)["test_regions"])


def get_tool_url(
    release: str,
    tool: str,
    *,
    path: str | Path | None = None,
) -> str:
    """Return the URL for one interactive tool of a given HSCLA release.

    Example: `get_tool_url('la2020', 'das_cutout')`.
    """

    releases = load(path)["releases"]
    if release not in releases:
        raise KeyError(f"Unknown HSCLA release {release!r}. Known: {sorted(releases)}")
    tools = releases[release]["tools"]
    if tool not in tools:
        raise KeyError(f"Unknown tool {tool!r} for release {release!r}. Known: {sorted(tools)}")
    return str(tools[tool])


def get_command_line_tool(name: str, *, path: str | Path | None = None) -> dict[str, Any]:
    """Return the info dict for one of the upstream NAOJ command-line scripts."""

    scripts = load(path)["command_line_tools"]["scripts"]
    if name not in scripts:
        raise KeyError(
            f"Unknown command-line tool {name!r}. Known: {sorted(scripts)}"
        )
    return dict(scripts[name])


def get_where_clause_functions(*, path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the list of server-side WHERE-clause helpers (coneSearch etc.)."""

    return list(load(path)["where_clause_functions"])


def get_sql_api(*, path: str | Path | None = None) -> dict[str, Any]:
    """Return the HTTP endpoint metadata for the HSCLA catalog SQL service."""

    return dict(load(path)["sql_api"])


def get_cutout_api(*, path: str | Path | None = None) -> dict[str, Any]:
    """Return the HTTP endpoint metadata for the HSCLA DAS cutout service."""

    return dict(load(path)["cutout_api"])


def get_psf_api(*, path: str | Path | None = None) -> dict[str, Any]:
    """Return the HTTP endpoint metadata for the HSCLA PSF picker service."""

    return dict(load(path)["psf_api"])


def get_release_version_token(release: str, *, path: str | Path | None = None) -> str:
    """Return the literal `release_version` string the SQL API expects.

    Example: `get_release_version_token('la2020')` → `'hscla2020'`. The
    short release key (`la2020`) is what we use everywhere else in the
    knowledge base; this helper is the one place that maps it to the
    server's wire format.
    """

    releases = load(path)["releases"]
    if release not in releases:
        raise KeyError(f"Unknown HSCLA release {release!r}. Known: {sorted(releases)}")
    body = releases[release]
    token = body.get("release_version_token")
    if not isinstance(token, str) or not token:
        raise KnowledgeBaseError(
            f"releases.{release}.release_version_token is missing or empty in the knowledge base"
        )
    return token
