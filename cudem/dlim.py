### dlim.py - DataLists IMproved
##
## Copyright (c) 2010 - 2023 Regents of the University of Colorado
##
## dlim.py is part of CUDEM
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
## A datalist is similar to an MBSystem datalist; it is a space-delineated file containing the following columns:
## data-path data-format data-weight data-name data-source data-date data-resolution data-type data-horz data-vert data-url
## Minimally, data-path is all that is needed.
##
## an associated inf and geojson file will be gerenated for each datalist
## only an associated inf file will be genereated for individual datasets
##
## Parse various dataset types by region/increments and yield data as xyz or array
## recursive data-structures which point to datasets (datalist, zip, fetches, etc) are negative format numbers, e.g. -1 for datalist
##
## supported datasets include: xyz, gdal, ogr, las/laz (laspy), mbs (MBSystem), fetches
##
## Initialize a datalist/dataset using init_data(list-of-datalist-entries) where the list of datalist entries can
## be any of the supported dataset formats. init_data will combine all the input datafiles into an internal
## datalist and process that.
##
## If region, x_inc, and y_inc are set, all processing will go through Dataset._stacks() where the data will be combined
## either using the 'supercede' or 'weighted-mean' method. _stacks will output a multi-banded gdal file with the following
## bands: 1: z, 2: count, 3: weight, 4: uncerainty, 5: source-uncertainty
##
## If want_mask is set, data will be passed through Dataset._mask() which will generate a multi-band gdal file where each
## mask band contains the mask of a specific dataset/datalist, depending on the input data. For a datalist, each band will
## contain a mask for each of the top-level datasets in the main datalist file. If want_sm is also set, the multi-band mask
## will be processed to an OGR supported vector, with a feature for each band and the metadata items as fields.
##
## Transform data between horizontal/vertical projections/datums by setting src_srs and dst_srs as 'EPSG:<>'
## if src_srs is not set, but dst_srs is, dlim will attempt to obtain the source srs from the data file itself
## or its respective inf file.
##
### Examples:
##
## my_data_list = ['nos_data.xy', 'my_grid.tif', 'gmrt', 'other_data.datalist'] 
## my_processed_datalist = init_data(my_data_list) # initialize the data
## my_processed_datalist.dump_xyz() # dump all the xyz data
##
## from cudem import regions
## my_region = regions.Region().from_list([-160, -150, 30, 40])
## my_data_list = ['nos_data.xy', 'my_grid.tif', 'gmrt', 'other_data.datalist'] 
## my_processed_datalist = init_data(my_data_list, src_region = my_region, x_inc = 1s, y_inc = 1s)
## my_stack = my_processed_datalist._stacks() # stack the data to the region/increments
## my_processed_datalist.archive_xyz() # archive the data to the region/increments
## my_mask = my_processed_datalist._mask() # mask the data to the region/increments
##
### TODO:
## allow input http data (output of fetches -l should be able to be used as a datalist)
## multiple -R in cli
## mask to stacks for supercede
## inf class
## fetch results class
## speed up xyz parsing, esp in yield_array
## do something about inf files (esp w/ fetches data)
## temp files
### Code:

import os
import sys
import re
import copy
import json
import laspy as lp
import math
from datetime import datetime

import numpy as np
from scipy.spatial import ConvexHull
import lxml.etree

from osgeo import gdal
from osgeo import ogr
from osgeo import osr

import cudem
from cudem import utils
from cudem import regions
from cudem import xyzfun
from cudem import gdalfun
from cudem import factory
from cudem import vdatums
from cudem import fetches
from cudem import FRED

## ==============================================
## Datalist convenience functions
## data_list is a list of dlim supported datasets
## ==============================================
def make_datalist(data_list, want_weight, want_uncertainty, region,
                  src_srs, dst_srs, x_inc, y_inc, sample_alg, verbose):
    """Make a datalist object from a list of supported datasets"""

    ## Make the datalist object
    xdl = Datalist(fn='scratch', data_format=-1, weight=None if not want_weight else 1,
                   uncertainty=None if not want_uncertainty else 0, src_region=region,
                   verbose=verbose, parent=None, src_srs=src_srs, dst_srs=dst_srs,
                   x_inc=x_inc, y_inc=y_inc, sample_alg=sample_alg)

    ## add the datasets from data_list to the datalist object
    xdl.data_entries = [DatasetFactory(fn=" ".join(['-' if x == "" else x for x in dl.split(",")]),
                                       weight=None if not want_weight else 1, uncertainty=None if not want_uncertainty else 0,
                                       src_region=region, verbose=verbose, src_srs=src_srs, dst_srs=dst_srs,
                                       x_inc=x_inc, y_inc=y_inc, sample_alg=sample_alg, parent=xdl,
                                       cache_dir=self.cache_dir)._acquire_module() for dl in data_list]
    return(xdl)

def write_datalist(data_list, outname=None):
    """write a datalist file from a datalist object"""
    
    if outname is None:
        outname = '{}_{}'.format(self.metadata['name'], utils.this_year())
    
    if os.path.exists('{}.datalist'.format(outname)):
        utils.remove_glob('{}.datalist*'.format(outname))
        
    with open('{}.datalist'.format(outname), 'w') as tmp_dl:
        [tmp_dl.write('{}\n'.format(x.format_entry())) for x in data_list]

    return('{}.datalist'.format(outname))

def init_data(data_list, region=None, src_srs=None, dst_srs=None, xy_inc=(None, None), sample_alg='bilinear',
              want_weight=False, want_uncertainty=False, want_verbose=True, want_mask=False, want_sm=False,
              invert_region=False, cache_dir=None):
    """initialize a datalist object from a list of supported dataset entries"""

    try:
        xdls = [DatasetFactory(mod=" ".join(['-' if x == "" else x for x in dl.split(",")]), data_format = None,
                               weight=None if not want_weight else 1, uncertainty=None if not want_uncertainty else 0,
                               src_srs=src_srs, dst_srs=dst_srs, x_inc=xy_inc[0], y_inc=xy_inc[1], sample_alg=sample_alg,
                               parent=None, src_region=region, invert_region=invert_region, cache_dir=cache_dir,
                               want_mask=want_mask, want_sm=want_sm, verbose=want_verbose)._acquire_module() for dl in data_list]

        if len(xdls) > 1:
            this_datalist = DataList(fn=xdls, data_format=-3, weight=None if not want_weight else 1,
                                     uncertainty=None if not want_uncertainty else 0, src_srs=src_srs, dst_srs=dst_srs,
                                     x_inc=xy_inc[0], y_inc=xy_inc[1], sample_alg=sample_alg, parent=None, src_region=region,
                                     invert_region=invert_region, cache_dir=cache_dir, want_mask=want_mask, want_sm=want_sm,
                                     verbose=want_verbose)
        else:
            this_datalist = xdls[0]

        return(this_datalist)
    
    except Exception as e:
        utils.echo_error_msg('could not initialize data, {}'.format(e))
        return(None)
 
## ==============================================
## Elevation Dataset class
## ==============================================
class ElevationDataset:
    """representing an Elevation Dataset
    
    This is the super class for all datalist (dlim) datasets .    
    Each dataset sub-class should define a dataset-specific    
    data parser and a generate_inf function to generate
    inf files. 

    Specifically, each sub-dataset should minimally define the following functions:
    
    sub_ds.generate_inf
    sub_ds.yield_xyz
    sub_ds.yield_array
    
    Where:
    generate_inf generates a dlim compatible inf file,
    yield_xyz yields the xyz elevation data (xyzfun.XYZPoint) from the dataset.
    yield_array yields the data as an array, the data array-set srcwin, and the gdal
    geotransform for the srcwin in a tuple: ([data_array-set], data_srcwin, data_gt)

    ----
    Parameters:

    fn: dataset filename or fetches module
    data_format: dataset format
    weight: dataset weight
    uncertainty: dataset uncertainty
    src_srs: dataset source srs
    dst_srs: dataset target srs
    x_inc: target dataset x/lat increment
    y_inc: target dataset y/lon increment
    want_mask: mask the data
    want_sm: generate spatial metadata vector
    sample_alg: the gdal resample algorithm
    metadata: dataset metadata
    parent: dataset parent obj
    region: ROI
    invert_region: invert the region
    cache_dir: cache_directory
    verbose: be verbose
    remote: dataset is remote
    params: the factory parameters
    """

    gdal_sample_methods = [
        'near', 'bilinear', 'cubic', 'cubicspline', 'lanczos',
        'average', 'mode',  'max', 'min', 'med', 'Q1', 'Q3', 'sum',
        'auto'
    ]

    ## todo: add transformation grid option (stacks += transformation_grid), geoids
    def __init__(self, fn = None, data_format = None, weight = 1, uncertainty = 0, src_srs = None,
                 dst_srs = 'epsg:4326', x_inc = None, y_inc = None, want_mask = False, want_sm = False,
                 sample_alg = 'bilinear', parent = None, src_region = None, invert_region = False,
                 cache_dir = None, verbose = False, remote = False, params = {},
                 metadata = {'name':None, 'title':None, 'source':None, 'date':None,
                             'data_type':None, 'resolution':None, 'hdatum':None,
                             'vdatum':None, 'url':None}):
        self.fn = fn # dataset filename or fetches module
        self.data_format = data_format # dataset format
        self.weight = weight # dataset weight
        self.uncertainty = uncertainty # dataset uncertainty
        self.src_srs = src_srs # dataset source srs
        self.dst_srs = dst_srs # dataset target srs
        self.x_inc = utils.str2inc(x_inc) # target dataset x/lat increment
        self.y_inc = utils.str2inc(y_inc) # target dataset y/lon increment
        self.want_mask = want_mask # mask the data
        self.want_sm = want_sm # generate spatial metadata vector
        self.sample_alg = sample_alg # the gdal resample algorithm
        self.metadata = copy.deepcopy(metadata) # dataset metadata
        self.parent = parent # dataset parent obj
        self.region = src_region # ROI
        self.invert_region = invert_region # invert the region
        self.cache_dir = cache_dir # cache_directory
        self.verbose = verbose # be verbose
        self.remote = remote # dataset is remote
        self.params = params # the factory parameters
        if not self.params:
            self.params['kwargs'] = self.__dict__.copy()
            self.params['mod'] = self.fn
            self.params['mod_name'] = self.data_format
            self.params['mod_args'] = {}
            
        #factory._set_params(self, mod=self.fn, mod_name=self.data_format) # set up the default factory parameters
        
    def __str__(self):
        return('<Dataset: {} - {}>'.format(self.metadata['name'], self.fn))
    
    def __repr__(self):
        return('<Dataset: {} - {}>'.format(self.metadata['name'], self.fn))

    def __call__(self):
        self.initialize()
        
    def initialize(self):
        #factory._set_mod_params(self) # set the module arguments, if any
        self._fn = None 
        self.dst_trans = None # srs transformation obj
        self.trans_region = None # transformed region
        self.src_trans_srs = None # source srs obj
        self.dst_trans_srs = None # target srs obj        
        self.archive_datalist = None # the datalist of the archived data
        self.data_entries = [] # 
        self.data_lists = {} #
        self.cache_dir = utils.cudem_cache() if self.cache_dir is None else self.cache_dir # cache directory

        if self.sample_alg not in self.gdal_sample_methods: 
            utils.echo_warning_msg(
                '{} is not a valid gdal warp resample algorithm, falling back to bilinear'.format(
                    self.sample_alg
                )
            )
            self.sample_alg = 'bilinear'
            
        if utils.fn_url_p(self.fn):
            self.remote = True
                        
        if self.valid_p():
            self.inf(check_hash=True if self.data_format == -1 else False)
            self.set_yield()
            self.set_transform()

        return(self)
    
    def generate_inf(self, callback=lambda: False):
        """set in dataset"""
        
        raise(NotImplementedError)
    
    def yield_xyz(self):
        """set in dataset"""
        
        raise(NotImplementedError)

    def yield_array(self):
        """set in dataset"""
        
        raise(NotImplementedError)
    
    def yield_xyz_from_entries(self):
        """yield from self.data_entries, list of datasets"""
        
        for this_entry in self.data_entries:
            for xyz in this_entry.xyz_yield:
                yield(xyz)
                
            if this_entry.remote:
                utils.remove_glob('{}*'.format(this_entry.fn))

    def yield_entries(self):
        """yield from self.data_entries, list of datasets"""
        
        for this_entry in self.data_entries:
            yield(this_entry)

    def set_yield(self):
        """set the yield strategy, either default (all points) or mask or stacks

        This sets both `self.array_yeild` and `self.xyz_yield`.
        
        if region, x_inc and y_inc are set, data will yield through _stacks. if self.mask is
        also set, will yield through _mask before yielding through _stacks.
        otherwise, data will yield directly from `self.yield_xyz` and `self.yield_array`
        """

        #self.array_yield = self.yield_array()
        self.xyz_yield = self.yield_xyz()
        #if self.want_archive: # archive only works when yielding xyz data.
        #    self.xyz_yield = self.archive_xyz()
        if self.region is not None and self.x_inc is not None:
            self.x_inc = utils.str2inc(self.x_inc)
            if self.y_inc is None:
                self.y_inc = self.x_inc
            else:
                self.y_inc = utils.str2inc(self.y_inc)

            out_name = os.path.join(self.cache_dir, '{}_{}'.format(
                utils.fn_basename2(os.path.basename(utils.str_or(self.fn, '_dlim_list'))),
                utils.append_fn('dlim_stacks', self.region, self.x_inc)))
                
            # if self.want_mask: # masking only makes sense with region/x_inc/y_inc...perhaps do a hull otherwise.
            #     self.array_yield = self._mask(out_name='{}_m'.format(out_name), fmt='GTiff')

            self.xyz_yield = self.stacks_yield_xyz(out_name=out_name, fmt='GTiff', want_mask=self.want_mask)

    def dump_xyz(self, dst_port=sys.stdout, encode=False, **kwargs):
        """dump the XYZ data from the dataset.

        data gets parsed through `self.xyz_yield`. See `set_yield` for more info.
        """
        
        for this_xyz in self.xyz_yield:
            this_xyz.dump(
                include_w=True if self.weight is not None else False,
                include_u=True if self.uncertainty is not None else False,
                dst_port=dst_port,
                encode=encode
            )

    def dump_xyz_direct(self, dst_port=sys.stdout, encode=False, **kwargs):
        """dump the XYZ data from the dataset

        data get dumped directly from `self.yield_xyz`.
        """
        
        for this_xyz in self.yield_xyz():
            this_xyz.dump(
                include_w=True if self.weight is not None else False,
                include_u=True if self.uncertainty is not None else False,
                dst_port=dst_port,
                encode=encode
            )

    def export_xyz_as_list(self, z_only = False, **kwargs):
        """return the XYZ data from the dataset as python list"""
        
        xyz_l = []
        for this_xyz in self.xyz_yield:
            if z_only:
                xyz_l.append(this_xyz.z)
            else:
                xyz_l.append(this_xyz.copy())
            
        return(xyz_l)
            
    def fetch(self):
        """fetch remote data from self.data_entries"""
        
        for entry in self.data_entries:
            if entry.remote:
                if entry._fn is None:
                    entry._fn = os.path.basename(self.fn)
                    
                f = utils.Fetch(
                    url=entry.fn, verbose=entry.verbose
                )
                if f.fetch_file(entry._fn) == 0:
                    entry.fn = entry._fn
            else:
                utils.echo_warning_msg('nothing to fetch')

    def valid_p(self, fmts=['scratch']):
        """check if self appears to be a valid dataset entry"""

        if self.fn is None:
            return(False)
        
        if self.data_format is None:
            return(False)
        
        if self.fn is not None:
            if self.fn not in fmts:
                if not isinstance(self.fn, list):
                    if not utils.fn_url_p(self.fn):
                        if self.data_format > -10:
                            if not os.path.exists(self.fn):
                                return (False)

                            if os.stat(self.fn).st_size == 0:
                                return(False)
                        
        return(True)
        
    def hash(self, sha1=False):
        """generate a hash of the xyz-dataset source file"""

        import hashlib
        BUF_SIZE = 65536
        if sha1:
            this_hash = hashlib.sha1()
        else:
            this_hash = hashlib.md5()
            
        try:
            with open(self.fn, 'rb') as f:
                while True:
                    data = f.read(BUF_SIZE)
                    if not data:
                        break
                    
                    this_hash.update(data)
            return(this_hash.hexdigest())
        except: return('0')

    def format_entry(self, sep=' '):
        dl_entry = sep.join([str(x) for x in [self.fn, '{}:{}'.format(self.data_format, factory.dict2args(self.params['mod_args'])), self.weight, self.uncertainty]])
        metadata = self.echo_()
        return(sep.join([dl_entry, metadata]))
        
    def echo_(self, sep=' ', **kwargs):
        """print self as a datalist entry string"""

        out = []
        for key in self.metadata.keys():
            if key != 'name':
                out.append(str(self.metadata[key]))

        #return(sep.join(['"{}"'.format(str(self.metadata[x])) for x in self.metadata.keys()]))
        return(sep.join(['"{}"'.format(str(x)) for x in out]))
    
    def echo(self, **kwargs):
        """print self.data_entries as a datalist entries."""

        for entry in self.parse_json():
            entry_path = os.path.abspath(entry.fn) if not self.remote else entry.fn
            l = [entry_path, entry.data_format]
            if entry.weight is not None:
                l.append(entry.weight)
                
            print('{}'.format(" ".join([str(x) for x in l])))

    def format_metadata(self, **kwargs):
        """format metadata from self, for use as a datalist entry."""

        return(self.echo_())
    
    def inf(self, check_hash=False, recursive_check=False, write_inf=True, **kwargs):
        """read/write an inf file

        If the inf file is not found, will attempt to generate one.
        The function `generate_inf` should be defined for each specific
        dataset sub-class.
        """

        inf_path = '{}.inf'.format(self.fn)
        mb_inf = False
        self.infos = {}

        ## try to parse the existing inf file as either a native inf json
        ## or as an MB-System inf file.
        if os.path.exists(inf_path):
            try:
                with open(inf_path) as i_ob:
                    self.infos = json.load(i_ob)
            except ValueError:
                try:
                    self.infos = MBSParser(
                        fn=self.fn, src_srs=self.src_srs).inf_parse().infos
                    self.check_hash = False
                    mb_inf = True
                except:
                    if self.verbose:
                        utils.echo_error_msg(
                            'failed to parse inf {}'.format(inf_path)
                        )
            except:
                if self.verbose:
                    utils.echo_error_msg(
                        'failed to parse inf {}'.format(inf_path)
                    )

        ## check has from inf file vs generated hash,
        ## if hashes are different, then generate a new
        ## inf file...only do this if check_hash is set
        ## to True, as this can be time consuming and not
        ## always necessary...
        if check_hash:
            if 'hash' in self.infos.keys():
                gen_inf = self.hash() != self.infos['hash']
            else:
                gen_inf = True
                
        elif not mb_inf:
            gen_inf = 'hash' not in self.infos.keys() or 'wkt' not in self.infos.keys()
        else:
            gen_inf = False

        ## generate a new inf file if it was deemed necessary.
        if gen_inf: # and not self.remote:
            #self.infos = self.generate_inf(None if not self.verbose else _prog.update)
            self.infos = self.generate_inf()
            if self.infos is not None:
                if 'minmax' in self.infos:
                    if self.infos['minmax'] is not None:
                        if write_inf:
                            try:
                                with open('{}.inf'.format(self.fn), 'w') as inf:
                                    inf.write(json.dumps(self.infos))
                            except:
                                pass

                if recursive_check and self.parent is not None:
                    self.parent.inf(check_hash=True)

            else:
                sys.exit(-1)    

        ## set the srs information
        if 'src_srs' not in self.infos.keys() or self.infos['src_srs'] is None:
            self.infos['src_srs'] = self.src_srs
        else:
            if self.src_srs is None:
                self.src_srs = self.infos['src_srs']
                
            if self.dst_trans is not None:
                if self.trans_region is None:
                    self.trans_region = regions.Region().from_list(self.infos['minmax'])
                    self.trans_region.src_srs = self.infos['src_srs']
                    self.trans_region.warp(self.dst_srs)
                    
        if 'format' not in self.infos.keys():
            self.infos['format'] = self.data_format

        return(self.infos)

    ## todo: clean up this function, it's messy and buggy
    def set_transform(self):
        """Set the transformation parameters for the dataset.

        this will set the osr transformation to be used in
        transforming all the data in this dataset...including vertical
        transformations.
        """
        
        if self.src_srs == '': self.src_srs = None
        if self.dst_srs == '': self.dst_srs = None
        if self.dst_srs is not None \
           and self.src_srs is not None \
           and self.src_srs != self.dst_srs:
            #print(self.src_srs)
            #tmp_srs = osr.SpatialReference()
            #tmp_srs.SetFromUserInput(self.src_srs)
            #print(gdalfun.osr_parse_srs(tmp_srs))
            ## parse out the horizontal and vertical epsgs if they exist
            src_horz, src_vert = gdalfun.epsg_from_input(self.src_srs)
            dst_horz, dst_vert = gdalfun.epsg_from_input(self.dst_srs)
            #print(src_horz_epsg, src_vert_epsg)
            ## set the horizontal OSR srs objects
            src_srs = osr.SpatialReference()
            src_srs.SetFromUserInput(src_horz)
            dst_srs = osr.SpatialReference()
            dst_srs.SetFromUserInput(dst_horz)
            ## generate the vertical transformation grids if called for
            ## check if transformation grid already exists, so we don't
            ## have to create a new one for every input file...!
            if dst_vert is not None \
               and src_vert is not None \
               and dst_vert != src_vert:
                vd_region = regions.Region(
                    src_srs=src_srs.ExportToProj4()
                ).from_list(
                    self.infos['minmax']
                ).warp(
                    dst_srs.ExportToProj4()
                ) if self.region is None else self.region.copy()

                ## use cudem.vdatums instead!
                vd_region.buffer(pct=2)
                trans_fn = os.path.join(self.cache_dir, '_vdatum_trans_{}_{}_{}'.format(
                    src_vert,
                    dst_vert,
                    vd_region.format('fn')
                ))

                #if not os.path.exists('{}.tif'.format(trans_fn)):
                trans_fn = vdatums.VerticalTransform(
                    vd_region, '3s', '3s', src_vert, dst_vert,
                    cache_dir=self.cache_dir
                ).run(outfile='{}.tif'.format(trans_fn))
                utils.echo_msg('generated {}'.format(trans_fn))
                
                # waffles_cmd = 'waffles -R {} -E 3s -M vdatum:vdatum_in={}:vdatum_out={} -O {} -c -k -D {}'.format(
                #     vd_region.format('str'),
                #     src_vert,
                #     dst_vert,
                #     trans_fn,
                #     self.cache_dir
                # )
                #if utils.run_cmd(waffles_cmd, verbose=True)[1] == 0:
                if os.path.exists(trans_fn):
                    out_src_srs = '{} +geoidgrids={}'.format(src_srs.ExportToProj4(), trans_fn)
                    out_dst_srs = '{}'.format(dst_srs.ExportToProj4())

                    if src_vert == '6360':
                        out_src_srs = out_src_srs + ' +vto_meter=0.3048006096012192'

                    if dst_vert == '6360':
                        out_dst_srs = out_dst_srs + ' +vto_meter=0.3048006096012192'
                else:
                    utils.echo_error_msg(
                        'failed to generate vertical transformation grid between {} and {} for this region!'.format(
                            src_vert, dst_vert
                        )
                    )
                    out_src_srs = src_srs.ExportToProj4()
                    out_dst_srs = dst_srs.ExportToProj4()
                
            else:
                out_src_srs = src_srs.ExportToProj4()
                out_dst_srs = dst_srs.ExportToProj4()

            ## setup final OSR transformation objects
            src_osr_srs = osr.SpatialReference()
            src_osr_srs.SetFromUserInput(out_src_srs)
            dst_osr_srs = osr.SpatialReference()
            dst_osr_srs.SetFromUserInput(out_dst_srs)
            try:
                src_osr_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                dst_osr_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except:
                pass

            ## transform input region if necessary
            self.src_trans_srs = out_src_srs
            self.dst_trans_srs = out_dst_srs
            self.dst_trans = osr.CoordinateTransformation(src_osr_srs, dst_osr_srs)
            #print(self.src_srs, self.dst_srs)
            #print(out_src_srs, out_dst_srs)
            if self.region is not None: # and self.region.src_srs != self.src_srs:
                self.trans_region = self.region.copy()
                self.trans_region.src_srs = out_dst_srs
                self.trans_region.warp(out_src_srs)
                self.trans_region.src_srs = out_src_srs
            else:
                self.trans_region = regions.Region().from_string(self.infos['wkt'])
                self.trans_region.src_srs = self.src_srs
                self.trans_region.warp(self.dst_srs)
            src_osr_srs = dst_osr_srs = None
            
    def parse_json(self):
        """
        parse the datasets from the datalist geojson file.
        The geojson vector is generated when `self.parse` is run
        for the first time on a datalist.
        
        Re-define this method when defining a dataset sub-class
        that represents recursive data structures. Otherwise, will
        just fall back to `parse`

        -------
        Yields:

        dataset object
        """
        
        for ds in self.parse():
            yield(ds)
    
    def parse(self):
        """parse the datasets from the dataset.
        
        Re-define this method when defining a dataset sub-class
        that represents recursive data structures (datalists, zip, etc).

        This will fill self.data_entries

        -------
        Yields:

        dataset object
        """
        
        if self.region is not None:
            try:
                inf_region = regions.Region().from_string(self.infos['wkt'])
            except:
                try:
                    inf_region = regions.Region().from_list(self.infos['minmax'])
                except:
                    inf_region = self.region.copy()
                
            if regions.regions_intersect_p(
                    inf_region,
                    self.region if self.dst_trans is None else self.trans_region
            ):
                self.data_entries.append(self)
                yield(self)
        else:
            self.data_entries.append(self)
            yield(self)

    def parse_data_lists(self, gather_data=True):
        """parse the data into a datalist dictionary"""
        
        for e in self.parse():
            if e.parent is not None:
                if e.parent.metadata['name'] in self.data_lists.keys():
                    self.data_lists[e.parent.metadata['name']]['data'].append(e)
                else:
                    self.data_lists[e.parent.metadata['name']] = {'data': [e], 'parent': e.parent}
                    
            else:
                self.data_lists[e.metadata['name']] = {'data': [e], 'parent': e}
                
        return(self)
    
    def archive_xyz(self, **kwargs):
        """Archive data from the dataset to XYZ in the given dataset region.
        
        will convert all data to XYZ within the given region and will arrange
        the data as a datalist based on inputs.

        Data comes from `self.xyz_yield` set in `self.set_yield`. So will process data through
        `_stacks` before archival if region, x_inc and y_inc are set.
        """

        def xdl2dir(xdl):
            this_dir = []
            while True:
                if xdl.parent is None:
                    out_dir = xdl.metadata['name']
                    if xdl.remote:
                        out_dir = out_dir.split(':')[0]

                    this_dir.append(out_dir)
                    break
                
                out_dir = xdl.parent.metadata['name']
                if xdl.parent.remote:
                    out_dir = utils.fn_bansename2(out_dir)
                    
                this_dir.append(out_dir)
                xdl = xdl.parent
                
            this_dir.reverse()
            return(this_dir)
        
        #aa_name = self.metadata['name'].split(':')[0]
        if self.region is None:
            a_name = '{}_{}'.format(self.metadata['name'], utils.this_year())
        else:
            a_name = '{}_{}_{}'.format(
                self.metadata['name'], self.region.format('fn'), utils.this_year())

        if self.remote:
            a_name = a_name.split(':')[0]
            
        self.parse_data_lists() # parse the datalist into a dictionary
        self.archive_datalist = '{}.datalist'.format(a_name)
        with open('{}.datalist'.format(a_name), 'w') as dlf:
            for x in self.data_lists.keys():
                utils.echo_msg(self.data_lists[x])

                x_name = utils.fn_basename2(os.path.basename(x.split(':')[0])) if self.data_lists[x]['parent'].remote else os.path.basename(x)
                if self.region is None:
                    a_dir = '{}_{}'.format(x_name, utils.this_year())
                else:
                    a_dir = '{}_{}_{}'.format(x_name, self.region.format('fn'), utils.this_year())
                    
                this_dir = xdl2dir(self.data_lists[x]['parent'])
                this_dir.append(a_dir)
                tmp_dir = this_dir
                dlf.write(
                    '{name}.datalist -1 {weight} {uncertainty} {metadata}\n'.format(
                        name=os.path.join(*(this_dir + [this_dir[-1]])),
                        weight = self.data_lists[x]['parent'].weight,
                        uncertainty = self.data_lists[x]['parent'].uncertainty,
                        #weight = 1 if self.weight is None else self.weight,
                        #uncertainty = 0 if self.uncertainty is None else self.uncertainty,
                        metadata = self.data_lists[x]['parent'].format_metadata()
                    )
                )
                this_dir = os.path.join(os.getcwd(), *this_dir)
                if not os.path.exists(this_dir):
                    os.makedirs(this_dir)
                    
                with open(
                        os.path.join(
                            this_dir, '{}.datalist'.format(os.path.basename(this_dir))), 'w'
                ) as sub_dlf:
                    for xyz_dataset in self.data_lists[x]['data']:
                        xyz_dataset.verbose=self.verbose
                        if xyz_dataset.remote == True:
                            ## we will want to split remote datasets into individual files if needed...
                            sub_xyz_path = '{}_{}.xyz'.format(utils.fn_basename2(os.path.basename(xyz_dataset.fn.split(':')[0])), self.region.format('fn'))
                        elif len(xyz_dataset.fn.split('.')) > 1:
                            xyz_ext = xyz_dataset.fn.split('.')[-1]
                            sub_xyz_path = '.'.join(
                                [utils.fn_basename(
                                    utils.slugify(
                                        os.path.basename(xyz_dataset.fn)
                                    ),
                                    xyz_dataset.fn.split('.')[-1]),
                                 'xyz']
                            )
                        else:
                            sub_xyz_path = '.'.join([utils.fn_basename2(os.path.basename(xyz_dataset.fn)), 'xyz'])

                        this_xyz_path = os.path.join(this_dir, sub_xyz_path)
                        sub_dlf.write('{} 168 1 0\n'.format(sub_xyz_path))            
                        with open(this_xyz_path, 'w') as xp:
                            for this_xyz in xyz_dataset.xyz_yield: # data will be processed independently of each other
                                #yield(this_xyz) # don't need to yield data here.
                                this_xyz.dump(include_w=True if self.weight is not None else False,
                                              include_u=True if self.uncertainty is not None else False,
                                              dst_port=xp, encode=False)

    def yield_block_array(self):
        """yield the xyz data as arrays, for use in `array_yield` or `yield_array`

        Yields:
          tuple: (list of arrays for [weighted]mean, weights mask, uncertainty mask, and count, srcwin, geotransform)
        """

        out_arrays = {'z':None, 'count':None, 'weight':None, 'uncertainty': None, 'mask':None}
        xcount, ycount, dst_gt = self.region.geo_transform(
            x_inc=self.x_inc, y_inc=self.y_inc, node='grid'
        )

        for this_xyz in self.yield_xyz():
            xpos, ypos = utils._geo2pixel(
                this_xyz.x, this_xyz.y, dst_gt, 'pixel'
            )
            if xpos < xcount and ypos < ycount and xpos >= 0 and ypos >= 0:
                w = this_xyz.w if self.weight is not None else 1
                u = this_xyz.u if self.uncertainty is not None else 0
                out_arrays['z'] = np.array([[this_xyz.z]])
                out_arrays['count'] = np.array([[1]])
                out_arrays['weight'] = np.array([[this_xyz.w]])
                out_arrays['uncertainty'] = np.array([[this_xyz.u]])
                this_srcwin = (xpos, ypos, 1, 1)
                yield(out_arrays, this_srcwin, dst_gt)

    # todo move mask_array to _stacks
    def _mask(self, out_name = None, method='multi', fmt='GTiff'):
        """generate a multi-banded  mask grid

        each band will represent a top-level dataset. If run from
        `parse` instead of `parse_json` will mask each dataset at it's most
        recent parent location.
        
        -----------
        Parameters:

        out_name: output mask raster basename
        method: masking method, either 'multi' or 'single'
                where 'multi' will generate a multi-band raster with masks for each dataset parent
                and 'single' will generate a single-band raster with a single data mask
        fmt: output gdal raster format
        """
        
        utils.set_cache(self.cache_dir)
        if out_name is None:
            out_name = '_m'.format(os.path.join(self.cache_dir, '{}'.format(
                utils.append_fn('_dlim_stacks', self.region, self.x_inc)
            )))

        xcount, ycount, dst_gt = self.region.geo_transform(
            x_inc=self.x_inc, y_inc=self.y_inc, node='grid'
        )

        with utils.CliProgress(
                message='masking data to {}/{} grid to {} {}'.format(ycount, xcount, self.region, out_name),
                verbose=self.verbose
        ) as pbar:        
            gdt = gdal.GDT_Int32
            mask_fn = '{}.{}'.format(out_name, gdalfun.gdal_fext(fmt))
            if method == 'single':
                driver = gdal.GetDriverByName(fmt)
                if os.path.exists(mask_fn):
                    driver.Delete(mask_fn)
                    
                m_ds = driver.Create(mask_fn, xcount, ycount, 0, gdt)
                m_ds.SetGeoTransform(dst_gt)
                m_band = m_ds.GetRasterBand(1)
                m_band.SetNoDataValue(ndv)
                for arrs, srcwin, gt in self.yield_array():
                    pbar.update()
                    m_array = m_band.ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                    m_array[arrs['count'] != 0] = 1
                    m_band.WriteArray(m_array, srcwin[0], srcwin[1])
                    yield((arrs, srcwin, gt))
            else:
                driver = gdal.GetDriverByName('MEM')
                m_ds = driver.Create(utils.make_temp_fn(out_name), xcount, ycount, 0, gdt)
                m_ds.SetGeoTransform(dst_gt)
                for this_entry in self.parse_json():
                    pbar.update()
                    bands = {m_ds.GetRasterBand(i).GetDescription(): i for i in range(1, m_ds.RasterCount + 1)}
                    if not this_entry.metadata['name'] in bands.keys():
                        m_ds.AddBand()
                        m_band = m_ds.GetRasterBand(m_ds.RasterCount)
                        m_band.SetNoDataValue(0)
                        m_band.SetDescription(this_entry.metadata['name'])
                        band_md = m_band.GetMetadata()
                        for k in this_entry.metadata.keys():
                            band_md[k] = this_entry.metadata[k]

                        band_md['weight'] = this_entry.weight
                        band_md['uncertainty'] = this_entry.uncertainty
                        m_band.SetMetadata(band_md)
                    else:
                        m_band = m_ds.GetRasterBand(bands[this_entry.metadata['name']])

                    for arrs, srcwin, gt in this_entry.yield_array():
                        m_array = m_band.ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                        m_array[arrs['count'] != 0] = 1
                        m_band.WriteArray(m_array, srcwin[0], srcwin[1])
                        yield((arrs, srcwin, gt))

            dst_ds = gdal.GetDriverByName(fmt).CreateCopy(mask_fn, m_ds, 0)

            ## create a vector of the masks (spatial-metadata)
            if self.want_sm:
                gdalfun.ogr_polygonize_multibands(dst_ds)
            
        m_ds = dst_ds =None

    def _stacks(self, supercede = False, out_name = None, ndv = -9999, fmt = 'GTiff', want_mask = False):
        """stack incoming arrays (from `array_yield`) together

        -----------
        Parameters:
        supercede (bool): if true, higher weighted data superceded lower weighted, otherwise
                           will combine data using weighted-mean
        out_name (str): the output stacked raster basename
        ndv (float): the desired no data value
        fmt (str): the output GDAL file format
        want_mask (bool): generate a data mask

        --------
        Returns:
        output-file-name of a multi-band raster with the following bands:
          z
          weights
          count
          uncertainty
          src uncertainty
        """

        ## check for x_inc, y_inc and region
        utils.set_cache(self.cache_dir)
        ## initialize the output raster
        if out_name is None:
            out_name = os.path.join(self.cache_dir, '{}'.format(
                utils.append_fn('_dlim_stacks', self.region, self.x_inc)
            ))
            
        out_file = '{}.{}'.format(out_name, gdalfun.gdal_fext(fmt))
        xcount, ycount, dst_gt = self.region.geo_transform(
            x_inc=self.x_inc, y_inc=self.y_inc, node='grid'
        )
        if xcount <= 0 or ycount <=0:
            utils.echo_error_msg(
                'could not create grid of {}x{} cells with {}/{} increments on region: {}'.format(
                    xcount, ycount, self.x_inc, self.y_inc, self.region
                )
            )
            sys.exit(-1)

        gdt = gdal.GDT_Float32
        c_gdt = gdal.GDT_Int32
        driver = gdal.GetDriverByName(fmt)
        if os.path.exists(out_file):
            driver.Delete(out_file)
        
        dst_ds = driver.Create(out_file, xcount, ycount, 5, gdt,
                               options=['COMPRESS=LZW', 'PREDICTOR=2', 'TILED=YES'] if fmt != 'MEM' else [])

        if dst_ds is None:
            utils.echo_error_msg('failed to create stack grid...')
            sys.exit(-1)
            
        dst_ds.SetGeoTransform(dst_gt)
        stacked_bands = {'z': dst_ds.GetRasterBand(1), 'count': dst_ds.GetRasterBand(2),
                         'weights': dst_ds.GetRasterBand(3), 'uncertainty': dst_ds.GetRasterBand(4),
                         'src_uncertainty': dst_ds.GetRasterBand(5) }        
        stacked_data = {'z': None, 'count': None, 'weights': None, 'uncertainty': None, 'src_uncertainty': None}
        
        for key in stacked_bands.keys():
            stacked_bands[key].SetNoDataValue(np.nan)
            #stacked_bands[key].SetNoDataValue(ndv)
            stacked_bands[key].SetDescription(key)
        
        ## incoming arrays can be quite large...perhaps chunks these
        ## incoming arrays arrs['z'], arrs['weight'] arrs['uncertainty'], and arrs['count']
        ## srcwin is the srcwin of the waffle relative to the incoming arrays
        ## gt is the geotransform of the incoming arrays
        ## `self.array_yield` is set in `self.set_array`
        #for arrs, srcwin, gt in self.yield_array():
        if want_mask:
            array_yield = self._mask(out_name='{}_m'.format(out_name), method='multi', fmt=fmt)
        else:
            array_yield = self.yield_array()
            
        # with utils.CliProgress(
        #         message='stacking data to {}/{} grid using {} method to {}'.format(
        #             ycount, xcount, 'supercede' if supercede else 'weighted mean', out_name
        #         ),
        #         verbose=self.verbose
        # ) as pbar:
        for arrs, srcwin, gt in array_yield:
            #pbar.update()
            ## Read the saved accumulated rasters at the incoming srcwin and set ndv to zero
            #print('stacks:', srcwin)
            for key in stacked_bands.keys():
                stacked_data[key] = stacked_bands[key].ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                #print(stacked_data[key].shape)
                #stacked_data[key][stacked_data[key] == ndv] = 0
                stacked_data[key][np.isnan(stacked_data[key])] = 0

            ## set incoming np.nans to zero and mask to non-nan count
            arrs['weight'][np.isnan(arrs['z'])] = 0
            arrs['uncertainty'][np.isnan(arrs['z'])] = 0
            arrs['z'][np.isnan(arrs['z'])] = 0
            for arr_key in arrs:
                if arrs[arr_key] is not None:
                    arrs[arr_key][np.isnan(arrs[arr_key])] = 0

            ## add the count to the accumulated rasters
            #print(stacked_data['count'].shape)
            #print(arrs['count'].shape)
            stacked_data['count'] += arrs['count']

            ## supercede based on weights, else do weighted mean
            ## todo: do (weighted) mean on cells with same weight
            if supercede:
                ## higher weight supercedes lower weight (first come first served atm)
                stacked_data['z'][arrs['weight'] > stacked_data['weights']] = arrs['z'][arrs['weight'] > stacked_data['weights']]
                stacked_data['src_uncertainty'][arrs['weight'] > stacked_data['weights']] = arrs['uncertainty'][arrs['weight'] > stacked_data['weights']]
                stacked_data['weights'][arrs['weight'] > stacked_data['weights']] = arrs['weight'][arrs['weight'] > stacked_data['weights']]
                #stacked_data['weights'][stacked_data['weights'] == 0] = np.nan
                ## uncertainty is src_uncertainty, as only one point goes into a cell
                stacked_data['uncertainty'][:] = stacked_data['src_uncertainty'][:]

                # ## reset all data where weights are zero to nan
                # for key in stacked_bands.keys():
                #     stacked_data[key][np.isnan(stacked_data['weights'])] = np.nan

            else:
                ## accumulate incoming z*weight and uu*weight                
                stacked_data['z'] += (arrs['z'] * arrs['weight'])
                stacked_data['src_uncertainty'] += (arrs['uncertainty'] * arrs['weight'])

                ## accumulate incoming weights (weight*weight?) and set results to np.nan for calcs
                stacked_data['weights'] += arrs['weight']
                stacked_data['weights'][stacked_data['weights'] == 0] = np.nan
                ## accumulate variance * weight
                stacked_data['uncertainty'] += arrs['weight'] * np.power((arrs['z'] - (stacked_data['z'] / stacked_data['weights'])), 2)

            ## write out results to accumulated rasters
            #stacked_data['count'][stacked_data['count'] == 0] = ndv
            stacked_data['count'][stacked_data['count'] == 0] = np.nan

            for key in stacked_bands.keys():
                #stacked_data[key][np.isnan(stacked_data[key])] = ndv
                #stacked_data[key][stacked_data['count'] == ndv] = ndv
                stacked_data[key][np.isnan(stacked_data['count'])] = np.nan
                if supercede:
                    stacked_data[key][np.isnan(stacked_data[key])] = ndv

                stacked_bands[key].WriteArray(stacked_data[key], srcwin[0], srcwin[1])

        ## Finalize weighted mean rasters and close datasets
        ## incoming arrays have all been processed, if weighted mean the
        ## "z" is the sum of z*weight, "weights" is the sum of weights
        ## "uncertainty" is the sum of variance*weight
        if not supercede:
            srcwin = (0, 0, dst_ds.RasterXSize, dst_ds.RasterYSize)
            for y in range(
                    srcwin[1], srcwin[1] + srcwin[3], 1
            ):
                for key in stacked_bands.keys():
                    stacked_data[key] = stacked_bands[key].ReadAsArray(srcwin[0], y, srcwin[2], 1)
                    #stacked_data[key][stacked_data[key] == ndv] = np.nan

                ## average the accumulated arrays for finalization
                ## z and u are weighted sums, so divide by weights
                stacked_data['weights'] = stacked_data['weights'] / stacked_data['count']
                stacked_data['src_uncertainty'] = (stacked_data['src_uncertainty'] / stacked_data['weights']) / stacked_data['count']
                stacked_data['z'] = (stacked_data['z'] / stacked_data['weights']) / stacked_data['count']

                ## apply the source uncertainty with the sub-cell variance uncertainty
                ## point density (count/cellsize) effects uncertainty? higer density should have lower unertainty perhaps...
                stacked_data['uncertainty'] = np.sqrt((stacked_data['uncertainty'] / stacked_data['weights']) / stacked_data['count'])
                stacked_data['uncertainty'] = np.sqrt(np.power(stacked_data['src_uncertainty'], 2) + np.power(stacked_data['uncertainty'], 2))

                ## write out final rasters
                for key in stacked_bands.keys():
                    stacked_data[key][np.isnan(stacked_data[key])] = ndv
                    stacked_bands[key].WriteArray(stacked_data[key], srcwin[0], y)

        ## set the final output nodatavalue
        for key in stacked_bands.keys():
            stacked_bands[key].DeleteNoDataValue()
        for key in stacked_bands.keys():
            stacked_bands[key].SetNoDataValue(ndv)
            
        dst_ds = None
        return(out_file)

    def stacks_yield_xyz(self, supercede = False, out_name = None, ndv = -9999, fmt = 'GTiff', want_mask = False):
        """yield the result of `_stacks` as xyz"""
        
        stacked_fn = self._stacks(supercede=supercede, out_name=out_name, ndv=ndv, fmt=fmt, want_mask=want_mask)
        sds = gdal.Open(stacked_fn)
        sds_gt = sds.GetGeoTransform()
        sds_z_band = sds.GetRasterBand(1) # the z band from stacks
        sds_w_band = sds.GetRasterBand(3) # the weight band from stacks
        sds_u_band = sds.GetRasterBand(4) # the uncertainty band from stacks
        srcwin = (0, 0, sds.RasterXSize, sds.RasterYSize)
        for y in range(srcwin[1], srcwin[1] + srcwin[3], 1):
            sz = sds_z_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
            sw = sds_w_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
            su = sds_u_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)            
            for x in range(0, sds.RasterXSize):
                z = sz[0, x]
                if z != ndv:
                    geo_x, geo_y = utils._pixel2geo(x, y, sds_gt)
                    out_xyz = xyzfun.XYZPoint(
                        x=geo_x, y=geo_y, z=z, w=sw[0, x], u=su[0, x]
                    )
                    yield(out_xyz)
        sds = None
    
    def vectorize_xyz(self):
        """Make a point vector OGR DataSet Object from src_xyz

        for use in gdal gridding functions
        """

        dst_ogr = '{}'.format(self.metadata['name'])
        ogr_ds = gdal.GetDriverByName('Memory').Create(
            '', 0, 0, 0, gdal.GDT_Unknown
        )
        layer = ogr_ds.CreateLayer(
            dst_ogr,
            geom_type=ogr.wkbPoint25D
        )
        for x in ['long', 'lat', 'elev', 'weight']:
            fd = ogr.FieldDefn(x, ogr.OFTReal)
            fd.SetWidth(12)
            fd.SetPrecision(8)
            layer.CreateField(fd)
            
        f = ogr.Feature(feature_def=layer.GetLayerDefn())        
        for this_xyz in self.xyz_yield:
            f.SetField(0, this_xyz.x)
            f.SetField(1, this_xyz.y)
            f.SetField(2, this_xyz.z)
            f.SetField(3, this_xyz.w)
                
            wkt = this_xyz.export_as_wkt(include_z=True)
            g = ogr.CreateGeometryFromWkt(wkt)
            f.SetGeometryDirectly(g)
            layer.CreateFeature(f)
            
        return(ogr_ds)
                        
## ==============================================
## XYZ dataset.
## file or stdin...
## ==============================================
class XYZFile(ElevationDataset):
    """representing an ASCII xyz dataset stream.

    Parse data from an xyz file/stdin

    generate_inf - generate an inf file for the xyz data
    yield_xyz - yield the xyz data as xyz
    yield_array - yield the xyz data as an array
                  must set the x_inc/y_inc in the
                  super class
    
    -----------
    Parameters:
    
    delim: the delimiter of the xyz data (str)
    xpos: the position (int) of the x value
    ypos: the position (int) of the y value
    zpos: the position (int) of the z value
    wpos: the position (int) of the w value (weight)
    upos: the position (int) of the u value (uncertainty)
    skip: number of lines to skip
    x_scale: scale the x value
    y_scale: scale the y value
    z_scale: scale the z value
    x_offset: offset the x value
    y_offset: offset the y value    
    """

    def __init__(self, delim = None, xpos = 0, ypos = 1, zpos = 2,
                 wpos = None, upos = None, skip = 0, x_scale = 1, y_scale = 1,
                 z_scale = 1, x_offset = 0, y_offset = 0, **kwargs):
        super().__init__(**kwargs)
        self.delim = delim # the file delimiter
        self.xpos = utils.int_or(xpos, 0) # the position of the x/lat value
        self.ypos = utils.int_or(ypos, 1) # the position of the y/lon value
        self.zpos = utils.int_or(zpos, 2) # the position of the z value
        self.wpos = utils.int_or(wpos) # the position of the weight value
        self.upos = utils.int_or(upos) # the position of the uncertainty value
        self.skip = utils.int_or(skip, 0) # number of header lines to skip
        self.x_scale = utils.float_or(x_scale, 1) # multiply x by x_scale
        self.y_scale = utils.float_or(y_scale, 1) # multiply y by y_scale
        self.z_scale = utils.float_or(z_scale, 1) # multiply z by z_scale
        #self.x_offset = utils.int_or(x_offset, 0) # offset x by x_offset
        self.x_offset = x_offset
        self.y_offset = utils.int_or(y_offset, 0) # offset y by y_offset

        #self.scoff = False
        self.rem = False        
        #self._known_delims = [None, ',', '/', ':'] # space and tab are 'None'
        #self.data_format = 168

    def init_ds(self):
        if self.delim is not None:
            xyzfun._known_delims.insert(0, self.delim)
            
        if self.x_offset == 'REM':
            self.x_offset = 0
            self.rem = True
            
        self.scoff = True if self.x_scale != 1 or self.y_scale != 1 or self.z_scale != 1 \
           or self.x_offset != 0 or self.y_offset != 0 else False

        #if self.src_srs is not None:
        #    self.set_transform()        
            
    def generate_inf(self, callback=lambda: False):
        """generate a infos dictionary from the xyz dataset"""

        pts = []
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['numpts'] = 0
        self.infos['format'] = self.data_format
        this_region = regions.Region()
        region_ = self.region
        self.region = None

        for i, l in enumerate(self.yield_xyz()):
            if i == 0:
                this_region.from_list([l.x, l.x, l.y, l.y, l.z, l.z])
            else:
                if l.x < this_region.xmin:
                    this_region.xmin = l.x
                elif l.x > this_region.xmax:
                    this_region.xmax = l.x
                if l.y < this_region.ymin:
                    this_region.ymin = l.y
                elif l.y > this_region.ymax:
                    this_region.ymax = l.y
                if l.z < this_region.zmin:
                    this_region.zmin = l.z
                elif l.z > this_region.zmax:
                    this_region.zmax = l.z
            #pts.append(l.export_as_list(include_z = True))
            self.infos['numpts'] = i

        self.infos['minmax'] = this_region.export_as_list(include_z = True)
        if self.infos['numpts'] > 0:
            ## todo: put qhull in a yield and perform while scanning for min/max
            # try:
            #     out_hull = [pts[i] for i in ConvexHull(
            #         pts, qhull_options='Qt'
            #     ).vertices]
            #     out_hull.append(out_hull[0])
            #     self.infos['wkt'] = regions.create_wkt_polygon(out_hull, xpos=0, ypos=1)
            # except:
            self.infos['wkt'] = this_region.export_as_wkt()
                
        self.region = region_
        self.infos['src_srs'] = self.src_srs
        
        return(self.infos)

    def line_delim(self, xyz_line):
        """guess a line delimiter"""

        for delim in xyzfun._known_delims:
            try:
                this_xyz = xyz_line.split(delim)
                if len(this_xyz) > 1:
                    return(this_xyz)
            except:
                pass
            
    def yield_xyz(self):
        """xyz file parsing generator"""
        
        if self.fn is not None:
            if os.path.exists(str(self.fn)):
                self.src_data = open(self.fn, "r")
            else:
                self.src_data = self.fn
        else:
            self.src_data = sys.stdin

        self.init_ds()            
        count = 0
        skip = self.skip
        for xyz_line in self.src_data:
            if count >= skip:
                this_xyz = self.line_delim(xyz_line)
                if this_xyz is None:
                    continue

                w = float(this_xyz[self.wpos]) if self.wpos is not None else 1
                u = float(this_xyz[self.upos]) if self.upos is not None else 0
                try:
                    this_xyz = xyzfun.XYZPoint(
                        x=this_xyz[self.xpos], y=this_xyz[self.ypos], z=this_xyz[self.zpos]
                    )
                except Exception as e:
                    utils.echo_error_msg('{} ; {}'.format(e, this_xyz))
                    this_xyz = xyzfun.XYZPoint()

                if this_xyz.valid_p():

                    if self.scoff:
                        this_xyz.x = (this_xyz.x+self.x_offset) * self.x_scale
                        this_xyz.y = (this_xyz.y+self.y_offset) * self.y_scale
                        this_xyz.z *= self.z_scale

                    if self.rem:
                        this_xyz.x = math.fmod(this_xyz.x+180,360)-180 

                    this_xyz.w = w if self.weight is None else self.weight * w
                    this_xyz.u = u if self.uncertainty is None else math.sqrt(self.uncertainty**2 + u**2)
                    if self.dst_trans is not None:
                        this_xyz.transform(self.dst_trans)

                    if self.region is not None and self.region.valid_p():
                        if self.invert_region:
                            if not regions.xyz_in_region_p(this_xyz, self.region):
                                count += 1
                                yield(this_xyz)
                        else:
                            if regions.xyz_in_region_p(this_xyz, self.region):
                                count += 1
                                yield(this_xyz)
                    else:
                        count += 1
                        yield(this_xyz)

            else: skip -= 1

        if self.verbose:
            utils.echo_msg(
                'parsed {} data records from {}{}'.format(
                    count, self.fn, ' @{}'.format(self.weight) if self.weight is not None else ''
                )
            )
            
        self.src_data.close()

    def yield_array(self):        
        for arrs in self.yield_block_array():
            yield(arrs)
        
## ==============================================
## LAS/LAZ Dataset.
## ==============================================
class LASFile(ElevationDataset):
    """representing an LAS/LAZ dataset.

    Process LAS/LAZ lidar files using pylas.
    
    get_epsg - attempt to parse the EPSG from the LAS file header
    generate_inf - generate an inf file for the LAS data
    yield_xyz - yield the LAS data as xyz
    yield_array - yield the LAS data as an array
                  must set the x_inc/y_inc in the
                  super class
    
    -----------
    Parameters:

    classes (str): a list of classes to parse, being a string with `/` seperator 
    """

    def __init__(self, classes='0/2/29/40', **kwargs):
        super().__init__(**kwargs)
        self.classes = [int(x) for x in classes.split('/')]

    def init_ds(self):
        if self.src_srs is None:
            self.src_srs = self.get_epsg()
            if self.src_srs is None:
                self.src_srs = self.infos['src_srs']
            
            self.set_transform()
            
        # self.las_region = None
        # if self.region is not None  and self.region.valid_p():
        #     self.las_region = self.region.copy() if self.dst_trans is None else self.trans_region.copy()

        # ## todo: fix this for dst_trans!
        # self.las_x_inc = self.x_inc
        # self.las_y_inc = self.y_inc
        # if self.las_x_inc is not None and self.las_y_inc is not None:
        #     if self.dst_trans is not None:
        #         self.las_x_inc, self.las_y_inc = self.las_region.increments(self.xcount, self.ycount)
        #         self.las_dst_gt = self.dst_gt
        #     else:
        #         self.las_xcount, self.las_ycount, self.las_dst_gt = self.las_region.geo_transform(
        #             x_inc=self.x_inc, y_inc=self.y_inc, node='grid'
        #         )

    def get_epsg(self):
        with lp.open(self.fn) as lasf:
            lasf_vlrs = lasf.header.vlrs
            for vlr in lasf_vlrs:
                #if 'OGC WKT' in vlr.description:
                #utils.echo_msg(vlr.description)
                if vlr.record_id == 2112:
                    src_srs = osr.SpatialReference()
                    src_srs.SetFromUserInput(vlr.string)
                    src_srs.AutoIdentifyEPSG()
                    srs_auth = src_srs.GetAttrValue('AUTHORITY', 1)
                    vert_cs = src_srs.GetAttrValue('vert_cs')
                    proj4 = src_srs.ExportToProj4()
                    vert_auth = src_srs.GetAttrValue('VERT_CS|AUTHORITY',1)
                    src_srs = None
                    
                    if srs_auth is not None:
                        if vert_auth is not None:
                            return('epsg:{}+{}'.format(srs_auth, vert_auth))
                        else:
                            return('epsg:{}'.format(srs_auth))
                    else:
                        return(proj4)
                    break
            return(None)
        
    def generate_inf(self, callback=lambda: False):
        """generate an inf file for a lidar dataset."""
        
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['numpts'] = 0
        self.infos['format'] = self.data_format
        this_region = regions.Region()

        with lp.open(self.fn) as lasf:
            self.infos['numpts'] = lasf.header.point_count
            this_region.from_list([lasf.header.x_min, lasf.header.x_max,
                                   lasf.header.y_min, lasf.header.y_max,
                                   lasf.header.z_min, lasf.header.z_max])

        self.infos['src_srs'] = self.src_srs if self.src_srs is not None else self.get_epsg()
        self.infos['minmax'] = this_region.export_as_list(
            include_z=True
        )
        self.infos['wkt'] = this_region.export_as_wkt()
        return(self.infos)

    def generate_inf_scan(self, callback=lambda: False):
        """generate an inf file for a lidar dataset.
        ... parse the data to obtain the hull region
        """

        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['numpts'] = 0
        this_region = regions.Region()

        pts = []
        region_ = self.region
        self.region = None

        for i, l in enumerate(self.yield_xyz()):
            if i == 0:
                this_region.from_list([l.x, l.x, l.y, l.y, l.z, l.z])
            else:
                if l.x < this_region.xmin:
                    this_region.xmin = l.x
                elif l.x > this_region.xmax:
                    this_region.xmax = l.x
                if l.y < this_region.ymin:
                    this_region.ymin = l.y
                elif l.y > this_region.ymax:
                    this_region.ymax = l.y
                if l.z < this_region.zmin:
                    this_region.zmin = l.z
                elif l.z > this_region.zmax:
                    this_region.zmax = l.z
            pts.append(l.export_as_list(include_z = True))
            self.infos['numpts'] = i

        self.infos['minmax'] = this_region.export_as_list(include_z = True)
        if self.infos['numpts'] > 0:
            try:
                out_hull = [pts[i] for i in ConvexHull(
                    pts, qhull_options='Qt'
                ).vertices]
                out_hull.append(out_hull[0])
                self.infos['wkt'] = regions.create_wkt_polygon(out_hull, xpos=0, ypos=1)
            except:
                self.infos['wkt'] = this_region.export_as_wkt()
                
        self.region = region_
        return(self.infos)

    def yield_points(self):
        self.init_ds()
        with lp.open(self.fn) as lasf:
            for points in lasf.chunk_iterator(2_000_000):
                #for points in lasf.read_points:
                points = points[(np.isin(points.classification, self.classes))]
                if self.region is not None  and self.region.valid_p():
                    las_region = self.region.copy() if self.dst_trans is None else self.trans_region.copy()
                    if self.invert_region:
                        points = points[((points.x > las_region.xmax) | (points.x < las_region.xmin)) | \
                                        ((points.y > las_region.ymax) | (points.y < las_region.ymin))]
                        if las_region.zmin is not None:
                            points =  points[(points.z < las_region.zmin)]
                            if las_region.zmax is not None:
                                points =  points[(points.z > las_region.zmax)]
                    else:
                        points = points[((points.x < las_region.xmax) & (points.x > las_region.xmin)) & \
                                        ((points.y < las_region.ymax) & (points.y > las_region.ymin))]
                        if las_region.zmin is not None:
                            points =  points[(points.z > las_region.zmin)]
                            
                        if las_region.zmax is not None:
                            points =  points[(points.z < las_region.zmax)]
                            
                    if len(points) > 0:
                        yield(points)
    
    def yield_xyz(self):
        """LAS file parsing generator"""
        
        count = 0
        for points in self.yield_points():
            dataset = np.vstack((points.x, points.y, points.z)).transpose()
            count += len(dataset)
            for point in dataset:
                this_xyz = xyzfun.XYZPoint(x=point[0], y=point[1], z=point[2],
                                           w=self.weight, u=self.uncertainty)
                if self.dst_trans is not None:
                    this_xyz.transform(self.dst_trans)

                yield(this_xyz)

        if self.verbose:
            utils.echo_msg(
                'parsed {} data records from {}{}'.format(
                    count, self.fn, ' @{}'.format(self.weight) if self.weight is not None else ''
                )
            )
                    
    def yield_array(self):
        out_arrays = {'z':None, 'count':None, 'weight':None, 'uncertainty': None, 'mask':None}
        count = 0
        for points in self.yield_points():            
            xcount, ycount, dst_gt = self.region.geo_transform(
                x_inc=self.x_inc, y_inc=self.y_inc, node='grid'
            )

            pixel_x = np.floor((points.x - dst_gt[0]) / dst_gt[1]).astype(int)
            pixel_y = np.floor((points.y - dst_gt[3]) / dst_gt[5]).astype(int)
            # pixel_x = ((points.x - self.las_dst_gt[0]) / self.las_dst_gt[1]).astype(int)
            # pixel_y = ((points.y - self.las_dst_gt[3]) / self.las_dst_gt[5]).astype(int)
            
            # pixel_upper_mask = pixel_x < self.las_xcount | pixel_y < self.las_ycount
            # pixel_lower_mask = pixel_x > 0 | pixel_y > 0
            # pixel_x = pixel_x[pixel_mask]
            # pixel_y = pixel_y[pixel_mask]

            pixel_z = points.z

            # pixel_z = pixel_z[pixel_x < xcount]            
            # pixel_y = pixel_y[pixel_x < xcount]            
            # pixel_x = pixel_x[pixel_x < xcount]

            # pixel_z = pixel_z[pixel_y < ycount]
            # pixel_x = pixel_x[pixel_y < ycount]
            # pixel_y = pixel_y[pixel_y < ycount]
            
            # pixel_y = pixel_y[pixel_x > 0]
            # pixel_x = pixel_x[pixel_x > 0]
            # pixel_x = pixel_x[pixel_y > 0]
            # pixel_y = pixel_y[pixel_y > 0]
            
            this_srcwin = (int(min(pixel_x)), int(min(pixel_y)), int(max(pixel_x) - min(pixel_x))+1, int(max(pixel_y) - min(pixel_y))+1)
            count += len(pixel_x)

            ## adjust pixels to srcwin and stack together
            pixel_x = pixel_x - this_srcwin[0]
            pixel_y = pixel_y - this_srcwin[1]
            pixel_xy = np.vstack((pixel_y, pixel_x)).T

            ## find the non-unique x/y points and mean their z values together
            unq, unq_idx, unq_inv, unq_cnt = np.unique(
                pixel_xy, axis=0, return_inverse=True, return_index=True, return_counts=True
            )
            cnt_msk = unq_cnt > 1
            cnt_idx, = np.nonzero(cnt_msk)
            idx_msk = np.in1d(unq_inv, cnt_idx)
            idx_idx, = np.nonzero(idx_msk)
            srt_idx = np.argsort(unq_inv[idx_msk])
            dup_idx = np.split(idx_idx[srt_idx], np.cumsum(unq_cnt[cnt_msk])[:-1])
            #zz = points.z[unq_idx]
            zz = pixel_z[unq_idx]
            u = np.zeros(zz.shape)
            dup_means = [np.mean(points.z[dup]) for dup in dup_idx]
            dup_stds = [np.std(points.z[dup]) for dup in dup_idx]
            zz[cnt_msk] = dup_means
            u[cnt_msk] = dup_stds

            ## make the output arrays to yield
            out_z = np.zeros((this_srcwin[3], this_srcwin[2]))
            out_z[unq[:,0], unq[:,1]] = zz
            out_z[out_z == 0] = np.nan
            out_arrays['z'] = out_z
            out_arrays['count'] = np.zeros((this_srcwin[3], this_srcwin[2]))
            out_arrays['count'][unq[:,0], unq[:,1]] = unq_cnt
            out_arrays['weight'] = np.ones((this_srcwin[3], this_srcwin[2]))
            out_arrays['uncertainty'] = np.zeros((this_srcwin[3], this_srcwin[2]))
            out_arrays['uncertainty'][unq[:,0], unq[:,1]] = u

            yield(out_arrays, this_srcwin, dst_gt)

        if self.verbose:
            utils.echo_msg(
                'parsed {} data records from {}{}'.format(
                    count, self.fn, ' @{}'.format(self.weight) if self.weight is not None else ''
                )
            )
                       
## ==============================================
## Raster Dataset.
## Parses with GDAL
## ==============================================
class GDALFile(ElevationDataset):
    """providing a GDAL raster dataset parser.

    Process/Parse GDAL supported raster files.
    See GDAL for more information regarding supported formats.
    
    generate_inf - generate an inf file for the RASTER data
    yield_xyz - yield the RASTER data as xyz
    yield_array - yield the RASTER data as an array

    -----------
    Parameters:

    mask: raster dataset to use as MASK dataset OR a band number
    weight_mask: raster dataset to use as weights (per-cell) OR a band number
    otherwise will use single value (weight) from superclass.
    uncertainty_mask: raster dataset to use as uncertainty (per-cell) OR a band number
    otherwise will use a single value (uncertainty) from superclass.
    open_options: GDAL open_options for raster dataset
    sample: sample method to use in resamplinig
    resample: resample the grid to `x_inc` and `y_inc` from superclass
    check_path: check to make sure path exists
    super_grid: Force processing of a supergrid (BAG files) (True/False)
    band_no: the band number of the elevation data
    """
    
    def __init__(self, mask = None, weight_mask = None, uncertainty_mask = None,  open_options = None,
                 sample = None, resample = True, check_path = True, super_grid = False, band_no = 1, **kwargs):
        super().__init__(**kwargs)
        self.mask = mask
        self.weight_mask = weight_mask
        self.uncertainty_mask = uncertainty_mask
        self.open_options = open_options
        self.sample = sample
        self.resample = resample
        self.check_path = check_path
        self.super_grid = super_grid
        self.band_no = band_no

    def init_ds(self):
        """initialize the raster dataset

        if x/y incs are set, will warp raster to that resolution.
        """

        if self.check_path and not os.path.exists(self.fn):
            return(None)

        ## apply any open_options that are specified
        try:
            self.open_options = self.open_options.split('/')
        except AttributeError:
            self.open_options = self.open_options
        except:
            self.open_options = None

        if self.valid_p() and self.src_srs is None:
            self.src_srs = gdalfun.gdal_get_srs(self.fn)

        ## set up any transformations and other options
        self.set_transform()
        self.sample_alg = self.sample if self.sample is not None else self.sample_alg
        self.dem_infos = gdalfun.gdal_infos(self.fn)
        if self.x_inc is not None and self.y_inc is not None and self.resample:
            self.resample_and_warp = True
        else:
            self.resample_and_warp = False

        ndv = utils.float_or(gdalfun.gdal_get_ndv(self.fn), -9999)
        if self.region is not None:
            self.warp_region = self.region.copy()
        else:
            self.warp_region = regions.Region().from_list(self.infos['minmax'])
            if self.dst_trans is not None:
                self.warp_region.src_srs = self.src_srs
                self.warp_region.warp(self.dst_srs)

        # if self.region.src_srs != self.src_srs:
        #     #if not regions.regions_within_ogr_p(self.warp_region, self.region) or self.invert_region:
        #     #    self.warp_region = self.region.copy()
        #     #else:
        #     if regions.regions_within_ogr_p(self.warp_region, self.region):
        #         self.warp_region.cut(self.region, self.x_inc, self.y_inc)
                
        #     if self.dst_trans is not None:
        #         self.dst_trans = None

        ## resample/warp src gdal file to specified x/y inc/dst_trans respectively
        if self.resample_and_warp:
            if self.dst_trans is not None:
                self.dst_trans = None

            if self.sample_alg == 'auto':
                if self.dem_infos['geoT'][1] >= self.x_inc and (self.dem_infos['geoT'][5]*-1) >= self.y_inc:
                    self.sample_alg = 'bilinear'
                else:
                    self.sample_alg = 'average'
                
            tmp_ds = self.fn
            if self.open_options is not None:
                self.src_ds = gdal.OpenEx(self.fn, open_options=self.open_options)
            else:
                self.src_ds = gdal.Open(self.fn)

            ## Sample/Warp
            ## resmaple and/or warp dataset based on target srs and x_inc/y_inc
            ## doing this in MEM has a bug, fix if able
            #tmp_warp = os.path.join(self.cache_dir, '_tmp_gdal.tif')
            tmp_warp = utils.make_temp_fn('{}'.format(tmp_ds), temp_dir=self.cache_dir)
            warp_ = gdalfun.sample_warp(tmp_ds, tmp_warp, self.x_inc, self.y_inc,
                                        src_srs=self.src_trans_srs, dst_srs=self.dst_trans_srs,
                                        src_region=self.warp_region, sample_alg=self.sample_alg,
                                        ndv=ndv, verbose=self.verbose)[0] 
            tmp_ds = None
            ## the following seems to be redundant...
            warp_ds = gdal.Open(tmp_warp)
            if warp_ds is not None:
                ## clip wapred ds to warped srcwin
                warp_ds_config = gdalfun.gdal_infos(warp_ds)
                gt = warp_ds_config['geoT']                
                srcwin = self.warp_region.srcwin(gt, warp_ds.RasterXSize, warp_ds.RasterYSize, node='grid')
                #warp_arr = warp_ds.GetRasterBand(1).ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                dst_gt = (gt[0] + (srcwin[0] * gt[1]), gt[1], 0., gt[3] + (srcwin[1] * gt[5]), 0., gt[5])
                out_ds_config = gdalfun.gdal_set_infos(srcwin[2], srcwin[3], srcwin[2] * srcwin[3], dst_gt, warp_ds_config['proj'],
                                                  warp_ds_config['dt'], warp_ds_config['ndv'], warp_ds_config['fmt'], None, None)

                in_bands = warp_ds.RasterCount
                self.src_ds = gdalfun.gdal_mem_ds(out_ds_config, bands=in_bands)
                if self.src_ds is not None:
                    for band in range(1, in_bands+1):
                        this_band = self.src_ds.GetRasterBand(band)
                        this_band.WriteArray(warp_ds.GetRasterBand(band).ReadAsArray(*srcwin))
                        self.src_ds.FlushCache()
                    
                warp_ds = warp_arr = None
                utils.remove_glob(tmp_warp)
        else:
            if self.open_options:
                self.src_ds = gdal.OpenEx(self.fn, open_options=self.open_options)
                if self.src_ds is None:
                    if self.verbose:
                        utils.echo_warning_msg('could not open file using open options {}'.format(self.open_options))
                        
                    self.src_ds = gdal.Open(self.fn)
            else:
                self.src_ds = gdal.Open(self.fn)

        if self.invert_region:
            src_ds_config = gdalfun.gdal_infos(self.src_ds)
            srcwin = self.warp_region.srcwin(src_ds_config['geoT'], src_ds_config['nx'], src_ds_config['ny'], node='grid')
            
            driver = gdal.GetDriverByName('MEM')
            mem_ds = driver.Create('msk', src_ds_config['nx'], src_ds_config['ny'], 1, src_ds_config['dt'])
            mem_ds.SetGeoTransform(src_ds_config['geoT'])
            mem_ds.SetProjection(src_ds_config['proj'])
            mem_band = mem_ds.GetRasterBand(1)
            mem_band.SetNoDataValue(src_ds_config['ndv'])
            mem_arr = np.ones((src_ds_config['ny'], src_ds_config['nx']))
            mem_arr[srcwin[0]:srcwin[0]+srcwin[2],
                    srcwin[1]:srcwin[1]+srcwin[3]] = src_ds_config['ndv']
            mem_band.WriteArray(mem_arr)

            ## self.mask gets set to a gdal ds masking out all data
            ## within the input region, this over-rides the input mask data,
            ## update this so as to not do that...
            self.mask = mem_ds
            
        return(self.src_ds)
        
    def generate_inf(self, check_z=False, callback=lambda: False):
        """generate a infos dictionary from the raster dataset"""
            
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['src_srs'] = self.src_srs if self.src_srs is not None else gdalfun.gdal_get_srs(self.fn)
        self.infos['format'] = self.data_format
        #src_ds = gdal.Open(self.fn)

        if self.open_options is not None:
            src_ds = gdal.OpenEx(self.fn, open_options=self.open_options)
        else:
            src_ds = gdal.Open(self.fn)
        
        if src_ds is not None:
            gt = src_ds.GetGeoTransform()
            this_region = regions.Region(src_srs=self.src_srs).from_geo_transform(
                #geo_transform=self.dem_infos['geoT'],
                #x_count=self.dem_infos['nx'],
                #y_count=self.dem_infos['ny']
                geo_transform=gt,
                x_count=src_ds.RasterXSize,
                y_count=src_ds.RasterYSize
            )
            if check_z:
                try:
                    zr = src_ds.GetRasterBand(utils.int_or(self.band_no, 1)).ComputeRasterMinMax()
                except:
                    zr = [None, None]
            else:
                zr = [None, None]
                
            this_region.zmin, this_region.zmax = zr[0], zr[1]
            self.infos['minmax'] = this_region.export_as_list(include_z=True)
            self.infos['numpts'] = src_ds.RasterXSize * src_ds.RasterYSize
            self.infos['wkt'] = this_region.export_as_wkt()
            src_ds = None
            
        return(self.infos)

    def get_srcwin(self, gt, x_size, y_size, node='grid'):
        if self.region is not None:
            if self.invert_region:
                srcwin = (
                    0, 0, x_size, y_size, node
                )
            else:
                if self.dst_trans is not None:
                    if self.trans_region is not None and self.trans_region.valid_p(
                            check_xy = True
                    ):
                        srcwin = self.trans_region.srcwin(
                            gt, x_size, y_size, node
                        )
                    else:
                        srcwin = (
                            0, 0, x_size, y_size, node
                        )

                else:
                    srcwin = self.region.srcwin(
                        gt, x_size, y_size, node
                    )

        else:
            srcwin = (
                0, 0, x_size, y_size
            )
        return(srcwin)
    
    def yield_array(self):
        """parse the data from gdal dataset src_ds"""

        self.init_ds()
        if self.src_ds is None:
            if self.verbose:
                utils.echo_error_msg('could not load raster file {}'.format(self.fn))
        else:
            count = 0
            band = self.src_ds.GetRasterBand(utils.int_or(self.band_no, 1))
            gt = self.src_ds.GetGeoTransform()
            ndv = utils.float_or(band.GetNoDataValue())
            mask_band = weight_band = uncertainty_band = None
            nodata = ['{:g}'.format(-9999), 'nan', float('nan')]
            if ndv is not None:
                nodata.append('{:g}'.format(ndv))

            band_data = count_data = weight_data = mask_data = None
            out_arrays = {'z':None, 'count':None, 'weight':None, 'uncertainty':None, 'mask':None}

            ## weight mask, each cell should have the corresponding weight
            ## weight_mask can either be a seperate gdal file or a band number
            ## corresponding to the appropriate band in src_ds 
            if self.weight_mask is not None:
                if utils.int_or(self.weight_mask) is not None:
                    weight_band = self.src_ds.GetRasterBand(int(self.weight_mask))
                elif os.path.exists(self.weight_mask): # some numbers now return true here (file-descriptors), check for int first!
                    if self.x_inc is not None and self.y_inc is not None:
                        src_weight = gdalfun.sample_warp(self.weight_mask, None, self.x_inc, self.y_inc,
                                                         src_srs=self.src_trans_srs, dst_srs=self.dst_trans_srs,
                                                         src_region=self.warp_region, sample_alg=self.sample_alg,
                                                         ndv=ndv, verbose=self.verbose)[0]
                    else:
                        src_weight = gdal.Open(self.weight_mask)

                    weight_band = src_weight.GetRasterBand(1)

                else:
                    utils.echo_warning_msg('could not load weight mask {}'.format(self.weight_mask))
                    weight_band = None

            ## uncertainty mask, each cell should have the corresponding uncertainty
            ## uncertainty_mask can either be a seperate gdal file or a band number
            ## corresponding to the appropriate band in src_ds 
            if self.uncertainty_mask is not None:
                if utils.int_or(self.uncertainty_mask):
                    uncertainty_band = self.src_ds.GetRasterBand(int(self.uncertainty_mask))
                elif os.path.exists(self.uncertainty_mask):
                    if self.x_inc is not None and self.y_inc is not None:
                        src_uncertainty = gdalfun.sample_warp(self.uncertainty_mask, None, self.x_inc, self.y_inc,
                                                              src_srs=self.src_trans_srs, dst_srs=self.dst_trans_srs,
                                                              src_region=self.warp_region, sample_alg=self.sample_alg,
                                                              ndv=ndv, verbose=self.verbose)[0]
                    else:
                        src_uncertainty = gdal.Open(self.uncertainty_mask)

                    uncertainty_band = src_uncertainty.GetRasterBand(1)
                else:
                    utils.echo_warning_msg('could not load uncertainty mask {}'.format(self.uncertainty_mask))
                    uncertainty_band = None

            ## mask dataset
            ## don't process masked data
            ## mask can either be a seperate gdal file or a band number
            ## correspinding to the appropriate band in src_ds
            if self.mask is not None:
                if self.invert_region:
                    ## if invert_region is set, self.mask was re-defined to mask
                    ## out the entire input region...
                    mask_band = self.mask.GetRasterBand(1)
                elif os.path.exists(self.mask):
                    if self.x_inc is not None and self.y_inc is not None:
                        src_mask = gdalfun.sample_warp(
                            self.mask, None, self.x_inc, self.y_inc,
                            src_srs=self.src_trans_srs, dst_srs=self.dst_trans_srs,
                            src_region=self.warp_region, sample_alg=self.sample_alg,
                            ndv=ndv, verbose=self.verbose
                        )[0]
                    else:
                        src_mask = gdal.Open(self.mask)
                    
                    mask_band = src_mask.GetRasterBand(1)
                elif utils.int_or(self.mask) is not None:
                    mask_band = self.src_ds.GetRasterBand(self.mask)
                else:
                    utils.echo_warning_msg('could not load mask {}'.format(self.mask))
                    mask_band = None

            ## parse through the data
            srcwin = self.get_srcwin(gt, self.src_ds.RasterXSize, self.src_ds.RasterYSize, node='pixel')
            for y in range(srcwin[1], (srcwin[1] + srcwin[3]), 1):
                band_data = band.ReadAsArray(srcwin[0], y, srcwin[2], 1).astype(float)
                if ndv is not None and not np.isnan(ndv):
                    band_data[band_data == ndv] = np.nan

                if np.all(np.isnan(band_data)):
                    continue
                
                this_origin = utils._pixel2geo(srcwin[0], y, gt, node='pixel')
                this_gt = (this_origin[0], gt[1], 0, this_origin[1], 0, gt[5])
                ## weights
                if weight_band is not None:
                    weight_data = weight_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
                    weight_ndv = float(weight_band.GetNoDataValue())
                    if not np.isnan(weight_ndv):
                        weight_data[weight_data==weight_ndv] = np.nan
                else:
                    weight_data = np.ones(band_data.shape)
                    if self.weight:
                        weight_data[:] = self.weight

                ## uncertainty
                if uncertainty_band is not None:
                    uncertainty_data = uncertainty_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
                    uncertainty_ndv = float(uncertainty_band.GetNoDataValue())
                    if not np.isnan(uncertainty_ndv):
                        uncertainty_data[uncertainty_data==uncertainty_ndv] = np.nan
                else:
                    uncertainty_data = np.zeros(band_data.shape)
                    if self.uncertainty:
                        uncertainty_data[:] = self.uncertainty
                        
                ## mask
                if mask_band is not None:
                    mask_data = mask_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
                    mask_ndv = float(mask_band.GetNoDataValue())
                    if not np.isnan(mask_ndv):
                        mask_data[mask_data==mask_ndv] = np.nan

                    ## mask the band data
                    band_data[np.isnan(mask_data)] = np.nan
                    ## mask the weight
                    if weight_band is not None:                    
                        weight_data[np.isnan(mask_data)] = np.nan
                        
                    ## mask the uncertainty
                    if uncertainty_band is not None:                    
                        uncertainty_data[np.isnan(mask_data)] = np.nan
                    
                if self.region is not None and self.region.valid_p():
                    ## set z-region
                    z_region = self.region.z_region()
                    if self.invert_region:
                        if z_region[0] is not None and z_region[1] is not None:
                            band_data[(band_data > z_region[0]) & (band_data < z_region[1])] = np.nan
                        
                        elif z_region[0] is not None:
                            band_data[band_data > z_region[0]] = np.nan
                        
                        elif z_region[1] is not None:
                            band_data[band_data < z_region[1]] = np.nan
                    else:
                        if z_region[0] is not None:
                            band_data[band_data < z_region[0]] = np.nan
                        
                        if z_region[1] is not None:
                            band_data[band_data > z_region[1]] = np.nan
                            
                    ## set w-region
                    if weight_band is not None:
                        ## set the w-region
                        w_region = self.region.w_region()
                        if self.invert_region:
                            if w_region[0] is not None and w_region[1] is not None:
                                band_data[(band_data > w_region[0]) & (band_data < w_region[1])] = np.nan

                            elif w_region[0] is not None:
                                band_data[band_data > w_region[0]] = np.nan
                        
                            elif w_region[1] is not None:
                                band_data[band_data < w_region[1]] = np.nan
                        else:
                            if w_region[0] is not None:
                                band_data[weight_data < w_region[0]] = np.nan
                        
                            if w_region[1] is not None:
                                band_data[weight_data > w_region[1]] = np.nan

                    ## set u-region
                    if uncertainty_band is not None:
                        ## set the u-region
                        u_region = self.region.u_region()
                        if self.invert_region:
                            if u_region[0] is not None and u_region[1] is not None:
                                band_data[(band_data > u_region[0]) & (band_data < u_region[1])] = np.nan

                            elif u_region[0] is not None:
                                band_data[band_data > u_region[0]] = np.nan
                        
                            elif u_region[1] is not None:
                                band_data[band_data < u_region[1]] = np.nan
                        else:
                            if u_region[0] is not None:
                                band_data[uncertainty_data < u_region[0]] = np.nan
                        
                            if u_region[1] is not None:
                                band_data[uncertainty_data > u_region[1]] = np.nan

                ## set count and out arrays, srcwin and gt
                count_data = np.zeros(band_data.shape)
                count_data[~np.isnan(band_data)] = 1
                count += np.count_nonzero(count_data)
                #count_data[count_data == 0] = np.nan
                out_arrays = {'z':band_data, 'count':count_data, 'weight':weight_data,
                              'mask':mask_data, 'uncertainty':uncertainty_data}
                ds_config = gdalfun.gdal_set_infos(1, y, y, this_gt, self.dst_srs, gdal.GDT_Float32,
                                              np.nan, 'GTiff', None, None)
                this_srcwin = (srcwin[0], y, srcwin[2], 1)
                yield(out_arrays, this_srcwin, this_gt)
                                            
            band = mask_band = weight_band = src_weight = src_mask = self.src_ds = None
            if self.verbose:
                utils.echo_msg(
                    'parsed {} data records from {}{}'.format(
                        count, self.fn, ' @{}'.format(self.weight) if self.weight is not None else ''
                    )
                )
                
    def yield_xyz(self):
        """yield the gdal file data as xyz"""
        
        for arrs, srcwin, gt in self.yield_array():
            z_array = arrs['z']
            w_array = arrs['weight']
            u_array = arrs['uncertainty']
            ycount, xcount = z_array.shape
            for y in range(0, ycount):
                for x in range(0, xcount):
                    z = z_array[y,x]
                    if not np.isnan(z):
                        geo_x, geo_y = utils._pixel2geo(x, y, gt)
                        out_xyz = xyzfun.XYZPoint(
                            x=geo_x, y=geo_y, z=z, w=w_array[y,x], u=u_array[y,x]
                        )
                        if self.dst_trans is not None and not self.resample_and_warp:
                            out_xyz.transform(self.dst_trans)

                        yield(out_xyz)
            
## ==============================================
## BAG Raster File.
## Uses GDAL
## ==============================================
class BAGFile(ElevationDataset):
    """providing a BAG raster dataset parser.

    Process supergrids at native resolution if they
    exist, otherwise process as normal grid.

    generate_inf - generate an inf file for the BAG data
    yield_xyz - yield the BAG data as xyz
    yield_array - yield the BAG data as an array
    
    -----------
    Parameters:

    explode (bool): Explode the BAG and process each super grid seperately.
    force_vr (bool): Force VR processing (if BAG file has bad header info)
    vr_strategy (str): VR strategy to use (MIN/MAX/AUTO)
    """

    def __init__(self, explode = False, force_vr = False, vr_strategy = 'MIN', **kwargs):
        super().__init__(**kwargs)
        self.explode = explode
        self.force_vr = force_vr
        self.vr_strategy = vr_strategy

    def init_ds(self):
        if self.src_srs is None:
            self.src_srs = gdalfun.gdal_get_srs(self.fn)
            self.set_transform()
        
    def generate_inf(self, callback=lambda: False):
        """generate a infos dictionary from the raster dataset"""
            
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['src_srs'] = self.src_srs if self.src_srs is not None else gdalfun.gdal_get_srs(self.fn)
        self.infos['format'] = self.data_format
        src_ds = gdal.Open(self.fn)
        if src_ds is not None:
            gt = src_ds.GetGeoTransform()
            this_region = regions.Region(src_srs=self.src_srs).from_geo_transform(
                geo_transform=gt,
                x_count=src_ds.RasterXSize,
                y_count=src_ds.RasterYSize
            )
            try:
                zr = src_ds.GetRasterBand(1).ComputeRasterMinMax()
            except:
                zr = [None, None]
                
            this_region.zmin, this_region.zmax = zr[0], zr[1]
            self.infos['minmax'] = this_region.export_as_list(include_z=True)
            self.infos['numpts'] = src_ds.RasterXSize * src_ds.RasterYSize
            self.infos['wkt'] = this_region.export_as_wkt()
            src_ds = None
            
        return(self.infos)

    def parse_(self, resample=True):
        mt = gdal.Info(self.fn, format='json')['metadata']['']
        oo = []

        if self.region is not None and self.region.valid_p():
            bag_region = self.region.copy() if self.dst_trans is None else self.trans_region.copy()
            inf_region = regions.Region().from_list(self.infos['minmax'])
            bag_region = regions.regions_reduce(bag_region, inf_region)
            bag_region.src_srs = self.infos['src_srs']
            
            oo.append('MINX={}'.format(bag_region.xmin))
            oo.append('MAXX={}'.format(bag_region.xmax))
            oo.append('MINY={}'.format(bag_region.ymin))
            oo.append('MAXY={}'.format(bag_region.ymax))
        else:
            bag_region = regions.Region().from_list(self.infos['minmax'])
            bag_region.src_srs = self.infos['src_srs']

        if self.dst_trans is not None:
            bag_region.warp(self.dst_srs)
            
        if ('HAS_SUPERGRIDS' in mt.keys() and mt['HAS_SUPERGRIDS'] == 'TRUE') \
           or self.force_vr \
           or 'MAX_RESOLUTION_X' in mt.keys() \
           or 'MAX_RESOLUTION_Y' in mt.keys():
            if self.explode:
                oo.append("MODE=LIST_SUPERGRIDS")
                src_ds = gdal.OpenEx(self.fn, open_options=oo)
                sub_datasets = src_ds.GetSubDatasets()
                src_ds = None

                for sub_dataset in sub_datasets:
                    sub_ds = GDALFile(fn=sub_dataset[0], data_format=200, src_srs=self.src_srs, dst_srs=self.dst_srs,
                                      weight=self.weight, src_region=bag_region, x_inc=self.x_inc, y_inc=self.y_inc,
                                      verbose=self.verbose, resample=resample, check_path=False, super_grid=True)
                    sub_ds.infos = {}
                    sub_ds.generate_inf()
                    yield(sub_ds)

            else:
                oo.append("MODE=RESAMPLED_GRID")
                oo.append("RES_STRATEGY={}".format(self.vr_strategy))
                sub_ds = GDALFile(fn=self.fn, data_format=200, band_no=1, open_options=oo, src_srs=self.src_srs, dst_srs=self.dst_srs,
                                  weight=self.weight, src_region=bag_region, x_inc=self.x_inc, y_inc=self.y_inc,
                                  verbose=self.verbose, resample=resample, uncertainty_mask=2)
                yield(sub_ds)
        else:
            sub_ds = GDALFile(fn=self.fn, data_format=200, band_no=1, src_srs=self.src_srs, dst_srs=self.dst_srs, weight=self.weight,
                              src_region=self.region, x_inc=self.x_inc, y_inc=self.y_inc, verbose=self.verbose,
                              resample=resample, uncertainty_mask=2)
            yield(sub_ds)

    def yield_xyz(self):
        self.init_ds()
        for ds in self.parse_():
            ds.initialize()
            for xyz in ds.yield_xyz():
                yield(xyz)

    def yield_array(self):
        self.init_ds()
        for ds in self.parse_():
            ds.initialize()
            for arr in ds.yield_array():
                yield(arr)
                
## ==============================================
## Multibeam Data.
## Uses MB-System
## ==============================================
class MBSParser(ElevationDataset):
    """providing an mbsystem parser

    Process MB-System supported multibeam data files.
    See MB-System for more information regarding supported
    file formats, etc.
    
    generate_inf - generate an inf file for the MBS data
    yield_xyz - yield the MBS data as xyz
    yield_array - yield the MBS data as an array
 
    -----------
    Parameters:

    mb_fmt=[]
    mb_exclude=[]
    """

    def __init__(self, mb_fmt = None, mb_exclude = 'A', **kwargs):
        super().__init__(**kwargs)
        self.mb_fmt = mb_fmt
        self.mb_exclude = mb_exclude
        #self.infos = {}

    def generate_inf(self, callback=lambda: False):
        self.infos['name'] = self.fn
        self.infos['hash'] = None
        self.infos['format'] = self.data_format
        try:
            utils.run_cmd('mbdatalist -O -V -I{}'.format(self.fn))
            self.inf_parse()
        except: pass
            
        return(self.infos)
            
    def inf_parse(self):
        self.infos['name'] = self.fn
        self.infos['minmax'] = [0,0,0,0,0,0]
        self.infos['hash'] = None
        this_row = 0
        dims = []
        with open('{}.inf'.format(self.fn)) as iob:
            for il in iob:
                til = il.split()
                if len(til) > 1:
                    if til[0] == 'Swath':
                        if til[2] == 'File:':
                            self.infos['name'] = til[3]
                            
                    if til[0] == 'Number':
                        if til[2] == 'Records:':
                            self.infos['numpts'] = utils.int_or(til[3])
                            
                    if til[0] == 'Minimum':
                        if til[1] == 'Longitude:':
                            self.infos['minmax'][0] = utils.float_or(til[2])
                            self.infos['minmax'][1] = utils.float_or(til[5])
                        elif til[1] == 'Latitude:':
                            self.infos['minmax'][2] = utils.float_or(til[2])
                            self.infos['minmax'][3] = utils.float_or(til[5])
                        elif til[1] == 'Depth:':
                            self.infos['minmax'][4] = utils.float_or(til[5]) * -1
                            self.infos['minmax'][5] = utils.float_or(til[2]) * -1
                            
                    if til[0] == 'CM':
                        if til[1] == 'dimensions:':
                            dims = [utils.int_or(til[2]), utils.int_or(til[3])]
                            cm_array = np.zeros((dims[0], dims[1]))
                            
                    if til[0] == 'CM:':
                        for j in range(0, dims[0]):
                            cm_array[this_row][j] = utils.int_or(til[j+1])
                        this_row += 1

        mbs_region = regions.Region().from_list(self.infos['minmax'])
        xinc = (mbs_region.xmax - mbs_region.xmin) / dims[0]
        yinc = (mbs_region.ymin - mbs_region.ymax) / dims[1]
        if abs(xinc) > 0 and abs(yinc) > 0:
            xcount, ycount, dst_gt = mbs_region.geo_transform(
                x_inc=xinc, y_inc=yinc
            )
            ds_config = {'nx': dims[0], 'ny': dims[1], 'nb': dims[1] * dims[0],
                         'geoT': dst_gt, 'proj': gdalfun.osr_wkt(self.src_srs),
                         'dt': gdal.GDT_Float32, 'ndv': 0, 'fmt': 'GTiff'}
            driver = gdal.GetDriverByName('MEM')
            ds = driver.Create(
                'tmp', ds_config['nx'], ds_config['ny'], 1, ds_config['dt']
            )
            ds.SetGeoTransform(ds_config['geoT'])
            if ds_config['proj'] is not None:
                ds.SetProjection(ds_config['proj'])
            ds_band = ds.GetRasterBand(1)
            ds_band.SetNoDataValue(ds_config['ndv'])
            ds_band.WriteArray(cm_array)
            tmp_ds = ogr.GetDriverByName('Memory').CreateDataSource('tmp_poly')
            tmp_layer = tmp_ds.CreateLayer('tmp_poly', None, ogr.wkbMultiPolygon)
            tmp_layer.CreateField(ogr.FieldDefn('DN', ogr.OFTInteger))
            gdal.Polygonize(ds_band, ds_band, tmp_layer, 0)
            multi = ogr.Geometry(ogr.wkbMultiPolygon)
            for feat in tmp_layer:
                feat.geometry().CloseRings()
                wkt = feat.geometry().ExportToWkt()
                multi.AddGeometryDirectly(ogr.CreateGeometryFromWkt(wkt))
            wkt = multi.ExportToWkt()
            tmp_ds = ds = None
        else:
            wkt = mbs_region.export_as_wkt()

        self.infos['wkt'] = wkt
        return(self)

    def parse_(self):
        if self.x_inc is not None and self.y_inc is not None:        
            with open('_mb_grid_tmp.datalist', 'w') as tmp_dl:
                tmp_dl.write('{} {} {}\n'.format(self.fn, self.mb_fmt if self.mb_fmt is not None else '', self.weight if self.mb_fmt is not None else ''))

            ofn = '_'.join(os.path.basename(self.fn).split('.')[:-1])
            mbgrid_region = self.region.copy()
            mbgrid_region = mbgrid_region.buffer(pct=2, x_inc=self.x_inc, y_inc=self.y_inc)
            utils.run_cmd(
                'mbgrid -I_mb_grid_tmp.datalist {} -E{}/{}/degrees! -O{} -A2 -F1 -C10/1 -S0 -T35'.format(
                    mbgrid_region.format('gmt'), self.x_inc, self.y_inc, ofn
                ), verbose=True
            )
            gdalfun.gdal2gdal('{}.grd'.format(ofn))
            utils.remove_glob('_mb_grid_tmp.datalist', '{}.cmd'.format(ofn), '{}.mb-1'.format(ofn), '{}.grd*'.format(ofn))
            mbs_ds = GDALFile(fn='{}.tif'.format(ofn), data_format=200, src_srs=self.src_srs, dst_srs=self.dst_srs,
                              weight=self.weight, x_inc=self.x_inc, y_inc=self.y_inc, sample_alg=self.sample_alg,
                              src_region=self.region, verbose=self.verbose)
            yield(mbs_ds)
            utils.remove_glob('{}.tif*'.format(ofn))
        else:
            yield(None)
            
    def yield_array(self):
        for ds in self.parse_():
            if ds is not None:
                for arr in ds.yield_array():
                    yield(arr)
            else:
                utils.echo_error_msg('could not parse MBS data {}'.format(self.fn))

    def yield_xyz(self):
        for ds in self.parse_():
            if ds is None:
                for line in utils.yield_cmd(
                        'mblist -M{} -OXYZ -I{}'.format(self.mb_exclude, self.fn),
                        verbose=True,
                ):
                    this_xyz = xyzfun.XYZPoint().from_string(line, delim='\t')
                    this_xyz.weight = self.weight
                    yield(this_xyz)
            else:
                for xyz in ds.yield_xyz():
                    yield(xyz)
                
    def yield_array_DEP(self):
        if self.x_inc is not None and self.y_inc is not None:        
            with open('_mb_grid_tmp.datalist', 'w') as tmp_dl:
                tmp_dl.write('{} {} {}\n'.format(self.fn, self.mb_fmt if self.mb_fmt is not None else '', self.weight if self.mb_fmt is not None else ''))

            ofn = '_'.join(os.path.basename(self.fn).split('.')[:-1])

            mbgrid_region = self.region.copy()
            mbgrid_region = mbgrid_region.buffer(pct=2, x_inc=self.x_inc, y_inc=self.y_inc)

            utils.run_cmd(
                'mbgrid -I_mb_grid_tmp.datalist {} -E{}/{}/degrees! -O{} -A2 -F1 -C10/1 -S0 -T35'.format(
                    mbgrid_region.format('gmt'), self.x_inc, self.y_inc, ofn
                ), verbose=True
            )
            gdalfun.gdal2gdal('{}.grd'.format(ofn))
            utils.remove_glob('_mb_grid_tmp.datalist', '{}.cmd'.format(ofn), '{}.mb-1'.format(ofn), '{}.grd*'.format(ofn))
            #gdalfun.set_ndv('{}.tif'.format(ofn), ndv=-99999, convert_array=True, verbose=False)
            xyz_ds = GDALFile(fn='{}.tif'.format(ofn), data_format=200, src_srs=self.src_srs, dst_srs=self.dst_srs,
                              weight=self.weight, x_inc=self.x_inc, y_inc=self.y_inc, sample_alg=self.sample_alg,
                              src_region=self.region, verbose=self.verbose)
            for arr in xyz_ds.yield_array():
                yield(arr)

            utils.remove_glob('{}.tif*'.format(ofn))
            
    def yield_xyz_DEP(self):
        if self.x_inc is not None and self.y_inc is not None:        
            with open('_mb_grid_tmp.datalist', 'w') as tmp_dl:
                tmp_dl.write('{} {} {}\n'.format(self.fn, self.mb_fmt if self.mb_fmt is not None else '', self.weight if self.mb_fmt is not None else ''))

            ofn = '_'.join(os.path.basename(self.fn).split('.')[:-1])

            mbgrid_region = self.region.copy()
            mbgrid_region = mbgrid_region.buffer(pct=2, x_inc=self.x_inc, y_inc=self.y_inc)

            utils.run_cmd(
                'mbgrid -I_mb_grid_tmp.datalist {} -E{}/{}/degrees! -O{} -A2 -F1 -C10/1 -S0 -T35'.format(
                    mbgrid_region.format('gmt'), self.x_inc, self.y_inc, ofn
                ), verbose=True
            )
            gdalfun.gdal2gdal('{}.grd'.format(ofn))
            utils.remove_glob('_mb_grid_tmp.datalist', '{}.cmd'.format(ofn), '{}.mb-1'.format(ofn), '{}.grd*'.format(ofn))
            #gdalfun.set_ndv('{}.tif'.format(ofn), ndv=-99999, convert_array=True, verbose=False)
            xyz_ds = GDALFile(fn='{}.tif'.format(ofn), data_format=200, src_srs=self.src_srs, dst_srs=self.dst_srs,
                              weight=self.weight, x_inc=self.x_inc, y_inc=self.y_inc, sample_alg=self.sample_alg,
                              src_region=self.region, verbose=self.verbose)
            for xyz in xyz_ds.yield_xyz():
                yield(xyz)

            utils.remove_glob('{}.tif*'.format(ofn))
        else:
            for line in utils.yield_cmd(
                    'mblist -M{} -OXYZ -I{}'.format(self.mb_exclude, self.fn),
                    verbose=True,
            ):
                this_xyz = xyzfun.XYZPoint().from_string(line, delim='\t')
                this_xyz.weight = self.weight
                yield(this_xyz)

## ==============================================
## OGR vector data such as S-57 dataset (.000)
## for ENC/E-Hydro
## ==============================================
class OGRFile(ElevationDataset):
    """providing an OGR 3D point dataset parser

    -----------
    Parameters:

    ogr_layer (str/int): the OGR layer containing elevation data
    weight_field (str): the field containing weight values
    uncertainty_field (str): the field containing uncertainty_values
    """

    _known_layer_names = ['SOUNDG', 'Elevation', 'elev', 'z', 'height', 'depth', 'topography']
    
    def __init__(self, ogr_layer = None, elev_field = None, weight_field = None, uncertainty_field = None, **kwargs):
        super().__init__(**kwargs)
        self.ogr_layer = ogr_layer
        self.elev_field = elev_field
        self.weight_field = weight_field
        self.uncertainty_field = uncertainty_field

    def find_elevation_layer(self, ds_ogr):
        for l in self._known_layer_names:
            test_layer = ds_ogr.GetLayerByName(l)
            if test_layer is not None:
                return((l, test_layer))
        return(None, None)
        
    def generate_inf(self, callback=lambda: False):
        """generate a infos dictionary from the ogr dataset"""

        pts = []
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['numpts'] = 0
        self.infos['format'] = self.data_format
        this_region = regions.Region()
        region_ = self.region
        self.region = None

        for i, l in enumerate(self.yield_xyz()):
            if i == 0:
                this_region.from_list([l.x, l.x, l.y, l.y, l.z, l.z])
            else:
                if l.x < this_region.xmin:
                    this_region.xmin = l.x
                elif l.x > this_region.xmax:
                    this_region.xmax = l.x
                if l.y < this_region.ymin:
                    this_region.ymin = l.y
                elif l.y > this_region.ymax:
                    this_region.ymax = l.y
                if l.z < this_region.zmin:
                    this_region.zmin = l.z
                elif l.z > this_region.zmax:
                    this_region.zmax = l.z
            self.infos['numpts'] = i

        self.infos['minmax'] = this_region.export_as_list(include_z = True)
        if self.infos['numpts'] > 0:
            self.infos['wkt'] = this_region.export_as_wkt()
                
        self.region = region_
        self.infos['src_srs'] = self.src_srs if self.src_srs is not None else gdalfun.ogr_get_srs(self.fn)
        
        return(self.infos)

    def yield_xyz(self):
        ds_ogr = ogr.Open(self.fn)
        #print(self.fn)
        if ds_ogr is not None:
            layer_name = None
            if self.ogr_layer is None:
                layer_name, layer_s = self.find_elevation_layer(ds_ogr)
            elif utils.str_or(self.ogr_layer) is not None:
                layer_name = self.ogr_layer
                layer_s = ds_ogr.GetLayerByName(layer_name)
            elif utils.int_or(self.ogr_layer) is not None:
                layer_s = ds_ogr.GetLayer(self.ogr_layer)
            else:
                layer_s = ds_ogr.GetLayer()

            if layer_s is not None:
                if self.region is not None:
                    layer_s.SetSpatialFilter(self.region.export_as_geom())

                for f in layer_s:
                    g = json.loads(f.GetGeometryRef().ExportToJson())
                    for xyz in g['coordinates']:
                        this_xyz = xyzfun.XYZPoint().from_list([float(x) for x in xyz])
                        if layer_name is not None:
                            if layer_name.lower() in ['soundg', 'depth']:
                                this_xyz.z *= -1

                        yield(this_xyz)

            ds_ogr = layer_s = None

    def yield_array(self):        
        for arrs in self.yield_block_array():
            yield(arrs)

class DataList(ElevationDataset):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def generate_inf(self, callback=lambda: False):
        """generate and return the infos blob of the datalist.
        """

        ## set self.region to None while generating
        ## the inf files, otherwise all the inf files
        ## will reflect the input region...
        _region = self.region
        out_region = None
        out_regions = []
        out_srs = []
        self.region = None
        self.infos['name'] = self.fn
        self.infos['numpts'] = 0
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        ## attempt to generate a datalist-vector geojson and
        ## if successful, fill it wil the datalist entries, using `parse`
        #json_status = self._init_datalist_vector()
        #if json_status == 0:        
        dst_srs_ = self.dst_srs
        if self.dst_srs is not None:
            self.dst_srs = self.dst_srs.split('+')[0]

        for entry in self.parse():
            if self.verbose:
                callback()

            if entry.src_srs is not None:
                out_srs.append(entry.src_srs)

                if self.dst_srs is not None:
                    #self.infos['src_srs'] = self.dst_srs
                    e_region = regions.Region().from_list(entry.infos['minmax'])
                    e_region.src_srs = entry.src_srs
                    e_region.warp(self.dst_srs)
                    entry_region = e_region.export_as_list(include_z=True)
                else:
                    entry_region = entry.infos['minmax']

            else:
                out_srs.append(None)
                entry_region = entry.infos['minmax']

            if regions.Region().from_list(entry_region).valid_p():
                out_regions.append(entry_region)
                if 'numpts' in self.infos.keys():
                    self.infos['numpts'] += entry.infos['numpts']
                    
        self.ds = self.layer = None
        count = 0
        ## merge all the gathered regions
        for this_region in out_regions:
            tmp_region = regions.Region().from_list(this_region)
            if tmp_region.valid_p():
                if count == 0:
                    out_region = tmp_region
                    count += 1
                else:
                    out_region = regions.regions_merge(out_region, tmp_region)
                    
        if out_region is not None:
            self.infos['minmax'] = out_region.export_as_list(include_z=True)
            self.infos['wkt'] = out_region.export_as_wkt()
        else:
            self.infos['minmax'] = None
            
        self.region = _region
        self.dst_srs = dst_srs_

        ## set the epsg for the datalist
        if 'src_srs' not in self.infos.keys() or self.infos['src_srs'] is None:
            if self.src_srs is not None:
                self.infos['src_srs'] = self.src_srs
            else:
                if all(x == out_srs[0] for x in out_srs):
                    self.infos['src_srs'] = out_srs[0]
        else:
            self.src_srs = self.infos['src_srs']
        
        return(self.infos)            
        
    def parse(self):
        if isinstance(self.fn, list):
            for this_ds in self.fn:
                this_ds.initialize()

                if this_ds is not None and this_ds.valid_p(
                        fmts=DatasetFactory._modules[this_ds.data_format]['fmts']
                ):
                    this_ds.initialize()
                    for ds in this_ds.parse_json():
                        self.data_entries.append(ds) # fill self.data_entries with each dataset for use outside the yield.
                        yield(ds)

    def yield_xyz(self):
        """parse the data from the data-list and yield as xyz"""
        
        for this_entry in self.parse_json():
            for xyz in this_entry.yield_xyz():
                yield(xyz)

    def yield_array(self):
        """parse the data from the data-list and yield as array-set"""
        
        for this_entry in self.parse_json():
            for arr in this_entry.yield_array():
                yield(arr)
                        
## ==============================================
## Datalist Class - Recursive data structure (-1)
##
## see cudem.datasets for superclass ElevationDataset
## ==============================================
class Datalist(ElevationDataset):
    """representing a datalist parser
    
    A datalist is an extended MB-System style datalist.
    
    Each datalist consists of datalist-entries, where a datalist entry has the following columns:
    path format weight uncertainty title source date data_type resolution hdatum vdatum url

    the datalist can contain datalist-entries to other datalist files, distributed across a
    file-system.
    """

    _datalist_json_cols = ['path', 'format', 'weight', 'uncertainty', 'name', 'title', 'source',
                           'date', 'data_type', 'resolution', 'hdatum', 'vdatum',
                           'url', 'mod_args']

    def __init__(self, fmt=None, **kwargs):
        super().__init__(**kwargs)

    def _init_datalist_vector(self):
        """initialize the datalist geojson vector.

        this vector is used to quickly parse data from distributed files
        across a file-system. Each data file will have it's own entry in
        the geojson datalist vector, containing gathered metadata based on the
        source datalist.
        """
        self.dst_layer = '{}'.format(self.fn)
        self.dst_vector = self.dst_layer + '.json'

        utils.remove_glob('{}.json'.format(self.dst_layer))
        if self.src_srs is not None:
            gdalfun.osr_prj_file('{}.prj'.format(self.dst_layer), self.src_srs)
            
        self.ds = ogr.GetDriverByName('GeoJSON').CreateDataSource(self.dst_vector)
        if self.ds is not None: 
            self.layer = self.ds.CreateLayer(
                '{}'.format(self.dst_layer), None, ogr.wkbMultiPolygon
            )
            [self.layer.CreateField(
                ogr.FieldDefn('{}'.format(f), ogr.OFTString)
            ) for f in self._datalist_json_cols]
            
            [self.layer.SetFeature(feature) for feature in self.layer]
            return(0)
        else:
            self.layer = None
            return(-1)

    def _create_entry_feature(self, entry, entry_region):
        """create a datalist entry feature and insert it into the datalist-vector geojson

        -----------
        Parameters:

        entry - the datalist entry object
        entry_region - the region of the datalist entry
        """
        
        entry_path = os.path.abspath(entry.fn) if not entry.remote else entry.fn
        entry_fields = [entry_path, entry.data_format, entry.weight, entry.uncertainty,
                        entry.metadata['name'], entry.metadata['title'], entry.metadata['source'],
                        entry.metadata['date'], entry.metadata['data_type'], entry.metadata['resolution'],
                        entry.metadata['hdatum'], entry.metadata['vdatum'], entry.metadata['url'],
                        utils.dict2args(entry.params['mod_args'])]
        dst_defn = self.layer.GetLayerDefn()
        entry_geom = ogr.CreateGeometryFromWkt(entry_region.export_as_wkt())
        out_feat = ogr.Feature(dst_defn)
        out_feat.SetGeometry(entry_geom)
        for i, f in enumerate(self._datalist_json_cols):
            out_feat.SetField(f, entry_fields[i])
            
        self.layer.CreateFeature(out_feat)
        
    def generate_inf(self, callback=lambda: False):
        """generate and return the infos blob of the datalist.
        """
        
        _region = self.region
        out_region = None
        out_regions = []
        out_srs = []
        self.region = None
        self.infos['name'] = self.fn
        self.infos['numpts'] = 0
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        ## attempt to generate a datalist-vector geojson and
        ## if successful, fill it wil the datalist entries, using `parse`
        json_status = self._init_datalist_vector()
        if json_status == 0:        
            dst_srs_ = self.dst_srs
            if self.dst_srs is not None:
                self.dst_srs = self.dst_srs.split('+')[0]

            for entry in self.parse():
                if self.verbose:
                    callback()

                if entry.src_srs is not None:
                    out_srs.append(entry.src_srs)

                    if self.dst_srs is not None:
                        #self.infos['src_srs'] = self.dst_srs
                        e_region = regions.Region().from_list(entry.infos['minmax'])
                        e_region.src_srs = entry.src_srs
                        e_region.warp(self.dst_srs)
                        entry_region = e_region.export_as_list(include_z=True)
                    else:
                        entry_region = entry.infos['minmax']

                else:
                    out_srs.append(None)
                    entry_region = entry.infos['minmax']

                if regions.Region().from_list(entry_region).valid_p():
                    self._create_entry_feature(entry, regions.Region().from_list(entry_region))
                    out_regions.append(entry_region)
                    if 'numpts' in self.infos.keys():
                        self.infos['numpts'] += entry.infos['numpts']
        else:
            return(None)
        
        self.ds = self.layer = None
        count = 0
        ## merge all the gathered regions
        for this_region in out_regions:
            tmp_region = regions.Region().from_list(this_region)
            if tmp_region.valid_p():
                if count == 0:
                    out_region = tmp_region
                    count += 1
                else:
                    out_region = regions.regions_merge(out_region, tmp_region)
                    
        if out_region is not None:
            self.infos['minmax'] = out_region.export_as_list(include_z=True)
            self.infos['wkt'] = out_region.export_as_wkt()
        else:
            self.infos['minmax'] = None
            
        self.region = _region
        self.dst_srs = dst_srs_

        ## set the epsg for the datalist
        if 'src_srs' not in self.infos.keys() or self.infos['src_srs'] is None:
            if self.src_srs is not None:
                self.infos['src_srs'] = self.src_srs
            else:
                if all(x == out_srs[0] for x in out_srs):
                    self.infos['src_srs'] = out_srs[0]
        else:
            self.src_srs = self.infos['src_srs']
        
        return(self.infos)

    def parse_json(self):
        """parse the datalist using the datalist-vector geojson.

        Quickly find data in a given region using the datalist-vector. The datalist-vector
        must have been previously generated using `parse`. If the datlist-vector is not found
        will fall-back to `parse` and generate a new datalist-vector geojson.

        -------
        Yields:
        dataset object of each dataset found in the datalist-vector
        """

        ## check for the datalist-vector geojson
        status = count = 0
        if os.path.exists('{}.json'.format(self.fn)):
            driver = ogr.GetDriverByName('GeoJSON')
            dl_ds = driver.Open('{}.json'.format(self.fn))
            if dl_ds is None:
                utils.echo_error_msg('could not open {}.json'.format(self.fn))
                status = -1
        else:
            status = -1

        ## parse the datalist-vector geojson and yield the results
        if status != -1:
            dl_layer = dl_ds.GetLayer()
            ldefn = dl_layer.GetLayerDefn()
            _boundsGeom = None
            if self.region is not None:
                _boundsGeom = self.region.export_as_geom() if self.dst_trans is None else self.trans_region.export_as_geom()

            dl_layer.SetSpatialFilter(_boundsGeom)
            count = len(dl_layer)
            # with utils.CliProgress(
            #         message='parsing {} datasets from datalist json {} @ {}'.format(count, self.fn, self.weight),
            #         total=len(dl_layer),
            #         verbose=self.verbose,
            #         sleep=10,
            # ) as pbar:
            for l,feat in enumerate(dl_layer):
                #pbar.update()
                ## filter by input source region extras (weight/uncertainty)
                if self.region is not None:
                    w_region = self.region.w_region()
                    if w_region[0] is not None:
                        if float(feat.GetField('weight')) < w_region[0]:
                            continue
                    if w_region[1] is not None:
                        if float(feat.GetField('weight')) > w_region[1]:
                            continue

                    u_region = self.region.u_region()
                    if u_region[0] is not None:
                        if float(feat.GetField('uncertainty')) < u_region[0]:
                            continue
                    if u_region[1] is not None:
                        if float(feat.GetField('uncertainty')) > u_region[1]:
                            continue

                ## extract the module arguments from the datalist-vector
                try:
                    ds_args = feat.GetField('mod_args')
                    data_set_args = utils.args2dict(list(ds_args.split(':')), {})
                except:
                    data_set_args = {}

                ## update existing metadata
                md = copy.deepcopy(self.metadata)
                for key in self.metadata.keys():
                    md[key] = feat.GetField(key)

                ## generate the dataset object to yield
                data_set = DatasetFactory(mod = '{} {} {} {}'.format(feat.GetField('path'), feat.GetField('format'),
                                                                     feat.GetField('weight'), feat.GetField('uncertainty')),
                                          weight=self.weight, uncertainty=self.uncertainty, parent=self, src_region=self.region,
                                          invert_region=self.invert_region, metadata=md, src_srs=self.src_srs, dst_srs=self.dst_srs,
                                          x_inc=self.x_inc, y_inc=self.y_inc, sample_alg=self.sample_alg, cache_dir=self.cache_dir,
                                          verbose=self.verbose, **data_set_args)._acquire_module()
                if data_set is not None and data_set.valid_p(
                        fmts=DatasetFactory._modules[data_set.data_format]['fmts']
                ):
                    data_set.initialize()
                    for ds in data_set.parse_json():
                        self.data_entries.append(ds) # fill self.data_entries with each dataset for use outside the yield.
                        yield(ds)

            dl_ds = dl_layer = None
                
        else:
            ## failed to find/open the datalist-vector geojson, so run `parse` instead and
            ## generate one for future use...
            #status = -1
            utils.echo_warning_msg('could not load datalist-vector json, falling back to parse')
            for ds in self.parse():
                yield(ds)
                                        
    def parse(self):
        """parse the datalist file.

        -------
        Yields:
        dataset object of each dataset found in the datalist        
        """

        status = 0
        if os.path.exists(self.fn):
            with open(self.fn, 'r') as f:
                count = sum(1 for _ in f)

            with open(self.fn, 'r') as op:
                # with utils.CliProgress(
                #         message='{}: parsing datalist {}'.format(utils._command_name, self.fn),
                # ) as pbar:
                for l, this_line in enumerate(op):
                    #pbar.update()
                    ## parse the datalist entry line
                    if this_line[0] != '#' and this_line[0] != '\n' and this_line[0].rstrip() != '':
                        md = copy.deepcopy(self.metadata)
                        md['name'] = utils.fn_basename2(os.path.basename(self.fn))
                        ## generate the dataset object to yield
                        data_set = DatasetFactory(mod=this_line, weight=self.weight, uncertainty=self.uncertainty, parent=self,
                                                  src_region=self.region, invert_region=self.invert_region, metadata=md,
                                                  src_srs=self.src_srs, dst_srs=self.dst_srs, x_inc=self.x_inc, y_inc=self.y_inc,
                                                  sample_alg=self.sample_alg, cache_dir=self.cache_dir, verbose=self.verbose)._acquire_module()

                        if data_set is not None and data_set.valid_p(
                                fmts=DatasetFactory._modules[data_set.data_format]['fmts']
                        ):
                            data_set.initialize()
                            # if data_set.data_format < -10:
                            #     print(data_set)
                            #     yield(data_set)
                            
                            ## filter with input source region, if necessary
                            ## check input source region against the dataset region found
                            ## in its inf file.
                            if self.region is not None and self.region.valid_p(check_xy=True):
                                try:
                                    inf_region = regions.Region().from_list(
                                        data_set.infos['minmax']
                                    )
                                except:
                                    inf_region = self.region.copy()

                                ## check this!
                                inf_region.wmin = data_set.weight
                                inf_region.wmax = data_set.weight
                                inf_region.umin = data_set.uncertainty
                                inf_region.umax = data_set.uncertainty

                                if regions.regions_intersect_p(
                                        inf_region,
                                        self.region if data_set.dst_trans is None else data_set.trans_region
                                ):
                                    for ds in data_set.parse():
                                        self.data_entries.append(ds) # fill self.data_entries with each dataset for use outside the yield.
                                        yield(ds)
                            else:
                                for ds in data_set.parse():
                                    self.data_entries.append(ds) # fill self.data_entries with each dataset for use outside the yield.
                                    yield(ds)

        ## self.fn is not a file-name, so check if self.data_entries not empty
        ## and return the dataset objects found there.
        elif len(self.data_entries) > 0:
            for data_set in self.data_entries:
                for ds in data_set.parse():
                    yield(ds)
        else:
            if self.verbose:
                utils.echo_warning_msg(
                    'could not open datalist/entry {}'.format(self.fn)
                )
            
    def yield_xyz(self):
        """parse the data from the datalist and yield as xyz"""
        
        for this_entry in self.parse_json():
            for xyz in this_entry.yield_xyz():
                yield(xyz)
                
            if this_entry.remote:
                utils.remove_glob('{}*'.format(this_entry.fn))

    def yield_array(self):
        """parse the data from the datalist and yield as array-set"""
        
        for this_entry in self.parse_json():
            for arr in this_entry.yield_array():
                yield(arr)

            if this_entry.remote:
                utils.remove_glob('{}*'.format(this_entry.fn))
                
## ==============================================
## ZIPlist Class - Recursive data structure - testing
## ==============================================
class ZIPlist(ElevationDataset):
    """Zip file parser.

    Parse supported datasets from a zipfile.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
    def generate_inf(self, callback = lambda: False):
        """return the region of the datalist and generate
        an associated `.inf` file if `inf_file` is True.
        """
        
        _region = self.region
        out_region = None
        out_regions = []
        self.region = None
        self.infos['name'] = self.fn
        self.infos['numpts'] = 0
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['format'] = self.data_format
        
        for entry in self.parse():
            if self.verbose:
                callback()

            out_regions.append(entry.infos['minmax'])
            if 'numpts' in self.infos.keys():
                self.infos['numpts'] += entry.infos['numpts']

        count = 0
        for this_region in out_regions:
            tmp_region = regions.Region().from_list(this_region)
            if tmp_region.valid_p():
                if count == 0:
                    out_region = tmp_region
                    count += 1
                else:
                    out_region = regions.regions_merge(out_region, tmp_region)
                    
        if out_region is not None:
            self.infos['minmax'] = out_region.export_as_list(include_z=True)
            self.infos['wkt'] = out_region.export_as_wkt()
        else:
            self.infos['minmax'] = None

        self.region = _region
        self.infos['src_srs'] = self.src_srs
        
        return(self.infos)
        
    def parse(self):
        import zipfile
        exts = [DatasetFactory()._modules[x]['fmts'] for x in DatasetFactory()._modules.keys()]
        exts = [x for y in exts for x in y]
        datalist = []
        if self.fn.split('.')[-1].lower() == 'zip':
            with zipfile.ZipFile(self.fn) as z:
                zfs = z.namelist()
                for ext in exts:
                    for zf in zfs:
                        if ext == zf.split('.')[-1]:
                            datalist.append(os.path.basename(zf))
                            
        for this_data in datalist:
            this_line = utils.p_f_unzip(self.fn, [this_data])[0]
            data_set = DatasetFactory(mod=this_line, data_format=None, weight=self.weight, uncertainty=self.uncertainty, parent=self,
                                      src_region=self.region, invert_region=self.invert_region, x_inc=self.x_inc,
                                      y_inc=self.y_inc, metadata=copy.deepcopy(self.metadata), src_srs=self.src_srs,
                                      dst_srs=self.dst_srs, cache_dir=self.cache_dir, verbose=self.verbose)._acquire_module()
            if data_set is not None and data_set.valid_p(
                    fmts=DatasetFactory._modules[data_set.data_format]['fmts']
            ):
                data_set.initialize()
                if self.region is not None and self.region.valid_p(check_xy=True):
                    try:
                        inf_region = regions.Region().from_string(
                            data_set.infos['wkt']
                        )
                    except:
                        try:
                            inf_region = regions.Region().from_list(
                                data_set.infos['minmax']
                            )
                        except:
                            inf_region = self.region.copy()
                            
                    inf_region.wmin = data_set.weight
                    inf_region.wmax = data_set.weight
                    inf_region.umin = data_set.uncertainty
                    inf_region.umax = data_set.uncertainty
                    if regions.regions_intersect_p(inf_region, self.region):
                        for ds in data_set.parse():
                            self.data_entries.append(ds)
                            yield(ds)
                else:
                    for ds in data_set.parse():
                        self.data_entries.append(ds)
                        yield(ds)
            
            utils.remove_glob('{}*'.format(this_data))

    def yield_xyz(self):
        for ds in self.parse():
            for xyz in ds.yield_xyz():
                yield(xyz)

    def yield_array(self):
        for ds in self.parse():
            for arr in ds.yield_array():
                yield(arr)
        
## ==============================================
##
## dlim Fetcher dataset class
##
## see cudem.fetches for various fetches supported
## datasets
##
## If a fetch module needs special processing define a sub-class
## of Fetcher and redefine the set_ds(self, result) function which returns a
## list of dlim dataset objects, where result is an item from the fetch result list.
## Otherwise, this Fetcher class can be used as default if the fetched data comes
## in a normal sort of way.
##
## Generally, though not always, if the fetched data is a raster then
## there is no need to redefine set_ds, though if the raster has insufficient
## information, such as with Copernicus, whose nodata value is not
## specified in the geotiff files, it may be best to create a simple
## sub-class for it.
##
## ==============================================
class Fetcher(ElevationDataset):
    """The generic fetches dataset type.

    This is used in waffles/dlim for on-the-fly remote data
    parsing and processing.
    
    See `fetches` for more information.
"""
    
    def __init__(self, **kwargs):
        super().__init__(remote=True, **kwargs)
        #print(self.fn)
        self.metadata['name'] = self.fn
        self.fetch_module = fetches.FetchesFactory(
            mod=self.fn, src_region=self.region, verbose=self.verbose, outdir=self.cache_dir
        )._acquire_module()
        if self.fetch_module is None:
            pass
        self.check_size=True
        
    def generate_inf(self, callback=lambda: False):
        """generate a infos dictionary from the Fetches dataset"""

        self.infos['name'] = self.fn
        self.infos['hash'] = None
        self.infos['numpts'] = 0
        if self.region is None:
            tmp_region = self.fetch_module.region
        else:
            tmp_region = self.region.copy()
            
        self.infos['minmax'] = tmp_region.export_as_list()    
        self.infos['wkt'] = tmp_region.export_as_wkt()
        self.infos['format'] = self.data_format
        self.infos['src_srs'] = self.fetch_module.src_srs
        #print(self.infos)
        return(self.infos)

    def parse(self):
        if self.region is not None:
            self.fetch_module.run()
            for result in self.fetch_module.results:
                if self.fetch_module.fetch(result, check_size=self.check_size) == 0:
                    for this_ds in self.yield_ds(result):
                        this_ds.initialize()
                        yield(this_ds)
        else:
            yield(self)
    
    def yield_ds(self, result):
        yield(DatasetFactory(mod=result[1], data_format=self.fetch_module.data_format, weight=self.weight, parent=self, src_region=self.region,
                             invert_region=self.invert_region, metadata=copy.deepcopy(self.metadata), uncertainty=self.uncertainty,
                             src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs, verbose=self.verbose,
                             cache_dir=self.fetch_module._outdir, remote=True)._acquire_module())

    def yield_xyz(self):
        for ds in self.parse():
            for xyz in ds.yield_xyz():
                yield(xyz)

    def yield_array(self):
        for ds in self.parse():
            for arr in ds.yield_array():
                yield(arr)

class GMRTFetcher(Fetcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        #self.__doc__ = self.fetch_module.__doc__

    def set_ds(self, result):
        with gdalfun.gdal_datasource(result[1], update = 1) as src_ds:
            md = src_ds.GetMetadata()
            md['AREA_OR_POINT'] = 'Point'
            src_ds.SetMetadata(md)
                        
        yield(DatasetFactory(fn=result[1], data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                             x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                             parent=self, invert_region = self.invert_region, metadata=copy.deepcopy(self.metadata),
                             cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())
        
class GEBCOFetcher(Fetcher):
    def __init__(self, exclude_tid = None, **kwargs):
        super().__init__(**kwargs)
        self.exclude_tid = []
        if utils.str_or(exclude_tid) is not None:
            for tid_key in exclude_tid.split('/'):
                self.exclude_tid.append(utils.int_or(tid_key))
        
    def yield_ds(self, result):
        wanted_gebco_fns = []
        gebco_fns = utils.p_unzip(result[1], ['tif'], self.fetch_module._outdir)
        
        ## fetch the TID zip if needed
        if self.exclude_tid:
            if fetches.Fetch(
                    self.fetch_module._gebco_urls['gebco_tid']['geotiff'], verbose=self.verbose
            ).fetch_file(os.path.join(self.fetch_module._outdir, 'gebco_tid.zip')) == 0:
                tid_fns = utils.p_unzip(os.path.join(self.fetch_module._outdir, 'gebco_tid.zip'), ['tif'], self.cache_dir)

                for tid_fn in tid_fns:
                    ds_config = gdalfun.gdal_infos(tid_fn)
                    
                    if self.region is not None and self.region.valid_p(check_xy=True):
                        inf_region = regions.Region().from_geo_transform(ds_config['geoT'], ds_config['nx'], ds_config['ny'])            
                        inf_region.wmin = self.weight
                        inf_region.wmax = self.weight
                        inf_region.umin = self.uncertainty
                        inf_region.umax = self.uncertainty

                        if regions.regions_intersect_p(inf_region, self.region):
                            wanted_gebco_fns.append(tid_fn)
                    else:
                        wanted_gebco_fns.append(tid_fn)

                for tid_fn in wanted_gebco_fns:
                    tmp_tid = utils.make_temp_fn('tmp_tid.tif', temp_dir=self.fetch_module._outdir)
                    with gdalfun.gdal_datasource(tid_fn) as tid_ds:
                        tid_config = gdalfun.gdal_infos(tid_ds)
                        tid_band = tid_ds.GetRasterBand(1)
                        tid_array = tid_band.ReadAsArray().astype(float)

                    tid_config['ndv'] = -9999
                    tid_config['dt'] = gdal.GDT_Float32                        
                    for tid_key in self.exclude_tid:
                        tid_array[tid_array == tid_key] = tid_config['ndv']
                        
                    for tid_key in self.fetch_module.tid_dic.keys():
                        tid_array[tid_array == tid_key] = self.fetch_module.tid_dic[tid_key][1]
                            
                    gdalfun.gdal_write(tid_array, tmp_tid, tid_config)

                    yield(DatasetFactory(mod=tid_fn.replace('tid_', ''), data_format='200:mask={tmp_tid}:weight_mask={tmp_tid}'.format(tmp_tid=tmp_tid), src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                                         x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                         parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                         cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())
                    
                    utils.remove_glob(tmp_tid)
        else:
            for gebco_fn in gebco_fns:
                ds_config = gdalfun.gdal_infos(gebco_fn)
                inf_region = regions.Region().from_geo_transform(ds_config['geoT'], ds_config['nx'], ds_config['ny'])
                if self.region is not None and self.region.valid_p(check_xy=True):                
                    inf_region.wmin = self.weight
                    inf_region.wmax = self.weight
                    inf_region.umin = self.uncertainty
                    inf_region.umax = self.uncertainty

                    if regions.regions_intersect_p(inf_region, self.region):
                        wanted_gebco_fns.append(gebco_fn)
                else:
                    wanted_gebco_fns.append(gebco_fn)    
            
            for gebco_fn in wanted_gebco_fns:
                yield(DatasetFactory(mod=gebco_fn, data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                                     x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, src_region=self.region,
                                     parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                     cache_dir=self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

    
class CopernicusFetcher(Fetcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.check_size=False
        
    def yield_ds(self, result):
        src_cop_dems = utils.p_unzip(result[1], ['tif'], outdir=self.fetch_module._outdir)
        for src_cop_dem in src_cop_dems:
            gdalfun.gdal_set_ndv(src_cop_dem, ndv=0, verbose=False)
            yield(DatasetFactory(mod=src_cop_dem, data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                                 x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                 parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                 cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

class FABDEMFetcher(Fetcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def yield_ds(self, result):
        src_fab_dems = utils.p_unzip(result[1], ['tif'], outdir=self.fetch_module._outdir)
        for src_fab_dem in src_fab_dems:
            gdalfun.gdal_set_ndv(src_fab_dem, ndv=0, verbose=False)
            yield(DatasetFactory(mod=src_fab_dem, data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                                 x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                 parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                 cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

class MarGravFetcher(Fetcher):
    def __init__(self, rasterize=False, bathy_only=False, upper_limit=None, lower_limit=None, **kwargs):
        super().__init__(**kwargs)
        self.rasterize = rasterize
        self.bathy_only = bathy_only
        self.upper_limit = utils.float_or(upper_limit)
        self.lower_limit = utils.float_or(lower_limit)

        if self.bathy_only:
            self.upper_limit = 0
        
    def yield_ds(self, result):
        mg_region = self.region.copy()
        mg_region.zmax = self.upper_limit
        mg_region.zmin = self.lower_limit
            
        if self.rasterize:
            from cudem import waffles
            mar_grav_fn = utils.make_temp_fn('mar_grav')
            # mar_grav_fn = os.path.join(
            #     self.cache_dir,
            #     utils.append_fn(
            #         'mar_grav', self.region, self.x_inc
            #     )
            # )
            _raster = waffles.WaffleFactory(mod='IDW:min_points=16', data=['{},168:x_offset=REM,1'.format(result[1])],
                                            src_region=mg_region, xinc=utils.str2inc('30s'), yinc=utils.str2inc('30s'),
                                            name=mar_grav_fn, node='pixel', verbose=True)._acquire_module()()
            yield(DatasetFactory(mod=_raster.fn, data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                                 x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=mg_region,
                                 parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                 cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())
        else:
            yield(DatasetFactory(mod=result[1], data_format='168:x_offset=REM', src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                                 x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=mg_region,
                                 parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                 cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

            
class HydroNOSFetcher(Fetcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def yield_ds(self, result):
        if result[2] == 'xyz':
            nos_fns = utils.p_unzip(result[1], exts=['xyz', 'dat'], outdir=self.fetch_module._outdir)
            for nos_fn in nos_fns:
                yield(DatasetFactory(mod=nos_fn, data_format='168:skip=1:xpos=2:ypos=1:zpos=3:z_scale=-1', src_srs='epsg:4326+5866', dst_srs=self.dst_srs,
                                     x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                     parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                     cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())
        elif result[2] == 'bag':
            bag_fns = utils.p_unzip(result[1], exts=['bag'], outdir=self.fetch_module._outdir)
            for bag_fn in bag_fns:
                if 'ellipsoid' not in bag_fn.lower() and 'vb' not in bag_fn.lower():
                    # with gdalfun.gdal_datasource(bag_fn) as bag_ds:
                    #     bag_srs = bag_ds.GetSpatialRef()
                    #     bag_srs.AutoIdentifyEPSG()
                    #     bag_srs.SetAuthority('VERT_CS', 'EPSG', 5866)
                    
                    yield(DatasetFactory(mod=bag_fn, data_format=201, src_srs=None, dst_srs=self.dst_srs,
                                         x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                         parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                         cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

class eHydroFetcher(Fetcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def find_src_region(self, result):
        """attempt to grab the src_region from the xml metadata"""
        
        src_region = None
        src_xmls = utils.p_unzip(result[1], ['xml', 'XML'], outdir=self.fetch_module._outdir)
        for src_xml in src_xmls:
            if src_region is None:
                this_xml = lxml.etree.parse(src_xml)
                if this_xml is not None:
                    try:
                        w = this_xml.find('.//westbc').text
                        e = this_xml.find('.//eastbc').text
                        n = this_xml.find('.//northbc').text
                        s = this_xml.find('.//southbc').text
                        src_region = regions.Region().from_list(
                            [float(w), float(e), float(s), float(n)]
                        )
                    except:
                        utils.echo_warning_msg('could not parse region')
                        src_region = self.region

            utils.remove_glob(src_xml)
        return(src_region)
        
    def find_epsg(self, src_region):
        """search the stateplane vector for a match
        keep buffering the src region until a match is found...
        """
        
        tmp_region = src_region.copy()
        utils.echo_msg(tmp_region)
        sp_fn = os.path.join(FRED.fetchdata, 'stateplane.geojson')
        sp = ogr.Open(sp_fn)
        layer = sp.GetLayer()
        this_geom = tmp_region.export_as_geom()
        layer.SetSpatialFilter(this_geom)
        cnt = 0
        while len(layer) == 0:
            cnt+=1
            tmp_region.buffer(pct=10)
            utils.echo_msg('buffering region ({}): {}'.format(cnt, tmp_region))
            this_geom = tmp_region.export_as_geom()
            layer.SetSpatialFilter(None)
            layer.SetSpatialFilter(this_geom)

        for feature in layer:
            src_epsg = feature.GetField('EPSG')
            break

        utils.echo_msg('ehydro epsg is: {}'.format(src_epsg))
        sp = None
        return(src_epsg)
        
    def yield_ds(self, result):
        src_region = self.find_src_region(result)
        if src_region is not None and src_region.valid_p():
            src_epsg = self.find_epsg(src_region)
            src_usaces = utils.p_unzip(result[1], ['XYZ', 'xyz', 'dat'], outdir=self.fetch_module._outdir)
            for src_usace in src_usaces:
                # usace_ds = XYZFile(fn=src_usace, data_format=168, x_scale=.3048, y_scale=.3048,
                #                    src_srs='epsg:{}+5866'.format(src_epsg) if src_epsg is not None else None, dst_srs=self.dst_srs,
                #                    x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                #                    parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                #                    cache_dir = self.fetch_module._outdir, verbose=self.verbose)
                usace_ds = DatasetFactory(mod=src_usace, data_format='168:x_scale=.3048:y_scale=.3048',
                                          src_srs='epsg:{}+5866'.format(src_epsg) if src_epsg is not None else None, dst_srs=self.dst_srs,
                                          x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                          parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                          cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module()

                
                ## check for median z value to see if they are using depth or height
                ## most data should be negative...
                usace_ds.initialize()
                usace_xyz = np.array(usace_ds.export_xyz_as_list(z_only=True))
                z_med = np.percentile(usace_xyz, 50)
                z_scale = .3048 if z_med < 0 else -.3048
                usace_ds = DatasetFactory(mod=src_usace, data_format='168:x_scale=.3048:y_scale=.3048:z_scale={}'.format(z_scale),
                                          src_srs='epsg:{}+5866'.format(src_epsg) if src_epsg is not None else None, dst_srs=self.dst_srs,
                                          x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                                          parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                                          cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module()
                
                yield(usace_ds)

class BlueTopoFetcher(Fetcher):
    def __init__(self, want_interpolation=False, **kwargs):
        super().__init__(**kwargs)
        self.want_interpolation = want_interpolation

    def yield_ds(self, result):
        sid = None
        if not self.want_interpolation:
            sid = gdalfun.gdal_extract_band(result[1], os.path.join(self.fetch_module._outdir, '_tmp_bt_tid.tif'), band=3, exclude=[0])[0]
        
        yield(DatasetFactory(mod=result[1], data_format='200:band_no=1:mask={}:uncertainty_mask=2'.format(sid), src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                             x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                             parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                             cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())        

class NGSFetcher(Fetcher):
    def __init__(self, datum = 'geoidHt', **kwargs):
        super().__init__(**kwargs)
        self.datum = datum
        if self.datum not in ['orthoHt', 'geoidHt', 'z', 'ellipHeight']:
            utils.echo_warning_msg('could not parse {}, falling back to geoidHt'.format(datum))
            self.datum = 'geoidHt'

    def yield_ds(self, result):
        with open(result[1], 'r') as json_file:
            r = json.load(json_file)
            if len(r) > 0:
                with open(os.path.join(self.fetch_module._outdir, '_tmp_ngs.xyz'), 'w') as tmp_ngs:
                    for row in r:
                        z = utils.float_or(row[self.datum])
                        if z is not None:
                            xyz = xyzfun.XYZPoint(src_srs='epsg:4326').from_list(
                                [float(row['lon']), float(row['lat']), z]
                            )
                            xyz.dump(dst_port=tmp_ngs)

        yield(DatasetFactory(mod=os.path.join(self.fetch_module._outdir, '_tmp_ngs.xyz'), data_format=168, src_srs='epsg:4326', dst_srs=self.dst_srs,
                             x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                             parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                             cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

class TidesFetcher(Fetcher):
    def __init__(self, s_datum='mllw', t_datum='msl', units='m', **kwargs):
        super().__init__(**kwargs)
        self.s_datum = s_datum
        self.t_datum = t_datum
        self.units = units
        
    def yield_ds(self, result):
        with open(result[1], 'r') as json_file:
            r = json.load(json_file)
            if len(r) > 0:
                with open(os.path.join(self.fetch_module._outdir, '_tmp_tides.xyz'), 'w') as tmp_ngs:
                    for feature in r['features']:
                        if self.fetch_module.station_id is not None:
                            if self.fetch_module.station_id != feature['attributes']['id']:
                                continue
                        lon = feature['attributes']['longitude']
                        lat = feature['attributes']['latitude']
                        if feature['attributes'][self.s_datum] != -99999.99 and feature['attributes'][self.t_datum] != -99999.99:
                            z = feature['attributes'][self.s_datum] - feature['attributes'][self.t_datum]
                            if self.units == 'm':
                                z = z * 0.3048

                            xyz = xyzfun.XYZPoint(src_srs='epsg:4326').from_list([lon, lat, z])
                            xyz.dump(dst_port=tmp_ngs)

        yield(DatasetFactory(mod=os.path.join(self.fetch_module._outdir, '_tmp_tides.xyz'), data_format=168, src_srs='epsg:4326', dst_srs=self.dst_srs,
                             x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                             parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                             cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())

# class CopernicusFetcher(Fetcher):
#     def __init__(self, **kwargs):
#         super().__init__(**kwargs)

#     def yield_ds(self, result):
#         out_ds = []
#         src_cop_dems = utils.p_unzip(result[1], ['tif'], outdir=self.fetch_module._outdir)
#         for src_cop_dem in src_cop_dems:
#             gdalfun.gdal_set_ndv(src_cop_dem, ndv=0, verbose=False)
#             out_ds.append(GDALFile(fn=src_cop_dem, data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
#                                    x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
#                                    parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
#                                    cache_dir = self.fetch_module._outdir, verbose=self.verbose))
#         return(out_ds)

class VDatumFetcher(Fetcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def yield_ds(self, result):
        v_gtx = utils.p_f_unzip(result[1], [result[2]], outdir=self.fetch_module._outdir)
        src_tif = os.path.join(self.fetch_module._outdir, '{}'.format(utils.fn_basename2(os.path.basename(result[1]))))
        utils.run_cmd('gdalwarp {} {} --config CENTER_LONG 0'.format(v_gtx[0], src_tif), verbose=True)
        yield(DatasetFactory(mod=src_tif, data_format=200, src_srs=self.fetch_module.src_srs, dst_srs=self.dst_srs,
                             x_inc=self.x_inc, y_inc=self.y_inc, weight=self.weight, uncertainty=self.uncertainty, src_region=self.region,
                             parent=self, invert_region = self.invert_region, metadata = copy.deepcopy(self.metadata),
                             cache_dir = self.fetch_module._outdir, verbose=self.verbose)._acquire_module())        
    
## ==============================================
## Dataset generator
##
## Parse a datalist entry and return the dataset
## object
## ==============================================
class DatasetFactory(factory.CUDEMFactory):
    
    _modules = {
        -1: {'name': 'datalist', 'fmts': ['datalist', 'mb-1', 'dl'], 'call': Datalist},
        -2: {'name': 'zip', 'fmts': ['zip'], 'call': ZIPlist},
        -3: {'name': 'data_list', 'fmts': [''], 'call': DataList},
        168: {'name': 'xyz', 'fmts': ['xyz', 'csv', 'dat', 'ascii', 'txt'], 'call': XYZFile},
        200: {'name': 'gdal', 'fmts': ['tif', 'tiff', 'img', 'grd', 'nc', 'vrt'], 'call': GDALFile},
        201: {'name': 'bag', 'fmts': ['bag'], 'call': BAGFile},
        300: {'name': 'las', 'fmts': ['las', 'laz'], 'call': LASFile},
        301: {'name': 'mbs', 'fmts': ['fbt'], 'call': MBSParser},
        302: {'name': 'ogr', 'fmts': ['000', 'shp', 'geojson', 'gpkg'], 'call': OGRFile},
        ## fetches modules
        -100: {'name': 'gmrt', 'fmts': ['gmrt'], 'call': GMRTFetcher},
        -101: {'name': 'gebco', 'fmts': ['gebco'], 'call': GEBCOFetcher},
        -102: {'name': 'copernicus', 'fmts': ['copernicus'], 'call': CopernicusFetcher},
        -103: {'name': 'fabdem', 'fmts': ['fabdem'], 'call': FABDEMFetcher},
        -104: {'name': 'nasadem', 'fmts': ['nasadem'], 'call': Fetcher},
        -105: {'name': 'mar_grav', 'fmts': ['mar_grav'], 'call': MarGravFetcher},
        -106: {'name': 'srtm_plus', 'fmts': ['srtm_plus'], 'call': Fetcher},
        -200: {'name': 'charts', 'fmts': ['charts'], 'call': Fetcher},
        -201: {'name': 'multibeam', 'fmts': ['multibeam'], 'call': Fetcher},
        -202: {'name': 'nos', 'fmts': ['nos'], 'call': HydroNOSFetcher},
        -203: {'name': 'ehydro', 'fmts': ['ehydro'], 'call': eHydroFetcher},
        -204: {'name': 'bluetopo', 'fmts': ['bluetopo'], 'call': BlueTopoFetcher},
        -205: {'name': 'ngs', 'fmts': ['ngs'], 'call': NGSFetcher},
        -206: {'name': 'tides', 'fmts': ['tides'], 'call': TidesFetcher},
        -207: {'name': 'digital_coast', 'fmts': ['digital_coast'], 'call': Fetcher},
        -208: {'name': 'ncei_thredds', 'fmts': ['ncei_thredds'], 'call': Fetcher},
        -209: {'name': 'tnm', 'fmts': ['tnm'], 'call': Fetcher},
        -300: {'name': 'emodnet', 'fmts': ['emodnet'], 'call': Fetcher},
        -301: {'name': 'chs', 'fmts': ['chs'], 'call': Fetcher}, # chs is broken
        -302: {'name': 'hrdem', 'fmts': ['hrdem'], 'call': Fetcher},
        -303: {'name': 'arcticdem', 'fmts': ['arcticdem'], 'call': Fetcher},
        -304: {'name': 'vdatum', 'fmts': ['vdatum'], 'call': VDatumFetcher},        
    }
    _datalist_cols = ['path', 'format', 'weight', 'uncertainty', 'title', 'source',
                      'date', 'type', 'resolution', 'horz', 'vert',
                      'url']

    _metadata_keys = ['name', 'title', 'source', 'date', 'data_type', 'resolution',
                      'hdatum', 'vdatum', 'url']
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    ## ==============================================
    ## redefine the factory default _parse_mod function for datasets
    ## the mod in this case is a datalist entry and the format key
    ## becomes the module
    ## ==============================================
    def _parse_mod(self, mod=None):
        """parse the datalist entry line"""

        self.kwargs['fn'] = mod
        if self.kwargs['fn'] is None:
            return(self)

        ## ==============================================
        ## mod exists as a file, no other entry items should occur, so
        ## guess the format and finish there...
        ## the format number becomes the mod_name
        ## check for specified data format as well
        ## ==============================================
        if os.path.exists(self.kwargs['fn']):
            if self.kwargs['data_format'] is None:
                self.mod_name = self.guess_data_format(self.kwargs['fn'])
                self.mod_args = {}
                self.kwargs['data_format'] = self.mod_name
            else:
                opts = str(self.kwargs['data_format']).split(':')
                if len(opts) > 1:
                    self.mod_name = int(opts[0])
                    self.mod_args = utils.args2dict(list(opts[1:]), {})
                else:
                    self.mod_name = int(self.kwargs['data_format'])
                    self.mod_args = {}
                    
                self.kwargs['data_format'] = self.mod_name

            #self.kwargs['weight'] = self.set_default_weight()
            #self.kwargs['uncertainty'] = self.set_default_uncertainty()
            # inherit metadata from parent if available
            self.kwargs['metadata'] = {}
            self.kwargs['metadata']['name'] = utils.fn_basename2(os.path.basename(self.kwargs['fn']))
            
            for key in self._metadata_keys:
                if key not in self.kwargs['metadata'].keys():
                    self.kwargs['metadata'][key] = None

            return(self.mod_name, self.mod_args)

        ## ==============================================
        ## if fn is not a path, parse it as a datalist entry
        ## ==============================================
        this_entry = re.findall(r'[^"\s]\S*|".+?"', self.kwargs['fn'].rstrip())        
        try:
            entry = [utils.str_or(x) if n == 0 \
                     else utils.str_or(x) if n < 2 \
                     else utils.float_or(x) if n < 3 \
                     else utils.float_or(x) if n < 4 \
                     else utils.str_or(x) \
                     for n, x in enumerate(this_entry)]

        except Exception as e:
            utils.echo_error_msg('could not parse entry {}'.format(self.kwargs['fn']))
            return(self)

        ## ==============================================
        ## data format - entry[1]
        ## guess format based on fn if not specified otherwise
        ## parse the format for dataset specific opts.
        ## ==============================================
        if len(entry) < 2:
            for key in self._modules.keys():
                se = entry[0].split('.')
                see = se[-1] if len(se) > 1 else entry[0].split(":")[0]
                if see in self._modules[key]['fmts']:
                    entry.append(int(key))
                    break
                
            if len(entry) < 2:
                utils.echo_error_msg('could not parse entry {}'.format(self.kwargs['fn']))
                return(self)
        else:
            opts = entry[1].split(':')
            if len(opts) > 1:
                self.mod_args = utils.args2dict(list(opts[1:]), {})
                entry[1] = opts[0]
            else:
                self.mod_args = {}

        self.mod_name = int(entry[1])
        self.kwargs['data_format'] = int(entry[1])

        ## ==============================================
        ## file-name (or fetches module name) - entry[0]
        ## set to relative path from parent
        ## ==============================================
        if self.kwargs['parent'] is None or entry[1] == -11:
            self.kwargs['fn'] = entry[0]
        else:
            self.kwargs['fn'] = os.path.join(
                os.path.dirname(self.kwargs['parent'].fn), entry[0]
            )
            
        ## ==============================================
        ## weight - entry[2]
        ## inherit weight of parent
        ## ==============================================
        if len(entry) < 3:
            entry.append(self.set_default_weight())
        elif entry[2] is None:
            entry[2] = self.set_default_weight()

        if self.kwargs['parent'] is not None:
            if self.kwargs['weight'] is not None:
                self.kwargs['weight'] *= entry[2]
        else:
            if self.kwargs['weight'] is not None:
                self.kwargs['weight'] = entry[2]

        ## ==============================================
        ## uncertainty - entry[3]
        ## combine with partent using root sum of squares
        ## ==============================================
        if len(entry) < 4:
            entry.append(self.set_default_uncertainty())
        elif entry[3] is None:
            entry[3] = self.set_default_uncertainty()

        if self.kwargs['parent'] is not None:
            if self.kwargs['uncertainty'] is not None:
                self.kwargs['uncertainty'] = math.sqrt(self.kwargs['uncertainty']**2 + entry[3]**2)
        else:
            if self.kwargs['uncertainty'] is not None:
                self.kwargs['uncertainty'] = entry[3]
                    
        ## ==============================================
        ## Optional arguments follow, for metadata generation
        ## ==============================================
        if 'metadata' not in self.kwargs:
            self.kwargs['metadata'] = {}

        for key in self._metadata_keys:
            if key not in self.kwargs['metadata'].keys():
                self.kwargs['metadata'][key] = None

        ## ==============================================
        ## title - entry[4]
        ## ==============================================
        if len(entry) < 5:
            entry.append(self.kwargs['metadata']['title'])
        else:
            self.kwargs['metadata']['title'] = entry[4]

        # if self.kwargs['parent'] is not None:
        #     if self.kwargs['parent'].metadata['title'] is None:
        #         self.kwargs['metadata']['name'] = None
        #     else:
        #         if self.kwargs['parent'].metadata['title'] == self.kwargs['metadata']['title']:
        #             self.kwargs['metadata']['name'] = self.kwargs['parent'].metadata['name']
            
        if self.kwargs['metadata']['name'] is None:
            self.kwargs['metadata']['name'] = utils.fn_basename2(os.path.basename(self.kwargs['fn']))
            
        ## ==============================================
        ## source - entry[5]
        ## ==============================================
        if len(entry) < 6:
            entry.append(self.kwargs['metadata']['source'])
        else:
            self.kwargs['metadata']['source'] = entry[5]

        ## ==============================================
        ## date - entry[6]
        ## ==============================================
        if len(entry) < 7:
            entry.append(self.kwargs['metadata']['date'])
        else:
            self.kwargs['metadata']['date'] = entry[6]

        ## ==============================================
        ## data type - entry[7]
        ## ==============================================
        if len(entry) < 8:
            entry.append(self.kwargs['metadata']['data_type'])
        else:
            self.kwargs['metadata']['data_type'] = entry[7]

        ## ==============================================
        ## resolution - entry[8]
        ## ==============================================
        if len(entry) < 9:
            entry.append(self.kwargs['metadata']['resolution'])
        else:
            self.kwargs['metadata']['resolution'] = entry[8]

        ## ==============================================
        ## hdatum - entry[9]
        ## ==============================================
        if len(entry) < 10:
            entry.append(self.kwargs['metadata']['hdatum'])
        else:
            self.kwargs['metadata']['hdatum'] = entry[9]

        ## ==============================================
        ## vdatum - entry[10]
        ## ==============================================
        if len(entry) < 11:
            entry.append(self.kwargs['metadata']['vdatum'])
        else:
            self.kwargs['metadata']['vdatum'] = entry[10]

        ## ==============================================
        ## url - entry[11]
        ## ==============================================
        if len(entry) < 12:
            entry.append(self.kwargs['metadata']['url'])
        else:
            self.kwargs['metadata']['url'] = entry[11]
        
        #self.kwargs['data_format'] = entry[1]
        #self.guess_data_format()
        #return(self)
        return(self.mod_name, self.mod_args)

    def set_default_weight(self):
        return(1)

    def set_default_uncertainty(self):
        return(0)
    
    def guess_data_format(self, fn):
        """guess a data format based on the file-name"""

        # if self.data_format is not None:
        #     return
        
        #if self.fn is not None:
        for key in self._modules.keys():
            if fn.split('.')[-1] in self._modules[key]['fmts']:
                return(key)
            
    def write_parameter_file(self, param_file: str):
        try:
            with open(param_file, 'w') as outfile:
                # params = self.__dict__.copy()
                # t = self
                # while True:
                #     if t.__dict__['parent'] is not None:
                #         t = t.__dict__['parent']

                #     else:
                #         ...
                        
                    
                # params['parent'] = self.__dict__['parent'].params()                    
                json.dump(self.__dict__, outfile)
                utils.echo_msg('New DatasetFactory file written to {}'.format(param_file))
                
        except:
            raise ValueError('DatasetFactory: Unable to write new parameter file to {}'.format(param_file))

            

## ==============================================
## Command-line Interface (CLI)
## $ dlim
##
## datalists cli
## ==============================================
datalists_usage = """{cmd} ({dl_version}): DataLists IMproved; Process and generate datalists

usage: {cmd} [ -acdghijquwEJPR [ args ] ] DATALIST,FORMAT,WEIGHT,UNCERTAINTY ...

Options:
  -R, --region\t\tRestrict processing to the desired REGION 
\t\t\tWhere a REGION is xmin/xmax/ymin/ymax[/zmin/zmax[/wmin/wmax/umin/umax]]
\t\t\tUse '-' to indicate no bounding range; e.g. -R -/-/-/-/-10/10/1/-/-/-
\t\t\tOR an OGR-compatible vector file with regional polygons. 
\t\t\tWhere the REGION is /path/to/vector[:zmin/zmax[/wmin/wmax/umin/umax]].
\t\t\tIf a vector file is supplied, will use each region found therein.
  -E, --increment\tBlock data to INCREMENT in native units.
\t\t\tWhere INCREMENT is x-inc[/y-inc]
  -X, --extend\t\tNumber of cells with which to EXTEND the output DEM REGION and a 
\t\t\tpercentage to extend the processing REGION.
\t\t\tWhere EXTEND is dem-extend(cell-count)[:processing-extend(percentage)]
\t\t\te.g. -X6:10 to extend the DEM REGION by 6 cells and the processing region by 10 
\t\t\tpercent of the input REGION.
  -J, --s_srs\t\tSet the SOURCE projection.
  -P, --t_srs\t\tSet the TARGET projection. (REGION should be in target projection) 

  --mask\t\tMASK the datalist to the given REGION/INCREMENTs
  --spatial-metadata\tGenerate SPATIAL METADATA of the datalist to the given REGION/INCREMENTs
  --archive\t\tARCHIVE the datalist to the given REGION[/INCREMENTs]
  --glob\t\tGLOB the datasets in the current directory to stdout
  --info\t\tGenerate and return an INFO dictionary of the dataset
  --weights\t\tOutput WEIGHT values along with xyz
  --uncertainties\tOutput UNCERTAINTY values along with xyz
  --quiet\t\tLower the verbosity to a quiet

  --modules\t\tDisplay the datatype descriptions and usage
  --help\t\tPrint the usage text
  --version\t\tPrint the version information

Supported datalist formats (see {cmd} --modules <dataset-key> for more info): 
  {dl_formats}

Examples:
  % {cmd} my_data.datalist -R -90/-89/30/31
  % {cmd} -R-90/-89/30/31/-100/100 *.tif -l -w > tifs_in_region.datalist
  % {cmd} -R my_region.shp my_data.xyz -w -s_srs epsg:4326 -t_srs epsg:3565 > my_data_3565.xyz

CIRES DEM home page: <http://ciresgroups.colorado.edu/coastalDEM>\
""".format(cmd=os.path.basename(sys.argv[0]), 
           dl_version=cudem.__version__,
           dl_formats=utils._cudem_module_name_short_desc(DatasetFactory._modules))

def datalists_cli(argv=sys.argv):
    """run datalists from command-line

    See `datalists_cli_usage` for full cli options.
    """

    dls = []
    src_srs = None
    dst_srs = None
    i_regions = []
    these_regions = []
    xy_inc = [None, None]
    extend = 0
    want_weights = False
    want_uncertainties = False
    want_mask = False
    want_inf = False
    want_list = False
    want_glob = False
    want_archive = False
    want_verbose = True
    want_region = False
    want_separate = False
    want_sm = False
    invert_region=False
    
    ## ==============================================
    ## parse command line arguments.
    ## ==============================================
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--region' or arg == '-R':
            i_regions.append(str(argv[i + 1]))
            i = i + 1
        elif arg[:2] == '-R':
            i_regions.append(str(arg[2:]))
        elif arg == '--increment' or arg == '-E':
            xy_inc = argv[i + 1].split('/')
            i = i + 1
        elif arg[:2] == '-E':
            xy_inc = arg[2:].split('/')
        elif arg == '--extend' or arg == '-X':
            extend = utils.int_or(argv[i + 1], 0)
            i += 1
        elif arg[:2] == '-X':
            extend = utils.int_or(argv[2:], 0)
        elif arg == '-s_srs' or arg == '--s_srs' or arg == '-J':
            src_srs = argv[i + 1]
            i = i + 1
        elif arg == '-t_srs' or arg == '--t_srs' or arg == '-P':
            dst_srs = argv[i + 1]
            i = i + 1
        elif arg == '--mask' or arg == '-m':
            want_mask = True
        elif arg == '--invert_region' or arg == '-v':
            invert_region = True
        elif arg == '--archive' or arg == '-a':
            want_archive = True
        elif arg == '--weights' or arg == '-w':
            want_weights = True
        elif arg == '--uncertainties' or arg == '-u':
            want_uncertainties = True
        elif arg == '--info' or arg == '-i':
            want_inf = True
        elif arg == '--region_inf' or arg == '-r':
            want_region = True
        elif arg == '--list' or arg == '-l':
            want_list = True
        elif arg == '--glob' or arg == '-g':
            want_glob = True
        elif arg == '--spatial-metadata' or arg == '-sm':
            want_sm = True
            want_mask = True
            
        elif arg == '--separate' or arg == '-s':
            want_separate = True

        elif arg == '--modules':
            utils.echo_modules(DatasetFactory._modules, None if i+1 >= len(argv) else int(sys.argv[i+1]))
            sys.exit(0)           
        elif arg == '--quiet' or arg == '-q':
            want_verbose = False
        elif arg == '--help' or arg == '-h':
            print(datalists_usage)
            sys.exit(1)
        elif arg == '--version' or arg == '-v':
            print('{}, version {}'.format(
                os.path.basename(sys.argv[0]), cudem.__version__)
                  )
            sys.exit(1)
        elif arg[0] == '-':
            print(datalists_usage)
            sys.exit(0)
        else: dls.append(arg)
        
        i = i + 1

    if len(xy_inc) < 2:
        xy_inc.append(xy_inc[0])
    elif len(xy_inc) == 0:
        xy_inc = [None, None]

    if want_glob:
        import glob
        for key in DatasetFactory()._modules.keys():
            if key != -1:
                for f in DatasetFactory()._modules[key]['fmts']:
                    globs = glob.glob('*.{}'.format(f))
                    [sys.stdout.write(
                        '{}\n'.format(
                            ' '.join(
                                [x, str(key), '1', '0']
                            )
                        )
                    ) for x in globs]
                    
        sys.exit(0)

    if not i_regions: i_regions = [None]
    these_regions = regions.parse_cli_region(i_regions, want_verbose)
    for rn, this_region in enumerate(these_regions):
        ## buffer the region by `extend` if xy_inc is set
        ## this effects the output naming of masks/stacks!
        ## do we want this like in waffles where the output name
        ## does not include the -X extend buffer?
        if xy_inc[0] is not None and xy_inc[1] is not None and this_region is not None:
            this_region.buffer(
                x_bv=(utils.str2inc(xy_inc[0])*extend),
                y_bv=(utils.str2inc(xy_inc[1])*extend)
            )
        if len(dls) == 0:
            sys.stderr.write(datalists_usage)
            utils.echo_error_msg('you must specify some type of data')
        else:
            ## intiialze the input data. Treat data from CLI as a datalist.
            this_datalist = init_data(
                dls, region=this_region, src_srs=src_srs, dst_srs=dst_srs, xy_inc=xy_inc, sample_alg='bilinear',
                want_weight=want_weights, want_uncertainty=want_uncertainties, want_verbose=want_verbose,
                want_mask=want_mask, want_sm=want_sm, invert_region=invert_region, cache_dir=None
            )
            if this_datalist is not None and this_datalist.valid_p(
                    fmts=DatasetFactory._modules[this_datalist.data_format]['fmts']
            ):
                this_datalist.initialize()
                if not want_weights:
                    this_datalist.weight = None
                    
                if not want_uncertainties:
                    this_datalist.uncertainty = None
                    
                if want_inf:
                    print(this_datalist.inf()) # output the datalist inf blob
                elif want_list:
                    this_datalist.echo() # output each dataset from the datalist
                elif want_region:
                    ## get the region and warp it if necessary
                    this_inf = this_datalist.inf()
                    this_region = regions.Region().from_list(this_inf['minmax'])
                    if dst_srs is not None:
                        if src_srs is not None:
                            this_region.src_srs = src_srs
                            this_region.warp(dst_srs)
                        elif 'src_srs' in this_inf and this_inf['src_srs'] is not None:
                            this_region.src_srs = this_inf['src_srs']
                            this_region.warp(dst_srs)
                    print(this_region.format('gmt'))
                elif want_archive:
                    this_datalist.archive_xyz() # archive the datalist as xyz
                else:
                    # try:
                    #    if want_separate: # process and dump each dataset independently
                    #        for this_entry in this_datalist.parse():
                    #            this_entry.dump_xyz()
                    #    else: # process and dump the datalist as a whole
                    this_datalist.dump_xyz()
                    # except KeyboardInterrupt:
                    #    utils.echo_error_msg('Killed by user')
                    #    break
                    # except BrokenPipeError:
                    #    utils.echo_error_msg('Pipe Broken')
                    #    break
                    # except Exception as e:
                    #    utils.echo_error_msg(e)
### End
