"""Tests for `hscla_tool.sql`.

All unit tests stub out `requests` so they run offline. One end-to-end
test against the live HSCLA service is included but only runs when the
env var `HSCLA_LIVE_TESTS=1` is set, so CI / casual runs stay hermetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from hscla_tool import config, sql

# --------------------------------------------------------------------------- #
# Fake HTTP transport
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: dict[str, Any] | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text if json_body is None else json.dumps(json_body)
        self.headers = headers or {}
        self.reason = "OK" if status_code < 400 else "ERR"

    def json(self) -> dict[str, Any]:
        if self._json is None:
            raise ValueError("response has no JSON body")
        return self._json

    def iter_content(self, chunk_size: int = 0):
        yield self.text.encode("utf-8")


class _FakeSession:
    """Stand-in for `requests.Session` that records POSTs and returns scripted replies."""

    def __init__(self, replies: dict[str, list[_FakeResponse]]) -> None:
        self._replies = {k: list(v) for k, v in replies.items()}
        self.cookies: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, *, json=None, timeout=None, stream=False) -> _FakeResponse:
        self.calls.append((url, json or {}))
        for key, queue in self._replies.items():
            if key in url:
                if not queue:
                    raise AssertionError(f"no scripted reply left for {url}")
                resp = queue.pop(0)
                # Mimic the cookie behavior of the real login endpoint.
                if "account/api/session" in url and resp.status_code == 200:
                    self.cookies["LAAUTH_SESSION"] = "abc123"
                return resp
        raise AssertionError(f"unexpected POST to {url}")


@pytest.fixture
def fake_creds(monkeypatch: pytest.MonkeyPatch) -> config.Credentials:
    monkeypatch.setenv("HSCLA_USR", "alice@example.com")
    monkeypatch.setenv("HSCLA_PWD", "hunter2")
    return config.load_credentials()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_cache_key_is_deterministic_and_short() -> None:
    key1 = sql._cache_key("SELECT 1", "la2020", "csv")
    key2 = sql._cache_key("SELECT 1", "la2020", "csv")
    assert key1 == key2
    assert len(key1) == 16


def test_cache_key_changes_with_inputs() -> None:
    base = sql._cache_key("SELECT 1", "la2020", "csv")
    assert sql._cache_key("SELECT 2", "la2020", "csv") != base
    assert sql._cache_key("SELECT 1", "la2020", "csv.gz") != base


def test_read_sql_csv_strips_hash_header(tmp_path: Path) -> None:
    p = tmp_path / "result.csv"
    p.write_text("# one,msg\r\n1,hello\r\n2,world\r\n", encoding="utf-8")
    df = sql._read_sql_csv(p)
    assert list(df.columns) == ["one", "msg"]
    assert df.iloc[0]["msg"] == "hello"


def test_read_sql_csv_handles_metainfo_comments(tmp_path: Path) -> None:
    # When include_metainfo_to_body=True, the server prepends comment lines.
    body = "# name,# job\n# SQL,# SELECT *\n#\n# col_a,col_b\n1,2\n3,4\n"
    p = tmp_path / "with_meta.csv"
    p.write_text(body, encoding="utf-8")
    df = sql._read_sql_csv(p)
    assert list(df.columns) == ["col_a", "col_b"]
    assert df.shape == (2, 2)


# --------------------------------------------------------------------------- #
# Client behavior with fake transport
# --------------------------------------------------------------------------- #


def _client_with(session: _FakeSession, creds: config.Credentials) -> sql.HscLaClient:
    return sql.HscLaClient(credentials=creds, session=session)


def test_login_sets_cookie_and_is_idempotent(fake_creds: config.Credentials) -> None:
    session = _FakeSession({
        "account/api/session": [_FakeResponse(json_body={"ok": True})],
    })
    client = _client_with(session, fake_creds)
    client.login()
    client.login()  # second call must be a no-op
    assert len([c for c in session.calls if "account/api/session" in c[0]]) == 1
    assert "LAAUTH_SESSION" in session.cookies


def test_login_raises_on_bad_credentials(fake_creds: config.Credentials) -> None:
    session = _FakeSession({
        "account/api/session": [_FakeResponse(status_code=401, json_body={"error": "no"})],
    })
    client = _client_with(session, fake_creds)
    with pytest.raises(sql.SqlError, match="login failed"):
        client.login()


def test_preview_returns_result_dict(fake_creds: config.Credentials) -> None:
    session = _FakeSession({
        "account/api/session": [_FakeResponse(json_body={"ok": True})],
        "preview": [_FakeResponse(json_body={
            "result": {"count": 1, "fields": ["one"], "rows": [["1"]]}
        })],
    })
    client = _client_with(session, fake_creds)
    out = client.preview_sql("SELECT 1 AS one")
    assert out == {"count": 1, "fields": ["one"], "rows": [["1"]]}

    # The submitted body must carry the canonical token, not the short release name.
    preview_call = [c for c in session.calls if "preview" in c[0]][0]
    body = preview_call[1]
    assert body["catalog_job"]["release_version"] == "hscla2020"
    assert body["clientVersion"] == 20190924.1
    assert body["credential"]["account_name"] == fake_creds.username


def test_run_sql_submits_polls_downloads_and_caches(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    # Two-step status: running -> done. download returns CSV with the # prefix.
    session = _FakeSession({
        "account/api/session": [_FakeResponse(json_body={"ok": True})],
        "submit": [_FakeResponse(json_body={
            "id": 42, "status": "waiting", "sql": "SELECT 1", "out_format": "csv",
        })],
        "status": [
            _FakeResponse(json_body={
                "id": 42, "status": "running",
                "sql": "SELECT 1", "out_format": "csv",
            }),
            _FakeResponse(json_body={
                "id": 42, "status": "done",
                "sql": "SELECT 1", "out_format": "csv",
            }),
        ],
        "download": [_FakeResponse(
            text="# one,msg\n1,hello\n",
            headers={"content-type": "text/csv"},
        )],
        "delete": [_FakeResponse(json_body={"ok": True})],
    })
    client = _client_with(session, fake_creds)

    df = client.run_sql(
        "SELECT 1 AS one, 'hello' AS msg",
        cache_dir=tmp_path / "sql",
        timeout=5.0,
    )
    assert list(df.columns) == ["one", "msg"]
    assert df.iloc[0]["msg"] == "hello"

    # On a second call the cached CSV is read and the network is not hit again.
    session.calls.clear()
    df2 = client.run_sql(
        "SELECT 1 AS one, 'hello' AS msg",
        cache_dir=tmp_path / "sql",
        timeout=5.0,
    )
    assert df2.equals(df)
    assert session.calls == []


def test_run_sql_raises_on_job_error(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession({
        "account/api/session": [_FakeResponse(json_body={"ok": True})],
        "submit": [_FakeResponse(json_body={
            "id": 9, "status": "waiting", "sql": "SELECT bogus", "out_format": "csv",
        })],
        "status": [_FakeResponse(json_body={
            "id": 9, "status": "error", "sql": "SELECT bogus",
            "out_format": "csv", "error": "column \"bogus\" does not exist",
        })],
    })
    client = _client_with(session, fake_creds)
    with pytest.raises(sql.JobError, match="bogus"):
        client.run_sql("SELECT bogus", cache_dir=tmp_path / "sql", timeout=5.0)


def test_run_sql_rejects_non_csv_format(fake_creds: config.Credentials) -> None:
    session = _FakeSession({"account/api/session": [_FakeResponse(json_body={"ok": True})]})
    client = _client_with(session, fake_creds)
    with pytest.raises(ValueError, match="csv"):
        client.run_sql("SELECT 1", out_format="fits")


def test_wait_for_job_times_out(fake_creds: config.Credentials) -> None:
    # Always-running, never-terminal job.
    statuses = [
        _FakeResponse(json_body={"id": 1, "status": "running", "sql": "", "out_format": "csv"})
        for _ in range(20)
    ]
    session = _FakeSession({
        "account/api/session": [_FakeResponse(json_body={"ok": True})],
        "status": statuses,
    })
    client = _client_with(session, fake_creds)
    elapsed: list[float] = []

    def fake_sleep(s: float) -> None:
        elapsed.append(s)

    # With timeout=0, the deadline is in the past before the first sleep, so we
    # raise immediately after the first status fetch.
    with pytest.raises(sql.JobTimeout):
        client.wait_for_job(1, timeout=0.0, poll_initial=1.0, sleep=fake_sleep)


# --------------------------------------------------------------------------- #
# Live test (opt-in via env var)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_preview_smallest_query() -> None:
    # Trivial query through preview; ~instant on the server side.
    out = sql.preview_sql("SELECT 1 AS one")
    assert out["count"] == 1
    assert out["fields"] == ["one"]
    assert out["rows"] == [["1"]]
