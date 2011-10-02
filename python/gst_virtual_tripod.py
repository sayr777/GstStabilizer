#!/usr/bin/env python
import glib,gobject,gst,sys

from itertools import izip

import numpy, math, array

import cPickle
from collections import deque

import cv


MAX_COUNT = 20

#
# Algo idea:
#
# - get features of img0
# - get optical flow (2 set of corresponding coordinates)
# - compute homography from these
#

# For now, application/x-motion-flow is a pickle format 2 of either None or a
# tuple containing two lists. Each list (let's call them c0 and c1) contain
# 2-tuples representing coordinates of a point, such that c0[i] is the (x,y)
# coordinate of a feature in the previous frame, and c1[i] is the (x,y)
# coordinate of the same feature in the current frame.


def img_of_buf(buf):
    if buf is None:
        return None
    struct = buf.caps[0]
    width = struct['width']
    height = struct['height']
    if struct.has_field('bpp'):
        # yeah, we only support 8 bits per channel
        channels = struct['bpp'] / 8
        depth = 8
    img = cv.CreateImageHeader((width, height), depth, channels);
    cv.SetData (img, buf.data)
    return img

def buf_of_img(img, bufmodel=None):
    buf = gst.Buffer(img.tostring())
    if bufmodel is not None:
        buf.caps = bufmodel.caps
        buf.duration = bufmodel.duration
        buf.timestamp = bufmodel.timestamp
        buf.offset = bufmodel.offset
        buf.offset_end = bufmodel.offset_end
    return buf


class OpticalFlowFinder(gst.Element):
    __gstdetails__ = ("Optical flow finder",
                    "Filter/Video",
                    "find optical flow between frames",
                    "Guillaume Emont")
    sink_template = gst.PadTemplate ("sink",
                                   gst.PAD_SINK,
                                   gst.PAD_ALWAYS,
                                   gst.Caps('video/x-raw-gray,depth=8'))

    src_template = gst.PadTemplate ("source",
                                     gst.PAD_SRC,
                                     gst.PAD_ALWAYS,
                                     gst.Caps('application/x-motion-flow'))

    __gsttemplates__ = (sink_template, src_template)

    PICKLE_FORMAT = 2

    corner_count = gobject.property(type=int,
                                 default=MAX_COUNT,
                                 blurb='number of corners to detect')
    corner_quality_level = gobject.property(type=float,
                                            default=0.1,
                                            blurb='Multiplier for the max/min eigenvalue; specifies the minimal accepted quality of image corners')
    corner_min_distance = gobject.property(type=int,
                                           default=200,
                                           blurb='Limit, specifying the minimum possible distance between the detected corners; Euclidian distance is used')
    win_size = gobject.property(type=int,
                                default=30,
                                blurb='Size of the search window of each pyramid level')
    pyramid_level = gobject.property(type=int,
                                     default=4,
                                     blurb='Maximal pyramid level number. If 0 , pyramids are not used (single level), if 1 , two levels are used, etc')
    max_iterations = gobject.property(type=int,
                                      default=50,
                                      blurb='maximum number of iterations to calculate optical flow')
    epsilon = gobject.property(type=float,
                                    default=0.001,
                                    blurb='terminate when we reach that difference or smaller')

    ignore_box_min_x = gobject.property(type=int,
                                        default=-1,
                                        blurb='left limit of the ignore box, deactivated if -1')
    ignore_box_max_x = gobject.property(type=int,
                                        default=-1,
                                        blurb='right limit of the ignore box, deactivated if -1')
    ignore_box_min_y = gobject.property(type=int,
                                        default=-1,
                                        blurb='top limit of the ignore box, deactivated if -1')
    ignore_box_max_y = gobject.property(type=int,
                                        default=-1,
                                        blurb='top limit of the ignore box, deactivated if -1')

    def __init__(self):
        gst.Element.__init__(self)

        self.sinkpad = gst.Pad(self.sink_template)
        self.sinkpad.set_chain_function(self.chain)
        self.add_pad(self.sinkpad)

        self.srcpad = gst.Pad(self.src_template)
        self.add_pad(self.srcpad)

        self._previous_frame = None

        self._mask = None


    def chain(self, pad, buf):

        if self._mask is None and self._has_ignore_box():
            # we consider the buffer height and width are constant, the whole
            # algorithm depends on it anyway. Shouldn't we enforce that somewhere?
            caps_struct = buf.get_caps()[0]
            height = caps_struct['height']
            width = caps_struct['width']
            self._mask = cv.CreateMatHeader(height, width, cv.CV_8UC1)
            data = array.array('B', '\1' * width * height)
            for x in xrange(self.ignore_box_min_x, self.ignore_box_max_x + 1):
                for y in xrange(self.ignore_box_min_y, self.ignore_box_max_y + 1):
                    data[y*height + x] = 0
            cv.SetData(self._mask, data.tostring())

        flow = self.optical_flow(self._previous_frame, buf)

        pickled_flow = cPickle.dumps(flow, self.PICKLE_FORMAT)
        new_buf = gst.Buffer (pickled_flow)
        new_buf.stamp(buf)

        self._previous_frame = buf

        return self.srcpad.push(new_buf)

    def _features(self, img):
        img_size = cv.GetSize(img)
        eigImage = cv.CreateImage(img_size, cv.IPL_DEPTH_8U, 1)
        tempImage = cv.CreateImage(img_size, cv.IPL_DEPTH_8U, 1)

        mask = None
        features = cv.GoodFeaturesToTrack(img, eigImage, tempImage,
                                          self.corner_count, #number of corners to detect
                                          self.corner_quality_level, #Multiplier for the max/min
                                                #eigenvalue; specifies the minimal
                                                #accepted quality of image corners
                                          self.corner_min_distance, # minimum distance between returned corners
                                          mask=self._mask
                                          )

        return cv.FindCornerSubPix(img, features, (10, 10), (-1, -1),
                                   (cv.CV_TERMCRIT_ITER | cv.CV_TERMCRIT_EPS,
                                    20, 0.03))

    def _filter_features(self, features, flter, errors):
        #filtered_errors = [err for (err, status) in zip(errors, flter) if status]
        #n = len(filtered_errors) / 2 # we want to keep the best third only
        #admissible_error = sorted(filtered_errors)[n]
        return [ p for (status,p, err) in zip(flter, features, errors)
                    if status and not self._in_ignore_box(p)] # and err < admissible_error]

    def _has_ignore_box(self):
        return (-1) not in (self.ignore_box_min_x, self.ignore_box_max_x,
                            self.ignore_box_min_y, self.ignore_box_max_y)

    def _in_ignore_box(self, (x, y)):
        return self._has_ignore_box() and x >= self.ignore_box_min_x and x <= self.ignore_box_max_x and y >= self.ignore_box_min_y and y <= self.ignore_box_max_y

    def optical_flow(self, buf0, buf1):
        """
        Return two sets of coordinates (c0, c1), in buf0 and buf1 respectively,
        such that c1[i] is the position in buf1 of the feature that is at c0[i]
        in buf0.
        """
        if buf0 is None:
            return None

        img0 = img_of_buf(buf0)
        img1 = img_of_buf(buf1)

        corners0 = self._features(img0)

        print "found %d features" % len(corners0)

        corners1, status, track_errors = cv.CalcOpticalFlowPyrLK (
                     img0, img1, None, None,
                     corners0,
                     (self.win_size,) * 2, # win size
                     self.pyramid_level, # pyramid level
                     (cv.CV_TERMCRIT_ITER|cv.CV_TERMCRIT_EPS, # stop type
                      self.max_iterations, # max iterations
                      self.epsilon), # min accuracy
                     0) # flags

        corners0 = self._filter_features(corners0, status, track_errors)
        corners1 = self._filter_features(corners1, status, track_errors)

        print "corners returned: %d, %d"  % (len(corners0), len(corners1))

        return (corners0, corners1)


gobject.type_register (OpticalFlowFinder)
ret = gst.element_register (OpticalFlowFinder, 'opticalflowfinder')

class OpticalFlowMuxer(gst.Element):
    """
    Base class for "muxers" of an optical flow stream and another stream having
    the same timestamps.
    When subclassing, you should implement mux and redefine main_sink_template
    """

    flow_sink_template = gst.PadTemplate ("flowsink",
                                           gst.PAD_SINK,
                                           gst.PAD_ALWAYS,
                                           gst.Caps('application/x-motion-flow'))

    # should be defined as a proper pad template in the subclass
    main_sink_template = None

    def __init__(self):
        gst.Element.__init__(self)

        self.flow_sink_pad = gst.Pad(self.flow_sink_template)
        self.flow_sink_pad.set_chain_function(self._chain)
        self.add_pad(self.flow_sink_pad)

        self.main_sink_pad = gst.Pad(self.main_sink_template)
        self.main_sink_pad.set_chain_function(self._chain)
        self.add_pad(self.main_sink_pad)

        # FIXME: shouldn't we just use gstreamer queues outside of the elemnt?
        self._pending_flow = deque()
        self._pending_main = deque()

    def mux(self, buf, flow):
        raise NotImplementedError("This method needs to be implemented in a subclass")

    def _chain(self, pad, buf):
        if pad == self.flow_sink_pad:
            self._pending_flow.append(buf)
        else: # self.main_sink_pad
            self._pending_main.append(buf)
        return self._try_mux()

    def _try_mux(self):
        if self._pending_flow and self._pending_main:
            flow_buf = self._pending_flow.popleft()
            flow = cPickle.loads(flow_buf.data)
            buf = self._pending_main.popleft()
            if buf.timestamp == flow_buf.timestamp:
                return self.mux(buf, flow)
            else:
                print "All going wrong!"
                return gst.FLOW_ERROR

        return gst.FLOW_OK


class ArrowDrawer(object):
    alpha = math.pi / 6.
    cos_alpha = math.cos(alpha)
    sin_alpha = math.sin(alpha)

    def draw_arrows(self, img, origins, ends):
        for origin, end in zip(origins, ends):
            self.draw_arrow(img, origin, end)

    def draw_arrow(self, img, origin, end):
        color = (255, 0, 0)
        width = 2
        def int_pos((x,y)):
            return (int(x), int(y))
        origin = int_pos(origin)
        end = int_pos(end)
        cv.Line(img, origin, end, color, width)
        points = self._compute_arrow_points(origin, end)
        if points is None:
            return
        C, D = points
        cv.Line(img, end, C, color, width)
        cv.Line(img, end, D, color, width)

    def _compute_arrow_points(self, (xa, ya), (xb, yb), length=20.):
        # The arrow tip is made by joining B (xb, yb) to C and D. This method
        # computes the coordinates of C and D.
        ab_distance = math.sqrt( (xb - xa)**2 + (yb-ya)**2)
        if ab_distance == 0.:
            return None
        cos_beta = (xa - xb) / ab_distance
        sin_beta = (ya - yb) / ab_distance
        cos_alpha = self.cos_alpha
        sin_alpha = self.sin_alpha
        xc = xb + length * (cos_alpha * cos_beta - sin_alpha * sin_beta)
        yc = yb + length * (sin_beta * cos_alpha + sin_alpha * cos_beta)
        xd = xb + length * (cos_alpha * cos_beta + sin_alpha * sin_beta)
        yd = yb + length * (sin_beta * cos_alpha - sin_alpha * cos_beta)

        return ((int(xc), int(yc)), (int(xd), int(yd)))


class OpticalFlowDrawer(OpticalFlowMuxer):
    __gstdetails__ = ("Optical flow drawer",
                    "Filter/Video",
                    "draw optical flow on frames",
                    "Guillaume Emont")
    main_sink_template = gst.PadTemplate ("mainsink",
                                          gst.PAD_SINK,
                                          gst.PAD_ALWAYS,
                                          gst.Caps('video/x-raw-rgb,depth=24'))
    src_template = gst.PadTemplate("src",
                                    gst.PAD_SRC,
                                    gst.PAD_ALWAYS,
                                    gst.Caps('video/x-raw-rgb,depth=24'))
    __gsttemplates__ = (OpticalFlowMuxer.flow_sink_template,
                        main_sink_template,
                        src_template)


    def __init__(self, *args, **kw):
        super(OpticalFlowDrawer, self).__init__(*args, **kw)

        self.srcpad = gst.Pad(self.src_template)
        self.add_pad(self.srcpad)


        self._drawer = ArrowDrawer()


    def mux(self, buf, flow):
        if flow is None:
            return self.srcpad.push(buf)
        origins, ends = flow

        img = img_of_buf(buf)

        self._drawer.draw_arrows(img, origins, ends)

        new_buf = buf_of_img(img, bufmodel=buf)

        return self.srcpad.push(new_buf)


gobject.type_register (OpticalFlowDrawer)
ret = gst.element_register (OpticalFlowDrawer, 'opticalflowdrawer')
