# perspecto

Generate images from DEMs

## Synopsis

```
perspecto [ -hvCM [ args ] ] DEM ...
```

## Description

Generate images from DEMs, including perspectives, hillshades, etc. (Table 1)

## Options
`-C, --cpt`
Color Pallette file (if not specified will auto-generate ETOPO CPT)

`-M, --module`
Desired perspecto MODULE and options. (see available Modules below)
Where MODULE is module[:mod_opt=mod_val[:mod_opt1=mod_val1[:...]]]

`--min_z`
Minimum z value to use in CPT

`--max_z`
Maximum z value to use in CPT

`--help`
Print the usage text

`--modules`
Display the module descriptions and usage

`--version`
Print the version information

**Table 1.** Modules available in the CUDEM software tool "perspecto"

|  ***Name***  |  ***Description*** |
|----------------------|----------------------------------|
| hillshade | generate a DEM hillshade (req. gdal/imagemagick) |
| perspective | generate a DEM perspective (req. POVRay) |
| sphere | generate a DEM on a sphere |
| figure1 | generate a DEM figure (req. GMT) |
| colorbar | generate a colorbar image based on the input DEM/CPT |

## Python API

## Examples