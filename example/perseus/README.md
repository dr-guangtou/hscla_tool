# Perseus / NGC 1275 end-to-end example

A worked example that walks `hscla_tool` through the HSCLA2020 archive
using the Perseus cluster as the science target. The field center is
**NGC 1275** (the Perseus BCG):

- RA  = 49.9506670° (J2000)
- Dec = +41.5117083° (J2000)
- Search radius = 2.0° (≈ 2.5 Mpc projected at z = 0.0176)

Real downloaded data goes to `/Volumes/galaxy/data/perseus/` (not
in the repo). QA figures and small tables stay in this directory.

## Planned steps

1. **Coverage** — enumerate HSCLA2020 coadd patches and per-band CCD
   frame counts inside the 2° circle. ✅ This is what `step1_coverage.py`
   does today.
2. Metadata collection (per-patch seeing, depth, n_visits, zeropoints).
3. Catalog data curation (forced photometry around the cluster).
4. Imaging data curation (calexp / det_bkgd / coadd+bg pulls into
   `/Volumes/galaxy/data/perseus/`).
5. Basic post-processing (stitching, LSB visualization, sanity plots).

## Step 1 — coverage map

Run from the repo root:

```
uv run python example/perseus/step1_coverage.py
```

The script

- loads the local Parquet mirror of `la2020.mosaic` and
  `la2020.frame` (at `/Volumes/galaxy/hsc/la2020/`),
- calls `coverage.region_coverage(..., size_deg=4.0, source='local')`
  to get the 4°-square bbox match (the circle inscribes in this box),
- post-filters patches by exact great-circle distance ≤ 2°,
- writes a per-patch CSV plus a per-band frame-count CSV,
- plots two QA figures: one combined sky map, one per-band facet,
- writes a markdown summary at `notes/step1_coverage.md`.

### What we found (2026-05-13)

- **421 patches** inside the 2° circle, across **4 broadbands**
  (HSC-G, HSC-R, HSC-I, HSC-Z; no HSC-Y, no narrow bands).
- 4 tracts touched: **15548, 15549, 15733, 15734**.
- Median seeing per band: **G 0.70″, R 0.53″, I 0.64″, Z 0.62″**.
- Frame counts (single-CCD level): **G 10506 / 102 visits, R 2060 /
  20, I 1030 / 10, Z 1339 / 13** — heavily HSC-G dominated.
- The HSCLA2020 footprint at Perseus is much smaller than the 2°
  circle (see `figures/step1_coverage_combined.png`). The deep
  pointing is centered roughly on NGC 1275 and extends ~1° on a side.

See `notes/step1_coverage.md` for the full table.

## Files in this directory

```
README.md                         # this file
step1_coverage.py                 # the step-1 driver
figures/
  step1_coverage_combined.png     # all bands overlaid on the 2° circle
  step1_coverage_per_band.png     # one panel per band
notes/
  step1_coverage.md               # text summary (totals, per-band stats)
  step1_patches.csv               # per-patch table (band, tract, patch,
                                  # corners, seeing, zeropt, sep_center)
  step1_frame_counts.csv          # per-band (n_frames, n_visits)
```
