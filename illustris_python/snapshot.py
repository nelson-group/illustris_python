""" Illustris Simulation: Public Data Release.
snapshot.py: File I/O related to the snapshot files. """
from __future__ import print_function

import numpy as np
import h5py
import six
from os.path import isfile

from .util import partTypeNum
from .groupcat import gcPath, offsetPath

def snapPath(basePath, snapNum, chunkNum=0):
    """ Return absolute path to a snapshot HDF5 file (modify as needed). """
    snapPath = basePath + '/snapdir_' + str(snapNum).zfill(3) + '/'
    filePath1 = snapPath + 'snap_' + str(snapNum).zfill(3) + '.' + str(chunkNum) + '.hdf5'
    filePath2 = filePath1.replace('/snap_', '/snapshot_')

    if isfile(filePath1):
        return filePath1
    return filePath2

def getNumPart(header):
    """ Calculate number of particles of all types given a snapshot header. """
    if 'NumPart_Total_HighWord' not in header:
        return header['NumPart_Total'] # new uint64 convention

    nTypes = 6

    nPart = np.zeros(nTypes, dtype=np.int64)
    for j in range(nTypes):
        nPart[j] = header['NumPart_Total'][j] | (header['NumPart_Total_HighWord'][j] << 32)

    return nPart


def loadSubset(basePath, snapNum, partType, fields=None, subset=None, mdi=None, sq=True, float32=False, result=None):
    """ Load a subset of fields for all particles/cells of a given partType.
        If offset and length specified, load only that subset of the partType.
        If mdi is specified, must be a list of integers of the same length as fields,
        giving for each field the multi-dimensional index (on the second dimension) to load.
          For example, fields=['Coordinates', 'Masses'] and mdi=[1, None] returns a 1D array
          of y-Coordinates only, together with Masses.
        If sq is True, return a numpy array instead of a dict if len(fields)==1.
        If float32 is True, load any float64 datatype arrays directly as float32 (save memory). 
        If result is not None, should be a dict containing pre-allocated ndarrays for each 
        requested field. And optionally: {field}_write_offset specifying the starting write offset 
        to place the result within result[{field}]."""
    if result is None: result = {}

    ptNum = partTypeNum(partType)
    gName = "PartType" + str(ptNum)

    # make sure fields is not a single element
    if isinstance(fields, six.string_types):
        fields = [fields]

    # load header from first chunk
    with h5py.File(snapPath(basePath, snapNum), 'r') as f:

        header = dict(f['Header'].attrs.items())
        nPart = getNumPart(header)

        # decide global read size, starting file chunk, and starting file chunk offset
        if subset:
            offsetsThisType = subset['offsetType'][ptNum] - subset['snapOffsets'][ptNum, :]

            fileNum = np.max(np.where(offsetsThisType >= 0))
            fileOff = offsetsThisType[fileNum]
            numToRead = subset['lenType'][ptNum]
        else:
            fileNum = 0
            fileOff = 0
            numToRead = nPart[ptNum]

        result['count'] = numToRead

        if not numToRead:
            # print('warning: no particles of requested type, empty return.')
            return result

        # find a chunk with this particle type
        i = 1
        while gName not in f:
            f = h5py.File(snapPath(basePath, snapNum, i), 'r')
            i += 1

        # if fields not specified, load everything
        if not fields:
            fields = list(f[gName].keys())

        for i, field in enumerate(fields):
            # verify existence
            if field not in f[gName].keys():
                raise Exception("Particle type ["+str(ptNum)+"] does not have field ["+field+"]")

            # replace local length with global
            shape = list(f[gName][field].shape)
            shape[0] = numToRead

            # multi-dimensional index slice load
            if mdi is not None and mdi[i] is not None:
                if len(shape) != 2:
                    raise Exception("Read error: mdi requested on non-2D field ["+field+"]")
                shape = [shape[0]]

            # allocate within return dict
            if field not in result:
                dtype = f[gName][field].dtype
                if dtype == np.float64 and float32: dtype = np.float32
                result[field] = np.zeros(shape, dtype=dtype)

    # loop over chunks
    wOffset = 0 if field+'_write_offset' not in result else result[field+'_write_offset']
    origNumToRead = numToRead

    while numToRead:
        f = h5py.File(snapPath(basePath, snapNum, fileNum), 'r')

        # no particles of requested type in this file chunk?
        if gName not in f:
            f.close()
            fileNum += 1
            fileOff  = 0
            continue

        # set local read length for this file chunk, truncate to be within the local size
        numTypeLocal = f['Header'].attrs['NumPart_ThisFile'][ptNum]

        numToReadLocal = numToRead

        if fileOff + numToReadLocal > numTypeLocal:
            numToReadLocal = numTypeLocal - fileOff

        #print('['+str(fileNum).rjust(3)+'] off='+str(fileOff)+' read ['+str(numToReadLocal)+\
        #      '] of ['+str(numTypeLocal)+'] remaining = '+str(numToRead-numToReadLocal), flush=True)

        # define slice in destination array
        out_slice = np.s_[wOffset:wOffset+numToReadLocal]

        # loop over each requested field for this particle type and load
        for i, field in enumerate(fields):
            # define hyperslab in source file
            source_slice = np.s_[fileOff:fileOff+numToReadLocal]
            if mdi is not None and mdi[i] is not None:
                source_slice = np.s_[fileOff:fileOff+numToReadLocal, mdi[i]]

            #result[field][out_slice] = f[gName][field][source_slice]
            f[gName][field].read_direct(result[field], source_sel=source_slice, dest_sel=out_slice)

        wOffset   += numToReadLocal
        numToRead -= numToReadLocal
        fileNum   += 1
        fileOff    = 0  # start at beginning of all file chunks other than the first

        f.close()

    # verify we read the correct number
    if origNumToRead != wOffset:
        raise Exception("Read ["+str(wOffset)+"] particles, but was expecting ["+str(origNumToRead)+"]")

    # only a single field? then return the array instead of a single item dict
    if sq and len(fields) == 1:
        return result[fields[0]]

    return result


def getSnapOffsets(basePath, snapNum, id, type):
    """ Compute offsets within snapshot for a particular group/subgroup. """
    r = {}

    # old or new format
    if 'fof_subhalo' in gcPath(basePath, snapNum):
        # use separate 'offsets_nnn.hdf5' files
        with h5py.File(offsetPath(basePath, snapNum), 'r') as f:
            groupFileOffsets = f['FileOffsets/'+type][()]
            r['snapOffsets'] = np.transpose(f['FileOffsets/SnapByType'][()])  # consistency
    else:
        # load groupcat chunk offsets from header of first file
        with h5py.File(gcPath(basePath, snapNum), 'r') as f:
            groupFileOffsets = f['Header'].attrs['FileOffsets_'+type]
            r['snapOffsets'] = f['Header'].attrs['FileOffsets_Snap']

    # calculate target groups file chunk which contains this id
    groupFileOffsets = int(id) - groupFileOffsets
    fileNum = np.max(np.where(groupFileOffsets >= 0))
    groupOffset = groupFileOffsets[fileNum]

    # load the length (by type) of this group/subgroup from the group catalog
    with h5py.File(gcPath(basePath, snapNum, fileNum), 'r') as f:
        r['lenType'] = f[type][type+'LenType'][groupOffset, :]

    # old or new format: load the offset (by type) of this group/subgroup within the snapshot
    if 'fof_subhalo' in gcPath(basePath, snapNum):
        with h5py.File(offsetPath(basePath, snapNum), 'r') as f:
            r['offsetType'] = f[type+'/SnapByType'][id, :]
    else:
        with h5py.File(gcPath(basePath, snapNum, fileNum), 'r') as f:
            r['offsetType'] = f['Offsets'][type+'_SnapByType'][groupOffset, :]

    return r


def loadSubhalo(basePath, snapNum, id, partType, fields=None):
    """ Load all particles/cells of one type for a specific subhalo
        (optionally restricted to a subset fields). """
    # load subhalo length, compute offset, call loadSubset
    subset = getSnapOffsets(basePath, snapNum, id, "Subhalo")
    return loadSubset(basePath, snapNum, partType, fields, subset=subset)


def loadHalo(basePath, snapNum, id, partType, fields=None):
    """ Load all particles/cells of one type for a specific halo
        (optionally restricted to a subset fields). """
    # load halo length, compute offset, call loadSubset
    subset = getSnapOffsets(basePath, snapNum, id, "Group")
    return loadSubset(basePath, snapNum, partType, fields, subset=subset)
