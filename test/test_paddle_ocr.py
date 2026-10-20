import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pymupdf as fitz

from custom_pdf2zh import paddle_ocr


class PaddleOCRTests(unittest.TestCase):
    def test_spanning_footnotes_replace_old_value_islands_as_complete_paragraphs(self):
        text = "a First complete note at 200 °C.b Another complete note at 220 °C."
        words = [(30, 100, 550, 120, text, 0, 0, 0)]
        plan = {
            "cells": [[20, 90, 580, 300]],
            "regions": [
                {"text": "fragment", "box": [30, 100, 200, 120], "table": True}
            ],
            "protected": [[300, 100, 350, 120]],
        }
        layout = {
            "prunedResult": {
                "width": 600,
                "height": 800,
                "parsing_res_list": [
                    {
                        "block_label": "table",
                        "block_content": "<table><tr><td>"
                        + text
                        + "</td></tr></table>",
                        "block_bbox": [20, 90, 580, 300],
                    }
                ],
            }
        }
        with fitz.open() as doc:
            result = paddle_ocr.apply_document_layout(
                doc.new_page(width=600, height=800), plan, layout, words, []
            )
        self.assertEqual(len(result["regions"]), 1)
        self.assertEqual(
            [p["label"] for p in result["regions"][0]["paragraphs"]], ["a", "b"]
        )
        self.assertFalse(result["protected"])

    def test_layout_omitted_label_is_recovered_without_duplicate_paragraph(self):
        words = [
            (60, 60, 150, 80, "Property", 0, 0, 0),
            (300, 400, 345, 420, "Date", 1, 1, 0),
        ]
        layout = {
            "prunedResult": {
                "width": 600,
                "height": 800,
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "Property",
                        "block_bbox": [55, 55, 155, 85],
                    }
                ],
            }
        }
        plan = {"cells": [], "regions": [], "protected": []}
        with fitz.open() as doc:
            result = paddle_ocr.apply_document_layout(
                doc.new_page(width=600, height=800), plan, layout, words, []
            )
        self.assertEqual([r["text"] for r in result["regions"]], ["Property", "Date"])
        self.assertEqual(result["coverage_recovered"], ["Date"])

    def test_borderless_symbol_table_translates_descriptions_and_preserves_symbols(
        self,
    ):
        words = [
            (40, 100, 60, 115, "C", 0, 0, 0),
            (140, 100, 260, 115, "design coefficient", 0, 0, 1),
        ]
        layout = {
            "prunedResult": {
                "width": 600,
                "height": 800,
                "parsing_res_list": [
                    {
                        "block_label": "table",
                        "block_content": "<table><tr><td>C</td><td>design coefficient</td></tr></table>",
                        "block_bbox": [30, 90, 400, 130],
                    }
                ],
            }
        }
        plan = {"cells": [], "regions": [], "protected": []}
        with fitz.open() as doc:
            result = paddle_ocr.apply_document_layout(
                doc.new_page(width=600, height=800), plan, layout, words, []
            )
        self.assertEqual([r["text"] for r in result["regions"]], ["design coefficient"])
        self.assertIn([40, 100, 60, 115], result["protected"])

    def test_contents_rejoins_separate_page_number_column_by_position(self):
        words = []
        for i in range(4):
            y = 100 + i * 30
            words.extend(
                [
                    (60, y, 160, y + 15, "Section title", i, i, 0),
                    (
                        540,
                        y,
                        550,
                        y + 15,
                        "iv" if i == 0 else str(i + 1),
                        i + 10,
                        i + 10,
                        0,
                    ),
                ]
            )
        layout = {
            "prunedResult": {
                "width": 600,
                "height": 800,
                "parsing_res_list": [
                    {
                        "block_label": "content",
                        "block_content": "Contents",
                        "block_bbox": [50, 90, 560, 220],
                    }
                ],
            }
        }
        plan = {"cells": [], "regions": [], "protected": []}
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            result = paddle_ocr.apply_document_layout(page, plan, layout, words, [])
        self.assertEqual(len(result["cells"]), 4)
        self.assertEqual(len(result["regions"]), 4)
        self.assertTrue(all(r["text"] == "Section title" for r in result["regions"]))
        self.assertEqual(len(result["protected"]), 4)

    def test_same_document_job_is_resumed_without_another_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.pdf"
            source.write_bytes(b"same document")
            receipt = Path(folder) / "receipt.json"
            key = paddle_ocr.document_key(source, [0])
            receipt.write_text(
                json.dumps(
                    {"source_key": key, "model": "PP-OCRv6", "jobId": "completed-job"}
                )
            )
            status = SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": "https://download.example/result"},
                    }
                },
            )
            result = SimpleNamespace(
                status_code=200,
                text=json.dumps({"result": {"ocrResults": [{"page": 1}]}}),
            )
            with (
                patch.object(paddle_ocr, "receipt_path", return_value=receipt),
                patch.object(
                    paddle_ocr.requests,
                    "post",
                    side_effect=AssertionError("must not re-upload"),
                ),
                patch.object(paddle_ocr.requests, "get", side_effect=[status, result]),
            ):
                pages = paddle_ocr.fetch_pages(
                    source, "example-token", lambda _: None, source_key=key
                )
            self.assertEqual(pages, [{"page": 1}])
            source.write_bytes(b"changed document")
            self.assertNotEqual(key, paddle_ocr.document_key(source, [0]))
            self.assertNotEqual(
                paddle_ocr.document_key(source, [0]),
                paddle_ocr.document_key(source, [1]),
            )

    def test_vl_paragraph_replaces_fragmented_ocr_without_rewriting_table_numbers(self):
        words = [
            (60, 60, 150, 80, "Rounded", 0, 0, 0),
            (180, 540, 240, 560, "3,5", 1, 1, 0),
            (65, 520, 150, 540, "Exposure", 1, 1, 1),
        ]
        value = [180, 540, 240, 560]
        plan = {
            "cells": [[60, 500, 280, 600]],
            "protected": [value],
            "regions": [
                {"text": "Rounded to 0", "box": [60, 60, 400, 90], "table": False},
                {"text": "1 mm", "box": [60, 90, 400, 120], "table": False},
                {"text": "Exposure", "box": [65, 520, 150, 540], "table": True},
            ],
        }
        layout = {
            "prunedResult": {
                "width": 1200,
                "height": 1600,
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "Rounded to the next greater 0.1 mm",
                        "block_bbox": [120, 120, 1000, 240],
                    },
                    {
                        "block_label": "table",
                        "block_content": "<table><tr><td>Exposure 30</td></tr></table>",
                        "block_bbox": [120, 1000, 560, 1200],
                    },
                ],
            }
        }
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            result = paddle_ocr.apply_document_layout(page, plan, layout, words, [])
        body = [r for r in result["regions"] if not r["table"]]
        self.assertEqual(len(body), 1)
        self.assertIn("0.1 mm", body[0]["text"])
        self.assertTrue(body[0]["rich"])
        self.assertIn(value, result["protected"])
        self.assertEqual(
            [r["text"] for r in result["regions"] if r["table"]], ["Exposure"]
        )

    def test_api_requests_word_boxes_and_never_sends_token_to_download_host(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.pdf"
            source.write_bytes(b"synthetic upload")
            response = SimpleNamespace(
                status_code=200, json=lambda: {"data": {"jobId": "job"}}
            )
            status = SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": "https://download.example/result"},
                    }
                },
            )
            result = SimpleNamespace(
                status_code=200,
                text=json.dumps(
                    {
                        "result": {
                            "ocrResults": [{"prunedResult": {"rec_texts": ["Sample"]}}]
                        }
                    }
                ),
            )
            with (
                patch.object(
                    paddle_ocr.requests, "post", return_value=response
                ) as submit,
                patch.object(
                    paddle_ocr.requests, "get", side_effect=[status, result]
                ) as get,
            ):
                pages = paddle_ocr.fetch_pages(source, "example-token", lambda _: None)
            self.assertEqual(len(pages), 1)
            options = json.loads(submit.call_args.kwargs["data"]["optionalPayload"])
            self.assertTrue(options["returnWordBox"])
            self.assertFalse(options["useDocUnwarping"])
            self.assertNotIn("headers", get.call_args_list[-1].kwargs)

    def test_auth_error_is_actionable_without_echoing_response_or_token(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.pdf"
            source.write_bytes(b"synthetic upload")
            with patch.object(
                paddle_ocr.requests,
                "post",
                return_value=SimpleNamespace(status_code=401),
            ):
                with self.assertRaisesRegex(RuntimeError, "认证失败"):
                    paddle_ocr.fetch_pages(source, "example-token", lambda _: None)

    def test_native_bypass_never_uploads_or_requires_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "native.pdf"
            with fitz.open() as doc:
                doc.new_page().insert_text((72, 72), "Native text")
                doc.save(source)
            before = source.read_bytes()
            with patch.object(
                paddle_ocr, "fetch_pages", side_effect=AssertionError("must bypass")
            ):
                result = paddle_ocr.add_text_layer(
                    source, Path(folder) / "ocr.pdf", lambda _: None
                )
            self.assertEqual(result["path"], str(source))
            self.assertEqual(source.read_bytes(), before)

    def test_exact_word_boxes_and_paragraph_grouping_without_guessed_widths(self):
        result = {
            "return_word_box": True,
            "rec_texts": ["Some prose", "continues here"],
            "rec_boxes": [[100, 100, 500, 140], [100, 145, 480, 185]],
            "text_word": [["Some", " ", "prose"], ["continues", "here"]],
            "text_word_boxes": [
                [[100, 100, 190, 140], [190, 100, 210, 140], [210, 100, 500, 140]],
                [[100, 145, 370, 185], [380, 145, 480, 185]],
            ],
        }
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            for point, text in [
                ((50, 68), "Some"),
                ((105, 68), "prose"),
                ((50, 90), "continues"),
                ((190, 90), "here"),
            ]:
                page.insert_text(point, text, fontsize=20)
            words, cells = paddle_ocr.word_geometry(result, page)
        self.assertLessEqual(words[0][0], 51)
        self.assertLessEqual(words[1][0], 106)
        self.assertLess(
            words[1][2], 200
        )  # The actual ink, not the oversized CTC envelope.
        self.assertEqual(words[0][5], words[-1][5])
        self.assertFalse(cells)
        result["return_word_box"] = False
        with self.assertRaisesRegex(RuntimeError, "逐词坐标"):
            paddle_ocr.word_geometry(result, None)


if __name__ == "__main__":
    unittest.main()
