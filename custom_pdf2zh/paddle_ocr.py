"""Hosted PP-OCRv6 recognition with word coordinates for scan translation."""

import json
import hashlib
import math
import re
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
import threading
from html.parser import HTMLParser
from difflib import SequenceMatcher

import numpy as np

import pymupdf as fitz
import requests

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"


def receipt_path(source_key, token, model):
    key = hashlib.sha256(
        json.dumps(
            [
                source_key,
                hashlib.sha256(token.encode()).hexdigest(),
                model,
                "word-box-v1",
            ]
        ).encode()
    ).hexdigest()
    return Path.home() / ".cache/pdf2zh/paddle-jobs" / (key + ".json")


def document_key(source, pages):
    digest = hashlib.sha256(Path(source).read_bytes())
    digest.update(json.dumps(pages).encode())
    return digest.hexdigest()


def fetch_pages(source, token, emit, model="PP-OCRv6", source_key=None):
    if not token:
        raise RuntimeError("请在扫描件识别设置中填写 PaddleOCR 访问令牌")
    headers = {"Authorization": "bearer " + token}

    def checked(response):
        if response.status_code in (401, 403):
            raise RuntimeError("PaddleOCR 认证失败，请检查访问令牌")
        if response.status_code == 429:
            raise RuntimeError("PaddleOCR 额度或请求频率受限，请稍后重试")
        if response.status_code != 200:
            try:
                error = response.json()
                detail = str(
                    error.get("errorMsg")
                    or error.get("message")
                    or error.get("msg")
                    or ""
                )
            except Exception:
                detail = ""
            detail = re.sub(r"https?://\S+", "[URL]", detail.replace(token, "***"))[
                :200
            ]
            raise RuntimeError(
                f"PaddleOCR 服务返回 HTTP {response.status_code} {detail}".strip()
            )
        return response.json()

    receipt = receipt_path(source_key, token, model) if source_key else None
    job = None
    if receipt and receipt.exists() and os.environ.get("PDF2ZH_OCR_REFRESH") != "1":
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            if saved.get("source_key") == source_key and saved.get("model") == model:
                job = saved["jobId"]
        except (ValueError, KeyError):
            pass
    resumed = bool(job)
    emit(
        {
            "stage": (
                f"复用同一文件的 {model} 识别任务"
                if job
                else f"上传扫描页到 {model} 云端"
            )
        }
    )
    options = {"useDocOrientationClassify": False, "useDocUnwarping": False}
    options.update(
        {"useTextlineOrientation": False, "returnWordBox": True}
        if model == "PP-OCRv6"
        else {"useChartRecognition": False}
    )
    if not job:
        with Path(source).open("rb") as stream:
            submitted = checked(
                requests.post(
                    JOB_URL,
                    headers=headers,
                    files={"file": stream},
                    data={
                        "model": model,
                        "optionalPayload": json.dumps(options),
                    },
                    timeout=(300, 300),
                )
            )
        job = submitted["data"]["jobId"]
        if receipt:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            partial = receipt.with_suffix(f".{os.getpid()}.part")
            partial.write_text(
                json.dumps({"source_key": source_key, "model": model, "jobId": job}),
                encoding="utf-8",
            )
            partial.replace(receipt)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = requests.get(JOB_URL + "/" + job, headers=headers, timeout=(30, 90))
        if receipt and resumed and status.status_code in (404, 410):
            receipt.unlink(missing_ok=True)
            return fetch_pages(source, token, emit, model, source_key)
        data = checked(status)["data"]
        state = data["state"]
        count = data.get("extractProgress", {}).get("extractedPages", 0)
        emit({"stage": f"{model} 识别扫描页（已完成 {count} 页）"})
        if state == "failed":
            if receipt:
                receipt.unlink(missing_ok=True)
            raise RuntimeError("PaddleOCR 云端识别失败，请在服务端检查任务状态")
        if state == "done":
            # Signed download URLs receive no API authorization header.
            result = requests.get(data["resultUrl"]["jsonUrl"], timeout=(30, 180))
            if result.status_code != 200:
                raise RuntimeError(f"PaddleOCR 结果下载失败：HTTP {result.status_code}")
            pages = []
            for line in result.text.splitlines():
                if line.strip():
                    row = json.loads(line)["result"]
                    pages.extend(
                        row[
                            (
                                "ocrResults"
                                if model == "PP-OCRv6"
                                else "layoutParsingResults"
                            )
                        ]
                    )
            return pages
        time.sleep(3)
    raise RuntimeError("PaddleOCR 识别等待超时，请稍后重试")


def word_geometry(result, page):
    """Use returned CTC word boxes, never infer word widths from string length."""
    result = result.get("prunedResult", result)
    if not result.get("return_word_box") or "text_word_boxes" not in result:
        raise RuntimeError("PaddleOCR 未返回逐词坐标，拒绝使用估算坐标排版")
    counts = {
        len(result[key])
        for key in ("rec_texts", "rec_boxes", "text_word", "text_word_boxes")
    }
    if len(counts) != 1:
        raise RuntimeError("PaddleOCR 文字行与坐标数量不符，已停止翻译")
    # Official PDF input is rasterized at 144dpi; rotation/unwarping are disabled.
    sx = page.rect.width / math.ceil(page.rect.width * 2)
    sy = page.rect.height / math.ceil(page.rect.height * 2)
    image = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pixels = np.frombuffer(image.samples, np.uint8).reshape(
        image.height, image.width, image.n
    )
    line_boxes = [fitz.Rect(b) for b in result["rec_boxes"]]
    envelopes = []
    for rect in line_boxes:
        cy = (rect.y0 + rect.y1) / 2
        top, bottom = rect.y0, rect.y1
        for other in line_boxes:
            overlap = min(rect.x1, other.x1) - max(rect.x0, other.x0)
            if overlap <= min(rect.width, other.width) * 0.5:
                continue
            oy = (other.y0 + other.y1) / 2
            if oy < cy - min(rect.height, other.height) * 0.5:
                top = max(top, (cy + oy) / 2)
            if oy > cy + min(rect.height, other.height) * 0.5:
                bottom = min(bottom, (cy + oy) / 2)
        envelopes.append((top, bottom))
    words, extra_cells = [], []
    previous, block, line_id = None, -1, -1
    for text, raw_box, tokens, boxes in zip(
        result["rec_texts"],
        result["rec_boxes"],
        result["text_word"],
        result["text_word_boxes"],
    ):
        if len(tokens) != len(boxes):
            raise RuntimeError("PaddleOCR 文字与坐标数量不符")
        line_id += 1
        y0, y1 = envelopes[line_id]
        box = fitz.Rect(raw_box) * fitz.Matrix(sx, sy)
        gap = box.y0 - previous.y1 if previous else 100000
        same_paragraph = (
            previous
            and 0 <= gap <= min(box.height, previous.height) * 0.65
            and abs(box.x0 - previous.x0) < 45
            and min(box.height, previous.height) / max(box.height, previous.height)
            > 0.72
        )
        if not same_paragraph:
            block += 1
        line_words = []
        for i, (token, points) in enumerate(zip(tokens, boxes)):
            if not token.strip():
                continue
            token = token.strip()
            # CTC word boxes describe alignment centres and can omit a glyph's
            # left stem. Whitespace boundaries and the detected line envelope
            # cover the complete ink, without estimating width by text length.
            left = (
                raw_box[0]
                if i == 0
                else (
                    (boxes[i - 1][0] + boxes[i - 1][2]) / 2
                    if not tokens[i - 1].strip()
                    else (boxes[i - 1][2] + points[0]) / 2
                )
            )
            right = (
                raw_box[2]
                if i == len(tokens) - 1
                else (
                    (boxes[i + 1][0] + boxes[i + 1][2]) / 2
                    if not tokens[i + 1].strip()
                    else (points[2] + boxes[i + 1][0]) / 2
                )
            )
            x0, x1 = max(0, math.ceil(left)), min(image.width, math.floor(right))
            iy0, iy1 = max(0, math.ceil(y0)), min(image.height, math.floor(y1))
            crop = pixels[iy0:iy1, x0:x1, :3]
            ys, xs = np.where(np.min(crop, axis=2) < 180) if crop.size else ([], [])
            if not len(xs):
                continue
            rect = fitz.Rect(
                x0 + int(min(xs)),
                iy0 + int(min(ys)),
                x0 + int(max(xs)) + 1,
                iy0 + int(max(ys)) + 1,
            ) * fitz.Matrix(sx, sy)
            line_words.append((*list(rect), token, block, line_id, i))
        # Contents/index rows have an isolated right-hand page number. Treat
        # each as a bounded row so translation cannot absorb its page number.
        if (
            line_words
            and line_words[-1][4].strip(".,").isdigit()
            and line_words[-1][0] > page.rect.width * 0.85
            and box.x0 < page.rect.width * 0.4
        ):
            extra_cells.append(list(box + (-2, -2, 2, 2)))
        words.extend(line_words)
        previous = box
    return words, extra_cells


def apply_document_layout(page, plan, layout, words, extra_cells, references=None):
    """VL paragraphs with measured table geometry and original display formulas."""
    from custom_pdf2zh.ocr_layout import has_prose, readable_crop
    from custom_pdf2zh.scan_math import inline_math
    from custom_pdf2zh.scan_render import technical

    layout = layout["prunedResult"]
    blocks = layout["parsing_res_list"]
    matrix = fitz.Matrix(
        page.rect.width / layout["width"], page.rect.height / layout["height"]
    )
    cells = [fitz.Rect(c) for c in plan["cells"]]
    plan["protected"] = [
        r
        for r in plan["protected"]
        if any((fitz.Rect(r).tl + fitz.Rect(r).br) / 2 in c for c in cells)
    ]
    plan["regions"] = [r for r in plan["regions"] if r["table"]]
    pix = page.get_pixmap(alpha=False, annots=False)
    pixels = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    mapped = [(b, fitz.Rect(b["block_bbox"]) * matrix) for b in blocks]
    contents = [rect for block, rect in mapped if block["block_label"] == "content"]
    if contents:
        # Some APIs return the page-number column as separate OCR lines. Join
        # by measured vertical position instead of translating the entire TOC
        # as a paragraph, which would collapse its right-aligned page numbers.
        from custom_pdf2zh.ocr_layout import text_plan

        rows = []
        selected = sorted(
            (
                w
                for w in words
                if any(
                    fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in rect
                    for rect in contents
                )
            ),
            key=lambda w: ((w[1] + w[3]) / 2, w[0]),
        )
        for word in selected:
            center = (word[1] + word[3]) / 2
            if not rows or abs(center - rows[-1][0]) > max(
                3, (word[3] - word[1]) * 0.6
            ):
                rows.append((center, []))
            rows[-1][1].append(word)
        if len(rows) > 3:
            boxes = []
            roman_pages = []
            ordered = [w for w in words if w not in selected]
            for _, row in rows:
                row.sort(key=lambda w: w[0])
                boxes.append(
                    [
                        min(w[0] for w in row) - 2,
                        min(w[1] for w in row) - 2,
                        max(w[2] for w in row) + 2,
                        max(w[3] for w in row) + 2,
                    ]
                )
                if (
                    len(row) > 1
                    and row[-1][0] > page.rect.width * 0.8
                    and re.fullmatch(
                        r"(?=[IVXLCDM]+$)M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})",
                        row[-1][4].upper(),
                    )
                ):
                    roman_pages.append(list(row.pop()[:4]))
                ordered.extend(row)
            result = text_plan(page, ordered, extra_cells=boxes)[1]
            result["protected"].extend(roman_pages)
            return result

    class Cells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.current = None
            self.items = []
            self.raw_items = []

        def handle_starttag(self, tag, attrs):
            if tag in {"td", "th"}:
                self.current = []
            elif tag == "br" and self.current is not None:
                self.current.append(" ")

        def handle_data(self, data):
            if self.current is not None:
                self.current.append(data)

        def handle_endtag(self, tag):
            if tag in {"td", "th"} and self.current is not None:
                raw = "".join(self.current).strip()
                raw = re.sub(r"\bor(?=PFA\b)", "or ", raw)
                raw = re.sub(
                    r"\b(GJ/m|kg/m)([23])\b",
                    lambda m: m[1] + {"2": "²", "3": "³"}[m[2]],
                    raw,
                )

                def reference(match):
                    key = re.sub(r"\s+", "", match[1]).upper()
                    number = (references or {}).get(key)
                    if not number:
                        return match[0]
                    repaired = (
                        match[0][: match.start(2) - match.start()]
                        + number
                        + match[0][match.end(2) - match.start() :]
                    )
                    plan.setdefault("ocr_repairs", []).append(
                        {
                            "before": match[0],
                            "after": repaired,
                            "evidence": "document bibliography standard identifier",
                        }
                    )
                    return repaired

                raw = re.sub(
                    r"((?:ISO|CEN/TS)\s*[\d:-]+)\s*\([^)]*?Bibliography\s*\[([A-Za-z])\]",
                    reference,
                    raw,
                )
                self.raw_items.append(raw)
                self.items.append(inline_math(raw).strip())
                self.current = None

    def narrative(area, paragraphs, erase_area=None):
        # A complete spanning paragraph owns its area, including old OCR
        # islands. Keeping those islands would leave source-language ghosts.
        area = fitz.Rect(area)
        erase_area = fitz.Rect(erase_area or area)
        center_in = (
            lambda raw: (fitz.Rect(raw).tl + fitz.Rect(raw).br) / 2 in erase_area
        )
        plan["regions"] = [r for r in plan["regions"] if not center_in(r["box"])]
        plan["protected"] = [r for r in plan["protected"] if not center_in(r)]
        plan["regions"].append(
            {
                "text": "\n".join(p["text"] for p in paragraphs),
                "paragraphs": paragraphs,
                "box": list(area),
                "erase": [list(erase_area)],
                "size": min(page.rect.width * 0.016, 21),
                "table": True,
                "rich": True,
                "limit": list(erase_area),
            }
        )

    for block, table_box in mapped:
        if block["block_label"] != "table":
            continue
        if not any((c.tl + c.br) / 2 in table_box for c in cells):
            # Borderless symbol/abbreviation lists are also tables. Preserve
            # the measured left column and translate the description column.
            from custom_pdf2zh.scan_render import regions_from_words

            parser = Cells()
            parser.feed(block["block_content"])
            notes = [x for x in parser.raw_items if re.match(r"^NOTE\b", x)]
            note_words = [
                w
                for w in words
                if w[4].upper() == "NOTE"
                and fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in table_box
            ]
            note_top = min((w[1] for w in note_words), default=table_box.y1 + 1)
            descriptions = [
                x
                for x in parser.items
                if has_prose(x) and not re.search(r"\d|[=<>≥≤±×÷°²³]", x)
            ]

            rows = []
            for word in sorted(
                (
                    w
                    for w in words
                    if fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in table_box
                ),
                key=lambda w: ((w[1] + w[3]) / 2, w[0]),
            ):
                center = (word[1] + word[3]) / 2
                if not rows or abs(center - rows[-1][0]) > max(
                    3, (word[3] - word[1]) * 0.6
                ):
                    rows.append((center, []))
                rows[-1][1].append(word)
            for _, row in rows:
                if min(w[1] for w in row) >= note_top - 2:
                    continue
                row.sort(key=lambda w: w[0])
                gaps = [(row[i + 1][0] - row[i][2], i + 1) for i in range(len(row) - 1)]
                if not gaps:
                    continue
                gap, split = max(gaps)
                if gap < max(w[3] - w[1] for w in row) * 1.5:
                    continue
                left, right = row[:split], row[split:]
                plan["protected"].extend([list(w[:4]) for w in left])
                box = [
                    right[0][0] - 2,
                    min(w[1] for w in right) - 2,
                    table_box.x1,
                    max(w[3] for w in right) + 3,
                ]
                compact = lambda text: re.sub(r"[^a-z]", "", text.lower())
                source_text = " ".join(w[4] for w in right)
                ranked = sorted(
                    (
                        (
                            SequenceMatcher(
                                None, compact(source_text), compact(x)
                            ).ratio(),
                            x,
                        )
                        for x in descriptions
                    ),
                    reverse=True,
                )
                if ranked and ranked[0][0] >= 0.75:
                    plan["regions"].append(
                        {
                            "text": ranked[0][1],
                            "box": box,
                            "erase": [box],
                            "size": min(page.rect.width * 0.017, 22),
                            "table": True,
                            "rich": True,
                            "limit": box,
                        }
                    )
                    continue
                regions, protected = regions_from_words(
                    right, [box], page.rect.width, page.rect.height
                )
                plan["regions"].extend(regions)
                plan["protected"].extend(protected)
            if notes and note_words:
                note_ink = [
                    w for w in words if note_top - 2 <= w[1] <= table_box.y1 + 2
                ]
                area = fitz.Rect(
                    min([table_box.x0] + [w[0] - 2 for w in note_ink]),
                    note_top - 2,
                    max([table_box.x1] + [w[2] + 2 for w in note_ink]),
                    max([table_box.y1 + 2] + [w[3] + 2 for w in note_ink]),
                )
                narrative(
                    area, [{"label": "NOTE", "text": re.sub(r"^NOTE\s*", "", notes[0])}]
                )
            continue
        parser = Cells()
        parser.feed(block["block_content"])
        from custom_pdf2zh.scan_narrative import split_footnotes

        for raw in parser.raw_items:
            paragraphs = split_footnotes(raw)
            if not paragraphs:
                continue
            compact = lambda value: re.sub(r"[^a-z]", "", value.lower())
            for cell in cells:
                if (
                    cell.width < page.rect.width * 0.7
                    or (cell.tl + cell.br) / 2 not in table_box
                ):
                    continue
                source = " ".join(
                    w[4]
                    for w in words
                    if fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in cell
                )
                if (
                    SequenceMatcher(
                        None, compact(source), compact(inline_math(raw))
                    ).ratio()
                    >= 0.85
                ):
                    narrative(
                        cell + (3, 3, -3, -3), paragraphs, cell + (0.5, 0.5, -0.5, -0.5)
                    )
        candidates = [
            x
            for x in parser.items
            if has_prose(x) and not re.search(r"\d|[=<>≥≤±×÷°²³]", x)
        ]
        compact = lambda text: re.sub(r"[^a-z]", "", text.lower())
        for cell in cells:
            if (cell.tl + cell.br) / 2 not in table_box:
                continue
            inside = [
                w
                for w in words
                if (fitz.Rect(w[:4]).tl + fitz.Rect(w[:4]).br) / 2 in cell
            ]
            source_text = " ".join(w[4] for w in inside)
            if not has_prose(source_text) or re.search(
                r"\d|[=<>≥≤±×÷°²³]", source_text
            ):
                continue
            ranked = sorted(
                (
                    (SequenceMatcher(None, compact(source_text), compact(x)).ratio(), x)
                    for x in candidates
                ),
                reverse=True,
            )
            if not ranked or ranked[0][0] < 0.86:
                continue
            limit = cell + (1, 1, -1, -1)
            plan["regions"] = [
                r
                for r in plan["regions"]
                if (fitz.Rect(r["box"]).tl + fitz.Rect(r["box"]).br) / 2 not in cell
            ]
            plan["protected"] = [
                r
                for r in plan["protected"]
                if (fitz.Rect(r).tl + fitz.Rect(r).br) / 2 not in cell
            ]
            plan["regions"].append(
                {
                    "text": ranked[0][1],
                    "box": list(limit),
                    "erase": [list(limit)],
                    "size": page.rect.width * 0.017,
                    "table": True,
                    "rich": True,
                    "limit": list(limit),
                }
            )
    for block, rect in mapped:
        kind, text = block["block_label"], block["block_content"]
        if kind in {"header", "number", "display_formula", "inline_formula"}:
            plan["protected"].append(list(rect))
            continue
        if kind not in {
            "text",
            "doc_title",
            "paragraph_title",
            "figure_title",
            "footer",
            "content",
            "reference_content",
        }:
            continue
        text = inline_math(text).strip()
        if kind == "doc_title":
            text = re.sub(r"\s*[—–]+\s*$", "", text)
        if not has_prose(text):
            plan["protected"].append(list(rect))
            continue
        x0, y0, x1, y1 = (rect & page.rect).irect
        if not readable_crop(pixels[max(y0, 0) : y1, max(x0, 0) : x1, :3]):
            continue
        selected = [
            w for w in words if (fitz.Rect(w[:4]).tl + fitz.Rect(w[:4]).br) / 2 in rect
        ]
        target = fitz.Rect(rect)
        prefix = re.match(
            r"^(?:\d+(?:\.\d+)*\s+|©\s*ISO\s*\d{4}\s*[-–—]?\s*|\[\d+\]\s*(?:(?:ISO|CEN/TS)\s*[\d:-]+,?\s*)?)",
            text,
        )
        if prefix:
            for word in selected:
                if not technical(word[4]):
                    break
                plan["protected"].append(list(word[:4]))
                target.x0 = max(target.x0, word[2] + 3)
            text = text[prefix.end() :]
        next_y = min(
            [
                other.y0 - 2
                for _, other in mapped
                if other.y0 >= rect.y1
                and min(other.x1, rect.x1) > max(other.x0, rect.x0)
            ]
            or [page.rect.height - 2]
        )
        heights = sorted(w[3] - w[1] for w in selected if w[3] > w[1])
        size = (
            heights[len(heights) // 2] if heights else min(22, rect.height * 0.75)
        ) * 1.1
        target.y1 = max(target.y1, min(rect.y1 + size * 0.5, next_y))
        if kind in {"paragraph_title", "doc_title"}:
            right = min(
                [
                    other.x0 - 5
                    for _, other in mapped
                    if other.x0 >= rect.x1
                    and min(other.y1, rect.y1) > max(other.y0, rect.y0)
                ]
                or [page.rect.width - 50]
            )
            target.x1 = max(target.x1, min(right, rect.x0 + page.rect.width * 0.7))
        plan["regions"].append(
            {
                "text": text,
                "box": list(target),
                "erase": [list(rect + (-1.5, -1.5, 1.5, 1.5))],
                "size": size,
                "table": False,
                "rich": True,
                "limit": list(page.rect),
            }
        )
    # Reconcile small text omitted by layout detection (for example a blank
    # change-log table's Date header). Expanded ownership prevents duplicate
    # translation of word boxes that slightly exceed a layout rectangle.
    owned = [r for _, r in mapped] + [
        fitz.Rect(box) for r in plan["regions"] for box in r.get("erase", [r["box"]])
    ]
    missing = []
    for word in words:
        box = fitz.Rect(word[:4])
        mid = (box.tl + box.br) / 2
        pad = max(3, box.height * 0.4)
        if any(mid in (area + (-pad, -pad, pad, pad)) for area in owned):
            continue
        x0, y0, x1, y1 = (box & page.rect).irect
        if has_prose(word[4]) and readable_crop(
            pixels[max(y0, 0) : y1, max(x0, 0) : x1, :3]
        ):
            missing.append(word)
    if missing:
        from custom_pdf2zh.scan_render import regions_from_words

        recovered, protected = regions_from_words(
            missing, [], page.rect.width, page.rect.height
        )
        plan["regions"].extend(recovered)
        plan["protected"].extend(protected)
        plan["coverage_recovered"] = [w[4] for w in missing]
    return plan


def add_text_layer(source, destination, emit):
    from custom_pdf2zh.ocr_layout import text_plan
    from custom_pdf2zh.workbench_ocr import image_only_pages

    source, destination = Path(source), Path(destination)
    candidates = image_only_pages(source)
    if not candidates:
        return {"path": str(source), "pages": [], "empty_pages": []}
    token = os.environ.get("PDF2ZH_PADDLEOCR_TOKEN") or os.environ.get(
        "PADDLEOCR_ACCESS_TOKEN", ""
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    upload = destination.with_suffix(".upload.pdf")
    source_key = document_key(source, candidates)
    try:
        with fitz.open(source) as selected:
            selected.select(candidates)
            for page in selected:
                page.clean_contents(sanitize=True)
            selected.save(upload, garbage=4, deflate=True)
        hybrid = os.environ.get("PDF2ZH_OCR_ENGINE") == "paddle-hybrid"
        layouts_vl = None
        if hybrid:
            lock = threading.Lock()

            def progress(event):
                with lock:
                    emit(event)

            with ThreadPoolExecutor(max_workers=2) as pool:
                geometry_job = pool.submit(
                    fetch_pages, upload, token, progress, source_key=source_key
                )
                content_job = pool.submit(
                    fetch_pages,
                    upload,
                    token,
                    progress,
                    "PaddleOCR-VL-1.6",
                    source_key=source_key,
                )
                pages, layouts_vl = geometry_job.result(), content_job.result()
        else:
            pages = fetch_pages(upload, token, emit, source_key=source_key)
    finally:
        upload.unlink(missing_ok=True)
    if len(pages) != len(candidates):
        raise RuntimeError("PaddleOCR 返回页数不符，已停止翻译")
    if layouts_vl is not None and len(layouts_vl) != len(candidates):
        raise RuntimeError("PaddleOCR-VL 返回页数不符，已停止翻译")
    font = fitz.Font("cjk")
    layouts = {
        "_source": str(source),
        "_ocr_engine": "paddle-hybrid" if hybrid else "PP-OCRv6",
    }
    titles = " ".join(
        block["block_content"]
        for parsed in (layouts_vl or [])[:8]
        for block in parsed["prunedResult"]["parsing_res_list"]
        if block["block_label"] in {"doc_title", "text"}
    ).lower()
    if "plastics piping systems" in titles and "polyethylene" in titles:
        layouts["_domain"] = "polyethylene-piping"
    recognized, empty = [], []
    references = {}
    for parsed in layouts_vl or []:
        for block in parsed["prunedResult"]["parsing_res_list"]:
            if block["block_label"] != "reference_content":
                continue
            match = re.match(
                r"\[(\d+)\]\s*((?:ISO|CEN/TS)\s*[\d:-]+)", block["block_content"]
            )
            if match:
                key = re.sub(r"\s+", "", match[2]).upper()
                references[key] = (
                    match[1]
                    if key not in references or references[key] == match[1]
                    else None
                )
    with fitz.open(source) as doc:
        for position, (index, result) in enumerate(zip(candidates, pages)):
            emit({"stage": f"构建 OCR 版面（{position + 1}/{len(pages)}）"})
            page = doc[index]
            words, extra_cells = word_geometry(result, page)
            if layouts_vl is not None and not any(
                block["block_label"] == "content"
                for block in layouts_vl[position]["prunedResult"]["parsing_res_list"]
            ):
                extra_cells = []
            if not words:
                empty.append(index + 1)
                continue
            raw_words = words
            words, layouts[str(index)] = text_plan(page, words, extra_cells=extra_cells)
            if layouts_vl is not None:
                layouts[str(index)] = apply_document_layout(
                    page,
                    layouts[str(index)],
                    layouts_vl[position],
                    raw_words,
                    extra_cells,
                    references,
                )
            page.insert_font(fontname="OCRSource", fontbuffer=font.buffer)
            for word in words:
                box, text = fitz.Rect(word[:4]), word[4]
                size = box.height / (font.ascender - font.descender)
                width = font.text_length(text, fontsize=size)
                if size <= 0 or width <= 0:
                    continue
                origin = fitz.Point(box.x0, box.y0 + size * font.ascender)
                page.insert_text(
                    origin,
                    text,
                    fontname="OCRSource",
                    fontsize=size,
                    render_mode=3,
                    morph=(origin, fitz.Matrix(box.width / width, 1)),
                )
            recognized.append(index + 1)
        if not recognized:
            raise RuntimeError("PaddleOCR 未识别出文字，已停止翻译")
        doc.save(destination, garbage=3, deflate=True)
    destination.with_suffix(".ocr.json").write_text(
        json.dumps(layouts), encoding="utf-8"
    )
    return {"path": str(destination), "pages": recognized, "empty_pages": empty}
