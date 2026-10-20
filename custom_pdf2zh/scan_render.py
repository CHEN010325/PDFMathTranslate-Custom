"""Bounded scan compositor. Numerical table content is never transcribed."""

from collections import defaultdict
import base64
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace

import pymupdf as fitz

UNITS = {
    "mm",
    "kg",
    "mg",
    "g",
    "h",
    "min",
    "bar",
    "mpa",
    "gj",
    "m",
    "m²",
    "m³",
    "°c",
    "iso",
    "en",
    "bs",
    "pe",
    "sdr",
    "mfr",
    "pn",
    "pfa",
    "pc",
    "cen",
    "ts",
    "mrs",
    "dn",
    "od",
    "mop",
    "astm",
    "din",
    "emin",
    "emax",
}


def technical(word):
    if re.fullmatch(r"[-=—–]+[A-Za-z]{4,}", word):
        return False  # OCR can mistake a list dash for '=' before a label.
    text = word.strip("()[]{}.,;:").lower()
    return (
        bool(re.search(r"[\d=<>≥≤±×÷€σπ°²³]", word))
        or bool(re.fullmatch(r"(?:kg|mg|g|gj|n)/(?:kg|m)[?²³23]?", text))
        or bool(re.fullmatch(r"PE[A-Z0-9]+", word.strip("(),;:")))
        or word.strip("(),;:") in {"Al", "AI"}  # A1 is often read as Al/AI.
        or text in UNITS | {"minh", "mins"}
        or not any(c.isalnum() for c in word)
        or len(text) == 1
    )


LABELS = {
    "date": "日期",
    "annex": "附录",
    "informative": "资料性",
    "normative": "规范性",
    "terms and definitions": "术语和定义",
    "symbols and abbreviated terms": "符号和缩略语",
    "abbreviated terms": "缩略语",
    "symbols": "符号",
    "material terms": "材料术语",
    "geometrical terms": "几何术语",
    "terms related to material characteristics": "与材料特性相关的术语",
    "terms related to service conditions": "与使用条件相关的术语",
    "terms related to joints": "与接头相关的术语",
    "pressure reduction factor": "压力折减系数",
    "pressure reduction factors": "压力折减系数",
    "maximum mean outside diameter": "最大平均外径",
    "minimum mean outside diameter": "最小平均外径",
    "nominal wall thickness": "公称壁厚",
    "characteristic": "特性",
    "characteristics": "特性",
    "requirements": "要求",
    "test method": "试验方法",
    "test parameters": "试验参数",
    "parameter": "参数",
    "value": "数值",
    "test temperature": "试验温度",
    "test period": "试验时间",
    "time": "时间",
    "number of test pieces": "试样数量",
    "number of samples": "试样数量",
    "loading mass": "负荷质量",
    "preparation of test pieces": "试样制备",
    "pipe diameter": "管径",
    "pipe diameter ratio": "管径比",
    "free": "自由",
    "test atmosphere": "试验气氛",
    "test medium": "试验介质",
    "oxygen": "氧气",
    "air": "空气",
    "nominal value": "公称值",
    "maximum deviation": "最大偏差",
    "mass fraction": "质量分数",
    "where": "式中",
    "coefficient": "系数",
    "temperature": "温度",
    "or": "或",
    "arrest": "止裂",
    "butt-fusion": "热熔对接",
    "butt-fusion joint": "热熔对接接头",
    "shall conform to": "应符合",
    "according to": "按照",
    "internal test pressure for": "内部试验压力，适用于",
    "deviation from nominal value": "相对公称值的偏差",
    "at least": "至少",
    "and": "和",
    "for": "用于",
    "at": "在",
    "to": "至",
    "table": "表",
    "by mass": "质量分数",
    "equivalent to": "相当于",
    "melt mass-flow rate": "熔体质量流动速率",
    "maximum deviation of": "最大偏差为",
    "of the nominated value": "（相对标称值）",
    "grade": "等级",
    "rating of dispersion": "分散等级",
    "pigment dispersion": "颜料分散性",
    "carbon black dispersion": "炭黑分散性",
    "carbon black content": "炭黑含量",
    "compound density": "混配料密度",
    "water content": "含水量",
    "volatile content": "挥发分含量",
    "water-in-water": "水-水",
    "type of test": "试验类型",
    "test to failure: ductile": "试验至破坏：韧性破坏",
    "pass brittle": "合格；脆性破坏",
    "fail": "不合格",
    "tensile strength for butt-fusion": "热熔对接接头的拉伸强度",
    "preconditioning: cumulative radiant exposure": "预处理：累计辐射暴露量",
    "of samples": "试样的",
    "below": "如下",
    "oxidation induction time": "氧化诱导时间",
    "thermal stability": "热稳定性",
    "resistance to weathering": "耐候性",
    "resistance to slow crack growth": "耐慢速裂纹增长",
    "resistance to rapid crack propagation": "耐快速裂纹扩展",
    "test to failure: ductile — pass brittle — fail": "试验至破坏：韧性破坏—合格；脆性破坏—不合格",
    "test to failure: ductile pass brittle — fail": "试验至破坏：韧性破坏—合格；脆性破坏—不合格",
    "hydrostatic strength": "静液压强度",
    "elongation at break": "断裂伸长率",
    "sample preparation dimension jointing": "试样制备、尺寸、连接",
}


def label_translation(text, lang_out):
    superscript = "^(" in text
    text = re.sub(r"\^\(([^)]*)\)", r"\1", text)
    normalized = re.sub(r"[®©?!”“*]", "", text).strip(" .,:;()\n").lower()
    normalized = normalized.lstrip("-=—– ")
    normalized = normalized.replace("oftest", "of test").replace("ofthe", "of the")
    normalized = re.sub(r"\bpieces[d]\b", "pieces", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s*:\s*", ": ", normalized)
    normalized = re.sub(r"\s*[—–]\s*", " — ", normalized)
    if lang_out.lower().startswith("zh") and normalized in LABELS:
        return LABELS[normalized]
    if lang_out.lower().startswith("zh"):
        for key, translation in LABELS.items():
            if normalized.startswith(key):
                footnote = normalized[len(key) :].replace(" ", "")
                if re.fullmatch(r"[a-i](?:,?[a-i]){0,2}", footnote):
                    return translation + ("^" if superscript else "") + f"({footnote})"


def translate_context(translator, text, lang_out):
    label = label_translation(text, lang_out)
    if label is not None:
        return label
    text = re.sub(
        r"\b(requirements|pieces|samples|content|dispersion|temperature|fusion|exposure|weathering|propagation)([a-i]{1,3})\b",
        r"\1 (\2)",
        text,
        flags=re.IGNORECASE,
    )
    return translator.translate(text)


def translate_cell(translator, context, markers, lang_out):
    # Translate only prose islands. Numeric images are never serialized into
    # an instruction prompt: small translation models can echo placeholders or
    # invent full-cell text when asked for structured output.
    parts = re.split(r"\{v\d+\}", context)
    result = []
    for part in parts:
        if not part.strip():
            result.append("")
            continue
        text = translate_context(translator, part.strip(), lang_out).strip()
        if not text or re.search(r"\{v\d+\}|\[figure\]", text):
            raise RuntimeError("扫描单元格译文无效，已停止导出")
        result.append(text)
    return result


def union(words):
    box = fitz.Rect(words[0][:4])
    for word in words[1:]:
        box |= fitz.Rect(word[:4])
    return box


def regions_from_words(words, cells, width, height):
    """Group prose while retaining immutable technical islands in tables."""
    from custom_pdf2zh.ocr_layout import has_prose

    groups = defaultdict(list)
    for word in words:
        center = (fitz.Rect(word[:4]).tl + fitz.Rect(word[:4]).br) / 2
        cell = next(
            (i for i, rect in enumerate(cells) if center in fitz.Rect(rect)), None
        )
        groups[("cell", cell) if cell is not None else ("body", word[5])].append(word)
    regions, protected = [], []

    def add(items, limit, table=False, context=None):
        if not items:
            return
        if not table:
            items = list(items)
            while items and technical(items[0][4]):
                protected.append(list(items.pop(0)[:4]))
            if not items:
                return
        text = " ".join(w[4] for w in items)
        if not has_prose(text) and context is None:
            protected.extend([list(w[:4]) for w in items])
            return
        box = union(items)
        target = (
            fitz.Rect(box.x0, box.y0 - 1, min(limit.x1, box.x1 + 4), box.y1 + 3) & limit
        )
        if table:
            target = (
                fitz.Rect(limit)
                if len(items) == len(group) and not any(technical(w[4]) for w in items)
                else fitz.Rect(
                    box.x0,
                    box.y0 - 1,
                    min(limit.x1, box.x1 + 1),
                    min(limit.y1, box.y1 + 3),
                )
            )
        size = sorted(w[3] - w[1] for w in items)[len(items) // 2] * 1.05
        if table:
            size = min(size, width * 0.017)
        line_words = defaultdict(list)
        for word in items:
            line_words[word[5:7]].append(word)
        regions.append(
            {
                "text": text,
                "box": list(target),
                # Cover the whole text line, including hyperlink underlines and
                # inter-word ink. Protected glyph islands are subtracted later.
                "erase": [
                    list(union(line) + (-2.5, -2.5, 2.5, 2.5))
                    for line in line_words.values()
                ],
                "size": size,
                "table": table,
                "limit": list(limit),
            }
        )
        if context:
            regions[-1].update(context)

    for (kind, key), group in groups.items():
        if kind == "cell":
            limit = fitz.Rect(cells[key]) + (1, 1, -1, -1)
            flags = [technical(w[4]) for w in group]
            joined = " ".join(w[4] for w in group)
            if limit.width > width * 0.7 and limit.height > 100 and len(joined) > 500:
                # A spanning narrative footnote cell needs paragraph reflow.
                # Carry its technical glyphs as inline source-image cutouts;
                # they never pass through OCR transcription or translation.
                # Keep punctuation and English articles with their sentence;
                # freezing an apostrophe or an article breaks translation.
                flags = [
                    technical(w[4])
                    and (
                        bool(re.search(r"\d|[=<>≥≤±×÷°²³]", w[4]))
                        or w[4].strip(".,;:()[]").lower() in UNITS
                        or (bool(re.fullmatch(r"[a-i]", w[4])) and w[0] < limit.x0 + 20)
                    )
                    for w in group
                ]
                parts = []
                for word, flag in zip(group, flags):
                    line = list(word[5:7]) if flag else None
                    paragraph = (
                        bool(re.fullmatch(r"[a-i]", word[4]))
                        and word[0] < limit.x0 + 20
                    )
                    if (
                        not parts
                        or parts[-1]["image"] != flag
                        or (flag and parts[-1]["line"] != line)
                        or paragraph
                    ):
                        parts.append(
                            {
                                "image": flag,
                                "line": line,
                                "words": [],
                                "paragraph": paragraph,
                            }
                        )
                    parts[-1]["words"].append(word)
                for part in parts:
                    part["text"] = " ".join(w[4] for w in part["words"])
                    if part["image"]:
                        part["box"] = list(
                            union(part["words"]) + (-0.5, -0.5, 0.5, 0.5)
                        )
                    del part["words"]
                regions.append(
                    {
                        "text": joined,
                        "box": list(limit),
                        "erase": [list(limit)],
                        "size": min(width * 0.017, 21),
                        "table": True,
                        "rich": True,
                        "limit": list(limit),
                        "flow_parts": parts,
                    }
                )
                continue
            has_references = re.search(
                r"\b[a-i]\s*\)|\b(?:requirements|pieces|samples|content|dispersion|temperature|fusion|exposure|weathering|propagation)[a-i]\b",
                joined,
                re.I,
            )
            if has_references and not re.search(r"\d|[=<>≥≤±×÷°²³]", joined):
                for i, word in enumerate(group):
                    if re.fullmatch(r"[a-i]|[,():]", word[4]):
                        flags[i] = False
            for i, word in enumerate(group):
                if (
                    word[4] in {"-", "—", "–", ":", ","}
                    and 0 < i < len(group) - 1
                    and not flags[i - 1]
                    and not flags[i + 1]
                ):
                    flags[i] = False
            if not any(flags):
                add(group, limit, True)
                continue
            # A table value can share a cell with prose. Never send its numeric
            # glyphs to translation or erase them, even if OCR misreads them.
            runs = []
            for word, flag in zip(group, flags):
                if not runs or runs[-1][0] != flag:
                    runs.append((flag, []))
                runs[-1][1].append(word)
            chunks, fragments, marker = [], [], 0
            for flag, items in runs:
                if flag:
                    chunks.append("{v%d}" % marker)
                    marker += 1
                    protected.extend([list(w[:4]) for w in items])
                else:
                    chunks.append(" ".join(w[4] for w in items))
                    fragments.append((marker, items))
            context = " ".join(chunks)
            for segment, items in fragments:
                add(
                    items,
                    limit,
                    True,
                    {
                        "context_id": f"cell-{key}",
                        "context_text": context,
                        "context_segment": segment,
                        "markers": marker,
                    },
                )
        else:
            lines = defaultdict(list)
            for word in group:
                lines[word[5:7]].append(word)
            pending = []
            for line in lines.values():
                if not has_prose(" ".join(w[4] for w in line)):
                    add(pending, fitz.Rect(0, 0, width, height))
                    pending = []
                    protected.extend([list(w[:4]) for w in line])
                    continue
                if (
                    pending
                    and union(line).y0 - union(pending).y1
                    > max(w[3] - w[1] for w in line) * 1.2
                ):
                    add(pending, fitz.Rect(0, 0, width, height))
                    pending = []
                pending.extend(line)
            add(pending, fitz.Rect(0, 0, width, height))
    # Each body paragraph gets only the free vertical space before its next
    # neighbor, never an entire model-detected layout or table rectangle.
    for region in regions:
        if region["table"]:
            continue
        box = fitz.Rect(region["box"])
        next_y = height
        for other in regions:
            if other is region:
                continue
            b = fitz.Rect(other["box"])
            if b.y0 >= box.y1 and min(b.x1, box.x1) > max(b.x0, box.x0):
                next_y = min(next_y, b.y0 - 2)
        box.y1 = min(box.y1 + max(2, region["size"] * 0.3), next_y)
        region["box"] = list(box)
    return regions, protected


def subtract(rect, obstacle):
    if (rect & obstacle).is_empty:
        return [rect]
    a = rect & obstacle
    return [
        r
        for r in [
            fitz.Rect(rect.x0, rect.y0, rect.x1, a.y0),
            fitz.Rect(rect.x0, a.y1, rect.x1, rect.y1),
            fitz.Rect(rect.x0, a.y0, a.x0, a.y1),
            fitz.Rect(a.x1, a.y0, rect.x1, a.y1),
        ]
        if not r.is_empty
    ]


def table_text_flow(page, region, text, protected, font):
    """Flow across the source line fragments, around immutable value islands."""
    areas = []
    limit = fitz.Rect(region["limit"])
    for raw in region["erase"]:
        pieces = [fitz.Rect(raw) & limit]
        for item in protected:
            obstacle = fitz.Rect(item) + (-0.75, -0.75, 0.75, 0.75)
            pieces = [p for rect in pieces for p in subtract(rect, obstacle)]
        areas.extend(p for p in pieces if p.width >= 2 and p.height >= 2)
    areas.sort(key=lambda rect: (rect.y0, rect.x0))
    size = region["size"]
    while size >= region["size"] * 0.3:
        writer = fitz.TextWriter(page.rect)
        remaining = text
        for area in areas:
            safe = fitz.Rect(area)
            safe.y1 += font.descender * size
            if safe.height <= font.ascender * size or safe.width < font.text_length(
                remaining[0], fontsize=size
            ):
                continue
            overflow = writer.fill_textbox(
                safe, remaining, font=font, fontsize=size, lineheight=1.12, warn=None
            )
            if not overflow:
                remaining = ""
                break
            # Recover the exact original suffix: reflowing must not join or
            # split decimal numbers and words at a previous rectangle's wraps.
            unwritten = sum(not c.isspace() for line, _ in overflow for c in line)
            consumed = sum(not c.isspace() for c in remaining) - unwritten
            if consumed:
                offset = 0
                while consumed and offset < len(remaining):
                    consumed -= not remaining[offset].isspace()
                    offset += 1
                remaining = remaining[offset:].lstrip()
        if not remaining:
            return writer, size
        size *= 0.9
    raise RuntimeError(
        f"第{page.number+1}页“{region['text'][:60]}”译文无法放入原区域，已停止导出"
    )


def compose_page(page, plan, translations):
    from custom_pdf2zh.scan_math import rich_text

    if len(plan["regions"]) != len(translations):
        raise RuntimeError("扫描译文段落数不符，已停止导出")
    font = fitz.Font("cjk")
    page.insert_font(fontname="ScanText", fontbuffer=font.buffer)
    painted = []
    with fitz.open() as probe:
        measure = probe.new_page(width=page.rect.width, height=page.rect.height)
        measure.insert_font(fontname="ScanText", fontbuffer=font.buffer)
        for region, text in zip(plan["regions"], translations):
            if re.search(r"\{v\d+\}|\[figure\]", text):
                raise RuntimeError("扫描译文含有内部占位符，拒绝导出")
            if not text.strip() and not region.get("context_validated"):
                raise RuntimeError("OCR 段落翻译为空，拒绝擦除原文")
            box = fitz.Rect(region["box"])
            flow = (
                region["table"]
                and not region.get("rich")
                and bool(region.get("context_id"))
            )
            if not flow:
                for raw in plan.get("protected", []):
                    p = fitz.Rect(raw) + (-0.75, -0.75, 0.75, 0.75)
                    pieces = subtract(box, p)
                    box = (
                        max(pieces, key=lambda r: r.get_area())
                        if pieces
                        else fitz.Rect()
                    )
            if box.is_empty:
                raise RuntimeError("OCR 排版区域无效")
            size = region["size"]
            markup, css, rich_scale = None, None, 1.0
            writer = None
            if flow and text.strip():
                writer, size = table_text_flow(
                    page, region, text, plan.get("protected", []), font
                )
            elif region.get("rich") and text.strip():
                markup = region.get("_flow_markup") or rich_text(text)
                css = f"*{{margin:0;padding:0}} body{{font-size:{size}pt;line-height:1.12}}"
                spare, rich_scale = measure.insert_htmlbox(
                    box, markup, css=css, scale_low=0.3
                )
                if spare < 0:
                    raise RuntimeError("OCR 完整段落无法放入原区域，已停止导出")
            elif text.strip():
                while (
                    measure.insert_textbox(
                        box, text, fontname="ScanText", fontsize=size, lineheight=1.12
                    )
                    < 0
                ):
                    size *= 0.9
                    if size < region["size"] * 0.3:
                        raise RuntimeError(
                            f"第{page.number+1}页“{region['text'][:60]}”译文无法放入原区域，已停止导出"
                        )
            limit = fitz.Rect(region["limit"])
            for raw in region["erase"]:
                pieces = [fitz.Rect(raw) & limit]
                for item in plan.get("protected", []):
                    obstacle = fitz.Rect(item) + (-0.75, -0.75, 0.75, 0.75)
                    pieces = [p for r in pieces for p in subtract(r, obstacle)]
                for rect in pieces:
                    if not rect.is_empty:
                        page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)
            if writer is not None:
                writer.write_text(page)
            elif markup is not None:
                spare, actual_scale = page.insert_htmlbox(
                    box, markup, css=css, scale_low=0.3
                )
                if spare < 0 or abs(actual_scale - rich_scale) > 0.001:
                    raise RuntimeError("OCR 段落排版测量与写入不一致")
                size *= rich_scale
            elif text.strip():
                if (
                    page.insert_textbox(
                        box, text, fontname="ScanText", fontsize=size, lineheight=1.12
                    )
                    < 0
                ):
                    raise RuntimeError("OCR 排版测量与写入不一致")
            painted.append(
                {
                    "source": region["text"],
                    "translation": text,
                    "box": list(box),
                    "scale": size / region["size"],
                }
            )
    return painted


def translate_scan(settings, file, send, cancel):
    from pdf2zh_next.translator.utils import get_translator
    from custom_pdf2zh.ocr_layout import has_plus_minus_bar

    plans = json.loads(Path(file).with_suffix(".ocr.json").read_text(encoding="utf-8"))
    source = Path(plans["_source"])
    output = Path(settings.translation.output)
    output.mkdir(parents=True, exist_ok=True)
    translator = get_translator(settings)
    from custom_pdf2zh.scan_narrative import ScanTerminology

    translator = ScanTerminology(
        translator, plans.get("_domain"), settings.translation.lang_out
    )
    pages = (
        {int(x) for x in settings.pdf.pages.split(",")} if settings.pdf.pages else None
    )
    entries = [
        (int(k), p)
        for k, p in plans.items()
        if k.isdigit() and (pages is None or int(k) + 1 in pages)
    ]

    def check_cancel():
        if cancel.is_set():
            raise RuntimeError("任务已取消")

    total = sum(len(plan["regions"]) for _, plan in entries)
    completed = 0
    audit = []
    with (
        fitz.open(source) as original,
        fitz.open(source) as doc,
        fitz.open(file) as recognized,
    ):
        for index, plan in entries:
            translations = []
            contexts = {}
            for region in plan["regions"]:
                if cancel.is_set():
                    raise RuntimeError("任务已取消")
                if not region["table"] and "+" in region["text"]:
                    bounds = fitz.Rect(region["box"])
                    operators = [
                        w
                        for w in recognized[index].get_text("words")
                        if w[4] == "+"
                        and (fitz.Rect(w[:4]).tl + fitz.Rect(w[:4]).br) / 2 in bounds
                    ]
                    if len(operators) == region["text"].count("+"):
                        corrections = iter(
                            (
                                "±"
                                if has_plus_minus_bar(
                                    original[index].get_pixmap(
                                        clip=fitz.Rect(w[:4]) + (-1, -1, 1, 1),
                                        matrix=fitz.Matrix(4, 4),
                                        alpha=False,
                                    )
                                )
                                else "+"
                            )
                            for w in operators
                        )
                        region["text"] = re.sub(
                            r"\+", lambda _: next(corrections), region["text"]
                        )
                if region.get("paragraphs"):
                    from custom_pdf2zh.scan_narrative import translate_paragraph
                    from custom_pdf2zh.scan_math import rich_text

                    rendered, readable = [], []
                    for paragraph in region["paragraphs"]:
                        label = paragraph["label"]
                        try:
                            translated = translate_paragraph(
                                translator,
                                paragraph["text"],
                                settings.translation.lang_out,
                                cancel_check=check_cancel,
                            )
                        except RuntimeError as exc:
                            raise RuntimeError(
                                f"第{index+1}页脚注{label}：{exc}"
                            ) from exc
                        if (
                            label == "NOTE"
                            and settings.translation.lang_out.startswith("zh")
                        ):
                            label = "注"
                        rendered.append(
                            f'<p style="margin:0 0 .35em 0">{rich_text(label)}&nbsp; {rich_text(translated)}</p>'
                        )
                        readable.append(label + " " + translated)
                    region["_flow_markup"] = "".join(rendered)
                    text = "\n".join(readable)
                elif region.get("flow_parts"):
                    from custom_pdf2zh.scan_math import rich_text

                    markup, readable = [], []
                    for part in region["flow_parts"]:
                        check_cancel()
                        if part.get("paragraph"):
                            markup.append("<br>")
                        if part["image"]:
                            rect = fitz.Rect(part["box"])
                            pix = original[index].get_pixmap(
                                clip=rect, matrix=fitz.Matrix(2, 2), alpha=False
                            )
                            data = base64.b64encode(pix.tobytes("png")).decode("ascii")
                            markup.append(
                                f'<img style="vertical-align:middle" width="{rect.width}" height="{rect.height}" src="data:image/png;base64,{data}">'
                            )
                            readable.append(part["text"])
                        else:
                            translated = translate_context(
                                translator, part["text"], settings.translation.lang_out
                            )
                            check_cancel()
                            if not translated.strip():
                                raise RuntimeError("表格脚注翻译为空")
                            markup.append(rich_text(translated))
                            readable.append(translated)
                    region["_flow_markup"] = " ".join(markup)
                    text = " ".join(readable)
                elif region.get("context_id"):
                    key = (
                        region["context_id"],
                        region["context_text"],
                        region["markers"],
                    )
                    if key not in contexts:
                        contexts[key] = translate_cell(
                            translator,
                            region["context_text"],
                            region["markers"],
                            settings.translation.lang_out,
                        )
                    text = contexts[key][region["context_segment"]].strip()
                    region["context_validated"] = True
                else:
                    text = translate_context(
                        translator, region["text"], settings.translation.lang_out
                    )
                if (not text or not text.strip()) and not region.get(
                    "context_validated"
                ):
                    raise RuntimeError("扫描段落翻译为空")
                translations.append(text)
                completed += 1
                send(
                    {
                        "type": "progress_update",
                        "stage": "扫描页段落翻译",
                        "stage_current": completed,
                        "stage_total": total,
                        "overall_progress": 20 + completed / max(total, 1) * 70,
                    }
                )
            if cancel.is_set():
                raise RuntimeError("任务已取消")
            audit.append(
                {
                    "page": index + 1,
                    "regions": compose_page(doc[index], plan, translations),
                    "protected": plan["protected"],
                }
            )
        mono = output / f"{source.stem}.{settings.translation.lang_out}.mono.pdf"
        doc.subset_fonts()
        doc.save(mono, garbage=4, deflate=True)
        dual = None
        if not settings.pdf.no_dual:
            dual = output / f"{source.stem}.{settings.translation.lang_out}.dual.pdf"
            with fitz.open() as bilingual:
                for i in range(len(doc)):
                    if settings.pdf.use_alternating_pages_dual:
                        bilingual.insert_pdf(original, from_page=i, to_page=i)
                        bilingual.insert_pdf(doc, from_page=i, to_page=i)
                    else:
                        a, b = original[i], doc[i]
                        target = bilingual.new_page(
                            width=a.rect.width + b.rect.width,
                            height=max(a.rect.height, b.rect.height),
                        )
                        target.show_pdf_page(
                            fitz.Rect(0, 0, a.rect.width, a.rect.height), original, i
                        )
                        target.show_pdf_page(
                            fitz.Rect(
                                a.rect.width,
                                0,
                                a.rect.width + b.rect.width,
                                b.rect.height,
                            ),
                            doc,
                            i,
                        )
                bilingual.save(dual, garbage=4, deflate=True)
    (output / "scan-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    usage = {
        "main": {
            name: getattr(translator, attr).value
            for name, attr in [
                ("total", "token_count"),
                ("prompt", "prompt_token_count"),
                ("completion", "completion_token_count"),
            ]
            if hasattr(translator, attr)
        }
    }
    send(
        {
            "type": "finish",
            "translate_result": SimpleNamespace(mono_pdf_path=mono, dual_pdf_path=dual),
            "token_usage": usage,
        }
    )


def scan_worker(settings, file, progress, cancel_pipe, logger_queue):
    from pdf2zh_next.high_level import SubprocessError

    cancel = threading.Event()

    def listen():
        try:
            cancel_pipe.recv()
            cancel.set()
        except (EOFError, OSError):
            pass

    threading.Thread(target=listen, daemon=True).start()
    try:
        translate_scan(settings, file, progress.send, cancel)
    except Exception as exc:
        progress.send(SubprocessError(message=f"扫描页翻译失败：{exc}"))
    finally:
        try:
            progress.send(None)
        except (OSError, EOFError):
            pass
        finally:
            progress.close()
        logger_queue.put(None)
