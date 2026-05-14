"""Translate the `hsc_sandbox/step1` curated SQL into la2020 column lists.

The sandbox stores ~662 hand-curated columns across 9 partner SQL files
(`s23b_wide_forced_<partner>_selected.sql`). Each file selects from
`s23b_wide.forced` (alias `f1`) plus one partner table. The la2020
schema has reshuffled which sub-table holds which column, so the
sandbox SQL cannot be used verbatim.

This script:

1. Parses every sandbox `*_selected.sql` and emits the curated
   (source_table, column_name) inventory.
2. Probes la2020.information_schema for each column name and finds the
   la2020 sub-table(s) it lives in.
3. Picks one la2020 host per column (preferring `forced`, then the
   sandbox-side hint, then any other match), and groups columns by host.
4. Writes a YAML mapping and per-host SQL templates under
   `example/perseus/sql/`. Each template selects exactly those columns
   for one tract: `INNER JOIN la2020.forced ... WHERE isprimary AND
   tractSearch(...)`.

Run from the repo root:

    uv run python example/perseus/build_la2020_sql.py

Outputs (deterministic, committed):

    example/perseus/sql/columns_la2020.yaml
    example/perseus/sql/missing_in_la2020.txt
    example/perseus/sql/la2020_<host>.sql.tmpl  (one per la2020 host)
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import yaml

from hscla_tool import sql

# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_SQL_DIR = Path(
    "/Users/shuang/Dropbox/work/project/otters/hsc_sandbox/step1/sql"
)
OUT_DIR = REPO_ROOT / "example" / "perseus" / "sql"

# Each pairwise sandbox file uses one of these alias→table maps. We hard-
# code rather than parse the FROM clause; the sandbox layout is stable.
PAIRWISE_FILES: dict[str, dict[str, str]] = {
    "s23b_wide_forced_forced2_selected.sql":  {"f1": "forced", "f2": "forced2"},
    "s23b_wide_forced_forced3_selected.sql":  {"f1": "forced", "f3": "forced3"},
    "s23b_wide_forced_forced4_selected.sql":  {"f1": "forced", "f4": "forced4"},
    "s23b_wide_forced_forced5_selected.sql":  {"f1": "forced", "f5": "forced5"},
    "s23b_wide_forced_forced6_selected.sql":  {"f1": "forced", "f6": "forced6"},
    "s23b_wide_forced_masks_selected.sql":    {"f1": "forced", "msk": "masks"},
    "s23b_wide_forced_meas_selected.sql":     {"f1": "forced", "m1": "meas"},
    "s23b_wide_forced_meas2_selected.sql":    {"f1": "forced", "m2": "meas2"},
    "s23b_wide_forced_photoz_mizuki_selected.sql": {"f1": "forced", "p1": "photoz_mizuki"},
}

# Column-name regex: `<alias>.<column>,?` on its own line.
COL_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*,?\s*$")

# When a column exists in multiple la2020 tables, prefer the host in this order.
HOST_PRIORITY: tuple[str, ...] = (
    "forced",
    "forced_aper", "forced_conv", "forced_conv_flag",
    "forced_flux", "forced_other",
    "forced_undeb_aper", "forced_undeb_conv", "forced_undeb_conv_flag",
    "meas",
    "meas_aper", "meas_centroid", "meas_cmodel",
    "meas_conv", "meas_conv_flag", "meas_flux", "meas_hsm", "meas_other",
)


# --------------------------------------------------------------------------- #
# Step 1: parse sandbox SQL
# --------------------------------------------------------------------------- #


def parse_sandbox_columns() -> dict[str, set[str]]:
    """Return {sandbox_table: set(column_names)} across all pairwise files."""

    by_table: dict[str, set[str]] = defaultdict(set)
    for fname, alias_map in PAIRWISE_FILES.items():
        path = SANDBOX_SQL_DIR / fname
        if not path.is_file():
            raise FileNotFoundError(path)
        for line in path.read_text().splitlines():
            match = COL_PATTERN.match(line)
            if not match:
                continue
            alias, column = match.group(1), match.group(2)
            if alias not in alias_map:
                continue
            sandbox_table = alias_map[alias]
            by_table[sandbox_table].add(column.lower())
    return dict(by_table)


# --------------------------------------------------------------------------- #
# Step 2: probe la2020 for each column's host table
# --------------------------------------------------------------------------- #


def fetch_la2020_column_homes(columns: set[str]) -> dict[str, list[str]]:
    """For each lowercase column name, return the la2020 tables that have it.

    Uses one information_schema query batched over the full set.
    """

    if not columns:
        return {}
    name_list = ",".join("'" + c.replace("'", "''") + "'" for c in sorted(columns))
    sql_text = (
        "SELECT column_name, table_name "
        "FROM information_schema.columns "
        f"WHERE table_schema='la2020' AND column_name IN ({name_list}) "
        "ORDER BY column_name, table_name"
    )
    payload = sql.preview_sql(sql_text)
    homes: dict[str, list[str]] = defaultdict(list)
    for col, tab in payload["rows"]:
        homes[col].append(tab)
    return dict(homes)


# --------------------------------------------------------------------------- #
# Step 3: pick a single host per column
# --------------------------------------------------------------------------- #


def pick_host(candidates: list[str], sandbox_hint: str) -> str | None:
    """Choose the best la2020 host table for one column.

    Priority:
      1. The sandbox source table name itself, if present in la2020.
         (covers forced→forced, meas→meas: exact match.)
      2. Tables in `HOST_PRIORITY` order (favoring forced first).
      3. Anything we can find.
    """

    if not candidates:
        return None
    if sandbox_hint in candidates:
        return sandbox_hint
    for table in HOST_PRIORITY:
        if table in candidates:
            return table
    return candidates[0]


def build_host_groups(
    sandbox_columns: dict[str, set[str]],
    la2020_homes: dict[str, list[str]],
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Group sandbox columns by chosen la2020 host table."""

    chosen: dict[str, set[str]] = defaultdict(set)
    missing: dict[str, list[str]] = defaultdict(list)  # host hint -> cols not in la2020
    for sandbox_table, cols in sandbox_columns.items():
        for col in cols:
            candidates = la2020_homes.get(col, [])
            host = pick_host(candidates, sandbox_hint=sandbox_table)
            if host is None:
                missing[sandbox_table].append(col)
                continue
            chosen[host].add(col)
    return dict(chosen), dict(missing)


# --------------------------------------------------------------------------- #
# Step 4: emit per-host SQL templates
# --------------------------------------------------------------------------- #


def make_sql_template(host: str, columns: list[str], *, has_object_id: bool) -> str:
    """Build a per-tract SQL template for one la2020 host table.

    The host's columns are emitted as `host_alias.<col>`. We always pull
    `object_id` first so downstream merges work; if the host is `forced`
    itself, we drop the redundant join.
    """

    base_cols = ["object_id"] + [c for c in columns if c != "object_id"]
    if host == "forced":
        select_lines = [f"  f.{c}" for c in base_cols]
        body = (
            "SELECT\n"
            + ",\n".join(select_lines)
            + "\nFROM la2020.forced AS f\n"
            "WHERE f.isprimary\n"
            "  AND tractSearch(f.object_id, {tract})"
        )
    else:
        # Pull object_id from t (the partner has it) so the file is
        # self-contained, but use f for the WHERE clause.
        select_lines = [f"  t.{c}" for c in base_cols]
        body = (
            "SELECT\n"
            + ",\n".join(select_lines)
            + f"\nFROM la2020.{host} AS t\n"
            "INNER JOIN la2020.forced AS f ON f.object_id = t.object_id\n"
            "WHERE f.isprimary\n"
            "  AND tractSearch(f.object_id, {tract})"
        )
    if not has_object_id:
        body = "-- WARNING: partner has no object_id column (impossible per probe).\n" + body
    return body + "\n"


def write_outputs(
    chosen: dict[str, set[str]],
    missing: dict[str, list[str]],
    sandbox_columns: dict[str, set[str]],
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # YAML mapping for inspection / downstream
    mapping = {host: sorted(cols) for host, cols in chosen.items()}
    mapping = dict(sorted(mapping.items()))
    yaml_path = OUT_DIR / "columns_la2020.yaml"
    yaml_path.write_text(yaml.safe_dump(mapping, sort_keys=True))
    print(f"wrote {yaml_path} ({sum(len(v) for v in mapping.values())} columns "
          f"across {len(mapping)} hosts)")

    # Missing-columns log (sandbox columns that don't exist in la2020 at all)
    miss_lines: list[str] = []
    for sandbox_table, cols in sorted(missing.items()):
        miss_lines.append(f"# {sandbox_table}: {len(cols)} columns not in la2020")
        miss_lines.extend(sorted(cols))
        miss_lines.append("")
    miss_path = OUT_DIR / "missing_in_la2020.txt"
    miss_path.write_text("\n".join(miss_lines) + "\n")
    print(f"wrote {miss_path} ({sum(len(v) for v in missing.values())} columns "
          f"with no la2020 home)")

    # Per-host SQL templates with a {tract} placeholder
    for host, cols in mapping.items():
        sql_path = OUT_DIR / f"la2020_{host}.sql.tmpl"
        sql_path.write_text(make_sql_template(host, cols, has_object_id=True))
        print(f"wrote {sql_path} ({len(cols)} columns)")

    # Per-sandbox-table provenance (for traceability)
    prov_path = OUT_DIR / "sandbox_provenance.yaml"
    prov_path.write_text(
        yaml.safe_dump(
            {tab: sorted(cols) for tab, cols in sorted(sandbox_columns.items())},
            sort_keys=True,
        )
    )
    print(f"wrote {prov_path}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    sandbox_columns = parse_sandbox_columns()
    print("sandbox column counts per source table:")
    for tab, cols in sorted(sandbox_columns.items()):
        print(f"  {tab}: {len(cols)}")

    all_cols = {c for cols in sandbox_columns.values() for c in cols}
    print(f"total distinct sandbox columns: {len(all_cols)}")

    la2020_homes = fetch_la2020_column_homes(all_cols)
    print(f"la2020 has {len(la2020_homes)} of those columns")

    chosen, missing = build_host_groups(sandbox_columns, la2020_homes)
    print("la2020 host -> column count:")
    for host, cols in sorted(chosen.items()):
        print(f"  {host}: {len(cols)}")

    if missing:
        print("dropped (no la2020 home):")
        for tab, cols in sorted(missing.items()):
            print(f"  {tab}: {len(cols)}")

    write_outputs(chosen, missing, sandbox_columns)


if __name__ == "__main__":
    main()
