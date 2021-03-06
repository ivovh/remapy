import sys
import struct
import os.path
import argparse
import math
import json
import io
import time
import re
import numpy as np
from pdfrw import PdfReader, PdfWriter, PageMerge, IndirectPdfDict, PdfDict
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.graphics.shapes import PolyLine, Drawing, Line


# Size
DEFAULT_IMAGE_WIDTH = 1404
DEFAULT_IMAGE_HEIGHT = 1872


# Mappings
default_stroke_color = {
    0: colors.Color(48/255., 63/255., 159/255.),        # Pen color 1
    1: colors.Color(211/255., 47/255., 47/255.),        # Pen color 2
    2: colors.Color(255/255., 255/255., 255/255.),      # Eraser
    3: colors.Color(255/255., 255/255., 0, alpha=0.25), # Highlighter
    # Own defined colors
    4: colors.Color(97/255., 97/255., 97/255.)             # Pencil
}


def pdf(rm_files_path, path_original_pdf, path_annotated_pdf, path_oap_pdf):
    """ Render pdf with annotations. The path_oap_pdf defines the pdf 
        which includes only annotated pages.
    """

    base_pdf = PdfReader(open(path_original_pdf, "rb"))

    # Parse remarkable files and write into pdf
    annotations_pdf = []

    for page_nr in range(base_pdf.numPages):
        rm_file_name = "%s/%d" % (rm_files_path, page_nr)
        rm_file = "%s.rm" % rm_file_name
        if not os.path.exists(rm_file):
            annotations_pdf.append(None)
            continue
            
        page_layout = base_pdf.pages[page_nr].MediaBox
        crop_box = base_pdf.pages[page_nr].CropBox
        if page_layout is None:
            page_layout = base_pdf.pages[page_nr].ArtBox

            if page_layout is None:
                annotations_pdf.append(None)
                continue
            
        image_width, image_height = float(page_layout[2]), float(page_layout[3])
        annotated_page = _render_rm_file(rm_file_name, image_width=image_width, image_height=image_height, crop_box=crop_box)
        if len(annotated_page.pages) <= 0:
            annotations_pdf.append(None)
        else:
            page = annotated_page.pages[0]
            annotations_pdf.append(page)
       
    # Merge annotations pdf and original pdf
    writer_full = PdfWriter()
    writer_oap = PdfWriter()
    for i in range(base_pdf.numPages):          
        annotations_page = annotations_pdf[i]

        if annotations_page != None:
            merger = PageMerge(base_pdf.pages[i])
            merger.add(annotations_page).render()
            writer_oap.addpage(base_pdf.pages[i])

        writer_full.addpage(base_pdf.pages[i])

    writer_full.write(path_annotated_pdf)
    writer_oap.write(path_oap_pdf)


def notebook(path, id, path_annotated_pdf, path_templates=None):
    
    rm_files_path = "%s/%s" % (path, id)
    annotations_pdf = []

    p = 0
    while True:
        rm_file_name = "%s/%d" % (rm_files_path, p)
        rm_file = "%s.rm" % rm_file_name

        if not os.path.exists(rm_file):
            break

        overlay = _render_rm_file(rm_file_name)
        annotations_pdf.append(overlay)
        p += 1  
    
    # Write empty notebook notes containing blank pages or templates
    writer = PdfWriter()
    templates = _get_templates_per_page(path, id, path_templates)
    for template in templates:
        if template == None:
            writer.addpage(_blank_page())
        else:
            writer.addpage(template.pages[0])
    writer.write(path_annotated_pdf)
    
    # Overlay empty notebook with annotations
    templates_pdf = PdfReader(path_annotated_pdf)
    for i in range(len(annotations_pdf)):

        empty_page = len(annotations_pdf[i].pages) <= 0
        if empty_page:
            continue 

        annotated_page = annotations_pdf[i].pages[0]
        if templates != None:
            merger = PageMerge(templates_pdf.pages[i])
            merger.add(annotated_page).render()
        else:
            output_pdf.addPage(annotated_page)
    
    writer = PdfWriter()
    writer.write(path_annotated_pdf, templates_pdf)



def _get_templates_per_page(path, id, path_templates):

    pagedata_file = "%s/%s.pagedata" % (path, id)
    with open(pagedata_file, 'r') as f:
        template_paths = ["%s/%s.png" % (path_templates, l.rstrip('\n')) for l in f] 
        
    templates = []
    for template_path in template_paths:
        if not os.path.exists(template_path):
            templates.append(None)
            continue

        packet = io.BytesIO()
        can = canvas.Canvas(packet, pagesize=(DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT))
        can.drawImage(template_path, 0, 0)
        can.save()
        packet.seek(0)
        templates.append(PdfReader(packet))

    return templates


def _blank_page(width=DEFAULT_IMAGE_WIDTH, height=DEFAULT_IMAGE_HEIGHT):
    blank = PageMerge()
    blank.mbox = [0, 0, width, height] # 8.5 x 11
    blank = blank.render()
    return blank


def _render_rm_file(rm_file_name, image_width=DEFAULT_IMAGE_WIDTH, 
        image_height=DEFAULT_IMAGE_HEIGHT, crop_box=None):
    """ Render the .rm files (old .lines). See also 
    https://plasma.ninja/blog/devices/remarkable/binary/format/2017/12/26/reMarkable-lines-file-format.html
    """
    
    rm_file = "%s.rm" % rm_file_name
    rm_file_metadata = "%s-metadata.json" % rm_file_name

    is_landscape = image_width > image_height
    if is_landscape:
        image_height, image_width = image_width, image_height

    crop_box = [0.0, 0.0, image_width, image_height] if crop_box is None else crop_box

    # Calculate the image height and width that we use for the overlay
    if is_landscape:
        image_width = float(crop_box[2]) - float(crop_box[0])
        image_height = max(image_height, image_width * DEFAULT_IMAGE_HEIGHT / DEFAULT_IMAGE_WIDTH)
        ratio = (image_height) / (DEFAULT_IMAGE_HEIGHT)
    else:    
        image_height = float(crop_box[3]) - float(crop_box[1])
        image_width = max(image_width, image_height * DEFAULT_IMAGE_WIDTH / DEFAULT_IMAGE_HEIGHT)
        ratio = (image_width) / (DEFAULT_IMAGE_WIDTH)
    
    # Is this a reMarkable .lines file?
    with open(rm_file, 'rb') as f:
        data = f.read()
    offset = 0
    expected_header_v3=b'reMarkable .lines file, version=3          '
    expected_header_v5=b'reMarkable .lines file, version=5          '
    if len(data) < len(expected_header_v5) + 4:
        abort('File too short to be a valid file')

    fmt = '<{}sI'.format(len(expected_header_v5))
    header, nlayers = struct.unpack_from(fmt, data, offset); offset += struct.calcsize(fmt)
    is_v3 = (header == expected_header_v3)
    is_v5 = (header == expected_header_v5)
    if (not is_v3 and not is_v5) or  nlayers < 1:
        abort('Not a valid reMarkable file: <header={}><nlayers={}>'.format(header, nlayers))
        return
    
    # Load name of layers; if layer name starts with # we use this color 
    # for this layer
    layer_colors = [None for l in range(nlayers)]
    if os.path.exists(rm_file_metadata):
        with open(rm_file_metadata, "r") as meta_file:
            layers = json.loads(meta_file.read())["layers"]
        
        for l in range(len(layers)):
            layer = layers[l]

            matches = re.search(r"#([^\s]+)", layer["name"], re.M|re.I)
            if not matches:
                continue 
            color_code = matches[0].lower()

            # Try to parse hex code
            try:
                has_alpha = len(color_code) > 7
                layer_colors[l] = colors.HexColor(color_code, hasAlpha=has_alpha)
                continue
            except:
                pass
            
            # Try to get from name
            color_code = color_code[1:]
            color_names = colors.getAllNamedColors()
            if color_code in color_names:
                layer_colors[l] = color_names[color_code]
            
            # No valid color found... automatic fallback to default


    # Iterate through layers on the page (There is at least one)
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=(image_width, image_height))
    for layer in range(nlayers):
        fmt = '<I'
        (nstrokes,) = struct.unpack_from(fmt, data, offset); offset += struct.calcsize(fmt)

        # Iterate through the strokes in the layer (If there is any)
        for stroke in range(nstrokes):
            if is_v3:
                fmt = '<IIIfI'
                pen, color, i_unk, pen_width, nsegments = struct.unpack_from(fmt, data, offset); offset += struct.calcsize(fmt)
            if is_v5:
                fmt = '<IIIffI'
                pen, color, i_unk, pen_width, unknown, nsegments = struct.unpack_from(fmt, data, offset); offset += struct.calcsize(fmt)
            
            opacity = 1
            last_x = -1.; last_y = -1.

            # Check which tool is used for both, v3 and v5 and set props
            # https://support.remarkable.com/hc/en-us/articles/115004558545-5-1-Tools-Overview
            is_highlighter = (pen == 5 or pen == 18)
            is_eraser = pen == 6
            is_eraser_area = pen == 8
            is_sharp_pencil = (pen == 7 or pen == 13) 
            is_tilt_pencil = (pen == 1 or pen == 14)
            is_marker = (pen == 3 or pen == 16)
            is_ballpoint = (pen == 2 or pen == 15)
            is_fineliner = (pen == 4 or pen == 17)
            is_brush = (pen == 0 or pen == 12)

            if is_sharp_pencil or is_tilt_pencil:
                color = 4

            if is_brush:
                pass
            elif is_ballpoint or is_fineliner:
                pen_width = 32 * pen_width * pen_width - 116 * pen_width + 107
            elif is_marker:
                pen_width = 64 * pen_width - 112
                opacity = 0.9
            elif is_highlighter:
                pen_width = 30
                opacity = 0.2
                color = 3
            elif is_eraser:
                pen_width = 1280 * pen_width * pen_width - 4800 * pen_width + 4510
                color = 2
            elif is_sharp_pencil or is_tilt_pencil:
                pen_width = 16 * pen_width - 27
                opacity = 0.9
            elif is_eraser_area:
                opacity = 0.
            else: 
                print('Unknown pen: {}'.format(pen))
                opacity = 0.

            # Iterate through the segments to form a polyline
            points = []
            width = []
            for segment in range(nsegments):
                fmt = '<ffffff'
                xpos, ypos, i_unk2, pressure, tilt, _ = struct.unpack_from(fmt, data, offset); offset += struct.calcsize(fmt)
                
                if is_ballpoint or is_brush:
                    width.append((6*pen_width + 2*pressure) / 8 * ratio)
                else:
                    width.append((5*pen_width + 2*tilt + 1*pressure) / 8 * ratio)

                xpos = ratio * xpos + float(crop_box[0])
                ypos = image_height - ratio * ypos + float(crop_box[1])
                points.extend([xpos, ypos])
            if is_eraser_area or is_eraser:
                continue
            
            # Render lines
            drawing = Drawing(image_width, image_height)
            can.setLineCap(1)

            if layer_colors[layer] is None:
                can.setStrokeColor(default_stroke_color[color])
            else:
                can.setStrokeColor(layer_colors[layer])

            p = can.beginPath()
            p.moveTo(points[0], points[1])
            for i in range(0, len(points), 2):
                can.setLineWidth(width[int(i/2)])
                p.lineTo(points[i], points[i+1])
                p.moveTo(points[i], points[i+1])
                if i % 10 == 0:
                    p.close()
            p.close()
            can.drawPath(p)
        
    can.save()
    packet.seek(0)
    overlay = PdfReader(packet)

    if is_landscape:
        for page in overlay.pages:
            page.Rotate=90

    return overlay


if __name__ == "__main__":
    main()