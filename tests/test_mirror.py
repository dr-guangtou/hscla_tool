"""Tests for `hscla_tool.mirror` and the local coverage path."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from hscla_tool import config, coverage, mirror

# --------------------------------------------------------------------------- #
# Synthetic mirror fixtures
# --------------------------------------------------------------------------- #


def _mosaic_rows() -> list[dict]:
    """Three patches: one squarely on the Perseus fixture, one nearby in a
    different band, one in a totally different sky region.
    Plus a fourth patch that 'wraps' RA=0 (to exercise the wrap guard)."""

    return [
        # Patch covering Perseus LSBG in HSC-G.
        dict(band="HSC-G", tract=9618, patch=23, patch_s="5,4", skymap_id=1001,
             ra2000=49.27, dec2000=41.24,
             llcra=49.20, llcdec=41.18, ulcra=49.20, ulcdec=41.30,
             urcra=49.34, urcdec=41.30, lrcra=49.34, lrcdec=41.18,
             seeing=0.68),
        # Same place but a different band.
        dict(band="HSC-I", tract=9618, patch=23, patch_s="5,4", skymap_id=1002,
             ra2000=49.27, dec2000=41.24,
             llcra=49.20, llcdec=41.18, ulcra=49.20, ulcdec=41.30,
             urcra=49.34, urcdec=41.30, lrcra=49.34, lrcdec=41.18,
             seeing=0.62),
        # Patch in a totally different region.
        dict(band="HSC-G", tract=9000, patch=10, patch_s="2,2", skymap_id=2001,
             ra2000=150.0, dec2000=2.0,
             llcra=149.9, llcdec=1.9, ulcra=149.9, ulcdec=2.1,
             urcra=150.1, urcdec=2.1, lrcra=150.1, lrcdec=1.9,
             seeing=0.71),
        # RA=0 wrap-crossing patch (LL/UL near 359.9, UR/LR near 0.1).
        dict(band="NB0515", tract=9999, patch=99, patch_s="9,9", skymap_id=3001,
             ra2000=0.0, dec2000=-15.0,
             llcra=359.95, llcdec=-15.05, ulcra=359.95, ulcdec=-14.95,
             urcra=0.05, urcdec=-14.95, lrcra=0.05, lrcdec=-15.05,
             seeing=0.85),
    ]


def _frame_rows() -> list[dict]:
    return [
        dict(frame_id="a1", visit=1, ccd=10, ccdname="010", band="HSC-G",
             ra2000=49.27, dec2000=41.24),
        dict(frame_id="a2", visit=1, ccd=11, ccdname="011", band="HSC-G",
             ra2000=49.30, dec2000=41.25),
        dict(frame_id="b1", visit=2, ccd=10, ccdname="010", band="HSC-R",
             ra2000=49.27, dec2000=41.24),
        dict(frame_id="x1", visit=99, ccd=10, ccdname="010", band="HSC-G",
             ra2000=150.0, dec2000=2.0),
    ]


@pytest.fixture
def tmp_mirror_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect mirror_root() to a temp dir for the test, with parquet files written."""

    root = tmp_path / "mirror"
    root.mkdir()
    pd.DataFrame(_mosaic_rows()).to_parquet(root / "mosaic.parquet", index=False)
    pd.DataFrame(_frame_rows()).to_parquet(root / "frame.parquet", index=False)
    monkeypatch.setenv(config.MIRROR_ROOT_ENV, str(root))
    return root


# --------------------------------------------------------------------------- #
# config.mirror_root
# --------------------------------------------------------------------------- #


def test_mirror_root_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.MIRROR_ROOT_ENV, str(tmp_path / "alt"))
    assert config.mirror_root() == tmp_path / "alt"


def test_mirror_root_require_exists_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.MIRROR_ROOT_ENV, str(tmp_path / "nope"))
    with pytest.raises(config.MirrorRootMissing):
        config.mirror_root(require_exists=True)


# --------------------------------------------------------------------------- #
# mirror module
# --------------------------------------------------------------------------- #


def test_mirror_path_and_is_mirrored(tmp_mirror_root: Path) -> None:
    assert mirror.mirror_path("mosaic") == tmp_mirror_root / "mosaic.parquet"
    assert mirror.is_mirrored("mosaic")
    assert not mirror.is_mirrored("mosaicframe")


def test_mirror_rejects_unknown_table(tmp_mirror_root: Path) -> None:
    with pytest.raises(mirror.MirrorError, match="not a supported"):
        mirror.mirror_path("forced")


def test_load_mirror_raises_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.MIRROR_ROOT_ENV, str(tmp_path / "missing"))
    with pytest.raises(mirror.MirrorError, match="no local mirror"):
        mirror.load_mirror("mosaic")


def test_load_mirror_round_trips(tmp_mirror_root: Path) -> None:
    df = mirror.load_mirror("mosaic")
    assert set(["band", "tract", "patch", "ra2000", "dec2000", "llcra"]).issubset(df.columns)
    assert len(df) == 4


# --------------------------------------------------------------------------- #
# coverage with source='local'
# --------------------------------------------------------------------------- #


def test_local_region_coverage_hits_perseus(tmp_mirror_root: Path) -> None:
    result = coverage.region_coverage(49.27, 41.24, size_deg=0.02, source="local")
    assert result.filters == ("HSC-G", "HSC-I")
    assert len(result.patches) == 2
    assert all(p.tract == 9618 for p in result.patches)
    # The HSC-G mean seeing in our fixture is just 0.68.
    assert math.isclose(result.mean_seeing_per_band["HSC-G"], 0.68)


def test_local_region_coverage_uncovered_is_empty(tmp_mirror_root: Path) -> None:
    result = coverage.region_coverage(200.0, 30.0, size_deg=0.02, source="local")
    assert result.covered is False
    assert result.patches == ()


def test_local_region_coverage_ignores_wrap_crossing_patches(tmp_mirror_root: Path) -> None:
    # The fourth synthetic patch wraps RA=0; we should not report it for a
    # query box near RA=180 (it would otherwise match via the naive envelope).
    result = coverage.region_coverage(180.0, -15.0, size_deg=0.5, source="local")
    assert result.covered is False


def test_local_region_coverage_uses_injected_df() -> None:
    df = pd.DataFrame(_mosaic_rows())
    result = coverage.region_coverage(49.27, 41.24, size_deg=0.02,
                                       source="local", mirror_df=df)
    assert result.filters == ("HSC-G", "HSC-I")


def test_local_frame_coverage_aggregates(tmp_mirror_root: Path) -> None:
    result = coverage.frame_coverage(49.27, 41.24, size_deg=0.0, source="local")
    assert "HSC-G" in result.band_summary
    assert result.band_summary["HSC-G"].n_frames == 2
    assert result.band_summary["HSC-G"].n_visits == 1
    assert result.band_summary["HSC-R"].n_frames == 1
    assert result.frames is None


def test_local_frame_coverage_detailed_returns_rows(tmp_mirror_root: Path) -> None:
    result = coverage.frame_coverage(49.27, 41.24, size_deg=0.0,
                                      source="local", detailed=True)
    assert result.frames is not None
    bands = {row["band"] for row in result.frames}
    assert bands == {"HSC-G", "HSC-R"}


def test_coverage_source_validation() -> None:
    with pytest.raises(ValueError, match="source"):
        coverage.region_coverage(0.0, 0.0, source="moon")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source"):
        coverage.frame_coverage(0.0, 0.0, source="moon")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Mixed-type column coercion (regression for the live `frame` build)
# --------------------------------------------------------------------------- #


def test_coerce_object_columns_to_string_handles_mixed_types(tmp_path: Path) -> None:
    # Pandas reads HSCLA `frame.object` as plain object-dtype with both
    # ints and strings; pyarrow refuses to write that. The coercion
    # helper should turn the column into pandas' nullable StringDtype.
    df = pd.DataFrame({
        "frame_id": ["a", "b", "c"],
        "object": ["target_one", 42, None],  # int and string mixed; with a real null
        "exptime": [120.0, 240.0, 60.0],
    })
    out = mirror._coerce_object_columns_to_string(df)
    # pandas 2.x calls the nullable dtype "string", pandas 3.x calls it "str";
    # the point is that it is no longer plain object.
    assert str(out["frame_id"].dtype) in {"string", "str"}
    assert str(out["object"].dtype) in {"string", "str"}
    # Numeric columns are left alone.
    assert out["exptime"].dtype.kind == "f"
    # And the result actually writes to Parquet.
    path = tmp_path / "round_trip.parquet"
    out.to_parquet(path, index=False, compression="zstd")
    back = pd.read_parquet(path)
    assert list(back["object"][:2]) == ["target_one", "42"]
    assert back["object"].iloc[2] is None or pd.isna(back["object"].iloc[2])
