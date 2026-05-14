# Perseus / NGC 1275 — Step 1: HSCLA2020 coverage

## Search

- Center: NGC 1275 (Perseus cluster BCG)
  - RA  = 49.9506670° (J2000)
  - Dec = 41.5117083° (J2000)
- Radius: 2° (great-circle)
- Source: local Parquet mirror of `la2020.mosaic` + `la2020.frame`
- Method: 4°-square bbox via `coverage.region_coverage(source='local')`,
  post-filtered to patches whose center or any corner falls within
  2° great-circle distance of the target.

## Totals

- Patches inside the circle: **421**
- Bands present: **4** — HSC-G, HSC-I, HSC-R, HSC-Z
- Tracts touched (4): 15548, 15549, 15733, 15734

## Coadd patches per band

| Band | n_patches | n_tracts | seeing median (″) | min (″) | max (″) |
|------|-----------|----------|-------------------|---------|---------|
| HSC-G | 105 | 4 | 0.699 | 0.670 | 0.734 |
| HSC-I | 105 | 4 | 0.644 | 0.618 | 0.700 |
| HSC-R | 105 | 4 | 0.533 | 0.499 | 0.668 |
| HSC-Z | 106 | 4 | 0.619 | 0.538 | 0.780 |

## Single-CCD frames per band (la2020.frame, 4°-box proximity)

| Band | n_frames | n_visits |
|------|----------|----------|
| HSC-G | 10506 | 102 |
| HSC-I | 1030 | 10 |
| HSC-R | 2060 | 20 |
| HSC-Z | 1339 | 13 |

## Files

- Patch catalog (CSV): [`notes/step1_patches.csv`](../notes/step1_patches.csv)
- Frame counts (CSV): [`notes/step1_frame_counts.csv`](../notes/step1_frame_counts.csv)
- Combined QA figure: [`figures/step1_coverage_combined.png`](../figures/step1_coverage_combined.png)
- Per-band QA figure: [`figures/step1_coverage_per_band.png`](../figures/step1_coverage_per_band.png)

## Notes

- HSC patches are roughly 12′ on a side; a 2° radius therefore inscribes
  on the order of ~100 patches per broadband if coverage is contiguous.
- The frame proximity test in `la2020.frame` uses a CCD-center margin of
  0.20° + half-box (`coverage.FRAME_HALF_DEG`), so a few rows outside
  the strict circle may sneak into the frame counts. The patch table,
  by contrast, is post-filtered by exact angular distance.
- For Step 2 (metadata collection) the patch CSV is the primary input —
  use `band`, `tract`, `patch_s` as the (band, tract, patch) key.
