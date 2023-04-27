### gdalfun.py - GDAL functions
##
## Copyright (c) 2010 - 2023 Regents of the University of Colorado
##
## demfun.py is part of CUDEM
##
## Permission is hereby granted, free of charge, to any person obtaining a copy 
## of this software and associated documentation files (the "Software"), to deal 
## in the Software without restriction, including without limitation the rights 
## to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies 
## of the Software, and to permit persons to whom the Software is furnished to do so, 
## subject to the following conditions:
##
## The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
##
## THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, 
## INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR 
## PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE 
## FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, 
## ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
##
### Commentary:
##
## Functions, etc. for common gdal/ogr/osr usage.
##
### Code:

import os
import shutil

from osgeo import gdal
from osgeo import osr
from osgeo import ogr
import numpy as np
import scipy
    
from cudem import utils
from cudem import regions
from cudem import xyzfun

## ==============================================
## OSR/WKT
## ==============================================
def wkt2geom(wkt):
    """transform a wkt to an ogr geometry

    -----------
    Parameters:
      wkt (wkt): a wkt geometry

    --------
    Returns:
      ogr-geom: the ogr geometry
    """
    
    return(ogr.CreateGeometryFromWkt(wkt))

def osr_wkt(src_srs, esri=False):
    """convert a src_srs to wkt"""
    
    try:
        sr = osr.SpatialReference()
        sr.SetFromUserInput(src_srs)
        if esri:
            sr.MorphToESRI()
            
        return(sr.ExportToWkt())
    except:
        return(None)

def osr_prj_file(dst_fn, src_srs):
    """generate a .prj file given a src_srs"""
    
    with open(dst_fn, 'w') as out:
        out.write(osr_wkt(src_srs, True))
        
    return(0)

def epsg_from_input(in_srs):
    src_srs = osr.SpatialReference()
    src_srs.SetFromUserInput(in_srs)

    ## HORZ
    if src_srs.IsGeographic() == 1:
        cstype = 'GEOGCS'
    else:
        cstype = 'PROJCS'

    src_srs.AutoIdentifyEPSG()
    an = src_srs.GetAuthorityName(cstype)
    src_horz = src_srs.GetAuthorityCode(cstype)

    if src_horz is None:
        src_horz = src_srs.ExportToProj4()
    else:
        src_horz = '{}:{}'.format(an, src_horz)
        
    ## VERT
    if src_srs.IsVertical() == 1:
        csvtype = 'VERT_CS'
        src_vert = src_srs.GetAuthorityCode(csvtype)
    else:
        src_vert = None

    return(src_horz, src_vert)
    
def osr_parse_srs(src_srs, return_vertcs = True):
    if src_srs is not None:
        if src_srs.IsLocal() == 1:
            return(src_srs.ExportToWkt())
        if src_srs.IsGeographic() == 1:
            cstype = 'GEOGCS'
        else:
            cstype = 'PROJCS'
            
        src_srs.AutoIdentifyEPSG()
        an = src_srs.GetAuthorityName(cstype)
        ac = src_srs.GetAuthorityCode(cstype)

        #if return_vertcs:
        if src_srs.IsVertical() == 1:
            csvtype = 'VERT_CS'
            vn = src_srs.GetAuthorityName(csvtype)
            vc = src_srs.GetAuthorityCode(csvtype)
        else:
            csvtype = vc = vn = None

        if an is not None and ac is not None:
            if vn is not None and vc is not None:
                return('{}:{}+{}'.format(an, ac, vc))
            else:
                return('{}:{}'.format(an, ac))
        else:
            dst_srs = src_srs.ExportToProj4()
            if dst_srs:
                    return(dst_srs)
            else:
                return(None)
    else:
        return(None)

## ==============================================
## OGR
## ==============================================
def ogr_fext(src_drv_name):
    """find the common file extention given a OGR driver name
    older versions of gdal can't do this, so fallback to known standards.

    Args:
      src_drv_name (str): a source OGR driver name

    Returns:
      list: a list of known file extentions or None
    """
    
    fexts = None
    try:
        drv = ogr.GetDriverByName(src_drv_name)
        fexts = drv.GetMetadataItem(gdal.DMD_EXTENSIONS)
        if fexts is not None:
            return(fexts.split()[0])
        else:
            return(None)
    except:
        if src_drv_name.lower() == 'gtiff': fext = 'tif'
        elif src_drv_name == 'HFA': fext = 'img'
        elif src_drv_name == 'GMT': fext = 'grd'
        elif src_drv_name.lower() == 'netcdf': fext = 'nc'
        else: fext = 'gdal'
        
        return(fext)


def gdal_get_srs(src_gdal):
    src_ds = gdal.Open(src_gdal)
    src_srs = src_ds.GetSpatialRef()
    src_ds = None
    return(osr_parse_srs(src_srs))
    
def ogr_get_srs(src_ogr):
    src_ds = ogr.Open(src_ogr, 0)
    src_srs = src_ds.GetLayer().GetSpatialRef()
    src_ds = None
    return(osr_parse_srs(src_srs))
    
def ogr_clip(src_ogr_fn, dst_region=None, layer=None, overwrite=False):

    dst_ogr_bn = '.'.join(src_ogr_fn.split('.')[:-1])
    dst_ogr_fn = '{}_{}.gpkg'.format(dst_ogr_bn, dst_region.format('fn'))
    
    if not os.path.exists(dst_ogr_fn) or overwrite:
        run_cmd('ogr2ogr -nlt PROMOTE_TO_MULTI {} {} -clipsrc {} {} '.format(dst_ogr_fn, src_ogr_fn, dst_region.format('te'), layer if layer is not None else ''), verbose=True)
                
    return(dst_ogr_fn)

def ogr_clip2(src_ogr_fn, dst_region=None, layer=None, overwrite=False):

    dst_ogr_bn = '.'.join(src_ogr_fn.split('.')[:-1])
    dst_ogr_fn = '{}_{}.gpkg'.format(dst_ogr_bn, dst_region.format('fn'))
    
    if not os.path.exists(dst_ogr_fn) or overwrite:
    
        src_ds = ogr.Open(src_ogr_fn)
        if layer is not None:
            src_layer = src_ds.GetLayer(layer)
        else:
            src_layer = src_ds.GetLayer()
            
        region_ogr = 'region_{}.shp'.format(dst_region.format('fn'))
        dst_region.export_as_ogr(region_ogr)
        region_ds = ogr.Open(region_ogr)
        region_layer = region_ds.GetLayer()

        driver = ogr.GetDriverByName('GPKG')
        dst_ds = driver.CreateDataSource(dst_ogr_fn)
        dst_layer = dst_ds.CreateLayer(layer if layer is not None else 'clipped', geom_type=ogr.wkbPolygon)

        ogr.Layer.Clip(src_layer, region_layer, dst_layer)

        src_ds = region_ds = dst_ds = None
        remove_glob('{}.*'.format('.'.join(region_ogr.split('.')[:-1])))
        
    return(dst_ogr_fn)

def ogr_clip3(src_ogr, dst_ogr, clip_region=None, dn="ESRI Shapefile"):
    driver = ogr.GetDriverByName(dn)
    ds = driver.Open(src_ogr, 0)
    layer = ds.GetLayer()

    clip_region.export_as_ogr('tmp_clip.{}'.format(ogr_fext(dn)))
    c_ds = driver.Open('tmp_clip.{}'.format(ogr_fext(dn)), 0)
    c_layer = c_ds.GetLayer()
    
    dst_ds = driver.CreateDataSource(dst_ogr)
    dst_layer = dst_ds.CreateLayer(
        dst_ogr.split('.')[0], geom_type=ogr.wkbMultiPolygon
    )

    layer.Clip(c_layer, dst_layer)
    ds = c_ds = dst_ds = None

def ogr_mask_union(src_layer, src_field, dst_defn=None):
    """`union` a `src_layer`'s features based on `src_field` where
    `src_field` holds a value of 0 or 1. optionally, specify
    an output layer defn for the unioned feature.

    returns the output feature class
    """
    
    if dst_defn is None:
        dst_defn = src_layer.GetLayerDefn()
        
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    src_layer.SetAttributeFilter("{} = 1".format(src_field))
    feats = len(src_layer)
    
    if feats > 0:
        with utils.CliProgress(total=len(src_layer), message='unioning {} features...'.format(feats)) as pbar:
            for n, f in enumerate(src_layer):
                pbar.update()
                f_geom = f.geometry()
                f_geom_valid = f_geom
                multi.AddGeometry(f_geom_valid)
            
    utils.echo_msg('setting geometry to unioned feature...')
    out_feat = ogr.Feature(dst_defn)
    out_feat.SetGeometry(multi)
    union = multi = None
    
    return(out_feat)

def ogr_empty_p(src_ogr, dn='ESRI Shapefile'):
    driver = ogr.GetDriverByName(dn)
    ds = driver.Open(src_ogr, 0)

    if ds is not None:
        layer = ds.GetLayer()
        fc = layer.GetFeatureCount()
        if fc == 0:
            return(True)
        else: return(False)
        
    else: return(True)

def ogr_polygonize_multibands(src_ds, dst_srs = 'epsg:4326', ogr_format='ESRI Shapefile', verbose=True):
    dst_layer = '{}_sm'.format(fn_basename2(src_ds.GetDescription()))
    dst_vector = dst_layer + '.{}'.format(ogr_fext(ogr_format))
    remove_glob('{}.*'.format(dst_layer))
    osr_prj_file('{}.prj'.format(dst_layer), dst_srs)
    ds = ogr.GetDriverByName(ogr_format).CreateDataSource(dst_vector)
    if ds is not None: 
        layer = ds.CreateLayer(
            '{}'.format(dst_layer), None, ogr.wkbMultiPolygon
        )
        [layer.SetFeature(feature) for feature in layer]
    else:
        layer = None

    layer.CreateField(ogr.FieldDefn('DN', ogr.OFTInteger))
    defn = None

    for b in range(1, src_ds.RasterCount+1):
        this_band = src_ds.GetRasterBand(b)
        this_band_md = this_band.GetMetadata()
        b_infos = gdal_infos(src_ds, scan=True, band=b)
        field_names = [field.name for field in layer.schema]
        for k in this_band_md.keys():
            if k[:9] not in field_names:
                layer.CreateField(ogr.FieldDefn(k[:9], ogr.OFTString))
        
        if b_infos['zr'][1] == 1:
            tmp_ds = ogr.GetDriverByName('Memory').CreateDataSource(
                '{}_poly'.format(this_band.GetDescription())
            )
                    
            if tmp_ds is not None:
                tmp_layer = tmp_ds.CreateLayer(
                    '{}_poly'.format(this_band.GetDescription()), None, ogr.wkbMultiPolygon
                )
                tmp_layer.CreateField(ogr.FieldDefn('DN', ogr.OFTInteger))
                for k in this_band_md.keys():
                    tmp_layer.CreateField(ogr.FieldDefn(k[:9], ogr.OFTString))
                
                if verbose:
                    echo_msg('polygonizing {} mask...'.format(this_band.GetDescription()))
                            
                status = gdal.Polygonize(
                    this_band,
                    None,
                    tmp_layer,
                    tmp_layer.GetLayerDefn().GetFieldIndex('DN'),
                    [],
                    callback = gdal.TermProgress if verbose else None
                )

                if len(tmp_layer) > 0:
                    if defn is None:
                        defn = tmp_layer.GetLayerDefn()

                    out_feat = gdal_ogr_mask_union(tmp_layer, 'DN', defn)
                    echo_msg('creating feature {}...'.format(this_band.GetDescription()))
                    for k in this_band_md.keys():
                        out_feat.SetField(k[:9], this_band_md[k])
                    
                    layer.CreateFeature(out_feat)

            if verbose:
                echo_msg('polygonized {}'.format(this_band.GetDescription()))
            tmp_ds = tmp_layer = None
    ds = None

## ==============================================
## GDAL
## ==============================================
class gdal_datasource:
    """

    use as:
    with gdal_datasource('input.tif') as src_ds:
        src_ds.do_things()

    where the input can be either a path-name to a gdal supported
    file or a gdal.Dataset object.

    this is for when you don't like opening and closing gdal datasurces all the time.
    """
    
    def __init__(self, src_gdal = None, update = False):
        self.src_gdal = src_gdal
        self.update = update
        self.src_ds = None

    ## todo: create new datasource if src_gdal is path, but doesn't exists...
    def __enter__(self):
        if isinstance(self.src_gdal, gdal.Dataset):
            self.src_ds = self.src_gdal

        elif utils.str_or(self.src_gdal) is not None and os.path.exists(self.src_gdal):
            self.src_ds = gdal.Open(self.src_gdal, 0 if not self.update else 1)

        return(self.src_ds)

    def __exit__(self, exc_type, exc_value, exc_traceback):
        ## don't close src_ds if incoming was already open
        #print(exc_type, exc_value, exc_traceback)
        if not isinstance(self.src_gdal, gdal.Dataset):
            self.src_ds = None
            
def gdal_fext(src_drv_name):
    """find the common file extention given a GDAL driver name
    older versions of gdal can't do this, so fallback to known standards.

    Args:
      src_drv_name (str): a source GDAL driver name

    Returns:
      list: a list of known file extentions or None
    """
    
    fexts = None
    try:
        drv = gdal.GetDriverByName(src_drv_name)
        if drv.GetMetadataItem(gdal.DCAP_RASTER):
            fexts = drv.GetMetadataItem(gdal.DMD_EXTENSIONS)
            
        if fexts is not None:
            return(fexts.split()[0])
        else:
            return(None)
    except:
        if src_drv_name.lower() == 'gtiff': fext = 'tif'
        elif src_drv_name == 'HFA': fext = 'img'
        elif src_drv_name == 'GMT': fext = 'grd'
        elif src_drv_name.lower() == 'netcdf': fext = 'nc'
        else: fext = 'gdal'
        
        return(fext)

def gdal_set_infos(nx, ny, nb, geoT, proj, dt, ndv, fmt, md, rc):
    """set a datasource config dictionary

    returns gdal_config dict.
    """
    
    return({'nx': nx, 'ny': ny, 'nb': nb, 'geoT': geoT, 'proj': proj, 'dt': dt,
            'ndv': ndv, 'fmt': fmt, 'metadata': md, 'raster_count': rc})
    
def gdal_infos(src_gdal, region = None, scan = False, band = 1):
    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            if region is not None:
                srcwin = region.srcwin(gt, src_ds.RasterXSize, src_ds.RasterYSize)
            else:
                srcwin = (0, 0, src_ds.RasterXSize, src_ds.RasterYSize)

            gt = src_ds.GetGeoTransform()        
            dst_gt = (gt[0] + (srcwin[0] * gt[1]), gt[1], 0., gt[3] + (srcwin[1] * gt[5]), 0., gt[5])
            src_band = src_ds.GetRasterBand(band)
            ds_config = {
                'nx': srcwin[2],
                'ny': srcwin[3],
                'nb': srcwin[2] * srcwin[3],
                'geoT': dst_gt,
                'proj': src_ds.GetProjectionRef(),
                'dt': src_band.DataType,
                'dtn': gdal.GetDataTypeName(src_band.DataType),
                'ndv': src_band.GetNoDataValue(),
                'fmt': src_ds.GetDriver().ShortName,
                'metadata': src_ds.GetMetadata(),
                'raster_count': src_ds.RasterCount,
                'zr': None,
            }
            if ds_config['ndv'] is None:
                ds_config['ndv'] = -9999

            if scan:
                src_arr = src_band.ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                ds_config['zr'] = src_band.ComputeRasterMinMax()
                src_arr = src_band = None

            return(ds_config)

def gdal_copy_infos(src_config):
    """copy src_config

    returns copied src_config dict.
    """
    
    dst_config = {}
    for dsc in src_config.keys():
        dst_config[dsc] = src_config[dsc]
        
    return(dst_config)
        
def gdal_set_srs(src_gdal, src_srs = 'epsg:4326', verbose = True):
    with gdal_datasource(src_gdal, update=True) as src_ds:    
        if src_ds is not None and src_srs is not None:
            try:
                src_ds.SetProjection(osr_wkt(src_srs))
                return(0)
            except Exception as e:
                if verbose:
                    utils.echo_warning_msg('could not set projection {}'.format(src_srs))
                return(None)
        else:
            return(None)    

def gdal_get_ndv(src_gdal, band = 1):
    with gdal_datasource(src_gdal) as src_ds:    
        if src_ds is not None:
            src_band = src_ds.GetRasterBand(band)
            return(src_band.GetNoDataValue())

def gdal_set_ndv(src_gdal, ndv = -9999, convert_array = False, verbose  =True):
    """set the nodata value of gdal datasource

    returns 0
    """

    with gdal_datasource(src_gdal, update=True) as src_ds:        
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            curr_nodata = ds_config['ndv']

            for band in range(1, src_ds.RasterCount+1):
                this_band = src_ds.GetRasterBand(band)
                this_band.DeleteNoDataValue()

            for band in range(1, src_ds.RasterCount+1):
                this_band = src_ds.GetRasterBand(band)
                this_band.SetNoDataValue(ndv)

            if convert_array:
                for band in range(1, src_ds.RasterCount+1):
                    this_band = src_ds.GetRasterBand(band)
                    arr = this_band.ReadAsArray()
                    if np.isnan(curr_nodata):
                        arr[np.isnan(arr)] = ndv
                    else:
                        arr[arr == curr_nodata] = ndv

                    this_band.WriteArray(arr)            
            return(0)
        else:
            return(None)

def gdal_has_ndv(src_gdal, band = 1):
    with gdal_datasource(src_gdal) as src_ds:        
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            ds_arr = ds.GetRasterBand(band).ReadAsArray()
            ds_arr[ds_arr == ds_config['ndv']] = np.nan

            if np.any(np.isnan(ds_arr)):
                return(True)
            
        return(False)
        
def gdal_mem_ds(ds_config, name = 'MEM', bands = 1):
    """Create temporary gdal mem dataset"""
        
    mem_driver = gdal.GetDriverByName('MEM')
    mem_ds = mem_driver.Create(name, ds_config['nx'], ds_config['ny'], bands, ds_config['dt'])
    if mem_ds is not None:
        mem_ds.SetGeoTransform(ds_config['geoT'])
        if ds_config['proj'] is not None:
            mem_ds.SetProjection(ds_config['proj'])

        for band in range(1, bands+1):
            mem_band = mem_ds.GetRasterBand(band)
            mem_band.SetNoDataValue(ds_config['ndv'])
        
    return(mem_ds)

def gdal_extract_band(src_gdal, dst_gdal, band = 1, exclude = [], inverse = False):

    with gdal_datasource(src_gdal) as src_ds:        
        ds_config = gdal_infos(src_ds)
        ds_band = src_ds.GetRasterBand(band)
        ds_array = ds_band.ReadAsArray()

    if ds_config['ndv'] is None:
        ds_config['ndv'] = -9999
    
    if exclude:
        for key in exclude:
            ds_array[ds_array == key] = ds_config['ndv']

    if inverse:
        ds_array = 1/ds_array
            
    return(gdal_write(ds_array, dst_gdal, ds_config))

def gdal_get_array(src_dem, band = 1):
    with gdal_datasource(src_gdal) as src_ds:        
        if src_ds is not None:
            src_band = src_ds.GetRasterBand(band)
            src_array = band.ReadAsArray()
            src_offset = band.GetOffset()
            src_scale = band.GetScale()

            if src_offset is not None or src_scale is not None:
                src_array = np.ndarray.astype(src_array, dtype=np.float32)

                if src_offset is not None:
                    src_array += offset

                if src_scale is not None:
                    src_array *= scale

            infos = gdal_infos(src_ds)
            return(src_array, infos)
        else:
            utils.echo_error_msg('could not open {}'.format(src_gdal))
            return(None, None)

def gdal_cut(src_gdal, src_region, dst_gdal, node='pixel', verbose=True):
    """cut src_ds datasource to src_region and output dst_gdal file

    returns [output-dem, status-code]
    """

    with gdal_datasource(src_gdal) as src_ds:        
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            gt = ds_config['geoT']
            srcwin = src_region.srcwin(gt, src_ds.RasterXSize, src_ds.RasterYSize, node=node)
            dst_gt = (gt[0] + (srcwin[0] * gt[1]), gt[1], 0., gt[3] + (srcwin[1] * gt[5]), 0., gt[5])
            out_ds_config = gdal_set_infos(srcwin[2], srcwin[3], srcwin[2] * srcwin[3], dst_gt,
                                           ds_config['proj'], ds_config['dt'], ds_config['ndv'],
                                           ds_config['fmt'], ds_config['metadata'], ds_config['raster_count'])

            in_bands = src_ds.RasterCount
            mem_ds = gdal_mem_ds(out_ds_config, bands=in_bands)
            for band in range(1, in_bands+1):
                this_band = mem_ds.GetRasterBand(band)
                this_band.WriteArray(src_ds.GetRasterBand(band).ReadAsArray(*srcwin))
                mem_ds.FlushCache()

            dst_ds = gdal.GetDriverByName(ds_config['fmt']).CreateCopy(dst_gdal, mem_ds, 0)                
            return(dst_gdal, 0)
        else:
            return(None, -1)

def gdal_clip(src_gdal, dst_gdal, src_ply = None, invert = False, verbose = True):
    """clip dem to polygon `src_ply`, optionally invert the clip.

    returns [gdal_raserize-output, gdal_rasterize-return-code]
    """

    gi = gdal_infos(src_gdal)
    g_region = regions.Region().from_geo_transform(geo_transform=gi['geoT'], x_count=gi['nx'], y_count=gi['ny'])
    tmp_ply = '__tmp_clp_ply.shp'
    
    out, status = utils.run_cmd('ogr2ogr {} {} -clipsrc {} -nlt POLYGON -skipfailures'.format(tmp_ply, src_ply, g_region.format('ul_lr')), verbose=verbose)
    if gi is not None and src_ply is not None:
        #if invert:
        #    gr_cmd = 'gdalwarp -cutline {} -cl {} {} {}'.format(tmp_ply, os.path.basename(tmp_ply).split('.')[0], src_dem, dst_dem)
        #    out, status = utils.run_cmd(gr_cmd, verbose=verbose)
        #else:
        shutil.copyfile(src_gdal, dst_gdal)
        gr_cmd = 'gdal_rasterize -burn {} -l {} {} {}{}'\
            .format(gi['ndv'], os.path.basename(tmp_ply).split('.')[0], tmp_ply, dst_gdal, ' -i' if invert else '')
        out, status = utils.run_cmd(gr_cmd, verbose=verbose)
        utils.remove_glob('__tmp_clp_ply.*')
    else:
        return(None, -1)
    
    return(out, status)

def crop(src_gdal, dst_gdal):
    with gdal_datasource(src_gdal) as src_ds: 
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            ds_arr = src_ds.GetRasterBand(1).ReadAsArray()
            ds_arr[ds_arr == ds_config['ndv']] = np.nan
            nans = np.isnan(ds_arr)

            nancols = np.all(nans, axis=0)
            nanrows = np.all(nans, axis=1)

            firstcol = nancols.argmin()
            firstrow = nanrows.argmin()        
            lastcol = len(nancols) - nancols[::-1].argmin()
            lastrow = len(nanrows) - nanrows[::-1].argmin()

            dst_arr = ds_arr[firstrow:lastrow,firstcol:lastcol]
            ds_arr = None

            dst_arr[np.isnan(dst_arr)] = ds_config['ndv']
            GeoT = ds_config['geoT']
            dst_x_origin = GeoT[0] + (GeoT[1] * firstcol)
            dst_y_origin = GeoT[3] + (GeoT[5] * firstrow)
            dst_geoT = [dst_x_origin, GeoT[1], 0.0, dst_y_origin, 0.0, GeoT[5]]
            ds_config['geoT'] = dst_geoT
            ds_config['nx'] = int(lastcol - firstcol)
            ds_config['ny'] = int(lastrow - firstrow)
            ds_config['nb'] = int(ds_config['nx'] * ds_config['ny'])

            return(gdal_write(dst_arr, dst_gdal, ds_config))
        else:
            return(None, -1)

def gdal_split(src_gdal, split_value = 0, band = 1):
    """split raster file `src_dem`into two files based on z value, 
    or if split_value is a filename, split raster by overlay, where upper is outside and lower is inside.

    returns [upper_grid-fn, lower_grid-fn]
    """

    def np_split(src_arr, sv = 0, nd = -9999):
        """split numpy `src_arr` by `sv` (turn u/l into `nd`)
        
        returns [upper_array, lower_array]
        """
        
        sv = utils.int_or(sv, 0)
        u_arr = np.array(src_arr)
        l_arr = np.array(src_arr)
        u_arr[u_arr <= sv] = nd
        l_arr[l_arr >= sv] = nd
        return(u_arr, l_arr)
    
    dst_upper = os.path.join(os.path.dirname(src_gdal), '{}_u.tif'.format(os.path.basename(src_gdal)[:-4]))
    dst_lower = os.path.join(os.path.dirname(src_gdal), '{}_l.tif'.format(os.path.basename(src_gdal)[:-4]))

    with gdal_datasource(src_gdal) as src_ds:        
        if src_ds is not None:
            src_config = gdal_infos(src_ds)
            dst_config = gdal_copy_infos(src_config)
            dst_config['fmt'] = 'GTiff'
            ds_arr = src_ds.GetRasterBand(band).ReadAsArray(0, 0, src_config['nx'], src_config['ny'])
            ua, la = np_split(ds_arr, split_value, src_config['ndv'])
            gdal_write(ua, dst_upper, dst_config)
            gdal_write(la, dst_lower, dst_config)
            ua = la = ds_arr = src_ds = None
            return([dst_upper, dst_lower])
        
        else:
            return(None)
        
def gdal_percentile(src_gdal, perc = 95, band = 1):
    """calculate the `perc` percentile of src_fn gdal file.

    return the calculated percentile
    """

    with gdal_datasource(src_gdal) as src_ds:        
        if src_ds is not None:
            ds_array = np.array(src_ds.GetRasterBand(band).ReadAsArray())
            ds_array[ds_array == src_ds.GetRasterBand(band).GetNoDataValue()] = np.nan
            x_dim = ds_array.shape[0]
            ds_array_flat = ds_array.flatten()
            ds_array = ds_array_flat[ds_array_flat != 0]
            if len(ds_array) > 0:
                p = np.nanpercentile(ds_array, perc)
                #percentile = 2 if p < 2 else p
            else: p = 2
            return(p)
        else:
            return(None)

def gdal_slope(src_gdal, dst_gdal, s = 111120):
    """generate a slope grid with GDAL

    return cmd output and status
    """
    
    gds_cmd = 'gdaldem slope {} {} {} -compute_edges'.format(src_gdal, dst_gdal, '' if s is None else '-s {}'.format(s))
    return(utils.run_cmd(gds_cmd))
    
def gdal_proximity(src_gdal, dst_gdal, band = 1):
    """compute a proximity grid via GDAL

    return 0 if success else None
    """

    prog_func = None
    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            src_band = src_ds.GetRasterBand(band)
            ds_config = gdal_infos(src_ds)
            drv = gdal.GetDriverByName('GTiff')
            dst_ds = drv.Create(dst_fn, ds_config['nx'], ds_config['ny'], 1, ds_config['dt'], [])                
            dst_ds.SetGeoTransform(ds_config['geoT'])
            dst_ds.SetProjection(ds_config['proj'])
            dst_band = dst_ds.GetRasterBand(1)
            dst_band.SetNoDataValue(ds_config['ndv'])
            gdal.ComputeProximity(src_band, dst_band, ['DISTUNITS=PIXEL'], callback = prog_func)
            dst_ds = None
            return(0)
        
        else:
            return(None)
        
def gdal_polygonize(src_gdal, dst_layer, verbose = False):
    '''run gdal.Polygonize on src_ds and add polygon to dst_layer'''

    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            ds_arr = src_ds.GetRasterBand(1)
            if verbose:
                utils.echo_msg('polygonizing {}...'.format(src_gdal))
                
            status = gdal.Polygonize(
                ds_arr, None, dst_layer, 0,
                callback=gdal.TermProgress if verbose else None
            )
            if verbose:
                utils.echo_msg('polygonized {}'.format(src_gdal))
                
            return(0, 0)
        
        else:
            return(-1, -1)

def gdal_mask(src_gdal, msk_gdal, out_gdal, msk_value = None, verbose = True):

    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            src_config = gdal_infos(src_ds)
            src_band = src_ds.GetRasterBand(1)
            src_array = src_band.ReadAsArray()

            tmp_region = regions.Region().from_geo_transform(src_config['geoT'], src_config['nx'], src_config['ny'])
            #tmp_ds = gdal.Open(msk_dem)
            with gdal_datasource(msk_gdal) as tmp_ds:
                msk_ds = sample_warp(
                    tmp_ds, None, src_config['geoT'][1], src_config['geoT'][5],
                    src_region=tmp_region, sample_alg='bilinear',
                    verbose=verbose
                )[0] 
                if msk_ds is not None:
                    msk_band = msk_ds.GetRasterBand(1)
                    msk_array = msk_band.ReadAsArray()

                    if msk_value is None:
                        msk_value = msk_band.GetNoDataValue()

                    src_array[msk_array == msk_value] = src_band.GetNoDataValue()

                    gdal_write(src_array, out_gdal, src_config)
                    msk_ds = None
        
def gdal_blur(src_gdal, dst_gdal, sf = 1):
    """gaussian blur on src_dem using a smooth-factor of `sf`
    runs np_gaussian_blur(ds.Array, sf)

    generates edges with nodata...
    """

    def np_gaussian_blur(in_array, size):
        """blur an array using fftconvolve from scipy.signal
        size is the blurring scale-factor.

        returns the blurred array
        """

        #from scipy.signal import fftconvolve
        #from scipy.signal import convolve
        padded_array = np.pad(in_array, size, 'symmetric')
        x, y = np.mgrid[-size:size + 1, -size:size + 1]
        g = np.exp(-(x**2 / float(size) + y**2 / float(size)))
        g = (g / g.sum()).astype(in_array.dtype)
        in_array = None
        out_array = np.convolve(padded_array, g, mode = 'valid')
        return(out_array)

    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            ## original array
            ds_array = src_ds.GetRasterBand(1).ReadAsArray(0, 0, ds_config['nx'], ds_config['ny'])
            ## copy original array as data mask
            msk_array = np.array(ds_array)
            ## set mask to 1/0
            msk_array[msk_array == ds_config['ndv']] = np.nan
            msk_array[~np.isnan(msk_array)] = 1
            #msk_array[np.isnan(msk_array)] = 0
            #msk_array[msk_array != ds_config['ndv']] = 1
            ds_array[np.isnan(msk_array)] = 0
            #ds_array[ds_array == ds_config['ndv']] = 0
            smooth_array = np_gaussian_blur(ds_array, int(sf))
            smooth_array = smooth_array * msk_array
            mask_array = ds_array = None
            smooth_array[np.isnan(smooth_array)] = ds_config['ndv']
            return(gdal_write(smooth_array, dst_gdal, ds_config))
        
        else:
            return([], -1)

def gdal_filter_outliers(src_gdal, dst_gdal, chunk_size=None, chunk_step=None, agg_level=1, replace=True):
    """scan a src_dem file for outliers and remove them
    
    aggressiveness depends on the outlier percentiles and the chunk_size/step; 75/25 is default 
    for statistical outlier discovery, 55/45 will be more aggressive, etc. Using a large chunk size 
    will filter more cells and find potentially more or less outliers depending on the data.
    agg_level is 1 to 9
    """

    agg_level = utils.int_or(agg_level)

    if agg_level is not None:
        percentile = 100 - (agg_level*10)
        if percentile < 50: percentle = 50
        if percentile > 100: percentile = 100
        #curv_percentile = 0 + (agg_level*10)

    percentile = agg_level    
    max_percentile = percentile
    min_percentile = 100 - percentile
    #curv_max_percentile = percentile
    #curv_min_percentile = 100 - percentile
    
    with gdal_datasource(src_gdal) as src_ds:
        if src_ is not None:
            tnd = 0        
            ds_config = gdal_infos(ds)
            ds_band = src_ds.GetRasterBand(1)
            #ds_array = ds_band.ReadAsArray(0, 0, ds_config['nx'], ds_config['ny'])
            gt = ds_config['geoT']
            gdt = gdal.GDT_Float32
            ndv = ds_band.GetNoDataValue()
            driver = gdal.GetDriverByName('GTiff')
            dst_ds = driver.Create(dst_gdal, ds.RasterXSize, ds.RasterYSize, 1, gdt,
                                   options=['COMPRESS=LZW', 'PREDICTOR=2', 'TILED=YES'])
            dst_ds.SetGeoTransform(gt)
            dst_band = dst_ds.GetRasterBand(1)
            dst_band.SetNoDataValue(ndv)

            if chunk_size is None:
                n_chunk = int(ds_config['nx'] * .05)
                n_chunk = 10 if n_chunk < 10 else n_chunk
            else:
                n_chunk = chunk_size

            chunk_step = utils.int_or(chunk_step)
            n_step = chunk_step if chunk_step is not None else int(n_chunk)
            n_step = n_chunk/4

            utils.echo_msg(
                'scanning {} for outliers with {}@{} using aggression level {} ({}/{})...'.format(
                    src_gdal, n_chunk, n_step, agg_level, max_percentile, min_percentile
                )
            )

            for srcwin in utils.yield_srcwin(
                    (ds.RasterYSize, ds.RasterXSize), n_chunk = n_chunk, step = n_step, verbose=True
            ):
                nd = 0
                band_data = ds_band.ReadAsArray(*srcwin)
                band_data[band_data == ds_config['ndv']] = np.nan

                if np.all(np.isnan(band_data)):
                    #dst_band.WriteArray(band_data, srcwin[0], srcwin[1])
                    continue

                coverage_data = ds_band.ReadAsArray(*srcwin)
                coverage_data[coverage_data == ds_config['ndv']] = np.nan

                this_geo_x_origin, this_geo_y_origin = utils._pixel2geo(srcwin[0], srcwin[1], gt)
                dst_gt = [this_geo_x_origin, float(gt[1]), 0.0, this_geo_y_origin, 0.0, float(gt[5])]
                dst_config = gdal_copy_infos(ds_config)
                dst_config['nx'] = srcwin[2]
                dst_config['ny'] = srcwin[3]
                dst_config['geoT'] = dst_gt

                if not np.all(band_data == band_data[0,:]):
                    px, py = np.gradient(band_data, gt[1])
                    slp_data = np.sqrt(px ** 2, py ** 2)
                    #slp_data = np.degrees(np.arctan(slp_data_))

                    px, py = np.gradient(slp_data, gt[1])
                    curv_data = np.sqrt(px ** 2, py ** 2)
                    #curv_data = np.degrees(np.arctan(curv_data_))

                    srcwin_perc75 = np.nanpercentile(band_data, max_percentile)
                    srcwin_perc25 = np.nanpercentile(band_data, min_percentile)
                    iqr_p = (srcwin_perc75 - srcwin_perc25) * 1.5
                    upper_limit = srcwin_perc75 + iqr_p
                    lower_limit = srcwin_perc25 - iqr_p
                    #utils.echo_msg('elev upper limit: {}'.format(upper_limit))
                    #utils.echo_msg('elev lower limit: {}'.format(lower_limit))

                    slp_srcwin_perc75 = np.nanpercentile(slp_data, max_percentile)
                    slp_srcwin_perc25 = np.nanpercentile(slp_data, min_percentile)
                    slp_iqr_p = (slp_srcwin_perc75 - slp_srcwin_perc25) * 1.5
                    slp_upper_limit = slp_srcwin_perc75 + slp_iqr_p
                    slp_lower_limit = slp_srcwin_perc25 - slp_iqr_p
                    #utils.echo_msg('slp upper limit: {}'.format(slp_upper_limit))
                    #utils.echo_msg('slp lower limit: {}'.format(slp_lower_limit))

                    curv_srcwin_perc75 = np.nanpercentile(curv_data, max_percentile)
                    curv_srcwin_perc25 = np.nanpercentile(curv_data, min_percentile)
                    curv_iqr_p = (curv_srcwin_perc75 - curv_srcwin_perc25) * 1.5
                    curv_upper_limit = curv_srcwin_perc75 + curv_iqr_p
                    curv_lower_limit = curv_srcwin_perc25 - curv_iqr_p
                    #utils.echo_msg('curv upper limit: {}'.format(curv_upper_limit))
                    #utils.echo_msg('curv lower limit: {}'.format(curv_lower_limit))
                    #print(curv_data)

                    band_data[(curv_data > curv_upper_limit)] = np.nan
                    #band_data[((band_data > upper_limit) | (band_data < lower_limit))] = np.nan
                    # band_data[((band_data > upper_limit) | (band_data < lower_limit)) \
                    #           & ((curv_data > curv_upper_limit) | (curv_data < curv_lower_limit)) \
                    #           & ((slp_data > slp_upper_limit) | (slp_data < slp_lower_limit))] = np.nan
                    #band_data[((band_data > upper_limit) | (band_data < lower_limit)) \
                    #          & (curv_data > curv_upper_limit)] = np.nan
                    #(curv_data > curv_upper_limit)] = np.nan

                    ## fill nodata here...
                    if replace:
                        point_indices = np.nonzero(~np.isnan(band_data))
                        if len(point_indices[0]):
                            point_values = band_data[point_indices]
                            xi, yi = np.mgrid[0:srcwin[3], 0:srcwin[2]]

                            try:
                                interp_data = scipy.interpolate.griddata(
                                    np.transpose(point_indices), point_values,
                                    (xi, yi), method='cubic'
                                )
                                interp_data[np.isnan(coverage_data)] = np.nan
                                dst_band.WriteArray(interp_data, srcwin[0], srcwin[1])
                            except:
                                dst_band.WriteArray(band_data, srcwin[0], srcwin[1])
                    else:
                        dst_band.WriteArray(band_data, srcwin[0], srcwin[1])
                        #dst_band.WriteArray(curv_data, srcwin[0], srcwin[1])

            dst_ds = None
            return(dst_gdal, 0)
        else:
            return(None, -1)
        
def sample_warp(
        src_dem, dst_dem, x_sample_inc, y_sample_inc,
        src_srs=None, dst_srs=None, src_region=None, sample_alg='bilinear',
        ndv=-9999, tap=False, size=False, verbose=False
):

    if size:
        xcount, ycount, dst_gt = src_region.geo_transform(
            x_inc=x_sample_inc, y_inc=y_sample_inc, node='pixel'
        )
        x_sample_inc = y_sample_inc = None
    else:
        xcount = ycount = None

    if src_region is not None:
        out_region = [src_region.xmin, src_region.ymin, src_region.xmax, src_region.ymax]
    else: 
        out_region = None

    if verbose:
        utils.echo_msg(
            'warping DEM: {} :: R:{} E:{}/{} S{} P{} -> T{}'.format(
                os.path.basename(str(src_dem)), out_region, x_sample_inc, y_sample_inc, sample_alg, src_srs, dst_srs
            )
        )
        #utils.echo_msg('gdalwarp -s_srs {} -t_srs {} -tr {} {} -r bilinear'.format(src_srs, dst_srs, x_sample_inc, y_sample_inc))
    
    dst_ds = gdal.Warp('' if dst_dem is None else dst_dem, src_dem, format='MEM' if dst_dem is None else 'GTiff',
                       xRes=x_sample_inc, yRes=y_sample_inc, targetAlignedPixels=tap, width=xcount, height=ycount,
                       dstNodata=ndv, outputBounds=out_region, outputBoundsSRS=dst_srs, resampleAlg=sample_alg, errorThreshold=0,
                       options=["COMPRESS=LZW", "TILED=YES"], srcSRS=src_srs, dstSRS=dst_srs, outputType=gdal.GDT_Float32,
                       callback=None)

    if dst_dem is None:
        return(dst_ds, 0)
    else:
        dst_ds = None
        return(dst_dem, 0)    

def gdal_mem_ds(ds_config, name = 'MEM', bands = 1):
    """Create temporary gdal mem dataset"""
        
    mem_driver = gdal.GetDriverByName('MEM')
    mem_ds = mem_driver.Create(name, ds_config['nx'], ds_config['ny'], bands, ds_config['dt'])
    if mem_ds is not None:
        mem_ds.SetGeoTransform(ds_config['geoT'])
        if ds_config['proj'] is not None:
            mem_ds.SetProjection(ds_config['proj'])

        for b in range(1, bands+1):
            mem_band = mem_ds.GetRasterBand(b)
            mem_band.SetNoDataValue(ds_config['ndv'])
        
    return(mem_ds)
    
def gdal_write(src_arr, dst_gdal, ds_config, dst_fmt='GTiff', max_cache=False, verbose=False):
    """write src_arr to gdal file dst_gdal using src_config

    returns [output-gdal, status-code]
    """
    
    driver = gdal.GetDriverByName(dst_fmt)
    if os.path.exists(dst_gdal):
        try:
            driver.Delete(dst_gdal)
        except Exception as e:
            echo_error_msg(e)
            remove_glob(dst_gdal)

    if max_cache:
        gdal.SetCacheMax(2**30)

    if ds_config['dt'] == 5:
        ds = driver.Create(dst_gdal, ds_config['nx'], ds_config['ny'], 1,
                           ds_config['dt'], options=['COMPRESS=LZW', 'PREDICTOR=2', 'TILED=YES'])
    else:
        ds = driver.Create(dst_gdal, ds_config['nx'], ds_config['ny'], 1,
                           ds_config['dt'], options=['COMPRESS=LZW', 'TILED=YES', 'PREDICTOR=3'])

    if ds is not None:
        ds.SetGeoTransform(ds_config['geoT'])
        try:
            ds.SetProjection(ds_config['proj'])
        except Exception as e:
            if verbose:
                echo_warning_msg('could not set projection {}'.format(ds_config['proj']))
            else: pass
        ds.GetRasterBand(1).SetNoDataValue(ds_config['ndv'])
        ds.GetRasterBand(1).WriteArray(src_arr)
        ds = src_arr = None        
        return(dst_gdal, 0)
    else:
        return(None, -1)

def gdal2gdal(src_dem, dst_fmt='GTiff', src_srs='epsg:4326', dst_dem=None, co=True):
    """convert the gdal file to gdal using gdal

    return output-gdal-fn"""
    
    if os.path.exists(src_dem):
        if dst_dem is None:
            #dst_dem = '{}.{}'.format(os.path.basename(src_dem).split('.')[0], gdal_fext(dst_fmt))
            dst_dem = '{}.{}'.format(fn_basename2(src_dem), gdal_fext(dst_fmt))
            
        if dst_fmt != 'GTiff':
            co = False
            
        if not co:
            gdal2gdal_cmd = ('gdal_translate {} {} -f {}'.format(src_dem, dst_dem, dst_fmt))
        else:
            gdal2gdal_cmd = ('gdal_translate {} {} -f {} -co TILED=YES -co COMPRESS=DEFLATE\
            '.format(src_dem, dst_dem, dst_fmt))
            
        out, status = run_cmd(gdal2gdal_cmd, verbose=False)
        if status == 0:
            return(dst_dem)
        else:
            return(None)
    else:
        return(None)

def gdal_yield_srcwin(src_gdal, n_chunk = 10, step = 5, verbose = False):
    """yield source windows in n_chunks at step"""
    
    ds_config = gdal_infos(src_gdal)
    gt = ds_config['geoT']
    x_chunk = n_chunk
    y_chunk = 0
    i_chunk = 0
    x_i_chunk = 0
    
    with utils.CliProgress(total=ds_config['nb']/step, message='chunking srcwin') as pbar:
        while True:
            y_chunk = n_chunk
            while True:
                this_x_chunk = ds_config['nx'] if x_chunk > ds_config['nx'] else x_chunk
                this_y_chunk = ds_config['ny'] if y_chunk > ds_config['ny'] else y_chunk
                this_x_origin = x_chunk - n_chunk
                this_y_origin = y_chunk - n_chunk
                this_x_size = int(this_x_chunk - this_x_origin)
                this_y_size = int(this_y_chunk - this_y_origin)
                if this_x_size == 0 or this_y_size == 0: break
                srcwin = (this_x_origin, this_y_origin, this_x_size, this_y_size)
                yield(srcwin)

                if y_chunk > ds_config['ny']:
                    break
                else:
                    y_chunk += step
                    i_chunk += 1
                    
                pbar.update(step)
                    
            if x_chunk > ds_config['nx']:
                break
            else:
                x_chunk += step
                x_i_chunk += 1
    
def gdal_chunks(src_gdal, n_chunk, band = 1):
    """split `src_gdal` GDAL file into chunks with `n_chunk` cells squared.

    returns a list of chunked filenames.
    """
    
    o_chunks = []
    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            src_band = src_ds.GetRasterBand(band)
            gt = ds_config['geoT']
            gt = list(gt)
            gt[0] = gt[0] - (gt[1]/2)
            gt[3] = gt[3] - (gt[5]/2)
            gt = tuple(gt)

            c_n = 0
            for srcwin in gdal_yield_srcwin(src_gdal, n_chunk = n_chunk, step = n_chunk):
                this_geo_x_origin, this_geo_y_origin = utils._pixel2geo(srcwin[0], srcwin[1], gt)
                dst_gt = [this_geo_x_origin, float(gt[1]), 0.0, this_geo_y_origin, 0.0, float(gt[5])]
                band_data = src_band.ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                if not np.all(band_data == band_data[0,:]):
                    dst_config = gdal_copy_infos(ds_config)
                    dst_config['nx'] = srcwin[2]
                    dst_config['ny'] = srcwin[3]
                    dst_config['geoT'] = dst_gt
                    this_region = regions.Region().from_geo_transform(dst_gt, dst_config['nx'], dst_config['ny'])
                    o_chunk = '{}_chnk{}.tif'.format(os.path.basename(src_gdal).split('.')[0], c_n)
                    dst_fn = os.path.join(os.path.dirname(src_gdal), o_chunk)
                    o_chunks.append(dst_fn)
                    utils.gdal_write(band_data, dst_fn, dst_config)
                    c_n += 1                
        return(o_chunks)

def gdal_yield_query(src_xyz, src_gdal, out_form, band = 1):
    """query a gdal-compatible grid file with xyz data.
    out_form dictates return values

    yields out_form results
    """

    with gdal_datasource(src_gdal) as src_ds:
        if src_ds is not None:
            ds_config = gdal_infos(src_ds)
            ds_band = src_ds.GetRasterBand(band)
            ds_gt = ds_config['geoT']
            ds_nd = ds_config['ndv']
            tgrid = ds_band.ReadAsArray()
            
    for xyz in src_xyz:
        x = xyz[0]
        y = xyz[1]
        try: 
            z = xyz[2]
        except:
            z = ds_nd

        if x > ds_gt[0] and y < float(ds_gt[3]):
            xpos, ypos = utils._geo2pixel(x, y, ds_gt, node='pixel')
            try: 
                g = tgrid[ypos, xpos]
            except: g = ds_nd
            d = c = m = s = ds_nd
            if g != ds_nd:
                d = z - g
                m = z + g
                outs = []
                for i in out_form:
                    outs.append(vars()[i])
                yield(outs)

def query(src_xyz, src_gdal, out_form, band = 1):
    """query a gdal-compatible grid file with xyz data.
    out_form dictates return values

    returns array of values
    """
    
    xyzl = []
    for out_q in gdal_yield_query(src_xyz, src_gdal, out_form, band=band):
        xyzl.append(np.array(out_q))
        
    return(np.array(xyzl))
    
### End
