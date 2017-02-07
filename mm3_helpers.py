#!/usr/bin/python
from __future__ import print_function
def warning(*objs):
    print(time.strftime("%H:%M:%S", time.localtime()), *objs, file=sys.stderr)
def information(*objs):
    print(time.strftime("%H:%M:%S", time.localtime()), *objs, file=sys.stdout)

# import modules
import sys
import os
import time
import inspect
import yaml
import json # for importing tiff metdata
try:
    import cPickle as pickle # pickle
except:
    import pickle
import numpy as np
import scipy.signal as spsig # used in channel finding
import scipy.stats as spstats
import struct # for interpretting strings as binary data
import re # regular expressions
import traceback
import copy

# Image analysis modules
from scipy import ndimage as ndi
from skimage import segmentation # used in make_masks and segmentation
from skimage.feature import match_template # used to align images
from skimage.filters import threshold_otsu
from skimage import morphology # many functions is segmentation used from this

# Parralelization modules
import multiprocessing
from multiprocessing import Pool

# user modules
# realpath() will make your script run, even if you symlink it
cmd_folder = os.path.realpath(os.path.abspath(
                              os.path.split(inspect.getfile(inspect.currentframe()))[0]))
if cmd_folder not in sys.path:
    sys.path.insert(0, cmd_folder)

# This makes python look for modules in ./external_lib
cmd_subfolder = os.path.realpath(os.path.abspath(
                                 os.path.join(os.path.split(inspect.getfile(
                                 inspect.currentframe()))[0], "external_lib")))
if cmd_subfolder not in sys.path:
    sys.path.insert(0, cmd_subfolder)

import tifffile as tiff

### functions ###########################################################
# load the parameters file into a global dictionary for this module
def init_mm3_helpers(param_file_path):
    # load all the parameters into a global dictionary
    global params
    with open(param_file_path, 'r') as param_file:
        params = yaml.safe_load(param_file)

    # set up how to manage cores for multiprocessing
    cpu_count = multiprocessing.cpu_count()
    if cpu_count == 32:
        num_analyzers = 20
    elif cpu_count == 8:
        num_analyzers = 14
    else:
        num_analyzers = cpu_count*2 - 2
    params['num_analyzers'] = num_analyzers

    return

# finds metdata in a tiff image which has been expoted with Nikon Elements.
def get_tif_metadata_elements(tif):
    '''This function pulls out the metadata from a tif file and returns it as a dictionary.
    This if tiff files as exported by Nikon Elements as a stacked tiff, each for one tpoint.
    tif is an opened tif file (using the package tifffile)


    arguments:
        fname (tifffile.TiffFile): TIFF file object from which data will be extracted
    returns:
        dictionary of values:
            'jdn' (float)
            'x' (float)
            'y' (float)
            'plane_names' (list of strings)

    Called by
    mm3.Compile

    '''

    # image Metadata
    idata = { 'fov': -1,
              't' : -1,
              'jd': -1 * 0.0,
              'x': -1 * 0.0,
              'y': -1 * 0.0,
              'planes': []}

    # get the fov and t simply from the file name
    idata['fov'] = int(tif.fname.split('xy')[1].split('.tif')[0])
    idata['t'] = int(tif.fname.split('xy')[0].split('t')[-1])

    # a page is plane, or stack, in the tiff. The other metdata is hidden down in there.
    for page in tif:
        for tag in page.tags.values():
            #print("Checking tag",tag.name,tag.value)
            t = tag.name, tag.value
            t_string = u""
            time_string = u""
            # Interesting tag names: 65330, 65331 (binary data; good stuff), 65332
            # we wnat to work with the tag of the name 65331
            # if the tag name is not in the set of tegs we find interesting then skip this cycle of the loop
            if tag.name not in ('65331', '65332', 'strip_byte_counts', 'image_width', 'orientation', 'compression', 'new_subfile_type', 'fill_order', 'max_sample_value', 'bits_per_sample', '65328', '65333'):
                #print("*** " + tag.name)
                #print(tag.value)
                pass
            #if tag.name == '65330':
            #    return tag.value
            if tag.name in ('65331'):
                # make info list a list of the tag values 0 to 65535 by zipoing up a paired list of two bytes, at two byte intervals i.e. ::2
                # note that 0X100 is hex for 256
                infolist = [a+b*0x100 for a,b in zip(tag.value[0::2], tag.value[1::2])]
                # get char values for each element in infolist
                for c_entry in range(0, len(infolist)):
                    # the element corresponds to an ascii char for a letter or bracket (and a few other things)
                    if infolist[c_entry] < 127 and infolist[c_entry] > 64:
                        # add the letter to the unicode string t_string
                        t_string += chr(infolist[c_entry])
                    #elif infolist[c_entry] == 0:
                    #    continue
                    else:
                        t_string += " "

                # this block will find the dTimeAbsolute and print the subsequent integers
                # index 170 is counting seconds, and rollover of index 170 leads to increment of index 171
                # rollover of index 171 leads to increment of index 172
                # get the position of the array by finding the index of the t_string at which dTimeAbsolute is listed not that 2*len(dTimeAbsolute)=26
                #print(t_string)

                arraypos = t_string.index("dXPos") * 2 + 16
                xarr = tag.value[arraypos:arraypos+4]
                b = ''.join(chr(i) for i in xarr)
                idata['x'] = float(struct.unpack('<f', b)[0])

                arraypos = t_string.index("dYPos") * 2 + 16
                yarr = tag.value[arraypos:arraypos+4]
                b = ''.join(chr(i) for i in yarr)
                idata['y'] = float(struct.unpack('<f', b)[0])

                arraypos = t_string.index("dTimeAbsolute") * 2 + 26
                shortarray = tag.value[arraypos+2:arraypos+10]
                b = ''.join(chr(i) for i in shortarray)
                idata['jd'] = float(struct.unpack('<d', b)[0])

                # extract plane names
                il = [a+b*0x100 for a,b in zip(tag.value[0::2], tag.value[1::2])]
                li = [a+b*0x100 for a,b in zip(tag.value[1::2], tag.value[2::2])]

                strings = list(zip(il, li))

                allchars = ""
                for c_entry in range(0, len(strings)):
                    if 31 < strings[c_entry][0] < 127:
                        allchars += chr(strings[c_entry][0])
                    elif 31 < strings[c_entry][1] < 127:
                        allchars += chr(strings[c_entry][1])
                    else:
                        allchars += " "

                allchars = re.sub(' +',' ', allchars)

                words = allchars.split(" ")

                planes = []
                for idx in [i for i, x in enumerate(words) if x == "sOpticalConfigName"]:
                    planes.append(words[idx+1])

                idata['planes'] = planes

    return idata

# finds metdata in a tiff image which has been expoted with nd2ToTIFF.py.
def get_tif_metadata_nd2ToTIFF(tif):
    '''This function pulls out the metadata from a tif file and returns it as a dictionary.
    This if tiff files as exported by the mm3 function mm3_nd2ToTIFF.py. All the metdata
    is found in that script and saved in json format to the tiff, so it is simply extracted here

    Paramters:
        tif: TIFF file object from which data will be extracted
    Returns:
        dictionary of values:
            'fov': -1,
            't' : -1,
            'jdn' (float)
            'x' (float)
            'y' (float)
            'planes' (list of strings)

    Called by
    mm3_Compile.get_tif_params

    '''
    # get the first page of the tiff and pull out image description
    # this dictionary should be in the above form
    idata = tif[0].image_description
    idata = json.loads(idata.decode('utf-8'))

    return idata

# finds the location of channels in a tif
def find_channel_locs(image_data):
    '''Finds the location of channels from a phase contrast image. The channels are returned in
    a dictionary where the key is the x position of the channel in pixel and the value is a
    dicionary with the open and closed end in pixels in y.


    Called by
    mm3_Compile.get_tif_params

    '''

    # declare temp variables from yaml parameter dict.
    chan_w = params['channel_width']
    chan_sep = params['channel_separation']
    crop_wp = int(params['channel_width_pad'] + params['channel_width']/2)
    chan_snr = params['channel_detection_snr']

    # Detect peaks in the x projection (i.e. find the channels)
    projection_x = image_data.sum(axis=0)
    # find_peaks_cwt is a function which attempts to find the peaks in a 1-D array by
    # convolving it with a wave. here the wave is the default wave used by the algorithm
    # but the minimum signal to noise ratio is specified
    peaks = spsig.find_peaks_cwt(projection_x, np.arange(chan_w-5,chan_w+5),
                                 min_snr=chan_snr)

    # If the left-most peak position is within half of a channel separation,
    # discard the channel from the list.
    if peaks[0] < (chan_sep / 2):
        peaks = peaks[1:]
    # If the diference between the right-most peak position and the right edge
    # of the image is less than half of a channel separation, discard the channel.
    if image_data.shape[1] - peaks[-1] < (chan_sep / 2):
        peaks = peaks[:-1]

    # Find the average channel ends for the y-projected image
    projection_y = image_data.sum(axis=1)
    # find derivative, must use int32 because it was unsigned 16b before.
    proj_y_d = np.diff(projection_y.astype(np.int32))
    # use the top third to look for closed end, is pixel location of highest deriv
    onethirdpoint_y = int(projection_y.shape[0]/3.0)
    default_closed_end_px = proj_y_d[:onethirdpoint_y].argmax()
    # use bottom third to look for open end, pixel location of lowest deriv
    twothirdpoint_y = int(projection_y.shape[0]*2.0/3.0)
    default_open_end_px = twothirdpoint_y + proj_y_d[twothirdpoint_y:].argmin()
    default_length = default_open_end_px - default_closed_end_px # used for checks

    # go through peaks and assign information
    # dict for channel dimensions
    chnl_loc_dict = {}
    # key is peak location, value is dict with {'closed_end_px': px, 'open_end_px': px}

    for peak in peaks:
        # set defaults
        chnl_loc_dict[peak] = {'closed_end_px': default_closed_end_px,
                                 'open_end_px': default_open_end_px}
        # redo the previous y projection finding with just this channel
        channel_slice = image_data[:, peak-crop_wp:peak+crop_wp]
        slice_projection_y = channel_slice.sum(axis = 1)
        slice_proj_y_d = np.diff(slice_projection_y.astype(np.int32))
        slice_closed_end_px = slice_proj_y_d[:onethirdpoint_y].argmax()
        slice_open_end_px = twothirdpoint_y + slice_proj_y_d[twothirdpoint_y:].argmin()
        slice_length = slice_open_end_px - slice_closed_end_px

        # check if these values make sense. If so, use them. If not, use default
        # make sure lenght is not 15 pixels bigger or smaller than default
        if slice_length + 15 < default_length or slice_length - 15 > default_length:
            continue
        # make sure ends are greater than 15 pixels from image edge
        if slice_closed_end_px < 15 or slice_open_end_px > image_data.shape[0] - 15:
            continue

        # if you made it to this point then update the entry
        chnl_loc_dict[peak] = {'closed_end_px': slice_closed_end_px,
                                 'open_end_px': slice_open_end_px}

    return chnl_loc_dict

# make masks from initial set of images (same images as clusters)
def make_masks(analyzed_imgs):
    '''
    Make masks goes through the channel locations in the image metadata and builds a consensus
    Mask for each image per fov, which it returns as dictionary named channel_masks.
    The keys in this dictionary are fov id, and the values is a another dictionary. This dict's keys are channel locations (peaks) and the values is a [2][2] array:
    [[minrow, maxrow],[mincol, maxcol]] of pixel locations designating the corner of each mask
    for each channel on the whole image

    One important consequence of these function is that the channel ids and the size of the
    channel slices are decided now. Updates to mask must coordinate with these values.

    Parameters
    analyzed_imgs : dict
        image information created by get_params

    Returns
    channel_masks : dict
        dictionary of consensus channel masks.

    Called By
    mm3_Compile.py

    Calls
    '''
    information("Determining initial channel masks...")

    # declare temp variables from yaml parameter dict.
    crop_wp = int(params['channel_width_pad'] + params['channel_width']/2)
    chan_lp = params['channel_length_pad']

    #intiaize dictionary
    channel_masks = {}

    # get the size of the images (hope they are the same)
    for img_k, img_v in analyzed_imgs.iteritems():
        image_rows = img_v['shape'][0] # x pixels
        image_cols = img_v['shape'][1] # y pixels
        break # just need one. using iteritems mean the whole dict doesn't load

    # get the fov ids
    fovs = []
    for img_k, img_v in analyzed_imgs.iteritems():
        if img_v['fov'] not in fovs:
            fovs.append(img_v['fov'])

    # max width and length across all fovs. channels will get expanded by these values
    # this important for later updates to the masks, which should be the same
    max_chnl_mask_len = 0
    max_chnl_mask_wid = 0

    # for each fov make a channel_mask dictionary from consensus mask for each fov
    for fov in fovs:
        # initialize a the dict and consensus mask
        channel_masks_1fov = {} # dict which holds channel masks {peak : [[y1, y2],[x1,x2]],...}
        consensus_mask = np.zeros([image_rows, image_cols]) # mask for labeling

        # bring up information for each image
        for img_k, img_v in analyzed_imgs.iteritems():
            # skip this one if it is not of the current fov
            if img_v['fov'] != fov:
                continue

            # for each channel in each image make a single mask
            img_chnl_mask = np.zeros([image_rows, image_cols])

            # and add the channel mask to it
            for chnl_peak, peak_ends in img_v['channels'].iteritems():
                # pull out the peak location and top and bottom location
                # and expand by padding (more padding done later for width)
                x1 = max(chnl_peak - crop_wp, 0)
                x2 = min(chnl_peak + crop_wp, image_cols)
                y1 = max(peak_ends['closed_end_px'] - chan_lp, 0)
                y2 = min(peak_ends['open_end_px'] + chan_lp, image_rows)

                # add it to the mask for this image
                img_chnl_mask[y1:y2, x1:x2] = 1

            # add it to the consensus mask
            consensus_mask += img_chnl_mask

        # average the consensus mask
        consensus_mask = consensus_mask.astype('float32') / float(np.amax(consensus_mask))

        # threshhold and homogenize each channel mask within the mask, label them
        # label when value is above 0.1 (so 90% occupancy), transpose.
        # the [0] is for the array ([1] is the number of regions)
        # It transposes and then transposes again so regions are labeled left to right
        # clear border it to make sure the channels are off the edge
        consensus_mask = segmentation.clear_border(consensus_mask.T > 0.1)
        consensus_mask = ndi.label(consensus_mask)[0].T

        # go through each label
        for label in np.unique(consensus_mask):
            if label == 0: # label zero is the background
                continue
            binary_core = consensus_mask == label

            # clean up the rough edges
            poscols = np.any(binary_core, axis = 0) # column positions where true (any)
            posrows = np.any(binary_core, axis = 1) # row positions where true (any)

            # channel_id givin by horizontal position
            # this is important. later updates to the positions will have to check
            # if their channels contain this median value to match up
            channel_id = int(np.median(np.where(poscols)[0]))

            # store the edge locations of the channel mask in the dictionary
            min_row = np.min(np.where(posrows)[0])
            max_row = np.max(np.where(posrows)[0])
            min_col = np.min(np.where(poscols)[0])
            max_col = np.max(np.where(poscols)[0])

            # if the min/max cols are within the image bounds,
            # add the mask, as 4 points, to the dictionary
            if min_col > 0 and max_col < image_cols:
                channel_masks_1fov[channel_id] = [[min_row, max_row], [min_col, max_col]]

                # find the largest channel width and height while you go round
                max_chnl_mask_len = int(max(max_chnl_mask_len, max_row - min_row))
                max_chnl_mask_wid = int(max(max_chnl_mask_wid, max_col - min_col))

        # add channel_mask dictionary to the fov dictionary, use copy to play it safe
        channel_masks[fov] = channel_masks_1fov.copy()

    # update all channel masks to be the max size
    cm_copy = channel_masks.copy()

    for fov, peaks in channel_masks.iteritems():
        # f_id = int(fov)
        for peak, chnl_mask in peaks.iteritems():
            # p_id = int(peak)
            # just add length to the open end (top of image, low column)
            if chnl_mask[0][1] - chnl_mask[0][0] !=  max_chnl_mask_len:
                cm_copy[fov][peak][0][1] = chnl_mask[0][0] + max_chnl_mask_len
            # enlarge widths around the middle, but make sure you don't get floats
            if chnl_mask[1][1] - chnl_mask[1][0] != max_chnl_mask_wid:
                wid_diff = max_chnl_mask_wid - (chnl_mask[1][1] - chnl_mask[1][0])
                if wid_diff % 2 == 0:
                    cm_copy[fov][peak][1][0] = max(chnl_mask[1][0] - wid_diff/2, 0)
                    cm_copy[fov][peak][1][1] = min(chnl_mask[1][1] + wid_diff/2, image_cols - 1)
                else:
                    cm_copy[fov][peak][1][0] = max(chnl_mask[1][0] - (wid_diff-1)/2, 0)
                    cm_copy[fov][peak][1][1] = min(chnl_mask[1][1] + (wid_diff+1)/2, image_cols - 1)

    return cm_copy

### functions about trimming, padding, and manipulating images
# define function for flipping the images on an FOV by FOV basis
def fix_orientation(image_data):
    '''
    Fix the orientation. The standard direction for channels to open to is down.

    called by
    process_tif
    get_params
    '''

    # user parameter indicates how things should be flipped
    image_orientation = params['image_orientation']

    # if this is just a phase image give in an extra layer so rest of code is fine
    flat = False # flag for if the image is flat or multiple levels
    if len(image_data.shape) == 2:
        image_data = np.expand_dims(image_data, 0)
        flat = True

    # setting image_orientation to 'auto' will use autodetection
    if image_orientation == "auto":
        # Pick the plane to analyze with the highest mean px value (should be phase)
        ph_channel = np.argmax([np.mean(image_data[ci]) for ci in range(image_data.shape[0])])

        # flip based on the index of the higest average row value
        # this should be closer to the opening
        if np.argmax(image_data[ph_channel].mean(axis = 1)) < image_data[ph_channel].shape[0] / 2:
            image_data = image_data[:,::-1,:]
        else:
            pass # no need to do anything

    # flip if up is chosen
    elif image_orientation == "up":
        return image_data[:,::-1,:]

    # do not flip the images if "down is the specified image orientation"
    elif image_orientation == "down":
        pass

    if flat:
        image_data = image_data[0] # just return that first layer

    return image_data

# cuts out channels from the image
def cut_slice(image_data, channel_loc):
    '''Takes an image and cuts out the channel based on the slice location
    slice location is the list with the peak information, in the form
    [][y1, y2],[x1, x2]]. Returns the channel slice as a numpy array.
    The numpy array will be a stack if there are multiple planes.

    if you want to slice all the channels from a picture with the channel_masks
    dictionary use a loop like this:

    for channel_loc in channel_masks[fov_id]: # fov_id is the fov of the image
        channel_slice = cut_slice[image_pixel_data, channel_loc]
        # ... do something with the slice

    NOTE: this function will try to determine what the shape of your
    image is and slice accordingly. It expects the images are in the order
    [t, x, y, c]. It assumes images with three dimensions are [x, y, c] not
    [t, x, y].
    '''

    # case where image is in form [x, y]
    if len(image_data.shape) == 2:
        # make slice object
        channel_slicer = np.s_[channel_loc[0][0]:channel_loc[0][1],
                               channel_loc[1][0]:channel_loc[1][1]]

    # case where image is in form [x, y, c]
    elif len(image_data.shape) == 3:
        channel_slicer = np.s_[channel_loc[0][0]:channel_loc[0][1],
                               channel_loc[1][0]:channel_loc[1][1],:]

    # case where image in form [t, x , y, c]
    elif len(image_data.shape) == 4:
        channel_slicer = np.s_[:,channel_loc[0][0]:channel_loc[0][1],
                                 channel_loc[1][0]:channel_loc[1][1],:]

    # slice based on appropriate slicer object.
    channel_slice = image_data[channel_slicer]

    return channel_slice

# remove margins of zeros from 2d numpy array
def trim_zeros_2d(array):
    # make the array equal to the sub array which has columns of all zeros removed
    # "all" looks along an axis and says if all of the valuse are such and such for each row or column
    # ~ is the inverse operator
    # using logical indexing
    array = array[~np.all(array == 0, axis = 1)]
    # transpose the array
    array = array.T
    # make the array equal to the sub array which has columns of all zeros removed
    array = array[~np.all(array == 0, axis = 1)]
    # transpose the array again
    array = array.T
    # return the array
    return array

# calculat cross correlation between pixels in channel stack
def channel_xcorr(channel_filepath):
    '''
    Function calculates the cross correlation of images in a
    stack to the first image in the stack. The output is an
    array that is the length of the stack with the best cross
    correlation between that image and the first image.

    The very first value should be 1.
    '''

    # load up the stack. should be 4D [t, x, y, c]
    with tiff.TiffFile(channel_filepath) as tif:
        image_data = tif.asarray()

    # just use the first plane, which should be the phase images
    if len(image_data.shape) > 3: # if there happen to be multiple planes
        image_data = image_data[:,:,:,0]

    # if there are more than 100 images, use 100 images evenly
    # spaced across the range
    if image_data.shape[0] > 100:
        spacing = int(image_data.shape[0] / 100)
        image_data = image_data[::spacing,:,:]
        if image_data.shape[0] > 100:
            image_data = image_data[:100,:,:]

    # we will compare all images to this one, needs to be padded to account for image drift
    first_img = np.pad(image_data[0,:,:], 10, mode='reflect')

    xcorr_array = [] # array holds cross correlation vaues
    for img in image_data:
        # use match_template to find all cross correlations for the
        # current image against the first image.
        xcorr_array.append(np.max(match_template(first_img, img)))

    return xcorr_array

### functions about subtraction
# worker function for doing subtraction
# average empty channels from stacks, making another TIFF stack
def average_empties_stack(fov_id, specs):
    '''Takes the fov file name and the peak names of the designated empties,
    averages them and saves the image

    Parameters
    fov_id : int
        FOV number
    specs : dict
        specifies whether a channel should be analyzed (1), used for making
        an average empty (0), or ignored (-1).

    Returns
        True if succesful.
        Saves empty stack to analysis folder

    '''

    information("Creating average empty channel for FOV %d." % fov_id)

    # directories for saving
    chnl_dir = params['experiment_directory'] + params['analysis_directory'] + 'channels/'
    empty_dir = params['experiment_directory'] + params['analysis_directory'] + 'empties/'

    # get peak ids of empty channels for this fov
    empty_peak_ids = []
    for peak_id, spec in specs[fov_id].items():
        if spec == 0: # 0 means it should be used for empty
            empty_peak_ids.append(peak_id)
    empty_peak_ids = sorted(empty_peak_ids) # sort for repeatability

    # depending on how many empties there are choose what to do
    # if there is no empty the user is going to have to copy another empty stack
    if len(empty_peak_ids) == 0:
        information("No empty channel designated for FOV %d." % fov_id)
        return False

    # if there is just one then you can just copy that channel
    elif len(empty_peak_ids) == 1:
        peak_id = empty_peak_ids[0]
        information("One empty channel (%d) designated for FOV %d." % (peak_id, fov_id))

        # copy that tiff stack with a new name as empty
        # channel_filename = params['experiment_name'] + '_xy%03d_p%04d.tif' % (fov_id, peak_id)
        channel_filename = params['experiment_name'] + '_xy%03d_p%04d_c0.tif' % (fov_id, peak_id)
        channel_filepath = chnl_dir + channel_filename

        with tiff.TiffFile(channel_filepath) as tif:
            avg_empty_stack = tif.asarray()

        # get just the phase data if it is multidimensional
        if len(avg_empty_stack.shape) > 3:
            avg_empty_stack = avg_empty_stack[:,:,:,0]

    # but if there is more than one empty you need to align and average them per timepoint
    elif len(empty_peak_ids) > 1:
        # load the image stacks into memory
        empty_stacks = [] # list which holds phase image stacks of designated empties
        for peak_id in empty_peak_ids:
            # load stack
            channel_filename = params['experiment_name'] + '_xy%03d_p%04d_c0.tif' % (fov_id, peak_id)
            channel_filepath = chnl_dir + channel_filename
            with tiff.TiffFile(channel_filepath) as tif:
                image_data = tif.asarray()

            # just get phase data and put it in list
            if len(image_data.shape) > 3:
                image_data = image_data[:,:,:,0]
            empty_stacks.append(image_data)

        information("%d empty channels designated for FOV %d." % (len(empty_stacks), fov_id))

        # go through time points and create list of averaged empties
        avg_empty_stack = [] # list will be later concatentated into numpy array
        time_points = range(image_data.shape[0]) # index is time
        for t in time_points:
            # get images from one timepoint at a time and send to alignment and averaging
            imgs = [stack[t] for stack in empty_stacks]
            avg_empty = average_empties(imgs) # function is in mm3
            avg_empty_stack.append(avg_empty)

        # concatenate list and then save out to tiff stack
        avg_empty_stack = np.stack(avg_empty_stack, axis=0)

    # save out data
    # make new name
    empty_filename = params['experiment_name'] + '_xy%03d_empty.tif' % fov_id
    empty_filepath = empty_dir + empty_filename
    tiff.imsave(empty_filepath, avg_empty_stack, compress=1) # save it
    information("Saved empty channel %s." % empty_filename)

    return True

# averages a list of empty channels
def average_empties(imgs):
    '''
    This function averages a set of images (empty channels) and returns a single image
    of the same size. It first aligns the images to the first image before averaging.

    Alignment is done by enlarging the first image using edge padding.
    Subsequent images are then aligned to this image and the offset recorded.
    These images are padded such that they are the same size as the first (padde) image but
    with the image in the correct (aligned) place. Edge padding is again used.
    The images are then placed in a stack and aveaged. This image is trimmed so it is the size
    of the original images

    Called by
    average_empties_stack

    '''

    aligned_imgs = [] # list contains the alingned, padded images
    pad_size = 10 # pixel size to use for padding (ammount that alignment could be off)

    for n, img in enumerate(imgs):
        # if this is the first image, pad it and add it to the stack
        if n == 0:
            ref_img = np.pad(img, pad_size, mode='reflect') # padded reference image
            aligned_imgs.append(ref_img)

        # otherwise align this image to the first padded image
        else:
            # find correlation between a convolution of img against the padded reference
            match_result = match_template(ref_img, img)

            # find index of highest correlation (relative to top left corner of img)
            y, x = np.unravel_index(np.argmax(match_result), match_result.shape)

            # pad img so it aligns and is the same size as reference image
            pad_img = np.pad(img, ((y, ref_img.shape[0] - (y + img.shape[0])),
                                   (x, ref_img.shape[1] - (x + img.shape[1]))), mode='reflect')
            aligned_imgs.append(pad_img)

    # stack the aligned data along 3rd axis
    aligned_imgs = np.dstack(aligned_imgs)
    # get a mean image along 3rd axis
    avg_empty = np.nanmean(aligned_imgs, axis=2)
    # trim off the padded edges
    avg_empty = avg_empty[pad_size:-1*pad_size, pad_size:-1*pad_size]
    # change type back to unsigned 16 bit not floats
    avg_empty = avg_empty.astype(dtype='uint16')

    return avg_empty

# Do subtraction for an fov over many timepoints
def subtract_fov_stack(fov_id, specs):
    '''
    For a given FOV, loads the precomputed empty stack and does subtraction on
    all peaks in the FOV designated to be analyzed


    Called by
    mm3_Subtract.py

    Calls
    mm3.subtract_phase

    '''

    information('Subtracting peaks for FOV %d.' % fov_id)

    # directories for saving
    chnl_dir = params['experiment_directory'] + params['analysis_directory'] + 'channels/'
    empty_dir = params['experiment_directory'] + params['analysis_directory'] + 'empties/'
    sub_dir = params['experiment_directory'] + params['analysis_directory'] + 'subtracted/'

    # load the empty stack
    empty_filename = params['experiment_name'] + '_xy%03d_empty.tif' % fov_id
    empty_filepath = empty_dir + empty_filename
    with tiff.TiffFile(empty_filepath) as tif:
        avg_empty_stack = tif.asarray()

    # determine which peaks are to be analyzed
    ana_peak_ids = []
    for peak_id, spec in specs[fov_id].items():
        if spec == 1: # 0 means it should be used for empty, -1 is ignore
            ana_peak_ids.append(peak_id)
    ana_peak_ids = sorted(ana_peak_ids) # sort for repeatability
    information("Subtracting %d channels for FOV %d." % (len(ana_peak_ids), fov_id))

    # load images for the peak and get phase images
    for peak_id in ana_peak_ids:
        information('Subtracting peak %d.' % peak_id)

        # channel_filename = params['experiment_name'] + '_xy%03d_p%04d.tif' % (fov_id, peak_id)
        channel_filename = params['experiment_name'] + '_xy%03d_p%04d_c0.tif' % (fov_id, peak_id)
        channel_filepath = chnl_dir + channel_filename
        with tiff.TiffFile(channel_filepath) as tif:
            image_data = tif.asarray()

        if len(image_data.shape) > 3:
            image_data = image_data[:,:,:,0] # just get phase data and put it in list

        # make a list for all time points to send to a multiprocessing pool
        # list will length of image_data with tuples (image, empty)
        subtract_pairs = zip(image_data, avg_empty_stack)

        # set up multiprocessing pool to do subtraction. Should wait until finished
        pool = Pool(processes=params['num_analyzers'])

        subtracted_imgs = pool.map(subtract_phase, subtract_pairs, chunksize=10)

        pool.close() # tells the process nothing more will be added.
        pool.join() # blocks script until everything has been processed and workers exit

        # stack them up along a time axis
        subtracted_stack = np.stack(subtracted_imgs, axis=0)

        # save out the subtracted stack
        sub_filename = params['experiment_name'] + '_xy%03d_p%04d_sub.tif' % (fov_id, peak_id)
        sub_filepath = sub_dir + sub_filename
        tiff.imsave(sub_filepath, subtracted_stack, compress=1) # save it
        information("Saved subtracted channel %s." % sub_filename)

    return True

# subtracts one image from another.
def subtract_phase(image_pair):
    '''subtract_phase aligns and subtracts a .
    Modified from subtract_phase_only by jt on 20160511
    The subtracted image returned is the same size as the image given. It may however include
    data points around the edge that are meaningless but not marked.

    We align the empty channel to the phase channel, then subtract.

    Parameters
    image_pair : tuple of length two with; (image, empty_mean)

    Returns
    (subtracted_image, offset) : tuple with the subtracted_image as well as the ammount it
        was shifted to be aligned with the empty. offset = (x, y), negative or positive
        px values.

    Called by
    subtract_fov_stack
    '''
    # get out data and pad
    cropped_channel, empty_channel = image_pair # [channel slice, empty slice]

    ### Pad cropped channel.
    pad_size = 10 # pixel size to use for padding (ammount that alignment could be off)
    padded_chnl = np.pad(cropped_channel, pad_size, mode='reflect')

    # ### Align channel to empty using match template.
    # use match template to get a correlation array and find the position of maximum overlap
    match_result = match_template(padded_chnl, empty_channel)
    # get row and colum of max correlation value in correlation array
    y, x = np.unravel_index(np.argmax(match_result), match_result.shape)

    # pad the empty channel according to alignment to be overlayed on padded channel.
    empty_paddings = [[y, padded_chnl.shape[0] - (y + empty_channel.shape[0])],
                      [x, padded_chnl.shape[1] - (x + empty_channel.shape[1])]]
    aligned_empty = np.pad(empty_channel, empty_paddings, mode='reflect')
    # now trim it off so it is the same size as the original channel
    aligned_empty = aligned_empty[pad_size:-1*pad_size, pad_size:-1*pad_size]

    ### Compute the difference between the empty and channel phase contrast images
    # subtract cropped cell image from empty channel.
    channel_subtracted = aligned_empty.astype('int32') - cropped_channel.astype('int32')

    # just zero out anything less than 0. This is what Sattar does
    channel_subtracted[channel_subtracted < 0] = 0
    channel_subtracted = channel_subtracted.astype('uint16') # change back to 16bit

    return channel_subtracted

### functions that deal with segmentation
def segment_image(image):
    '''Segments a subtracted image and returns a labeled image

    Parameters
    image : a ndarray which is an image. This should be the subtracted image

    Returns
    labeled_image : a ndarray which is also an image. Labeled values, which
        should correspond to cells, all have the same integer value starting with 1.
        Non labeled area should have value zero.
    '''

    # threshold image
    thresh = threshold_otsu(image) # finds optimal OTSU thershhold value
    threshholded = image > thresh # will create binary image

    # tool for morphological transformations
    tool = morphology.disk(2)

    # Opening = erosion then dialation.
    # opening smooths images, breaks isthmuses, and eliminates protrusions.
    # "opens" dark gaps between bright features.
    morph = morphology.binary_opening(image > thresh, tool) # threshhold and then use tool

    # Here are the above steps one by one for debugging
    # eroded = binary_erosion(threshholded, tool)
    # dilated = binary_dilation(eroded, tool)
    # opened = np.zeros_like(dilated)
    # opened[:] = dilated

    # Erode again to help break cells touching at end
    # morph = morphology.binary_erosion(morph, morphology.disk(1))

    # zero out rows that have very few pixels
    # widens or creates gaps between cells
    # sum of rows (how many pixels are occupied in each row)
    line_profile = np.sum(morph, axis=1)
    # find highest value, aka width of fattest cell
    max_width = max(line_profile)
    # find indexes of rows where sum is less than 1/5th of this value.
    zero_these_indicies = np.all([line_profile < (max_width/5), line_profile > 0], axis=0)
    zero_these_indicies = np.where(zero_these_indicies)
    # zero out those rows
    morph[zero_these_indicies] = 0

    # here is the method based on watershedding
    # # label image regions to create markers for watershedding
    # # connectivity=1 means the pixels have to be next to eachother (not diagonal)
    # # return_num=True means return the number of labels, useful for checking
    # markers, label_num = morphology.label(morph, connectivity=1, return_num=True)
    #
    # # remove artifacts connected to image border
    # segmentation.clear_border(markers, in_place=True)
    # # remove small objects
    # if label_num > 1: # use conditional becaues it warns if there is only one label.
    #     # the minsize here may need to be a function of the magnification.
    #     morphology.remove_small_objects(markers, min_size=100, in_place=True)
    #
    # # watershed. Markers are where to start.
    # # mask means to not watershed outside of the OTSU threshhold
    # #labeled_image = morphology.watershed(image, markers, mask=threshholded)
    # labeled_image = markers

    ### here is the method based on the diffusion algorithm
    # Generate the markers based on distance to the background
    distance = ndi.distance_transform_edt(morph)
    # here we zero anything less than 3 pixels from the boarder
    distance[distance < 2] = 0
    # anything that is left make a 1
    distance[distance > 1] = 1
    distance = distance.astype('int8') # convert (distance is actually a matrix of floats)

    # remove small objects
    # remove artifacts connected to image border
    cleared = segmentation.clear_border(distance)
    # remove small objects. Note how we are relabeling here, as remeove_small_objects
    # wants a labeled image, and only works if there is more than one label
    cleared, label_num = morphology.label(cleared, connectivity=1, return_num=True)
    if label_num > 1:
        cleared = morphology.remove_small_objects(cleared, min_size=50)

    # relabel now that small objects have been removed
    markers = morphology.label(cleared)
    # set anything outside of OTSU threshold to -1 so it will not be labeled
    markers[threshholded == 0] = -1
    # label using the random walker (diffusion watershed) algorithm
    labeled_image = segmentation.random_walker(image, markers)
    # put negative values back to zero for proper image
    labeled_image[labeled_image == -1] = 0

    return labeled_image

# Do segmentation for an channel time stack
def segment_chnl_stack(fov_id, peak_id):
    '''
    For a given fov and peak (channel), do segmentation for all images in the
    subtracted .tif stack.

    Called by
    mm3_Segment.py

    Calls
    mm3.segment_image
    '''

    information('Segmenting FOV %d, channel %d.' % (fov_id, peak_id))

    # directories for loading and saving images
    sub_dir = params['experiment_directory'] + params['analysis_directory'] + 'subtracted/'
    seg_dir = params['experiment_directory'] + params['analysis_directory'] + 'segmented/'

    # load the subtracted stack
    sub_filename = params['experiment_name'] + '_xy%03d_p%04d_sub.tif' % (fov_id, peak_id)
    sub_filepath = sub_dir + sub_filename
    with tiff.TiffFile(sub_filepath) as tif:
        sub_stack = tif.asarray()

    # set up multiprocessing pool to do segmentation. Will do everything before going on.
    pool = Pool(processes=params['num_analyzers'])

    # send the 3d array to multiprocessing
    segmented_imgs = pool.map(segment_image, sub_stack, chunksize=10)

    pool.close() # tells the process nothing more will be added.
    pool.join() # blocks script until everything has been processed and workers exit

    # # image by image for debug
    # segmented_imgs = []
    # segmented_imgs.append(segment_image(sub_stack[0]))
    # segmented_imgs.append(segment_image(sub_stack[1]))

    # stack them up along a time axis
    segmented_imgs = np.stack(segmented_imgs, axis=0)

    # save out the subtracted stack
    seg_filename = params['experiment_name'] + '_xy%03d_p%04d_seg.tif' % (fov_id, peak_id)
    seg_filepath = seg_dir + seg_filename
    tiff.imsave(seg_filepath, segmented_imgs.astype('uint16'), compress=1) # save it
    information("Saved segmented channel %s." % seg_filename)

    return True

### functions about converting dates and times
### Functions
def days_to_hmsm(days):
    hours = days * 24.
    hours, hour = math.modf(hours)
    mins = hours * 60.
    mins, min = math.modf(mins)
    secs = mins * 60.
    secs, sec = math.modf(secs)
    micro = round(secs * 1.e6)
    return int(hour), int(min), int(sec), int(micro)

def hmsm_to_days(hour=0, min=0, sec=0, micro=0):
    days = sec + (micro / 1.e6)
    days = min + (days / 60.)
    days = hour + (days / 60.)
    return days / 24.

def date_to_jd(year,month,day):
    if month == 1 or month == 2:
        yearp = year - 1
        monthp = month + 12
    else:
        yearp = year
        monthp = month
    # this checks where we are in relation to October 15, 1582, the beginning
    # of the Gregorian calendar.
    if ((year < 1582) or
        (year == 1582 and month < 10) or
        (year == 1582 and month == 10 and day < 15)):
        # before start of Gregorian calendar
        B = 0
    else:
        # after start of Gregorian calendar
        A = math.trunc(yearp / 100.)
        B = 2 - A + math.trunc(A / 4.)
    if yearp < 0:
        C = math.trunc((365.25 * yearp) - 0.75)
    else:
        C = math.trunc(365.25 * yearp)
    D = math.trunc(30.6001 * (monthp + 1))
    jd = B + C + D + day + 1720994.5
    return jd

def datetime_to_jd(date):
    days = date.day + hmsm_to_days(date.hour,date.minute,date.second,date.microsecond)
    return date_to_jd(date.year, date.month, days)
