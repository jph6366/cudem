### waffles.py
##
## Copyright (c) 2010 - 2021 CIRES Coastal DEM Team
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
### Code:

import sys
import os
import math
import numpy as np
from osgeo import gdal
from osgeo import ogr
import cudem
from cudem import dlim
from cudem import datasets
from cudem import regions
from cudem import utils
from cudem import xyzfun
from cudem import demfun
from cudem import metadata
from cudem import vdatumfun

class Waffle:
    """Representing a WAFFLES DEM/MODULE.
    Specific Gridding modules are sub-classes of this class.
    See WaffleFactory for module specifications and generation.
    """
    
    def __init__(self, data=[], src_region=None, inc=None, name='waffles_dem',
                 node='pixel', fmt='GTiff', extend=0, extend_proc=20, weights=None,
                 fltr=[], sample=None, clip=None, chunk=None, epsg=4326,
                 verbose=False, archive=False, mask=False, spat=False, clobber=True):

        self.data = data
        self.region = src_region
        self.inc = inc
        self.name = name
        self.node = node
        self.fmt = fmt
        self.extend = extend
        self.extend_proc = extend_proc
        self.weights = weights
        self.fltr = fltr
        self.sample = sample
        self.clip = clip
        self.chunk = chunk
        self.epsg = utils.int_or(epsg)
        self.mod = None
        self.archive = archive
        self.mask = mask
        self.clobber = clobber
        self.verbose = verbose
        self.gc = utils.config_check()
        self.spat = spat
        self.block_t = None
        self.ogr_ds = None
        
        if self.node == 'grid':
            self.region = self.region.buffer(self.inc*.5)
        self.p_region = self._proc_region()
        self.d_region = self._dist_region()

        self.data_ = data

        self.data = [dlim.DatasetFactory(
            fn=" ".join(['-' if x == "" else x for x in dl.split(":")]),
            src_region=self.p_region, verbose=self.verbose, weight=self.weights,
            epsg=self.epsg).acquire_dataset() for dl in self.data]

        self.data = [d for d in self.data if d is not None]
        
        self.fn = '{}.tif'.format(self.name)            
        self.waffled = False

    ## ==============================================
    ## Waffles REGIONS
    ##
    ## proc_region - processing region (extended by self.extend and self.extend_proc
    ## dist_region - distribution region (extended by self.extend)
    ## ==============================================
    def _proc_region(self):
        
        pr = regions.Region().from_region(self.region)
        return(pr.buffer((self.inc*self.extend_proc) + (self.inc*self.extend)))

    def _dist_region(self):
            
        dr = regions.Region().from_region(self.region)
        return(dr.buffer((self.inc*self.extend)))

    ## ==============================================
    ## XYZ Spatial Indexing
    ##
    ## xyz_block_t - grid the xyz data into data buckets
    ## xyz_ds - generate an OGR in-memory vector dataset
    ## ==============================================
    def _xyz_block_t(self, src_xyz):
        """block the src_xyz data for fast lookup"""

        xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.inc)
        self.block_t = np.empty((ycount, xcount), dtype=object)
        for y in range(0, ycount):
            for x in range(0, xcount):
                self.block_t[y,x] = []

        if self.verbose: utils.echo_msg('blocking data to {}/{} grid'.format(ycount, xcount))
        for this_xyz in src_xyz:
            if regions.xyz_in_region_p(this_xyz, self.p_region):
                xpos, ypos = utils._geo2pixel(this_xyz.x, this_xyz.y, dst_gt)
                if xpos < xcount and ypos < ycount:
                    self.block_t[ypos,xpos].append(xyzfun.XYZPoint().from_list(
                        this_xyz.export_as_list(include_z=True, include_w=True)))

    def _xyz_ds(self, src_xyz):
        """Make a point vector OGR DataSet Object from src_xyz"""

        dst_ogr = '{}'.format(self.name)
        self.ogr_ds = gdal.GetDriverByName('Memory').Create('', 0, 0, 0, gdal.GDT_Unknown)
        layer = self.ogr_ds.CreateLayer(dst_ogr, geom_type = ogr.wkbPoint25D)
        fd = ogr.FieldDefn('long', ogr.OFTReal)
        fd.SetWidth(10)
        fd.SetPrecision(8)
        layer.CreateField(fd)
        fd = ogr.FieldDefn('lat', ogr.OFTReal)
        fd.SetWidth(10)
        fd.SetPrecision(8)
        layer.CreateField(fd)
        fd = ogr.FieldDefn('elev', ogr.OFTReal)
        fd.SetWidth(12)
        fd.SetPrecision(12)
        layer.CreateField(fd)
        if self.weights:
            fd = ogr.FieldDefn('weight', ogr.OFTReal)
            fd.SetWidth(6)
            fd.SetPrecision(6)
            layer.CreateField(fd)
        f = ogr.Feature(feature_def = layer.GetLayerDefn())
        
        for this_xyz in self.yield_xyz():#src_xyz:
            f.SetField(0, this_xyz.x)
            f.SetField(1, this_xyz.y)
            f.SetField(2, this_xyz.z)
            if self.weights:
                f.SetField(3, this_xyz.w)
            wkt = this_xyz.export_as_wkt(include_z=True)
            g = ogr.CreateGeometryFromWkt(wkt)
            f.SetGeometryDirectly(g)
            layer.CreateFeature(f)
        return(self.ogr_ds)

    ## ==============================================
    ## XYZ data yield functions
    ##
    ## xyz_block - block and yield the xyz data
    ## xyz_mask - mask and yield the xyz data
    ## yield_xyz - yield the xyz data
    ## ============================================== 
    def _xyz_block(self, src_xyz):
        """block the src_xyz data to the mean block value

        Yields:
          list: xyz data for each block with data
        """

        xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.inc)
        sumArray = np.zeros((ycount, xcount))
        gdt = gdal.GDT_Float32
        ptArray = np.zeros((ycount, xcount))
        if self.weights:
            wtArray = np.zeros((ycount, xcount))
        if self.verbose:
            utils.echo_msg('blocking data to {}/{} grid'.format(ycount, xcount))
        for this_xyz in src_xyz:
            if self.weights: this_xyz.z = this_xyz.z * this_xyz.w
            if regions.xyz_in_region_p(this_xyz, self.p_region):
                xpos, ypos = utils._geo2pixel(this_xyz.x, this_xyz.y, dst_gt)
                try:
                    sumArray[ypos, xpos] += this_xyz.z
                    ptArray[ypos, xpos] += 1
                    if weights: wtArray[ypos, xpos] += this_xyz.w
                except: pass

        ptArray[ptArray == 0] = np.nan
        if self.weights:
            wtArray[wtArray == 0] = 1
            outarray = (sumArray / wtArray) / ptArray
        else: outarray = sumArray / ptArray

        sumArray = ptArray = None
        if self.weights: wtArray = None

        outarray[np.isnan(outarray)] = -9999

        for y in range(0, ycount):
            for x in range(0, xcount):
                geo_x, geo_y = utils._pixel2geo(x, y, dst_gt)
                out_xyz = xyzfun.XYZPoint(x=geo_x, y=geo_y, z=outarray[y,x])
                if out_xyz.z != -9999:
                    yield(out_xyz)
                        
    def _xyz_mask(self, src_xyz, dst_gdal, dst_format='GTiff'):
        """Create a num grid mask of xyz data. The output grid
        will contain 1 where data exists and 0 where no data exists.

        yields the xyz data
        """

        xcount, ycount, dst_gt = self.region.geo_transform(x_inc=self.inc)
        ptArray = np.zeros((ycount, xcount))
        ds_config = demfun.set_infos(
            xcount, ycount, (xcount*ycount), dst_gt, utils.sr_wkt(self.epsg),
            gdal.GDT_Float32, -9999, 'GTiff')
        for this_xyz in src_xyz:
            yield(this_xyz)
            if regions.xyz_in_region_p(this_xyz, self.region):
                xpos, ypos = utils._geo2pixel(this_xyz.x, this_xyz.y, dst_gt)
                try:
                    ptArray[ypos, xpos] = 1
                except:
                    pass
        out, status = utils.gdal_write(ptArray, dst_gdal, ds_config)    

    ## TODO: allow spat-meta and archive at same time...
    def yield_xyz(self, block=False, **kwargs):
        """yields the xyz data"""
        
        for xdl in self.data:
            if self.spat:
                xyz_yield = metadata.SpatialMetadata(
                    data=[xdl.fn], src_region=self.p_region, inc=self.inc, extend=self.extend, epsg=self.epsg,
                    node=self.node, name=self.name, verbose=self.verbose).yield_xyz()
            elif self.archive:
                xyz_yield = xdl.archive_xyz()
            else:
                xyz_yield = xdl.yield_xyz()
            if self.mask:
                xyz_yield = self._xyz_mask(xyz_yield, '{}_m.tif'.format(self.name))
            if block:
                xyz_yield = self._xyz_block(xyz_yield)
            for xyz in xyz_yield:
                yield(xyz)

        if self.spat:
            dst_layer = '{}_sm'.format(self.name)
            dst_vector = dst_layer + '.shp'
            utils.run_cmd(
                'ogr2ogr -clipsrc {} __tmp_clip.shp {} -overwrite -nlt POLYGON -skipfailures'.format(self.d_region.format('ul_lr'), dst_vector),
                verbose=True)
            utils.run_cmd(
                'ogr2ogr {} __tmp_clip.shp -overwrite'.format(dst_vector),
                verbose=True)
            utils.remove_glob('__tmp_clip.*')

    def dump_xyz(self, dst_port=sys.stdout, encode=False, **kwargs):
        for xyz in self.yield_xyz(**kwargs):
            xyz.dump(include_w = True if self.weights is not None else False, dst_port=dst_port, encode=encode, **kwargs)

    ## ==============================================
    ## DEM generation
    ##
    ## run - run the WAFFLES module (set via module class)
    ## process - process the outpt WAFFLES DEM (filter, clip, cut, set)
    ## generate - run and process the WAFFLES module
    ## valid_p - check if the output WAFFLES DEM is valid
    ## ============================================== 
    def _process(self, fn=None, filter_=False):
        if fn is None:
            fn = self.fn
        
        demfun.set_nodata(fn, nodata=-9999, convert_array=True)
        
        if filter_:
            if len(self.fltr) > 0:
                for f in self.fltr:
                    fltr_val = None
                    split_val = None
                    fltr_opts = f.split(':')
                    fltr = fltr_opts[0]
                    if len(fltr_opts) > 1:
                        fltr_val = fltr_opts[1]
                    if len(fltr_opts) > 2:
                        split_val= fltr_opts[2]

                    if demfun.filter_(fn, '__tmp_fltr.tif', fltr=fltr, fltr_val=fltr_val) == 0:
                        os.rename('__tmp_fltr.tif', fn)
            
        if self.sample is not None:
            if demfun.sample(fn, '__tmp_sample.tif', self.sample, self.d_region)[1] == 0:
                os.rename('__tmp_sample.tif', fn)
            
        if self.clip is not None:
            if demfun.clip(fn, src_ply=self.clip)[1] == 0:
                os.rename('__tmp_cut__.tif', '{}'.format(fn))
                
        if demfun.cut(fn, self.d_region, '__tmp_cut__.tif')[1] == 0:
            os.rename('__tmp_cut__.tif', '{}'.format(fn))
            
        demfun.set_metadata(fn, node=self.node)
        demfun.set_epsg(fn, self.epsg)
        
        if self.fmt != 'GTiff':
            out_dem = utils.gdal2gdal(fn, dst_fmt=self.fmt)
            if out_dem is not None:
                utils.remove_glob(fn)
        return(self)
    
    def generate(self):
        self.run()
        if self.mask:
            self._process(fn='{}_m.tif'.format(self.name), filter_=False)
        self.waffled = True
        return(self._process(filter_=True))

    def valid_p(self):
        if not os.path.exists(self.fn): return(False)
        gdi = demfun.infos(self.fn, scan=True)
        
        if gdi is not None:
            if np.isnan(gdi['zr'][0]):
                return(False)
        else:
            return(False)
        return(True)            

    def run(self):
        raise(NotImplementedError)
    
## ==============================================
## Waffles GMT surface module
## ==============================================
class GMTSurface(Waffle):
    """Grid the data using GMT 'surface'.
    Data passes through GMT 'blockmean' using weighted mean value if self.weights is True
    """
    
    def __init__(self, tension = .35, relaxation = 1.2,
                 lower_limit = 'd', upper_limit = 'd', **kwargs):
        """generate a DEM with GMT surface"""

        super().__init__(**kwargs)
        
        if self.gc['GMT'] is None:
            utils.echo_error_msg('GMT must be installed to use the SURFACE module')
            return(None, -1)
        
        self.tension = tension
        self.relaxation = relaxation
        self.lower_limit = lower_limit
        self.upper_limit = upper_limit

        out, status = utils.run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)
        
        self.mod = 'surface'
        
    def run(self):        
        dem_surf_cmd = ('gmt blockmean {} -I{:.10f}{} -V -r | gmt surface -V {} -I{:.10f} -G{}.tif=gd+n-9999:GTiff -T{} -Z{} -Ll{} -Lu{} -r\
        '.format(self.p_region.format('gmt'), self.inc, ' -Wi' if self.weights else '', self.p_region.format('gmt'),
                 self.inc, self.name, self.tension, self.relaxation, self.lower_limit, self.upper_limit))
        out, status = utils.run_cmd(dem_surf_cmd, verbose = self.verbose, data_fun = lambda p: self.dump_xyz(dst_port=p, encode=True))
        return(self)

## ==============================================
## Waffles GMT triangulate module
## ==============================================
class GMTTriangulate(Waffle):
    """Grid the data using GMT 'triangulate'.

    Returns:
      self: Waffle Object
    """
    
    def __init__(self, **kwargs):
        """generate a DEM with GMT triangulate"""

        super().__init__(**kwargs)
        
        if self.gc['GMT'] is None:
            utils.echo_error_msg('GMT must be installed to use the TRIANGULATE module')
            return(None, -1)

        out, status = utils.run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)
        
        self.mod = 'triangulate'
        
    def run(self):
        dem_tri_cmd = 'gmt triangulate -V {} -I{:.10f} -G{}.tif=gd:GTiff -r'.format(self.p_region.format('gmt'), self.inc, self.name)
        out, status = utils.run_cmd(dem_tri_cmd, verbose = self.verbose,
                                    data_fun = lambda p: self.dump_xyz(dst_port=p, encode=True))
        return(self)

## ==============================================
## Waffles MBGrid module
## ==============================================
class WafflesMBGrid(Waffle):
    def __init__(self, dist='10/3', tension=35, use_datalists=False, **kwargs):
        super().__init__(**kwargs)

        if self.gc['MBGRID'] is None:
            utils.echo_error_msg('MB-System must be installed to use the MBGRID module')
            return(None, -1)

        self.dist = dist
        self.tension = tension
        self.use_datalists = use_datalists
        self.mod = 'mbgird'

    def run(self):
        raise(NotImplementedError)
        
# def waffles_mbgrid(wg, dist = '10/3', tension = 35, use_datalists = False):
#     """Generate a DEM with MBSystem's mbgrid program.
    
#     Args:
#       wg (dict): a waffles config dictionary
#       dist (str): mbgrid -C switch
#       tension (float): spline tension while gridding
#       use_datalists (bool): use geomods datalists to parse data
    
#     if `use_datalists` is True, will parse the datalist through
#     waffles instead of mbsystem.
#     Note: without `use_datalists` as True, only mbsystem supported data formats may be used.

#     Returns:
#       list: [{'dem': ['dem-fn', 'raster']}, status]    
#     """
    
#     if wg['gc']['MBGRID'] is None:
#         utils.echo_error_msg('MBSystem must be installed to use the MBGRID module')
#         return(None, -1)
#     if wg['gc']['GMT'] is None:
#         utils.echo_error_msg('GMT must be installed to use the MBGRID module')
#         return(None, -1)

#     if use_datalists:
#         #datalist_archive(wg, arch_dir = '.mb_tmp_datalist', verbose = True)
#         archive = wg['archive']
#         wg['archive'] = True
#         for xyz in waffles_yield_datalist(wg): pass
#         wg['datalist'] = datalists.datalist_major(['archive/{}.datalist'.format(wg['name'])])
#         wg['archive'] = archive

#     region = waffles_proc_region(wg)
#     region = regions.region_buffer(region, wg['inc'] * -.5)
#     xsize, ysize, gt = regions.region2gt(waffles_proc_region(wg), wg['inc'])
    
#     if len(dist.split('/')) == 1: dist = dist + '/2'
#     mbgrid_cmd = ('mbgrid -I{} {} -D{}/{} -O{} -A2 -G100 -F1 -N -C{} -S0 -X0.1 -T{} {} \
#     '.format(wg['datalist'], regions.region_format(region, 'gmt'), xsize, ysize, wg['name'], dist, tension, '-M' if wg['mask'] else ''))
#     for out in utils.yield_cmd(mbgrid_cmd, verbose = wg['verbose']): sys.stderr.write('{}'.format(out))
#     #out, status = utils.run_cmd(mbgrid_cmd, verbose = wg['verbose'])

#     gmtfun.gmt_grd2gdal('{}.grd'.format(wg['name']))
#     utils.remove_glob('*.cmd', '*.mb-1', '{}.grd'.format(wg['name']))
#     if use_datalists and not wg['archive']: utils.remove_glob('archive')

#     if wg['mask']:
#         num_grd = '{}_num.grd'.format(wg['name'])
#         dst_msk = '{}_msk.tif=gd+n-9999:GTiff'.format(wg['name'])
#         out, status = gmtfun.gmt_num_msk(num_grd, dst_msk, verbose = wg['verbose'])
#         utils.remove_glob(num_grd, '*_sd.grd')
#     if not use_datalists:
#         if wg['spat'] or wg['archive']:
#             for xyz in waffles_yield_datalist(wg): pass
#     return({'dem': ['{}.tif'.format(wg['name']), 'raster']}, 0)
    
## ==============================================
## Waffles 'NUM grid' module
## ==============================================
class WafflesNum(Waffle):
    """Generate an Uninterpolated grid from XYZ data;
    num methods include: mask, mean, num, landmask and any gmt grd2xyz -A option.
    """
    
    def __init__(self, mode='n', **kwargs):
        """generate an uninterpolated Grid
        `mode` of `n` generates a num grid
        `mode` of `m` generates a mean grid
        `mode` of `k` generates a mask grid
        `mode` of `w` generates a wet/dry mask grid
        `mode` of `A` generates via GMT 'xyz2grd'
        """

        super().__init__(**kwargs)        
        self.mode = mode
        self.mod = 'num'

    def _xyz_num(self, src_xyz):
        """Create a GDAL supported grid from xyz data """

        xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.inc)
        if self.verbose:
            progress = utils.CliProgress('generating uninterpolated num grid {} @ {}/{}'.format(self.mode, ycount, xcount))
        if self.mode == 'm' or self.mode == 'w':
            sumArray = np.zeros((ycount, xcount))
        gdt = gdal.GDT_Float32
        ptArray = np.zeros((ycount, xcount))
        ds_config = demfun.set_infos(xcount, ycount, xcount * ycount, dst_gt, utils.sr_wkt(self.epsg), gdt, -9999, 'GTiff')
        for this_xyz in src_xyz:
            if regions.xyz_in_region_p(this_xyz, self.p_region):
                xpos, ypos = utils._geo2pixel(this_xyz.x, this_xyz.y, dst_gt)
                try:
                    if self.mode == 'm' or self.mode == 'w':
                        sumArray[ypos, xpos] += this_xyz.z
                    if self.mode == 'n' or self.mode == 'm':
                        ptArray[ypos, xpos] += 1
                    else: ptArray[ypos, xpos] = 1
                except Exception as e:
                    pass
        if self.mode == 'm' or self.mode == 'w':
            ptArray[ptArray == 0] = np.nan
            outarray = sumArray / ptArray
            if self.mode == 'w':
                outarray[outarray >= 0] = 1
                outarray[outarray < 0] = 0
        elif self.mode == 'n':
            outarray = ptArray
        else:
            outarray = ptArray
        outarray[np.isnan(outarray)] = -9999
        if self. verbose:
            progress.end(0, 'generated uninterpolated num grid {} @ {}/{}'.format(self.mode, ycount, xcount))
        utils.gdal_write(outarray, self.fn, ds_config)
        
    def run(self):
        if self.mode.startswith('A'):
            if self.gc['GMT'] is None:
                utils.echo_error_msg('GMT must be installed to use the Mode `A` with the NUM module')
                return(None, -1)

            out, status = utils.run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)
            
            dem_xyz2grd_cmd = ('gmt xyz2grd -{} -V {} -I{:.10f} -G{}.tif=gd:GTiff -r{}'.format(
                self.mode, self.p_region.format('gmt'), self.inc, self.name, ' -Wi' if self.weights else '',))
            out, status = utils.run_cmd(dem_xyz2grd_cmd, verbose=self.verbose,
                                        data_fun=lambda p: self.dump_xyz(dst_port=p, encode=True))
        else:
            self._xyz_num(self.yield_xyz(block=True))
            
        return(self)

class WafflesIDW(Waffle):
    """Generate an IDW DEM.
    If self.weights is True, use weights as uncertainty values
    to generate a UIDW DEM as described here: 
    https://ir.library.oregonstate.edu/concern/graduate_projects/79407x932
    """
    
    def __init__(self, radius='1', power=2, **kwargs):
        super().__init__(**kwargs)
        self.radius = utils.str2inc(radius)
        self.power = power
        self.mod = 'IDW'

    def _distance(self, pnt0, pnt1):
        return(math.sqrt(sum([(a-b)**2 for a, b in zip(pnt0, pnt1)])))
    
    def run(self):
        xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.inc)
        ds_config = demfun.set_infos(xcount, ycount, xcount * ycount, dst_gt,
                                     utils.sr_wkt(self.epsg), gdal.GDT_Float32,
                                     -9999, self.fmt)
        outArray = np.empty((ycount, xcount))
        outArray[:] = np.nan

        if self.verbose:
            progress = utils.CliProgress('generating IDW grid @ {} and {}/{}'.format(self.radius, ycount, xcount))
            i=0

        self._xyz_block_t(self.yield_xyz())
        #hash_t, xyz_t = xyz_block_t(self.yield_xyz(), self.p_region, self.inc)
            
        for y in range(0, ycount):
            if self.verbose:
                i+=1
                progress.update_perc((i, ycount))
                
            for x in range(0, xcount):
                xyz_bucket = []
                z_list = []
                dw_list = []
                if self.weights:
                    ww_list = []

                xg, yg = utils._pixel2geo(x, y, dst_gt)
                block_region = regions.Region(xmin=xg-self.radius, xmax=xg+self.radius,
                                              ymin=yg-self.radius, ymax=yg+self.radius)
                srcwin = block_region.srcwin(dst_gt, xcount, ycount)
                
                for y_i in range(srcwin[1], srcwin[1] + srcwin[3], 1):
                    for x_i in range(srcwin[0], srcwin[0] + srcwin[2], 1):
                        for b in self.block_t[y_i, x_i]:
                            xyz_bucket.append(b)

                for this_xyz in xyz_bucket:
                    d = self._distance([this_xyz.x, this_xyz.y], [xg, yg])
                    #d = utils.euc_dst([this_xyz.x, this_xyz.y], [xg, yg])
                    z_list.append(this_xyz.z)
                    if d > 0:
                        dw_list.append(1./(d**self.power))
                    else: dw_list.append(0)
                    
                    if self.weights:
                        w = this_xyz.w
                        if w > 0:
                            ww_list.append(1./(w**self.power))
                        else: ww_list.append(0)

                if len(dw_list) > 0:
                    dwt = np.transpose(dw_list)
                    if self.weights:
                        wwt = np.transpose(ww_list)
                        den = sum(np.array(dw_list)*np.array(ww_list))
                        if den > 0:
                            outArray[y,x] = np.dot(z_list, (np.array(dwt)*np.array(wwt)))/den
                        else: outArray[y,x] = sum(z_list)/len(z_list)
                        
                    else:
                        dw_list_sum = sum(dw_list)
                        if dw_list_sum > 0:
                            outArray[y,x] = np.dot(z_list, dwt)/dw_list_sum
                        else: outArray[y,x] = sum(z_list)/len(z_list)
        if self.verbose:
            progress.end(0, 'generated IDW grid {}/{}'.format(ycount, xcount))
        ds = None            
        outArray[np.isnan(outArray)] = -9999
        out, status = utils.gdal_write(outArray, '{}.tif'.format(self.name), ds_config)
        return(self)
            
## ==============================================
## Waffles 'Vdatum conversion grid' module
## note: U.S. Only
## ==============================================
class WafflesVdatum(Waffle):
    """vertical datum transformation grid via NOAA's VDATUM."""
    
    def __init__(self, ivert='navd88', overt='mhw', region='3', jar=None, **kwargs):
        """generate a 'conversion-grid' with vdatum.
        
        output will be the differences (surfaced) between 
        `ivert` and `overt` for the region
        
        Args: 
          ivert (str): input vertical datum string
          overt (str): output vertical datum string
          region (str): vdatum grid region
          jar (path): path to vdatum .jar file
        
        Returns:
          list: [{'dem': ['dem-fn', 'raster']}, status]
        """

        super().__init__(**kwargs)
        
        #if self.gc['VDATUM'] is None:
        #    utils.echo_error_msg('VDatum must be installed to use the VDATUM module')
        #    return(None, -1)
        
        self.mod = 'vdatum'
        self.vdc = vdatumfun.Vdatum(ivert=ivert, overt=overt, region=region, jar=jar, verbose=True)

    def _create_null(self, outfile, extent, cellsize, nodata, outformat, verbose, overwrite):
        """create a nodata grid"""
        
        xcount, ycount, gt = extent.geo_transform(x_inc = cellsize)
        ds_config = demfun.set_infos(xcount, ycount, xcount * ycount, gt, utils.sr_wkt(4326),
                                     gdal.GDT_Float32, nodata, outformat)
        nullArray = np.zeros( (ycount, xcount) )
        nullArray[nullArray==0]=nodata
        utils.gdal_write(nullArray, outfile, ds_config)
        
    def run(self):

        self._create_null('empty.tif', self.p_region, 0.00083333, 0, 'GTiff', self.verbose, True)
        demfun.set_nodata('empty.tif')
        empty = datasets.RasterFile(fn='empty.tif', data_format=200, src_region=self.region, epsg=self.epsg,
                                    name=self.name, verbose=self.verbose)

        with open('empty.xyz', 'w') as mt_xyz:
            empty.dump_xyz(mt_xyz)
        self.vdc.run_vdatum('empty.xyz')

        if os.path.exists('result/empty.xyz') and os.stat('result/empty.xyz').st_size != 0:
            empty_xyz = datasets.XYZFile(fn='result/empty.xyz', data_format=168, src_region=self.region, epsg=self.epsg,
                                     name=self.name, verbose=self.verbose)
            empty_infos = empty_xyz.inf()
            
            ll = 'd' if empty_infos['minmax'][4] < 0 else '0'
            lu = 'd' if empty_infos['minmax'][5] > 0 else '0'

            GMTSurface(data=['result/empty.xyz'], name=self.name, src_region=self.region, inc=self.inc,
                       epsg=self.epsg, verbose=self.verbose, tension=0, upper_limit=lu, lower_limit=ll).generate()
        else:
            utils.echo_error_msg('failed to generate VDatum grid, check settings')
            vd_out = {}
            status = -1

        utils.remove_glob('empty.*', 'result/*', 'result')
        return(self)

## ==============================================
## Waffles GDAL_GRID module
## see gdal_grid for more info and gridding algorithms
## ==============================================
class WafflesGDALGrid(Waffle):
    
    def __init__(self, block=False, **kwargs):
        """run gdal grid using alg_str

        parse the data through xyzfun.xyz_block to get weighted mean before
        building the GDAL dataset to pass into gdal_grid
        
        Args: 
          alg_str (str): the gdal_grid algorithm string
        """
        
        super().__init__(**kwargs)
        self.block_p = block
        self.alg_str = 'linear:radius=-1'
        self.mod = self.alg_str.split(':')[0]
        
    def run(self):
        
        _prog = utils.CliProgress('running GDAL GRID {} algorithm @ {}...\
        '.format(self.alg_str.split(':')[0], self.p_region.format('fn')))
        _prog_update = lambda x, y, z: _prog.update()
        ds = self._xyz_ds(self.yield_xyz(block=self.block_p))
        
        if ds.GetLayer().GetFeatureCount() == 0:
            utils.echo_error_msg('no input data')
            
        xcount, ycount, dst_gt = self.p_region.geo_transform(x_inc=self.inc)
        
        gd_opts = gdal.GridOptions(outputType = gdal.GDT_Float32, noData = -9999, format = 'GTiff', width = xcount,
                                   height = ycount, algorithm = self.alg_str, callback = _prog_update if self.verbose else None,
                                   outputBounds = [self.p_region.xmin, self.p_region.ymax, self.p_region.xmax, self.p_region.ymin])
        
        gdal.Grid('{}.tif'.format(self.name), ds, options = gd_opts)
        demfun.set_nodata('{}.tif'.format(self.name, nodata=-9999, convert_array=False))
        _prog.end(0, 'ran GDAL GRID {} algorithm @ {}.'.format(self.alg_str.split(':')[0], self.p_region.format('fn')))
        ds = None
        return(self)

class WafflesLinear(WafflesGDALGrid):

    def __init__(self, radius=None, nodata=-9999, **kwargs):
        super().__init__(**kwargs)
        
        radius = self.inc * 4 if radius is None else utils.str2inc(radius)
        self.alg_str = 'linear:radius={}:nodata={}'.format(radius, nodata)

class WafflesInvDst(WafflesGDALGrid):

    def __init__(self, power = 2.0, smoothing = 0.0, radius1 = None, radius2 = None, angle = 0.0,
                   max_points = 0, min_points = 0, nodata = -9999, **kwargs):
        super().__init__(**kwargs)

        radius1 = self.inc * 2 if radius1 is None else utils.str2inc(radius1)
        radius2 = self.inc * 2 if radius2 is None else utils.str2inc(radius2)
        self.alg_str = 'invdist:power={}:smoothing={}:radius1={}:radius2={}:angle={}:max_points={}:min_points={}:nodata={}'\
            .format(power, smoothing, radius1, radius2, angle, max_points, min_points, nodata)

class WafflesMovingAverage(WafflesGDALGrid):

    def __init__(self, radius1=None, radius2=None, angle=0.0, min_points=0, nodata=-9999, **kwargs):
        super().__init__(**kwargs)

        radius1 = self.inc * 2 if radius1 is None else utils.str2inc(radius1)
        radius2 = self.inc * 2 if radius2 is None else utils.str2inc(radius2)
        self.alg_str = 'average:radius1={}:radius2={}:angle={}:min_points={}:nodata={}'\
            .format(radius1, radius2, angle, min_points, nodata)
        
class WafflesNearest(WafflesGDALGrid):

    def __init__(self, radius1=None, radius2=None, angle=0.0, nodata=-9999, **kwargs):
        super().__init__(**kwargs)

        radius1 = self.inc * 2 if radius1 is None else utils.str2inc(radius1)
        radius2 = self.inc * 2 if radius2 is None else utils.str2inc(radius2)
        self.alg_str = 'nearest:radius1={}:radius2={}:angle={}:nodata={}'\
            .format(radius1, radius2, angle, min_points, nodata)
            
class WaffleFactory:
    """Find and generate a WAFFLE object for DEM generation."""
    
    _modules = {
        'surface': {
            'name': 'surface',
            'datalist-p': True,
            'description': '''SPLINE DEM via GMT surface\n
Generate a DEM using GMT's surface command
        
< surface:tension=.35:relaxation=1.2:lower_limit=d:upper_limit=d >
 :tension=[0-1] - Spline tension.
 :relaxation=[val] - Spline relaxation factor.
 :lower_limit=[val] - Constrain interpolation to lower limit.
 :upper_limit=[val] - Constrain interpolation to upper limit.''',
        },
        'triangulate': {
            'name': 'triangulate',
            'datalist-p': True,
            'description': '''TRIANGULATION DEM via GMT triangulate\n
Generate a DEM using GMT's triangulate command.
        
< triangulate >''',
        },
        'num': {
            'name': 'num',
            'datalist-p': True,
            'description': """Uninterpolated DEM populated by <mode>.\n
Generate an uninterpolated DEM using <mode> option.
Using mode of 'A<mode>' uses GMT's xyz2grd command, 
see gmt xyz2grd --help for more info.

< num:mode=n >
 :mode=[key] - specify mode of grid population: 
  keys: k (mask), m (mean), n (num), w (wet), A<mode> (gmt xyz2grd)""",
        },
        'linear': {
            'name': 'linear',
            'datalist-p': True,
            'description': """LINEAR DEM via gdal_grid\n
Generate a DEM using GDAL's gdal_grid command.
see gdal_grid --help for more info

< linear:radius=-1 >
 :radius=[val] - search radius""",
        },
        'nearest': {
            'name': 'nearest',
            'datalist-p': True,
            'description': """NEAREST DEM via gdal_grid\n
Generate a DEM using GDAL's gdal_grid command.
see gdal_grid --help for more info

< nearest:radius1=0:radius2=0:angle=0:nodata=0 >
 :radius1=[val] - search radius
 :radius2=[val] - search radius""",
        },
        'average': {
            'name': 'average',
            'datalist-p': True,
            'description': """Moving AVERAGE DEM via gdal_grid\n
Generate a DEM using GDAL's gdal_grid command.
see gdal_grid --help for more info

< nearest:radius1=0:radius2=0:angle=0:min_points=0:nodata=0 >
 :radius1=[val] - search radius
 :radius2=[val] - search radius""",
        },
        'invdst': {
            'name': 'invdst',
            'datalist-p': True,
            'description': """INVERSE DISTANCE DEM via gdal_grid\n
Generate a DEM using GDAL's gdal_grid command.
see gdal_grid --help for more info

< invdst:power=2.0:smoothing=0.0:radius1=0:radius2=0:angle=0:max_points=0:min_points=0:nodata=0 >
 :radius1=[val] - search radius
 :radius2=[val] - search radius""",
        },
        'IDW': {
            'name': 'IDW',
            'datalist-p': True,
            'description': """INVERSE DISTANCE WEIGHTED DEM\n
            """,
        },
        'vdatum': {
            'name': 'vdatum',
            'datalist-p': False,
            'description': """VDATUM conversion grid.
            """
        },
        'mbgrid': {
            'name': 'mbgrid',
            'datalist-p': True,
            'description': """SPLINE grid via MB-System's mbgrid.
            """
        },
    }
    
    def __init__(self, mod=None, data=[], src_region=None, inc=None, name='waffles_dem',
                 node='pixel', fmt='GTiff', extend=0, extend_proc=20, weights=None,
                 fltr=[], sample = None, clip=None, chunk=None, epsg=4326,
                 verbose = False, archive=False, mask=False, spat=False, clobber=True):

        self.mod = mod
        self.mod_args = {}
        self.data = data
        self.region = src_region
        self.inc = inc
        self.name = name
        self.node = node
        self.fmt = fmt
        self.extend = extend
        self.extend_proc = extend_proc
        self.weights = weights
        self.fltr = fltr
        self.sample = sample
        self.clip = clip
        self.chunk = chunk
        self.epsg = utils.int_or(epsg)
        self.archive = archive
        self.mask = mask
        self.spat = spat
        self.clobber = clobber
        self.verbose = verbose

        if self.mod is not None:
            self.parse_mod()
            
    def parse_mod(self):
        opts = self.mod.split(':')
        if opts[0] in WaffleFactory()._modules.keys():
            if len(opts) > 1:
                self.mod_args = utils.args2dict(list(opts[1:]))
            self.mod = opts[0]
        else:
            utils.echo_error_msg('invalid module name `{}`'.format(opts[0]))
        return(self)
        
    def acquire_surface(self, **kwargs):
        return(GMTSurface(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_triangulate(self, **kwargs):
        return(GMTTriangulate(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_num(self, **kwargs):
        return(WafflesNum(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_linear(self, **kwargs):
        return(WafflesLinear(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_average(self, **kwargs):
        return(WafflesMovingAverage(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_invdst(self, **kwargs):
        return(WafflesInvDst(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))
    
    def acquire_nearest(self, **kwargs):
        return(WafflesNearest(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_IDW(self, **kwargs):
        return(WafflesIDW(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))
    
    def acquire_vdatum(self, **kwargs):
        return(WafflesVdatum(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire_mbgrid(self, **kwargs):
        return(WafflesMBGrid(
            data=self.data, src_region=self.region, inc=self.inc, name=self.name,
            node=self.node, fmt=self.fmt, extend=self.extend, extend_proc=self.extend_proc,
            weights=self.weights, fltr=self.fltr, sample=self.sample, clip=self.clip,
            chunk=self.chunk, epsg=self.epsg, archive=self.archive, mask=self.mask, spat=self.spat,
            clobber=self.clobber, verbose=self.verbose, **kwargs, **self.mod_args))

    def acquire(self, **kwargs):
        if self.mod == 'surface':
            return(self.acquire_surface(**kwargs))
        if self.mod == 'triangulate':
            return(self.acquire_triangulate(**kwargs))
        if self.mod == 'num':
            return(self.acquire_num(**kwargs))
        if self.mod == 'linear':
            return(self.acquire_linear(**kwargs))
        if self.mod == 'average':
            return(self.acquire_average(**kwargs))
        if self.mod == 'nearest':
            return(self.acquire_nearest(**kwargs))
        if self.mod == 'invdst':
            return(self.acquire_invdst(**kwargs))
        if self.mod == 'IDW':
            return(self.acquire_IDW(**kwargs))
        if self.mod == 'vdatum':
            return(self.acquire_vdatum(**kwargs))
        if self.mod == 'mbgrid':
            return(self.acquire_mbgrid(**kwargs))
    
    # def acquire_module_by_name(self, mod_name, **kwargs):
    #     if mod_name == 'surface':
    #         return(self.acquire_surface(**kwargs))
    #     if mod_name == 'triangulate':
    #         return(self.acquire_triangulate(**kwargs))
    #     if mod_name == 'num':
    #         return(self.acquire_num(**kwargs))
    #     if mod_name == 'linear':
    #         return(self.acquire_linear(**kwargs))
    #     if mod_name == 'average':
    #         return(self.acquire_average(**kwargs))
    #     if mod_name == 'nearest':
    #         return(self.acquire_nearest(**kwargs))
    #     if mod_name == 'invdst':
    #         return(self.acquire_invdst(**kwargs))
    #     if mod_name == 'IDW':
    #         return(self.acquire_IDW(**kwargs))
    #     if mod_name == 'vdatum':
    #         return(self.acquire_vdatum(**kwargs))

## ==============================================
## Command-line Interface (CLI)
## $ waffles
##
## waffles cli
## ==============================================
_waffles_module_short_desc = lambda: ', '.join(
    ['{}'.format(key) for key in WaffleFactory()._modules])
_waffles_module_long_desc = lambda x: 'waffles modules:\n% waffles ... -M <mod>:key=val:key=val...\n\n  ' + '\n  '.join(
    ['\033[1m{:14}\033[0m{}\n'.format(key, x[key]['description']) for key in x]) + '\n'

waffles_cli_usage = """{cmd} ({wf_version}): Generate DEMs and derivatives.

usage: {cmd} [OPTIONS] DATALIST

Options:
  -R, --region\t\tSpecifies the desired REGION;
\t\t\tThis can either be a GMT-style region ( -R xmin/xmax/ymin/ymax )
\t\t\tor an OGR-compatible vector file with regional polygons. 
\t\t\tIf a vector file is supplied it will search each region found therein.
\t\t\tIf omitted, use the region gathered from the data in DATALIST.
  -E, --increment\tGridding CELL-SIZE in native units or GMT-style increments.
\t\t\tappend :<inc> to resample the output to the given <inc>: -E.3333333s:.1111111s
  -F, --format\t\tOutput grid FORMAT. [GTiff]
  -M, --module\t\tDesired Waffles MODULE and options. (see available Modules below)
\t\t\tsyntax is -M module:mod_opt=mod_val:mod_opt1=mod_val1:...
  -O, --output-name\tBASENAME for all outputs.
  -P, --epsg\t\tHorizontal projection of data as EPSG code [4326]
  -X, --extend\t\tNumber of cells with which to EXTEND the REGION.
\t\t\tappend :<num> to extend the processing region: -X6:12
  -T, --filter\t\tFILTER the output DEM using one or multiple filters. <fltr:fltr_val:split_value=z>
\t\t\tAvailable filters:
\t\t\t1: perform a Gaussian filter at -T1:<factor>.
\t\t\t2: use a Cosine Arch Filter at -T2:<dist(km)> search distance.
\t\t\t3: Spike Filter at -T3:<stand-dev. threshhold>.
\t\t\tThe -T switch may be set multiple times to perform multiple filters.
\t\t\tAppend :split_value=<num> to only filter values below z-value <num>.
\t\t\te.g. -T1:10:split_value=0 to smooth bathymetry (z<0) using Gaussian filter
  -C, --clip\t\tCLIP the output to the clip polygon -C<clip_ply.shp:invert=False>
  -G, --wg-config\tA waffles config JSON file. If supplied, will overwrite all other options.
\t\t\tgenerate a waffles_config JSON file using the --config flag.

  -p, --prefix\t\tSet BASENAME (-O) to PREFIX (append inc/region/year info to output BASENAME).
  -r, --grid-node\tUse grid-node registration, default is pixel-node
  -w, --weights\t\tUse weights provided in the datalist to weight overlapping data.

  -a, --archive\t\tARCHIVE the datalist to the given region.
  -m, --mask\t\tGenerate a data MASK raster.
  -s, --spat\t\tGenerate SPATIAL-METADATA.
  -c, --continue\tDon't clobber existing files.
  -q, --quiet\t\tLower verbosity to a quiet.

  --help\t\tPrint the usage text
  --config\t\tSave the waffles config JSON and major datalist
  --modules\t\tDisply the module descriptions and usage
  --version\t\tPrint the version information

Datalists and data formats:
  A datalist is a file that contains a number of datalist entries, 
  while an entry is a space-delineated line:
  `/path/to/data format weight data,meta,data`

Supported datalist formats: 
  {dl_formats}

Modules (see waffles --modules <module-name> for more info):
  {modules}

CIRES DEM home page: <http://ciresgroups.colorado.edu/coastalDEM>
""".format(cmd=os.path.basename(sys.argv[0]),
           dl_formats=dlim._datalist_fmts_short_desc(),
           modules=_waffles_module_short_desc(),
           wf_version=cudem.__version__)

def waffles_cli(argv = sys.argv):
    """run waffles from command-line

    See `waffles_cli_usage` for full cli options.
    """
    
    dls = []
    i_regions = []
    these_regions = []
    module = None
    wg_user = None
    want_prefix = False
    status = 0
    i = 1
    wg = {}
    wg['verbose'] = True
    wg['sample'] = None
    wg['fltr'] = []
    
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
            wg['inc'] = utils.str2inc(incs[0])
            if len(incs) > 1: wg['sample'] = utils.str2inc(incs[1])
            i = i + 1
        elif arg[:2] == '-E':
            incs = arg[2:].split(':')
            wg['inc'] = utils.str2inc(arg[2:].split(':')[0])
            if len(incs) > 1: wg['sample'] = utils.str2inc(incs[1])
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
        elif arg == '--epsg' or arg == '-P':
            wg['epsg'] = utils.int_or(argv[i + 1], 4326)
            i = i + 1
        elif arg[:2] == '-P': wg['epsg'] = utils.int_or(arg[2:], 4326)
        
        elif arg == '-w' or arg == '--weights': wg['weights'] = 1
        elif arg == '-t' or arg == '--threads': want_threads = True
        elif arg == '-p' or arg == '--prefix': want_prefix = True
        elif arg == '-a' or arg == '--archive': wg['archive'] = True
        elif arg == '-m' or arg == '--mask': wg['mask'] = True
        elif arg == '-s' or arg == '--spat': wg['spat'] = True
        elif arg == '-c' or arg == '--continue': wg['clobber'] = False
        elif arg == '-r' or arg == '--grid-node': wg['node'] = 'grid'

        elif arg == '--quiet' or arg == '-q': wg['verbose'] = False
        elif arg == '--config': want_config = True
        elif arg == '--modules' or arg == '-m':
            try:
                if argv[i + 1] in WaffleFactory()._modules.keys():
                    sys.stderr.write(_waffles_module_long_desc({k: WaffleFactory()._modules[k] for k in (argv[i + 1],)}))
                else: sys.stderr.write(_waffles_module_long_desc(WaffleFactory()._modules))
            except: sys.stderr.write(_waffles_module_long_desc(WaffleFactory()._modules))
            sys.exit(0)
        elif arg == '--help' or arg == '-h':
            sys.stderr.write(waffles_cli_usage)
            sys.exit(0)
        elif arg == '--version' or arg == '-v':
            sys.stdout.write('{}\n'.format(cudem.__version__))
            sys.exit(0)
        elif arg[0] == '-':
            print(waffles_cli_usage)
            utils.echo_error_msg('{} is not a valid waffles cli switch'.format(arg))
            sys.exit(0)
        else: dls.append(arg)
        i += 1

    if module is None:
        sys.stderr.write(waffles_cli_usage)
        utils.echo_error_msg('''must specify a waffles -M module.''')
        sys.exit(-1)
                
    # if WaffleFactory()._modules[mod]['datalist-p']:
    #     if len(dls) == 0:
    #         sys.stderr.write(waffles_cli_usage)
    #         utils.echo_error_msg('''must specify a datalist/entry, try gmrt or srtm for global data.''')
    #         sys.exit(-1)

    ## ==============================================
    ## check the increment
    ## ==============================================
    if wg['inc'] is None:
        sys.stderr.write(waffles_cli_usage)
        utils.echo_error_msg('''must specify a gridding increment.''')
        sys.exit(-1)
    
    ## ==============================================
    ## set the datalists and names
    ## ==============================================
    wg['data'] = dls

    for i_region in i_regions:
        tmp_region = regions.Region().from_string(i_region)
        if tmp_region.valid_p():
            these_regions.append(tmp_region)
        else:
            tmp_region = regions.ogr_wkts(i_region)
            for i in tmp_region:
                if i.valid_p():
                    these_regions.append(i)
                    
    if len(these_regions) == 0:
        these_regions = []
        utils.echo_error_msg('failed to parse region(s), {}'.format(i_regions))
    else:
        if wg['verbose']: utils.echo_msg('parsed {} region(s)'.format(len(these_regions)))
    
    for i, this_region in enumerate(these_regions):
        wg['src_region'] = this_region
        if want_prefix or len(these_regions) > 1:
            wg['name'] = utils.append_fn(wg['name'], wg['src_region'], wg['sample'] if wg['sample'] is not None else wg['inc'])

        #wf = WaffleFactory(**wg).acquire_module_by_name(mod, **mod_args).generate()
        wf = WaffleFactory(mod=module, **wg).acquire().generate()
        utils.echo_msg(wf)
        #utils.echo_msg(wf['dem'].mod)

### End
