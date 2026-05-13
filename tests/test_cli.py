"""Offline tests for the ``hscla`` console script.

We never touch the network here — each subcommand stubs out the module
function it would normally call. The goal is to pin the CLI surface
(argparse plumbing, default output paths, exit codes, quiet/verbose
behavior) rather than re-test the underlying modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from hscla_tool import archive as _archive
from hscla_tool import cli
from hscla_tool import coverage as _coverage
from hscla_tool import cutout as _cutout
from hscla_tool import mirror as _mirror
from hscla_tool import psf as _psf

# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect every CLI default into a fresh tmp dir."""

    monkeypatch.setenv("HSCLA_TOOL_CACHE", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# Parser plumbing
# --------------------------------------------------------------------------- #


def test_parser_has_eight_subcommands() -> None:
    parser = cli._build_parser()
    # argparse exposes choices via the first subparsers action.
    sub_action = next(
        a for a in parser._actions if isinstance(a, type(parser._actions[-1])) and a.choices
    )
    assert set(sub_action.choices) == {
        "coverage", "frames", "cutout", "cutouts", "psf", "sql",
        "crossmatch", "mirror", "archive",
    }


def test_top_level_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "hscla coverage" in out or "coverage" in out


def test_missing_subcommand_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code != 0


# --------------------------------------------------------------------------- #
# coverage / frames
# --------------------------------------------------------------------------- #


def _fake_region_coverage(covered: bool) -> _coverage.RegionCoverage:
    if not covered:
        return _coverage.RegionCoverage(
            filters=(), patches=(), mean_seeing_per_band={},
        )
    patches = (
        _coverage.PatchInfo(band="HSC-I", tract=15548, patch=10006,
                            patch_s="1,6", skymap_id=1, ra2000=49.2,
                            dec2000=41.2, seeing=0.567),
        _coverage.PatchInfo(band="HSC-R", tract=15548, patch=10006,
                            patch_s="1,6", skymap_id=1, ra2000=49.2,
                            dec2000=41.2, seeing=float("nan")),
    )
    return _coverage.RegionCoverage(
        filters=("HSC-I", "HSC-R"),
        patches=patches,
        mean_seeing_per_band={"HSC-I": 0.567},
    )


def test_coverage_covered_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake(ra: float, dec: float, **_: Any) -> _coverage.RegionCoverage:
        assert ra == 49.0 and dec == 41.0
        return _fake_region_coverage(covered=True)

    monkeypatch.setattr(_coverage, "region_coverage", fake)
    rc = cli.main(["coverage", "49.0", "41.0", "--size-deg", "0.03"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "HSC-I, HSC-R" in out
    assert "1,6" in out
    assert "mean seeing" in out


def test_coverage_uncovered_prints_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        _coverage, "region_coverage",
        lambda *a, **kw: _fake_region_coverage(covered=False),
    )
    rc = cli.main(["coverage", "0", "0"])
    assert rc == 0
    assert "no HSCLA coadd coverage" in capsys.readouterr().out


def test_frames_prints_band_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _coverage.FrameCoverage(
        filters=("HSC-G", "HSC-I"),
        band_summary={
            "HSC-G": _coverage.BandFrameSummary("HSC-G", n_frames=10, n_visits=2),
            "HSC-I": _coverage.BandFrameSummary("HSC-I", n_frames=7, n_visits=1),
        },
        frames=None,
    )
    monkeypatch.setattr(_coverage, "frame_coverage", lambda *a, **kw: fake)
    rc = cli.main(["frames", "1.0", "2.0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "HSC-G" in out and "HSC-I" in out
    assert "10" in out and "7" in out


# --------------------------------------------------------------------------- #
# cutout
# --------------------------------------------------------------------------- #


class _FakeHDUList(list):
    closed = False

    def close(self) -> None:
        self.closed = True


def _make_fake_cutout(tmp_path: Path, band: str = "HSC-I") -> _cutout.Cutout:
    fits_path = tmp_path / "fakecache" / f"{band}.fits"
    fits_path.parent.mkdir(parents=True, exist_ok=True)
    fits_path.write_bytes(b"SIMPLE FAKE FITS")
    # Build a minimal Cutout; image/mask/variance can be sentinel non-None.
    return _cutout.Cutout(
        band=band, ra=49.0, dec=41.0, size_arcsec=108.0, kind="coadd",
        fits_path=fits_path,
        hdul=_FakeHDUList([object(), object(), object()]),  # type: ignore[arg-type]
        image=object(),  # type: ignore[arg-type]
        mask_hdu=object(),  # type: ignore[arg-type]
        variance=object(),  # type: ignore[arg-type]
    )


def test_cutout_default_out_path_is_auto_named(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_cutout(tmp_path)
    monkeypatch.setattr(_cutout, "fetch_cutout", lambda *a, **kw: fake)
    rc = cli.main(["cutout", "49.0", "41.0", "--size-arcsec", "108", "--band", "HSC-I"])
    assert rc == 0
    out_path = Path(capsys.readouterr().out.strip())
    assert out_path.parent == tmp_path / "cutouts"
    assert "ra49.0000" in out_path.name
    assert "dec+41.0000" in out_path.name
    assert "HSC-I" in out_path.name
    assert out_path.is_file()


def test_cutout_no_coverage_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*a: Any, **kw: Any) -> None:
        raise _cutout.NoCoverageError("nothing here")

    monkeypatch.setattr(_cutout, "fetch_cutout", boom)
    rc = cli.main(["cutout", "0", "0"])
    assert rc == cli.EXIT_NO_COVERAGE
    err = capsys.readouterr().err
    assert "no coverage" in err


def test_cutouts_batch_writes_named_files_and_logs_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Build a 2-row CSV input and a fake BatchResult with one success + one
    # no-coverage row to confirm the CLI prints the saved path on stdout
    # and the failure row on stderr.
    src = tmp_path / "inputs.csv"
    pd.DataFrame({
        "ra":          [49.27, 198.0],
        "dec":         [41.25, 29.5],
        "size_arcsec": [108.0, 108.0],
        "band":        ["HSC-I", "HSC-I"],
    }).to_csv(src, index=False)

    good = _make_fake_cutout(tmp_path, band="HSC-I")
    bad_exc = _cutout.NoCoverageError("HSCLA has no HSC-I coadd coverage at (198, 29)")
    fake_result = _cutout.BatchResult(
        cutouts=(good, None),
        failures=((1, bad_exc),),
    )
    monkeypatch.setattr(_cutout, "fetch_cutouts", lambda *a, **kw: fake_result)

    rc = cli.main(["cutouts", str(src)])
    assert rc == 0
    captured = capsys.readouterr()
    # Stdout: one path per saved file. Only the covered row produces one.
    stdout_lines = [ln for ln in captured.out.strip().splitlines() if ln]
    assert len(stdout_lines) == 1
    saved = Path(stdout_lines[0])
    assert saved.is_file()
    assert saved.parent == tmp_path / "cutouts"
    assert "ra49.0000" in saved.name and "dec+41.0000" in saved.name
    assert "HSC-I" in saved.name
    # Stderr: friendly summary + per-row failure note.
    assert "saved 1/2 cutouts" in captured.err
    assert "row 1: HSCLA has no" in captured.err


def test_cutouts_batch_missing_input_returns_bad_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["cutouts", "/nonexistent.csv"])
    assert rc == cli.EXIT_BAD_ARGS
    assert "not found" in capsys.readouterr().err


def test_cutout_explicit_out_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _make_fake_cutout(tmp_path)
    monkeypatch.setattr(_cutout, "fetch_cutout", lambda *a, **kw: fake)
    dest = tmp_path / "custom" / "cut.fits"
    rc = cli.main(["cutout", "49.0", "41.0", "--out", str(dest)])
    assert rc == 0
    out_line = capsys.readouterr().out.strip()
    assert Path(out_line) == dest
    assert dest.is_file()


# --------------------------------------------------------------------------- #
# psf
# --------------------------------------------------------------------------- #


def _make_fake_psf(tmp_path: Path, band: str = "HSC-I") -> _psf.Psf:
    import numpy as np
    fits_path = tmp_path / "fakepsfcache" / f"{band}.fits"
    fits_path.parent.mkdir(parents=True, exist_ok=True)
    fits_path.write_bytes(b"SIMPLE FAKE PSF FITS")

    class _Hdu:
        data = np.ones((41, 41), dtype="float64") / (41 * 41)
        header: dict[str, Any] = {}

    return _psf.Psf(
        band=band, ra=49.0, dec=41.0, kind="coadd",
        fits_path=fits_path,
        hdul=_FakeHDUList([_Hdu()]),  # type: ignore[arg-type]
        psf_hdu=_Hdu(),  # type: ignore[arg-type]
    )


def test_psf_default_out_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    fake = _make_fake_psf(tmp_path)
    monkeypatch.setattr(_psf, "fetch_psf", lambda *a, **kw: fake)
    rc = cli.main(["psf", "49.0", "41.0", "--band", "HSC-I"])
    assert rc == 0
    out_path = Path(capsys.readouterr().out.strip())
    assert out_path.parent == tmp_path / "psfs"
    assert out_path.is_file()


# --------------------------------------------------------------------------- #
# sql
# --------------------------------------------------------------------------- #


def test_sql_run_writes_csv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    from hscla_tool import sql as _sqlmod

    monkeypatch.setattr(_sqlmod, "run_sql", lambda sql, **kw: df)
    rc = cli.main(["sql", "SELECT 1"])
    assert rc == 0
    out_path = Path(capsys.readouterr().out.strip())
    assert out_path.parent == tmp_path / "sql"
    assert out_path.is_file()
    written = pd.read_csv(out_path)
    assert list(written.columns) == ["id", "name"]
    assert len(written) == 2


def test_sql_preview_prints_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hscla_tool import sql as _sqlmod

    monkeypatch.setattr(
        _sqlmod, "preview_sql",
        lambda sql, **kw: {"fields": ["a", "b"], "rows": [[1, "x"], [2, "y"]]},
    )
    rc = cli.main(["sql", "SELECT a,b FROM t", "--preview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a\tb" in out
    assert "1\tx" in out


def test_sql_empty_query_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["sql", "  "])
    assert rc == cli.EXIT_BAD_ARGS
    assert "empty query" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# crossmatch
# --------------------------------------------------------------------------- #


def test_crossmatch_emits_warning_banner_and_writes_csv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "input.csv"
    pd.DataFrame({"ra": [49.0], "dec": [41.0]}).to_csv(src, index=False)

    matched = pd.DataFrame({
        "match_input_id": ["row_0"], "match_ra": [49.0], "match_dec": [41.0],
        "object_id": [1], "match_distance": [0.1],
    })
    from hscla_tool import crossmatch as _xm

    monkeypatch.setattr(_xm, "match", lambda *a, **kw: matched)
    rc = cli.main(["crossmatch", str(src), "--radius-arcsec", "1.0"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    out_path = Path(captured.out.strip())
    assert out_path.parent == tmp_path / "crossmatch"
    assert out_path.is_file()


def test_crossmatch_missing_input_returns_bad_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["crossmatch", "/nonexistent.csv"])
    assert rc == cli.EXIT_BAD_ARGS
    assert "not found" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# mirror
# --------------------------------------------------------------------------- #


def test_mirror_status_lists_supported_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "hscla_tool.cli._config.mirror_root", lambda *a, **kw: tmp_path
    )
    rc = cli.main(["mirror", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    for table in _mirror.SUPPORTED_TABLES:
        assert table in out


def test_mirror_build_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: dict[str, Any] = {}

    def fake(table: str, **kw: Any) -> Path:
        called["table"] = table
        return tmp_path / f"{table}.parquet"

    monkeypatch.setattr(_mirror, "build_mirror", fake)
    rc = cli.main(["mirror", "build", "mosaic"])
    assert rc == 0
    assert called["table"] == "mosaic"
    out = capsys.readouterr().out.strip()
    assert out.endswith("mosaic.parquet")


def test_mirror_missing_volume_returns_exit_4(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*a: Any, **kw: Any) -> None:
        raise _mirror.MirrorError("not built")

    monkeypatch.setattr(_mirror, "build_mirror", boom)
    rc = cli.main(["mirror", "build", "mosaic"])
    assert rc == cli.EXIT_MIRROR_MISSING
    assert "not built" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #


def test_archive_download_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dest = tmp_path / "calexp.fits"
    dest.write_bytes(b"x" * 1024)

    def fake(tract: int, patch: str, band: str, kind: str, **kw: Any) -> _archive.ArchiveFile:
        return _archive.ArchiveFile(
            url="http://x", path=dest, kind=kind, band=band,
            tract=tract, patch=patch, bytes=dest.stat().st_size,
        )

    monkeypatch.setattr(_archive, "download_patch_file", fake)
    rc = cli.main(["archive", "download", "15548", "1,6", "HSC-I"])
    assert rc == 0
    assert Path(capsys.readouterr().out.strip()) == dest


def test_archive_list_prints_filenames(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _StubClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def list_patch_files(self, tract: int, patch: str, band: str) -> tuple[str, ...]:
            return ("calexp-HSC-I-15548-1,6.fits", "forced_src-HSC-I-15548-1,6.fits")

    monkeypatch.setattr(_archive, "HscLaArchiveClient", _StubClient)
    rc = cli.main(["archive", "list", "15548", "1,6", "HSC-I"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "calexp-HSC-I-15548-1,6.fits",
        "forced_src-HSC-I-15548-1,6.fits",
    ]


def test_archive_download_failure_returns_exit_5(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*a: Any, **kw: Any) -> None:
        raise _archive.ArchiveError("server said no")

    monkeypatch.setattr(_archive, "download_patch_file", boom)
    rc = cli.main(["archive", "download", "1", "0,0", "HSC-I"])
    assert rc == cli.EXIT_FETCH_FAILURE
    assert "server said no" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Quiet flag
# --------------------------------------------------------------------------- #


def test_quiet_suppresses_progress(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        _coverage, "region_coverage",
        lambda *a, **kw: _fake_region_coverage(covered=False),
    )
    rc = cli.main(["--quiet", "coverage", "1", "2"])
    assert rc == 0
    captured = capsys.readouterr()
    # No "querying coadd coverage..." progress line on stderr.
    assert "querying coadd coverage" not in captured.err
