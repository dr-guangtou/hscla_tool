"""HSCLA login and download-folder settings.

This module is the only place in the package that reads environment
variables. Everything else asks here.

Two env vars are required for any real work:

- `HSCLA_USR`: HSCLA account name
- `HSCLA_PWD`: HSCLA account password

These are read at import time of the top-level package (see
`hscla_tool/__init__.py`). If either is missing, the import raises
`MissingCredentialsError` immediately. This is deliberate: we would
rather you find out you forgot to set them now than three hours into a
download script. They normally live in `~/.zshenv` (loaded by every
zsh shell, login or not).

One optional env var controls where downloads go:

- `HSCLA_TOOL_CACHE`: absolute path to the folder where cutouts, PSFs,
  SQL results, etc. should be saved. If unset, we use `./outputs/`
  relative to the current working directory for small / test runs.
  Production callers should set this (or pass an explicit path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

USERNAME_ENV = "HSCLA_USR"
PASSWORD_ENV = "HSCLA_PWD"
CACHE_ENV = "HSCLA_TOOL_CACHE"
DEFAULT_CACHE_DIR = Path("outputs")
MIRROR_ROOT_ENV = "HSCLA_MIRROR_ROOT"
DEFAULT_MIRROR_ROOT = Path("/Volumes/galaxy/hsc/la2020")


class MissingCredentialsError(RuntimeError):
    """Raised when `HSCLA_USR` and/or `HSCLA_PWD` are not set."""


@dataclass(frozen=True)
class Credentials:
    """HSCLA account name and password, read from the environment."""

    username: str
    password: str

    def __repr__(self) -> str:
        return f"Credentials(username={self.username!r}, password='***')"


def load_credentials(env: dict[str, str] | None = None) -> Credentials:
    """Return HSCLA credentials, or raise if they aren't both set.

    A custom environment can be passed in for testing. The default reads
    from `os.environ`.
    """

    source = os.environ if env is None else env
    user = source.get(USERNAME_ENV, "").strip()
    pwd = source.get(PASSWORD_ENV, "").strip()
    missing = [name for name, val in ((USERNAME_ENV, user), (PASSWORD_ENV, pwd)) if not val]
    if missing:
        names = " and ".join(missing)
        raise MissingCredentialsError(
            f"{names} not set. HSCLA credentials are read from environment variables "
            f"{USERNAME_ENV} and {PASSWORD_ENV}. On this machine they normally live in "
            f"~/.zshenv; open a new shell or `source ~/.zshenv` to load them."
        )
    return Credentials(username=user, password=pwd)


class MirrorRootMissing(RuntimeError):
    """Raised when the local-mirror root doesn't exist (e.g., the volume isn't mounted)."""


def mirror_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    require_exists: bool = False,
) -> Path:
    """Resolve the directory that holds local Parquet mirrors of HSCLA tables.

    Precedence:
        1. `explicit` argument, if given.
        2. `HSCLA_MIRROR_ROOT` env var, if set.
        3. `/Volumes/galaxy/hsc/la2020/` (default external-volume path).

    Pass `require_exists=True` to raise `MirrorRootMissing` if the path
    is not a directory (typical reason: the external volume isn't
    mounted). When `require_exists=False`, the path is returned even if
    it doesn't exist — callers like `build_mirror` will create it.
    """

    if explicit is not None:
        path = Path(explicit).expanduser()
    elif (env_value := os.environ.get(MIRROR_ROOT_ENV)):
        path = Path(env_value).expanduser()
    else:
        path = DEFAULT_MIRROR_ROOT
    if require_exists and not path.is_dir():
        raise MirrorRootMissing(
            f"Local mirror root {path} is not a directory. "
            f"Mount the volume or set {MIRROR_ROOT_ENV} to override the default."
        )
    return path


def cache_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the folder where downloaded files should land.

    Precedence:
        1. `explicit` argument, if given.
        2. `HSCLA_TOOL_CACHE` env var, if set.
        3. `./outputs/` relative to the current working directory.

    The returned folder is created on first use.
    """

    if explicit is not None:
        path = Path(explicit).expanduser()
    elif (env_value := os.environ.get(CACHE_ENV)):
        path = Path(env_value).expanduser()
    else:
        path = DEFAULT_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
