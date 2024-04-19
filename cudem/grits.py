### grits.py 
##
## Copyright (c) 2024 Regents of the University of Colorado
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
## filter grids using various methods
##
### Code:

import os
import sys
import shutil
import math
import traceback

import numpy as np
import scipy
from scipy.signal import fftconvolve
from scipy import interpolate

from tqdm import trange

from osgeo import gdal

from cudem import utils
from cudem import gdalfun
from cudem import regions
from cudem import factory

## ==============================================
## GRITS
## ==============================================
class Grits:
    def __init__(
            self, src_dem = None, dst_dem = None, band = 1, min_z = None, max_z = None,
            count_mask = None, weight_mask = None, uncertainty_mask = None, cache_dir = './',
            verbose = True, params = {}, **kwargs
    ):
        self.src_dem = src_dem
        self.dst_dem = dst_dem
        self.band = band
        self.min_z = utils.float_or(min_z)
        self.max_z = utils.float_or(max_z)
        self.weight_mask = weight_mask
        self.uncertainty_mask = uncertainty_mask
        self.count_mask = count_mask
        self.cache_dir = cache_dir
        self.verbose = verbose
        self.params = params
        self.kwargs = kwargs

        if self.dst_dem is None:
            self.dst_dem = '{}_filtered.{}'.format(
                utils.fn_basename2(self.src_dem), utils.fn_ext(self.src_dem)
            )            
        
    def __call__(self):
        return(self.generate())

    def init_ds(self, src_ds = None):
        self.ds_config = gdalfun.gdal_infos(src_ds)
        self.ds_band = src_ds.GetRasterBand(self.band)
        self.gt = self.ds_config['geoT']

    def generate(self):
        ## copy src_dem to dst_dem
        shutil.copyfile(self.src_dem, self.dst_dem)
        
        if self.verbose:
            utils.echo_msg('filtering {} using {}'.format(self.src_dem, self))
            
        self.run()
        self.split_by_z()
        return(self)        
        
    def run(self):
        raise(NotImplementedError)
    
    def split_by_z(self):
        """Split the filtered DEM by z-value"""
        
        if self.max_z is not None or self.min_z is not None:
            with gdalfun.gdal_datasource(self.src_dem) as src_ds:
                if src_ds is not None:
                    self.init_ds(src_ds)
                    elev_array = self.ds_band.ReadAsArray()
                    mask_array = np.zeros((self.ds_config['ny'], self.ds_config['nx']))                
                    mask_array[elev_array == self.ds_config['ndv']] = np.nan
                    if self.min_z is not None:
                        mask_array[elev_array > self.min_z] = 1
                        if self.max_z is not None:
                            mask_array[elev_array > self.max_z] = 0
                        
                    elif self.max_z is not None:
                        mask_array[elev_array < self.max_z] = 1
                        if self.min_z is not None:
                            mask_array[elev_array < self.min_z] = 0
                        
                    elev_array[mask_array == 1] = 0
                    
                    with gdalfun.gdal_datasource(self.dst_dem, update=True) as s_ds:
                        if s_ds is not None:
                            s_band = s_ds.GetRasterBand(1)
                            s_array = s_band.ReadAsArray()
                            s_array = s_array * mask_array
                            smoothed_array = s_array + elev_array
                            elev_array = None
                            s_band.WriteArray(smoothed_array)
        return(self)

    def get_outliers(self, in_array, percentile=75):
        """get the outliers from in_array based on the percentile"""

        if percentile <= 50:
            percentile = 51
            
        if percentile >= 100:
            percentile = 99

        max_percentile = percentile
        min_percentile = 100 - percentile

        perc_max = np.nanpercentile(in_array, max_percentile)
        perc_min = np.nanpercentile(in_array, min_percentile)
        iqr_p = (perc_max - perc_min) * 1.5
        upper_limit = perc_max + iqr_p
        lower_limit = perc_min - iqr_p

        return(upper_limit, lower_limit)

## ==============================================
## Blur
## ==============================================
class Blur(Grits):
    """Blur DEM values using a Gaussian Blur

    -----------
    Parameters:

    blur_factor(int) - the blur factor
    """
    
    def __init__(self, blur_factor = 1, **kwargs):
        super().__init__(**kwargs)
        self.blur_factor = blur_factor

    def np_gaussian_blur(self, in_array, size):
        """blur an array using fftconvolve from scipy.signal
        size is the blurring scale-factor.

        returns the blurred array
        """

        padded_array = np.pad(in_array, size, 'symmetric')
        x, y = np.mgrid[-size:size + 1, -size:size + 1]
        g = np.exp(-(x**2 / float(size) + y**2 / float(size)))
        g = (g / g.sum()).astype(in_array.dtype)
        in_array = None
        out_array = fftconvolve(padded_array, g, mode = 'valid')
        
        return(out_array)
        
    def run(self):
        """gaussian blur on src_dem using a smooth-factor of `sf`
        runs np_gaussian_blur(ds.Array, sf)

        generates edges with nodata...
        """
        
        status = -1
        with gdalfun.gdal_datasource(self.src_dem) as src_ds:
            if src_ds is not None:
                self.init_ds(src_ds)
                #ds_config = gdalfun.gdal_infos(src_ds)
                ## original array
                ds_array = self.ds_band.ReadAsArray(0, 0, self.ds_config['nx'], self.ds_config['ny'])
                ## copy original array as data mask
                msk_array = np.array(ds_array)
                ## set mask to 1/0
                msk_array[msk_array == self.ds_config['ndv']] = np.nan
                msk_array[~np.isnan(msk_array)] = 1
                ds_array[np.isnan(msk_array)] = 0
                smooth_array = self.np_gaussian_blur(ds_array, int(self.blur_factor))
                smooth_array = smooth_array * msk_array
                mask_array = ds_array = None
                smooth_array[np.isnan(smooth_array)] = self.ds_config['ndv']

                with gdalfun.gdal_datasource(self.dst_dem, update=True) as dst_ds:
                    dst_band = dst_ds.GetRasterBand(self.band)
                    dst_band.WriteArray(smooth_array)
                
                #status = gdalfun.gdal_write(smooth_array, self.dst_dem, self.ds_config)

        return(self.dst_dem, 0)

## ==============================================
## GMT grdfilter
## ==============================================
class GMTgrdfilter(Grits):
    """Filter a DEM through GMT's `grdfilter`; see `gmt grdfilter --help`

    -----------
    Parameters:

    filter_type(str) - The grdfilter filter type (grdfilter -F)
    dist(str) - The grdfilter distance value (grdfilter -D)
    node(str) - Either 'grid' or 'pixel'
    """
    
    def __init__(self, filter_type = 'c3s', dist='1', node='pixel', **kwargs):
        super().__init__(**kwargs)
        self.filter_type = filter_type
        self.dist = dist
        self.node = node

    def run(self):
        """filter `src_dem` using GMT grdfilter"""

        #if self.dst_dem is None:
        tmp_dst_dem = utils.make_temp_fn('{}_filtered.{}'.format(
            utils.fn_basename2(self.src_dem), utils.fn_ext(self.src_dem)
        ), self.cache_dir)
        #else:
        #    tmp_dst_dem = self.dst_dem

        ft_cmd1 = (
            'gmt grdfilter -V {} -G{}=gd:GTiff -F{} -D1{}'.format(
                self.src_dem, tmp_dst_dem, self.filter_type, self.dist, ' -rp' if self.node == 'pixel' else ''
            )
        )

        out, status = utils.run_cmd(ft_cmd1, verbose=self.verbose)

        out_array = gdalfun.gdal_get_array(tmp_dst_dem, 1)
        with gdalfun.gdal_datasource(self.dst_dem, update=True) as dst_ds:
            dst_band = dst_ds.GetRasterBand(self.band)
            dst_band.Write(out_array)            
        
        return(self.dst_dem, 0)

## ==============================================
## Outliers - filter outliers
## ==============================================
class Outliers(Grits):
    """Remove outliers from the input DEM.

    -----------
    Parameters:

    percentile(float) - the percentile to use in calculating outliers
    chunk_size(int) the moving window size in pixels
    chunk_step(int) - the moving window step in pixels
    return_mask(bool) - save the generated outlier mask
    interpolation(str) - interpolation method to use for neighborhood calculations (linear, cubic or nearest)
    aggressive(bool) - use straight percentiles instead of outliers
    multi_pass(bool) - pass through the data at multiple chunk_sizes
    """
    
    def __init__(self, chunk_size = None, chunk_step = None, max_chunk = None, max_step = None, percentile = 75, return_mask = False,
                 elevation_weight = 1, curvature_weight = 1, slope_weight = 1, tpi_weight = 1, unc_weight = 1,
                 rough_weight = 1, tri_weight = 1, aggressive = False, multipass = 1,
                 interpolation='nearest', **kwargs):
        
        super().__init__(**kwargs)
        self.chunk_size = chunk_size
        self.chunk_step = chunk_step
        self.max_chunk = max_chunk
        self.max_step = max_step
        self.percentile = utils.float_or(percentile)
        self.return_mask = return_mask
        self.elevation_weight = utils.float_or(elevation_weight, 1)
        self.curvature_weight = utils.float_or(curvature_weight, 1)
        self.slope_weight = utils.float_or(slope_weight, 1)
        self.tpi_weight = utils.float_or(tpi_weight, 1)
        self.unc_weight = utils.float_or(unc_weight, 1)
        self.rough_weight = utils.float_or(rough_weight, 1)
        self.tri_weight = utils.float_or(tri_weight, 1)
        self.aggressive = aggressive
        self.interpolation = interpolation
        self.multipass = utils.int_or(multipass, 1)

        ## setup the uncertainty data if wanted
        if self.uncertainty_mask is not None:
            self.unc_is_band = False
            self.unc_is_fn = False
            if utils.int_or(self.uncertainty_mask) is not None:
                self.unc_is_band = True
                self.uncertainty_mask = utils.int_or(self.uncertainty_mask)
            elif os.path.exists(self.uncertainty_mask):
                self.unc_is_fn = True
            else:
                self.uncertainty_mask = None

    def init_chunks(self, src_ds = None):
        src_arr, src_den = self.gdal_density(src_ds)
        self._chunks(src_arr)
        src_arr = src_den = None

    def _chunks(self, src_arr):
        n_den = self._density(src_arr)
        if round(n_den) == 1:
            n_den = .945
        
        # self.n_chunk = utils.int_or(
        #     self.chunk_size,
        #     math.ceil(
        #         (math.sqrt(src_arr.size) * (1-n_den)) *
        #         (((math.sqrt(src_arr.size) * (1-n_den)) * inc) * ((1-n_den) * .1))
        #     )
        # )
        #self.n_chunk = utils.int_or(self.chunk_size, math.ceil((math.sqrt(src_arr.size) * (1-n_den)) * n_den))
        self.n_chunk = utils.int_or(self.chunk_size, math.ceil((math.sqrt(src_arr.size * (.005 * (1-n_den))))))
        self.n_step = utils.int_or(self.chunk_step, math.ceil(self.n_chunk / 5))

        self.max_chunk = utils.int_or(self.max_chunk, math.ceil((math.sqrt(src_arr.size * (.05 * (1-n_den))))))
        #self.max_chunk = utils.int_or(self.max_chunk, math.ceil((math.sqrt(src_arr.size) * (1-n_den))))
        self.max_step = utils.int_or(self.max_step, math.ceil((self.max_chunk / 5)))
        if self.max_step > self.max_chunk:
            self.max_step = self.max_chunk
            
        # if round(n_den) < 1:
        #     self.n_chunk = utils.int_or(self.chunk_size, math.ceil((math.sqrt(src_arr.size) * (1-n_den)) * n_den))
        #     #self.n_step = utils.int_or(self.chunk_step, math.ceil(self.n_chunk / 10))
        #     #self.n_chunk = utils.int_or(self.chunk_size, math.ceil(math.sqrt(src_arr.size) * ((1-n_den) * .1)))
        #     #self.n_step = utils.int_or(self.chunk_step, math.ceil(self.n_chunk / (10 * (1-n_den))))
        #     #self.n_step = utils.int_or(self.chunk_step, math.ceil(self.n_chunk * (1-n_den)))
        
        #     #return(n_chunk, n_step)
        # else:
        #     self.n_chunk = utils.int_or(self.chunk_size, math.ceil(math.sqrt(src_arr.size) * .05))

        # if self.n_chunk < 10:
        #     self.n_chunk = self.max_chunk

        utils.echo_msg('{} {} < {} {}'.format(self.n_chunk, self.n_step, self.max_chunk, self.max_step))
        
    def _density(self, src_arr):
        nonzero = np.count_nonzero(~np.isnan(src_arr))
        dd = nonzero / src_arr.size
        return(dd)        
        
    def gdal_density(self, src_ds = None):

        src_arr, src_config = gdalfun.gdal_get_array(src_ds)
        src_arr[src_arr == src_config['ndv']] = np.nan

        return(src_arr, self._density(src_arr))
        
    def generate_mask_ds(self, src_ds = None):
        ## to hold the mask data
        self.mask_mask_fn = '{}{}'.format(utils.fn_basename2(self.src_dem), '_outliers.tif')
        mask_mask = np.zeros((src_ds.RasterYSize, src_ds.RasterXSize))
        driver = gdal.GetDriverByName('GTiff')
        self.mask_mask_ds = driver.Create(self.mask_mask_fn, self.ds_config['nx'], self.ds_config['ny'], 2, gdal.GDT_Float32,
                                          options=['COMPRESS=DEFLATE', 'PREDICTOR=1', 'TILED=YES', 'BIGTIFF=YES'])
        self.mask_mask_ds.SetGeoTransform(self.ds_config['geoT'])
        self.mask_mask_band = self.mask_mask_ds.GetRasterBand(1)
        self.mask_count_band = self.mask_mask_ds.GetRasterBand(2)
        #self.mask_weight_band = self.mask_mask_ds.GetRasterBand(3)
        self.mask_mask_band.SetNoDataValue(0)
        self.mask_mask_band.WriteArray(mask_mask)
        self.mask_count_band.WriteArray(mask_mask)
        #self.mask_weight_band.WriteArray(mask_mask)
        mask_mask = None
        #return(mask_mask_ds, mask_mask_fn)
                
    def generate_mem_ds(self, band_data = None, srcwin = None):
        ## interpolate the srcwin for neighborhood calculations                    
        tmp_band_data = band_data
        if np.all(np.isnan(tmp_band_data)):
            return(None)

        if np.any(np.isnan(band_data)):                        
            point_indices = np.nonzero(~np.isnan(tmp_band_data))
            if len(point_indices[0]):
                point_values = tmp_band_data[point_indices]
                xi, yi = np.mgrid[0:srcwin[3], 0:srcwin[2]]

                try:
                    tmp_band_data = scipy.interpolate.griddata(
                        np.transpose(point_indices), point_values,
                        (xi, yi), method=self.interpolation
                    )
                except:
                    pass

                point_values = xi = yi = None
            point_indices = None
        
        ## generate a mem datasource to feed into gdal.DEMProcessing
        dst_gt = (self.gt[0] + (srcwin[0] * self.gt[1]), self.gt[1], 0., self.gt[3] + (srcwin[1] * self.gt[5]), 0., self.gt[5])
        srcwin_config = gdalfun.gdal_set_infos(srcwin[2], srcwin[3], srcwin[2]*srcwin[3], dst_gt, self.ds_config['proj'],
                                                self.ds_band.DataType, self.ds_config['ndv'], 'GTiff', {}, 1)
        srcwin_ds = gdalfun.gdal_mem_ds(srcwin_config, name='MEM', bands=1, src_srs=None)
        srcwin_band = srcwin_ds.GetRasterBand(1)
        srcwin_band.SetNoDataValue(self.ds_config['ndv'])
        tmp_band_data[np.isnan(tmp_band_data)] = self.ds_config['ndv']
        srcwin_band.WriteArray(tmp_band_data)
        srcwin_ds.FlushCache()
        tmp_band_data = None
        
        return(srcwin_ds)
                
    def gdal_dem(self, input_ds = None, var = None):
        if var == 'curvature':
            return(self.gdal_dem_curvature(input_ds=input_ds))
        
        tmp_ = utils.make_temp_fn('gdaldem_{}.tif'.format(var), self.cache_dir)
        tmp_ds = gdal.DEMProcessing(tmp_, input_ds, var, computeEdges=True, scale=111120)

        return(tmp_ds, tmp_)
    
    def gdal_dem_curvature(self, input_ds = None):
        slp_ds, slp_fn = self.gdal_dem(input_ds=input_ds, var='slope')
        curv_ds, curv_fn = self.gdal_dem(input_ds=slp_ds, var='slope')
        slp_ds = None
        utils.remove_glob(slp_fn)
        return(curv_ds, curv_fn)
        
    def mask_outliers(
            self, src_data=None, mask_data=None, count_data=None, percentile=75, upper_only=False, src_weight=1, verbose=False
    ):
        if src_data is not None and mask_data is not None and count_data is not None:
            upper_limit, lower_limit = self.get_outliers(src_data, percentile)

            if verbose:
                utils.echo_msg('{} {}'.format(upper_limit, lower_limit))

            #count_data[:] += 1
            src_upper = src_data[src_data > upper_limit]
            if src_upper.size > 0:
                src_max = src_upper.max()
                if upper_limit != 0:
                    mask_data[(src_data > upper_limit)] = np.sqrt(
                        (np.power(mask_data[(src_data > upper_limit)], 2) +
                         np.power(src_weight * np.abs((src_upper - upper_limit) / (src_max - upper_limit)), 2))
                    )
                    #mask_data[(src_data > upper_limit)] += (src_weight * np.abs((src_upper - upper_limit) / (src_max - upper_limit)))
                    # mask_data[(src_data > upper_limit)] = np.sqrt(
                    #     (np.power(mask_data[(src_data > upper_limit)], 2) +
                    #      np.power(src_weight * np.abs((src_upper / upper_limit)), 2))
                    # )
                    count_data[(src_data > upper_limit)] += 1
                    #weight_data[(src_data > upper_limit)] += src_weight

            if not upper_only:
                src_lower = src_data[src_data < lower_limit]
                if src_lower.size > 0:
                    src_min = src_lower.min()
                    if lower_limit != 0:
                        mask_data[(src_data < lower_limit)] = np.sqrt(
                            (np.power(mask_data[(src_data < lower_limit)], 2) +
                             np.power(src_weight * np.abs((src_lower - lower_limit) / (src_min - lower_limit)), 2))
                        )
                        #mask_data[(src_data < lower_limit)] += (src_weight * np.abs((src_lower - lower_limit) / (src_min - lower_limit)))
                        # mask_data[(src_data < lower_limit)] = np.sqrt(
                        #     (np.power(mask_data[(src_data < lower_limit)], 2) +
                        #      np.power(src_weight * np.abs((src_lower / lower_limit)), 2))
                        # )
                        count_data[(src_data < lower_limit)] += 1
                        #weight_data[(src_data < lower_limit)] += src_weight

    def mask_gdal_dem_outliers(
            self, srcwin_ds = None, band_data = None, mask_mask_data = None, mask_count_data = None,
            var = None, percentile = 75, upper_only = False, src_weight = None
    ):
        """apply gdaldem outliers"""

        tmp_ds, tmp_fn = self.gdal_dem(input_ds=srcwin_ds, var=var)
        tmp_data = tmp_ds.GetRasterBand(1).ReadAsArray()
        tmp_data[((np.isnan(band_data)) | (tmp_data == self.ds_config['ndv']))] = np.nan                
        self.mask_outliers(
            src_data=tmp_data, mask_data=mask_mask_data, count_data=mask_count_data,
            percentile=percentile, upper_only=upper_only, src_weight=src_weight
        )
        tmp_ds = tmp_data = None
        utils.remove_glob(tmp_fn)
        return(0)
                    
    def run(self):
        """Scan a src_gdal file for outliers and remove them."""
            
        with gdalfun.gdal_datasource(
                self.src_dem, update=False#update=True if self.dst_dem is None else False
        ) as src_ds:
            if src_ds is not None:
                self.init_ds(src_ds=src_ds)
                self.generate_mask_ds(src_ds=src_ds)
                self.init_chunks(src_ds=src_ds)
                src_config = gdalfun.gdal_infos(src_ds)
                
                # uncertainty ds
                unc_band = None
                if self.uncertainty_mask is not None:
                    if self.unc_is_fn:
                        unc_ds = gdal.Open(self.uncertainty_mask)
                        unc_band = unc_ds.GetRasterBand(1)
                    elif self.unc_is_band:
                        unc_band = src_ds.GetRasterBand(self.uncertainty_mask)

                chunks_it = np.linspace(self.n_chunk, self.max_chunk, self.multipass)
                steps_it = np.linspace(self.n_step, self.max_step, self.multipass)
                
                for n, chunk in enumerate(chunks_it):
                    step = steps_it[n]
                    n+=1
                    
                    # self.elevation_weight *= (1/n**2)
                    # self.curvature_weight *= (1/n)
                    # self.slope_weight *= (1/n)
                    # self.rough_weight *= (1/n)
                    # self.tri_weight *= (1/n)
                    # self.tpi_weight *= (1/n)

                    #utils.echo_msg('{} {} {}'.format(self.elevation_weight, self.curvature_weight, self.slope_weight))
                    #utils.echo_msg('{} {} {}'.format(self.rough_weight, self.tri_weight, self.tpi_weight))
                    for srcwin in utils.yield_srcwin(
                            (src_ds.RasterYSize, src_ds.RasterXSize), n_chunk=chunk,
                            step=step, verbose=self.verbose, msg='scanning for outliers ({})'.format(self.percentile)
                    ):
                        ## buffer based on density
                        band_data = self.ds_band.ReadAsArray(*srcwin)
                        band_data[band_data == self.ds_config['ndv']] = np.nan
                        if np.all(np.isnan(band_data)):
                            band_data = None
                            continue

                        ## read in the mask data for the srcwin
                        mask_mask_data = self.mask_mask_band.ReadAsArray(*srcwin) # read in the mask id data
                        mask_count_data = self.mask_count_band.ReadAsArray(*srcwin) # read in the count data
                        #mask_weight_data = self.mask_weight_band.ReadAsArray(*srcwin) # read in the weight data

                        ## apply the elevation outliers
                        self.mask_outliers(
                            src_data=band_data, mask_data=mask_mask_data, count_data=mask_count_data,
                            percentile=self.percentile, src_weight=self.elevation_weight
                        )

                        ## apply uncertainty outliers
                        if unc_band is not None:
                            unc_data = unc_band.ReadAsArray(*srcwin)
                            unc_data[(np.isnan(band_data) | (unc_data == self.ds_config['ndv']) | (unc_data == 0))] = np.nan
                            self.mask_outliers(
                                src_data=unc_data, mask_data=mask_mask_data, count_data=mask_count_data,
                                percentile=self.percentile, upper_only=True, src_weight=self.unc_weight
                            )
                            unc_data = None

                        ## generate a mem datasource to feed into gdal.DEMProcessing
                        srcwin_ds = self.generate_mem_ds(band_data=band_data, srcwin=srcwin)
                        if srcwin_ds is None:
                            band_data = None
                            continue

                        # ## apply slope outliers
                        # self.mask_gdal_dem_outliers(srcwin_ds=srcwin_ds, band_data=band_data, mask_mask_data=mask_mask_data,
                        #                             mask_count_data=mask_count_data, percentile=75, upper_only=True,
                        #                             src_weight=self.slope_weight, var='slope')

                        ## apply curvature outliers
                        self.mask_gdal_dem_outliers(srcwin_ds=srcwin_ds, band_data=band_data, mask_mask_data=mask_mask_data,
                                                    mask_count_data=mask_count_data, percentile=self.percentile, upper_only=True,
                                                    src_weight=self.curvature_weight, var='curvature')

                        # ## apply roughness outliers
                        # self.mask_gdal_dem_outliers(srcwin_ds=srcwin_ds, band_data=band_data, mask_mask_data=mask_mask_data, mask_weight_data=mask_weight_data,
                        #                             mask_count_data=mask_count_data, percentile=75, upper_only=True,
                        #                             src_weight=self.rough_weight, var='roughness')

                        ## apply tri outliers
                        self.mask_gdal_dem_outliers(srcwin_ds=srcwin_ds, band_data=band_data, mask_mask_data=mask_mask_data,
                                                    mask_count_data=mask_count_data, percentile=self.percentile, upper_only=True,
                                                    src_weight=self.tri_weight, var='TRI')

                        ## apply TPI outliers
                        self.mask_gdal_dem_outliers(srcwin_ds=srcwin_ds, band_data=band_data, mask_mask_data=mask_mask_data,
                                                    mask_count_data=mask_count_data, percentile=self.percentile, upper_only=False,
                                                    src_weight=self.tpi_weight, var='TPI')

                        srcwin_ds = None

                        ## write the mask data to file
                        self.mask_mask_band.WriteArray(mask_mask_data, srcwin[0], srcwin[1])
                        self.mask_count_band.WriteArray(mask_count_data, srcwin[0], srcwin[1])
                        #self.mask_weight_band.WriteArray(mask_weight_data, srcwin[0], srcwin[1])
                        band_data = mask_mask_data = mask_count_data = None

                mask_mask_data = self.mask_mask_band.ReadAsArray()
                mask_mask_data[mask_mask_data == 0] = np.nan
                mask_count_data = self.mask_count_band.ReadAsArray()
                mask_count_data[mask_count_data == 0] = np.nan
                #mask_weight_data = self.mask_weight_band.ReadAsArray()
                #mask_weight_data[mask_weight_data == 0] = 1
                #mask_mask_data *= mask_count_data
                #mask_mask_data /= mask_count_data

                #mask_weight_data = mask_weight_data / mask_count_data
                #mask_mask_data = (mask_mask_data / mask_weight_data) / mask_count_data

                #self.mask_mask_band.WriteArray(mask_mask_data)
                #self.mask_weight_band.WriteArray(mask_weight_data)
                
                count_upper_limit = np.nanpercentile(mask_count_data, self.percentile)
                if self.aggressive:
                    mask_upper_limit = np.nanpercentile(mask_mask_data, self.percentile)
                    mask_lower_limit = None
                    #count_upper_limit = np.nanpercentile(mask_count_data, self.percentile)
                else:
                    mask_upper_limit, mask_lower_limit = self.get_outliers(mask_mask_data, self.percentile)
                    #count_upper_limit, count_lower_limit = self.get_outliers(mask_count_data, self.percentile)
                    
                outlier_mask = ((mask_mask_data > mask_upper_limit) & (mask_count_data >= count_upper_limit))
                #outlier_mask = ((mask_mask_data > mask_upper_limit))
                    
                if self.verbose:
                    utils.echo_msg('outliers: {} {}'.format(mask_upper_limit, mask_lower_limit))
                    utils.echo_msg('counts: {}'.format(count_upper_limit))                    
                        
                src_data = self.ds_band.ReadAsArray()
                src_data[outlier_mask] = self.ds_config['ndv']
                with gdalfun.gdal_datasource(self.dst_dem, update=True) as dst_ds:
                    dst_band = dst_ds.GetRasterBand(self.band)
                    dst_band.WriteArray(src_data)

                src_data = None
                if self.verbose:
                    utils.echo_msg('removed {} outliers{}.'.format(
                        np.count_nonzero(outlier_mask), ' (aggressive)' if self.aggressive else ''
                    ))
                    
                self.mask_mask_ds = dst_ds = unc_ds = mask_mask_data = mask_count_data = None
                if not self.return_mask:
                    utils.remove_glob(self.mask_mask_fn)
                    
                return(self.dst_dem, 0)
            else:
                return(None, -1)

## ==============================================
## Flats - filter out flat areas
## ============================================
class Flats(Grits):
    """Remove flat areas from the input DEM

    -----------
    Parameters:

    size_threshold(int) - the minimum flat area in pixels to remove
    n_chunk(int) - the moving window size in pixels
    """
    
    def __init__(self, size_threshold = None, n_chunk = None, **kwargs):
        super().__init__(**kwargs)
        self.size_threshold = size_threshold
        self.n_chunk = n_chunk
        
    def run(self):
        """Discover and remove flat zones"""
        
        count = 0
        with gdalfun.gdal_datasource(self.src_dem) as src_ds:
            if src_ds is not None:
                self.init_ds(src_ds)
                if self.n_chunk is None:
                    self.n_chunk = self.ds_config['nb']

                for srcwin in gdalfun.gdal_yield_srcwin(src_ds, n_chunk=self.n_chunk, step=self.n_chunk, verbose=True):
                    src_arr = self.ds_band.ReadAsArray(*srcwin).astype(float)
                    #src_arr[src_arr == src_config['ndv']] = np.nan

                    uv, uv_counts = np.unique(src_arr, return_counts=True)
                    if self.size_threshold is None:
                        _size_threshold = self.get_outliers(uv_counts, 99)[0]
                    else:
                        _size_threshold = self.size_threshold

                    uv_ = uv[uv_counts > _size_threshold]
                    if len(uv_) > 0:
                        for i in trange(
                                0,
                                len(uv_),
                                desc='{}: removing flattened data greater than {} cells'.format(
                                    os.path.basename(sys.argv[0]), _size_threshold
                                ),
                                leave=self.verbose
                        ):
                            mask = src_arr == uv_[i]
                            count += np.count_nonzero(mask)
                            src_arr[mask] = self.ds_config['ndv']

                    with gdalfun.gdal_datasource(self.dst_dem, update=True) as dst_ds:
                        dst_band = dst_ds.GetRasterBand(self.band)
                        dst_band.WriteArray(src_arr, srcwin[0], srcwin[1])

        utils.echo_msg('removed {} flats.'.format(count))
        return(self.dst_dem, 0)

class GritsFactory(factory.CUDEMFactory):
    _modules = {
        'blur': {'call': Blur},
        'grdfilter': {'call': GMTgrdfilter},
        'outliers': {'call': Outliers},
        'flats': {'call': Flats},
    }
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

## ==============================================
## Command-line Interface (CLI)
## $ grits
##
## grits cli
## ==============================================
grits_cli_usage = """{cmd}

usage: {cmd} [ -hvCMNUWX [ args ] ] DEM ...

Options:

  -M, --module\t\t\tDesired grits MODULE and options. (see available Modules below)
\t\t\t\tWhere MODULE is module[:mod_opt=mod_val[:mod_opt1=mod_val1[:...]]]

  -N, --min_z\t\t\tMinimum z value (filter data above this value)
  -X, --max_z\t\t\tMaximum z value (filter data below this value)
  -U, --uncertainty_mask\tAn associated uncertainty raster or band number
  -W, --weight_mask\t\tAn associated weight raster or band number
  -C, --count_mask\t\tAn associated count raster or band number

  --help\t\t\tPrint the usage text
  --modules\t\t\tDisplay the module descriptions and usage
  --version\t\t\tPrint the version information

Supported GRITS modules (see grits --modules <module-name> for more info): 
  {d_formats}

Examples:
  % {cmd} input_dem.tif -M blur
  % {cmd} input_dem.tif --uncertainty_mask input_dem_u.tif --max_z 0 -M outliers:percentile=65
""".format(cmd=os.path.basename(sys.argv[0]),
           d_formats=factory._cudem_module_short_desc(GritsFactory._modules))
        
#if __name__ == '__main__':
def grits_cli(argv = sys.argv):
    i = 1
    src_dem = None
    dst_dem = None
    wg_user = None
    module = None
    min_z = None
    max_z = None
    uncertainty_mask = None
    weight_mask = None
    count_mask = None
    
    while i < len(argv):
        arg = argv[i]
        if arg == '--module' or arg == '-M':
            module = str(argv[i + 1])
            i += 1
        elif arg[:2] == '-M':
            module = str(arg[2:])
        elif arg == '--min_z' or arg == '-N':
            min_z = utils.float_or(argv[i + 1])
            i += 1
        elif arg[:2] == '-N':
            min_z = utils.float_or(arg[2:])
        elif arg == '--max_z' or arg == '-X':
            max_z = utils.float_or(argv[i + 1])
            i += 1
        elif arg[:2] == 'X':
            max_z = utils.float_or(arg[2:])            
        elif arg == '--uncertainty_mask' or arg == '-U':
            uncertainty_mask = argv[i + 1]
            i += 1
        elif arg[:2] == '-U':
            uncertainty_mask = arg[2:]
        elif arg == '--weight_mask' or arg == '-@':
            weight_mask = argv[i + 1]
            i += 1
        elif arg[:2] == '-W':
            weight_mask = arg[2:]
        elif arg == '--count_mask' or arg == '-C':
            count_mask = argv[i + 1]
            i += 1
        elif arg[:2] == '-C':
            count_mask = arg[2:]
        
        elif arg == '--modules' or arg == '-m':
            factory.echo_modules(GritsFactory._modules, None if i+1 >= len(argv) else sys.argv[i+1])
            sys.exit(0)            
        elif arg == '--help' or arg == '-h':
            sys.stderr.write(grits_cli_usage)
            sys.exit(0)
        elif arg == '--version' or arg == '-v':
            sys.stdout.write('{}\n'.format(cudem.__version__))
            sys.exit(0)
        elif arg[0] == '-':
            sys.stdout.write(grits_cli_usage)
            utils.echo_error_msg('{} is not a valid grits cli switch'.format(arg))
            sys.exit(0)
        else:
            wg_user = arg
        i += 1

    if module is None:
        sys.stderr.write(grits_cli_usage)
        utils.echo_error_msg(
            '''must specify a grits -M module.'''
        )
        sys.exit(-1)

    if module.split(':')[0] not in GritsFactory()._modules.keys():
        utils.echo_error_msg(
            '''{} is not a valid grits module, available modules are: {}'''.format(
                module.split(':')[0], factory._cudem_module_short_desc(GritsFactory._modules)
            )
        )
        sys.exit(-1)
        
    ## ==============================================
    ## load the user wg json and run grits with that.
    ## ==============================================
    if wg_user is not None:
        if os.path.exists(wg_user):
            try:
                with open(wg_user, 'r') as wgj:
                    wg = json.load(wgj)
                    if wg['src_region'] is not None:
                        wg['src_region'] = regions.Region().from_list(
                            wg['src_region']
                        )

                    this_waffle = waffles.WaffleFactory(**wg).acquire()
                    this_waffle.mask = True
                    this_waffle.clobber = False

                    if not this_waffle.valid_p():
                        this_waffle.generate()

                    src_dem = this_waffle.fn
            except:
                src_dem = wg_user
        else:
            sys.stderr.write(grits_cli_usage)
            utils.echo_error_msg(
                'specified waffles config file/DEM does not exist, {}'.format(wg_user)
            )
            sys.exit(-1)
    else:
        sys.stderr.write(grits_cli_usage)
        utils.echo_error_msg(
            'you must supply a waffles config file or an existing DEM; see waffles --help for more information.'
        )
        sys.exit(-1)

    this_grits = GritsFactory(
        mod=module, src_dem=src_dem, min_z=min_z, max_z=max_z, uncertainty_mask=uncertainty_mask, weight_mask=weight_mask,
        count_mask=count_mask
    )
    if this_grits is not None:
        try:
            this_grits_module = this_grits._acquire_module()
            if this_grits_module is not None:
                out_dem = this_grits_module()
                utils.echo_msg('filtered {}'.format(out_dem))
            else:
                utils.echo_error_msg('could not acquire grits module {}'.format(module))
        except KeyboardInterrupt:
            utils.echo_error_msg('Killed by user')
        except Exception as e:
            utils.echo_error_msg(e)
            print(traceback.format_exc())
        
### End
