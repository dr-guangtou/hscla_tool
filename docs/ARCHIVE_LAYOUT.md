# HSCLA2020 direct file archive — directory layout

The HSCLA2020 file tree at

> **`https://hscla.mtk.nao.ac.jp/archive/files/la2020/`**

publishes the raw hscPipe outputs as a plain Apache autoindex over
HTTP Basic auth. You can download per-patch coadd images, per-patch
catalog FITS, per-visit warps, single-CCD calibrated frames, and
single-CCD source catalogs **without going through the cutout / PSF
services**. This is the right entry point when you want whole patches
of `calexp` / `forced_src` / `meas` rather than small region cutouts.

This document is the **observed** structure of the tree, mapped by
live exploration on 2026-05-13. The upstream documentation for this
file layout is minimal, so this file is the working reference for
ourselves and future agents. If you find a path or file kind that
isn't documented here, **append it to this file** rather than holding
the knowledge in your head.

> **!!! Hard rule — 1 TB session limit !!!**
>
> If a single download session would pull **more than 1 TB**, you
> **must** contact the NAOJ database team at
> `hscla-contact@ml.nao.ac.jp` **before** starting. This is an
> archive-wide policy, not a per-machine one. The `meas` and `calexp`
> files alone are ~150–180 MB each, so a few thousand patches across
> all 17+ filters can pass 1 TB quickly — plan and budget upfront.

Auth and resumability:

- **HTTP Basic auth** with the same `HSCLA_USR` / `HSCLA_PWD`
  credentials used by the cutout / PSF services. (The SQL service
  uses session-cookie auth instead — see `CLAUDE.md` for the
  inconsistencies cheat-sheet.)
- The server advertises `Accept-Ranges: bytes`, so partial downloads
  can resume with a `Range: bytes=<offset>-` header. The
  `HscLaArchiveClient` in `hscla_tool.archive` does this automatically
  for per-patch downloads.
- Patch names contain a comma: `1,6`. The path must be URL-encoded as
  `1%2C6`; our archive client handles this.

---

## Top-level tree

```
/archive/files/la2020/
├── deepCoadd/             # per-visit warps that feed the coadd
├── deepCoadd-results/     # per-patch coadd outputs (calexp, forced_src, meas, ...)
├── jointcal-results/      # per-visit per-CCD photometric calibration
└── <visit>/               # 451 numeric dirs (single-exposure outputs)
    ├── 00815/             # 5-digit IDs from 00815 ... 03278
    ├── 00816/
    ├── ...
    └── 03278/
```

The 451 numeric directories at the top level look like **observation
"runs"** or **pointing groupings**, not raw visit IDs. The visit ID
that appears *inside* the file names is a separate 7-digit zero-padded
number (e.g. `0003540`); the parent dir name (`00912`) does not appear
inside its own filenames. We have not confirmed the upstream meaning
of the parent number — record it if you find it.

---

## Filter inventory

Filters observed under `deepCoadd-results/` (the canonical inventory):

| Filter type      | Codes                                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------------------------ |
| Broadband (HSC)  | `HSC-G`, `HSC-R`, `HSC-I`, `HSC-Y`, `HSC-Z`                                                            |
| Intermediate band| `IB0945`                                                                                               |
| Narrowband (NB)  | `NB0387`, `NB0400`, `NB0468`, `NB0515`, `NB0527`, `NB0656`, `NB0718`, `NB0816`, `NB0921`, `NB0926`, `NB0973` |
| Cross-band index | `merged/` (no filter; cross-band detection / reference; see below)                                     |

`jointcal-results/` carries the same broadband filters **plus** two
extra codes: **`HSC-I2`** and **`HSC-R2`**. Best guess: re-processed
runs with the second-pass calibration. We have not confirmed which of
`HSC-I` vs `HSC-I2` is the canonical version on this archive — flag
this when you cite calibration files.

---

## `deepCoadd-results/<filter>/<tract>/<patch>/` — per-patch coadd products

This is the most useful subtree for analysis. Each leaf directory
holds **9 FITS files** for one (filter, tract, patch) combination:

| File                              | Approx. size | Content (observed)                                                                                       |
| --------------------------------- | ------------ | -------------------------------------------------------------------------------------------------------- |
| `calexp-<F>-<T>-<P>.fits`         | ~145 MB      | Coadd image (multi-extension FITS: image + mask + variance + metadata). Same product as the DAS cutout `coadd`. |
| `forced_src-<F>-<T>-<P>.fits`     | ~35 MB       | Forced photometry source catalog (binary table). Position fixed by the reference band; flux measured here. |
| `meas-<F>-<T>-<P>.fits`           | ~180 MB      | Unforced ("meas") source catalog (binary table). Largest single file in a patch.                          |
| `deblendedFlux-<F>-<T>-<P>.fits`  | ~140 MB      | Per-source deblended flux table.                                                                          |
| `det-<F>-<T>-<P>.fits`            | ~6 MB        | Single-band detection catalog. HDU[1] is a BinTable with `id`, `coord_ra`, `coord_dec`, `parent`, `footprint` (typical ~7000 rows per patch). |
| `det_bkgd-<F>-<T>-<P>.fits`       | ~50 KB       | Detection-step background model (6-HDU file: 33×33 background field + per-mask-plane summary tables; MP_* mask plane definitions live here too). |
| `ran-<F>-<T>-<P>.fits`            | ~10 MB       | Uniform random points within the patch footprint; useful for clustering / completeness work.              |
| `srcMatch-<F>-<T>-<P>.fits`       | ~25 KB       | Small match table between detected sources and an external reference catalog. HDU[1] is BinTable with `first`, `second`, `distance` (~800 rows). |
| `srcMatchFull-<F>-<T>-<P>.fits`   | ~2 MB        | Full match table (more columns / more rows than `srcMatch`).                                              |

URL pattern:

```
https://hscla.mtk.nao.ac.jp/archive/files/la2020/deepCoadd-results/
    <FILTER>/<TRACT>/<PATCH_URL_ENCODED>/<KIND>-<FILTER>-<TRACT>-<PATCH>.fits
```

Where `<PATCH_URL_ENCODED>` is `"1%2C6"` for patch `1,6`. Example:
`/archive/files/la2020/deepCoadd-results/HSC-I/5921/4%2C6/calexp-HSC-I-5921-4,6.fits`.

The `hscla_tool.archive` module (`download_patch_file`,
`download_coadd_image`, `download_forced_catalog`) handles this URL
construction, the URL-encoding, the HTTP Basic auth, and resumable
downloads. Its `SUPPORTED_KINDS` tuple matches the 9 file kinds above.

**Sample exists on disk after one cutout call**: `calexp` for any
patch you've already pulled via the DAS cutout service is the
*same product* the file tree serves, but the cutout service crops to
your requested box, while this tree gives you the **whole patch** —
typically ~12 arcmin square.

### FITS internal notes (partial — see "Known gaps" below)

- All these files have a near-empty `PrimaryHDU` (`BITPIX=16, NAXIS=0`,
  ~5–24 header keys). The real data is always in extensions. Don't
  judge a file by `hdul[0]`.
- Binary-table extensions (`det`, `forced_src`, `meas`,
  `deblendedFlux`, `srcMatch`, `srcMatchFull`) use the AFW-table
  hscPipe convention. Column names follow the
  `<measurement>_<component>` pattern (e.g. `coord_ra`, `coord_dec`,
  `parent`, `footprint`, `id`, `flags`, ...). Per-table column
  inventories are not yet documented here.
- The `det_bkgd` mask-plane HDU carries the same `MP_*` cards we see
  in DAS cutouts (e.g. `MP_BAD`, `MP_SAT`, `MP_INTRP`, `MP_CR`,
  `MP_EDGE`, `MP_DETECTED`, `MP_DETECTED_NEGATIVE`, `MP_SUSPECT`,
  `MP_NO_DATA`, `MP_BRIGHT_OBJECT`, `MP_CROSSTALK`, `MP_NOT_DEBLENDED`,
  `MP_UNMASKEDNAN`, `MP_CLIPPED`, `MP_REJECTED`, `MP_SENSOR_EDGE`,
  `MP_INEXACT_PSF`). The `hscla_tool.mask.parse_mask_planes` /
  `decode` helpers work against these too.

---

## `deepCoadd-results/merged/<tract>/<patch>/` — cross-band products

Two files per `(tract, patch)`, with **no filter in the name** because
they are filter-agnostic:

| File                          | Approx. size | Content                                                                                                  |
| ----------------------------- | ------------ | -------------------------------------------------------------------------------------------------------- |
| `mergeDet-<T>-<P>.fits`       | ~400 KB      | Cross-band merged detection catalog (HDU[1] BinTable with `flags`, `id`, `coord_ra`, `coord_dec`, `parent`, `footprint` columns). Used as the position reference for forced photometry across bands. |
| `ref-<T>-<P>.fits`            | ~1 MB        | Reference catalog used for forced photometry — same positions as `mergeDet` but with additional reference columns / shape info. |

URL example:
`/archive/files/la2020/deepCoadd-results/merged/2970/0%2C7/mergeDet-2970-0,7.fits`.

The `mergeDet` / `ref` pair is what makes the per-band `forced_src`
catalogs share an `object_id` namespace across all bands.

---

## `deepCoadd/<filter>/<tract>/<patch>/` — per-visit warps

For every patch under each filter, this tree carries the
**per-visit warped frames** that hscPipe stacked to make the coadd:

| File pattern                                                    | Sizes seen        | Content                                                                  |
| --------------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------ |
| `psfMatchedWarp-<F>-<T>-<P>-<VISIT>.fits`                       | 3 – 90 MB         | Single-visit image warped onto the patch grid **and PSF-homogenized**. One file per contributing visit. |
| `warp-<F>-<T>-<P>-<VISIT>.fits`                                 | 3 – 108 MB        | Single-visit warp **without** the PSF homogenization. One file per contributing visit. |

A single patch can carry **dozens of visits** in each flavor. For
`HSC-I/5921/4,6` we observed visits `91750`–`91776` plus `230456`,
`230458`, `230466`, `230468` — i.e. multiple observing campaigns
contribute to the same coadd. `EXPTIME` per file ranges 30 / 250 / 300
seconds depending on the campaign.

The total bytes per patch under `deepCoadd/` can easily exceed
**3–5 GB** when you sum across visits. Plan budgets accordingly; this
is where the 1 TB rule bites you fastest.

URL example:
`/archive/files/la2020/deepCoadd/HSC-I/5921/4%2C6/psfMatchedWarp-HSC-I-5921-4,6-91770.fits`.

> `hscla_tool.archive` does not currently expose helpers for these
> per-visit warp files. Use the raw `HscLaArchiveClient.download_*`
> primitives or extend the module if you need them.

---

## `jointcal-results/<filter>/<tract>/` — per-visit per-CCD photo calibration

Per-tract directories with **per-visit per-CCD** photometric
calibration files produced by `jointcal`:

| File pattern                                          | Approx. size | Content                                                                |
| ----------------------------------------------------- | ------------ | ---------------------------------------------------------------------- |
| `jointcal_photoCalib-<VISIT>-<CCD>.fits`              | small        | One photometric calibration solution per (visit, CCD) for the tract.   |

We observed 1,694 files in `jointcal-results/HSC-I/4001/` — i.e.
roughly one per (contributing visit × CCD) for that tract. Not all
visits use all 112 HSC science CCDs, and CCD 009 is the well-known
dead chip in HSC.

URL example:
`/archive/files/la2020/jointcal-results/HSC-I/4001/jointcal_photoCalib-0043714-000.fits`.

There is also a sibling tree under `jointcal-results/HSC-I2/` (and
`HSC-R2/`) whose role we have not yet confirmed; see "Filter
inventory" above.

---

## `/<visit_dir>/<filter>/` — single-exposure outputs

Each top-level numeric directory holds at least one filter
subdirectory; the filter subdirectory has exactly two child folders:

```
/<visit_dir>/<filter>/
├── corr/    # per-CCD calibrated frames + backgrounds
└── output/  # per-CCD source catalogs and src-match products
```

### `corr/` files

| File pattern                                | Content                                                  |
| ------------------------------------------- | -------------------------------------------------------- |
| `BKGD-<VISIT>-<CCD>.fits`                   | 10-HDU per-CCD background model (image-plane background, similar shape to `det_bkgd` but bigger, with mask-plane definitions). Observed in `/00912/HSC-R/corr/`. CCD numbering goes 000, 001, ... skipping 009 (the dead chip), up to ~110. |

The `corr/` directory may carry **other** per-CCD calibrated products
(`CORR-`, `FLAT-`, ...) for some visit-dirs; the live listing we did
for `/00912/HSC-R/corr/` showed only `BKGD-*` files in the first
window we sampled. Worth re-probing if you need a specific product.

### `output/` files

For `/00912/HSC-R/output/` (15,244 files total) we observed **four
filename prefixes**, each contributing 3,811 files (= one per
(visit, CCD) tuple):

| File pattern                                | Content                                                                |
| ------------------------------------------- | ---------------------------------------------------------------------- |
| `ICSRC-<VISIT>-<CCD>.fits`                  | Single-CCD intermediate / initial source catalog (pre-PSF model).      |
| `SRC-<VISIT>-<CCD>.fits`                    | Single-CCD final source catalog.                                       |
| `SRCMATCH-<VISIT>-<CCD>.fits`               | Small src-match table for that (visit, CCD).                           |
| `SRCMATCHFULL-<VISIT>-<CCD>.fits`           | Full src-match table for that (visit, CCD).                            |

A single top-level visit-dir like `/00912/` therefore aggregates
catalog and background data for **thousands of distinct (visit, CCD)
tuples** in one filter; the parent dir number does *not* match the
visit IDs inside, so treat the top-level name as an opaque grouping
key.

URL example:
`/archive/files/la2020/00912/HSC-R/output/SRC-0003540-001.fits`.

---

## URL encoding and patch / CCD numbering quirks

- **Patch names use a comma**: `0,7`, `1,6`, `4,4`, ... When building
  URLs, encode the comma as `%2C`. The autoindex listings on the
  server use the encoded form in `<a href>`, so a quick way to extract
  patch names is to URL-decode each `href`.
- **Tracts are integers** (e.g. `5921`, `4001`).
- **Filters are case-sensitive**: `HSC-I` works, `hsc-i` and `HSC-i`
  do not. Narrowband filters use a 4-digit `NB####` token where
  `####` is the central wavelength in nm.
- **CCD numbers** are 3-digit zero-padded (`000`–`111` for HSC science
  chips). CCD `009` is the well-known failed CCD and is absent from
  every visit / filter we have looked at.
- **Visit IDs** appear as 7-digit zero-padded numbers inside file
  names (e.g. `0003540`, `0043714`, `0091770`, `0230456`). The
  5-digit number on the top-level directory is a **separate** grouping
  ID (campaign / pointing run? — to be confirmed).

---

## How the toolkit currently uses this archive

`hscla_tool.archive` (`HscLaArchiveClient`) supports the
`deepCoadd-results/<filter>/<tract>/<patch>/` subtree and its 9 file
kinds: `calexp`, `forced_src`, `meas`, `deblendedFlux`, `det`,
`det_bkgd`, `ran`, `srcMatch`, `srcMatchFull`. Module-level shortcuts
`download_coadd_image` and `download_forced_catalog` are convenience
wrappers. Downloads are content-addressed under
`${HSCLA_TOOL_CACHE}/archive/<band>/<tract>/<patch>/`,
mirroring the upstream layout, with `Range`-based resume on
interruption.

Nothing in the toolkit currently knows about:

- `deepCoadd-results/merged/` (`mergeDet` / `ref`)
- `deepCoadd/<filter>/<tract>/<patch>/` (per-visit warps)
- `jointcal-results/<filter>/<tract>/` (per-visit per-CCD photo cal)
- `/<visit_dir>/<filter>/{corr,output}/` (single-exposure outputs)

These are the obvious next layers if a science workflow needs them.
File a follow-up branch when you add support — the layout above is
the canonical reference for what each file means.

---

## Known gaps (TODOs for later passes)

Update this section as you learn more.

- [ ] Confirm what the top-level numeric directory ID (e.g. `00912/`)
  actually means upstream. Visit ID? Pointing? Campaign? Date?
- [ ] Decide whether `HSC-I2` / `HSC-R2` under `jointcal-results/` are
  canonical for science use, or just bookkeeping for re-processed
  campaigns.
- [ ] Per-binary-table column inventories for `forced_src`, `meas`,
  `deblendedFlux`, `det`, `srcMatchFull`, `mergeDet`, `ref`. The
  AFW-table column names follow the
  `<measurement>_<component>[_<suffix>]` convention but the full
  schema needs a real `fits.open` pass on a sample of each. (Tip:
  `astropy.io.fits.getdata(path, hdu=1).dtype.names`.)
- [ ] FITS-header KEYWORDS worth standardizing on per file kind
  (FILTER, TRACT, PATCH, MAGZERO / FLUXMAG0, EXPTIME, MJD-OBS,
  TELESCOP, INSTRUME). The PrimaryHDU is mostly empty; useful
  metadata lives in the extension headers and we haven't surveyed
  them yet.
- [ ] Whether `corr/` carries non-`BKGD` files for any visit-dir
  (suspected: `CORR-*` calibrated images, possibly `MASK-*` etc.).
- [ ] Whether all 451 top-level numeric dirs follow the
  `<filter>/{corr,output}/` shape, or only the ones we sampled.
