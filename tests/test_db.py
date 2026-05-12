"""Tests for `hscla_tool.db`. No network."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from hscla_tool import db


# A small but structurally valid knowledge-base fixture. Mirrors only the
# fields that `db._validate` actually checks, kept short on purpose.
GOOD_DOC: dict = {
    "archive": {
        "name": "HSC Legacy Archive",
        "home": "https://hscla.mtk.nao.ac.jp/doc/home/",
        "credentials": {"username_env": "HSCLA_USR", "password_env": "HSCLA_PWD"},
    },
    "releases": {
        "la2020": {
            "name": "HSCLA2020",
            "release_version_token": "hscla2020",
            "tools": {
                "sql_search": "https://hscla.mtk.nao.ac.jp/datasearch/",
                "das_cutout": "https://hscla.mtk.nao.ac.jp/das_cutout/la2020/",
            },
        },
    },
    "sql_api": {
        "login_url": "https://hscla.mtk.nao.ac.jp/account/api/session",
        "base_url": "https://hscla.mtk.nao.ac.jp/datasearch/api/catalog_jobs/",
        "endpoints": {
            "preview": "preview",
            "submit": "submit",
            "status": "status",
            "download": "download",
            "delete": "delete",
            "cancel": "cancel",
        },
        "client_version": 20190924.1,
        "status_values": {
            "in_progress": ["waiting", "running"],
            "terminal": ["done", "error", "canceled", "deleted"],
        },
    },
    "command_line_tools": {
        "repo": "https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools",
        "scripts": {
            "catalog_query": {
                "entrypoint": "hscSspQuery.py",
                "purpose": "Submit SQL jobs against HSCLA.",
            },
        },
    },
    "catalogs": {
        "schema": "la2020",
        "tables": {
            "forced": {"kind": "forced", "role": "summary", "description": "Forced summary."},
            "mosaic": {"kind": "metadata", "description": "Coadd metadata."},
        },
    },
    "photometry": {"flux_unit": "nanojansky"},
    "where_clause_functions": [
        {"name": "coneSearch", "signature": "coneSearch(...)", "description": "..."},
    ],
    "test_regions": {
        "covered_lsbg": {
            "description": "Perseus LSBG with HSCLA coverage.",
            "ra_deg": 49.265759499639465,
            "dec_deg": 41.24859266109193,
            "box_size_deg": 0.03,
        },
        "uncovered_blank": {
            "description": "No HSCLA coverage.",
            "ra_deg": 198.1261597689148,
            "dec_deg": 29.561429698176415,
        },
    },
}


@pytest.fixture(autouse=True)
def _clear_cache():
    db._cached_load.cache_clear()
    yield
    db._cached_load.cache_clear()


def _write(tmp_path: Path, payload: dict) -> Path:
    out = tmp_path / "hscla_db.yaml"
    out.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# Real shipped YAML
# --------------------------------------------------------------------------- #


def test_real_yaml_loads_and_validates() -> None:
    data = db.load()
    assert "catalogs" in data
    assert "la2020" in data["releases"]


def test_real_yaml_known_table_lookup() -> None:
    forced = db.get_table("forced")
    assert "description" in forced
    assert forced["kind"] == "forced"


def test_real_yaml_fixtures_present() -> None:
    assert set(db.list_fixtures()) >= {"covered_lsbg", "uncovered_blank"}
    perseus = db.get_fixture("covered_lsbg")
    assert isinstance(perseus["ra_deg"], float)
    assert isinstance(perseus["dec_deg"], float)


def test_real_yaml_tool_url_resolves() -> None:
    url = db.get_tool_url("la2020", "das_cutout")
    assert url.startswith("https://")
    assert "la2020" in url


def test_real_yaml_sql_api_endpoints() -> None:
    api = db.get_sql_api()
    assert api["login_url"].startswith("https://")
    assert api["base_url"].endswith("/")
    assert {"submit", "status", "download", "delete", "preview"} <= set(api["endpoints"])
    assert set(api["status_values"]) >= {"in_progress", "terminal"}


def test_real_yaml_release_version_token() -> None:
    assert db.get_release_version_token("la2020") == "hscla2020"


def test_list_tables_filter_by_kind() -> None:
    forced_tables = db.list_tables(kind="forced")
    assert "forced" in forced_tables
    assert all(db.get_table(name)["kind"] == "forced" for name in forced_tables)


# --------------------------------------------------------------------------- #
# Synthetic YAML — happy + strict-validation failures
# --------------------------------------------------------------------------- #


def test_synthetic_good_yaml_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, GOOD_DOC)
    data = db.load(path)
    assert data["catalogs"]["tables"]["forced"]["kind"] == "forced"


def test_missing_top_level_key_is_rejected(tmp_path: Path) -> None:
    bad = copy.deepcopy(GOOD_DOC)
    del bad["catalogs"]
    path = _write(tmp_path, bad)
    with pytest.raises(db.KnowledgeBaseError, match="catalogs"):
        db.load(path)


def test_table_without_description_is_rejected(tmp_path: Path) -> None:
    bad = copy.deepcopy(GOOD_DOC)
    del bad["catalogs"]["tables"]["forced"]["description"]
    path = _write(tmp_path, bad)
    with pytest.raises(db.KnowledgeBaseError, match="description"):
        db.load(path)


def test_table_with_blank_description_is_rejected(tmp_path: Path) -> None:
    bad = copy.deepcopy(GOOD_DOC)
    bad["catalogs"]["tables"]["forced"]["description"] = "   "
    path = _write(tmp_path, bad)
    with pytest.raises(db.KnowledgeBaseError, match="description"):
        db.load(path)


def test_invalid_tool_url_is_rejected(tmp_path: Path) -> None:
    bad = copy.deepcopy(GOOD_DOC)
    bad["releases"]["la2020"]["tools"]["sql_search"] = "not-a-url"
    path = _write(tmp_path, bad)
    with pytest.raises(db.KnowledgeBaseError, match="not a valid http"):
        db.load(path)


def test_fixture_without_coordinates_is_rejected(tmp_path: Path) -> None:
    bad = copy.deepcopy(GOOD_DOC)
    del bad["test_regions"]["covered_lsbg"]["ra_deg"]
    path = _write(tmp_path, bad)
    with pytest.raises(db.KnowledgeBaseError, match="ra_deg"):
        db.load(path)


def test_unknown_table_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="no_such_table"):
        db.get_table("no_such_table")


def test_unknown_release_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="no_such_release"):
        db.get_tool_url("no_such_release", "das_cutout")


def test_missing_yaml_file_raises(tmp_path: Path) -> None:
    with pytest.raises(db.KnowledgeBaseError, match="not found"):
        db.load(tmp_path / "absent.yaml")
