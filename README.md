# hscla_tool

Tools to handle the data query, fetch, and process using the HSC Legacy Archive

- HSC Legacy Archive: https://hscla.mtk.nao.ac.jp/doc/home/
- Updated document for HSCLA_2020: https://hscla.mtk.nao.ac.jp/doc/
    - HSCLA2020 includes data taken up through 2020 and is the latest release.
    - The data reduction pipeline is `hscPipe v.8`, the same with HSC PDR3.
    - Information about the HSCLA2020 data: https://hscla.mtk.nao.ac.jp/doc/available-data-hscla2020/

## Data Access (Interactive tool)

- The general data access page: https://hscla.mtk.nao.ac.jp/doc/data-access-hscla2020/

- SQL search: https://hscla.mtk.nao.ac.jp/datasearch/ 
    - The schema browser of the database: https://hscla.mtk.nao.ac.jp/schema/ 
- DAS Image Cutout: https://hscla.mtk.nao.ac.jp/das_cutout/la2020/
    - Manual: https://hscla.mtk.nao.ac.jp/das_cutout/la2020/manual.html
- PSF picker: https://hscla.mtk.nao.ac.jp/psf/la2020/
    - Manual: https://hscla.mtk.nao.ac.jp/psf/la2020/manual.html
- DAS search form: https://hscla.mtk.nao.ac.jp/das_search/la2020/
    - Manual: https://hscla.mtk.nao.ac.jp/das_search/la2020/usage.html
- File structure: https://hscla.mtk.nao.ac.jp/archive/files/la2020/

## Data Access (Command line scripts)

- All tools: https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020?ref_type=heads
    - `catalogQuery`: https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020/catalogQuery?ref_type=heads 
        - Key script: `hscSspQuery.py`
    - `colorPostage`: https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020/colorPostage?ref_type=heads
        - Key script: `colorPostage.py`
    - `downloadCutout`: https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020/downloadCutout?ref_type=heads
        - Key script: `downloadCutout.py`
    - `downloadPsf`: https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/-/tree/master/la2020/downloadPsf?ref_type=heads
        - Key script: `downloadPsf.py`
    - `hscSspCrossMatch`: https://hsc-gitlab.mtk.nao.ac.jp/ssp-software/data-access-tools/tree/master/pdr2/hscSspCrossMatch
        - Key script: `hscSspCrossMatch.py`
        - Use the following option for HSCLA: `--rerun=la2020`

## HSCLA 2020 Catalogs: 

### Key metadata: 

- `la2020.mosaic`: Metadata table of mosaiced/stacked coadd image data
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.mosaic
- `la2020.frame`: Metadata table of reduced CCD image data
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.frame
- `la2020.photocalib`: Metadata table of photometric calibration of CCD images. Zeropoints in this table resulted from mosaicking (more precisely, jointcal.py)
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.photocalib

### Key science data: 

- `la2020.forced`: The summary table of forced photometry on coadd images.
    - Basic information (ID, survey footprint, isPrimary, Milky Way extinction coefficients)
    - Footprint and peak: `merge_footprint_` and `merge_peak_` in all possible filters.
    - All the pixel flags. 
    - CModel photometry
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced
- `la2020.forced_aper`: aperture fluxes
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_aper
- `la2020.forced_conv`: PSF-convolved aperture fluxes.
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_conv
- `la2020.forced_flux`: Gaussian, PSF, Kron, Undeblended PSF, Undeblended Kron fluxes. 
    - Undeblended means that the photometry was performed on the "parent" footprint of the image.
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_flux
- `la2020.forced_other`: input count values, variance, localbackground, SDSS shape, double Shaplet PSF, SDSS centroid
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_other
- `la2020.forced_undeb_aper`: Undeblended aperture photometry (without homogenizing the PSF)
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_undeb_aper
- `la2020.forced_undeb_conv`: undeblended aperture photometry after homogenizing the PSF through convolution.
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_undeb_conv
    - This is the best photometry for photo-z
- `la2020.forced_undeb_conv_flag`: the flags for the `forced_undeb_conv` photometry. 
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.forced_undeb_conv_flag
- `la2020.meas`: The summary table of unforced measurements on coadd images.
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas
- `la2020.meas_aper`: unforced aperture photometry
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_aper 
- `la2020.meas_centroid`: Naive centroid; SDSS centroid
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_centroid
- `la2020.meas_cmodel`: Unforced CModel 
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_cmodel
- `la2020.meas_conv`: Unforced convolved aperture photometry 
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_conv
- `la2020.meas_flux`: Unforced Gaussian, PSF, and Kron flux.
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_flux
- `la2020.meas_hsm`: HSM PSF measurements
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_hsm 
- `la2020.meas_other`: input count, variance, local background, footprint area, SDSS shape, blendedness, and double Shaplet PSF.
    - https://hscla.mtk.nao.ac.jp/schema/#la2020.la2020.meas_other
    - SDSS shape, and blendedness are the most useful information here.




### Notes about photometric data: 

- Fluxes are in nano-jansky units, and positions are in sky coordinates. 
- Shapes and ellipticities are re-projected into the planes tangent to the celestial sphere at the objects' own positions (the first axis parallels RA, the second axis DEC; the coordinates in the tangent planes are flipped compared to coadd images).
- The following search functions are available in where-clauses:
    - `coneSearch(coord, RA[deg], DEC[deg], RADIUS[arcsec]) -> boolean`: This function returns True if coord is within a circle at (RA, DEC) with its radius RADIUS.
    - `boxSearch(coord, RA1, RA2, DEC1, DEC2) -> boolean`: This function returns True if coord is within a box [RA1, RA2] x [DEC1, DEC2] (Units are degrees). Note that boxSearch(coord, 350, 370, DEC1, DEC2) is different from boxSearch(coord, 350, 10, DEC1, DEC2). In the former, ra in [350, 360] U [0, 10]; while in the latter, ra in [10, 350].
    - `tractSearch(object_id, TRACT) -> boolean`: This function returns True if tract = TRACT.
    - `tractSearch(object_id, TRACT1, TRACT2)` -> boolean: This function returns True if tract in [TRACT1, TRACT2].
