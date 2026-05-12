"""`hscla_tool` — knowledge base and Python toolkit for HSCLA2020.

Importing this package checks your HSCLA login right away (env vars
`HSCLA_USR` and `HSCLA_PWD`). If either is missing, the import fails
with a clear error. See `docs/SPEC.md` for the rationale and
`data/hscla_db.yaml` for the structured catalog of HSCLA endpoints,
tables, and test fixtures.
"""

from __future__ import annotations

from hscla_tool.config import (
    Credentials,
    MissingCredentialsError,
    cache_dir,
    load_credentials,
)

__version__ = "0.0.1"

# Verify the HSCLA login at import time. Deliberate; see SPEC §5.1.
load_credentials()

__all__ = [
    "Credentials",
    "MissingCredentialsError",
    "__version__",
    "cache_dir",
    "load_credentials",
]
