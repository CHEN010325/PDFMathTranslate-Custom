import tempfile
import unittest
from pathlib import Path

import pymupdf

from custom_pdf2zh.outline_text import filter_render_units, outline_curves


class OutlineTextTests(unittest.TestCase):
    def test_shared_form_is_matched_against_original_after_output_is_rewritten(self):
        from babeldoc.format.pdf.document_il.backend.pdf_creater import (
            CharacterRenderUnit, CurveRenderUnit,
        )
        from babeldoc.format.pdf.document_il.il_version_1 import Box, PdfCharacter, PdfCurve

        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as label, pymupdf.open() as doc:
            label.new_page(width=200, height=100).insert_text(
                (30, 40), "HI", fontsize=12, render_mode=3
            )
            for _ in range(2):
                page = doc.new_page(width=200, height=100)
                shared = page.show_pdf_page(page.rect, label)
            self.assertIn(shared, {xref for xref, *_ in doc[0].get_xobjects()})
            source = Path(directory) / "source.pdf"
            doc.save(source)
            doc.update_stream(shared, b"")
            with pymupdf.open(stream=doc.tobytes(), filetype="pdf") as rewritten:
                self.assertFalse(rewritten[1].get_texttrace())

            boxes = [Box(x=30.5, y=60, x2=37.7, y2=68), Box(x=39.5, y=60, x2=41, y2=68)]
            char = CharacterRenderUnit(
                PdfCharacter(box=boxes[0], char_unicode="译", pdf_character_id=1), 2
            )
            glyphs = [
                CurveRenderUnit(PdfCurve(box=box, fill_background=True), 1)
                for box in boxes
            ]
            self.assertEqual(filter_render_units(source, 1, [*glyphs, char]), [char])

    def test_unrelated_visible_text_cannot_authorize_erasing_paths(self):
        from babeldoc.format.pdf.document_il.il_version_1 import Box, PdfCharacter, PdfCurve

        with pymupdf.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text((30, 40), "HI", fontsize=12, render_mode=3)
            page.insert_text((30, 40), "X", fontsize=12)
            boxes = [Box(x=30.5, y=60, x2=37.7, y2=68), Box(x=39.5, y=60, x2=41, y2=68)]
            curves = [PdfCurve(box=box, fill_background=True) for box in boxes]
            char = PdfCharacter(box=boxes[0], char_unicode="X", pdf_character_id=88)
            self.assertEqual(outline_curves(page, curves, [char]), set())

    def test_hidden_label_matches_glyph_paths_but_preserves_border(self):
        from babeldoc.format.pdf.document_il.il_version_1 import Box, PdfCurve

        with pymupdf.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text((30, 40), "HI", fontsize=12, render_mode=3)
            glyphs = [
                PdfCurve(box=Box(x=30.5, y=60, x2=37.7, y2=68), fill_background=True),
                PdfCurve(box=Box(x=39.5, y=60, x2=41, y2=68), fill_background=True),
            ]
            border = PdfCurve(box=Box(x=25, y=55, x2=47, y2=73), fill_background=True)
            self.assertEqual(
                outline_curves(page, glyphs + [border]), {id(c) for c in glyphs}
            )
            self.assertEqual(outline_curves(page, glyphs, replacements=[]), set())

    def test_visible_text_does_not_trigger_removal(self):
        from babeldoc.format.pdf.document_il.il_version_1 import Box, PdfCurve

        with pymupdf.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text((30, 40), "HI", fontsize=12)
            curve = PdfCurve(box=Box(x=30.5, y=60, x2=37.7, y2=68), fill_background=True)
            self.assertEqual(outline_curves(page, [curve]), set())
