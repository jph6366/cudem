#!/usr/bin/env python
### gdal_outliers.py
##
## Copyright (c) 2021 CIRES Coastal DEM Team
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
## filter outliers in a gdal compatible DEM
##
### Code:

import sys
from cudem import demfun
from cudem import utils

_version = '0.0.1'
_usage = '''gdal_outliers.py ({}): filter z outliers in a DEM

usage: gdal_outliers.py [ file ]

 Options:
  file\t\tThe input DEM file-name

  --help\tPrint the usage text
  --version\tPrint the version information

 Examples:
 % gdal_outliers.py input.tif

CIRES DEM home page: <http://ciresgroups.colorado.edu/coastalDEM>
'''.format(_version)

if __name__ == "__main__":

    elev = None
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '-help' or arg == '--help' or arg == '-h':
            sys.stderr.write(_usage)
            sys.exit(1)
        elif arg == '-version' or arg == '--version':
            sys.stderr.write('{}\n'.format(_version))
            sys.exit(1)
        elif elev is None: elev = arg
        else:
            sys.stderr.write(_usage)
            sys.exit(1)
        i = i + 1

    if elev is None:
        sys.stderr.write(_usage)
        utils.echo_error_msg('you must enter an input file')
        sys.exit(1)
  
    dst_gdal = elev.split('.')[0] + '_fltr.tif'
    chunk_size = 10
    chunk_step = chunk_size
    threshhold = 5
    utils.echo_msg('filtering {} to {}'.format(elev, dst_gdal))
    demfun.filter_outliers(elev, dst_gdal, threshhold=threshhold, chunk_size=chunk_size, chunk_step=chunk_step)

### End
