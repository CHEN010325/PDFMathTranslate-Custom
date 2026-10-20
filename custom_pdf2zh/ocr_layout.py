"""Conservative scan geometry: ruled cells and original-pixel protection."""

from collections import defaultdict
import re

import cv2
import numpy as np
import pymupdf


def ruled_cells(pix):
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[
        :, :, :3
    ]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    binary = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY_INV)[1]
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, np.ones((1, max(25, pix.width // 35)), np.uint8)
    )
    vertical = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, np.ones((max(25, pix.height // 40), 1), np.uint8)
    )
    grid = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    contours, hierarchy = cv2.findContours(
        grid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    cells = []
    if hierarchy is not None:
        for contour, info in zip(contours, hierarchy[0]):
            if info[3] < 0:
                continue  # Cell interiors are holes in the connected grid.
            px, py, pw, ph = cv2.boundingRect(contours[info[3]])
            if pw < pix.width * 0.35 or ph < 35:
                continue
            if np.mean(gray[py : py + ph, px : px + pw] > 215) < 0.65:
                continue  # Photo details and logos are not ruled tables.
            x, y, w, h = cv2.boundingRect(contour)
            if w >= 25 and h >= 14 and w * h >= 650:
                cells.append([x + 1, y + 1, x + w - 1, y + h - 1])
    return rgb, cells


def has_prose(text):
    # Standalone variables, standards IDs, units and numerical cells should be
    # left as source pixels. Numbers inside prose remain in the translation.
    words = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    short = {
        "air",
        "dry",
        "wet",
        "yes",
        "no",
        "not",
        "and",
        "for",
        "the",
        "or",
        "gas",
        "oil",
        "hot",
        "low",
        "any",
        "all",
    }
    return any(
        len(word) >= 4
        or word.lower() in short
        or any(c.isalpha() and ord(c) >= 0x590 for c in word)
        for word in words
    )


def readable_crop(crop):
    if not crop.size:
        return False
    white = float(np.mean(np.min(crop, axis=2) > 215))
    colored = float(np.mean(np.max(crop, axis=2) - np.min(crop, axis=2) > 40))
    # Bold black headings and blue hyperlinks are ink on light paper. Colored
    # banners are a colored background and must retain their original pixels.
    return white >= 0.15 and colored <= max(0.1, white + 0.2)


def has_plus_minus_bar(pix):
    """Confirm a lower minus bar from pixels before correcting OCR '+' to '±'."""
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    ink = np.min(rgb[:, :, :3], axis=2) < 150
    ys, xs = np.where(ink)
    if not len(xs):
        return False
    ink = ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    wide = np.flatnonzero(ink.sum(axis=1) >= ink.shape[1] * 0.65)
    if not len(wide):
        return False
    bands = np.split(wide, np.where(np.diff(wide) > 1)[0] + 1)
    return len(bands) == 2 and bands[0][0] >= max(2, ink.shape[0] * 0.12)


def text_plan(page, words, recognize_cell=None, extra_cells=None):
    bounds = page.rect * page.derotation_matrix
    rotation = page.rotation
    try:
        page.set_rotation(0)
        pix = page.get_pixmap(alpha=False, annots=False)
    finally:
        page.set_rotation(rotation)
    rgb, cells = ruled_cells(pix)
    sx, sy = bounds.width / pix.width, bounds.height / pix.height
    cells = [[x0 * sx, y0 * sy, x1 * sx, y1 * sy] for x0, y0, x1, y1 in cells]
    cells.extend(extra_cells or [])
    if recognize_cell:
        outside = [
            w
            for w in words
            if not any(
                (pymupdf.Rect(w[:4]).tl + pymupdf.Rect(w[:4]).br) / 2 in pymupdf.Rect(c)
                for c in cells
            )
        ]
        for i, cell in enumerate(cells):
            recovered = recognize_cell(page, cell, i)
            if recovered:
                outside.extend(recovered)
            else:
                outside.extend(
                    w
                    for w in words
                    if (pymupdf.Rect(w[:4]).tl + pymupdf.Rect(w[:4]).br) / 2
                    in pymupdf.Rect(cell)
                )
        words = outside
    groups = defaultdict(list)
    for word in words:
        box = pymupdf.Rect(word[:4])
        center = (box.tl + box.br) / 2
        cell = next(
            (i for i, rect in enumerate(cells) if center in pymupdf.Rect(rect)), None
        )
        groups[("cell", cell) if cell is not None else ("line", *word[5:7])].append(
            word
        )
    selected = []
    readable = []
    for word in words:
        x0, y0, x1, y1 = word[:4]
        crop = rgb[
            max(0, int(y0 / sy)) : max(0, int(y1 / sy)),
            max(0, int(x0 / sx)) : max(0, int(x1 / sx)),
        ]
        if readable_crop(crop):
            readable.append(word)
    for group in groups.values():
        if not has_prose(" ".join(w[4] for w in group)):
            continue
        for word in group:
            x0, y0, x1, y1 = word[:4]
            crop = rgb[
                max(0, int(y0 / sy)) : max(0, int(y1 / sy)),
                max(0, int(x0 / sx)) : max(0, int(x1 / sx)),
            ]
            if not crop.size:
                continue
            # White text on a photo/banner is part of the picture. Erasing its
            # rectangle with white would destroy the picture, so retain it.
            if not readable_crop(crop):
                continue
            selected.append(word)
    from custom_pdf2zh.scan_render import regions_from_words

    regions, protected = regions_from_words(
        readable, cells, bounds.width, bounds.height
    )
    return selected, {
        "height": bounds.height,
        "cells": cells,
        "erase": [list(w[:4]) for w in selected],
        "regions": regions,
        "protected": protected,
    }
