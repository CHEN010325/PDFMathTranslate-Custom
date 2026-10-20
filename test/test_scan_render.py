"""Pixel and layout contracts for scan composition, independent of a model."""

import unittest

import pymupdf as f

from custom_pdf2zh.scan_render import (
    regions_from_words,
    compose_page,
    translate_cell,
    technical,
)


class ScanRenderTests(unittest.TestCase):
    def test_missing_translation_rejected_before_modifying_page(self):
        with f.open() as doc:
            page = doc.new_page()
            page.insert_text((50, 50), "Keep original")
            before = page.get_pixmap().samples
            with self.assertRaisesRegex(RuntimeError, "段落数不符"):
                compose_page(page, {"regions": [{}]}, [])
            self.assertEqual(before, page.get_pixmap().samples)

    def test_spanning_narrative_cell_reflows_with_source_value_images(self):
        with f.open() as doc:
            page = doc.new_page(width=600, height=500)
            for row in range(10):
                page.insert_text(
                    (40, 70 + row * 22),
                    "A long narrative statement about test requirements and preparation at 210 C.",
                    fontsize=10,
                )
            words = page.get_text("words")
            regions, protected = regions_from_words(
                words, [[30, 45, 570, 305]], 600, 500
            )
            self.assertEqual(len(regions), 1)
            parts = regions[0]["flow_parts"]
            self.assertTrue(any(p["image"] and "210" in p["text"] for p in parts))
            self.assertFalse(any("210" in p["text"] for p in parts if not p["image"]))
            self.assertFalse(protected)

    def test_table_translation_never_sends_numeric_placeholders_to_model(self):
        class Translator:
            def __init__(self):
                self.calls = []

            def translate(self, text):
                self.calls.append(text)
                return "片段译文"

        translator = Translator()
        parts = translate_cell(
            translator,
            "Shall conform to {v0} Exposure {v1} Number oftest pieces?",
            2,
            "zh",
        )
        self.assertEqual(parts, ["应符合", "片段译文", "试样数量"])
        self.assertEqual(translator.calls, ["Exposure"])
        for value in ("kg/m?", "mg/kg", "GJ/m²", "PEBO", "minh", "0,20", "Al,", "AI"):
            self.assertTrue(technical(value), value)
        self.assertFalse(technical("=Number"))

    def test_blue_hyperlinks_are_prose_and_colored_banners_are_not(self):
        import numpy as np
        from custom_pdf2zh.ocr_layout import readable_crop

        link = np.full((10, 10, 3), 255, dtype=np.uint8)
        link[:6, :, :] = [20, 60, 220]
        self.assertTrue(readable_crop(link))
        banner = np.full((10, 10, 3), [20, 60, 220], dtype=np.uint8)
        banner[:1] = 255
        self.assertFalse(readable_crop(banner))

    def test_math_operator_correction_requires_visible_lower_bar(self):
        import numpy as np
        from custom_pdf2zh.ocr_layout import has_plus_minus_bar

        image = np.full((36, 30, 3), 255, dtype=np.uint8)
        image[4:24, 14:17] = 0
        image[12:15, 5:26] = 0
        pix = f.Pixmap(f.csRGB, 30, 36, image.tobytes(), False)
        self.assertFalse(has_plus_minus_bar(pix))
        image[29:32, 5:26] = 0
        pix = f.Pixmap(f.csRGB, 30, 36, image.tobytes(), False)
        self.assertTrue(has_plus_minus_bar(pix))
        image[4:12, 14:17] = 255
        pix = f.Pixmap(f.csRGB, 30, 36, image.tobytes(), False)
        self.assertFalse(has_plus_minus_bar(pix))

    def test_table_values_grid_and_formula_are_immutable_and_labels_are_erased(self):
        with f.open() as source, f.open() as doc:
            art = source.new_page(width=600, height=500)
            art.insert_font(fontname="Source", fontbuffer=f.Font("cjk").buffer)
            for x in (60, 300, 540):
                art.draw_line((x, 120), (x, 360), width=1)
            for y in (120, 200, 280, 360):
                art.draw_line((60, y), (540, y), width=1)
            for point, text in [
                ((80, 160), "Test temperature"),
                ((320, 160), "VALUE"),
                ((80, 230), "Exposure"),
                ((320, 225), "≥3,5 GJ/m²"),
                ((320, 253), "Shall conform to ISO16871"),
                ((80, 320), "Nominal value"),
                ((320, 320), "0,20 < MFR < 1,40"),
                ((320, 345), "Rating of dispersion A1"),
            ]:
                art.insert_text(point, text, fontname="Source", fontsize=12)
            art.insert_text(
                (80, 420), "PFA = fT x fA x PN", fontname="Source", fontsize=14
            )
            scan = doc.new_page(width=600, height=500)
            scan.insert_image(scan.rect, stream=art.get_pixmap().tobytes("png"))
            cells = [
                [x0, y0, x1, y1]
                for y0, y1 in [(121, 199), (201, 279), (281, 359)]
                for x0, x1 in [(61, 299), (301, 539)]
            ]
            words = [
                (*w[:4], "Al" if w[4] == "A1" else w[4], *w[5:])
                for w in art.get_text("words")
            ]
            regions, protected = regions_from_words(words, cells, 600, 500)
            sent = " ".join(r["text"] for r in regions)
            self.assertIn("VALUE", sent)
            self.assertIn("Test temperature", sent)
            self.assertNotIn("3,5", sent)
            self.assertNotIn("1,40", sent)
            self.assertNotIn("ISO16871", sent)
            self.assertNotIn("Al", sent)
            clips = [
                f.Rect(310, 208, 535, 231),
                f.Rect(310, 306, 535, 326),
                f.Rect(70, 402, 290, 426),
                f.Rect(299, 120, 301, 360),
                f.Rect(next(w[:4] for w in words if w[4] == "Al")),
            ]
            before = [scan.get_pixmap(clip=clip).samples for clip in clips]
            compose_page(
                scan,
                {"regions": regions, "protected": protected},
                ["中文说明" for _ in regions],
            )
            self.assertEqual(
                before, [scan.get_pixmap(clip=clip).samples for clip in clips]
            )
            # The English glyphs sat below the new label's top-aligned baseline.
            pix = scan.get_pixmap(clip=f.Rect(78, 150, 190, 162))
            self.assertGreaterEqual(min(pix.samples), 245)
            self.assertIn("中文", scan.get_text())

    def test_empty_translation_does_not_erase_source(self):
        with f.open() as doc:
            page = doc.new_page()
            page.insert_text((60, 80), "Original text")
            before = page.get_pixmap().samples
            plan = {
                "regions": [
                    {
                        "text": "Original text",
                        "box": [60, 65, 200, 90],
                        "erase": [[60, 65, 200, 90]],
                        "size": 12,
                        "table": False,
                        "limit": [0, 0, 595, 842],
                    }
                ],
                "protected": [],
            }
            with self.assertRaisesRegex(RuntimeError, "为空"):
                compose_page(page, plan, [""])
            self.assertEqual(before, page.get_pixmap().samples)


if __name__ == "__main__":
    unittest.main()
