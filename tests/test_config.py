"""Tests for `hscla_tool.config`. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

from hscla_tool import config


def test_load_credentials_happy_path() -> None:
    creds = config.load_credentials({"HSCLA_USR": "alice", "HSCLA_PWD": "s3cret"})
    assert creds.username == "alice"
    assert creds.password == "s3cret"


def test_credentials_repr_hides_password() -> None:
    creds = config.Credentials(username="alice", password="s3cret")
    text = repr(creds)
    assert "alice" in text
    assert "s3cret" not in text


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"HSCLA_USR": "alice"},
        {"HSCLA_PWD": "s3cret"},
        {"HSCLA_USR": "", "HSCLA_PWD": "s3cret"},
        {"HSCLA_USR": "alice", "HSCLA_PWD": "   "},
    ],
)
def test_load_credentials_raises_when_missing(env: dict[str, str]) -> None:
    with pytest.raises(config.MissingCredentialsError) as info:
        config.load_credentials(env)
    msg = str(info.value)
    assert "HSCLA_USR" in msg or "HSCLA_PWD" in msg


def test_cache_dir_explicit_argument_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HSCLA_TOOL_CACHE", str(tmp_path / "from_env"))
    target = tmp_path / "from_arg"
    result = config.cache_dir(target)
    assert result == target.resolve()
    assert result.is_dir()


def test_cache_dir_uses_env_var_when_no_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "from_env"
    monkeypatch.setenv("HSCLA_TOOL_CACHE", str(target))
    result = config.cache_dir()
    assert result == target.resolve()
    assert result.is_dir()


def test_cache_dir_falls_back_to_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HSCLA_TOOL_CACHE", raising=False)
    monkeypatch.chdir(tmp_path)
    result = config.cache_dir()
    assert result == (tmp_path / "outputs").resolve()
    assert result.is_dir()
