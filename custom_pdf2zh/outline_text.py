"""Match searchable, invisible labels to their visible vector glyphs."""

import pymupdf


def _rect(box, matrix):
    return pymupdf.Rect(box.x, box.y, box.x2, box.y2) * matrix


def _matches(glyph, ink, size):
    return (
        ink.x0 >= glyph.x0 - size * 0.2
        and ink.x1 <= glyph.x1 + size * 0.2
        and ink.x1 > glyph.x0 and ink.x0 < glyph.x1
    )


def outline_curves(source_page, curves, replacements=None):
    """Return filled paths that can be matched one-to-one to a hidden run."""
    spans = source_page.get_texttrace()
    hidden = [span for span in spans if span["type"] == 3]
    if not hidden:
        return set()
    visible = [
        pymupdf.Rect(box) for span in spans if span["type"] != 3 and span["opacity"]
        for code, _, _, box in span["chars"]
        if 0 <= code <= 0x10FFFF and not chr(code).isspace()
    ]
    matrix = source_page.transformation_matrix
    candidates = [
        (curve, _rect(curve.box, matrix))
        for curve in curves
        if curve.box and curve.fill_background and not curve.stroke_path
        and not curve.relocation_transform and not curve.passthrough_paint
        and not curve.pdf_original_path_primitive
    ]
    rendered = (
        [(char.xobj_id, _rect(char.box, matrix)) for char in replacements
         if char.box and char.pdf_character_id is not None and char.char_unicode
         and char.char_unicode.strip()]
        if replacements is not None else None
    )
    removed = set()
    for span in hidden:
        if span["dir"][0] < 0.99 or span["size"] <= 0:
            continue
        size = span["size"]
        glyphs = [
            pymupdf.Rect(box) for code, _, _, box in span["chars"]
            if 0 <= code <= 0x10FFFF and chr(code).isprintable() and not chr(code).isspace()
        ]
        if len(glyphs) < 2:
            continue
        run = pymupdf.Rect(span["bbox"])
        if any((run & box).get_area() > box.get_area() * 0.35 for box in visible):
            continue
        band = run + (-size * 0.2, -size * 0.15, size * 0.2, size * 0.15)
        inks = sorted(
            ((curve, ink) for curve, ink in candidates
             if band.contains(ink) and ink.width > 0 and 0 < ink.height <= size * 1.15),
            key=lambda item: item[1].x0,
        )
        # Pair by reading order. Italics and kerning can overlap adjacent advances.
        if len(inks) == len(glyphs) and all(
            _matches(glyph, ink, size) for glyph, (_, ink) in zip(glyphs, inks)
        ):
            matches = [curve for curve, _ in inks]
        else:
            # A nearby icon can fall inside the padded band but between advances.
            options = [
                [curve for curve, ink in inks
                 if _matches(glyph, ink, size)
                 and glyph.x0 <= (ink.x0 + ink.x1) / 2 <= glyph.x1]
                for glyph in glyphs
            ]
            if any(len(group) != 1 for group in options):
                continue
            matches = [group[0] for group in options]
            if len({id(curve) for curve in matches}) != len(glyphs):
                continue
        form = matches[0].xobj_id
        if any(curve.xobj_id != form for curve in matches):
            continue
        if rendered is not None:
            start = glyphs[0] + (-size * 0.35, -size * 0.2, size * 0.35, size * 0.2)
            if not any(
                owner == form and start.contains(box.tl + (box.width / 2, box.height / 2))
                for owner, box in rendered
            ):
                continue
        removed.update(map(id, matches))
    return removed


def filter_render_units(source_path, page_number, units):
    from babeldoc.format.pdf.document_il.backend.pdf_creater import (
        CharacterRenderUnit,
        CurveRenderUnit,
    )

    curves = [unit.curve for unit in units if isinstance(unit, CurveRenderUnit)]
    if not curves:
        return units
    characters = [unit.char for unit in units if isinstance(unit, CharacterRenderUnit)]
    if not characters:
        return units
    with pymupdf.open(source_path) as source:
        removed = outline_curves(source[page_number], curves, characters)
    return [
        unit for unit in units
        if not isinstance(unit, CurveRenderUnit) or id(unit.curve) not in removed
    ]
