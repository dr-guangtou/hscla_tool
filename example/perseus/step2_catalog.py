"""Step 2 of the Perseus example: per-tract coadd-level photometric catalogs.

For each tract that overlaps the 2° circle around NGC 1275, submit one
HSCLA SQL job per la2020 host table that holds curated columns ported
from `hsc_sandbox/step1` (see `build_la2020_sql.py`). Each job applies
the same forced-side selection:

    f.isprimary AND tractSearch(f.object_id, <tract>)

The split-then-merge pattern follows the sandbox recipe: each per-host
file is self-contained on object_id, and the per-tract union is built
locally with `step2_catalog_merge.py` (separate driver, not part of
this step).

Per-tract artefacts land at::

    /Volumes/galaxy/data/perseus/catalogs/tract_<tract>/la2020_<host>.csv.gz

A small JSON manifest tracks job state so re-runs only redo missing
tables.

Run from the repo root:

    uv run python example/perseus/step2_catalog.py --smoke         # tiny LIMIT
    uv run python example/perseus/step2_catalog.py --tract 15548   # one tract
    uv run python example/perseus/step2_catalog.py                 # all four
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hscla_tool import sql

LOGGER = logging.getLogger("perseus.step2")

TRACTS: tuple[int, ...] = (15548, 15549, 15733, 15734)

SQL_DIR = Path(__file__).resolve().parent / "sql"
TEMPLATE_GLOB = "la2020_*.sql.tmpl"

CATALOG_ROOT_DEFAULT = Path("/Volumes/galaxy/data/perseus/catalogs")
MANIFEST_NAME = "manifest.json"
OUT_FORMAT = "csv.gz"


# --------------------------------------------------------------------------- #
# Host discovery + SQL building
# --------------------------------------------------------------------------- #


def discover_hosts() -> list[str]:
    """la2020 host names, in alphabetical order, derived from template files."""

    hosts: list[str] = []
    for path in sorted(SQL_DIR.glob(TEMPLATE_GLOB)):
        # filename: la2020_<host>.sql.tmpl
        stem = path.name.removeprefix("la2020_").removesuffix(".sql.tmpl")
        hosts.append(stem)
    if not hosts:
        raise FileNotFoundError(
            f"no SQL templates under {SQL_DIR}; "
            "run `uv run python example/perseus/build_la2020_sql.py` first."
        )
    return hosts


def template_path(host: str) -> Path:
    return SQL_DIR / f"la2020_{host}.sql.tmpl"


def build_sql(tract: int, host: str, *, limit: int | None = None) -> str:
    body = template_path(host).read_text().rstrip().format(tract=int(tract))
    if limit:
        body = body + f"\nLIMIT {int(limit)}"
    return body


# --------------------------------------------------------------------------- #
# Output layout + manifest
# --------------------------------------------------------------------------- #


def tract_dir(root: Path, tract: int) -> Path:
    return root / f"tract_{int(tract)}"


def output_path(root: Path, tract: int, host: str) -> Path:
    return tract_dir(root, tract) / f"la2020_{host}.{OUT_FORMAT}"


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME


def load_manifest(root: Path) -> dict[str, dict]:
    path = manifest_path(root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def save_manifest(root: Path, manifest: dict[str, dict]) -> None:
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def manifest_key(tract: int, host: str) -> str:
    return f"{int(tract)}/la2020_{host}"


# --------------------------------------------------------------------------- #
# Job runner
# --------------------------------------------------------------------------- #


@dataclass
class JobResult:
    tract: int
    host: str
    status: str          # "done" | "skipped" | "error"
    path: Path | None
    sql: str
    job_id: int | None
    elapsed_s: float
    error: str | None = None


def run_one(
    client: sql.HscLaClient,
    tract: int,
    host: str,
    *,
    root: Path,
    limit: int | None,
    skip_existing: bool,
    job_timeout: float,
) -> JobResult:
    """Submit + poll + download a single (tract, host) job."""

    sql_text = build_sql(tract, host, limit=limit)
    out_path = output_path(root, tract, host)
    started = time.monotonic()
    label = f"tract={tract} host={host}"

    if skip_existing and out_path.is_file():
        LOGGER.info("%s: skip (file exists at %s)", label, out_path)
        return JobResult(
            tract=tract, host=host, status="skipped", path=out_path,
            sql=sql_text, job_id=None, elapsed_s=0.0,
        )

    LOGGER.info("%s: submit", label)
    try:
        job = client.submit_sql(sql_text, out_format=OUT_FORMAT)
    except sql.SqlError as exc:
        LOGGER.error("%s: submit failed: %s", label, exc)
        return JobResult(
            tract=tract, host=host, status="error", path=None,
            sql=sql_text, job_id=None, elapsed_s=time.monotonic() - started,
            error=str(exc),
        )

    LOGGER.info("%s: job_id=%d submitted, polling", label, job.id)
    try:
        finished = client.wait_for_job(job.id, timeout=job_timeout)
    except (sql.JobError, sql.JobTimeout) as exc:
        LOGGER.error("%s: job_id=%d failed: %s", label, job.id, exc)
        return JobResult(
            tract=tract, host=host, status="error", path=None,
            sql=sql_text, job_id=job.id, elapsed_s=time.monotonic() - started,
            error=str(exc),
        )

    LOGGER.info("%s: downloading -> %s", label, out_path)
    try:
        client.download_job(finished.id, out_path)
    finally:
        try:
            client.delete_job(finished.id)
        except sql.SqlError as exc:
            LOGGER.warning("%s: could not delete job %d: %s", label, finished.id, exc)

    elapsed = time.monotonic() - started
    LOGGER.info("%s: done in %.1f s (%.2f MB)",
                label, elapsed, out_path.stat().st_size / 1e6)
    return JobResult(
        tract=tract, host=host, status="done", path=out_path,
        sql=sql_text, job_id=finished.id, elapsed_s=elapsed,
    )


def run_all(
    tracts: tuple[int, ...],
    hosts: tuple[str, ...],
    *,
    root: Path,
    limit: int | None,
    skip_existing: bool,
    job_timeout: float,
) -> list[JobResult]:
    client = sql.HscLaClient()
    manifest = load_manifest(root)
    results: list[JobResult] = []
    for tract in tracts:
        tract_dir(root, tract).mkdir(parents=True, exist_ok=True)
        for host in hosts:
            result = run_one(
                client, tract, host,
                root=root, limit=limit,
                skip_existing=skip_existing,
                job_timeout=job_timeout,
            )
            results.append(result)
            manifest[manifest_key(tract, host)] = {
                "tract": tract,
                "host": host,
                "status": result.status,
                "job_id": result.job_id,
                "path": str(result.path) if result.path else None,
                "elapsed_s": round(result.elapsed_s, 1),
                "limit": limit,
                "error": result.error,
            }
            save_manifest(root, manifest)
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tract", type=int, action="append",
        help="Tract id; may be passed multiple times. Default: all four.",
    )
    parser.add_argument(
        "--host", type=str, action="append",
        help="la2020 host table name (e.g. 'forced', 'meas_cmodel'). "
             "May be passed multiple times. Default: all discovered.",
    )
    parser.add_argument(
        "--root", type=Path, default=CATALOG_ROOT_DEFAULT,
        help=f"Output root directory (default: {CATALOG_ROOT_DEFAULT}).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="If set, append LIMIT N to every query (smoke-test mode).",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="Shortcut for --limit 100.",
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true",
        help="Re-fetch even if the output file already exists.",
    )
    parser.add_argument(
        "--job-timeout", type=float, default=2.0 * 3600.0,
        help="Per-job wall-clock timeout in seconds (default 2 h).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the SQL that would be submitted, then exit.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    all_hosts = discover_hosts()
    tracts = tuple(args.tract) if args.tract else TRACTS
    hosts = tuple(args.host) if args.host else tuple(all_hosts)
    unknown = [h for h in hosts if h not in all_hosts]
    if unknown:
        LOGGER.error("unknown host(s): %s; known: %s", unknown, all_hosts)
        return 2

    limit = 100 if args.smoke else args.limit
    skip_existing = not args.no_skip_existing

    if args.dry_run:
        for tract in tracts:
            for host in hosts:
                print(f"-- tract {tract} | host {host}")
                print(build_sql(tract, host, limit=limit))
                print()
        return 0

    if not os.path.isdir(args.root.parent):
        LOGGER.error("output root parent does not exist: %s. "
                     "Mount /Volumes/galaxy or pass --root.", args.root.parent)
        return 2

    args.root.mkdir(parents=True, exist_ok=True)
    LOGGER.info("output root: %s", args.root)
    LOGGER.info("tracts: %s", ", ".join(map(str, tracts)))
    LOGGER.info("hosts: %s", ", ".join(hosts))
    if limit:
        LOGGER.info("smoke / LIMIT mode: %d rows per query", limit)

    results = run_all(
        tracts, hosts,
        root=args.root,
        limit=limit,
        skip_existing=skip_existing,
        job_timeout=args.job_timeout,
    )

    n_done = sum(1 for r in results if r.status == "done")
    n_skip = sum(1 for r in results if r.status == "skipped")
    n_err = sum(1 for r in results if r.status == "error")
    LOGGER.info("summary: %d done, %d skipped, %d errors", n_done, n_skip, n_err)
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
