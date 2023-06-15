### waffles.py
##
## Copyright (c) 2010 - 2023 Regents of the University of Colorado
##
## waffles.py is part of CUDEM
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
## Generate DEMs from a variety of data sources.
## Use CLI command 'waffles'
##
## Supported input datatypes include:
## datalist, las/laz, gdal, bag, ogr, xyz, mbs, fetches
## see cudem.dlim for more information on supported datasets
##
## Supported gridding modules include:
## gmt-surface (GMT), gmt-triangulate (GMT), gmt-nearneighbor (GMT), mbgrid (MB-System), IDW (CUDEM), num (CUDEM/GMT),
## coastline (CUDEM), cudem (CUDEM), stacks (CUDEM), gdal-inv-dst (GDAL), gdal-linear (GDAL), gdal-average (GDAL),
## gdal-nearest (GDAL), scipy (SCIPY)
##
## GMT, GDAL and MB-System are required for full functionality.
##
## Process all input data through cudem.dlim first, minimally, using cudem.dlim.init_data(list-of-data).
##
## Data will be processed through cudem.dlim._stacks prior to any interpolation. This will produce a weighted-mean (or weight-superceded)
## grid of the data to use for interpolation. See cudem.dlim for more information.
##
## The interpolated DEM will be post-processed through Waffle._process() where it will be filtered, resampled, clipped, set limits,
## cut to final output size, filled with metadata. Optionally, if want_uncertainty is set, _process() will also calculate the
## interpolation uncertainty.
##
### Code:

import sys
import os
import math
import json
import time
import glob
import traceback
from tqdm import tqdm

import numpy as np
from scipy import interpolate
from scipy import spatial
from scipy import ndimage
import threading
import multiprocessing as mp
try:
   import Queue as queue
except: import queue as queue

from osgeo import gdal
from osgeo import ogr
from osgeo import osr

import cudem
from cudem import dlim
from cudem import regions
from cudem import utils
from cudem import xyzfun
from cudem import gdalfun
from cudem import vdatums
from cudem import factory
from cudem import fetches
        
## Data cache directory, hold temp data, fetch data, etc here.
waffles_cache = utils.cudem_cache()

def waffles_filter(src_dem, dst_dem, fltr = 1, fltr_val = None, split_val = None, mask = None, node = 'pixel'):
    """filter raster using smoothing factor `fltr`; optionally
    only smooth bathymetry (sub-zero) using a split_val of 0.

    -----------
    Parameters:
    fltr (int): the filter to use, 1, 2 or 3
    flt_val (varies): the filter value, varies by filter.
    split_val (float): an elevation value (only filter below this value)
    """

    def grdfilter(src_dem, dst_dem, dist='c3s', node='pixel', verbose=False):
        """filter `src_dem` using GMT grdfilter"""

        ft_cmd1 = ('gmt grdfilter -V {} -G{} -F{} -D1{}'.format(src_dem, dst_dem, dist, ' -rp' if node == 'pixel' else ''))
        return(utils.run_cmd(ft_cmd1, verbose=verbose))
    
    utils.echo_msg('filtering DEM {} using {}@{}'.format(src_dem, fltr, fltr_val))
    if os.path.exists(src_dem):
        ## Filter the DEM (1=blur, 2=grdfilter, 3=outliers)
        if int(fltr) == 1:
            out, status = gdalfun.gdal_blur(
                src_dem, 'tmp_fltr.tif', fltr_val if fltr_val is not None else 10)
        elif int(fltr) == 2:
            out, status = grdfilter(
                src_dem, 'tmp_fltr.tif=gd:GTiff', dist = fltr_val if fltr_val is not None else '1s',
                node = node, verbose = True)
        elif int(fltr) == 3:
            out, status = gdalfun.filter_outliers(
                src_dem, 'tmp_fltr.tif', agg_level=fltr_val if fltr_val is not None else 5, replace=True)
        else:
            utils.echo_warning_msg('invalid filter {}, defaulting to blur'.format(fltr))
            out, status = gdalfun.gdal_blur(src_dem, 'tmp_fltr.tif', fltr_val if utils.int_or(fltr_val) is not None else 10)
            
        if status != 0:
            return(status)

        ## Split the filtered DEM by z-value
        split_val = utils.float_or(split_val)
        if split_val is not None:
            with gdalfun.gdal_datasource(src_dem) as src_ds:
                if src_ds is not None:
                    ds_config = gdalfun.gdal_infos(src_ds)
                    elev_array = src_ds.GetRasterBand(1).ReadAsArray()
                    mask_array = np.zeros((ds_config['ny'], ds_config['nx']))                
                    mask_array[elev_array == ds_config['ndv']] = 0
                    mask_array[elev_array < split_val] = 1
                    elev_array[elev_array < split_val] = 0

                    with gdalfun.gdal_datasource('tmp_fltr.tif') as s_ds:
                        if s_ds is not None:
                            s_array = s_ds.GetRasterBand(1).ReadAsArray()
                            s_array = s_array * mask_array
                            smoothed_array = s_array + elev_array
                            elev_array = None
                            gdalfun.gdal_write(smoothed_array, dst_dem, ds_config)

                    utils.remove_glob('tmp_fltr.tif')
        else:
            os.replace('tmp_fltr.tif', dst_dem)
        return(0)
    
    else:
        return(-1)

## ==============================================
##
## WAFFLES
##
## TODO
## add upper/lower limits to waffle class and
## remove from individual waffle modules.
##
## ==============================================
class Waffle:
    """Representing a WAFFLES DEM/MODULE.
    Specific Gridding modules are sub-classes of this class.
    See WaffleFactory for module specifications and generation.

    -----------
    Procedures:
      yield_xyz() - yield the xyz data from self.data
      dump_xyz() - dump the xyz data from self.data to port
      run() - run the WAFFLES module (function set via module sub-class)
      generate() - run and process the WAFFLES module
    """
    
    def __init__(self, data: list = [], src_region: regions.Region = None, inc: str = None, xinc: str = None, yinc: str = None,
                 xsize: int = None, ysize: int = None, name: str = 'waffles_dem', node: str = 'pixel', fmt: str = 'GTiff',
                 extend: int = 0, extend_proc: float = 0, want_weight: bool = False, want_uncertainty: bool = False, fltr: list = [],
                 sample: str = 'bilinear', xsample: str = None, ysample: str = None, clip: str = None, chunk: int = None,
                 dst_srs: str = None, srs_transform: bool = False, verbose: bool = False, archive: bool = False, want_mask: bool = False,
                 keep_auxiliary: bool = False, want_sm: bool = False, clobber: bool = True, ndv: float = -9999, block: bool = False,
                 cache_dir: str = waffles_cache, supercede: bool = False, upper_limit: float = None, lower_limit: float = None,
                 want_stack: bool = True, params: dict = {}):
        self.params = params # the factory parameters
        self.data = data # list of data paths/fetches modules to grid
        self.datalist = None # the datalist which holds the processed datasets
        self.region = src_region # the region to grid
        self.src_region = src_region # the region to grid
        self.inc = inc # the gridding increments [xinc, yinc]
        self.xinc = xinc # the x/lon gridding increment
        self.yinc = yinc # the y/lat gridding increment
        self.sample = sample # the gdal sample algorithm to use when needed
        self.xsample = xsample # the x/lon increment to sample the output dem
        self.ysample = ysample # the y/lat increment to sample the output dem
        self.name = name # the output dem basename
        self.node = node # the grid node method, either 'grid' or 'pixel'
        self.fmt = fmt # the gdal-compatible output dem file format
        self.extend = extend # extend the dem region by this many pixels
        self.extend_proc = extend_proc # extend the dem processing region by this percentage
        self.want_weight = want_weight # use weights, either None or 1
        self.want_uncertainty = want_uncertainty # apply/calculate uncertainty
        self.fltr = fltr # a list of filters (see waffles_filter for options)
        self.clip = clip # ogr compatible vector file or keyword module to clip output dem
        self.chunk = chunk # process the dem in this many chunks
        self.dst_srs = dst_srs # the output dem projection
        self.srs_transform = srs_transform # transform data to the dst_srs
        self.archive = archive # archive the data used in this dem
        self.want_mask = want_mask # mask the incoming datalist
        self.supercede = supercede # higher weighted data supercedes lower weighted data
        self.upper_limit = utils.float_or(upper_limit)
        self.lower_limit = utils.float_or(lower_limit)
        self.keep_auxiliary = keep_auxiliary
        self.clobber = clobber # clobber the output dem file
        self.verbose = verbose # increase verbosity
        self.cache_dir = cache_dir # directory path to store cahced data
        self.ndv = ndv # no data value for the dem
        self.block = block # block the data (defunct)
        self.block_t = None # block the data (defunct)
        self.ogr_ds = None # datasets as an ogr object
        self.data_ = data # store data paths here, self.data gets processed to dlim datasets
        self.fn = '{}.tif'.format(self.name) # output dem filename
        self.want_sm = want_sm # generate spatial metadata
        self.aux_dems = [] # list of auxiliary dems fns
        self.want_stack = want_stack # generate the stacked rasters
        self.stack = None # multi-banded stacked raster from cudem.dlim
        self.stack_ds = None # the stacked raster as a dlim dataset object
        # if not self.params:
        #     self.params['kwargs'] = self.__dict__.copy()
        #     self.params['mod'] = 'Unkownn'
        #     self.params['mod_name'] = 'Unknown'
        #     self.params['mod_args'] = {}
            
        #factory._set_params(self) # set up the default factory parameters
        
    def __str__(self):
        return('<Waffles: {}>'.format(self.name))
    
    def __repr__(self):
        return('<Waffles: {}>'.format(self.name))
    
    def initialize(self):
        #factory._set_mod_params(self, mf=WaffleFactory) # set the module arguments, if any
        if self.verbose:
            utils.echo_msg('initializing waffles module < \033[1m{}\033[m >'.format(self.params['mod']))
            
        self.fn = '{}.{}'.format(self.name, gdalfun.gdal_fext(self.fmt)) # output dem filename
        self.gc = utils.config_check() # cudem config file holding foriegn programs and versions
        self._init_regions() # initialize regions
        self._init_incs() # initialize increments
        if self.want_stack:
            self._init_data(set_incs=True) # initialize data, setting set_incs to True will force dlim to process the data to the set increments
            
        self.xcount, self.ycount, self.dst_gt = self.p_region.geo_transform(x_inc=self.xinc, y_inc=self.yinc, node='grid')
        self.ds_config = gdalfun.gdal_set_infos(
            self.xcount, self.ycount, (self.xcount*self.ycount), self.dst_gt, gdalfun.osr_wkt(self.dst_srs),
            gdal.GDT_Float32, self.ndv, self.fmt, None, None
        )
        #self.cache_dir = os.path.join(os.path.dirname(self.name), '.cudem')
        return(self)
    
    def __call__(self):
        self.initialize()
        return(self.generate())
    
    def _init_regions(self):
        """Initialize and set regions"""
        
        if isinstance(self.region, list):
            self.region = regions.Region().from_list(self.region)
        elif not isinstance(self.region, regions.Region):
            raise ValueError('could not parse region')
        
        if self.node == 'grid':
            self.region = self.region.buffer(x_bv=self.xinc*.5, y_bv=self.yinc*.5)
            
        self.d_region = self._dist_region()
        self.p_region = self._proc_region()
        self.c_region = self._coast_region()
        self.ps_region = self.p_region.copy()
        self.ps_region = self.ps_region.buffer(
            x_bv=self.xinc*-.5, y_bv=self.yinc*-.5, x_inc=self.xinc, y_inc=self.yinc
        )

        if self.verbose:
            utils.echo_msg('input region: {}'.format(self.region))
            utils.echo_msg('distribution region: {}'.format(self.d_region))
            utils.echo_msg('processing region: {}'.format(self.p_region))
        
    def _init_data(self, set_incs=False):
        """Initialize the data for processing
        parses data paths to dlim dataset objects.

        set `set_incs` to True to block/sample datasets to given increment
        this function sets `self.data` to a list of dataset objects.
        """
        
        self.data = dlim.init_data(self.data, region=self.p_region, src_srs=None, dst_srs=self.dst_srs,
                                   xy_inc=(self.xinc, self.yinc), sample_alg=self.sample, want_weight=self.want_weight,
                                   want_uncertainty=self.want_uncertainty, want_verbose=self.verbose, want_mask=self.want_mask,
                                   invert_region=False, cache_dir=self.cache_dir)
        self.data.initialize()
        if not self.want_weight:
            self.data.weight = None
            
        if not self.want_uncertainty:
            self.data.uncertainty = None

    def _init_incs(self):
        self.xinc = utils.str2inc(self.xinc)
        self.yinc = utils.str2inc(self.yinc)
        self.xsample = utils.str2inc(self.xsample)
        self.ysample = utils.str2inc(self.ysample)
        
    def _coast_region(self):
        """processing region (extended by self.extend_proc)"""

        cr = self.d_region.copy()
        return(cr.buffer(pct=self.extend_proc, x_inc=self.xinc, y_inc=self.yinc))
    
    def _proc_region(self):
        """processing region (extended by percentage self.extend_proc)"""

        pr = self.d_region.copy()
        return(pr.buffer(pct=self.extend_proc, x_inc=self.xinc, y_inc=self.yinc))
    
    def _dist_region(self):
        """distribution region (extended by self.extend)."""
        
        dr = self.region.copy()
        if self.xsample is None and self.ysample is None:
            return(
                dr.buffer(
                    x_bv=(self.xinc*self.extend),
                    y_bv=(self.yinc*self.extend)
                )
            )
        else:
            return(
                dr.buffer(
                    x_bv=(self.xsample*self.extend),
                    y_bv=(self.ysample*self.extend)
                )
            )
            
    def dump_xyz(self, dst_port=sys.stdout, encode=False):
        """dump the stacked xyz data to dst_port

        use this to dump data into a foreign cli program, such as GMT.
        """

        for xyz in self.stack_ds.yield_xyz():
            xyz.dump(
                include_w = self.want_weight,
                include_u = self.want_uncertainty,
                dst_port=dst_port,
                encode=encode,
            )
    
    def generate(self):
        """run and process the WAFFLES module"""

        if os.path.exists(self.fn):
            if not self.clobber:
                utils.echo_warning_msg(
                    'DEM {} already exists, skipping...'.format(self.fn)
                )
                return(self)
        else:
            if not os.path.exists(os.path.dirname(self.fn)):
                try:
                    os.makedirs(os.path.dirname(self.fn))
                except: pass
            
        ## Generate in Chunks of self.chunk by self.chunk
        if self.chunk is not None:
            #xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.xinc, y_inc=self.yinc, node='grid')
            chunks = []
            for srcwin in utils.yield_srcwin((self.ycount, self.xcount), self.chunk, verbose=self.verbose):
                this_geo_x_origin, this_geo_y_origin = utils._pixel2geo(srcwin[0], srcwin[1], self.dst_gt)
                this_geo_x_end, this_geo_y_end = utils._pixel2geo(srcwin[0]+srcwin[2], srcwin[1]+srcwin[3], self.dst_gt)
                this_gt = [this_geo_x_origin, float(self.dst_gt[1]), 0.0, this_geo_y_origin, 0.0, float(self.dst_gt[5])]
                #this_region = self.region.copy()
                this_region = regions.Region()
                this_region.from_geo_transform(geo_transform=this_gt, x_count=srcwin[2], y_count=srcwin[3])
                this_region.buffer(pct=10, x_inc = self.xinc, y_inc = self.yinc)
                ## update for **mod_args
                this_params = self.params.copy()
                this_params['kwargs']['src_region'] = this_region
                this_params['kwargs']['chunk'] = None
                this_params['kwargs']['name'] = utils.append_fn('_chunk', this_region, self.xinc, high_res=True)
                #this_params['kwargs']['name'] = os.path.join(self.cache_dir, utils.append_fn('_chunk', this_region, self.xinc, high_res=True))
                this_waffle = WaffleFactory().load_parameter_dict(this_params)
                this_waffle_module = this_waffle._acquire_module()
                this_waffle_module.initialize()
                this_waffle_module.generate()
                if this_waffle_module.valid_p():
                    chunks.append(this_waffle_module.fn)

            ## todo combine aux_dems
            if len(chunks) > 0:
                g = gdal.Warp(self.fn, chunks, format='GTiff', resampleAlg='cubicspline',
                              options=["COMPRESS=LZW", "TILED=YES"])
                g = None
                
            utils.remove_glob(*chunks)
        else:
            ## stack the data and run the waffles module
            if self.want_stack:
                stack_name = '{}_stack'.format(os.path.basename(self.name))
                mask_name = '{}_msk'.format(stack_name)
                mask_fn = '{}.{}'.format(os.path.join(self.cache_dir, mask_name), gdalfun.gdal_fext(self.fmt))
                num_threads = 8
                # try:
                #     sk = dlim.stacks_ds(self.data, n_threads=num_threads, out_name=os.path.join(self.cache_dir, stack_name),
                #                         supercede=self.supercede, want_mask=self.want_mask)
                #     sk.daemon = True
                
                #     sk.start()
                #     sk.join()                
                # except (KeyboardInterrupt, SystemExit):
                #     utils.echo_error_msg('user breakage...please wait while fetches exits.')
                #     stop_threads = True
                #     while not sk.arr_q.empty():
                #         try:
                #             sk.arr_q.get(False)
                #         except Empty:
                #             continue
                        
                #         sk.arr_q.task_done()                        

                # self.stack = sk.out_file
                if not self.clobber and os.path.exists(os.path.join(self.cache_dir, '{}.{}'.format(stack_name, gdalfun.gdal_fext('GTiff')))):
                    self.stack = os.path.join(self.cache_dir, '{}.{}'.format(stack_name, gdalfun.gdal_fext('GTiff')))
                else:
                    self.stack = self.data._stacks(out_name=os.path.join(self.cache_dir, stack_name),
                                                   supercede=self.supercede, want_mask=self.want_mask or self.want_sm)
                    
                self.stack_ds = dlim.GDALFile(fn=self.stack, band_no=1, weight_mask=3, uncertainty_mask=4,
                                              data_format=200, src_srs=self.dst_srs, dst_srs=self.dst_srs, x_inc=self.xinc,
                                              y_inc=self.yinc, src_region=self.p_region, weight=1, verbose=self.verbose).initialize()

                if self.keep_auxiliary:
                    self.aux_dems.append(self.stack)

                #if self.want_mask or self.want_sm:
                #    self.aux_dems.append('{}.{}'.format(os.path.join(self.cache_dir, mask_name), gdalfun.gdal_fext(self.fmt)))
                        
            self.run()
            
            ## post-process the DEM(s)
            waffle_dem = WaffleDEM(self.fn, cache_dir=self.cache_dir, verbose=self.verbose).initialize()
            if waffle_dem.valid_p():
                waffle_dem.process(ndv=self.ndv, xsample=self.xsample, ysample=self.ysample, region=self.d_region, clip_str=self.clip,
                                   node=self.node, upper_limit=self.upper_limit, lower_limit=self.lower_limit, dst_srs=self.dst_srs,
                                   dst_fmt=self.fmt)
            #else:
            #    return(None)

            ## post-process the mask
            if self.want_mask or self.want_sm:
                mask_dem = WaffleDEM(mask_fn, cache_dir=self.cache_dir, verbose=self.verbose).initialize()
                if mask_dem.valid_p():
                    mask_dem.process(ndv=0, xsample=self.xsample, ysample=self.ysample, region=self.d_region, clip_str=self.clip,
                                     node=self.node, dst_srs=self.dst_srs, dst_fmt=self.fmt, set_metadata=False,
                                     dst_dir=os.path.dirname(self.fn) if self.want_mask else None)

                    if self.want_sm:
                        with gdalfun.gdal_datasource(mask_dem.fn) as msk_ds:
                            sm_layer, sm_fmt = gdalfun.ogr_polygonize_multibands(msk_ds)

                        sm_files = glob.glob('{}.*'.format(sm_layer))
                        #sm_files = glob.glob('{}_sm.*'.format(os.path.join(self.cache_dir, mask_name)))
                        for sm_file in sm_files:
                            os.rename(sm_file, '{}_sm.{}'.format(self.name, sm_file[-3:]))
            
            ## calculate estimated uncertainty of the interpolation
            #if self.want_uncertainty:
                
            # if self.want_uncertainty:
            #     iu = InterpolationUncertainty(dem=self, percentile=95, sims=2, chnk_lvl=None, max_sample=None)
            #     unc_out, unc_status = iu.run()
            #     if unc_status == 0:
            #         self.aux_dems.append(unc_out['prox_unc'][0])

            ## post-process any auxiliary rasters
            for aux_dem in self.aux_dems:
                aux_dem = WaffleDEM(aux_dem, cache_dir=self.cache_dir, verbose=self.verbose).initialize()
                if aux_dem.valid_p():
                    aux_dem.process(ndv=None, xsample=self.xsample, ysample=self.ysample, region=self.d_region, clip_str=self.clip,
                                    node=self.node, dst_srs=self.dst_srs, dst_fmt=self.fmt, dst_dir=os.path.dirname(self.fn),
                                    set_metadata=False)

            ## reset the self.stack to new post-processed fn and ds
            if self.want_stack and self.keep_auxiliary:
                self.stack = os.path.join(os.path.dirname(self.fn), os.path.basename(self.stack))
                self.stack_ds = dlim.GDALFile(fn=self.stack, band_no=1, weight_mask=3, uncertainty_mask=4,
                                              data_format=200, src_srs=self.dst_srs, dst_srs=self.dst_srs, x_inc=self.xinc,
                                              y_inc=self.yinc, src_region=self.p_region, weight=1, verbose=self.verbose).initialize()
                        
        return(self)

    def run(self):
        """run the WAFFLES module (set via sub-module class)."""
        
        raise(NotImplementedError)

## ==============================================
##
## Waffles Raster Stacking
##
## ==============================================
class WafflesScratch(Waffle):
    """STACK data into a DEM. 
    
    Generate a DEM using a raster STACKing method. 
    By default, will calculate the [weighted]-mean where overlapping cells occur. 
    Set supercede to True to overwrite overlapping cells with higher weighted data.

    stack data to generate DEM. No interpolation
    occurs with this module. To guarantee a full DEM,
    use a background DEM with a low weight, such as GMRT or GEBCO,
    which will be stacked upon to create the final DEM.
    
    -----------
    Parameters:
    
    min_count=[val] - only retain data cells if they contain `min_count` overlapping data
    
    < stacks:min_count=None >
    """

    ## todo: add parameters for specifying outputs...
    def __init__(self, min_count = None, **kwargs):
        super().__init__(**kwargs)
        self.min_count = min_count
        
    def run(self):
        return(self)

class WafflesStacks(Waffle):
    """STACK data into a DEM. 
    
    Generate a DEM using a raster STACKing method. 
    By default, will calculate the [weighted]-mean where overlapping cells occur. 
    Set supercede to True to overwrite overlapping cells with higher weighted data.

    stack data to generate DEM. No interpolation
    occurs with this module. To guarantee a full DEM,
    use a background DEM with a low weight, such as GMRT or GEBCO,
    which will be stacked upon to create the final DEM.
    
    -----------
    Parameters:
    
    min_count=[val] - only retain data cells if they contain `min_count` overlapping data
    
    < stacks:min_count=None >
    """

    ## todo: add parameters for specifying outputs...
    def __init__(self, min_count = None, **kwargs):
        super().__init__(**kwargs)
        self.min_count = min_count
        
    def run(self):
        z_ds = gdal.GetDriverByName(self.fmt).Create(
            '{}.{}'.format(self.name, gdalfun.gdal_fext(self.fmt)), self.xcount, self.ycount, 1, self.ds_config['dt'],
            options=['COMPRESS=LZW', 'PREDICTOR=2', 'TILED=YES']
        )
        z_ds.SetGeoTransform(self.dst_gt)
        z_band = z_ds.GetRasterBand(1)
        z_band.SetNoDataValue(self.ndv)
        for arrs, srcwin, gt in self.stack_ds.yield_array():
            arrs['z'][np.isnan(arrs['z'])] = self.ndv
            z_band.WriteArray(arrs['z'], srcwin[0], srcwin[1])
            
        z_ds = None            
        return(self)

## ==============================================
##
## The Flattening
##
## ==============================================
class WafflesFlatten(Waffle):
    """Stack the data into a DEM and then hydro-flatten all the void areas.

    specify 'size_threshold' to only flatten voids above threshold.

    -----------
    Parameters:
    
    min_count=[val] - only retain data cells if they contain `min_count` overlapping data
    size_threshold=[val] - the minimum size void to flatten (in cells)
    """
    
    ## todo: add parameters for specifying outputs...
    def __init__(self, min_count = None, size_threshold=1, **kwargs):
        super().__init__(**kwargs)
        self.min_count = min_count
        self.size_threshold = size_threshold
        
    def run(self):
        gdalfun.gdal_hydro_flatten(self.stack, dst_dem=self.fn, band=1, size_threshold=self.size_threshold)
        return(self)
    
## ==============================================
##
## Waffles IDW
##
## ==============================================
class Invdisttree():
    """ inverse-distance-weighted interpolation using KDTree:
    @Denis via https://stackoverflow.com/questions/3104781/inverse-distance-weighted-idw-interpolation-with-python
    https://creativecommons.org/licenses/by-nc-sa/3.0/

invdisttree = Invdisttree( X, z )  -- data points, values
interpol = invdisttree( q, nnear=3, eps=0, p=1, weights=None, stat=0 )
    interpolates z from the 3 points nearest each query point q;
    For example, interpol[ a query point q ]
    finds the 3 data points nearest q, at distances d1 d2 d3
    and returns the IDW average of the values z1 z2 z3
        (z1/d1 + z2/d2 + z3/d3)
        / (1/d1 + 1/d2 + 1/d3)
        = .55 z1 + .27 z2 + .18 z3  for distances 1 2 3

    q may be one point, or a batch of points.
    eps: approximate nearest, dist <= (1 + eps) * true nearest
    p: use 1 / distance**p
    weights: optional multipliers for 1 / distance**p, of the same shape as q
    stat: accumulate wsum, wn for average weights

How many nearest neighbors should one take ?
a) start with 8 11 14 .. 28 in 2d 3d 4d .. 10d; see Wendel's formula
b) make 3 runs with nnear= e.g. 6 8 10, and look at the results --
    |interpol 6 - interpol 8| etc., or |f - interpol*| if you have f(q).
    I find that runtimes don't increase much at all with nnear -- ymmv.

p=1, p=2 ?
    p=2 weights nearer points more, farther points less.
    In 2d, the circles around query points have areas ~ distance**2,
    so p=2 is inverse-area weighting. For example,
        (z1/area1 + z2/area2 + z3/area3)
        / (1/area1 + 1/area2 + 1/area3)
        = .74 z1 + .18 z2 + .08 z3  for distances 1 2 3
    Similarly, in 3d, p=3 is inverse-volume weighting.

Scaling:
    if different X coordinates measure different things, Euclidean distance
    can be way off.  For example, if X0 is in the range 0 to 1
    but X1 0 to 1000, the X1 distances will swamp X0;
    rescale the data, i.e. make X0.std() ~= X1.std() .

A nice property of IDW is that it's scale-free around query points:
if I have values z1 z2 z3 from 3 points at distances d1 d2 d3,
the IDW average
    (z1/d1 + z2/d2 + z3/d3)
    / (1/d1 + 1/d2 + 1/d3)
is the same for distances 1 2 3, or 10 20 30 -- only the ratios matter.
In contrast, the commonly-used Gaussian kernel exp( - (distance/h)**2 )
is exceedingly sensitive to distance and to h.

quite heavy on memory when large grid-size...

    """
# anykernel( dj / av dj ) is also scale-free
# error analysis, |f(x) - idw(x)| ? todo: regular grid, nnear ndim+1, 2*ndim

    def __init__( self, X, z, leafsize=10, stat=0 ):
        assert len(X) == len(z), "len(X) %d != len(z) %d" % (len(X), len(z))
        self.tree = spatial.cKDTree( X, leafsize=leafsize )  # build the tree
        self.z = z
        self.stat = stat
        self.wn = 0
        self.wsum = None;

    def __call__(self, q, nnear=6, eps=0, p=1, dub=np.inf, weights=None):
        # nnear nearest neighbours of each query point --
        q = np.asarray(q)
        qdim = q.ndim
        if qdim == 1:
            q = np.array([q])
        if self.wsum is None:
            self.wsum = np.zeros(nnear)

        eps = utils.float_or(eps, .1)
        self.distances, self.ix = self.tree.query(q, k=nnear, eps=eps, distance_upper_bound=dub)
        interpol = np.zeros((len(self.distances),) + np.shape(self.z[0]))
        jinterpol = 0
        for dist, ix in zip( self.distances, self.ix ):
            if np.any(np.isinf(dist)):
                wz = np.nan
            elif nnear == 1:
                wz = self.z[ix]
            elif dist[0] < 1e-10:
                wz = self.z[ix[0]]
            else:  # weight z s by 1/dist --
                w = 1 / dist**p
                if weights is not None:
                    w *= weights[ix]  # >= 0
                    
                w /= np.sum(w)
                wz = np.dot( w, self.z[ix] )
                if self.stat:
                    self.wn += 1
                    self.wsum += w
                    
            interpol[jinterpol] = wz
            jinterpol += 1
        return interpol if qdim > 1  else interpol[0]

class WafflesIDW(Waffle):
    """INVERSE DISTANCE WEIGHTED DEM
    
    Generate a DEM using an Inverse Distance Weighted algorithm.
    If weights are used, will generate a UIDW DEM, using weight values as inverse uncertainty,
    as described here: https://ir.library.oregonstate.edu/concern/graduate_projects/79407x932
    and here: https://stackoverflow.com/questions/3104781/inverse-distance-weighted-idw-interpolation-with-python

    -----------
    Parameters:
    
    power=[val] - weight**power
    min_points=[val] - minimum neighbor points for IDW
    radius=[val] - search radius (in cells), only fill data cells within radius from data
    chunk_size=[val] - size of chunks in pixels

    < IDW:min_points=8:radius=inf:power=1:chunk_size=None >
    """
    
    def __init__(
            self,
            power=1,
            min_points=8,
            radius=None,
            chunk_size=None,
            **kwargs
    ):
        super().__init__(**kwargs)
        self.power = utils.float_or(power)
        self.min_points = utils.int_or(min_points)
        self.radius = np.inf if radius is None else utils.str2inc(radius) 
        self.chunk_size = chunk_size
        self.chunk_step = None
        
    def run(self):
        if self.verbose:
            if self.min_points:
                utils.echo_msg(
                    'generating IDW grid @ {}/{} looking for at least {} neighbors within {} pixels'.format(
                        self.ycount, self.xcount, self.min_points, self.radius
                    )
                )
            else:
                utils.echo_msg(
                    'generating IDW grid @ {}/{}'.format(self.ycount, self.xcount)
                )
            i=0

        if self.chunk_size is None:
            n_chunk = int(self.ds_config['nx'] * .1)
            n_chunk = 10 if n_chunk < 10 else n_chunk
        else: n_chunk = self.chunk_size
        if self.chunk_step is None:
            n_step = int(n_chunk/2)
        else: n_step = self.chunk_step

        stack_ds = gdal.Open(self.stack)
        
        points_band = stack_ds.GetRasterBand(1)
        points_no_data = points_band.GetNoDataValue()
        weights_band = stack_ds.GetRasterBand(3)
        weights_no_data = weights_band.GetNoDataValue()

        #try:
        interp_ds = stack_ds.GetDriver().Create(
            self.fn, stack_ds.RasterXSize, stack_ds.RasterYSize, bands=1, eType=points_band.DataType,
            options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        )
        interp_ds.SetProjection(stack_ds.GetProjection())
        interp_ds.SetGeoTransform(stack_ds.GetGeoTransform())
        interp_band = interp_ds.GetRasterBand(1)
        interp_band.SetNoDataValue(np.nan)
        #except:
        #    return(self)

        points_array = points_band.ReadAsArray()
        #points_array[points_array == points_no_data] = np.nan
        #point_indices = np.nonzero(~np.isnan(points_array))
        point_indices = np.nonzero(points_array != points_no_data)
        point_values = points_array[point_indices]
        points_array = None

        if self.want_weight:
            weights_array = weights_band.ReadAsArray()
            weight_values = weights_array[point_indices]
            weights_array = None
        else:
            weight_values = None

        stack_ds = None
        invdisttree = Invdisttree(np.transpose(point_indices), point_values, leafsize=10, stat=1)
        for srcwin in utils.yield_srcwin((self.ycount, self.xcount), n_chunk=n_chunk,
                                         msg='Generating IDW DEM', end_msg='Generated IDW DEM',
                                         verbose=self.verbose):
            # if np.count_nonzero(np.isnan(points_array)) == 0:
            #     #if not np.all(~np.nan(points_array)):
            #     # y_origin = srcwin[1]-srcwin_buff[1]
            #     # x_origin = srcwin[0]-srcwin_buff[0]
            #     # y_size = y_origin + srcwin[3]
            #     # x_size = x_origin + srcwin[2]
            #     # points_array = points_array[y_origin:y_size,x_origin:x_size]
            #     interp_band.WriteArray(points_array, srcwin[0], srcwin[1])

            if len(point_indices[0]):
                xi, yi = np.mgrid[srcwin[0]:srcwin[0]+srcwin[2],
                                  srcwin[1]:srcwin[1]+srcwin[3]]
                interp_data = invdisttree(
                    np.vstack((yi.flatten(), xi.flatten())).T,
                    nnear=self.min_points,
                    eps=.1,
                    p=self.power,
                    dub=self.radius,
                    weights=weight_values
                )
                interp_data = np.reshape(interp_data, (srcwin[2], srcwin[3]))
                interp_band.WriteArray(interp_data.T, srcwin[0], srcwin[1])
                
        interp_ds = point_values = weight_values = None        
        return(self)    
    
## ==============================================
##
## Scipy interpolate.griddata gridding (linear, cubic, nearest)
##
## https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.griddata.html
## TODO: rename this in some way
##
## ==============================================
def write_array_queue(wq, q, m):
    while True:
        wq_args = wq.get()
        #utils.echo_msg('writing array to disk')
        m.interp_band.WriteArray(wq_args[0], wq_args[1][0], wq_args[1][1])
        wq.task_done()

def scipy_queue(q, wq, m, p):
    while True:
        this_srcwin = q.get()
        p.update(msg='gridding data to {}: {}'.format(m, q.qsize()))
        try:
            interp_array = m.grid_srcwin(this_srcwin)
        except Exception as e:
            utils.echo_msg(e)
            utils.echo_warning_msg('failed to grid srcwin {}, placing back into queue'.format(this_srcwin))
            q.put(this_srcwin)
            q.task_done()
            continue

        wq.put([interp_array, this_srcwin])
        q.task_done()
               
class grid_scipy(threading.Thread):
    def __init__(self, mod, n_threads=3):
        threading.Thread.__init__(self)
        self.mod = mod
        self.scipy_q = queue.Queue()
        self.grid_q = queue.Queue()
        self.n_threads = n_threads
        self.pbar = utils.CliProgress('gridding data to {}'.format(self.mod))
        
    def run(self):
        for _ in range(1):
            tg = threading.Thread(target=write_array_queue, args=(self.grid_q, self.scipy_q, self.mod))
            tg.daemon = True
            tg.start()

        for _ in range(self.n_threads):
            t = threading.Thread(target=scipy_queue, args=(self.scipy_q, self.grid_q, self.mod, self.pbar))
            t.daemon = True
            t.start()

        for this_srcwin in utils.yield_srcwin(
                n_size=(self.mod.ycount, self.mod.xcount), n_chunk=self.mod.chunk_size,
                step=self.mod.chunk_size, verbose=True
        ):
            self.scipy_q.put(this_srcwin)

        self.grid_q.join()
        self.scipy_q.join()
        self.pbar.end(0, 'gridded data to {}'.format(self.mod))
        
class WafflesSciPy(Waffle):
    """Generate DEM using Scipy gridding interpolation
    
    Generate a DEM using Scipy's gridding interpolation
    Optional gridding methods are 'linear', 'cubic' and 'nearest'
    https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.griddata.html

    -----------
    Parameters:
    
    method=[linear/cubic/nearest] - interpolation method to use
    chunk_size=[val] - size of chunks in pixels
    chunk_buffer=[val] - size of the chunk buffer in pixels

    < scipy:method=<method>:chunk_size=None:chunk_buffer=40 >
    """
    
    def __init__(self, method = 'linear', chunk_size = None, chunk_buffer = 10,
                 chunk_step = None, num_threads = 1, **kwargs):
        """generate a `scipy` dem"""
        
        super().__init__(**kwargs)
        self.methods = ['linear', 'cubic', 'nearest']
        self.method = method
        self.chunk_size = utils.int_or(chunk_size)
        self.chunk_step = chunk_step
        self.chunk_buffer = utils.int_or(chunk_buffer)
        self.num_threads = utils.int_or(num_threads, 1)

    def _run(self):
        self.open()
        #srcwins = utils.chunk_srcwin(n_size=(self.ycount, self.xcount), n_chunk=self.chunk_size, step=self.chunk_size, verbose=True)
        try:
            gs = grid_scipy(self, n_threads=self.num_threads)
            gs.daemon = True
    
            gs.start()
            gs.join()
        except (KeyboardInterrupt, SystemExit):
            utils.echo_error_msg('user breakage...please wait while fetches exits.')
            stop_threads = True
            while not gs.scipy_q.empty():
                try:
                    gs.scipy_q.get(False)
                except Empty:
                    continue
                        
                gs.scipy_q.task_done()
            
        self.close()
        return(self)
        
    def open(self):
        if self.method not in self.methods:
            utils.echo_error_msg(
                '{} is not a valid interpolation method, options are {}'.format(
                    self.method, self.methods
                )
            )
            return(self)
        
        if self.chunk_size is None:
            self.chunk_size = int(self.ds_config['nx'] * .1)
            self.chunk_size = 10 if self.chunk_size < 10 else self.chunk_size
            
        if self.chunk_step is None:
            self.chunk_step = int(self.chunk_size/2)

        self.stack_ds = gdal.Open(self.stack)
        self.points_band = self.stack_ds.GetRasterBand(1)
        self.points_no_data = self.points_band.GetNoDataValue()        
        self.interp_ds = self.stack_ds.GetDriver().Create(
            self.fn, self.stack_ds.RasterXSize, self.stack_ds.RasterYSize, bands=1, eType=self.points_band.DataType,
            options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        )
        if self.interp_ds is not None:
            self.interp_ds.SetProjection(self.stack_ds.GetProjection())
            self.interp_ds.SetGeoTransform(self.stack_ds.GetGeoTransform())
            self.interp_band = self.interp_ds.GetRasterBand(1)
            self.interp_band.SetNoDataValue(np.nan)
        else:
            utils.echo_error_msg('could not create {}...'.format(self.fn))
            return(self)
        
        if self.verbose:
            utils.echo_msg('buffering srcwin by {} pixels'.format(self.chunk_buffer))

        self.points_array = self.points_band.ReadAsArray()
        self.stack_ds = None

    def close(self):
        self.interp_ds = self.stack_ds = None            
            
    def grid_srcwin(self, srcwin):
        srcwin_buff = utils.buffer_srcwin(srcwin, (self.ycount, self.xcount), self.chunk_buffer)
        points_array = self.points_array[srcwin_buff[1]:srcwin_buff[1]+srcwin_buff[3],srcwin_buff[0]:srcwin_buff[0]+srcwin_buff[2]]
        #print(points_array)
        points_array[points_array == self.points_no_data] = np.nan
        point_indices = np.nonzero(~np.isnan(points_array))
        if np.count_nonzero(np.isnan(points_array)) == 0:
            y_origin = srcwin[1]-srcwin_buff[1]
            x_origin = srcwin[0]-srcwin_buff[0]
            y_size = y_origin + srcwin[3]
            x_size = x_origin + srcwin[2]
            points_array = points_array[y_origin:y_size,x_origin:x_size]
            #self.interp_band.WriteArray(points_array, srcwin[0], srcwin[1])
            return(points_array)

        elif len(point_indices[0]):
            point_values = points_array[point_indices]
            #utils.echo_msg(point_values)
            xi, yi = np.mgrid[0:srcwin_buff[2],
                              0:srcwin_buff[3]]
            #try:
            interp_data = interpolate.griddata(
                np.transpose(point_indices), point_values,
                (xi, yi), method=self.method
            )
            # while np.any(interp_data[np.isnan(interp_data)]):
            #     utils.echo_msg('nodata in {}'.format(srcwin))
            #     point_indices = np.nonzero(~np.isnan(interp_data))
            #     point_values = interp_data[point_indices]
            #     interp_data = interpolate.griddata(
            #         np.transpose(point_indices), point_values,
            #         (xi, yi), method=self.method
            #     )

            y_origin = srcwin[1]-srcwin_buff[1]
            x_origin = srcwin[0]-srcwin_buff[0]
            y_size = y_origin + srcwin[3]
            x_size = x_origin + srcwin[2]
            interp_data = interp_data[y_origin:y_size,x_origin:x_size]
            #self.interp_band.WriteArray(interp_data, srcwin[0], srcwin[1])
            #utils.echo_msg(interp_data)
            return(interp_data)
            #except Exception as e:
            #    return(points_array)
                
        return(None)
        
    def run(self):
        if self.method not in self.methods:
            utils.echo_error_msg(
                '{} is not a valid interpolation method, options are {}'.format(
                    self.method, self.methods
                )
            )
            return(self)
        
        if self.chunk_size is None:
            n_chunk = int(self.ds_config['nx'] * .1)
            n_chunk = 10 if n_chunk < 10 else n_chunk
        else:
            n_chunk = self.chunk_size
            
        if self.chunk_step is None:
            n_step = int(n_chunk/2)
            #n_step = n_chunk
        else:
            n_step = self.chunk_step

        stack_ds = gdal.Open(self.stack)
        points_band = stack_ds.GetRasterBand(1)
        points_no_data = points_band.GetNoDataValue()
        interp_ds = stack_ds.GetDriver().Create(
            self.fn, stack_ds.RasterXSize, stack_ds.RasterYSize, bands=1, eType=points_band.DataType,
            options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        )
        if interp_ds is not None:
            interp_ds.SetProjection(stack_ds.GetProjection())
            interp_ds.SetGeoTransform(stack_ds.GetGeoTransform())
            interp_band = interp_ds.GetRasterBand(1)
            interp_band.SetNoDataValue(np.nan)
        else:
            utils.echo_error_msg('could not create {}...'.format(self.fn))
            return(self)
        
        if self.verbose:
            utils.echo_msg('buffering srcwin by {} pixels'.format(self.chunk_buffer))
       
        for srcwin in utils.yield_srcwin((self.ycount, self.xcount), n_chunk=n_chunk, verbose=self.verbose, step=n_step):
            chunk_buffer = self.chunk_buffer
            #while True:
            srcwin_buff = utils.buffer_srcwin(srcwin, (self.ycount, self.xcount), chunk_buffer)
            points_array = points_band.ReadAsArray(*srcwin_buff)
            points_array[points_array == points_no_data] = np.nan
            point_indices = np.nonzero(~np.isnan(points_array))
            #point_indices = np.nonzero(points_array != points_no_data)
            if np.count_nonzero(np.isnan(points_array)) == 0:
                #if not np.all(~np.nan(points_array)):
                y_origin = srcwin[1]-srcwin_buff[1]
                x_origin = srcwin[0]-srcwin_buff[0]
                y_size = y_origin + srcwin[3]
                x_size = x_origin + srcwin[2]
                points_array = points_array[y_origin:y_size,x_origin:x_size]
                interp_band.WriteArray(points_array, srcwin[0], srcwin[1])
                #break

            elif len(point_indices[0]):
                point_values = points_array[point_indices]
                xi, yi = np.mgrid[0:srcwin_buff[2],
                                  0:srcwin_buff[3]]

                try:
                    interp_data = interpolate.griddata(
                        np.transpose(point_indices), point_values,
                        (xi, yi), method=self.method
                    )
                    # while np.any(interp_data[np.isnan(interp_data)]):
                    #     utils.echo_msg('nodata in {}'.format(srcwin))
                    #     point_indices = np.nonzero(~np.isnan(interp_data))
                    #     point_values = interp_data[point_indices]
                    #     interp_data = interpolate.griddata(
                    #         np.transpose(point_indices), point_values,
                    #         (xi, yi), method=self.method
                    #     )
                    
                    y_origin = srcwin[1]-srcwin_buff[1]
                    x_origin = srcwin[0]-srcwin_buff[0]
                    y_size = y_origin + srcwin[3]
                    x_size = x_origin + srcwin[2]
                    interp_data = interp_data[y_origin:y_size,x_origin:x_size]
                        
                    #chunk_buffer *= 2
                    #utils.echo_msg('chunk buffer to {}'.format(chunk_buffer))
                    #else:
                    interp_band.WriteArray(interp_data, srcwin[0], srcwin[1])
                    #break
                except Exception as e:
                    continue
                
        interp_ds = stack_ds = point_values = weight_values = None            
        return(self)

class WafflesLinear(WafflesSciPy):
    """LINEAR (triangulated) DEM
    
    -----------
    Parameters:
    
    chunk_size (int): size of chunks in pixels
    chunk_step (iint):  size of chunks in pixels
    chunk_buffer (int):  size of the chunk buffer in pixels
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.method = 'linear'
        
class WafflesCubic(WafflesSciPy):
    """CUBIC (triangulated) DEM
    
    -----------
    Parameters:
    
    chunk_size=[val] - size of chunks in pixels
    chunk_buffer=[val] - size of the chunk buffer in pixels
    """
        
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.method = 'cubic'

class WafflesNearest(WafflesSciPy):
    """NEAREST neighbor DEM
    
-----------
Parameters:
    
chunk_size=[val] - size of chunks in pixels
chunk_buffer=[val] - size of the chunk buffer in pixels
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.method = 'nearest'
        
## ==============================================
##
## GMT Surface
##
## TODO: update to use pygmt
##
## ==============================================
class GMTSurface(Waffle):
    """SPLINE DEM via GMT surface
    
    Generate a DEM using GMT's surface command
    see gmt surface --help for more info.

    -----------
    Parameters:
   
    tension=[0-1] - spline tension.
    relaxation=[val] - spline relaxation factor.
    aspect=[val/None] - gridding aspect
    breakline=[path/None] - use xyz dataset at `path` as a breakline
    convergence=[val/None] - gridding convergence
    blockmean=[True/False] - pipe the data through gmt blockmean before gridding
    geographic=[True/Faslse] - data/grid are geographic

    < gmt-surface:tension=.35:relaxation=1:max_radius=None:aspect=None:breakline=None:convergence=None:blockmean=False:geographic=True >
    """
    
    def __init__(self, tension=.35, relaxation=1, max_radius=None,
                 aspect=None, breakline=None, convergence=None, blockmean=False,
                 geographic=True, **kwargs):
        super().__init__(**kwargs)
        self.tension = tension
        self.convergence = utils.float_or(convergence)
        self.relaxation = relaxation
        self.breakline = breakline
        self.max_radius = max_radius
        self.aspect = aspect
        self.blockmean = blockmean
        self.geographic = geographic
        
    def run(self):
        if self.gc['GMT'] is None:
            utils.echo_error_msg(
                'GMT must be installed to use the SURFACE module'
            )
            return(None, -1)

        out, status = utils.run_cmd(
            'gmt gmtset IO_COL_SEPARATOR = SPACE',
            verbose = False
        )        
                
        dem_surf_cmd = ('')
        if self.blockmean:
            dem_surf_cmd = (
                'gmt blockmean {} -I{:.16f}/{:.16f}+e{}{} -V |'.format(
                    self.ps_region.format('gmt'), self.xinc, self.yinc,
                    ' -W' if self.want_weight else '', ' -fg' if self.geographic else ''
                )
            )

        dem_surf_cmd += (
            'gmt surface -V {} -rp -I{:.16f}/{:.16f}+e -G{}.tif=gd+n{}:GTiff -T{} -Z{} {}{}{}{}{}'.format(
                self.p_region.format('gmt'), self.xinc, self.yinc,
                self.name, self.ndv, self.tension, self.relaxation,
                ' -D{}'.format(self.breakline) if self.breakline is not None else '',
                ' -M{}'.format(self.max_radius) if self.max_radius is not None else '',
                ' -C{}'.format(self.convergence) if self.convergence is not None else '',
                ' -A{}'.format(self.aspect) if self.aspect is not None else '',
                ' -fg' if self.geographic else '',
            )
        )
        
        out, status = utils.run_cmd(
            dem_surf_cmd,
            verbose=self.verbose,
            data_fun=lambda p: self.dump_xyz(
                dst_port=p, encode=True
            )
        )
        return(self)

## ==============================================
##
## GMT Triangulate
##
## TODO: update to use pygmt
##
## ==============================================
class GMTTriangulate(Waffle):
    """TRIANGULATION DEM via GMT triangulate
    
    Generate a DEM using GMT's triangulate command.
    see gmt triangulate --help for more info.

    < gmt-triangulate >
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)        
        
    def run(self):
        if self.gc['GMT'] is None:
            utils.echo_error_msg(
                'GMT must be installed to use the TRIANGULATE module'
            )
            return(None, -1)

        out, status = utils.run_cmd(
            'gmt gmtset IO_COL_SEPARATOR = SPACE',
            verbose = False
        )        
        
        dem_tri_cmd = 'gmt triangulate -V {} -I{:.14f}/{:.14f} -G{}.tif=gd:GTiff'.format(
            self.ps_region.format('gmt'), self.xinc, self.yinc, self.name
        )
        out, status = utils.run_cmd(
            dem_tri_cmd,
            verbose = self.verbose,
            data_fun = lambda p: self.dump_xyz(
                dst_port=p, encode=True
            )
        )        
        return(self)

## ==============================================
##
## GMT Near Neighbor
##
## TODO: update to use pygmt
##
## ==============================================
class GMTNearNeighbor(Waffle):
    """NEARNEIGHBOR DEM via GMT nearneighbor
    
    Generate a DEM using GMT's nearneighbor command.
    see gmt nearneighbor --help for more info.
    
    -----------
    Parameters:
    
    radius=[val] - search radius
    sectors=[val] - sector information
    
    < gmt-nearneighbor:radius=None:sectors=None >
    """
    
    def __init__(self, radius=None, sectors=None, **kwargs):
        super().__init__(**kwargs) 
        self.radius = radius
        self.sectors = sectors
        
    def run(self):
        if self.gc['GMT'] is None:
            utils.echo_error_msg(
                'GMT must be installed to use the NEARNEIGHBOR module'
            )
            return(None, -1)

        out, status = utils.run_cmd(
            'gmt gmtset IO_COL_SEPARATOR = SPACE',
            verbose = False
        )        

        dem_nn_cmd = 'gmt nearneighbor -V {} -I{:.14f}/{:.14f} -G{}.tif=gd+n{}:GTiff{}{}{}'.format(
            self.ps_region.format('gmt'), self.xinc, self.yinc, self.name, self.ndv,
            ' -W' if self.want_weight else '', ' -N{}'.format(self.sectors) if self.sectors is not None else '',
            ' -S{}'.format(self.radius) if self.radius is not None else ' -S{}'.format(self.xinc),
        )
        out, status = utils.run_cmd(
            dem_nn_cmd,
            verbose = self.verbose,
            data_fun = lambda p: self.dump_xyz(
                dst_port=p, encode=True
            )
        )        
        return(self)

## ==============================================
##
## MB-System mbgrid
##
## ==============================================
class WafflesMBGrid(Waffle):
    """SPLINE DEM via MB-System's mbgrid.
    
    Generate a DEM using MB-System's mbgrid command.
    By default, will use MB-Systems datalist processes.
    set `use_datalist=True` to use CUDEM's dlim instead.
    see mbgrid --help for more info

    -----------
    Parameters:
    
    dist=[val] - the dist variable to use in mbgrid
    tension=[val] - the spline tension value (0-inf)
    use_stack=[True/False] - use built-in datalists rather than mbdatalist
    
    < mbgrid:dist='10/3':tension=35:use_datalists=False >
    """
    
    def __init__(self, dist='10/3', tension=35, use_stack=True, nc=False, **kwargs):
        super().__init__(**kwargs)
        self.nc = nc
        self.dist = dist
        self.tension = tension
        self.use_stack = use_stack
                
    def _gmt_num_msk(self, num_grd, dst_msk):
        """generate a num-msk from a NUM grid using GMT grdmath

        Args:
          num_grd (str): pathname to a source `num` grid file
          dst_msk (str): pathname to a destination `msk` grid file

        Returns:
          list: [cmd-output, cmd-return-code]
        """

        if self.gc['MBGRID'] is None:
            utils.echo_error_msg(
                'MB-System must be installed to use the MBGRID module'
            )
            return(None, -1)

        if self.gc['GMT'] is None:
            utils.echo_error_msg(
                'GMT must be installed to use the MBGRID module'
            )
            return(None, -1)

        num_msk_cmd = 'gmt grdmath -V {} 0 MUL 1 ADD 0 AND = {}'.format(
            num_grd, dst_msk
        )
        return(
            utils.run_cmd(
                num_msk_cmd, verbose=self.verbose
            )
        )

    def _gmt_grd2gdal(self, src_grd, dst_fmt='GTiff'):
        """convert the grd file to tif using GMT

        Args:
          src_grd (str): a pathname to a grid file
          dst_fmt (str): the output GDAL format string

        Returns:
          str: the gdal file name or None
        """

        dst_gdal = '{}.{}'.format(
            os.path.basename(src_grd).split('.')[0], galfun.gdal_fext(dst_fmt)
        )        
        grd2gdal_cmd = 'gmt grdconvert {} {}=gd+n{}:{} -V'.format(
            src_grd, dst_gdal, self.ndv, dst_fmt
        )
        out, status = utils.run_cmd(
            grd2gdal_cmd, verbose=self.verbose
        )
        if status == 0:
            return(dst_gdal)
        else:
            return(None)
        
    def _gmt_grdsample(self, src_grd, dst_fmt='GTiff'):
        """convert the grd file to tif using GMT

        Args:
          src_grd (str): a pathname to a grid file
          dst_fmt (str): the output GDAL format string

        Returns:
          str: the gdal file name or None
        """

        dst_gdal = '{}.{}'.format(
            os.path.basename(src_grd).split('.')[0], galfun.gdal_fext(dst_fmt)
        )
        grdsample_cmd = 'gmt grdsample {} -T -G{}=gd+n{}:{} -V'.format(
            src_grd, dst_gdal, self.ndv, dst_fmt
        )        
        out, status = utils.run_cmd(
            grdsample_cmd, verbose=self.verbose
        )        
        if status == 0:
            return(dst_gdal)        
        else:
            return(None)

    def stack2mbdatalist(self):
        mb_datalist_fn = os.path.join(self.cache_dir, '_tmp_mb.datalist')
        mb_stack_xyz = os.path.join(self.cache_dir, '{}.xyz'.format(utils.fn_basename2(self.stack)))
        with open(mb_datalist_fn, 'w') as dl:
            dl.write('{} 168 1'.format(mb_stack_xyz))
                
        with open(mb_stack_xyz, 'w') as stack_xyz:
            self.dump_xyz(stack_xyz)

        return(mb_datalist_fn)
        
    def run(self):
        mb_datalist = self.stack2mbdatalist() if self.use_stack else self.data.fn
        out_name = os.path.join(self.cache_dir, self.name)
        mbgrid_cmd = 'mbgrid -I{} {} -D{}/{} -O{} -A2 -F1 -N -C{} -S0 -X0.1 -T{}'.format(
            mb_datalist, self.p_region.format('gmt'), self.xcount, self.ycount, out_name, self.dist, self.tension
        )

        out, status = utils.run_cmd(mbgrid_cmd, verbose=self.verbose)
        if status == 0:
            gdal2gdal_cmd = ('gdal_translate {} {} -f {} -co TILED=YES -co COMPRESS=DEFLATE\
            '.format('{}.grd'.format(out_name), self.fn, self.fmt))            
            out, status = utils.run_cmd(gdal2gdal_cmd, verbose=self.verbose)
            
            
        # for out in utils.yield_cmd(mbgrid_cmd, verbose=self.verbose):
        #     sys.stderr.write('{}'.format(out))
                    
        return(self)

## ==============================================
##
## GDAL gridding (invdst, linear, nearest, average)
##
## ==============================================
class WafflesGDALGrid(Waffle):
    """Waffles GDAL_GRID module.

    see gdal_grid for more info and gridding algorithms
    """
    
    def __init__(self, **kwargs):
        """run gdal grid using alg_str

        parse the data through xyzfun.xyz_block to get weighted mean before
        building the GDAL dataset to pass into gdal_grid
        
        Args: 
          alg_str (str): the gdal_grid algorithm string
        """
        
        super().__init__(**kwargs)
        self.alg_str = 'linear:radius=-1'
        self.mod = self.alg_str.split(':')[0]

    def _vectorize_stack(self):
        """Make a point vector OGR DataSet Object from src_xyz

        for use in gdal gridding functions.
        """

        dst_ogr = '{}'.format(self.name)
        ogr_ds = gdal.GetDriverByName('Memory').Create(
            '', 0, 0, 0, gdal.GDT_Unknown
        )
        layer = ogr_ds.CreateLayer(
            dst_ogr, geom_type=ogr.wkbPoint25D
        )
        for x in ['long', 'lat', 'elev', 'weight']:
            fd = ogr.FieldDefn(x, ogr.OFTReal)
            fd.SetWidth(12)
            fd.SetPrecision(8)
            layer.CreateField(fd)
            
        f = ogr.Feature(feature_def=layer.GetLayerDefn())        
        #with utils.CliProgress(message='vectorizing stack', verbose=self.verbose) as pbar:
        with tqdm(desc='vectorizing stack') as pbar:
            for this_xyz in self.stack_ds.yield_xyz():
                pbar.update()
                f.SetField(0, this_xyz.x)
                f.SetField(1, this_xyz.y)
                f.SetField(2, float(this_xyz.z))
                if self.want_weight:
                    f.SetField(3, this_xyz.w)

                wkt = this_xyz.export_as_wkt(include_z=True)
                g = ogr.CreateGeometryFromWkt(wkt)
                f.SetGeometryDirectly(g)
                layer.CreateFeature(f)
            
        return(ogr_ds)
        
    def run(self):
        utils.echo_msg(
            'running GDAL GRID {} algorithm @ {} and {}/{}...'.format(
                self.alg_str.split(':')[0], self.p_region.format('fn'), self.xcount, self.ycount
            )
        )
        #_prog = utils.CliProgress(message='running GDAL GRID {} algorithm'.format(self.alg_str))
        _prog = tqdm(desc='running GDAL GRID {} algorithm'.format(self.alg_str))
        _prog_update = lambda x, y, z: _prog.update()
        ogr_ds = self._vectorize_stack()
        if ogr_ds.GetLayer().GetFeatureCount() == 0:
            utils.echo_error_msg('no input data')
            
        gd_opts = gdal.GridOptions(
            outputType = gdal.GDT_Float32,
            noData = self.ndv,
            format = 'GTiff',
            width = self.xcount,
            height = self.ycount,
            algorithm = self.alg_str,
            callback = _prog_update if self.verbose else None,
            outputBounds = [
                self.p_region.xmin, self.p_region.ymax,
                self.p_region.xmax, self.p_region.ymin
            ]
        )
        gdal.Grid('{}.tif'.format(self.name), ogr_ds, options=gd_opts)
        gdalfun.gdal_set_ndv('{}.tif'.format(self.name, ndv=self.ndv, convert_array=False))
        ogr_ds = None
        return(self)

class GDALLinear(WafflesGDALGrid):
    """LINEAR DEM via gdal_grid
    Generate a DEM using GDAL's gdal_grid command.
    see gdal_grid --help for more info

    -----------
    Parameters:
    
    radius=[val] - search radius
    
    < gdal-linear:radius=-1 >
    """
    
    def __init__(self, radius=None, nodata=-9999, **kwargs):
        super().__init__(**kwargs)        
        radius = self.xinc * 4 if radius is None else utils.str2inc(radius)
        self.alg_str = 'linear:radius={}:nodata={}'.format(radius, nodata)
        
class GDALInvDst(WafflesGDALGrid):
    """INVERSE DISTANCE DEM via gdal_grid
    
    Generate a DEM using GDAL's gdal_grid command.
    see gdal_grid --help for more info

    -----------
    Parameters:
    
    radius1=[val] - search radius 1
    radius2=[val] - search radius 2
    power=[val] - weight**power
    min_points=[val] - minimum points per IDW bucket (use to fill entire DEM)

    < gdal-invdst:power=2.0:smoothing=0.0:radius1=0:radius2=0:angle=0:max_points=0:min_points=0:nodata=0 >
    """
    
    def __init__(self, power = 2.0, smoothing = 0.0, radius1 = None, radius2 = None, angle = 0.0,
                   max_points = 0, min_points = 0, nodata = -9999, **kwargs):
        super().__init__(**kwargs)
        radius1 = self.xinc * 2 if radius1 is None else utils.str2inc(radius1)
        radius2 = self.yinc * 2 if radius2 is None else utils.str2inc(radius2)
        self.alg_str = 'invdist:power={}:smoothing={}:radius1={}:radius2={}:angle={}:max_points={}:min_points={}:nodata={}'\
            .format(power, smoothing, radius1, radius2, angle, max_points, min_points, nodata)
                
class GDALMovingAverage(WafflesGDALGrid):
    """Moving AVERAGE DEM via gdal_grid
    
    Generate a DEM using GDAL's gdal_grid command.
    see gdal_grid --help for more info
    
    -----------
    Parameters:
    
    radius1=[val] - search radius 1
    radius2=[val] - search radius 2
    min_points=[val] - minimum points per bucket (use to fill entire DEM)

    < gdal-average:radius1=0:radius2=0:angle=0:min_points=0:nodata=0 >
    """
    
    def __init__(self, radius1=None, radius2=None, angle=0.0, min_points=0, nodata=-9999, **kwargs):
        super().__init__(**kwargs)
        radius1 = self.xinc * 2 if radius1 is None else utils.str2inc(radius1)
        radius2 = self.yinc * 2 if radius2 is None else utils.str2inc(radius2)
        self.alg_str = 'average:radius1={}:radius2={}:angle={}:min_points={}:nodata={}'\
            .format(radius1, radius2, angle, min_points, nodata)
                
class GDALNearest(WafflesGDALGrid):
    """NEAREST DEM via gdal_grid
    
    Generate a DEM using GDAL's gdal_grid command.
    see gdal_grid --help for more info

    -----------
    Parameters:
    
    radius1=[val] - search radius 1
    radius2=[val] - search radius 2
    angle=[val] - angle
    nodata=[val] - nodata

    < gdal-nearest:radius1=0:radius2=0:angle=0:nodata=0 >
    """
    
    def __init__(self, radius1=None, radius2=None, angle=0.0, nodata=-9999, **kwargs):
        super().__init__(**kwargs)
        radius1 = self.xinc * 2 if radius1 is None else utils.str2inc(radius1)
        radius2 = self.yinc * 2 if radius2 is None else utils.str2inc(radius2)
        self.alg_str = 'nearest:radius1={}:radius2={}:angle={}:nodata={}'\
            .format(radius1, radius2, angle, nodata)
    
## ==============================================
##
## Waffles 'num' - no interpolation
##
## just use stacks...
##
## ==============================================
class WafflesNum(Waffle):
    """Uninterpolated DEM populated by <mode>.
    
    Generate an uninterpolated DEM using <mode> option.
    Using mode of 'A<mode>' uses GMT's xyz2grd command, 
    see gmt xyz2grd --help for more info.
    
    mode keys: k (mask), m (mean), n (num), w (wet), A<mode> (gmt xyz2grd)
    
    -----------
    Parameters:
    
    mode=[key] - specify mode of grid population
    min_count=[val] - minimum number of points per cell

    < num:mode=n:min_count=None >
    """
    
    def __init__(self, mode='n', min_count=None, **kwargs):
        """generate an uninterpolated Grid
        `mode` of `n` generates a num grid
        `mode` of `m` generates a mean grid
        `mode` of `k` generates a mask grid
        `mode` of `w` generates a wet/dry mask grid
        `mode` of `A` generates via GMT 'xyz2grd'
        """
        
        super().__init__(**kwargs)        
        self.mode = mode
        self.min_count = min_count
        
    def _xyz_num(self):
        """Create a GDAL supported grid from xyz data """

        xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.xinc, y_inc=self.yinc)
        if self.verbose:
            utils.echo_msg(
                'generating uninterpolated NUM grid `{}` @ {}/{}'.format(
                    self.mode, ycount, xcount
                )
            )          
            
        if self.mode == 'm' or self.mode == 'w':
            sum_array = np.zeros((ycount, xcount))
            
        count_array = np.zeros((ycount, xcount))
        gdt = gdal.GDT_Float32
        ds_config = gdalfun.gdal_set_infos(
            xcount,
            ycount,
            xcount * ycount,
            dst_gt,
            gdalfun.osr_wkt(self.dst_srs),
            gdt,
            self.ndv,
            'GTiff',
            None,
            None
        )
        
        for this_xyz in self.yield_xyz():
            if regions.xyz_in_region_p(this_xyz, self.p_region):
                xpos, ypos = utils._geo2pixel(
                    this_xyz.x, this_xyz.y, dst_gt, 'pixel'
                )
                
                try:
                    if self.mode == 'm' or self.mode == 'w':
                        sum_array[ypos, xpos] += this_xyz.z
                    if self.mode == 'n' or self.mode == 'm' or self.mode == 'w':
                        count_array[ypos, xpos] += 1
                    else:
                        count_array[ypos, xpos] = 1
                        
                except Exception as e:
                    pass
                
        if self.mode == 'm' or self.mode == 'w':
            count_array[count_array == 0] = np.nan
            out_array = (sum_array/count_array)
            if self.mode == 'w':
                out_array[out_array >= 0] = 1
                out_array[out_array < 0] = 0
        else:
            out_array = count_array

        out_array[np.isnan(out_array)] = self.ndv            
        gdalfun.gdal_write(out_array, self.fn, ds_config)
        
    def run(self):
        if self.mode.startswith('A'):
            if self.gc['GMT'] is None:
                utils.echo_error_msg(
                    'GMT must be installed to use the Mode `A` with the NUM module'
                )
                return(None, -1)

            out, status = utils.run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)            
            dem_xyz2grd_cmd = 'gmt xyz2grd -{} -V {} -I{:.14f}/{:.14f}+e -G{}.tif=gd:GTiff'.format(
                self.mode,
                self.ps_region.format('gmt'),
                self.xinc,
                self.yinc,
                self.name
            )
            
            out, status = utils.run_cmd(
                dem_xyz2grd_cmd,
                verbose=self.verbose,
                data_fun=lambda p: self.dump_xyz(
                    dst_port=p, encode=True
                )
            )
        else:
            self._xyz_num()
                        
        return(self)
    
## ==============================================
##
## Waffles VDatum
##
## ==============================================
class WafflesVDatum(Waffle):
    """VDATUM transformation grid.
    Generate a Vertical DATUM transformation grid.

    -----------
    Parameters:
    
    vdatum_in=[vdatum] - input vertical datum
    vdatum_out=[vdatum] - output vertical datum
    
    < vdatum:vdatum_in=None:vdatum_out=None >
    """

    def __init__(self, vdatum_in=None, vdatum_out=None, **kwargs):
        super().__init__(**kwargs)
        self.vdatum_in = vdatum_in
        self.vdatum_out = vdatum_out        

    def run(self):
        vdatums.VerticalTransform(
            self.p_region, self.xinc, self.yinc, self.vdatum_in, self.vdatum_out,
            cache_dir=waffles_cache
        ).run(outfile='{}.tif'.format(self.name))
        return(self)
    
## ==============================================
##
## Waffles Coastline/Landmask
##
## ==============================================
class WafflesCoastline(Waffle):
    """COASTLINE (land/etc-mask) generation
    
    Generate a coastline (land/etc-mask) using a variety of sources. 
    User data can be provided to provide source for further land masking. 
    Output raster will mask land-areas as 1 and oceans/(lakes/buildings) as 0.
    Output vector will polygonize land-areas.
    
    -----------
    Parameters:
    
    want_gmrt=[True/False] - use GMRT to fill background (will use Copernicus otherwise)
    want_copernicus=[True/False] - use COPERNICUS to fill background
    want_nhd=[True/False] - use high-resolution NHD to fill US coastal zones
    want_lakes=[True/False] - mask LAKES using HYDROLAKES
    want_buildings=[True/False] - mask BUILDINGS using OSM
    osm_tries=[val] - OSM max server attempts
    min_building_length=[val] - only use buildings larger than val
    want_wsf=[True/False] - mask BUILDINGS using WSF
    invert=[True/False] - invert the output results
    polygonize=[True/False] - polygonize the output

    < coastline:want_gmrt=False:want_nhd=True:want_lakes=False:want_buildings=False:invert=False:polygonize=True >
    """
    
    def __init__(
            self,
            want_nhd=True,
            want_copernicus=True,
            want_gmrt=False,
            want_lakes=False,
            want_buildings=False,
            min_building_length=None,
            want_osm_planet=False,
            invert=False,
            polygonize=True,
            osm_tries=5,
            want_wsf=False,
            **kwargs
    ):
        """Generate a landmask from various sources.

        sources include:
        GMRT
        Copernicus
        NHD
        HydroLakes
        OSM Buildings
        *user-data*
        
        set polyginize to False to skip polyginizing the landmask, set
        polygonize to an integer to control how many output polygons will
        be generated.

        output raster mask will have 0 over water and 1 over land.
        """

        super().__init__(**kwargs)
        self.want_nhd = want_nhd
        self.want_gmrt = want_gmrt
        self.want_copernicus = want_copernicus
        self.want_lakes = want_lakes
        self.want_buildings = want_buildings
        self.want_wsf = want_wsf
        self.min_building_length = min_building_length
        self.want_osm_planet = want_osm_planet
        self.invert = invert
        self.polygonize = polygonize
        self.osm_tries = utils.int_or(osm_tries, 5)
        self.coast_array = None
        self.ds_config = None
            
    def run(self):
        self.f_region = self.p_region.copy()
        self.f_region.buffer(pct=5, x_inc=self.xinc, y_inc=self.yinc)
        self.f_region.src_srs = self.dst_srs
        self.wgs_region = self.f_region.copy()
        self.wgs_srs = 'epsg:4326'
        if self.dst_srs is not None:
            self.wgs_region.warp(self.wgs_srs)
        else:
            self.dst_srs = self.wgs_srs

        #self.wgs_srs = osr.SpatialReference()
        #self.wgs_srs.SetFromUserInput('epsg:4326')

        horz_epsg, vert_epsg = gdalfun.epsg_from_input(self.dst_srs)
        self.cst_srs = horz_epsg
        #self.cst_srs = osr.SpatialReference()
        #self.cst_srs.SetFromUserInput('epsg:{}'.format(horz_epsg))
        
        self._load_coast_mask()

        if self.want_gmrt:
            self._load_gmrt()
            
        if self.want_copernicus:
            self._load_copernicus()

        if self.want_nhd:
            self._load_nhd()

        if self.want_lakes:
            self._load_lakes()

        if self.want_buildings:
            self._load_bldgs()

        if self.want_wsf:
            self._load_wsf()
            
        #if len(self.data) > 0:
        if self.want_stack:
            self._load_data()

        if self.verbose:
            utils.echo_msg(
                'finanlizing array for region {} at {} {}...'.format(
                    self.p_region.format('gmt'), self.ds_config['nx'], self.ds_config['ny']
                )
            )
        self._finalize_array()
        self._write_coast_array()
        if self.polygonize:
            if utils.int_or(self.polygonize) is not None:
                self._write_coast_poly(poly_count=self.polygonize)
            else:
                self._write_coast_poly()

        if np.all(self.coast_array == 0) or np.all(self.coast_array == 1):
            return(None)
        else:
            return(self)

    def _finalize_array(self):
        self.coast_array[self.coast_array > 0] = 1
        self.coast_array[self.coast_array <= 0] = 0
        if self.invert:
            self.coast_array[self.coast_array == 0] = 2
            self.coast_array[self.coast_array == 1] = 0
            self.coast_array[self.coast_array == 2] = 1
            
    def _load_coast_mask(self):
        """create a nodata grid"""
        
        xcount, ycount, gt = self.p_region.geo_transform(x_inc=self.xinc, y_inc=self.yinc)
        self.ds_config = gdalfun.gdal_set_infos(xcount, ycount, xcount * ycount, gt,
                                                self.cst_srs, gdal.GDT_Float32,
                                                self.ndv, 'GTiff', None, None)        
        self.coast_array = np.zeros( (ycount, xcount) )

    def _load_gmrt(self):
        """GMRT - Global low-res.

        Used to fill un-set cells.
        """
        
        this_gmrt = fetches.GMRT(
            src_region=self.wgs_region, verbose=self.verbose, layer='topo', outdir=self.cache_dir
        )
        this_gmrt.run()
        fr = fetches.fetch_results(this_gmrt)
        fr.daemon = True
        fr.start()
        fr.join()

        gmrt_result = this_gmrt.results[0]
        gmrt_tif = os.path.join(this_gmrt._outdir, gmrt_result[1])
        gmrt_ds = gdalfun.gdal_mem_ds(self.ds_config, name='gmrt', src_srs=self.wgs_srs)
        gdal.Warp(gmrt_ds, gmrt_tif, dstSRS=self.cst_srs, resampleAlg=self.sample)
        gmrt_ds_arr = gmrt_ds.GetRasterBand(1).ReadAsArray()
        gmrt_ds_arr[gmrt_ds_arr > 0] = 1
        gmrt_ds_arr[gmrt_ds_arr < 0] = 0
        self.coast_array += gmrt_ds_arr
        gmrt_ds = gmrt_ds_arr = None
        
    def _load_copernicus(self):
        """copernicus"""

        this_cop = fetches.CopernicusDEM(
            src_region=self.wgs_region, verbose=self.verbose, datatype='1', outdir=self.cache_dir
        )
        this_cop.run()
        fr = fetches.fetch_results(this_cop, check_size=False)
        fr.daemon = True
        fr.start()
        fr.join()
        
        for i, cop_result in enumerate(this_cop.results):
            cop_tif = os.path.join(this_cop._outdir, cop_result[1])
            gdalfun.gdal_set_ndv(cop_tif, 0, verbose=False)
            cop_ds = gdalfun.gdal_mem_ds(self.ds_config, name='copernicus', src_srs=self.wgs_srs)
            gdal.Warp(
                cop_ds, cop_tif, dstSRS=self.cst_srs, resampleAlg=self.sample,
                callback=False, srcNodata=0
            )
            cop_ds_arr = cop_ds.GetRasterBand(1).ReadAsArray()
            cop_ds_arr[cop_ds_arr != 0] = 1
            self.coast_array += cop_ds_arr
            cop_ds = cop_ds_arr = None

    def _load_wsf(self):
        """wsf"""

        this_wsf = fetches.WSF(
            src_region=self.wgs_region, verbose=self.verbose, outdir=self.cache_dir
        )
        this_wsf.run()

        fr = fetches.fetch_results(this_wsf, check_size=False)
        fr.daemon = True
        fr.start()
        fr.join()
       
        for i, wsf_result in enumerate(this_wsf.results):
            wsf_tif = os.path.join(this_wsf._outdir, wsf_result[1])
            gdalfun.gdal_set_ndv(wsf_tif, 0, verbose=False)
            wsf_ds = gdalfun.gdal_mem_ds(self.ds_config, name='wsf', src_srs=self.wgs_srs)
            gdal.Warp(
                wsf_ds, wsf_tif, dstSRS=self.cst_srs, resampleAlg='cubicspline',
                callback=gdal.TermProgress, srcNodata=0, outputType=gdal.GDT_Float32
            )
            wsf_ds_arr = wsf_ds.GetRasterBand(1).ReadAsArray()
            wsf_ds_arr[wsf_ds_arr != 0 ] = -1
            self.coast_array += wsf_ds_arr
            wsf_ds = wsf_ds_arr = None
            
    def _load_nhd(self):
        """USGS NHD (HIGH-RES U.S. Only)
        Fetch NHD (NHD High/Plus) data from TNM to fill in near-shore areas. 
        High resoultion data varies by location...
        """

        this_tnm = fetches.TheNationalMap(
            src_region=self.wgs_region, verbose=self.verbose, where="Name LIKE '%Hydro%'",
            extents='HU-8 Subbasin,HU-4 Subregion', outdir=self.cache_dir
        )
        this_tnm.run()
        fr = fetches.fetch_results(this_tnm)
        fr.daemon = True
        fr.start()
        fr.join()

        tnm_ds = gdalfun.gdal_mem_ds(self.ds_config, name='nhd', src_srs=self.wgs_srs)
        if len(this_tnm.results) > 0:
            for i, tnm_result in enumerate(this_tnm.results):
                tnm_zip = os.path.join(this_tnm._outdir, tnm_result[1])
                tnm_zips = utils.unzip(tnm_zip, self.cache_dir)
                gdb = '.'.join(tnm_zip.split('.')[:-1]) + '.gdb'
                utils.run_cmd(
                    'ogr2ogr -update -append nhdArea_merge.shp {} NHDArea -where "FType=312 OR FType=336 OR FType=445 OR FType=460 OR FType=537" -clipdst {} 2>/dev/null'.format(
                        gdb, self.p_region.format('ul_lr')
                    ),
                    verbose=self.verbose
                )
                utils.run_cmd(
                    'ogr2ogr -update -append nhdArea_merge.shp {} NHDPlusBurnWaterBody -clipdst {} 2>/dev/null'.format(
                        gdb, self.p_region.format('ul_lr')
                    ),
                    verbose=self.verbose
                )
                utils.run_cmd(
                    'ogr2ogr -update -append nhdArea_merge.shp {} NHDWaterBody -where "FType=493 OR FType=466" -clipdst {} 2>/dev/null'.format(
                        gdb, self.p_region.format('ul_lr')
                    ),
                    verbose=self.verbose
                )

            utils.run_cmd(
                'gdal_rasterize -burn 1 nhdArea_merge.shp nhdArea_merge.tif -te {} -ts {} {} -ot Int32'.format(
                    self.p_region.format('te'),
                    self.ds_config['nx'],
                    self.ds_config['ny'],
                ),
                verbose=self.verbose
            )

            try:
                gdal.Warp(tnm_ds, 'nhdArea_merge.tif', dstSRS=self.cst_srs, resampleAlg=self.sample)
            except:
                tnm_ds = None
                
            if tnm_ds is not None:
                tnm_ds_arr = tnm_ds.GetRasterBand(1).ReadAsArray()
                tnm_ds_arr[tnm_ds_arr < 1] = 0
                self.coast_array -= tnm_ds_arr
                tnm_ds = tnm_ds_arr = None
                
            utils.remove_glob('nhdArea_merge.*')

    def _load_lakes(self):
        """HydroLakes -- Global Lakes"""
        
        this_lakes = fetchesHydroLakes(
            src_region=self.wgs_region, verbose=self.verbose, outdir=self.cache_dir
        )
        this_lakes.run()        
        fr = fetches.fetch_results(this_lakes)
        fr.daemon = True
        fr.start()
        fr.join()
        
        lakes_shp = None
        lakes_zip = os.path.join(this_lakes._outdir, this_lakes.results[0][1])
        lakes_shps = utils.unzip(lakes_zip, self.cache_dir)
        for i in lakes_shps:
            if i.split('.')[-1] == 'shp':
                lakes_shp = i

        lakes_ds = gdalfun.gdal_mem_ds(self.ds_config, name='lakes', src_srs=self.wgs_srs)
        lakes_warp_ds = gdalfun.gdal_mem_ds(self.ds_config, name='lakes', src_srs=self.wgs_srs)
        lk_ds = ogr.Open(lakes_shp)
        if lk_ds is not None:
            lk_layer = lk_ds.GetLayer()
            lk_layer.SetSpatialFilter(self.f_region.export_as_geom())
            gdal.RasterizeLayer(lakes_ds, [1], lk_layer, burn_values=[-1])
            gdal.Warp(lakes_warp_ds, lakes_ds, dstSRS=self.cst_srs, resampleAlg=self.sample)
            lakes_ds_arr = lakes_warp_ds.GetRasterBand(1).ReadAsArray()
            self.coast_array[lakes_ds_arr == -1] = 0
            lakes_ds = lk_ds = lakes_warp_ds = None
        else:
            utils.echo_error_msg('could not open {}'.format(lakes_shp))

    def _load_bldgs(self):
        """load buildings from OSM
        
        OSM has a size limit, so will chunk the region and
        do multiple osm calls
        """

        this_osm = fetches.OpenStreetMap(
            src_region=self.wgs_region, verbose=self.verbose,
            planet=self.want_osm_planet, chunks=True, q='buildings', fmt='osm',
            min_length=self.min_building_length, outdir=self.cache_dir
        )
        this_osm.run()
        os.environ["OGR_OSM_OPTIONS"] = "INTERLEAVED_READING=YES"
        os.environ["OGR_OSM_OPTIONS"] = "OGR_INTERLEAVED_READING=YES"
        # with utils.CliProgress(
        #         total=len(this_osm.results),
        #         message='processing OSM buildings',
        # ) as pbar:
        with tqdm(
                total=len(this_osm.results),
                desc='processing OSM buildings',
        ) as pbar:
            for n, osm_result in enumerate(this_osm.results):
                pbar.update()
                osm_z = os.path.join(this_osm._outdir, osm_result[1])
                if fetches.Fetch(osm_result[0], verbose=True).fetch_file(
                        osm_z, check_size=False, tries=self.osm_tries, read_timeout=3600
                ) >= 0:
                    #if True:
                    if osm_result[-1] == 'bz2':
                        osm_planet = utils.unbz2(osm_z, self.cache_dir)
                        osm_file = utils.ogr_clip(osm_planet, self.wgs_region)
                        _clipped = True
                    elif osm_result[-1] == 'pbf':
                        osm_file = utils.ogr_clip(osm_z, self.wgs_region, 'multipolygons')
                        _clipped = True
                    else:
                        osm_file = osm_z
                        _clipped = False

                    if os.path.getsize(osm_file) == 366:
                        continue

                    out, status = utils.run_cmd(
                        'gdal_rasterize -burn -1 -l multipolygons {} bldg_osm.tif -te {} -ts {} {} -ot Int32 -q'.format(
                            osm_file,
                            self.p_region.format('te'),
                            self.ds_config['nx'],
                            self.ds_config['ny'],
                        ),
                        verbose=False
                    )

                    if status == 0:
                        bldg_ds = gdal.Open('bldg_osm.tif')
                        if bldg_ds is not None:
                            bldg_ds_arr = bldg_ds.GetRasterBand(1).ReadAsArray()
                            self.coast_array[bldg_ds_arr == -1] = 0
                            bldg_ds = bldg_ds_arr = None

                        bldg_ds = None

                    utils.remove_glob('bldg_osm.tif*')

        bldg_ds = bldg_warp_ds = None
        
    def _load_data(self):
        """load data from user datalist and amend coast_array"""

        for this_arr in self.stack_ds.yield_array():
            data_arr = this_arr[0]['z']
            srcwin = this_arr[1]
            data_arr[np.isnan(data_arr)] = 0
            data_arr[data_arr > 0] = 1
            data_arr[data_arr < 0] = -1
            self.coast_array[srcwin[1]:srcwin[1]+srcwin[3],
                             srcwin[0]:srcwin[0]+srcwin[2]] += data_arr

    def _write_coast_array(self):
        """write coast_array to file"""

        if self.verbose:
            utils.echo_msg('writing array to {}.tif...'.format(self.name))
            
        gdalfun.gdal_write(
            self.coast_array, '{}.tif'.format(self.name), self.ds_config,
        )

    def _write_coast_poly(self, poly_count=None):
        """convert to coast_array vector"""

        if self.verbose:
            utils.echo_msg('polygonizing array to {}.shp...'.format(self.name))
        
        poly_count = utils.int_or(poly_count)
        tmp_ds = ogr.GetDriverByName('ESRI Shapefile').CreateDataSource(
            '{}_tmp_c.shp'.format(self.name)
        )
        
        if tmp_ds is not None:
            tmp_layer = tmp_ds.CreateLayer('{}_tmp_c'.format(os.path.basename(self.name)), None, ogr.wkbMultiPolygon)
            tmp_layer.CreateField(ogr.FieldDefn('DN', ogr.OFTInteger))
            gdalfun.gdal_polygonize('{}.tif'.format(self.name), tmp_layer, verbose=self.verbose)
            tmp_ds = None
            
        utils.run_cmd(
            'ogr2ogr -dialect SQLITE -sql "SELECT * FROM {}_tmp_c WHERE DN=0 {}" {}.shp {}_tmp_c.shp'.format(
                os.path.basename(self.name), 'order by ST_AREA(geometry) desc limit {}'.format(poly_count) if poly_count is not None else '', self.name, self.name),
            verbose=self.verbose
        )        
        utils.remove_glob('{}_tmp_c.*'.format(self.name))
        utils.run_cmd(
            'ogrinfo -dialect SQLITE -sql "UPDATE {} SET geometry = ST_MakeValid(geometry)" {}.shp'.format(
                self.name, self.name),
            verbose=self.verbose
        )        
        #print(self.dst_srs)
        #print(self.cst_srs.ExportToProj4())
        gdalfun.osr_prj_file(self.name + '.prj', self.dst_srs)

## ==============================================
##
## Waffles Lakes Bathymetry
##
## ==============================================
class WafflesLakes(Waffle):
    """Estimate lake bathymetry.
    
    By default, will return lake bathymetry as depth values (positive down), 
    to get elevations (positive up), set apply_elevations=True.

    -----------
    Parameters:
    
    apply_elevations=[True/False] - use COPERNICUS to apply lake level elevations to output
    min_area=[val] - minimum lake area to consider
    max_area=[val] - maximum lake area to consider
    min_id=[val] - minimum lake ID to consider
    max_id=[val] - maximum lake ID to consider
    depth=[globathy/hydrolakes/val] - obtain the depth value from GloBathy, HydroLakes or constant value
    
    < lakes:apply_elevations=False:min_area=None:max_area=None:min_id=None:max_id=None:depth=globathy:elevations=copernicus >
    """
    
    def __init__(
            self,
            apply_elevations=False,
            min_area=None,
            max_area=None,
            min_id=None,
            max_id=None,
            depth='globathy',
            elevations='copernicus',
            **kwargs
    ):
        super().__init__(**kwargs)
        self._mask = None
        self.apply_elevations = apply_elevations
        self.min_area = min_area
        self.max_area = max_area
        self.min_id = min_id
        self.max_id = max_id
        self.depth = depth
        self.elevations = elevations
        self.ds_config = None
            
    def _fetch_lakes(self):
        """fetch hydrolakes polygons"""

        this_lakes = fetches.HydroLakes(
            src_region=self.p_region, verbose=self.verbose, outdir=self.cache_dir
        )
        this_lakes.run()
        fr = fetches.fetch_results(this_lakes, check_size=False)
        fr.daemon = True
        fr.start()
        fr.join()

        lakes_shp = None
        lakes_zip = os.path.join(this_lakes._outdir, this_lakes.results[0][1])
        lakes_shps = utils.unzip(lakes_zip, self.cache_dir)
        for i in lakes_shps:
            if i.split('.')[-1] == 'shp':
                lakes_shp = i

        return(lakes_shp)

    def _fetch_globathy(self, ids=[]):
        """fetch globathy csv data and process into dict"""
        
        import csv
        
        _globathy_url = 'https://springernature.figshare.com/ndownloader/files/28919991'
        globathy_zip = os.path.join(self.cache_dir, 'globathy_parameters.zip')
        fetches.Fetch(_globathy_url, verbose=self.verbose).fetch_file(globathy_zip, check_size=False)
        globathy_csvs = utils.unzip(globathy_zip, self.cache_dir)        
        globathy_csv = os.path.join(self.cache_dir, 'GLOBathy_basic_parameters/GLOBathy_basic_parameters(ALL_LAKES).csv')
        with open(globathy_csv, mode='r') as globc:
            reader = csv.reader(globc)
            next(reader)
            if len(ids) > 0:
                globd = {}
                for row in reader:
                    if int(row[0]) in ids:
                        globd[int(row[0])] = float(row[-1])
                        ids.remove(int(row[0]))
                        
                    if len(ids) == 0:
                        break
            else:
                globd = {int(row[0]):float(row[-1]) for row in reader}

        return(globd)

    def _fetch_gmrt(self, gmrt_region=None):
        """GMRT - Global low-res.
        """

        if gmrt_region is None:
            gmrt_region = self.p_region
        
        this_gmrt = fetches.GMRT(
            src_region=gmrt_region, verbose=self.verbose, layer='topo', outdir=self.cache_dir
        )
        this_gmrt.run()

        fr = fetches.fetch_results(this_gmrt)
        fr.daemon = True
        fr.start()
        fr.join()

        dst_srs = osr.SpatialReference()
        dst_srs.SetFromUserInput(self.dst_srs)
        
        gmrt_tif = os.path.join(this_gmrt._outdir, this_gmrt.results[1])
        gmrt_ds = gdalfun.gdal_mem_ds(self.ds_config, name='gmrt')
        gdal.Warp(gmrt_ds, gmrt_tif, dstSRS=dst_srs, resampleAlg=self.sample)
        return(gmrt_ds)
    
    def _fetch_copernicus(self, cop_region=None):
        """copernicus"""

        if cop_region is None:
            cop_region = self.p_region
            
        this_cop = fetches.CopernicusDEM(
            src_region=cop_region, verbose=self.verbose, datatype='1', outdir=self.cache_dir
        )
        this_cop.run()

        fr = fetches.fetch_results(this_cop, check_size=False)
        fr.daemon = True
        fr.start()
        fr.join()

        dst_srs = osr.SpatialReference()
        dst_srs.SetFromUserInput(self.dst_srs)
        
        cop_ds = gdalfun.gdal_mem_ds(self.ds_config, name='copernicus')
        [gdal.Warp(cop_ds, os.path.join(this_cop._outdir, cop_result[1]), dstSRS=dst_srs, resampleAlg=self.sample) for cop_result in this_cop.results]
        
        return(cop_ds)
    
    def generate_mem_ogr(self, geom, srs):
        """Create temporary polygon vector layer from feature geometry 
        so that we can rasterize it (Rasterize needs a layer)
        """
        
        ds = ogr.GetDriverByName('MEMORY').CreateDataSource('')
        Layer = ds.CreateLayer('', geom_type=ogr.wkbPolygon, srs=srs)
        outfeature = ogr.Feature(Layer.GetLayerDefn())
        outfeature.SetGeometry(geom)
        Layer.SetFeature(outfeature)

        return(ds)

    ##
    ## Adapted from GLOBathy
    def apply_calculation(self, shore_distance_arr, lake_depths_arr, shore_arr=None):
        """
        Apply distance calculation, which is each pixel's distance to shore, multiplied
        by the maximum depth, all divided by the maximum distance to shore. This provides
        a smooth slope from shore to lake max depth.

        shore_distance - Input numpy array containing array of distances to shoreline.
            Must contain positive values for distance away from shore and 0 elsewhere.
        max_depth - Input value with maximum lake depth.
        NoDataVal - value to assign to all non-lake pixels (zero distance to shore).
        """

        labels, nfeatures = ndimage.label(shore_distance_arr)
        nfeatures = np.arange(1, nfeatures +1)
        maxes = ndimage.maximum(shore_distance_arr, labels, nfeatures)
        max_dist_arr = np.zeros(np.shape(shore_distance_arr))
        # with utils.CliProgress(
        #         total=len(nfeatures),
        #         message='applying labels...',
        # ) as pbar:
        with tqdm(
                total=len(nfeatures),
                desc='applying labels.',
        ) as pbar:
            for n, x in enumerate(nfeatures):
                pbar.update()
                max_dist_arr[labels==x] = maxes[x-1]
        
        max_dist_arr[max_dist_arr == 0] = np.nan
        bathy_arr = (shore_distance_arr * lake_depths_arr) / max_dist_arr
        bathy_arr[bathy_arr == 0] = np.nan

        if shore_arr is None \
           or shore_arr.size == 0 \
           or shore_arr[~np.isnan(bathy_arr)].size == 0 \
           or shore_arr[~np.isnan(bathy_arr)].max() == 0:
            utils.echo_warning_msg('invalid shore array, using default shore value of zero')
            bathy_arr = 0 - bathy_arr
        else:
            bathy_arr = shore_arr - bathy_arr

        utils.echo_msg('applied shore elevations to lake depths')
        bathy_arr[np.isnan(bathy_arr)] = 0
        return(bathy_arr)    
    
    def run(self):
        self.wgs_region = self.p_region.copy()
        if self.dst_srs is not None:
            self.wgs_region.warp('epsg:4326')            
        else:
            self.dst_srs = 'epsg:4326'
        
        #self.p_region.buffer(pct=2)
        
        lakes_shp = self._fetch_lakes()
        lk_ds = ogr.Open(lakes_shp, 1)
        lk_layer = lk_ds.GetLayer()

        ## filter layer to region
        filter_region = self.p_region.copy()
        lk_layer.SetSpatialFilter(filter_region.export_as_geom())

        ## filter by ID
        if self.max_id is not None:
            lk_layer.SetAttributeFilter('Hylak_id < {}'.format(self.max_id))
            
        if self.min_id is not None:
            lk_layer.SetAttributeFilter('Hylak_id > {}'.format(self.min_id))

        ## filter by Area
        if self.max_area is not None:
            lk_layer.SetAttributeFilter('Lake_area < {}'.format(self.max_area))
            
        if self.min_area is not None:
            lk_layer.SetAttributeFilter('Lake_area > {}'.format(self.min_area))
            
        lk_features = lk_layer.GetFeatureCount()
        if lk_features == 0:
            utils.echo_error_msg('no lakes found in region')
            return(self)
        
        ## get lake ids and globathy depths
        lk_ids = []
        [lk_ids.append(feat.GetField('Hylak_id')) for feat in lk_layer]
        utils.echo_msg('using Lake IDS: {}'.format(lk_ids))
        
        lk_regions = self.p_region.copy()
        # with utils.CliProgress(
        #         total=len(lk_layer),
        #         message='processing {} lakes'.format(lk_features),
        # ) as pbar:
        with tqdm(
                total=len(lk_layer),
                desc='processing {} lakes'.format(lk_features),
        ) as pbar:            
            for lk_f in lk_layer:
                pbar.update()
                this_region = regions.Region()
                lk_geom = lk_f.GetGeometryRef()
                lk_wkt = lk_geom.ExportToWkt()
                this_region.from_list(ogr.CreateGeometryFromWkt(lk_wkt).GetEnvelope())
                lk_regions = regions.regions_merge(lk_regions, this_region)

        while not regions.regions_within_ogr_p(self.p_region, lk_regions):
            utils.echo_msg('buffering region by 2 percent to gather all lake boundaries...')
            self.p_region.buffer(pct=2, x_inc=self.xinc, y_inc=self.yinc)

        ## fetch and initialize the copernicus data
        if self.elevations == 'copernicus':
            cop_ds = self._fetch_copernicus(cop_region=self.p_region)
            cop_band = cop_ds.GetRasterBand(1)
        elif self.elevations == 'gmrt':
            cop_ds = self._fetch_gmrt(gmrt_region=self.p_region)
            cop_band = cop_ds.GetRasterBand(1)
        elif utils.float_or(self.elevations) is not None:
            cop_band = None
            cop_arr = np.zeros((self.ds_config['nx'], self.ds_config['ny']))
            cop_arr[:] = self.elevations
        elif self.elevations == 'self':
            elev_ds = self.stacked_rasters['z']
            if elev_ds is not None:
                dst_srs = osr.SpatialReference()
                dst_srs.SetFromUserInput(self.dst_srs)
                cop_ds = gdalfun.gdal_mem_ds(self.ds_config, name='cop')
                gdal.Warp(cop_ds, elev_ds, dstSRS=dst_srs, resampleAlg=self.sample)
                cop_band = cop_ds.GetRasterBand(1)
            else:
                cop_band = None
                cop_arr = None
        elif os.path.exists(self.elevations):
            elev_ds = gdal.Open(self.elevations)
            if elev_ds is not None:
                dst_srs = osr.SpatialReference()
                dst_srs.SetFromUserInput(self.dst_srs)
                cop_ds = gdalfun.gdal_mem_ds(self.ds_config, name='cop')
                gdal.Warp(cop_ds, elev_ds, dstSRS=dst_srs, resampleAlg=self.sample)
                cop_band = cop_ds.GetRasterBand(1)
            else:
                cop_band = None
                cop_arr = None
        else:
            cop_band = None
            cop_arr = None
        
        ## initialize the tmp datasources
        prox_ds = gdalfun.gdal_mem_ds(self.ds_config, name='prox')
        msk_ds = gdalfun.gdal_mem_ds(self.ds_config, name='msk')
        msk_band = None
        globd = None
        
        if len(lk_ids) == 0:
            return(self)
        
        if self.depth == 'globathy':
            globd = self._fetch_globathy(ids=lk_ids[:])
            ## rasterize hydrolakes using id
            gdal.RasterizeLayer(msk_ds, [1], lk_layer, options=["ATTRIBUTE=Hylak_id"], callback=gdal.TermProgress)
            msk_ds.FlushCache()
            msk_band = msk_ds.GetRasterBand(1)
            msk_band.SetNoDataValue(self.ds_config['ndv'])

            ## assign max depth from globathy
            msk_arr = msk_band.ReadAsArray()
            # with utils.CliProgress(
            #         total=len(lk_ids),
            #         message='Assigning Globathy Depths to rasterized lakes...',
            # ) as pbar:
            with tqdm(
                    total=len(lk_ids),
                    desc='Assigning Globathy Depths to rasterized lakes...',
            ) as pbar:
                
                for n, this_id in enumerate(lk_ids):
                    depth = globd[this_id]
                    msk_arr[msk_arr == this_id] = depth
                    pbar.update()
            
        elif self.depth == 'hydrolakes':
            gdal.RasterizeLayer(msk_ds, [1], lk_layer, options=["ATTRIBUTE=Depth_avg"], callback=gdal.TermProgress)
            msk_ds.FlushCache()
            msk_band = msk_ds.GetRasterBand(1)
            msk_band.SetNoDataValue(self.ds_config['ndv'])
            msk_arr = msk_band.ReadAsArray()
            
        elif utils.float_or(self.depth) is not None:
            msk_arr = np.zeros((self.ds_config['nx'], self.ds_config['ny']))
            msk_arr[:] = self.depth
            
        else:            
            msk_arr = np.ones((self.ds_config['nx'], self.ds_config['ny']))
            
        ## calculate proximity of lake cells to shore
        if msk_band is None:
            gdal.RasterizeLayer(msk_ds, [1], lk_layer, options=["ATTRIBUTE=Hylak_id"], callback=gdal.TermProgress)
            msk_ds.FlushCache()
            msk_band = msk_ds.GetRasterBand(1)
            msk_band.SetNoDataValue(self.ds_config['ndv'])
            
        lk_ds = None
        prox_band = prox_ds.GetRasterBand(1)
        proximity_options = ["VALUES=0", "DISTUNITS=PIXEL"]
        gdal.ComputeProximity(msk_band, prox_band, options=proximity_options, callback=gdal.TermProgress)        
        prox_arr = prox_band.ReadAsArray()
        if cop_band is not None:
            cop_arr = cop_band.ReadAsArray()

        utils.echo_msg('Calculating simulated lake depths...')
        ## apply calculation from globathy
        bathy_arr = self.apply_calculation(
            prox_arr,
            msk_arr,
            shore_arr=cop_arr,
        )

        bathy_arr[bathy_arr == 0] = self.ndv        
        gdalfun.gdal_write(
            bathy_arr, '{}.tif'.format(self.name), self.ds_config,
        )            

        prox_ds = msk_ds = cop_ds = None
        return(self)

## ==============================================
##
## Waffles 'CUDEM' gridding
##
## combined gridding method (stacks/surface/coastline/IDW)
##
## ==============================================
class WafflesCUDEM(Waffle):
    """CUDEM integrated DEM generation.
    
    Generate an topo/bathy integrated DEM using a variety of data sources.
    Will iterate <pre_count> pre-surfaces at lower-resolutions.
    Each pre-surface will be clipped to <landmask> if it exists and smoothed with <smoothing> factor.
    Each pre-surface is used in subsequent pre-surface(s)/final DEM at each iterative weight.

    generate a DEM with `pre_surface`s which are generated
    at lower resolution and with various weight threshholds.

    To generate a typical CUDEM tile, generate 1 pre-surface ('bathy_surface'), clipped to a coastline.
    Use a min_weight that excludes low-resolution bathymetry data from being used as input in the final
    DEM generation. 

    -----------
    Parameters:
    
    landmask (bool): path to coastline vector mask or set as `coastline` to auto-generate
    min_weight (float): the minumum weight to include in the final DEM
    pre_count (int): number of pre-surface iterations to perform
    pre_upper_limit (float) - the upper elevation limit of the pre-surfaces (used with landmask)
    poly_count (int) - the number of polygons (from largest to smallest) to extract from the landmask
    mode (str) - the waffles module to perform the initial pre-surface
    """
    
    def __init__(self, min_weight=None, pre_count = 1, pre_upper_limit = -0.1, landmask = False,
                 poly_count = True, mode = 'gmt-surface', filter_outliers = None, **kwargs):
        super().__init__(**kwargs)
        self.min_weight = utils.float_or(min_weight)
        self.pre_count = utils.int_or(pre_count, 1)
        self.landmask = landmask
        self.pre_upper_limit = utils.float_or(pre_upper_limit, -0.1) if landmask else None
        self.poly_count = poly_count
        self.mode = mode
        if self.mode not in ['gmt-surface', 'IDW']:
            self.mode = 'IDW'
            
        self.filter_outliers = utils.int_or(filter_outliers)
        #if self.filter_outliers is not None:
        #    self.filter_outliers = 1 if self.filter_outliers > 9 or self.filter_outliers < 1 else self.filter_outliers

    ## todo: remove coastline after processing...
    def generate_coastline(self, pre_data=None):
        cst_region = self.p_region.copy()
        cst_region.wmin = self.min_weight
        cst_fn = '{}_cst'.format(os.path.join(self.cache_dir, os.path.basename(self.name)))
        coastline = WaffleFactory(mod='coastline', polygonize=self.poly_count, data=pre_data, src_region=cst_region,
                                  xinc=self.xinc, yinc=self.yinc, name=cst_fn, node=self.node, dst_srs=self.dst_srs,
                                  srs_transform=self.srs_transform, clobber=True, verbose=False)._acquire_module()
        coastline.initialize()
        # with utils.CliProgress(message='Generating coastline {}...'.format(cst_fn),
        #                        end_message='Generated coastline {}.'.format(cst_fn),
        #                        verbose=self.verbose) as pbar:
        with tqdm(desc='generating coastline {}...'.format(cst_fn)) as pbar:
            coastline.generate()

        if coastline is not None:
            return('{}.shp:invert=True'.format(coastline.name))
        else:
            return(None)
            
    def run(self):
        pre = self.pre_count
        pre_weight = 0
        final_region = self.d_region.copy()
        pre_region = self.p_region.copy()
        pre_region.wmin = None
        if self.min_weight is None:
            self.min_weight = gdalfun.gdal_percentile(self.stack, perc=75, band=3)
        
        if self.verbose:
            utils.echo_msg('cudem min weight is: {}'.format(self.min_weight))

        ## Remove outliers from the stacked data
        if self.filter_outliers is not None:
            gdalfun.gdal_filter_outliers(
                self.stack, None, replace=False
            )
        #os.replace('_tmp_fltr.tif', self.stack)
        # demfun.mask_(w, n, '_tmp_w.tif', verbose=self.verbose)
            # os.replace('_tmp_w.tif', w)
            # demfun.mask_(c, n, '_tmp_c.tif', verbose=self.verbose)
            # os.replace('_tmp_c.tif', c)
            
        ## initial data to pass through surface (stack)
        stack_data_entry = '{},200:band_no=1:weight_mask=3:uncertainty_mask=4:sample=average,1'.format(self.stack)
        pre_data = [stack_data_entry]
        ## generate coastline
        if self.landmask:
            coastline = self.generate_coastline(pre_data=pre_data)
            #if coastline is not None:
            pre_clip = coastline
            #print(pre_clip)

        ## Grid/Stack the data `pre` times concluding in full resolution @ min_weight
        while pre >= 0:
            pre_xinc = float(self.xinc * (3**pre))
            pre_yinc = float(self.yinc * (3**pre))
            xsample = self.xinc * (3**(pre - 1))
            ysample = self.yinc * (3**(pre - 1))
            if xsample == 0:
                xsample = self.xinc
                
            if ysample == 0:
                ysample = self.yinc
                
            ## if not final output, setup the configuration for the pre-surface
            if pre != self.pre_count:
                pre_weight = self.min_weight/(pre + 1) if pre > 0 else self.min_weight
                if pre_weight == 0:
                    pre_weight = 1-e20
                    
                _pre_name_plus = os.path.join(self.cache_dir, utils.append_fn('_pre_surface', pre_region, pre+1))
                pre_data_entry = '{}.tif,200:sample=cubicspline:check_path=True,{}'.format(_pre_name_plus, pre_weight)
                pre_data = [stack_data_entry, pre_data_entry]
                pre_region.wmin = pre_weight

            ## reset pre_region for final grid
            if pre == 0:
                pre_region = self.p_region.copy()

            _pre_name = os.path.join(self.cache_dir, utils.append_fn('_pre_surface', pre_region, pre))
            if self.verbose:
                utils.echo_msg('pre region: {}'.format(pre_region))
                
            waffles_mod = self.mode if pre==self.pre_count else 'stacks' if pre != 0 else 'IDW:radius=10'
            #'linear:chunk_step=None:chunk_buffer=10'
            pre_surface = WaffleFactory(mod = waffles_mod, data=pre_data, src_region=pre_region, xinc=pre_xinc if pre !=0 else self.xinc,
                                        yinc=pre_yinc if pre !=0 else self.yinc, xsample=self.xinc if pre !=0 else None, ysample=self.yinc if pre != 0 else None,
                                        name=_pre_name, node=self.node, want_weight=True, want_uncertainty=self.want_uncertainty,
                                        dst_srs=self.dst_srs, srs_transform=self.srs_transform, clobber=True, verbose=self.verbose,
                                        clip=pre_clip if pre !=0 else None, supercede=True if pre == 0 else self.supercede,
                                        upper_limit=self.pre_upper_limit if pre != 0 else None, keep_auxiliary=False)._acquire_module()
            pre_surface.initialize()
            pre_surface.generate()
            pre -= 1

        #os.replace(pre_surface.fn, self.fn)
        gdalfun.gdal_hydro_flatten(pre_surface.fn, dst_dem=self.fn, band=1, size_threshold=10)
        return(self)

## ==============================================
##
## Waffles TESTING
##
## ==============================================
class WafflesCUBE(Waffle):
    """
    BathyCUBE - doesn't seem to work as expected, likely doing something wrong here...

    https://github.com/noaa-ocs-hydrography/bathygrid
    
    """
    
    def __init__(
            self,
            chunk_size=None,
            chunk_buffer=40,
            **kwargs):
        """generate a `CUBE` dem"""
        
        super().__init__(**kwargs)
        self.chunk_size = utils.int_or(chunk_size)
        self.chunk_step = None
        self.chunk_buffer = chunk_buffer
        
    def run(self):

        try:
            import bathycube.cube as cube
        except:
            utils.echo_error_msg('could not import bathycube, it may not be installed')
            return(self)

        from scipy import optimize
        
        # _x = []
        # _y = []
        # _z = []
        # for this_xyz in self.yield_xyz():
        #     _x.append(this_xyz.x)
        #     _y.append(this_xyz.y)
        #     _z.append(this_xyz.z)

        # _x = np.array(_x)
        # _y = np.array(_y)
        # _z = np.array(_z)

        # print(_z.size)
        # print(_z.shape)
        # print(len(_z))
        
        if self.chunk_size is None:
            n_chunk = int(self.ds_config['nx'] * .1)
            n_chunk = 10 if n_chunk < 10 else n_chunk
        else: n_chunk = self.chunk_size
        if self.chunk_step is None:
            n_step = int(n_chunk/2)
        else: n_step = self.chunk_step

        points_ds = gdal.Open(self.stacked_rasters['z'])
        points_band = points_ds.GetRasterBand(1)
        points_no_data = points_band.GetNoDataValue()
        
        #try:
        interp_ds = points_ds.GetDriver().Create(
            self.fn, points_ds.RasterXSize, points_ds.RasterYSize, bands=1, eType=points_band.DataType,
            options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        )
        # driver = gdal.GetDriverByName('GTiff')
        # interp_ds = driver.Create(
        #     self.fn, ds_config['nx'], ds_config['ny'], bands=1, eType=ds_config['dt'],
        #     options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        # )

        interp_ds.SetProjection(points_ds.GetProjection())
        interp_ds.SetGeoTransform(points_ds.GetGeoTransform())
        #interp_ds.SetGeoTransform(ds_config['geoT'])
        interp_band = interp_ds.GetRasterBand(1)
        interp_band.SetNoDataValue(np.nan)

        uncert_ds = points_ds.GetDriver().Create(
            '{}_unc.tif'.format(self.name), points_ds.RasterXSize, points_ds.RasterYSize, bands=1, eType=points_band.DataType,
            options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        )

        # uncert_ds = driver.Create(
        #     self.fn, ds_config['nx'], ds_config['ny'], bands=1, eType=ds_config['dt'],
        #     options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        # )
        uncert_ds.SetProjection(points_ds.GetProjection())
        uncert_ds.SetGeoTransform(points_ds.GetGeoTransform())
        #uncert_ds.SetGeoTransform(ds_config['geoT'])
        uncert_band = uncert_ds.GetRasterBand(1)
        uncert_band.SetNoDataValue(np.nan)
            
        # except:
        #     return(self)
        
        if self.verbose:
            utils.echo_msg('buffering srcwin by {} pixels'.format(self.chunk_buffer))

        _x, _y = np.mgrid[0:ds_config['nx'], 0:ds_config['ny']]
        _x = _x.ravel()
        _y = _y.ravel()

        _z = points_band.ReadAsArray()
        _z = _z.T
        _z = _z.ravel()
        point_indices = np.nonzero(_z != ds_config['ndv'])
        point_values = _z[point_indices]        
        xi = _x[point_indices]
        yi = _y[point_indices]

        thu = np.ones(len(point_values))
        thu[:] = 2
        #thu = thu[point_indices]
        a = .25
        b = .0075
        tvu = np.sqrt((a**2 + (b * abs(point_values))**2))
        
        #tvu = np.random.uniform(low=.1, high=.3, size=ds_config['nb'])
        #thu = np.random.uniform(low=.3, high=1.3, size=ds_config['nb'])
        
        #tvu = (np.random.uniform(low=.1, high=np.std(point_values), size=ds_config['nb']))
        #print(tvu)

        #print(np.std(point_values, axis=0))
        #print([point_values.mean() - 3 * point_values.std(), point_values.mean() + 3 * point_values.std()])

        #thu = np.linspace(point_values.mean() - 3 * point_values.std(), point_values.mean() + 3 * point_values.std(), ds_config['nb'])
        
        #tvu = np.linspace(0.2, .3, ds_config['nb'])
        #thu = np.linspace(0.2, .3, ds_config['nb'])
        #tvu = np.ones(ds_config['nb'])
        #thu = np.ones(ds_config['nb'])
        #tvui = tvu[point_indices]
        #thui = thu[point_indices]
        #_z[:] = _z*-1
        # print(_x)
        # print(_y)
        # print(_z)
        # print(thu)
        # print(tvu)
        numrows, numcols = (ds_config['nx'], ds_config['ny'])
        res_x, res_y = ds_config['geoT'][1], ds_config['geoT'][5]*-1
        depth_grid, uncertainty_grid, ratio_grid, numhyp_grid = cube.run_cube_gridding(
            point_values, thu, tvu, xi, yi, self.ds_config['nx'], self.ds_config['ny'],
            min(_x), max(_y), 'local', 'order1a', 1, 1)
        print(depth_grid)
        depth_grid = np.flip(depth_grid)
        depth_grid = np.fliplr(depth_grid)
        interp_band.WriteArray(depth_grid)
        uncertainty_grid = np.flip(uncertainty_grid)
        uncertainty_grid = np.fliplr(uncertainty_grid)
        uncert_band.WriteArray(uncertainty_grid)
            
        return(self)

class WafflesBGrid(Waffle):
    """
    BathyCUBE - doesn't seem to work as expected, likely doing something wrong here...

    https://github.com/noaa-ocs-hydrography/bathygrid
    
    """
    
    def __init__(
            self,
            chunk_size=None,
            chunk_buffer=40,
            **kwargs):
        """generate a `CUBE` dem"""
        
        super().__init__(**kwargs)
        self.chunk_size = utils.int_or(chunk_size)
        self.chunk_step = None
        self.chunk_buffer = chunk_buffer
        
    def run(self):

        try:
            import bathycube.cube as cube
        except:
            utils.echo_error_msg('could not import bathycube, it may not be installed')
            return(self)

        from scipy import optimize
        
        # self._stacks_array(
        #     out_name='{}_cube_stack'.format(self.name),
        #     supercede=self.supercede
        # )
        # n = '{}_cube_stack_s.tif'.format(self.name)
        # w = '{}_cube_stack_w.tif'.format(self.name)
        # c = '{}_cube_stack_c.tif'.format(self.name)
        _x = []
        _y = []
        _z = []
        for this_xyz in self.yield_xyz():
            _x.append(this_xyz.x)
            _y.append(this_xyz.y)
            _z.append(this_xyz.z)

        _x = np.array(_x)
        _y = np.array(_y)
        _z = np.array(_z)

        dtyp = [('x', np.float64), ('y', np.float64), ('z', np.float32), ('tvu', np.float32), ('thu', np.float32)]
        data = np.empty(len(_x), dtype=dtyp)
        data['x'] = _x
        data['y'] = _y
        data['z'] = _z
        
        # print(_z.size)
        # print(_z.shape)
        # print(len(_z))
        
        if self.chunk_size is None:
            n_chunk = int(ds_config['nx'] * .1)
            n_chunk = 10 if n_chunk < 10 else n_chunk
        else: n_chunk = self.chunk_size
        if self.chunk_step is None:
            n_step = int(n_chunk/2)
        else: n_step = self.chunk_step

        # points_ds = gdal.Open(n)
        # points_band = points_ds.GetRasterBand(1)
        # points_no_data = points_band.GetNoDataValue()
        
        #try:
        # interp_ds = points_ds.GetDriver().Create(
        #     self.fn, points_ds.RasterXSize, points_ds.RasterYSize, bands=1, eType=points_band.DataType,
        #     options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        # )
        # driver = gdal.GetDriverByName('GTiff')
        # interp_ds = driver.Create(
        #     self.fn, ds_config['nx'], ds_config['ny'], bands=1, eType=ds_config['dt'],
        #     options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        # )

        # #interp_ds.SetProjection(points_ds.GetProjection())
        # #interp_ds.SetGeoTransform(points_ds.GetGeoTransform())
        # #interp_ds.SetGeoTransform(ds_config['geoT'])
        # interp_band = interp_ds.GetRasterBand(1)
        # interp_band.SetNoDataValue(np.nan)

        # # uncert_ds = points_ds.GetDriver().Create(
        # #     '{}_unc.tif'.format(self.name), points_ds.RasterXSize, points_ds.RasterYSize, bands=1, eType=points_band.DataType,
        # #     options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        # # )

        # uncert_ds = driver.Create(
        #     self.fn, ds_config['nx'], ds_config['ny'], bands=1, eType=ds_config['dt'],
        #     options=["BLOCKXSIZE=256", "BLOCKYSIZE=256", "TILED=YES", "COMPRESS=LZW", "BIGTIFF=YES"]
        # )
        # #uncert_ds.SetProjection(points_ds.GetProjection())
        # #uncert_ds.SetGeoTransform(points_ds.GetGeoTransform())
        # #uncert_ds.SetGeoTransform(ds_config['geoT'])
        # uncert_band = uncert_ds.GetRasterBand(1)
        # uncert_band.SetNoDataValue(np.nan)
            
        # except:
        #     return(self)
        
        if self.verbose:
            utils.echo_msg('buffering srcwin by {} pixels'.format(self.chunk_buffer))

        # _x, _y = np.mgrid[0:ds_config['nx'], 0:ds_config['ny']]
        # _x = _x.flatten()
        # _y = _y.flatten()

        # _z = points_band.ReadAsArray()
        # _z = _z.T
        # _z = _z.ravel()
        # point_indices = np.nonzero(_z != ds_config['ndv'])
        # point_values = _z[point_indices]        
        # xi = _x[point_indices]
        # yi = _y[point_indices]

        thu = np.ones(len(_z))
        thu[:] = 2
        #thu = thu[point_indices]
        a = .25
        b = .0075
        tvu = np.sqrt((a**2 + (b * abs(_z))**2))

        data['tvu'] = tvu
        data['thu'] = thu
        #tvu = np.random.uniform(low=.1, high=.3, size=ds_config['nb'])
        #thu = np.random.uniform(low=.3, high=1.3, size=ds_config['nb'])
        
        #tvu = (np.random.uniform(low=.1, high=np.std(point_values), size=ds_config['nb']))
        #print(tvu)

        #print(np.std(point_values, axis=0))
        #print([point_values.mean() - 3 * point_values.std(), point_values.mean() + 3 * point_values.std()])

        #thu = np.linspace(point_values.mean() - 3 * point_values.std(), point_values.mean() + 3 * point_values.std(), ds_config['nb'])
        
        #tvu = np.linspace(0.2, .3, ds_config['nb'])
        #thu = np.linspace(0.2, .3, ds_config['nb'])
        #tvu = np.ones(ds_config['nb'])
        #thu = np.ones(ds_config['nb'])
        #tvui = tvu[point_indices]
        #thui = thu[point_indices]
        #_z[:] = _z*-1
        # print(_x)
        # print(_y)
        # print(_z)
        # print(thu)
        # print(tvu)
        numrows, numcols = (ds_config['nx'], ds_config['ny'])
        res_x, res_y = ds_config['geoT'][1], ds_config['geoT'][5]*-1


        from bathygrid.convenience import create_grid
        # a single resolution grid that is entirely within computer memory
        bg = create_grid(folder_path='./', grid_type='single_resolution')
        # add points from two multibeam lines, EPSG:26917 with vertical reference 'waterline'
        bg.add_points(data, 'test1', ['line1', 'line2'], 26965, 'waterline')
        print(bg.points_count)
        assert not bg.is_empty

        # grid by looking up the mean depth of each tile to determine resolution
        bg.grid()

        print(bg.resolutions)
        out_tif = os.path.join(bg.output_folder, self.fn)
        bg.export(out_tif, export_format='geotiff')
        
        # # check to see if the new bags are written
        # new_bag = os.path.join(bg.output_folder, 'outtiff_0.5.bag')
        # new_bag_two = os.path.join(bg.output_folder, 'outtiff_1.0.bag')
        # assert os.path.exists(new_bag)
        # assert os.path.exists(new_bag_two)

        # # Get the total number of cells in the variable resolution grid for each resolution
        # bg.cell_count
        # bg.coverage_area
        
        # depth_grid, uncertainty_grid, ratio_grid, numhyp_grid = cube.run_cube_gridding(
        #     point_values,
        #     thu,
        #     tvu,
        #     xi,
        #     yi,
        #     ds_config['nx'],
        #     ds_config['ny'],
        #     min(_x),
        #     max(_y),
        #     'local',
        #     'order1a',
        #     1,
        #     1,
        # )
        # print(depth_grid)
        # depth_grid = np.flip(depth_grid)
        # depth_grid = np.fliplr(depth_grid)
        # interp_band.WriteArray(depth_grid)
        # uncertainty_grid = np.flip(uncertainty_grid)
        # uncertainty_grid = np.fliplr(uncertainty_grid)
        # uncert_band.WriteArray(uncertainty_grid)
            
        return(self)

class WafflesPatch(Waffle):
    """PATCH an existing DEM with new data.
    
    Patch an existing DEM with data from the datalist.

    -----------
    Parameters:
    
    dem=[path] - the path the the DEM to update
    min_weight=[val] - the minumum data weight to include in the patched DEM
    """
    
    def __init__(
            self,
            radius=None,
            min_weight=1,
            max_diff=.25,
            dem=None,
            **kwargs
    ):
        super().__init__(**kwargs)
        self.radius = utils.str2inc(radius)
        self.min_weight = utils.float_or(min_weight)
        self.max_diff = utils.float_or(max_diff)
        self.dem = dem

    def yield_diff(self, src_dem, max_diff=.25):
        '''query a gdal-compatible grid file with xyz data.
        out_form dictates return values

        yields out_form results'''

        def con_dec(x, dec):
            '''Return a float string with n decimals
            (used for ascii output).'''

            if x is None:
                utils.echo_error_msg('Attempting to convert a None value.')
                return
            fstr = "%." + str(dec) + "f"
            return fstr % x
      
        try:
            ds = gdal.Open(src_dem)
        except: ds = None
        if ds is not None:
            ds_config = gdalfun.gdal_infos(ds)
            ds_band = ds.GetRasterBand(1)
            ds_gt = ds_config['geoT']
            ds_nd = ds_config['ndv']
            tgrid = ds_band.ReadAsArray()
            dsband = ds = None

            for xyz in self.yield_xyz():
                #if xyz.x > ds_gt[0] and xyz.y < float(ds_gt[3]):
                try: 
                    xpos, ypos = utils._geo2pixel(xyz.x, xyz.y, ds_gt, 'pixel')
                    g = tgrid[ypos, xpos]
                except: g = ds_nd
                
                if g != ds_nd:
                    d = xyz.z - g
                    s = math.fabs(d / (xyz.z + (g+0.00000001)))

                    if s < max_diff:
                        xyz.z = d
                        yield(xyz)
            ds = None
        
    def query_dump(self, dst_port=sys.stdout, encode=False,  max_diff=.25, **kwargs):
        for xyz in self.yield_diff(self.dem, max_diff):
            xyz.dump(
                include_w = self.want_weight,
                include_u = self.want_uncertainty,
                dst_port=dst_port,
                encode=encode,
                **kwargs
            )
        
    def run(self):
        if dem is not None:
            if os.path.exists(dem):
                self.dem = dem
        elif os.path.exists('{}.tif'.format(self.name)):
            self.dem = '{}.tif'.format(self.name)
            self.name = '{}_update'.format(self.name)
        else:
            utils.echo_error_msg('must specify DEM to patch (:dem=fn) to run the patch module.')
            return(None)

        dem_infos = gdalfun.gdal_infos(self.dem)
        dem_region = regions.Region().from_geo_transform(geo_transform=dem_infos['geoT'], x_count=dem_infos['nx'], y_count=dem_infos['ny'])

        if not regions.regions_intersect_p(self.region, dem_region):
            utils.echo_error_msg('input region does not intersect with input DEM')


        # grid the difference to array using query_dump / num
        # polygonize the differences and add small buffer (1% or so)
        # make zero array, inverse clipped to buffered polygonized diffs
        # surface zero array and diffs...
        # add surfaced diffs to self.dem
                        
        # diff_cmd = 'gmt blockmedian {region} -I{xinc}/{yinc} | gmt surface {region} -I{xinc}/{yinc} -G_diff.tif=gd+n-9999:GTiff -T.1 -Z1.2 -V -rp -C.5 -Lud -Lld -M{radius}'.format(
        #     region=dem_region, xinc=dem_infos['geoT'][1], yinc=-1*dem_infos['geoT'][5], radius=self.radius
        # )
        diff_cmd = 'gmt blockmedian {region} -I{xinc}/{yinc} | gmt surface {region} -I{xinc}/{yinc} -G_diff.tif=gd+n{ndv}:GTiff -T.1 -Z1.2 -V -Lud -Lld'.format(
            region=self.region.format('gmt'), xinc=self.xinc, yinc=self.yinc, ndv=self.ndv, radius=self.radius
        )

        out, status = utils.run_cmd(
            diff_cmd,
            verbose=self.verbose,
            data_fun=lambda p: self.query_dump(
                dst_port=p, encode=True, max_diff=self.max_diff
            )
        )

        utils.echo_msg('smoothing diff grid...')
        smooth_dem, status = gdalfun.gdal_blur('_diff.tif', '_tmp_smooth.tif', 5)

        if self.xinc != dem_infos['geoT']:
            utils.echo_msg('resampling diff grid...')
            if gdalfun.sample_warp('_diff.tif', '__tmp_sample.tif', dem_infos['geoT'][1], -1*dem_infos['geoT'][5],
                                   src_region=dem_region, verbose=self.verbose)[1] == 0:
                os.replace('__tmp_sample.tif', '_tmp_smooth.tif')
            else:
                utils.echo_warning_msg('failed to resample diff grid')

        #if self.xinc != dem_infos['geoT']:
        if self.verbose:
            utils.echo_msg('resampling diff grid...')
            
        diff_ds = gdalfun.sample_warp(
            '_diff.tif',
            None,
            dem_infos['geoT'][1],
            -1*dem_infos['geoT'][5],
            src_region=dem_region,
            verbose=self.verbose
        )[0]
        
        #utils.remove_glob('{}.tif'.format(self.name))

        utils.echo_msg('applying diff grid to dem')
        #diff_ds = gdal.Open('_tmp_smooth.tif')
        diff_band = diff_ds.GetRasterBand(1)
        diff_arr = diff_band.ReadAsArray()
        diff_arr[diff_arr == dem_infos['ndv']] = 0
        diff_arr[diff_arr == self.ndv] = 0
        
        dem_ds = gdal.Open(self.dem)
        dem_band = dem_ds.GetRasterBand(1)
        dem_arr = dem_band.ReadAsArray()

        update_dem_arr = diff_arr + dem_arr
        gdalfun.gdal_write(update_dem_arr, '{}.tif'.format(self.name), dem_infos)
        
        diff_ds = dem_ds = None
        #utils.remove_glob('_tmp_smooth.tif', '_diff.tif')
        return(self)

## ==============================================
##
## Waffles Interpolation Uncertainty module
##
## Calculate the uncertainty of interpolated cells in a
## (waffles) raster
##
## todo: no mask, use stack
## ==============================================
class InterpolationUncertainty:#(Waffle):

    def __init__(self, dem = None,  percentile = 95, sims = None, chnk_lvl = None, max_sample = None):
        """calculate cell-level interpolation uncertainty

        Args:
          percentile (int): max percentile
          sims (int): number of split-sample simulations
          chnk_lvl (int): the 'chunk-level'
        """

        self.dem = dem
        self.percentile = percentile
        self.sims = sims
        self.max_sample = max_sample
        self.chnk_lvl = chnk_lvl        
        self._zones = ['LD0','LD1','LD2','MD0','MD1','MD2','HD0', 'HD1', 'HD2']
        self.prox = None
        self.slope = None

        #multi_mask_fn = '{}_stack_m.{}'.format(os.path.join(self.dem.cache_dir, self.dem.name), gdalfun.gdal_fext(self.dem.fmt))
        #self.mask_fn = gdalfun.gdal_multi_mask2single_mask(multi_mask_fn)


    def _mask_analysis(self, src_gdal, region = None):
        """
        returns the number of filled cells, the total number of cells 
        and the percent of total cells filled.
        """
       
        ds_config = gdalfun.gdal_infos(src_gdal)
        if region is not None:
            srcwin = region.srcwin(ds_config['geoT'], ds_config['nx'], ds_config['ny'])
        else:
            srcwin = (0, 0, ds_config['nx'], ds_config['ny'])
          
        ds_arr = src_gdal.GetRasterBand(1).ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
        ds_arr[ds_arr == ds_config['ndv']] = np.nan
        ds_arr[~np.isnan(ds_arr)] = 1
        msk_sum = np.nansum(ds_arr)
        msk_max = float(srcwin[2] * srcwin[3])
        msk_perc = float((msk_sum / msk_max) * 100.)
        dst_arr = None

        return(msk_sum, msk_max, msk_perc)

    def _prox_analysis(self, src_gdal, region = None, band = 1):
        """
        returns the percentile of values in the srcwin
        """
        
        ds_config = gdalfun.gdal_infos(src_gdal)
        if region is not None:
            srcwin = region.srcwin(ds_config['geoT'], ds_config['nx'], ds_config['ny'])
        else:
            srcwin = (0, 0, ds_config['nx'], ds_config['ny'])
            
        ds_arr = src_gdal.GetRasterBand(band).ReadAsArray(*srcwin)
        #ds_arr[ds_arr == ds_config['ndv']] = np.nan
        prox_perc = np.percentile(ds_arr, 95)
        dst_arr = None

        return(prox_perc)

    def _gen_prox(self, out_prox = None):
        """
        generate a proximity grid from the data mask raster
        
        returns the output proximity grid's fn
        """
        
        if out_prox is None:
            out_prox = '{}_prox.tif'.format(self.dem.params['mod'])
            
        utils.echo_msg('generating proximity grid {}...'.format(out_prox))
        gdalfun.gdal_proximity(self.dem.stack, out_prox)
        if self.dem.dst_srs is not None:
            gdalfun.gdal_set_srs(out_prox, self.dem.dst_srs)

        return(out_prox)

    def _gen_slope(self, out_slope = None):
        """
        generate a slope grid from the elevation raster
        
        returns the output slope grid's fn
        """

        if out_slope is None:
            out_slope = '{}_slope.tif'.format(self.dem.params['mod'])
            
        utils.echo_msg('generating slope grid {}...'.format(out_slope))
        gdalfun.gdal_slope(self.dem.fn, out_slope)
        if self.dem.dst_srs is not None:
            gdalfun.gdal_set_srs(out_slope, self.dem.dst_srs)

        return(out_slope)

    def _regions_sort(self, trainers, t_num = 25, verbose = False):
        """sort regions (trainers is a list of regions) by distance; 
        a region is a list: [xmin, xmax, ymin, ymax].
        
        returns the sorted region-list
        """

        train_sorted = []
        for z, train in enumerate(trainers):
            train_d = []
            np.random.shuffle(train)
            train_total = len(train)
            while True:
                if verbose:
                    utils.echo_msg_inline('sorting training tiles [{}]'.format(len(train)))
                    
                if len(train) == 0:
                    break
                
                this_center = train[0][0].center()
                train_d.append(train[0])
                train = train[1:]
                if len(train_d) > t_num or len(train) == 0:
                    break
                
                dsts = [utils.euc_dst(this_center, x[0].center()) for x in train]
                min_dst = np.percentile(dsts, 50)
                d_t = lambda t: utils.euc_dst(this_center, t[0].center()) > min_dst
                np.random.shuffle(train)
                train.sort(reverse=True, key=d_t)
            #if verbose:
            #    utils.echo_msg(' '.join([x[0].format('gmt') for x in train_d[:t_num]]))
                
            train_sorted.append(train_d)
            
        if verbose:
            utils.echo_msg_inline('sorting training tiles [OK]\n')
            
        return(train_sorted)

    def _select_split(self, o_xyz, sub_region, sub_bn, verbose = False):
        """split an xyz file into an inner and outer region."""
        
        out_inner = '{}_inner.xyz'.format(sub_bn)
        out_outer = '{}_outer.xyz'.format(sub_bn)
        xyz_ds = dlim.XYZFile(fn=o_xyz, data_format=168, src_region=sub_region).initialize()
        with open(out_inner, 'w') as sub_inner:
            xyz_ds.dump_xyz_direct(dst_port=sub_inner)
            
        xyz_ds.invert_region = True
        with open(out_outer, 'w') as sub_outer:
            xyz_ds.dump_xyz_direct(dst_port=sub_outer)
            
        return([out_inner, out_outer])
    
    def _err_fit_plot(self, xdata, ydata, out, fitfunc, bins_final, std_final, sampling_den, max_int_dist,
                      dst_name = 'unc', xa = 'distance'):
        """plot a best fit plot with matplotlib

        Args:
          xdata (list): list of x-axis data
          ydata (list): list of y-axis data

        """

        #try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.offsetbox import AnchoredText

        short_name="All Terrain"
        
        fig = plt.figure()
        ax = plt.subplot(111)

        plt_data=ax.scatter(bins_final, std_final, zorder=1, label='Error St. Dev.', marker="o", color="black", s=30)
        #plt_best_fit,=ax.plot(xdata,ydata, zorder=1, linewidth=2.0)
        plt_best_fit,=ax.plot(xdata, fitfunc(out, xdata), '-')
        
        box = ax.get_position()
        ax.set_position([box.x0, box.y0 + box.height * 0.1, box.width, box.height * 0.9])

        plt.tick_params(
            axis='x',          # changes apply to the x-axis
            which='both',      # both major and minor ticks are affected
            bottom='on',      # ticks along the bottom edge are off
            top='off',         # ticks along the top edge are off
            labelbottom='on') # labels along the bottom edge are off

        anchored_text = AnchoredText(short_name, loc=2)
        anchored_text.patch.set_boxstyle("round,pad=0.,rounding_size=0.2")
        ax.add_artist(anchored_text)

        anchored_text2 = AnchoredText(" $y = {%gx}^{%g}$ "%(out[1],out[2]), loc=1)
        #add r2 value using below
        #anchored_text2 = AnchoredText(" $y = {%gx}^{%g}$      $r^2=%g$ "%(coeff1,coeff2,rsquared), loc=1)
        anchored_text2.patch.set_boxstyle("round,pad=0.,rounding_size=0.2")
        ax.add_artist(anchored_text2)

        str_ss_samp_den="Sampling Density = " + str(sampling_den) + " %"
        anchored_text3 = AnchoredText(str_ss_samp_den, loc=4)
        anchored_text3.patch.set_boxstyle("round,pad=0.,rounding_size=0.2")
        ax.add_artist(anchored_text3)

        plt.legend([plt_data, plt_best_fit], ['Interpolation Error St Dev', 'Best-Fit Line'], loc='upper center', bbox_to_anchor=(0.5, 1.15), fancybox=True, shadow=True, ncol=2, fontsize=14)

        plt.tick_params(
            axis='x',          # changes apply to the x-axis
            which='both',      # both major and minor ticks are affected
            bottom='on',      # ticks along the bottom edge are off
            top='off',         # ticks along the top edge are off
            labelbottom='on') # labels along the bottom edge are off
        
        plt.xlabel('Distance from Measurement (cells)', fontsize=14)
        plt.ylabel('Interpolation Error St Dev (m)', fontsize=14)
        plt.xlim(xmin=0)
        plt.xlim(xmax=int(max_int_dist)+1)
        plt.ylim(ymin=0)
        y_max=max(std_final)+(0.25*max(std_final))
        plt.ylim(ymax=y_max)

        # plt.plot(xdata, ydata, 'o')
        # plt.plot(xdata, fitfunc(out, xdata), '-')
        #plt.xlabel(xa)
        #plt.ylabel('Interpolation Error (m)')
        out_png = '{}_bf.png'.format(dst_name)
        plt.savefig(out_png)
        plt.close()

        #except: utils.echo_error_msg('you need to install matplotlib to run uncertainty plots...')

    def _err_scatter_plot(self, error_arr, dist_arr, mean, std, max_int_dist, bins_orig, sampling_den,
                          dst_name = 'unc', xa = 'distance'):
        """plot a scatter plot with matplotlib

        Args:
          error_arr (array): an array of errors
          dist_arr (array): an array of distances

        """

        #try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.offsetbox import AnchoredText

        short_name="All Terrain"

        fig = plt.figure()
        ax = plt.subplot(111)
        plt_data=ax.scatter(dist_arr, error_arr, zorder=1, label="Measurements", marker=".", color="black", s=20)
        plt_data_uncert=ax.errorbar(bins_orig, mean, yerr=std, fmt='r-', linewidth=3)
        box = ax.get_position()
        ax.set_position([box.x0, box.y0 + box.height * 0.1, box.width, box.height * 0.9])

        plt.tick_params(
            axis='x',          # changes apply to the x-axis
            which='both',      # both major and minor ticks are affected
            bottom='on',      # ticks along the bottom edge are off
            top='off',         # ticks along the top edge are off
            labelbottom='on') # labels along the bottom edge are off

        anchored_text = AnchoredText(short_name, loc=2)
        anchored_text.patch.set_boxstyle("round,pad=0.,rounding_size=0.2")
        ax.add_artist(anchored_text)

        str_ss_samp_den="Sampling Density = " + str(sampling_den) + " %"
        anchored_text3 = AnchoredText(str_ss_samp_den, loc=4)
        anchored_text3.patch.set_boxstyle("round,pad=0.,rounding_size=0.2")
        ax.add_artist(anchored_text3)

        plt.legend([plt_data, plt_data_uncert], ["Interpolation Error", "Mean +/- St. Deviation"], loc="upper center", bbox_to_anchor=(0.5, 1.15), fancybox=True, shadow=True, ncol=2, fontsize=14)
        plt.xlabel("Distance from Measurement (cells)", fontsize=14)
        plt.ylabel("Interpolation Error (m)", fontsize=14)
        plt.xlim(xmin=0)
        plt.xlim(xmax=int(max_int_dist)+1)

        #plt.xlabel(xa)
        #plt.ylabel('Interpolation Error (m)')
        out_png = "{}_scatter.png".format(dst_name)
        plt.savefig(out_png)
        plt.close()

        #xcept: utils.echo_error_msg('you need to install matplotlib to run uncertainty plots...')

    def _err2coeff(self, err_arr, sampling_den, coeff_guess = [0, 0.1, 0.2], dst_name = 'unc', xa = 'distance', plots = False):
        """calculate and plot the error coefficient given err_arr which is 
        a 2 col array with `err dist

        Args:
          error_arr (array): an array of errors and distances

        Returns:
          list: [coefficient-list]
        """

        from scipy import optimize
        
        error = err_arr[:,0]
        distance = err_arr[:,1]
        max_err = np.max(error)
        min_err = np.min(error)
        max_int_dist = int(np.max(distance))
        nbins = 10
        n, _ = np.histogram(distance, bins = nbins)
        while 0 in n:
            nbins -= 1
            n, _ = np.histogram(distance, bins=nbins)
            
        serror, _ = np.histogram(distance, bins=nbins, weights=error)
        serror2, _ = np.histogram(distance, bins=nbins, weights=error**2)

        mean = serror / n
        std = np.sqrt(serror2 / n - mean * mean)
        ydata = np.insert(std, 0, 0)
        bins_orig=(_[1:] + _[:-1]) / 2
        
        xdata = np.insert(bins_orig, 0, 0)
        xdata[xdata - 0 < 0.0001] = 0.0001
        while len(xdata) < 3:
            xdata = np.append(xdata, 0)
            ydata = np.append(ydata, 0)
            
        fitfunc = lambda p, x: p[0] + p[1] * (x ** p[2])
        errfunc = lambda p, x, y: y - fitfunc(p, x)
        out, cov, infodict, mesg, ier = optimize.leastsq(
            errfunc, coeff_guess, args=(xdata, ydata), full_output=True
        )

        if plots:
            try:
                self._err_fit_plot(xdata, ydata, out, fitfunc, bins_orig, std, sampling_den, max_int_dist, dst_name, xa)
                self._err_scatter_plot(error, distance, mean, std, max_int_dist, bins_orig, sampling_den, dst_name, xa)
            except:
               utils.echo_error_msg('unable to generate error plots, please check configs.')
            
        return(out)

    def _sub_region_analysis(self, sub_regions):
        """sub-region analysis

        return the sub-zones.
        """
        
        sub_zones = {}
        #dem_ds = gdal.Open(self.dem.fn)
        #msk_ds = gdal.Open(self.mask_fn)
        stack_ds = gdal.Open(self.dem.stack)
        prox_ds = gdal.Open(self.prox)
        slp_ds = gdal.Open(self.slope)
        # with utils.CliProgress(
        #         total=len(sub_regions),
        #         message='analyzing {} sub-regions'.format(len(sub_regions)),
        # ) as pbar:
        with tqdm(
                desc='analyzing {} sub-regions'.format(len(sub_regions)),
                total=len(sub_regions),
        ) as pbar:
            for sc, sub_region in enumerate(sub_regions):
                pbar.update()
                s_sum, s_g_max, s_perc = self._mask_analysis(stack_ds, region=sub_region)
                if s_sum == 0:
                    continue

                s_dc = gdalfun.gdal_infos(stack_ds, region=sub_region, scan=True)
                p_perc = self._prox_analysis(prox_ds, region=sub_region)
                slp_perc = self._prox_analysis(slp_ds, region=sub_region)
                #utils.echo_msg('{} {} {} {} {}'.format(s_sum, s_g_max, s_perc, p_perc, slp_perc))
                zone = None
                ## assign the region to the zone based on the density/slope
                if p_perc < self.prox_perc_33 or abs(p_perc - self.prox_perc_33) < 0.01:
                    if slp_perc < self.slp_perc_33 or abs(slp_perc - self.slp_perc_33) < 0.01:
                        zone = self._zones[6]
                    elif slp_perc < self.slp_perc_66 or abs(slp_perc - self.slp_perc_66) < 0.01:
                        zone = self._zones[7]
                    else:
                        zone = self._zones[8]
                elif p_perc < self.prox_perc_66 or abs(p_perc - self.prox_perc_66) < 0.01:
                    if slp_perc < self.slp_perc_33 or abs(slp_perc - self.slp_perc_33) < 0.01:
                        zone = self._zones[3]
                    elif slp_perc < self.slp_perc_66 or abs(slp_perc - self.slp_perc_66) < 0.01:
                        zone = self._zones[4]
                    else:
                        zone = self._zones[5]
                else:
                    if slp_perc < self.slp_perc_33 or abs(slp_perc - self.slp_perc_33) < 0.01:
                        zone = self._zones[0]
                    elif slp_perc < self.slp_perc_66 or abs(slp_perc - self.slp_perc_66) < 0.01:
                        zone = self._zones[1]
                    else:
                        zone = self._zones[2]

                if zone is not None:
                    sub_zones[sc + 1] = [sub_region, s_g_max, s_sum, s_perc, p_perc, zone]
            
        #dem_ds = msk_ds =
        stack_ds = prox_ds = slp_ds = None
        return(sub_zones)
     
    def _split_sample(self, trainers, perc, max_dist):
        """split-sample simulations and error calculations
        sims = max-simulations
        """
            
        sim = 0
        last_ec_d = None
        #s_dp = []
        s_dp = np.loadtxt(self.prox_errs)
        utils.echo_msg('loaded {} error points from {}'.format(len(s_dp), self.prox_errs))
        utils.echo_msg('simulation\terrors\tmean-error\tproximity-coeff\tp_diff')
        
        # with utils.CliProgress(
        #         message='performing MAX {} SPLIT-SAMPLE simulations looking for MIN {} sample errors'.format(self.sims, self.max_sample)
        # ) as pbar:
        ## multi-process?
        while True:
            status = 0
            sim += 1
            trains = self._regions_sort(trainers, verbose=False)
            tot_trains = len([x for s in trains for x in s])

            for z, train in enumerate(trains):
                train_h = train[:12] # 25
                ss_samp = perc

                ## ==============================================
                ## perform split-sample analysis on each training region.
                ## ==============================================
                for n, sub_region in enumerate(train_h):
                    #pbar.update()
                    ss_samp = perc
                    this_region = sub_region[0].copy()
                    if sub_region[3] < ss_samp:
                       ss_samp = sub_region[3]
                    
                    ## ==============================================
                    ## extract the xyz data for the region from the DEM
                    ## ==============================================
                    o_xyz = '{}_{}.xyz'.format(self.dem.name, n)
                    with gdalfun.gdal_datasource(self.dem.stack) as ds:
                       #ds_config = gdalfun.gdal_infos(self.dem.fn)
                       ds_config = gdalfun.gdal_infos(ds)
                       b_region = this_region.copy()
                       b_region.buffer(pct=20, x_inc=self.dem.xinc, y_inc=self.dem.yinc)
                       srcwin = b_region.srcwin(ds_config['geoT'], ds_config['nx'], ds_config['ny'])

                       ## TODO: extract weights here as well...
                       with open(o_xyz, 'w') as o_fh:
                           for xyz in gdalfun.gdal_parse(ds, srcwin=srcwin):
                               xyz.dump(dst_port=o_fh)

                    if os.stat(o_xyz).st_size == 0:
                        continue

                    ## ==============================================
                    ## split the xyz data to inner/outer; outer is
                    ## the data buffer, inner will be randomly sampled
                    ## ==============================================
                    s_inner, s_outer = self._select_split(o_xyz, this_region, 'sub_{}'.format(n))
                    if os.stat(s_inner).st_size == 0:
                        continue

                    sub_xyz = np.loadtxt(s_inner, ndmin=2, delimiter=' ')                        
                    ss_len = len(sub_xyz)
                    #sx_cnt = int(sub_region[2] * (ss_samp / 100.)) if ss_samp is not None else ss_len-1
                    #sx_cnt = int(sub_region[1] * (ss_samp / 100.)) + 1
                    sx_cnt = int(ss_len * (ss_samp / 100.))
                    #utils.echo_msg(sub_region)
                    #utils.echo_msg(ss_samp)
                    #utils.echo_msg(ss_len)
                    #utils.echo_msg(sx_cnt)
                    
                    sx_cnt = 1 if sx_cnt < 1 or sx_cnt >= ss_len else sx_cnt
                    sub_xyz_head = 'sub_{}_head_{}.xyz'.format(n, sx_cnt)
                    np.random.shuffle(sub_xyz)
                    np.savetxt(sub_xyz_head, sub_xyz[:sx_cnt], '%f', ' ')

                    ## ==============================================
                    ## generate the random-sample DEM
                    ## ==============================================
                    mod = self.dem.params['mod']
                    mod_args = self.dem.params['mod_args']
                    this_mod = '{}:{}'.format(mod, factory.dict2args(mod_args))
                    kwargs = self.dem.params['kwargs']
                    kwargs['name'] = 'sub_{}'.format(n)
                    kwargs['data'] = [s_outer, sub_xyz_head]
                    kwargs['src_region'] = b_region
                    kwargs['want_mask'] = True
                    kwargs['keep_auxiliary'] = True # to keep mask
                    kwargs['want_uncertainty'] = False
                    kwargs['verbose'] = False
                    kwargs['clobber'] = True
                    this_waffle = WaffleFactory(mod=this_mod, **kwargs)._acquire_module()
                    this_waffle.initialize()
                    wf = this_waffle.generate()

                    ## ==============================================
                    ## generate the random-sample data PROX and SLOPE
                    ## ==============================================
                    sub_prox = '{}_prox.tif'.format(wf.name)
                    gdalfun.gdal_proximity('{}_stack_m.tif'.format(wf.name), sub_prox)

                    ## ==============================================
                    ## Calculate the random-sample errors
                    ## todo: account for source uncertainty (rms with xyz?)
                    ## ==============================================
                    sub_xyd = gdalfun.gdal_query(sub_xyz[sx_cnt:], wf.fn, 'xyd')

                    sub_dp = gdalfun.gdal_query(sub_xyd, sub_prox, 'xyzg')
                    utils.remove_glob('{}*'.format(sub_xyz_head))
                    if sub_dp is not None and len(sub_dp) > 0:
                        try:
                            s_dp = np.vstack((s_dp, sub_dp))
                        except:
                            s_dp = sub_dp

                    utils.remove_glob('{}*'.format(wf.stack))
                    utils.remove_glob('{}*'.format(o_xyz), 'sub_{}*'.format(n))

            if s_dp is not None and len(s_dp) > 0:
                d_max = self.region_info[self.dem.name][4]
                s_dp = s_dp[s_dp[:,3] < max_dist,:]
                s_dp = s_dp[s_dp[:,3] >= 1,:]
                prox_err = s_dp[:,[2,3]]

                if last_ec_d is None:
                    last_ec_d = [0, 0.1, 0.2]
                    last_ec_diff = 10
                else:
                    last_ec_diff = abs(last_ec_d[2] - last_ec_d[1])

                ec_d = utils._err2coeff(prox_err[:50000000], perc, coeff_guess=last_ec_d,
                                        dst_name=self.dem.name + '_prox', xa='Distance to Nearest Measurement (cells)')
                ec_diff = abs(ec_d[2] - ec_d[1])
                ec_l_diff = abs(last_ec_diff - ec_diff)
                utils.echo_msg('{}\t{}\t{}\t{}\t{}'.format(sim, len(s_dp), np.mean(prox_err, axis=0)[0], ec_d, ec_l_diff))

                ## continue if we haven't reached max_sample
                if len(s_dp) < self.max_sample:
                    last_ec_d = ec_d
                    continue

                ## continue if we got back the default err coeff
                if ec_d[0] == 0 and ec_d[1] == 0.1 and ec_d[2] == 0.2:
                    last_ec_d = ec_d
                    continue
                
                ## break if we gathered enough simulation errors
                if sim >= int(self.sims): 
                    break

                ## break if the err coeff doesn't change much
                if abs(last_ec_diff - ec_diff) < 0.01:
                    break

                last_ec_d = ec_d
            else:
                utils.echo_msg('{}\t{}\t{}\t{}\t{}'.format(sim, len(s_dp), None, None, None, None))

        with open(self.prox_errs, 'a') as prox_dat:
            np.savetxt(prox_dat, prox_err, '%f', ' ')
            
        return([ec_d])
        
    def run(self):
        print(self.dem.stack)
        s_dp = s_ds = None
        unc_out = {}
        zones = ['low-dens-low-slp', 'low-dens-mid-slp', 'low-dens-high-slp',
                 'mid-dens-low-slp', 'mid-dens-mid-slp', 'mid-dens-high-slp',
                 'high-dens-low-slp', 'high-dens-mid-slp', 'high-dens-low-slp']
        utils.echo_msg('running INTERPOLATION uncertainty module using {}...'.format(self.dem.params['mod']))
        
        if self.prox is None:
            self.prox = self._gen_prox('{}_u.tif'.format(self.dem.name))

        if self.slope is None:
            self.slope = self._gen_slope()
            
        ## ==============================================
        ## region and der. analysis
        ## ==============================================
        self.region_info = {}
        with gdalfun.gdal_datasource(self.dem.stack) as tmp_ds:
            num_sum, g_max, num_perc = self._mask_analysis(tmp_ds)

        self.prox_percentile = gdalfun.gdal_percentile(self.prox, self.percentile)
        self.prox_perc_33 = gdalfun.gdal_percentile(self.prox, 25)
        self.prox_perc_66 = gdalfun.gdal_percentile(self.prox, 75)
        self.prox_perc_100 = gdalfun.gdal_percentile(self.prox, 100)

        self.slp_percentile = gdalfun.gdal_percentile(self.slope, self.percentile)
        self.slp_perc_33 = gdalfun.gdal_percentile(self.slope, 25)
        self.slp_perc_66 = gdalfun.gdal_percentile(self.slope, 75)
        self.slp_perc_100 = gdalfun.gdal_percentile(self.slope, 100)

        self.region_info[self.dem.name] = [self.dem.region, g_max, num_sum, num_perc, self.prox_percentile]
        for x in self.region_info.keys():
            utils.echo_msg('region: {}: {}'.format(x, self.region_info[x]))

        ## ==============================================
        ## chunk region into sub regions
        ## ==============================================
        #chnk_inc = int((num_sum / math.sqrt(g_max)) / num_perc) * 2
        #chnk_inc = chnk_inc if chnk_inc > 10 else 10

        chnk_inc = int(self.prox_percentile)
        
        utils.echo_msg('chunk inc is: {}'.format(chnk_inc))
        sub_regions = self.dem.region.chunk(self.dem.xinc, chnk_inc)
        utils.echo_msg('chunked region into {} sub-regions @ {}x{} cells.'.format(len(sub_regions), chnk_inc, chnk_inc))

        ## ==============================================
        ## sub-region analysis
        ## ==============================================
        sub_zones = self._sub_region_analysis(sub_regions)
        #sub_zones.append(self._sub_region_analysis(sub_regions, prox_or_slp='slp'))

        ## ==============================================
        ## sub-region density and percentiles
        ## ==============================================
        s_dens = np.array([sub_zones[x][3] for x in sub_zones.keys()])
        s_5perc = np.percentile(s_dens, 5)
        s_dens = None
        #utils.echo_msg('Sampling density for region is: {:.16f}'.format(s_5perc))
        utils.echo_msg('Sampling density for region is: {:.16f}'.format(num_perc))

        ## ==============================================
        ## zone analysis / generate training regions
        ## ==============================================
        trainers = []
        t_perc = 95
        s_perc = 50

        for z, this_zone in enumerate(self._zones):
            tile_set = [sub_zones[x] for x in sub_zones.keys() if sub_zones[x][5] == self._zones[z]]
            if len(tile_set) > 0:
                d_50perc = np.percentile(np.array([x[3] for x in tile_set]), 50)
            else:
                continue
            
            t_trainers = [x for x in tile_set if x[3] < d_50perc or abs(x[3] - d_50perc) < 0.01]
            utils.echo_msg(
                'possible {} training zones: {} @ MAX {}'.format(
                    self._zones[z].upper(), len(t_trainers), d_50perc
                )
            )
            trainers.append(t_trainers)
            
        utils.echo_msg('analyzed {} sub-regions.'.format(len(sub_regions)))

        ## ==============================================
        ## split-sample simulations and error calculations
        ## sims = max-simulations
        ## ==============================================
        if self.sims is None:
            self.sims = int(len(sub_regions)/tot_trains)
            
            #self.sims = 12

        if self.max_sample is None:
            self.max_sample = int((self.region_info[self.dem.name][1] - self.region_info[self.dem.name][2]) * .1)

        utils.echo_msg('max sample is {}, max sims is {}'.format(self.max_sample, self.sims))


        if self.accumulate:
            ec_d = self._split_sample(trainers, num_perc, chnk_inc/2)[0]
        else:
            prox_errs = np.loadtxt(self.prox_errs)
            ec_d = utils._err2coeff(prox_errs[:50000000], perc, coeff_guess=[0, 0.1, 0.2],
                                    dst_name=self.prox_errs[:-3], xa='Distance to Nearest Measurement (cells)')

        ## ==============================================
        ## Save/Output results
        ## apply error coefficient to full proximity grid
        ## TODO: USE numpy/gdal instead!
        ## ==============================================
        utils.echo_msg('applying coefficient to PROXIMITY grid {}'.format(self.prox))
        with gdalfun.gdal_datasource(self.prox, update=True) as prox_ds:
            prox_inf = gdalfun.gdal_infos(prox_ds)
            prox_band = prox_ds.GetRasterBand(1)
            prox_arr = prox_band.ReadAsArray()
            prox_arr = ec_d[1] * (prox_arr**ec_d[2])
            #prox_arr = (ec_d[0] + ec_d[1]) * prox_arr**ec_d[2]
            #prox_arr[prox_arr == pro

            with gdalfun.gdal_datasource(self.dem.stack) as stack_ds:
                unc_inf = gdalfun.gdal_infos(stack_ds, band=4)
                unc_band = stack_ds.GetRasterBand(4)
                unc_arr = unc_band.ReadAsArray()
                unc_arr[unc_arr == unc_inf['ndv']] = 0
                out_arr = prox_arr + unc_arr
                prox_band.SetNoDataValue(self.dem.ndv)
                prox_band.WriteArray(out_arr)
            
        if self.dem.dst_srs is not None:
            status = gdalfun.gdal_set_srs(self.prox, src_srs=self.dem.dst_srs)
            
        utils.echo_msg('applied coefficient {} to PROXIMITY grid'.format(ec_d))
        utils.remove_glob(self.slope)

        unc_out['prox_unc'] = [self.prox, 'raster']
        unc_out['prox_bf'] = ['{}_prox_bf.png'.format(self.dem.name), 'image']
        unc_out['prox_scatter'] = ['{}_prox_scatter.png'.format(self.dem.name), 'image']

        return(unc_out, 0)

## ==============================================
##
## Waffles Uncertainty module
##
## ==============================================
class WafflesUncertainty(Waffle):

    def __init__(self, waffles_module='IDW', percentile = 95, sims = 1, chnk_lvl = None,
                 max_sample = None, accumulate = True, **kwargs):
        """calculate cell-level interpolation uncertainty

        Args:
          waffles_module (str): waffles module string
          percentile (int): max percentile
          sims (int): number of split-sample simulations
          chnk_lvl (int): the 'chunk-level'
        """

        self.waffles_module_args = {}
        tmp_waffles = Waffle()
        for kpam, kval in kwargs.items():
            if kpam not in tmp_waffles.__dict__:
                self.waffles_module_args[kpam] = kval

        for kpam, kval in self.waffles_module_args.items():
            del kwargs[kpam]
            
        super().__init__(**kwargs)

        # print(self.params)
        # for kpam, kval in self.waffles_module_args.items():
        #     print(kpam)
        
        self.waffles_module = waffles_module
        self.percentile = percentile
        self.sims = sims
        self.max_sample = max_sample
        self.chnk_lvl = chnk_lvl
        self.accumulate = accumulate
        self.prox_errs = '{}_errs.dat'.format(self.waffles_module)
        if os.path.exists(os.path.join(utils.cudem_data, self.prox_errs)):
            self.prox_errs = os.path.join(self.cudem_data, self.prox_errs)
        else:
            self.accumulate = True

        print(self.accumulate)
        print(self.prox_errs)
        
        self._zones = ['LD0','LD1','LD2','MD0','MD1','MD2','HD0', 'HD1', 'HD2']
        self.prox = None
        self.slope = None

    def _mask_analysis(self, src_gdal, region = None):
        """
        returns the number of filled cells, the total number of cells 
        and the percent of total cells filled.
        """
       
        ds_config = gdalfun.gdal_infos(src_gdal)
        if region is not None:
            srcwin = region.srcwin(ds_config['geoT'], ds_config['nx'], ds_config['ny'])
        else:
            srcwin = (0, 0, ds_config['nx'], ds_config['ny'])
          
        ds_arr = src_gdal.GetRasterBand(1).ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
        ds_arr[ds_arr == ds_config['ndv']] = np.nan
        ds_arr[~np.isnan(ds_arr)] = 1
        msk_sum = np.nansum(ds_arr)
        msk_max = float(srcwin[2] * srcwin[3])
        msk_perc = float((msk_sum / msk_max) * 100.)
        dst_arr = None

        return(msk_sum, msk_max, msk_perc)

    def _prox_analysis(self, src_gdal, region = None, band = 1):
        """
        returns the percentile of values in the srcwin
        """
        
        ds_config = gdalfun.gdal_infos(src_gdal)
        if region is not None:
            srcwin = region.srcwin(ds_config['geoT'], ds_config['nx'], ds_config['ny'])
        else:
            srcwin = (0, 0, ds_config['nx'], ds_config['ny'])
            
        ds_arr = src_gdal.GetRasterBand(band).ReadAsArray(*srcwin).astype(float)
        ds_arr[ds_arr == ds_config['ndv']] = np.nan
        prox_perc = np.nanpercentile(ds_arr, 95)
        dst_arr = None

        return(prox_perc)

    def _gen_prox(self, out_prox = None):
        """
        generate a proximity grid from the data mask raster
        
        returns the output proximity grid's fn
        """
        
        if out_prox is None:
            out_prox = '{}_prox.tif'.format(self.params['mod_args']['waffles_module'])
            
        utils.echo_msg('generating proximity grid {}...'.format(out_prox))
        gdalfun.gdal_proximity(self.stack, out_prox)
        if self.dst_srs is not None:
            gdalfun.gdal_set_srs(out_prox, self.dst_srs)

        return(out_prox)

    def _gen_slope(self, out_slope = None):
        """
        generate a slope grid from the elevation raster
        
        returns the output slope grid's fn
        """

        if out_slope is None:
            out_slope = '{}_slope.tif'.format(self.params['mod_args']['waffles_module'])
            
        utils.echo_msg('generating slope grid {}...'.format(out_slope))
        gdalfun.gdal_slope(self.stack, out_slope)
        if self.dst_srs is not None:
            gdalfun.gdal_set_srs(out_slope, self.dst_srs)

        return(out_slope)

    def _regions_sort(self, trainers, t_num = 25, verbose = False):
        """sort regions (trainers is a list of regions) by distance; 
        a region is a list: [xmin, xmax, ymin, ymax].
        
        returns the sorted region-list
        """

        train_sorted = []
        for z, train in enumerate(trainers):
            train_d = []
            np.random.shuffle(train)
            train_total = len(train)
            while True:
                if verbose:
                    utils.echo_msg_inline('sorting training tiles [{}]'.format(len(train)))
                    
                if len(train) == 0:
                    break
                
                this_center = train[0][0].center()
                train_d.append(train[0])
                train = train[1:]
                if len(train_d) > t_num or len(train) == 0:
                    break
                
                dsts = [utils.euc_dst(this_center, x[0].center()) for x in train]
                min_dst = np.percentile(dsts, 50)
                d_t = lambda t: utils.euc_dst(this_center, t[0].center()) > min_dst
                np.random.shuffle(train)
                train.sort(reverse=True, key=d_t)
            #if verbose:
            #    utils.echo_msg(' '.join([x[0].format('gmt') for x in train_d[:t_num]]))
                
            train_sorted.append(train_d)
            
        if verbose:
            utils.echo_msg_inline('sorting training tiles [OK]\n')
            
        return(train_sorted)

    def _select_split(self, o_xyz, sub_region, sub_bn, verbose = False):
        """split an xyz file into an inner and outer region."""
        
        out_inner = '{}_inner.xyz'.format(sub_bn)
        out_outer = '{}_outer.xyz'.format(sub_bn)
        xyz_ds = dlim.XYZFile(fn=o_xyz, data_format=168, src_region=sub_region).initialize()
        with open(out_inner, 'w') as sub_inner:
            xyz_ds.dump_xyz_direct(dst_port=sub_inner)
            
        xyz_ds.invert_region = True
        with open(out_outer, 'w') as sub_outer:
            xyz_ds.dump_xyz_direct(dst_port=sub_outer)
            
        return([out_inner, out_outer])    

    def _sub_region_analysis(self, sub_regions):
        """sub-region analysis

        return the sub-zones.
        """
        
        sub_zones = {}
        #dem_ds = gdal.Open(self.fn)
        #msk_ds = gdal.Open(self.mask_fn)
        stack_ds = gdal.Open(self.stack)
        prox_ds = gdal.Open(self.prox)
        slp_ds = gdal.Open(self.slope)
        # with utils.CliProgress(
        #         total=len(sub_regions),
        #         message='analyzing {} sub-regions'.format(len(sub_regions)),
        # ) as pbar:
        with tqdm(
                total=len(sub_regions),
                desc='analyzing {} sub-regions'.format(len(sub_regions)),
        ) as pbar:
            for sc, sub_region in enumerate(sub_regions):
                pbar.update()
                s_sum, s_g_max, s_perc = self._mask_analysis(stack_ds, region=sub_region)
                if s_sum == 0:
                    continue

                #s_dc = gdalfun.gdal_infos(stack_ds, region=sub_region, scan=True)
                p_perc = self._prox_analysis(prox_ds, region=sub_region)
                slp_perc = self._prox_analysis(slp_ds, region=sub_region)
                utils.echo_msg('{} {} {} {} {}'.format(s_sum, s_g_max, s_perc, p_perc, slp_perc))
                zone = None
                ## assign the region to the zone based on the density/slope
                if p_perc < self.prox_perc_33 or abs(p_perc - self.prox_perc_33) < 0.01:
                    if slp_perc < self.slp_perc_33 or abs(slp_perc - self.slp_perc_33) < 0.01:
                        zone = self._zones[6]
                    elif slp_perc < self.slp_perc_66 or abs(slp_perc - self.slp_perc_66) < 0.01:
                        zone = self._zones[7]
                    else:
                        zone = self._zones[8]
                elif p_perc < self.prox_perc_66 or abs(p_perc - self.prox_perc_66) < 0.01:
                    if slp_perc < self.slp_perc_33 or abs(slp_perc - self.slp_perc_33) < 0.01:
                        zone = self._zones[3]
                    elif slp_perc < self.slp_perc_66 or abs(slp_perc - self.slp_perc_66) < 0.01:
                        zone = self._zones[4]
                    else:
                        zone = self._zones[5]
                else:
                    if slp_perc < self.slp_perc_33 or abs(slp_perc - self.slp_perc_33) < 0.01:
                        zone = self._zones[0]
                    elif slp_perc < self.slp_perc_66 or abs(slp_perc - self.slp_perc_66) < 0.01:
                        zone = self._zones[1]
                    else:
                        zone = self._zones[2]

                if zone is not None:
                    sub_zones[sc + 1] = [sub_region, s_g_max, s_sum, s_perc, p_perc, zone]
            
        #dem_ds = msk_ds =
        stack_ds = prox_ds = slp_ds = None
        return(sub_zones)
     
    def _split_sample(self, trainers, perc, max_dist):
        """split-sample simulations and error calculations
        sims = max-simulations
        """
            
        sim = 0
        last_ec_d = None
        #s_dp = []
        s_dp = np.loadtxt(self.prox_errs)
        utils.echo_msg('loaded {} error points from {}'.format(len(s_dp), self.prox_errs))
        # with utils.CliProgress(
        #         message='performing MAX {} SPLIT-SAMPLE simulations looking for MIN {} sample errors'.format(self.sims, self.max_sample)
        # ) as pbar:
        with tqdm(
                desc='performing MAX {} SPLIT-SAMPLE simulations looking for MIN {} sample errors'.format(self.sims, self.max_sample)
        ) as pbar:
            utils.echo_msg('simulation\terrors\tmean-error\tproximity-coeff\tp_diff')
            
            while True:
                status = 0
                sim += 1
                trains = self._regions_sort(trainers, verbose=False)
                tot_trains = len([x for s in trains for x in s])

                for z, train in enumerate(trains):
                    train_h = train[:3] # 25
                    ss_samp = perc

                    ## ==============================================
                    ## perform split-sample analysis on each training region.
                    ## ==============================================
                    for n, sub_region in enumerate(train_h):
                        pbar.update()
                        ss_samp = perc
                        this_region = sub_region[0].copy()
                        if sub_region[3] < ss_samp:
                           ss_samp = sub_region[3]

                        ## ==============================================
                        ## extract the xyz data for the region from the DEM
                        ## ==============================================
                        o_xyz = '{}_{}.xyz'.format(self.name, n)
                        with gdalfun.gdal_datasource(self.stack) as ds:
                           #ds_config = gdalfun.gdal_infos(self.fn)
                           ds_config = gdalfun.gdal_infos(ds)
                           b_region = this_region.copy()
                           b_region.buffer(pct=20, x_inc=self.xinc, y_inc=self.yinc)
                           srcwin = b_region.srcwin(ds_config['geoT'], ds_config['nx'], ds_config['ny'])

                           ## TODO: extract weights here as well...
                           with open(o_xyz, 'w') as o_fh:
                               for xyz in gdalfun.gdal_parse(ds, srcwin=srcwin):
                                   xyz.dump(dst_port=o_fh)

                        if os.stat(o_xyz).st_size == 0:
                            continue

                        ## ==============================================
                        ## split the xyz data to inner/outer; outer is
                        ## the data buffer, inner will be randomly sampled
                        ## ==============================================
                        s_inner, s_outer = self._select_split(o_xyz, this_region, 'sub_{}'.format(n))
                        if os.stat(s_inner).st_size == 0:
                            continue

                        sub_xyz = np.loadtxt(s_inner, ndmin=2, delimiter=' ')                        
                        ss_len = len(sub_xyz)
                        #sx_cnt = int(sub_region[2] * (ss_samp / 100.)) if ss_samp is not None else ss_len-1
                        #sx_cnt = int(sub_region[1] * (ss_samp / 100.)) + 1
                        sx_cnt = int(ss_len * (ss_samp / 100.))
                        #utils.echo_msg(sub_region)
                        #utils.echo_msg(ss_samp)
                        #utils.echo_msg(ss_len)
                        #utils.echo_msg(sx_cnt)

                        sx_cnt = 1 if sx_cnt < 1 or sx_cnt >= ss_len else sx_cnt
                        sub_xyz_head = 'sub_{}_head_{}.xyz'.format(n, sx_cnt)
                        np.random.shuffle(sub_xyz)
                        np.savetxt(sub_xyz_head, sub_xyz[:sx_cnt], '%f', ' ')

                        ## ==============================================
                        ## generate the random-sample DEM
                        ## ==============================================
                        #mod = self.params['mod']
                        #mod_args = self.params['mod_args']
                        this_mod = '{}:{}'.format(self.waffles_module, factory.dict2args(self.waffles_module_args))
                        kwargs = self.params['kwargs']
                        kwargs['name'] = 'sub_{}'.format(n)
                        kwargs['data'] = [s_outer, sub_xyz_head]
                        kwargs['src_region'] = b_region
                        #kwargs['want_mask'] = True
                        #kwargs['keep_auxiliary'] = True # to keep mask
                        kwargs['want_uncertainty'] = False
                        kwargs['verbose'] = False
                        kwargs['clobber'] = True
                        this_waffle = WaffleFactory(mod=this_mod, **kwargs)._acquire_module()
                        this_waffle.initialize()
                        wf = this_waffle.generate()

                        ## ==============================================
                        ## generate the random-sample data PROX and SLOPE
                        ## ==============================================
                        sub_prox = '{}_prox.tif'.format(wf.name)
                        gdalfun.gdal_proximity(wf.stack, sub_prox)

                        ## ==============================================
                        ## Calculate the random-sample errors
                        ## todo: account for source uncertainty (rms with xyz?)
                        ## ==============================================
                        sub_xyd = gdalfun.gdal_query(sub_xyz[sx_cnt:], wf.fn, 'xyd')
                        sub_dp = gdalfun.gdal_query(sub_xyd, sub_prox, 'zg')
                        utils.remove_glob('{}*'.format(sub_xyz_head))
                        if sub_dp is not None and len(sub_dp) > 0:
                            try:
                                s_dp = np.vstack((s_dp, sub_dp))
                            except:
                                s_dp = sub_dp

                            with open(self.prox_errs, 'a') as prox_dat:
                                np.savetxt(prox_dat, sub_dp, '%f', ' ')

                        utils.remove_glob('{}*'.format(wf.stack))
                        utils.remove_glob('{}*'.format(o_xyz), 'sub_{}*'.format(n))

                if s_dp is not None and len(s_dp) > 0:
                    d_max = self.region_info[self.name][4]
                    # s_dp = s_dp[s_dp[:,3] < max_dist,:]
                    # s_dp = s_dp[s_dp[:,3] >= 1,:]
                    # prox_err = s_dp[:,[2,3]]

                    prox_err = s_dp[s_dp[:,1] < max_dist,:]
                    prox_err = s_dp[s_dp[:,1] >= 1,:]

                    if last_ec_d is None:
                        last_ec_d = [0, 0.1, 0.2]
                        last_ec_diff = 10
                    else:
                        last_ec_diff = abs(last_ec_d[2] - last_ec_d[1])

                    ec_d = utils._err2coeff(prox_err[:50000000], perc, coeff_guess=last_ec_d,
                                            dst_name=self.name + '_prox', xa='Distance to Nearest Measurement (cells)')
                    ec_diff = abs(ec_d[2] - ec_d[1])
                    ec_l_diff = abs(last_ec_diff - ec_diff)
                    utils.echo_msg('{}\t{}\t{}\t{}\t{}'.format(sim, len(prox_err), np.mean(prox_err, axis=0)[0], ec_d, ec_l_diff))
                    utils.echo_msg(ec_d[0] + ec_d[1])

                    ## continue if we got back the default err coeff
                    if ec_d[0] == 0 and ec_d[1] == 0.1 and ec_d[2] == 0.2:
                        last_ec_d = ec_d
                        continue

                    ## continue if we haven't reached max_sample
                    if len(s_dp) < self.max_sample:
                        last_ec_d = ec_d
                        continue

                    ## break if we gathered enough simulation errors
                    if sim >= int(self.sims): 
                        break
                    
                    ## break if the err coeff doesn't change much
                    if abs(last_ec_diff - ec_diff) < 0.01:
                        break

                    last_ec_d = ec_d
                else:
                    utils.echo_msg('{}\t{}\t{}\t{}\t{}'.format(sim, len(s_dp), None, None, None, None))

        #np.savetxt('prox_err.xyz', prox_err, '%f', ' ')

        return([ec_d])
        
    def run(self):
        print(self.stack)
        s_dp = s_ds = None
        unc_out = {}
        zones = ['low-dens-low-slp', 'low-dens-mid-slp', 'low-dens-high-slp',
                 'mid-dens-low-slp', 'mid-dens-mid-slp', 'mid-dens-high-slp',
                 'high-dens-low-slp', 'high-dens-mid-slp', 'high-dens-low-slp']
        utils.echo_msg('running UNCERTAINTY module using {}...'.format(self.params['mod_args']['waffles_module']))
            
        ## ==============================================
        ## region and der. analysis
        ## ==============================================
        self.region_info = {}
        with gdalfun.gdal_datasource(self.stack) as tmp_ds:
            num_sum, g_max, num_perc = self._mask_analysis(tmp_ds)

        if self.prox is None:
            self.prox = self._gen_prox('{}_u.tif'.format(self.name))
            
        self.prox_percentile = gdalfun.gdal_percentile(self.prox, self.percentile)
        self.prox_perc_33 = gdalfun.gdal_percentile(self.prox, 25)
        self.prox_perc_66 = gdalfun.gdal_percentile(self.prox, 75)
        self.prox_perc_100 = gdalfun.gdal_percentile(self.prox, 100)

        if self.slope is None:
            self.slope = self._gen_slope()
            
        self.slp_percentile = gdalfun.gdal_percentile(self.slope, self.percentile)
        self.slp_perc_33 = gdalfun.gdal_percentile(self.slope, 25)
        self.slp_perc_66 = gdalfun.gdal_percentile(self.slope, 75)
        self.slp_perc_100 = gdalfun.gdal_percentile(self.slope, 100)

        print(self.prox_perc_33, self.prox_perc_66, self.prox_perc_100)
        print(self.slp_perc_33, self.slp_perc_66, self.slp_perc_100)
        
        self.region_info[self.name] = [self.region, g_max, num_sum, num_perc, self.prox_percentile]
        for x in self.region_info.keys():
            utils.echo_msg('region: {}: {}'.format(x, self.region_info[x]))

        ## ==============================================
        ## chunk region into sub regions
        ## ==============================================
        #chnk_inc = int((num_sum / math.sqrt(g_max)) / num_perc) * 2
        #chnk_inc = chnk_inc if chnk_inc > 10 else 10

        chnk_inc = int(self.prox_percentile)
        
        utils.echo_msg('chunk inc is: {}'.format(chnk_inc))
        sub_regions = self.region.chunk(self.xinc, chnk_inc)
        utils.echo_msg('chunked region into {} sub-regions @ {}x{} cells.'.format(len(sub_regions), chnk_inc, chnk_inc))

        ## ==============================================
        ## sub-region analysis
        ## ==============================================
        sub_zones = self._sub_region_analysis(sub_regions)
        #sub_zones.append(self._sub_region_analysis(sub_regions, prox_or_slp='slp'))

        ## ==============================================
        ## sub-region density and percentiles
        ## ==============================================
        s_dens = np.array([sub_zones[x][3] for x in sub_zones.keys()])
        s_5perc = np.percentile(s_dens, 5)
        s_dens = None
        #utils.echo_msg('Sampling density for region is: {:.16f}'.format(s_5perc))
        utils.echo_msg('Sampling density for region is: {:.16f}'.format(num_perc))

        ## ==============================================
        ## zone analysis / generate training regions
        ## ==============================================
        trainers = []
        t_perc = 95
        s_perc = 50

        for z, this_zone in enumerate(self._zones):
            tile_set = [sub_zones[x] for x in sub_zones.keys() if sub_zones[x][5] == self._zones[z]]
            if len(tile_set) > 0:
                d_50perc = np.percentile(np.array([x[3] for x in tile_set]), 50)
            else:
                continue
            
            t_trainers = [x for x in tile_set if x[3] < d_50perc or abs(x[3] - d_50perc) < 0.01]
            utils.echo_msg(
                'possible {} training zones: {} @ MAX {}'.format(
                    self._zones[z].upper(), len(t_trainers), d_50perc
                )
            )
            trainers.append(t_trainers)
            
        utils.echo_msg('analyzed {} sub-regions.'.format(len(sub_regions)))

        ## ==============================================
        ## split-sample simulations and error calculations
        ## sims = max-simulations
        ## ==============================================
        if self.sims is None:
            self.sims = int(len(sub_regions)/tot_trains)
            
            #self.sims = 12

        if self.max_sample is None:
            self.max_sample = int((self.region_info[self.name][1] - self.region_info[self.name][2]) * .1)

        utils.echo_msg('max sample is {}, max sims is {}'.format(self.max_sample, self.sims))
        #ec_d = self._split_sample(trainers, num_perc, chnk_inc/2)[0]
        if self.accumulate:
            ec_d = self._split_sample(trainers, num_perc, chnk_inc/2)[0]
        #else:
        prox_errs = np.loadtxt(self.prox_errs)
        ec_d = utils._err2coeff(prox_errs, num_perc, coeff_guess=[0, 0.1, 0.2],
                                dst_name=self.prox_errs[:-3], xa='Distance to Nearest Measurement (cells)')

        ## ==============================================
        ## Save/Output results
        ## apply error coefficient to full proximity grid
        ## TODO: USE numpy/gdal instead!
        ## ==============================================
        utils.echo_msg('applying coefficient to PROXIMITY grid {}'.format(self.prox))

        with gdalfun.gdal_datasource(self.prox, update=True) as prox_ds:
            prox_inf = gdalfun.gdal_infos(prox_ds)
            prox_band = prox_ds.GetRasterBand(1)
            prox_arr = prox_band.ReadAsArray().astype(float)
            with gdalfun.gdal_datasource(self.stack) as stack_ds:
                unc_inf = gdalfun.gdal_infos(stack_ds, band=4)
                unc_band = stack_ds.GetRasterBand(4)
                unc_arr = unc_band.ReadAsArray()
                unc_arr[unc_arr == unc_inf['ndv']] = 0
                out_arr = unc_arr + ec_d[1] * (prox_arr**ec_d[2])
                #prox_arr = (ec_d[0] + ec_d[1]) * prox_arr**ec_d[2]
                #prox_arr[prox_arr == pro

        # utils.echo_msg('combining uncertainty grid {} with proximity uncertainty'.format(self.stack))
        # with gdalfun.gdal_datasource(self.stack) as stack_ds:
        #     unc_inf = gdalfun.gdal_infos(stack_ds, band=4)
        #     unc_band = stack_ds.GetRasterBand(4)
        #     unc_arr = unc_band.ReadAsArray()
        #     unc_arr[unc_arr == unc_inf['ndv']] = 0

        #out_arr = prox_arr + unc_arr
        unc_out = gdalfun.gdal_write(out_arr, '{}.{}'.format(self.name, 'tif'), self.ds_config)[0]
        #prox_band.SetNoDataValue(self.ndv)
        #prox_band.WriteArray(out_arr)
            
        if self.dst_srs is not None:
            status = gdalfun.gdal_set_srs(self.prox, src_srs=self.dst_srs)
            
        utils.echo_msg('applied coefficient {} to PROXIMITY grid'.format(ec_d))
        utils.remove_glob(self.slope)
        utils.remove_glob(self.prox)

        #unc_out['prox_unc'] = [self.prox, 'raster']
        #unc_out['prox_bf'] = ['{}_prox_bf.png'.format(self.name), 'image']
        #unc_out['prox_scatter'] = ['{}_prox_scatter.png'.format(self.name), 'image']

        return(unc_out, 0)
    
## ==============================================
## WaffleDEM which holds a gdal DEM to process
## WaffleDEM(fn='module_output.tif')
## ==============================================
class WaffleDEM:
    def __init__(self, fn: str = 'this_waffle.tif', ds_config: dict = {}, cache_dir: str = waffles_cache, verbose: bool = True, waffle: Waffle = None):
        self.fn = fn # the dem filename
        self.ds_config = ds_config # a dem config dictionary (see gdalfun.gdal_infos)
        self.cache_dir = cache_dir
        self.verbose = verbose # verbosity
        #self.dem_ds = None # the dem as a gdal datasource
        self.dem_region = None # the dem regions.Region()
        self.waffle = waffle

    def initialize(self):
        if os.path.exists(self.fn):
            dem_ds = gdal.Open(self.fn, 1)

            if dem_ds is not None:
                self.ds_config = gdalfun.gdal_infos(dem_ds, scan=True)
                self.dem_region = regions.Region().from_geo_transform(self.ds_config['geoT'], self.ds_config['nx'], self.ds_config['ny'])

                dem_ds = None
            
        return(self)

    def valid_p(self):
        """check if the WAFFLES DEM appears to be valid"""

        if not os.path.exists(self.fn):
            return(False)
        
        self.initialize()
        if self.ds_config is None:
            return(False)
        
        if not 'zr' in self.ds_config:
            return(False)
            
        if np.isnan(self.ds_config['zr'][0]):
            return(False)
        
        return(True)
        
    def process(self, filter_ = None, ndv = None, xsample = None, ysample = None, region = None, node= None,
                clip_str = None, upper_limit = None, lower_limit = None, dst_srs = None, dst_fmt = None, dst_dir = None,
                set_metadata = True):

        if self.verbose:
            utils.echo_msg('post processing DEM {}...'.format(self.fn))
        
        if self.ds_config is None:
            self.initialize()

        if ndv is not None:
            self.set_nodata(ndv)
        else:
            if self.ds_config['ndv'] is not None:
                self.set_nodata(self.ds_config['ndv'])

        ## filtering the DEM will change the weights/uncertainty
        if filter_ is not None:
            self.filter_(filter_)

        ## resamples all bands
        self.resample(xsample=xsample, ysample=ysample, ndv=ndv, region=region)

        ## clip/cut
        self.clip(clip_str=clip_str)
        if region is not None:
            self.cut(region=region)

        ## setting limits will change the weights/uncertainty for flattened data
        self.set_limits(upper_limit=upper_limit, lower_limit=lower_limit)
        if dst_srs is not None:
            self.set_srs(dst_srs=dst_srs)
            
        self.set_metadata(node=node)
        self.reformat(out_fmt=dst_fmt)
        self.move(out_dir=dst_dir)
            
    def set_nodata(self, ndv):
        if self.ds_config['ndv'] != ndv:
            gdalfun.gdal_set_ndv(self.fn, ndv=ndv, convert_array=True, verbose=self.verbose)
            self.ds_config['ndv'] = ndv

    def filter_(self, fltr = []):
        if len(fltr) > 0:
            for f in fltr:
                fltr_val = None
                split_val = None
                fltr_opts = f.split(':')
                fltr = fltr_opts[0]
                if len(fltr_opts) > 1:
                    fltr_val = fltr_opts[1]

                if len(fltr_opts) > 2:
                    split_val= fltr_opts[2]

                # fails if fltr_val in float
                if waffles_filter(
                        fn, '__tmp_fltr.tif', fltr=fltr, fltr_val=fltr_val, split_val=split_val,
                ) == 0:
                    os.replace('__tmp_fltr.tif', fn)
            
    def resample(self, region = None, xsample = None, ysample = None, ndv = -9999, sample_alg = 'cubicspline'):
        if xsample is not None or ysample is not None:
            warp_fn = os.path.join(self.cache_dir, '__tmp_sample.tif')
            if gdalfun.sample_warp(self.fn, warp_fn, xsample, ysample, src_region=region,
                                   sample_alg=sample_alg, ndv=ndv, verbose=self.verbose)[1] == 0:
                os.replace(warp_fn, self.fn)
                self.initialize()

    def clip(self, clip_str = None):
        ## todo: update for multi-band
        ## todo: re-add coastline option
        if clip_str is not None:
            clip_args = {}
            cp = clip_str.split(':')
            clip_args['src_ply'] = cp[0]
            clip_args['verbose'] = self.verbose
            clip_args = factory.args2dict(cp[1:], clip_args)

            #         if clip_args['src_ply'] == 'coastline':
            #             self.coast = WaffleFactory(mod='coastline:polygonize=False', data=self.data_, src_region=self.p_region,
            #                                        xinc=self.xsample if self.xsample is not None else self.xinc, yinc=self.ysample if self.ysample is not None else self.yinc,
            #                                        name='tmp_coast', node=self.node, want_weight=self.want_weight, want_uncertainty=self.want_uncertainty, dst_srs=self.dst_srs,
            #                                        srs_transform=self.srs_transform, clobber=True, verbose=self.verbose)._acquire_module()
            #             self.coast.initialize()
            #             self.coast.generate()
            #             gdalfun.gdal_mask(fn, self.coast.fn, '__tmp_clip__.tif', msk_value=1, verbose=self.verbose)
            #             os.replace('__tmp_clip__.tif', '{}'.format(fn))

            if os.path.exists(clip_args['src_ply']):
                if ogr.Open(clip_args['src_ply']) is not None:
                    tmp_clip = utils.make_temp_fn('__tmp_clip__.tif', temp_dir=self.cache_dir)
                    if gdalfun.gdal_clip(self.fn, tmp_clip, **clip_args)[1] == 0:
                        os.replace(tmp_clip, self.fn)
                        self.initialize()
                else:
                    utils.echo_error_msg('could not read {}'.format(clip_args['src_ply']))
                    
            else:
                utils.echo_error_msg('could not find clip ogr source/clip keyword {}'.format(clip_args['src_ply']))
                
    def cut(self, region = None):
        _tmp_cut, cut_status = gdalfun.gdal_cut(self.fn, region, utils.make_temp_fn('__tmp_cut__.tif', temp_dir=self.cache_dir), node='grid')        
        if cut_status == 0:
            os.replace(_tmp_cut, self.fn)
            self.initialize()

    def set_limits(self, upper_limit = None, lower_limit = None, band = 1):
        ## limit in other bands?? or chose band to limit??
        upper_limit = utils.float_or(upper_limit)
        lower_limit = utils.float_or(lower_limit)
        if upper_limit is not None or lower_limit is not None:
            dem_ds = gdal.Open(self.fn, 1)
            if dem_ds is not None:
                src_band = dem_ds.GetRasterBand(band)
                band_data = src_band.ReadAsArray()

                if upper_limit is not None:
                    if self.verbose:
                        utils.echo_msg('setting upper_limit to {}'.format(upper_limit))

                    band_data[band_data > upper_limit] = upper_limit

                if lower_limit is not None:
                    if self.verbose:
                        utils.echo_msg('setting lower_limit to {}'.format(lower_limit))

                    band_data[band_data < lower_limit] = lower_limit

                src_band.WriteArray(band_data)
                
                dem_ds = None
                self.initialize()

    def set_srs(self, dst_srs = None):
        gdalfun.gdal_set_srs(self.fn, src_srs=dst_srs, verbose=self.verbose)
        self.initialize()

    def reformat(self, out_fmt = None):
        if out_fmt is not None:
            if out_fmt != self.ds_config['fmt']:
                out_fn = '{}.{}'.format(utils.fn_basename2(self.fn), gdalfun.gdal_fext(out_fmt))
                out_ds = gdal.GetDriverByName(out_fmt).CreateCopy(out_fn, 0)
                if out_ds is not None:
                    utils.remove_glob(self.fn)
                    self.fn = out_fn
                    self.initialize()

    def move(self, out_dir = None):
        if out_dir is not None:
            out_fn = os.path.join(out_dir, os.path.basename(self.fn))
            os.replace(self.fn, out_fn)
            self.fn = out_fn
            self.initialize()
                    
    def set_metadata(self, cudem = False, node = 'pixel'):
        """add metadata to the waffled raster

        Args: 
          cudem (bool): add CUDEM metadata
        """
        
        dem_ds = gdal.Open(self.fn, 1)
        if dem_ds is not None:        
            md = self.ds_config['metadata']
            md['TIFFTAG_DATETIME'] = '{}'.format(utils.this_date())
            if node == 'pixel':
                md['AREA_OR_POINT'] = 'Area'
                md['NC_GLOBAL#node_offset'] = '1'
                md['tos#node_offset'] = '1'
            else:
                md['AREA_OR_POINT'] = 'Point'
                md['NC_GLOBAL#node_offset'] = '0'
                md['tos#node_offset'] = '0'

            if cudem:
                md['TIFFTAG_COPYRIGHT'] = 'DOC/NOAA/NESDIS/NCEI > National Centers for Environmental Information, NESDIS, NOAA, U.S. Department of Commerce'
                if self.ds_config['zr'][1] < 0:
                    tb = 'Bathymetry'
                elif self.ds_config['zr'][0] > 0:
                    tb = 'Topography'
                else:
                    tb = 'Topography-Bathymetry'

                srs=osr.SpatialReference(wkt=self.ds_config['proj'])
                vdatum=srs.GetAttrValue('vert_cs')
                md['TIFFTAG_IMAGEDESCRIPTION'] = '{}; {}'.format(tb, '' if vdatum is None else vdatum)

            dem_ds.SetMetadata(md)
            dem_ds = None
            
class WaffleFactory(factory.CUDEMFactory):
    _modules = {
        'stacks': {'name': 'stacks', 'stack': True, 'call': WafflesStacks},
        'IDW': {'name': 'IDW', 'stack': True, 'call': WafflesIDW},
        #'scipy': {'name': 'scipy', 'stack': True, 'call': WafflesSciPy},
        'linear': {'name': 'linear', 'stack': True, 'call': WafflesLinear},
        'cubic': {'name': 'cubic', 'stack': True, 'call': WafflesCubic},
        'nearest': {'name': 'nearest', 'stack': True, 'call': WafflesNearest},
        'gmt-surface': {'name': 'surface', 'stack': True, 'call':GMTSurface},
        'gmt-triangulate': {'name': 'triangulate','stack': True, 'call': GMTTriangulate},
        'gmt-nearneighbor': {'name': 'nearneihbor', 'stack': True, 'call': GMTNearNeighbor},
        'mbgrid': {'name': 'mbgrid', 'stack': True, 'call': WafflesMBGrid},
        'gdal-linear': {'name': 'linear', 'stack': True, 'call': GDALLinear},
        'gdal-nearest': {'name': 'nearest', 'stack': True, 'call': GDALNearest},
        'gdal-average': {'name': 'average', 'stack': True, 'call': GDALMovingAverage},
        'gdal-invdst': {'name': 'invdst', 'stack': True, 'call': GDALInvDst},
        'vdatum': {'name': 'vdatum', 'stack': False, 'call': WafflesVDatum},
        'coastline': {'name': 'coastline', 'stack': False, 'call': WafflesCoastline},
        'lakes': {'name': 'lakes', 'stack': False, 'call': WafflesLakes},
        'cudem': {'name': 'cudem', 'stack': True, 'call': WafflesCUDEM},
        'uncertainty': {'name': 'uncertainty', 'stack': True, 'call': WafflesUncertainty},
        'scratch': {'name': 'scratch', 'stack': True, 'call': WafflesScratch},
        'flatten': {'name': 'flatten', 'stack': True, 'call': WafflesFlatten},
        #'num': {'name': 'num', 'stack': True, 'call': WafflesNum}, # defunct
        #'patch': {'name': 'patch', 'stack': True, 'call': WafflesPatch},
        #'cube': {'name': 'cube', 'stack': True, 'call': WafflesCUBE},
        #'bgrid': {'name': 'bgrid', 'stack': True, 'call': WafflesBGrid},                
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _set_modules(self):
        pass
    
## ==============================================
## Command-line Interface (CLI)
## $ waffles
##
## waffles cli
## ==============================================
waffles_cli_usage = """{cmd} ({wf_version}): Generate DEMs and derivatives.

usage: {cmd} [OPTIONS] DATALIST

Options:
  -R, --region\t\t\tSpecifies the desired REGION;
\t\t\t\tWhere a REGION is xmin/xmax/ymin/ymax[/zmin/zmax[/wmin/wmax]]
\t\t\t\tUse '-' to indicate no bounding range; e.g. -R -/-/-/-/-10/10/1/-
\t\t\t\tOR an OGR-compatible vector file with regional polygons. 
\t\t\t\tWhere the REGION is /path/to/vector[:zmin/zmax[/wmin/wmax]].
\t\t\t\tIf a vector file is supplied, will use each region found therein.
  -E, --increment\t\tGridding INCREMENT and RESAMPLE-INCREMENT in native units.
\t\t\t\tWhere INCREMENT is x-inc[/y-inc][:sample-x-inc/sample-y-inc]
  -S, --sample_alg\t\tReSAMPLE algorithm to use (from gdalwarp)
\t\t\t\tSet as 'auto' to use 'average' when down-sampling and 'bilinear' when up-sampling
\t\t\t\tThis switch controls resampling of input raster datasets as well as resampling
\t\t\t\tthe final DEM if RESAMPLE-INCREMENT is set in -E
  -F, --format\t\t\tOutput grid FORMAT. [GTiff]
  -M, --module\t\t\tDesired Waffles MODULE and options. (see available Modules below)
\t\t\t\tWhere MODULE is module[:mod_opt=mod_val[:mod_opt1=mod_val1[:...]]]
  -O, --output-name\t\tBASENAME for all outputs.
  -P, --t_srs\t\t\tProjection of REGION and output DEM.
  -X, --extend\t\t\tNumber of cells with which to EXTEND the output DEM REGION and a 
\t\t\t\tpercentage to extend the processing REGION.
\t\t\t\tWhere EXTEND is dem-extend(cell-count)[:processing-extend(percentage)]
\t\t\t\te.g. -X6:10 to extend the DEM REGION by 6 cells and the processing region by 10 
\t\t\t\tpercent of the input REGION.
  -T, --filter\t\t\tFILTER the output DEM using one or multiple filters. 
\t\t\t\tWhere FILTER is fltr_id[:fltr_val[:split_value=z]]
\t\t\t\tAvailable FILTERS:
\t\t\t\t1: perform a Gaussian Filter at -T1:<factor>.
\t\t\t\t2: use a Cosine Arch Filter at -T2:<dist(km)> search distance.
\t\t\t\t3: perform an Outlier Filter at -T3:<aggression<1-9>>.
\t\t\t\tThe -T switch may be set multiple times to perform multiple filters.
\t\t\t\tAppend :split_value=<num> to only filter values below z-value <num>.
\t\t\t\te.g. -T1:10:split_value=0 to smooth bathymetry (z<0) using Gaussian filter
  -C, --clip\t\t\tCLIP the output to the clip polygon -C<clip_ply.shp:invert=False>
  -K, --chunk\t\t\tGenerate the DEM in CHUNKs
  -G, --wg-config\t\tA waffles config JSON file. If supplied, will overwrite all other options.
\t\t\t\tGenerate a waffles_config JSON file using the --config flag.
  -D, --cache-dir\t\tCACHE Directory for storing temp data.
\t\t\t\tDefault Cache Directory is ~/.cudem_cache; cache will be cleared after a waffles session
\t\t\t\tto retain the data, use the --keep-cache flag
  -N, --nodata\t\t\tNODATA value of output DEM
  -L, --limits\t\t\tLIMIT the output elevations values, append 'u<value>' for the upper limit or 'l<value' for lower limit

  -f, --transform\t\tTransform all data to PROJECTION value set with --t_srs/-P where applicable.
  -p, --prefix\t\t\tSet BASENAME (-O) to PREFIX (append <RES>_nYYxYY_wXXxXX_<YEAR>v<VERSION> info to output BASENAME).
\t\t\t\tnote: Set Resolution, Year and Version by setting this to 'res=X:year=XXXX:version=X', 
\t\t\t\tleave blank for default of <INCREMENT>, <CURRENT_YEAR> and <1>, respectively.
  -r, --grid-node\t\tUse grid-node registration, default is pixel-node
  -w, --want-weight\t\tUse weights provided in the datalist to weight overlapping data.
  -u, --want-uncertainty\tGenerate/Use uncertainty either calculated or provided in the datalist.
  -m, --want-mask\t\tMask the processed datalist.
  -a, --archive\t\t\tARCHIVE the datalist to the given region.
  -k, --keep-cache\t\tKEEP the cache data intact after run
  -x, --keep-auxiliary\t\tKEEP the auxiliary rastesr intact after run (mask, uncertainty, weights, count)
  -d, --supercede\t\thigher weighted data supercedes lower weighted data
  -s, --spatial-metadata\tGenerate SPATIAL-METADATA.
  -c, --continue\t\tDon't clobber existing files.
  -q, --quiet\t\t\tLower verbosity to a quiet.

  --help\t\t\tPrint the usage text
  --config\t\t\tSave the waffles config JSON and major datalist
  --modules\t\t\tDisplay the module descriptions and usage
  --version\t\t\tPrint the version information

Datalists and data formats:
  A datalist is a file that contains a number of datalist entries, 
  while an entry is a space-delineated line:
  `path [format weight [name source date type resolution hdatum vdatum url]]`

Supported datalist formats: 
  {dl_formats}

Modules (see waffles --modules <module-name> for more info):
  {modules}

CIRES DEM home page: <http://ciresgroups.colorado.edu/coastalDEM>
""".format(cmd=os.path.basename(sys.argv[0]),
           dl_formats=factory._cudem_module_name_short_desc(dlim.DatasetFactory._modules),
           modules=factory._cudem_module_short_desc(WaffleFactory._modules),
           wf_version=cudem.__version__)

## add upper/lower limit to cli (use same syntax as GMT, e.g. -Lu0 -Ll0
def waffles_cli(argv = sys.argv):
    """run waffles from command-line

    See `waffles_cli_usage` for full cli options.
    """
    
    dls = []
    i_regions = []
    these_regions = []
    module = 'scratch'
    wg_user = None
    want_prefix = False
    prefix_args = {}
    want_config = False
    keep_cache = False
    status = 0
    i = 1
    wg = {}
    wg['verbose'] = True
    wg['sample'] = 'bilinear'
    wg['xsample'] = None
    wg['ysample'] = None
    wg['dst_srs'] = None
    wg['srs_transform'] = False
    wg['fltr'] = []
    wg['name'] = 'waffles'
    wg['cache_dir'] = waffles_cache
    wg['ndv'] = -9999

    ## todo: gather multiple -R options (do for all)
    while i < len(argv):
        arg = argv[i]
        if arg == '--region' or arg == '-R':
            i_regions.append(str(argv[i + 1]))
            i += 1
        elif arg[:2] == '-R': i_regions.append(str(arg[2:]))
        elif arg == '--module' or arg == '-M':
            module = str(argv[i + 1])
            i += 1
        elif arg[:2] == '-M': module = str(arg[2:])
        elif arg == '--increment' or arg == '-E':
            incs = argv[i + 1].split(':')
            xy_inc = incs[0].split('/')
            wg['xinc'] = utils.str2inc(xy_inc[0])
            if len(xy_inc) > 1:
                wg['yinc'] = utils.str2inc(xy_inc[1])
            else:
                wg['yinc'] = utils.str2inc(xy_inc[0])
            if len(incs) > 1:
                xy_samples = incs[1].split('/')
                wg['xsample'] = utils.str2inc(xy_samples[0])
                if len(xy_samples) > 1:
                    wg['ysample'] = utils.str2inc(xy_samples[1])
                else:
                    wg['ysample'] = utils.str2inc(xy_samples[0])
            i = i + 1
        elif arg[:2] == '-E':
            incs = arg[2:].split(':')
            xy_inc = incs[0].split('/')
            wg['xinc'] = utils.str2inc(xy_inc[0])
            if len(xy_inc) > 1:
                wg['yinc'] = utils.str2inc(xy_inc[1])
            else:
                wg['yinc'] = utils.str2inc(xy_inc[0])
            if len(incs) > 1:
                xy_samples = incs[1].split('/')
                wg['xsample'] = utils.str2inc(xy_samples[0])
                if len(xy_samples) > 1:
                    wg['ysample'] = utils.str2inc(xy_samples[1])
                else:
                    wg['ysample'] = utils.str2inc(xy_samples[0])

        elif arg == '--sample_alg' or arg == '-S':
            wg['sample'] = argv[i + 1]
            i += 1
        elif arg[:2] == '-S': wg['sample'] = arg[2:]
                    
        elif arg == '--outname' or arg == '-O':
            wg['name'] = argv[i + 1]
            i += 1
        elif arg[:2] == '-O': wg['name'] = arg[2:]
        elif arg == '--format' or arg == '-F':
            wg['fmt'] = argv[i + 1]
            i += 1
        elif arg[:2] == '-F': wg['fmt'] = arg[2:]
        elif arg == '--filter' or arg == '-T':
            wg['fltr'].append(argv[i + 1])
            i += 1
        elif arg[:2] == '-T': wg['fltr'].append(arg[2:])
        elif arg == '--extend' or arg == '-X':
            exts = argv[i + 1].split(':')
            wg['extend'] = utils.int_or(exts[0], 0)
            if len(exts) > 1: wg['extend_proc'] = utils.int_or(exts[1], 10)
            i += 1
        elif arg[:2] == '-X':
            exts = arg[2:].split(':')
            wg['extend'] = utils.int_or(exts[0], 0)
            if len(exts) > 1: wg['extend_proc'] = utils.int_or(exts[1], 10)
        elif arg == '--wg-config' or arg == '-G':
            wg_user = argv[i + 1]
            i += 1
        elif arg[:2] == '-G': wg_user = arg[2:]
        elif arg == '--clip' or arg == '-C':
            wg['clip'] = argv[i + 1]
            i = i + 1
        elif arg[:2] == '-C': wg['clip'] = arg[2:]
        elif arg == '--chunk' or arg == '-K':
            wg['chunk'] = utils.int_or(argv[i + 1], None)
            i = i + 1
        elif arg[:2] == '-K': wg['chunk'] = utils.int_or(arg[2:], None)
        elif arg == '--t_srs' or arg == '-P' or arg == '-t_srs':
            wg['dst_srs'] = utils.str_or(argv[i + 1], 'epsg:4326')
            i = i + 1
        elif arg[:2] == '-P': wg['dst_srs'] = utils.str_or(arg[2:], 'epsg:4326')
        ## update cache_dir to default to current utils.cache_dir or if an arg, dont add .cudem_cache!
        elif arg == '--cache-dir' or arg == '-D' or arg == '-cache-dir':
            wg['cache_dir'] = os.path.join(utils.str_or(argv[i + 1], os.path.expanduser('~')), '.cudem_cache')
            i = i + 1
        elif arg[:2] == '-D': wg['cache_dir'] = os.path.join(utils.str_or(argv[i + 1], os.path.expanduser('~')), '.cudem_cache')
        elif arg == '--nodata' or arg == '-N' or arg == '-ndv':
            wg['ndv'] = utils.float_or(argv[i + 1], -9999)
            i = i + 1
        elif arg[:2] == '-D': wg['ndv'] = utils.float_or(argv[i + 1], -9999)

        elif arg == '--limits' or arg == '-L':
            this_limit = argv[i + 1]
            if this_limit.startswith('u'):
                wg['upper_limit'] = utils.float_or(this_limit[1:])
            elif this_limit.startswith('l'):
                wg['lower_limit'] = utils.float_or(this_limit[1:])
                
            i = i + 1
        elif arg[:2] == '-L':
            this_limit = argv[i + 1]
            if this_limit.startswith('u'):
                wg['upper_limit'] = utils.float_or(this_limit[1:])
            elif this_limit.startswith('l'):
                wg['lower_limit'] = utils.float_or(this_limit[1:])
        
        elif arg == '--transform' or arg == '-f' or arg == '-transform':
            wg['srs_transform'] = True
            if wg['dst_srs'] is None:
                wg['dst_srs'] = 'epsg:4326'
                
        elif arg == '-w' or arg == '--want-weight':
            wg['want_weight'] = True
            
        elif arg == '-u' or arg == '--want-uncertainty':
            wg['want_uncertainty'] = True
            #wg['want_mask'] = True
            #wg['keep_auxiliary'] = True
            
        elif arg == '-p' or arg == '--prefix':
            want_prefix = True
            try:
                prefix_opts = argv[i + 1].split(':')
                prefix_args = factory.args2dict(prefix_opts, prefix_args)
                if len(prefix_args) > 0:
                    i += 1
            except:
                pass

        elif arg == '--mask' or arg == '-m': wg['want_mask'] = True
        elif arg == '-k' or arg == '--keep-cache': keep_cache = True
        elif arg == '-x' or arg == '--keep-auxiliary': wg['keep_auxiliary'] = True
        elif arg == '-t' or arg == '--threads': want_threads = True
        elif arg == '-a' or arg == '--archive': wg['archive'] = True
        elif arg == '-d' or arg == '--supercede': wg['supercede'] = True
        elif arg == '-s' or arg == '--spatial-metadata': wg['want_sm'] = True
        elif arg == '-c' or arg == '--continue': wg['clobber'] = False
        elif arg == '-r' or arg == '--grid-node': wg['node'] = 'grid'

        elif arg == '--quiet' or arg == '-q': wg['verbose'] = False
        elif arg == '--config': want_config = True
        elif arg == '--modules':
            factory.echo_modules(WaffleFactory._modules, None if i+1 >= len(argv) else sys.argv[i+1])
            sys.exit(0)
        elif arg == '--help' or arg == '-h':
            sys.stderr.write(waffles_cli_usage)
            sys.exit(0)
        elif arg == '--version' or arg == '-v':
            sys.stdout.write('{}\n'.format(cudem.__version__))
            sys.exit(0)
        elif arg[0] == '-':
            sys.stderr.write(waffles_cli_usage)
            utils.echo_error_msg('{} is not a valid waffles cli switch'.format(arg))
            sys.exit(0)
        else: dls.append(arg)
        i += 1

    ## ==============================================
    ## load the user wg json and run waffles with that.
    ## ==============================================
    if wg_user is not None:
        if os.path.exists(wg_user):
            try:
                with open(wg_user, 'r') as wgj:
                    wg = json.load(wgj)
                    if wg['kwargs']['src_region'] is not None:
                        wg['kwargs']['src_region'] = regions.Region().from_list(
                            wg['kwargs']['src_region']
                        )
            
                this_waffle = WaffleFactory(mod=wg['mod'], **wg['kwargs'])
                this_waffle_module = this_waffle._acquire_module()
                #this_wg = this_waffle._export_config(parse_data=False)
                # with utils.CliProgress(
                #         message='Generating: {}'.format(this_waffle),
                #         verbose=wg['kwargs']['verbose'],
                # ) as pbar:
                with tqdm(
                        desc='Generating: {}'.format(this_waffle),
                ) as pbar:
                    this_waffle_module.initialize()
                    this_waffle_module.generate()
            except (KeyboardInterrupt, SystemExit):
                utils.echo_error_msg('user breakage...please wait while waffles exits...')
                sys.exit(-1)
            except Exception as e:
                utils.echo_error_msg(e)
                traceback.print_exc()
                sys.exit(-1)

            sys.exit(0)
        else:
            utils.echo_error_msg(
                'specified waffles config file does not exist, {}'.format(wg_user)
            )
            sys.stderr.write(waffles_cli_usage)
            sys.exit(-1)

    ## ==============================================
    ## Otherwise run from cli options...
    ## set the dem module
    ## ==============================================        
    # if module is None:
    #     # sys.stderr.write(waffles_cli_usage)
    #     # utils.echo_error_msg(
    #     #     '''must specify a waffles -M module.'''
    #     # )
    #     # sys.exit(-1)

    if module.split(':')[0] not in WaffleFactory()._modules.keys():
        utils.echo_error_msg(
            '''{} is not a valid waffles module, available modules are: {}'''.format(
                module.split(':')[0], factory._cudem_module_short_desc(WaffleFactory._modules)
            )
        )
        sys.exit(-1)
        
    if WaffleFactory()._modules[module.split(':')[0]]['stack']:
        if len(dls) == 0:
            sys.stderr.write(waffles_cli_usage)
            utils.echo_error_msg('''must specify a datalist/entry, try gmrt or srtm for global data.''')
            sys.exit(-1)
    else:
        wg['want_stack'] = True if len(dls) > 0 else False

    ## ==============================================
    ## check the increment
    ## ==============================================
    if 'xinc' in wg.keys():
        if wg['xinc'] is None:
            sys.stderr.write(waffles_cli_usage)
            utils.echo_error_msg('''must specify a gridding increment.''')
            sys.exit(-1)
        else:
            if wg['yinc'] is None:
                wg['yinc'] = wg['xinc']
    else:
        sys.stderr.write(waffles_cli_usage)
        utils.echo_error_msg('''must specify a gridding increment.''')
        sys.exit(-1)      
    
    ## ==============================================
    ## set the datalists and names
    ## ==============================================
    wg['data'] = dls    
    these_regions = regions.parse_cli_region(i_regions, wg['verbose'])
    name = wg['name']

    for i, this_region in enumerate(these_regions):
        wg['src_region'] = this_region
        if want_prefix or len(these_regions) > 1:
            wg['name'] = utils.append_fn(
                name, wg['src_region'], wg['xsample'] if wg['xsample'] is not None else wg['xinc'], **prefix_args
            )
        if want_config:
            #wg['src_region'] = this_region.format('cudem')
            wg['src_region'] = this_region.export_as_list()
            this_waffle = WaffleFactory(mod=module, **wg)
            print(this_waffle)
            this_waffle.write_parameter_file('{}.json'.format(this_waffle['kwargs']['name']))
            #this_wg = this_waffle._export_config(parse_data=True)
            #utils.echo_msg(json.dumps(this_wg, indent=4, sort_keys=True))
            #with open('{}.json'.format(this_wg['name']), 'w') as wg_json:
            #    utils.echo_msg('generating waffles config file: {}.json'.format(this_wg['name']))
            #    wg_json.write(json.dumps(this_wg, indent=4, sort_keys=True))
        else:
            this_waffle = WaffleFactory(mod=module, **wg)
            #print(this_waffle)
            if this_waffle is not None:
                #this_wg = this_waffle._export_config(parse_data=False)
                this_waffle_module = this_waffle._acquire_module()
                #print(this_waffle_module)
                if this_waffle_module is not None:
                    this_waffle_module()
                    # with utils.CliProgress(
                    #         message='Generating: {}'.format(this_waffle),
                    #         verbose=wg['verbose'],
                    # ) as pbar:
                    #     try:
                    #this_waffle_module.initialize()
                    #this_waffle_module.generate()
                    #this_waffle_module()
                    # except (KeyboardInterrupt, SystemExit):
                    #     utils.echo_error_msg('user breakage...please wait while waffles exits....')
                    #     sys.exit(-1)
                    # except Exception as e:
                    #     utils.echo_error_msg(e)
                    #     sys.exit(-1)
                else:
                    if wg['verbose']:
                        utils.echo_error_msg('could not acquire waffles module {}'.format(module))
                        
        #if not keep_cache:
        #    utils.remove_glob(wg['cache_dir'])
### End
