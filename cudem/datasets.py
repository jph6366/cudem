### datasets.py - Datasets
##
## Copyright (c) 2010 - 2021 CIRES Coastal DEM Team
##
## datasets.py is part of CUDEM
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

import os
import sys
import json
import hashlib
import numpy as np
from scipy import spatial
from osgeo import gdal
from osgeo import ogr
from osgeo import osr
from cudem import utils
from cudem import regions
from cudem import xyzfun
from cudem import demfun

## ==============================================
## XYZDataset object, holding xyz-able Elevation data
## ==============================================
class XYZDataset():
    """representing an Elevation Dataset

    This is the super class for all datalist (dlim) datasets.
    Each dataset sub-class should define a dataset-specific
    data parser (to xyz) and a generate_inf function to generate
    inf files. 

    Specifically, each sub-dataset should minimally define the following:

      sub_ds.generate_inf()
      sub_ds.yield_xyz()

    where:
      generate_inf() - generates a dlim compatible inf file,
      yield_xyz() - yields the xyz elevation data (xyzfun.XYZPoint) from the dataset.

    This class provides:
      inf() - read/parse an inf file
      hash() - generate a hash of the dataset
      echo() - print the dataset as a datalist entry string
      archive_xyz() - archive the dataset
      mask_xyz() - mask the dataset
      parse() - parses the dataset and yields each elevation dataset found within.
                a dataset that holds other datasets (such as datalists, archives or fetch modules)
                should parse down to actual elevation datasets defined in this file.
                elevation datasets should just return themselves as the only item in a list.
    """

    def __init__(self, fn=None, data_format=None, weight=1, epsg=4326, name="XYZDataset", title=None,
                 source=None, date=None, data_type=None, resolution=None, vdatum=None, url=None,
                 parent=None, src_region=None, warp=None, verbose=False, remote=None):

        self.fn = fn
        self.data_format = data_format
        self.weight = weight
        self.name = name
        self.title = title
        self.epsg = utils.int_or(epsg)
        self.warp = utils.int_or(warp)
        self.source = source
        self.date = date
        self.data_type = data_type
        self.resolution = resolution
        self.vdatum = vdatum
        self.url = url
        self.parent = parent
        self.verbose = verbose
        self.region = src_region

        self._fn = None
        self.infos = {}
        self.data_entries = []
        self.data_lists = {}
        
        self.remote = remote
        if utils.fn_url_p(self.fn):
            self.remote = True

        if self.valid_p():
            if not self.remote:
                self.inf()
            
    def fetch(self):
        for entry in self.data_entries:
            if entry.remote:
                if entry._fn is None:
                    entry._fn = os.path.basename(self.fn)
                f = utils.Fetch(url=entry.fn, verbose=entry.verbose)
                if f.fetch_file(entry._fn) == 0:
                    entry.fn = entry._fn
            else:
                utils.echo_warning_msg('nothing to fetch')

    ## ==============================================
    ## validity checks
    ## ==============================================
    def valid_p(self, fmts=[]):
        """check if self appears to be a valid datalist entry

        Returns:
          bools: True if dataset is valid else False
        """
        
        if self.fn is None: return(False)
        if self.data_format is None: return(False)
        if self.fn is not None:
            if self.fn not in fmts:
                if not utils.fn_url_p(self.fn):
                    if not os.path.exists(self.fn): return (False)
                    if os.stat(self.fn).st_size == 0: return(False)
        return(True)
        
    def hash(self, sha1=False):
        """generate a hash of the xyz-dataset source file

        Returns:
          str: hexdigest
        """
        
        BUF_SIZE = 65536  # lets read stuff in 64kbchunks!
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

    ## ==============================================
    ## dataset formatting
    ## ==============================================
    def echo_(self, **kwargs):
        """print self as a datalist entry string"""

        return(' '.join(
            [str(x) for x in [self.fn, self.data_format, self.weight, self.title, self.source,
                              self.date, self.data_type, self.resolution, self.epsg, self.vdatum,
                              self.url]]))
    
    def echo(self, **kwargs):
        """print self.data_entries as a datalist entries."""

        for entry in self.parse():
            l = [entry.fn, entry.data_format]
            if entry.weight is not None: l.append(entry.weight)
            print('{}'.format(" ".join([str(x) for x in l])))

    def format_metadata(self, **kwargs):
        """format metadata from self, for use as a datalist entry."""
        
        return('{} {} {} {} {} {} {} {} {}'.format(
            self.weight, self.title, self.source,
            self.date, self.data_type, self.resolution, self.epsg, self.vdatum,
            self.url))

    ## ==============================================
    ## inf generation/parsing
    ## ==============================================
    def generate_inf(self):
        """define this in sub-class"""

        raise(NotImplementedError)
            
    def inf(self, check_hash = False, **kwargs):
        """read/write an inf file

        If the inf file is not found, will attempt to generate one.
        The function `generate_inf()` should be defined for each specific
        dataset sub-class.

        Args:
          kwargs (dict): any arguments to pass to the dataset parser
        
        Returns:
          dict: xyz-dataset info dictionary
        """
        
        inf_path = '{}.inf'.format(self.fn)

        if os.path.exists(inf_path):
            try:
                with open(inf_path) as i_ob:
                    self.infos = json.load(i_ob)
            except ValueError:
                try:
                    self.infos = MBSParser(
                        fn=self.fn, epsg=self.epsg).inf_parse().infos
                    self.check_hash = False
                except:
                    utils.echo_error_msg('failed to parse inf {}'.format(inf_path))
            except:
                utils.echo_error_msg('failed to parse inf {}'.format(inf_path))
        else:
            self.infos = {}
        
        if check_hash:
            if 'hash' in self.infos.keys():
                gen_inf = self.hash() != self.infos['hash']
            else:
                gen_inf = True
        else:
            gen_inf = 'hash' not in self.infos.keys() or 'wkt' not in self.infos.keys()

        if gen_inf:
            utils.echo_msg("generating inf for {}".format(self.fn))
            self.infos = self.generate_inf()
            self.infos['format'] = self.data_format
            if 'minmax' in self.infos:
                if self.infos['minmax'] is not None:
                    try:
                        with open('{}.inf'.format(self.fn), 'w') as inf:
                            inf.write(json.dumps(self.infos))
                    except:
                        #if self.parent is not None:
                        with open('{}_{}.inf'.format(
                                'dlim_tmp', self.region.format('fn')), 'w') as inf:
                            inf.write(json.dumps(self.infos))
            if self.parent is not None:
                utils.remove_glob('{}.inf'.format(self.parent.fn))
                self.parent.infos = {}
            self.infos['epsg'] = self.epsg
        self.infos['format'] = self.data_format
        return(self.infos)

    ## ==============================================
    ## dataset parsing
    ## ==============================================
    def parse(self):
        """Parse the datasets from the dataset.

        Re-define this method when defining a dataset sub-class
        that represents recursive data structures (datalists, zip, etc).
        """
        
        if self.region is not None:
            inf_region = regions.Region().from_string(self.infos['wkt'])
            if regions.regions_intersect_p(inf_region, self.region):
                self.data_entries.append(self)
                yield(self)
        else:
            self.data_entries.append(self)
            yield(self)

    def parse_data_lists(self):
        """parse the data into a datalist dictionary"""
        
        for e in self.data_entries:
            if e.parent is not None:
                if e.parent.name in self.data_lists.keys():
                    self.data_lists[e.parent.name]['data'].append(e)
                else:
                    self.data_lists[e.parent.name] = {'data': [e], 'parent': e.parent}
            else:
                self.data_lists[e.name] = {'data': [e], 'parent': e}
        return(self)

    ## ==============================================
    ## XYZ parsing
    ## ==============================================
    def yield_xyz(self):
        """define this in sub-class"""
        
        raise(NotImplementedError)

    def archive_xyz(self, **kwargs):
        """Archive data data from the dataset to XYZ in the given dataset region."""
        
        def xdl2dir(xdl):
            this_dir = []
            while True:
                if xdl.parent is None:
                    break
                this_dir.append(xdl.parent.name)
                xdl = xdl.parent
            this_dir.reverse()
            return(this_dir)
        
        if self.region is None:
            a_name = self.name
        else:
            a_name = '{}_{}_{}'.format(
                self.name, self.region.format('fn'), utils.this_year())
        
        with open('{}.datalist'.format(a_name), 'w') as dlf:
            for x in self.data_lists.keys():
                a_dir = '{}_{}_{}'.format(x, self.region.format('fn'), utils.this_year())
                this_dir = xdl2dir(self.data_lists[x]['parent'])
                this_dir.append(a_dir)
                tmp_dir = this_dir
                dlf.write('{}.datalist -1 {}\n'.format(os.path.join(*this_dir, this_dir[-1]), self.data_lists[x]['parent'].format_metadata()))
                this_dir = os.path.join(os.getcwd(), *this_dir)
                if not os.path.exists(this_dir):
                    os.makedirs(this_dir)
                with open(os.path.join(this_dir, '{}.datalist'.format(os.path.basename(this_dir))), 'w') as sub_dlf:
                    for xyz_dataset in self.data_lists[x]['data']:
                        sub_xyz_path = '.'.join(
                            [utils.fn_basename(os.path.basename(utils.slugify(xyz_dataset.fn)),
                                               xyz_dataset.fn.split('.')[-1]),
                             'xyz'])
                        
                        this_xyz_path = os.path.join(this_dir, sub_xyz_path)
                        sub_dlf.write('{} 168\n'.format(sub_xyz_path))
                        
                        with open(this_xyz_path, 'w') as xp:
                            for this_xyz in xyz_dataset.yield_xyz(**kwargs):
                                yield(this_xyz)
                                this_xyz.dump(include_w=True if self.weight is not None else False,
                                              dst_port=xp, encode=False)
        Datalist(fn='{}.datalist'.format(a_name)).parse()
            
    def mask_xyz(self, dst_gdal, dst_inc, dst_format='GTiff', **kwargs):
        """Create a num grid mask of xyz data. The output grid
        will contain 1 where data exists and 0 where no data exists.

        yields the xyz data
        """

        xcount, ycount, dst_gt = self.region.geo_transform(x_inc=dst_inc)
        ptArray = np.zeros((ycount, xcount))
        ds_config = demfun.set_infos(
            xcount, ycount, (xcount*ycount), dst_gt, utils.sr_wkt(self.epsg),
            gdal.GDT_Float32, -9999, 'GTiff')
        for this_xyz in self.yield_xyz(**kwargs):
            yield(this_xyz)
            if regions.xyz_in_region_p(this_xyz, self.region):
                xpos, ypos = utils._geo2pixel(this_xyz.x, this_xyz.y, dst_gt)
                try:
                    ptArray[ypos, xpos] = 1
                except:
                    pass
        out, status = utils.gdal_write(ptArray, dst_gdal, ds_config)    
        
    def dump_xyz(self, dst_port=sys.stdout, encode=False, **kwargs):
        """dump the XYZ data from the dataset"""
        
        for this_xyz in self.yield_xyz(**kwargs):
            this_xyz.dump(include_w=True if self.weight is not None else False,
                          dst_port=dst_port, encode=encode)

    def export_xyz_as_list(self, **kwargs):
        """return the XYZ data from the dataset as python list"""
        
        xyz_l = []
        for this_xyz in self.yield_xyz(**kwargs):
            xyz_l.append(xyzfun.XYZPoint().from_list(this_xyz.export_as_list(include_z=True, include_w=True)))
            
        return(xyz_l)
            
class MBSParser(XYZDataset):
    """providing an mbsystem parser"""

    def __init__(self, **kwargs):
        
        super().__init__(**kwargs)
        self.infos = {}
        
    def generate_inf(self):
        self.infos['name'] = self.fn
        self.infos['hash'] = None
        try:
            utils.run_cmd('mbdatalist -O -V -I{}'.format(self.fn))
            self.inf_parse()
        except: pass
            
        return(self.infos)

    def yield_xyz(self):
        for line in utils.yield_cmd('mblist -MA -OXYZ -I{} -F{}'.format(self.fn, self.data_format)):
            this_xyz = xyzfun.XYZPoint().from_string(line)
            this_xyz.weight = self.weight
            yield(this_xyz)
            
    def inf_parse(self):

        self.infos['name'] = self.fn
        self.infos['minmax'] = [0,0,0,0,0,0]
        self.infos['hash'] = None
        dims = []
        this_row = 0

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
            xcount, ycount, dst_gt = mbs_region.geo_transform(x_inc=xinc, y_inc=yinc)

            ds_config = {'nx': dims[0], 'ny': dims[1], 'nb': dims[1] * dims[0],
                         'geoT': dst_gt, 'proj': utils.sr_wkt(self.epsg),
                         'dt': gdal.GDT_Float32, 'ndv': 0, 'fmt': 'GTiff'}

            driver = gdal.GetDriverByName('MEM')
            ds = driver.Create('tmp', ds_config['nx'], ds_config['ny'], 1, ds_config['dt'])
            ds.SetGeoTransform(ds_config['geoT'])
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

## ==============================================
## XYZFile class - ASCII XYZ data
## ==============================================
class XYZFile(XYZDataset):
    """representing an xyz dataset stream
    """

    def __init__(self, delim=None, xpos=0, ypos=1, zpos=2, skip=0, x_scale=1, y_scale=1,
                 z_scale=1, x_offset=0, y_offset=0, **kwargs):
        
        self.delim = delim
        self.xpos = xpos
        self.ypos = ypos
        self.zpos = zpos
        self.skip = skip
        self.x_scale = x_scale
        self.y_scale = y_scale
        self.z_scale = z_scale
        self.x_offset = x_offset
        self.y_offset = y_offset
        
        self._known_delims = [',', '/', ':']
        self._known_fmts = ['xyz', 'csv', 'dat', 'ascii']
        super().__init__(**kwargs)
        
    def generate_inf(self):
        """generate a infos dictionary from the xyz dataset

        Returns:
          dict: a data-entry infos dictionary
        """
                
        pts = []
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self.infos['numpts'] = 0
        this_region = regions.Region()

        region_ = self.region
        self.region = None

        for i, l in enumerate(self.yield_xyz()):
            if i == 0:
                this_region.from_list([l.x, l.x, l.y, l.y, l.z, l.z])
            else:
                #try:
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
                #except: pass
            pts.append(l.export_as_list(include_z = True))
            self.infos['numpts'] = i

        self.infos['minmax'] = this_region.export_as_list(include_z = True)
        if self.infos['numpts'] > 0:
            try:
                out_hull = [pts[i] for i in spatial.ConvexHull(pts, qhull_options='Qt').vertices]
                out_hull.append(out_hull[0])
                self.infos['wkt'] = create_wkt_polygon(out_hull, xpos=0, ypos=1)
            except:
                self.infos['wkt'] = this_region.export_as_wkt()
        self.region = region_
        return(self.infos)
        
    def line_delim(self, xyz_line):
        """guess a line delimiter
        Args:
          xyz_line (str): a string representing delimited data.

        Returns:
          str: delimiter (or None)
        """

        for delim in self._known_delims:
            this_xyz = xyz_line.split(delim)
            if len(this_xyz) > 1:
                self.delim = delim
                
    def yield_xyz(self):
        """xyz file parsing generator

        Yields:
          xyz: xyz data
        """
        
        if self.fn is not None:
            if os.path.exists(str(self.fn)):
                self.src_data = open(self.fn, "r")
            else:
                self.src_data = sys.stdin
        else:
            self.src_data = sys.stdin
        
        sk = self.skip
        this_xyz = xyzfun.XYZPoint(w = 1)
        if self.region is not None:
            if self.region.epsg != self.epsg:
                if self.warp is not None:
                    if self.region.epsg != self.warp:
                        self.region.warp(warp_epsg=self.epsg)
                else: self.region.warp(warp_epsg=self.epsg)
                                
        warp_epsg = utils.int_or(self.warp)
        if warp_epsg is not  None and self.epsg is not None:
            src_srs = osr.SpatialReference()
            src_srs.ImportFromEPSG(self.epsg)

            dst_srs = osr.SpatialReference()
            dst_srs.ImportFromEPSG(warp_epsg)
            try:
                src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except:
                pass
            dst_trans = osr.CoordinateTransformation(src_srs, dst_srs)
        else: dst_trans = None
        
        ln = 0
        for xyz_line in self.src_data:
            if ln >= sk:
                if self.delim is None: self.line_delim(xyz_line)
                this_xyz.from_string(xyz_line, delim=self.delim,
                                     x_pos=self.xpos, y_pos=self.ypos)
                if this_xyz.valid_p():
                    this_xyz.x = (this_xyz.x+self.x_offset) * self.x_scale
                    this_xyz.y = (this_xyz.y+self.y_offset) * self.y_scale
                    this_xyz.z *= self.z_scale
                    this_xyz.w = self.weight
                    if self.region is not None and self.region.valid_p():
                        if regions.xyz_in_region_p(this_xyz, self.region):
                            if dst_trans is not None:
                                this_xyz.transform(dst_trans)
                            ln += 1
                            yield(this_xyz)
                    else:
                        if dst_trans is not None:
                            this_xyz.transform(dst_trans)
                        ln +=1
                        yield(this_xyz)        
            else: sk -= 1
        if self.verbose: utils.echo_msg('parsed {} data records from {}'.format(ln, self.fn))
        self.src_data.close()
        
## ==============================================
## RasterFile Class - GDAL supported raster data
## ==============================================
class RasterFile(XYZDataset):
    """providing a raster dataset parser
    """

    def __init__(self, mask=None, step=1, outf=None, **kwargs):

        self.mask = mask
        self.step = step

        self.src_ds = None
        self.ds_config = None
        self.ds_open_p = False

        self.outf = outf
        super().__init__(**kwargs)
        
    def _open_ds(self):
        """open the gdal datasource and gather infos 

        Returns:
          raster_parser: self
        """
        
        if self.fn is not None:
            if os.path.exists(str(self.fn)):
                try:
                    self.src_ds = gdal.Open(self.fn)
                except:
                    self.src_ds = None
            else:
                self.src_ds = None
        else:
            self.src_ds = None

        if self.src_ds is not None:
            self.ds_open_p = True
            self.gather_infos()
        else:
            self.ds_open_p = False
        return(self)

    def _close_ds(self):
        """close the gdal datasource

        Returns:
          raster_parser: self
        """
        
        self.src_ds = None
        self.ds_config = None
        self.ds_open_p = False
        return(self)

    def generate_inf(self):
        """generate a infos dictionary from the raster dataset

        Returns:
          dict: a data-entry infos dictionary
        """
            
        self.infos['name'] = self.fn
        self.infos['hash'] = self.hash()#dl_hash(self.fn)
        self._open_ds()

        if self.ds_open_p:
            gt = self.src_ds.GetGeoTransform()
            #srcwin = (0, 0, self.src_ds.RasterXSize, self.src_ds.RasterYSize)
            this_region = regions.Region().from_geo_transform(
                geoT=gt, x_count=self.src_ds.RasterXSize, y_count=self.src_ds.RasterYSize)
            try:
                zr = self.src_ds.GetRasterBand(1).ComputeRasterMinMax()
            except:
                zr = [None, None]
            this_region.zmin = zr[0]
            this_region.zmax = zr[1]
            self.infos['minmax'] = this_region.export_as_list(include_z=True)
            self.infos['numpts'] = self.src_ds.RasterXSize * self.src_ds.RasterYSize
            self.infos['wkt'] = this_region.export_as_wkt()
        self._close_ds()
        return(self.infos)

    def set_epsg(self, epsg = 4326):
        if self.ds_open_p:
            self.src_ds.SetProjection(sr_wkt(int(epsg)))

    def cut(self):
        if self.ds_open_p:
            ds_config = self.gather_infos()
            gt = ds_config['geoT']
            srcwin = region.srcwin(gt, ds_config['nx'], ds_config['ny'])
            
            ds_arr = self.src_ds.GetRasterBand(1).ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
            dst_gt = (gt[0] + (srcwin[0] * gt[1]), gt[1], 0., gt[3] + (srcwin[1] * gt[5]), 0., gt[5])
            out_ds_config = self.set_infos(
                srcwin[2], srcwin[3], srcwin[2] * srcwin[3], dst_gt, ds_config['proj'], ds_config['dt'],
                ds_config['ndv'], ds_config['fmt'])

            return(utils.gdal_write(ds_arr, dst_fn, out_ds_config))
        else: return(None, -1)
        
    def set_infos(nx, ny, nb, geoT, proj, dt, ndv, fmt):
        """set a datasource config dictionary
            
        returns gdal_config dict."""
        
        return({'nx': nx, 'ny': ny, 'nb': nb, 'geoT': geoT, 'proj': proj, 'dt': dt, 'ndv': ndv, 'fmt': fmt})
        
    def gather_infos(self, scan=False):
        """gather information from `src_ds` GDAL dataset

        Returns:
          raster_parser: self
        """

        if self.ds_open_p:
            self.gt = self.src_ds.GetGeoTransform()
            if self.region is not None and self.region.valid_p(check_xy = True):
                self.srcwin = self.region.srcwin(self.gt, self.src_ds.RasterXSize, self.src_ds.RasterYSize)
            else: self.srcwin = (0, 0, self.src_ds.RasterXSize, self.src_ds.RasterYSize)
            src_band = self.src_ds.GetRasterBand(1)
            #self.gt = (gt[0] + (self.srcwin[0]*gt[1]), gt[1], 0., gt[3] + (self.srcwin[1]*gt[5]), 0., gt[5])
            #self.gt = gt
            
            proj = self.src_ds.GetProjectionRef()
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(proj)
            src_srs.AutoIdentifyEPSG()
            srs_auth = src_srs.GetAuthorityCode(None)

            self.epsg = utils.int_or(srs_auth)
            self.x_count = self.srcwin[2]
            self.y_count = self.srcwin[3]
            self.dt = src_band.DataType
            self.dtn = gdal.GetDataTypeName(src_band.DataType)
            self.ndv = src_band.GetNoDataValue()
            if self.ndv is None: self.ndv = -9999
            self.fmt = self.src_ds.GetDriver().ShortName
            self.zr = None

            if scan:
                src_arr = src_band.ReadAsArray(srcwin[0], self.srcwin[1], self.srcwin[2], self.srcwin[3])
                self.zr = (np.amin(src_arr), np.amax(src_arr))
                src_arr = None
        return(self)
    
    def yield_xyz(self):
        """parse the data from gdal dataset src_ds (first band only)

        Yields:
          xyz: the parsed xyz data
        """

        self._open_ds()
        out_xyz = xyzfun.XYZPoint(w = 1)
        if self.src_ds is not None:
            ln = 0
            band = self.src_ds.GetRasterBand(1)
            gt = self.gt
            warp_epsg = self.warp
            
            if warp_epsg is not None and self.epsg is not None:
                src_srs = osr.SpatialReference()
                src_srs.ImportFromEPSG(self.epsg)

                dst_srs = osr.SpatialReference()
                dst_srs.ImportFromEPSG(warp_epsg)
                try:
                    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                except:
                    pass
                dst_trans = osr.CoordinateTransformation(src_srs, dst_srs)
            else:
                dst_trans = None
                        
            msk_band = None
            if self.mask is not None:
                src_mask = gdal.Open(self.mask)
                msk_band = src_mask.GetRasterBand(1)

            nodata = ['{:g}'.format(-9999), 'nan', float('nan')]
            if self.ndv is not None:
                nodata.append('{:g}'.format(self.ndv))
            for y in range(self.srcwin[1], self.srcwin[1] + self.srcwin[3], 1):
                band_data = band.ReadAsArray(self.srcwin[0], y, self.srcwin[2], 1)
                if self.region is not None:
                    z_region = self.region.z_region()
                    if z_region[0] is not None:
                        band_data[band_data < z_region[0]] = -9999
                    if z_region[1] is not None:
                        band_data[band_data > z_region[1]] = -9999
                if msk_band is not None:
                   msk_data = msk_band.ReadAsArray(self.srcwin[0], y, self.srcwin[2], 1)
                   band_data[msk_data==0]=-9999
                band_data = np.reshape(band_data, (self.srcwin[2], ))
                for x_i in range(0, self.srcwin[2], 1):
                    x = x_i + self.srcwin[0]
                    z = band_data[x_i]
                    if '{:g}'.format(z) not in nodata:
                        out_xyz.x, out_xyz.y = utils._pixel2geo(x, y, gt)
                        out_xyz.z = z
                        out_xyz.w = self.weight
                        if self.region is not None and self.region.valid_p():
                            if regions.xyz_in_region_p(out_xyz, self.region):
                                ln += 1
                                if dst_trans is not None: out_xyz.transform(dst_trans)
                                yield(out_xyz)
                        else:
                            ln += 1
                            if dst_trans is not None: out_xyz.transform(dst_trans)
                            yield(out_xyz)
            band = None
            src_mask = None
            msk_band = None
            if self.verbose:
                utils.echo_msg('parsed {} data records from {}'.format(ln, self.fn))
        self._close_ds()

### End