#!/usr/bin/env python
#
# Copyright 2011 Igalia S.L. and Guillaume Emont
# Contact: Guilaume Emont <guijemont@igalia.com>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import gobject,gst

import cv2
import numpy

from cv_gst_util import *

from flow_muxer import OpticalFlowMuxer

from cv_flow_finder import LucasKanadeFinder, SURFFinder


class OpticalFlowCorrector(gst.Element):
    __gstdetails__ = ("Optical flow corrector",
                    "Filter/Video",
                    "Correct frames according to global optical flow so as to invert it ('stabilise' images)",
                    "Guillaume Emont")
    sink_template = gst.PadTemplate ("sink",
                                      gst.PAD_SINK,
                                      gst.PAD_ALWAYS,
                                      gst.Caps('video/x-raw-rgb,depth=24'))
    src_template = gst.PadTemplate("src",
                                    gst.PAD_SRC,
                                    gst.PAD_ALWAYS,
                                    gst.Caps('video/x-raw-rgb,depth=24'))
    __gsttemplates__ = (sink_template, src_template)

    # Algorithms to chose from:
    LUCAS_KANADE = 1
    SURF = 2

    corner_count = gobject.property(type=int,
                                 default=50,
                                 blurb='number of corners to detect')
    corner_quality_level = gobject.property(type=float,
                                            default=0.1,
                                            blurb='Multiplier for the max/min eigenvalue; specifies the minimal accepted quality of image corners')
    corner_min_distance = gobject.property(type=int,
                                           default=50,
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
    algorithm = gobject.property(type=int,
                                 default=LUCAS_KANADE,
                                 blurb= """algorithm to use:
                                 %d: Lucas Kanade (discreet, fast, precise, not good for big changes between frames)
                                 %d: SURF (Speeded Up Robust Feature, finds features, finds them again)""" % (LUCAS_KANADE, SURF))
    multiply_transforms = gobject.property(type=bool,
                                           default=False,
                                           blurb='whether to multiply transform matrices, or to compare transformed images instead)')

    def __init__(self, *args, **kw):
        super(OpticalFlowCorrector, self).__init__(*args, **kw)

        self.srcpad = gst.Pad(self.src_template)
        self.add_pad(self.srcpad)

        self.sinkpad = gst.Pad(self.sink_template)
        self.sinkpad.set_chain_function(self._chain)
        self.add_pad(self.sinkpad)

        self._reference_img = None
        self._reference_blob = None
        self._last_output_img = None
        self._reference_transform = numpy.asarray([[1., 0., 0.],
                                                   [0., 1., 0.],
                                                   [0., 0., 1.]],
                                                   dtype=numpy.float128)

        self._finder = None

    def _create_finder(self):

        if self.algorithm == self.LUCAS_KANADE:
            finder = LucasKanadeFinder(self.corner_count,
                                             self.corner_quality_level,
                                             self.corner_min_distance,
                                             self.win_size,
                                             self.pyramid_level,
                                             self.max_iterations,
                                             self.epsilon)
        elif self.algorithm == self.SURF:
            finder = SURFFinder()
        else:
            raise ValueError("Unknown algorithm")
        return finder

    def _chain(self, pad, buf):
        if self._reference_img is None:
            self._reference_img = img_of_buf(buf)
            self._last_output_img = self._reference_img
            self._reference_blob = None
            return self.srcpad.push(buf)

        if self._finder is None:
            self._finder = self._create_finder()

        print "-- buf timestamp: %.4f" % (buf.timestamp/float(gst.SECOND))

        flow,blob = self._get_flow(buf)
        if flow is None:
            return self.srcpad.push(buf)

        try:
            transform = self._perspective_transform_from_flow(flow)

            if self.props.multiply_transforms:
                # since we get the flow between original frames, we need to
                # accumulate the transformations
                 self._reference_transform = \
                    transform.dot(self._reference_transform)
            else:
                self._reference_transform = transform

            img = img_of_buf(buf)

            new_img = self._last_output_img.copy()
            
            new_img = cv2.warpPerspective(img,
                                          numpy.asarray(self._reference_transform,
                                                        dtype=numpy.float64),
                                          (img.shape[1], img.shape[0]),
                                          dst=new_img,
                                          flags=cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_TRANSPARENT)

            new_buf = buf_of_img(new_img, bufmodel=buf)
            if self.props.multiply_transforms:
                self._reference_img = img
                self._reference_blob = blob
            else:
                self._reference_img = new_img
                self._reference_blob = self._finder.warp_blob(blob, transform)
            self._last_output_img = new_img
            return self.srcpad.push(new_buf)
        except cv2.error,e :
            print "got an opencv error (%s), not applying any transform for this frame" % e.message
            self._reference_img = img_of_buf(buf)
            self._reference_blob = None
            return self.srcpad.push(buf)

    def _perspective_transform_from_flow(self, (points0, points1)):
        # Ransac and its threshold allow us to easily weed out outliers.
        transform, mask = cv2.findHomography(points0, points1,
                                             method=cv2.RANSAC,
                                             ransacReprojThreshold=3)
        return transform

    def _get_flow(self, buf):

        if self.algorithm == self.LUCAS_KANADE \
                           and self._finder.mask is None \
                           and self._has_ignore_box():
            # we consider the buffer height and width are constant, the whole
            # algorithm depends on it anyway. Shouldn't we enforce that somewhere?
            caps_struct = buf.get_caps()[0]
            height = caps_struct['height']
            width = caps_struct['width']
            self._finder.mask = cv.CreateMatHeader(height, width, cv.CV_8UC1)
            data = array.array('B', '\1' * width * height)
            for x in xrange(self.ignore_box_min_x, self.ignore_box_max_x + 1):
                for y in xrange(self.ignore_box_min_y, self.ignore_box_max_y + 1):
                    data[y*height + x] = 0
            cv.SetData(self._finder.mask, data.tostring())

        color_img = img_of_buf(buf)
        gray_img = gray_scale(color_img)
        gray_ref_img = gray_scale(self._reference_img)

        ret = self._finder.optical_flow_img(gray_ref_img, gray_img,
                                             self._reference_blob)
        return ret

    def _has_ignore_box(self):
        return (-1) not in (self.ignore_box_min_x, self.ignore_box_max_x,
                            self.ignore_box_min_y, self.ignore_box_max_y)


gobject.type_register (OpticalFlowCorrector)
ret = gst.element_register (OpticalFlowCorrector, 'opticalflowcorrector')
