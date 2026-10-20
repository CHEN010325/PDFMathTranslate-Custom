"""Workbench OCR routing and source fidelity (unittest; no model required)."""

import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pymupdf

from custom_pdf2zh import workbench_ocr as ocr


class WorkbenchOCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.output = self.root / "ocr.pdf"

    def tearDown(self):
        self.temp.cleanup()

    def make_document(self, rotation=0):
        with pymupdf.open() as doc, pymupdf.open() as scan:
            native = doc.new_page()
            native.insert_text(
                (72, 72),
                "Native text: preserve color, formulas and fonts",
                color=(1, 0, 0),
            )
            scan_page = scan.new_page()
            scan_page.insert_text(
                (60, 90), "Wall thickness and pipe dimensions", fontsize=18
            )
            scan_page.insert_text(
                (60, 125), "Table 1: 10 mm, 20 mm, 30 mm", fontsize=15
            )
            image = scan_page.get_pixmap(dpi=160).tobytes("png")
            page = doc.new_page()
            page.insert_image(page.rect, stream=image)
            page.set_rotation(rotation)
            page.insert_link(
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(20, 20, 100, 40),
                    "uri": "https://example.com",
                }
            )
            doc.new_page()  # A blank native page must never trigger OCR.
            doc.save(self.source)

    def test_native_bypass_does_not_rewrite_or_download(self):
        with pymupdf.open() as doc:
            doc.new_page().insert_text((50, 50), "Native text")
            doc.save(self.source)
        original = self.source.read_bytes()
        with patch.object(
            ocr.asyncio,
            "create_subprocess_exec",
            side_effect=AssertionError("must bypass"),
        ):
            result = asyncio.run(
                ocr.prepare_pdf(self.source, self.output, "en", lambda _: None)
            )
        self.assertEqual(result["path"], str(self.source))
        self.assertEqual(result["pages"], [])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.output.exists())

    def test_candidate_selection_preserves_native_and_blank(self):
        self.make_document()
        self.assertEqual(ocr.image_only_pages(self.source), [1])

    def test_native_pdf_with_blank_bitmap_does_not_require_ocr(self):
        with pymupdf.open() as art, pymupdf.open() as doc:
            blank = art.new_page().get_pixmap()
            doc.new_page().insert_text((50, 50), "Native text")
            page = doc.new_page()
            page.insert_image(page.rect, pixmap=blank)
            doc.save(self.source)
        self.assertEqual(ocr.image_only_pages(self.source), [])

    def test_invalid_language_rejected_before_download(self):
        with self.assertRaises(ValueError):
            ocr.tessdata("../../bad", lambda _: None)

    def test_missing_explicit_language_data_actionable(self):
        with patch.dict(os.environ, {"TESSDATA_PREFIX": str(self.root)}):
            with self.assertRaisesRegex(RuntimeError, "TESSDATA_PREFIX"):
                ocr.tessdata("eng", lambda _: None)

    def test_cancellation_stops_ocr_worker(self):
        self.make_document()
        processes = []
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            proc = await real_spawn(*args, **kwargs)
            processes.append(proc)
            return proc

        def cancel_on_progress(_):
            raise asyncio.CancelledError()

        async def run():
            with patch.object(ocr.asyncio, "create_subprocess_exec", spawn):
                with self.assertRaises(asyncio.CancelledError):
                    await ocr.prepare_pdf(
                        self.source, self.output, "en", cancel_on_progress
                    )

        asyncio.run(run())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(self.output.exists())

    def test_mixed_document_keeps_native_style_flags_and_page_order(self):
        import copy

        self.make_document()

        class Settings:
            def model_copy(self, deep=False):
                return copy.deepcopy(self)

        settings = Settings()
        settings.pdf = SimpleNamespace(
            ocr_workaround=False, pages=None, use_alternating_pages_dual=False
        )
        settings.translation = SimpleNamespace(
            output=str(self.root / "out"), lang_out="zh"
        )
        calls = []

        async def translate(config, source):
            calls.append((config.pdf.ocr_workaround, config.pdf.pages, source))
            output = Path(config.translation.output)
            output.mkdir(parents=True)
            paths = {}
            for kind in ("mono", "dual"):
                path = output / f"result.{kind}.pdf"
                with pymupdf.open() as doc:
                    stride = (
                        2
                        if kind == "dual" and config.pdf.use_alternating_pages_dual
                        else 1
                    )
                    for index in range(3 * stride):
                        doc.new_page().insert_text(
                            (30, 30),
                            f"{'OCR' if config.pdf.ocr_workaround else 'NATIVE'} {index+1}",
                        )
                    doc.save(path)
                paths[f"{kind}_pdf_path"] = path
            yield {"type": "finish", "translate_result": SimpleNamespace(**paths)}

        async def run():
            return [
                event
                async for event in ocr.translation_events(
                    settings, self.source, self.output, [2], translate
                )
            ]

        for alternating in (False, True):
            with self.subTest(alternating=alternating):
                settings.pdf.use_alternating_pages_dual = alternating
                calls.clear()
                events = asyncio.run(run())
                self.assertEqual(
                    calls, [(False, "1", self.source), (True, "2", self.output)]
                )
                self.assertFalse(settings.pdf.ocr_workaround)
                self.assertIsNone(settings.pdf.pages)
                for kind in ("mono", "dual"):
                    with pymupdf.open(
                        getattr(events[-1]["translate_result"], f"{kind}_pdf_path")
                    ) as doc:
                        expected = (
                            [
                                "NATIVE 1",
                                "NATIVE 2",
                                "OCR 3",
                                "OCR 4",
                                "NATIVE 5",
                                "NATIVE 6",
                            ]
                            if kind == "dual" and alternating
                            else ["NATIVE 1", "OCR 2", "NATIVE 3"]
                        )
                        self.assertEqual([p.get_text().strip() for p in doc], expected)
                self.assertFalse((self.root / "translation-parts").exists())

    def test_real_ocr_source_pixels_geometry_links_and_text(self):
        data = Path.home() / ".cache/pdf2zh/tessdata_best/4.1.0/eng.traineddata"
        if not data.exists():
            self.skipTest("local English OCR data not installed")
        for rotation in (0, 90):
            with self.subTest(rotation=rotation):
                self.source.unlink(missing_ok=True)
                self.output.unlink(missing_ok=True)
                self.make_document(rotation)
                before = hashlib.sha256(self.source.read_bytes()).hexdigest()
                result = ocr.add_text_layer(
                    self.source, self.output, "eng", lambda _: None
                )
                self.assertEqual(result["pages"], [2])
                with pymupdf.open(self.source) as src, pymupdf.open(self.output) as dst:
                    self.assertEqual(len(src), len(dst))
                    for a, b in zip(src, dst):
                        self.assertEqual(a.rect, b.rect)
                        self.assertEqual(a.rotation, b.rotation)
                        self.assertEqual(a.get_pixmap().samples, b.get_pixmap().samples)
                    self.assertEqual(src[0].get_text(), dst[0].get_text())
                    self.assertEqual(src[0].get_fonts(), dst[0].get_fonts())
                    self.assertIn("thickness", dst[1].get_text())
                    self.assertFalse(
                        any("GlyphLess" in item[3] for item in dst[1].get_fonts())
                    )
                    self.assertEqual(
                        dst[1].get_links()[0]["uri"], "https://example.com"
                    )
                self.assertEqual(
                    hashlib.sha256(self.source.read_bytes()).hexdigest(), before
                )

    def test_numeric_cells_and_standalone_formula_remain_source_pixels(self):
        from custom_pdf2zh.ocr_layout import text_plan

        with pymupdf.open() as art, pymupdf.open() as doc:
            page = art.new_page()
            for x in (70, 290, 520):
                page.draw_line((x, 150), (x, 350), width=2)
            for y in (150, 250, 350):
                page.draw_line((70, y), (520, y), width=2)
            for point, text in [
                ((90, 200), "Temperature"),
                ((310, 200), "Coefficient"),
                ((90, 300), "20"),
                ((310, 300), "1.00"),
                ((90, 450), "PFA = fT x fA x PN"),
            ]:
                page.insert_text(point, text, fontsize=20)
            scan = doc.new_page()
            scan.insert_image(scan.rect, stream=page.get_pixmap(dpi=160).tobytes("png"))
            before = scan.get_pixmap().samples
            words, plan = text_plan(scan, page.get_text("words"))
            text = " ".join(w[4] for w in words).lower()
            self.assertIn("temperature", text)
            self.assertNotIn("1.00", text)
            self.assertNotIn("pfa", text)
            self.assertEqual(len(plan["cells"]), 4)
            self.assertEqual(before, scan.get_pixmap().samples)

    def test_child_adapter_target_is_restored_after_error(self):
        from pdf2zh_next import high_level

        original = high_level._translate_wrapper
        self.source.with_suffix(".ocr.json").write_text("{}", encoding="utf-8")
        settings = SimpleNamespace(pdf=SimpleNamespace(ocr_workaround=True))

        async def fail(config, path):
            self.assertIs(high_level._translate_wrapper, ocr.scan_translate_wrapper)
            raise RuntimeError("test failure")
            yield

        async def run():
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                async for _ in ocr.scan_engine_events(settings, self.source, fail):
                    pass

        asyncio.run(run())
        self.assertIs(high_level._translate_wrapper, original)

    def test_overlapping_scan_streams_keep_adapter_until_last_stream_exits(self):
        from pdf2zh_next import high_level

        self.source.with_suffix(".ocr.json").write_text("{}", encoding="utf-8")
        settings = SimpleNamespace(pdf=SimpleNamespace(ocr_workaround=True))
        original = high_level._translate_wrapper

        async def run():
            entered = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]

            async def stream(config, path, index):
                entered[index].set()
                await release[index].wait()
                self.assertIs(high_level._translate_wrapper, ocr.scan_translate_wrapper)
                yield {"type": "finish"}

            async def consume(index):
                async def translate(config, path):
                    async for event in stream(config, path, index):
                        yield event

                async for _ in ocr.scan_engine_events(settings, self.source, translate):
                    pass

            first = asyncio.create_task(consume(0))
            await entered[0].wait()
            second = asyncio.create_task(consume(1))
            await entered[1].wait()
            release[0].set()
            await first
            self.assertIs(high_level._translate_wrapper, ocr.scan_translate_wrapper)
            release[1].set()
            await second

        asyncio.run(run())
        self.assertIs(high_level._translate_wrapper, original)

    def test_translation_api_merges_and_normalizes_partial_ocr_settings(self):
        from custom_pdf2zh.webapp import engine, server
        from unittest.mock import AsyncMock

        with (
            patch.object(engine, "LIBRARY_ROOT", self.root),
            patch.object(engine, "SETTINGS_FILE", self.root / "settings.json"),
            patch.object(engine, "_resolve", return_value=self.source),
            patch.object(engine, "start_translation", return_value="task") as start,
        ):
            engine.save_settings({"ocr": {"engine": "PP-OCRv6", "token": "stored"}})
            for partial, expected in [
                (
                    {"engine": "paddle-hybrid"},
                    {"engine": "paddle-hybrid", "token": "stored"},
                ),
                ({"token": " new "}, {"engine": "paddle-hybrid", "token": "new"}),
                ({"token": ""}, {"engine": "paddle-hybrid", "token": ""}),
            ]:
                request = SimpleNamespace(
                    json=AsyncMock(return_value={"path": "test.pdf", "ocr": partial})
                )
                asyncio.run(server.translate(request))
                self.assertEqual(start.call_args.args[1]["ocr"], expected)
                self.assertEqual(engine.load_settings()["ocr"], expected)


if __name__ == "__main__":
    unittest.main()
