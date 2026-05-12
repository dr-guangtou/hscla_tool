"""HSCLA catalog SQL client.

Talks to the HSCLA SQL service at
``https://hscla.mtk.nao.ac.jp/datasearch/api/catalog_jobs/``. Endpoints
and request shapes were confirmed by live probe on 2026-05-12, and the
metadata lives in `data/hscla_db.yaml` under `sql_api` — this module
is the only place that turns that metadata into HTTP calls.

Two ways to query:

* ``preview_sql(sql)`` — fast (server-side ~5 s timeout), useful for
  small lookups (e.g. ``information_schema`` introspection,
  ``SELECT COUNT(*) ...``). Returns a parsed result dict.
* ``run_sql(sql)`` — full job pipeline: submit, poll, download CSV,
  return a `pandas.DataFrame`. Results are cached by content hash, so
  re-running the same query is a free local read.

Authentication is session-cookie based: we POST email + password to
``/account/api/session`` once, capture the ``LAAUTH_SESSION`` cookie,
and reuse it for every subsequent request. The catalog-job endpoints
also require the credential repeated in the JSON body plus a
``clientVersion`` float — both are added automatically.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pandas as pd
import requests

from hscla_tool import config, db

LOGGER = logging.getLogger(__name__)

DEFAULT_RELEASE = "la2020"
DEFAULT_OUT_FORMAT = "csv"
DEFAULT_HTTP_TIMEOUT = 60.0           # seconds, per HTTP call
DEFAULT_JOB_TIMEOUT = 45 * 60.0       # seconds, total wall time for run_sql
DEFAULT_POLL_INITIAL = 1.0            # seconds, first sleep
DEFAULT_POLL_MAX = 60.0               # seconds, ceiling on the backoff


class SqlError(RuntimeError):
    """Base class for HSCLA SQL client failures."""


class JobError(SqlError):
    """Raised when a submitted SQL job finishes in a non-`done` state."""

    def __init__(self, job: "Job"):
        self.job = job
        msg = job.raw.get("error") or f"job {job.id} ended with status={job.status!r}"
        super().__init__(msg)


class JobTimeout(SqlError):
    """Raised when a job exceeds the wall-clock budget given to `wait_for_job`."""


@dataclass(frozen=True)
class Job:
    """A snapshot of one catalog-job's state returned by the server."""

    id: int
    status: str
    out_format: str
    sql: str
    raw: dict = field(repr=False)

    @classmethod
    def from_response(cls, payload: dict) -> "Job":
        return cls(
            id=int(payload["id"]),
            status=str(payload["status"]),
            out_format=str(payload.get("out_format", "")),
            sql=str(payload.get("sql", "")),
            raw=payload,
        )


class HscLaClient:
    """Thin, session-cookie aware HTTP client for the HSCLA SQL API."""

    def __init__(
        self,
        credentials: config.Credentials | None = None,
        *,
        session: requests.Session | None = None,
        release: str = DEFAULT_RELEASE,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.credentials = credentials or config.load_credentials()
        self.release = release
        self.timeout = timeout
        self._session = session or requests.Session()
        self._logged_in = False
        api = db.get_sql_api()
        self._login_url: str = api["login_url"]
        self._base_url: str = api["base_url"].rstrip("/") + "/"
        self._endpoints: dict[str, str] = dict(api["endpoints"])
        self._client_version: float = float(api["client_version"])
        statuses = api["status_values"]
        self._in_progress: set[str] = set(statuses["in_progress"])
        self._terminal: set[str] = set(statuses["terminal"])

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #

    def login(self) -> None:
        """Authenticate against the HSCLA account service and capture the session cookie."""

        if self._logged_in:
            return
        resp = self._session.post(
            self._login_url,
            json={"email": self.credentials.username, "password": self.credentials.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise SqlError(
                f"HSCLA login failed: {resp.status_code} {resp.reason} — "
                f"check that {config.USERNAME_ENV}/{config.PASSWORD_ENV} are correct."
            )
        if "LAAUTH_SESSION" not in self._session.cookies:
            raise SqlError("HSCLA login returned 200 but did not set LAAUTH_SESSION cookie.")
        self._logged_in = True
        LOGGER.debug("HSCLA login ok, session cookie set")

    def _post(self, endpoint_key: str, body: dict[str, Any], *, stream: bool = False) -> requests.Response:
        self.login()
        url = self._base_url + self._endpoints[endpoint_key]
        full_body = {
            "credential": {
                "account_name": self.credentials.username,
                "password": self.credentials.password,
            },
            "clientVersion": self._client_version,
            **body,
        }
        resp = self._session.post(url, json=full_body, timeout=self.timeout, stream=stream)
        if not stream and resp.status_code >= 400:
            raise SqlError(f"{endpoint_key} -> {resp.status_code}: {resp.text.strip()[:400]}")
        return resp

    # ------------------------------------------------------------------ #
    # SQL operations
    # ------------------------------------------------------------------ #

    def preview_sql(self, sql: str, *, release: str | None = None) -> dict[str, Any]:
        """Run a small, fast SQL query through the preview endpoint.

        The server enforces a short timeout (~5 s) on previews, so this
        is appropriate for `information_schema` lookups, `COUNT(*)`,
        and other tiny queries. Returns the parsed JSON `result`
        payload: `{"count": int, "fields": [str, ...], "rows": [[...], ...]}`.
        """

        token = db.get_release_version_token(release or self.release)
        body = {"catalog_job": {"sql": sql, "release_version": token}}
        resp = self._post("preview", body)
        payload = resp.json()
        if "result" not in payload:
            raise SqlError(f"preview response had no 'result': {payload!r}")
        return dict(payload["result"])

    def submit_sql(
        self,
        sql: str,
        *,
        release: str | None = None,
        out_format: str = DEFAULT_OUT_FORMAT,
        include_metainfo: bool = False,
        skip_syntax_check: bool = False,
        nomail: bool = True,
    ) -> Job:
        """Submit a SQL job and return the initial Job snapshot."""

        token = db.get_release_version_token(release or self.release)
        body = {
            "catalog_job": {
                "sql": sql,
                "out_format": out_format,
                "include_metainfo_to_body": include_metainfo,
                "release_version": token,
            },
            "nomail": nomail,
            "skip_syntax_check": skip_syntax_check,
        }
        resp = self._post("submit", body)
        job = Job.from_response(resp.json())
        LOGGER.info("submitted HSCLA SQL job %d (status=%s)", job.id, job.status)
        return job

    def job_status(self, job_id: int) -> Job:
        """Poll the server once and return the latest Job snapshot."""

        resp = self._post("status", {"id": int(job_id)})
        return Job.from_response(resp.json())

    def cancel_job(self, job_id: int) -> None:
        """Stop a running job. Safe to call on already-terminal jobs."""

        self._post("cancel", {"id": int(job_id)})

    def delete_job(self, job_id: int) -> None:
        """Delete a finished job and its output from the server."""

        self._post("delete", {"id": int(job_id)})

    def wait_for_job(
        self,
        job_id: int,
        *,
        timeout: float = DEFAULT_JOB_TIMEOUT,
        poll_initial: float = DEFAULT_POLL_INITIAL,
        poll_max: float = DEFAULT_POLL_MAX,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Job:
        """Poll until the job reaches a terminal status, with exponential backoff."""

        deadline = time.monotonic() + timeout
        interval = poll_initial
        while True:
            job = self.job_status(job_id)
            if job.status in self._terminal:
                LOGGER.info("job %d finished with status=%s", job.id, job.status)
                if job.status != "done":
                    raise JobError(job)
                return job
            if job.status not in self._in_progress:
                LOGGER.warning("job %d has unexpected status %r", job.id, job.status)
            now = time.monotonic()
            if now >= deadline:
                raise JobTimeout(
                    f"job {job_id} still {job.status!r} after {timeout:.0f} s; "
                    f"cancel it manually or call wait_for_job with a larger timeout."
                )
            sleep(min(interval, deadline - now))
            interval = min(interval * 2.0, poll_max)

    def download_job(self, job_id: int, dest: Path) -> Path:
        """Download a finished job's output to `dest` (parent dirs auto-created)."""

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self._post("download", {"id": int(job_id)}, stream=True)
        if resp.status_code >= 400:
            raise SqlError(f"download failed: {resp.status_code} {resp.text.strip()[:400]}")
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
        return dest

    # ------------------------------------------------------------------ #
    # High-level convenience
    # ------------------------------------------------------------------ #

    def run_sql(
        self,
        sql: str,
        *,
        release: str | None = None,
        out_format: str = DEFAULT_OUT_FORMAT,
        cache: bool = True,
        cache_dir: Path | None = None,
        timeout: float = DEFAULT_JOB_TIMEOUT,
        delete_after: bool = True,
    ) -> pd.DataFrame:
        """Submit a SQL job, wait for it, download the result, return a DataFrame.

        Only `csv` and `csv.gz` are wired up here for `pandas` parsing.
        For other formats (`fits`, `sqlite3`, ...) use `submit_sql` +
        `wait_for_job` + `download_job` directly.
        """

        if out_format not in {"csv", "csv.gz"}:
            raise ValueError(
                f"run_sql currently understands csv / csv.gz only (got {out_format!r}); "
                f"use submit_sql + download_job for {out_format!r}."
            )
        release = release or self.release
        cache_root = Path(cache_dir) if cache_dir is not None else config.cache_dir() / "sql"
        cache_path = cache_root / f"{_cache_key(sql, release, out_format)}.{out_format}"

        if cache and cache_path.is_file():
            LOGGER.info("cache hit for SQL query: %s", cache_path)
            return _read_sql_csv(cache_path)

        job = self.submit_sql(sql, release=release, out_format=out_format)
        finished = self.wait_for_job(job.id, timeout=timeout)
        try:
            self.download_job(finished.id, cache_path)
        finally:
            if delete_after:
                try:
                    self.delete_job(finished.id)
                except SqlError as exc:
                    LOGGER.warning("could not delete job %d on the server: %s", finished.id, exc)
        return _read_sql_csv(cache_path)


# --------------------------------------------------------------------------- #
# Module-level convenience
# --------------------------------------------------------------------------- #


def run_sql(sql: str, **kwargs: Any) -> pd.DataFrame:
    """One-line shortcut: instantiate a client and run the query."""

    return HscLaClient().run_sql(sql, **kwargs)


def preview_sql(sql: str, **kwargs: Any) -> dict[str, Any]:
    """One-line shortcut for the preview endpoint."""

    return HscLaClient().preview_sql(sql, **kwargs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cache_key(sql: str, release: str, out_format: str) -> str:
    payload = json.dumps({"sql": sql, "release": release, "fmt": out_format}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _read_sql_csv(path: Path) -> pd.DataFrame:
    """Parse an HSCLA CSV download into a DataFrame.

    Every HSCLA CSV starts with one or more lines prefixed by ``#``.
    With ``include_metainfo_to_body=False`` (our default) there is just
    one such line — the column header — and the rest is data. With
    metainfo on, there are several ``#``-prefixed metadata lines and
    the *last* one before the data is the header. This routine handles
    both shapes by finding the run of leading ``#`` lines and treating
    its final entry as the header row, stripped of its prefix.
    """

    if path.suffix == ".gz":
        import gzip
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("#"):
            header_idx = i
        else:
            break
    if header_idx < 0:
        return pd.read_csv(io.StringIO(text))
    header = lines[header_idx].lstrip("#").lstrip()
    data = "\n".join([header, *lines[header_idx + 1:]])
    return pd.read_csv(io.StringIO(data))
