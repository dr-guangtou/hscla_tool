"""Step 2 (file-archive flavor): bulk-fetch per-patch forced_src FITS.

Iterates over the patches enumerated in `notes/step1_patches.csv` and
downloads the per-patch forced-photometry catalog from the HSCLA file
archive (one FITS per `(band, tract, patch)`). Serial by default; the
archive module handles resume-from-interrupt via a `.tmp` sidecar and
HTTP `Range:` request.

Output layout (matches the archive module's cache convention)::

    /Volumes/galaxy/data/perseus/archive/<band>/<tract>/<patch>/forced_src-...fits

A small JSON manifest tracks completion so re-runs only download
missing files.

Run from the repo root:

    uv run python example/perseus/step2_archive_fetch.py --tract 15548  # pilot
    uv run python example/perseus/step2_archive_fetch.py               # all 4 tracts
    uv run python example/perseus/step2_archive_fetch.py --kind meas   # also pull meas
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

import pandas as pd

from hscla_tool import archive

LOGGER = logging.getLogger("perseus.step2.archive")

EXAMPLE_DIR = Path(__file__).resolve().parent
DEFAULT_PATCHES_CSV = EXAMPLE_DIR / "notes" / "step1_patches.csv"
DEFAULT_ROOT = Path("/Volumes/galaxy/data/perseus")
MANIFEST_NAME = "archive_manifest.json"


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FetchPlan:
    band: str
    tract: int
    patch: str        # canonical 'x,y' form (matches HSCLA URLs)
    kind: str

    @property
    def key(self) -> str:
        return f"{self.band}/{self.tract}/{self.patch}/{self.kind}"


def load_plan(
    patches_csv: Path,
    *,
    tracts: tuple[int, ...] | None,
    bands: tuple[str, ...] | None,
    kinds: tuple[str, ...],
) -> list[FetchPlan]:
    df = pd.read_csv(patches_csv)
    if tracts:
        df = df[df["tract"].isin(tracts)]
    if bands:
        df = df[df["band"].isin(bands)]
    df = df.sort_values(["band", "tract", "patch_s"])
    plans: list[FetchPlan] = []
    for _, row in df.iterrows():
        for kind in kinds:
            plans.append(FetchPlan(
                band=str(row.band),
                tract=int(row.tract),
                patch=str(row.patch_s),
                kind=kind,
            ))
    return plans


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def fetch_one(
    client: archive.HscLaArchiveClient,
    plan: FetchPlan,
    *,
    root: Path,
) -> tuple[str, dict]:
    """Return (status, manifest_entry) for one (band, tract, patch, kind)."""

    dest = root / "archive" / plan.band / str(plan.tract) / plan.patch \
        / f"{plan.kind}-{plan.band}-{plan.tract}-{plan.patch}.fits"
    started = time.monotonic()
    if dest.is_file():
        return "skipped", {
            "status": "skipped",
            "path": str(dest),
            "bytes": dest.stat().st_size,
            "elapsed_s": 0.0,
            "error": None,
        }
    try:
        result = client.download_patch_file(
            tract=plan.tract,
            patch=plan.patch,
            band=plan.band,
            kind=plan.kind,
            dest=dest,
        )
    except archive.ArchiveError as exc:
        return "error", {
            "status": "error",
            "path": None,
            "bytes": 0,
            "elapsed_s": round(time.monotonic() - started, 1),
            "error": str(exc),
        }
    elapsed = time.monotonic() - started
    return "done", {
        "status": "done",
        "path": str(result.path),
        "bytes": int(result.bytes),
        "elapsed_s": round(elapsed, 1),
        "error": None,
    }


def run(
    plans: list[FetchPlan],
    *,
    root: Path,
) -> dict[str, int]:
    client = archive.HscLaArchiveClient()
    manifest = load_manifest(root)
    counts = {"done": 0, "skipped": 0, "error": 0}
    total = len(plans)
    bytes_done = 0
    started_all = time.monotonic()
    for idx, plan in enumerate(plans, 1):
        label = f"[{idx}/{total}] {plan.key}"
        LOGGER.info("%s: start", label)
        status, entry = fetch_one(client, plan, root=root)
        entry.update(band=plan.band, tract=plan.tract,
                     patch=plan.patch, kind=plan.kind)
        manifest[plan.key] = entry
        save_manifest(root, manifest)
        counts[status] += 1
        if entry["bytes"]:
            bytes_done += int(entry["bytes"])
        elapsed_total = time.monotonic() - started_all
        rate = (bytes_done / 1e6) / elapsed_total if elapsed_total > 0 else 0.0
        LOGGER.info("%s: %s (%.1f MB, %.1f s, running %.1f MB/s)",
                    label, status, entry["bytes"] / 1e6,
                    entry["elapsed_s"], rate)
    return counts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patches-csv", type=Path, default=DEFAULT_PATCHES_CSV,
        help=f"Path to step-1 patches CSV (default: {DEFAULT_PATCHES_CSV}).",
    )
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT,
        help=f"Output root (default: {DEFAULT_ROOT}).",
    )
    parser.add_argument(
        "--tract", type=int, action="append",
        help="Tract id; may be passed multiple times. Default: all in CSV.",
    )
    parser.add_argument(
        "--band", type=str, action="append",
        help="Band name (e.g. HSC-I); may be passed multiple times. "
             "Default: all in CSV.",
    )
    parser.add_argument(
        "--kind", type=str, action="append",
        help="Per-patch file kind (e.g. forced_src, meas). "
             "Default: forced_src only.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the fetch plan and exit.",
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

    if not args.patches_csv.is_file():
        LOGGER.error("patches CSV not found: %s", args.patches_csv)
        return 2

    tracts = tuple(args.tract) if args.tract else None
    bands = tuple(args.band) if args.band else None
    kinds = tuple(args.kind) if args.kind else ("forced_src",)

    plans = load_plan(args.patches_csv, tracts=tracts, bands=bands, kinds=kinds)
    if not plans:
        LOGGER.warning("no patches to fetch (filters cut everything).")
        return 0

    LOGGER.info("fetch plan: %d files (kinds=%s, tracts=%s, bands=%s)",
                len(plans), kinds, tracts or "all", bands or "all")

    if args.dry_run:
        for p in plans:
            print(p.key)
        return 0

    if not os.path.isdir(args.root.parent):
        LOGGER.error("output root parent does not exist: %s. "
                     "Mount /Volumes/galaxy or pass --root.", args.root.parent)
        return 2
    args.root.mkdir(parents=True, exist_ok=True)
    LOGGER.info("output root: %s", args.root)

    counts = run(plans, root=args.root)
    LOGGER.info("summary: %d done, %d skipped, %d errors",
                counts["done"], counts["skipped"], counts["error"])
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
