"""Image-only PDF OCR preflight, isolated from the native-text translation path.

The worker adds invisible text without replacing source images. It runs in a
separate process so cancelling a workbench job also stops OCR and downloads.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
import urllib.request

import pymupdf

LANGUAGES = {
    "en": "eng",
    "zh": "chi_sim",
    "zh-cn": "chi_sim",
    "zh-tw": "chi_tra",
    "ja": "jpn",
    "ko": "kor",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "ru": "rus",
}
ENGLISH_SHA256 = "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba"


def image_only_pages(path: Path) -> list[int]:
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise RuntimeError("PDF 已加密，请先解密后重试")
        pages = []
        for i, page in enumerate(doc):
            if page.get_text().strip() or not page.get_image_info():
                continue
            # Uniform bitmap separators do not need OCR or a language download.
            preview = page.get_pixmap(
                matrix=pymupdf.Matrix(0.2, 0.2), alpha=False, annots=False
            )
            if preview.color_count(colors=False) > 1:
                pages.append(i)
        return pages


def tessdata(language: str, emit) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", language):
        raise ValueError("OCR 语言无效，请使用 eng 或 eng+deu 等 Tesseract 语言代码")
    override = os.environ.get("TESSDATA_PREFIX")
    cache = (
        Path(override)
        if override
        else Path.home() / ".cache/pdf2zh/tessdata_best/4.1.0"
    )
    cache.mkdir(parents=True, exist_ok=True)
    for code in dict.fromkeys(language.split("+")):
        target = cache / f"{code}.traineddata"
        if target.is_file():
            if (
                override
                or code != "eng"
                or hashlib.sha256(target.read_bytes()).hexdigest() == ENGLISH_SHA256
            ):
                continue
        if override:
            raise RuntimeError(f"TESSDATA_PREFIX 中缺少 OCR 语言数据：{code}")
        emit({"stage": f"下载 OCR 语言数据（{code}，首次使用）"})
        partial = target.with_suffix(f".{os.getpid()}.part")
        try:
            url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/4.1.0/{code}.traineddata"
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                partial.open("wb") as out,
            ):
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            if not partial.stat().st_size:
                raise RuntimeError("下载的 OCR 数据为空")
            if (
                code == "eng"
                and hashlib.sha256(partial.read_bytes()).hexdigest() != ENGLISH_SHA256
            ):
                raise RuntimeError("OCR 英文语言数据校验失败")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return cache


def add_text_layer(source: Path, destination: Path, language: str, emit) -> dict:
    from custom_pdf2zh.ocr_layout import text_plan

    candidates = image_only_pages(source)
    if not candidates:
        return {"path": str(source), "pages": [], "empty_pages": []}
    data = tessdata(language, emit)
    recognized_pages, empty_pages = [], []
    font = pymupdf.Font("cjk")
    layouts = {"_source": str(source)}

    def recognize_cell(page, cell, index):
        from PIL import Image, ImageOps

        rect = pymupdf.Rect(cell)
        pix = page.get_pixmap(dpi=240, clip=rect, alpha=False, annots=False)
        original = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if rect.height < 85:
            # AUTO page segmentation can drop a single bold header entirely.
            # Repetition provides a text-block context; only first-copy boxes
            # are retained, so no duplicated text reaches translation.
            image = Image.new(
                "RGB", (original.width + 50, (original.height + 25) * 3 + 25), "white"
            )
            for repeat in range(3):
                image.paste(original, (25, 25 + repeat * (original.height + 25)))
        else:
            image = ImageOps.expand(original, border=25, fill="white")
        raster = pymupdf.Pixmap(
            pymupdf.csRGB, image.width, image.height, image.tobytes(), False
        )
        raster.set_dpi(240, 240)
        with pymupdf.open(
            stream=raster.pdfocr_tobytes(language=language, tessdata=str(data))
        ) as recognized:
            return [
                (
                    w[0] + rect.x0 - 7.5,
                    w[1] + rect.y0 - 7.5,
                    w[2] + rect.x0 - 7.5,
                    w[3] + rect.y0 - 7.5,
                    w[4],
                    10000 + index,
                    w[6],
                    w[7],
                )
                for w in recognized[0].get_text("words")
                if (w[1] + w[3]) / 2 < (original.height + 25) * 72 / 240
            ]

    destination.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as doc:
        for index in candidates:
            page = doc[index]
            emit(
                {
                    "stage": f"OCR 识别扫描页（{index + 1}/{len(doc)}）",
                    "page": index + 1,
                }
            )
            # The OCR raster is only an analysis input. Its pixels never replace
            # the original scan (nor its links, annotations, or page geometry).
            rotation = page.rotation
            try:
                page.set_rotation(0)
                pix = page.get_pixmap(dpi=300, alpha=False, annots=False)
            finally:
                page.set_rotation(rotation)
            with pymupdf.open(
                stream=pix.pdfocr_tobytes(language=language, tessdata=str(data))
            ) as ocr:
                del pix
                if not ocr[0].get_text().strip():
                    empty_pages.append(index + 1)
                    continue
                # Tesseract embeds GlyphLessFont: its characters have no visible
                # outlines. BabelDOC may redraw those characters as formulas,
                # producing black boxes. Use a real embedded font for the hidden
                # layer, while keeping the OCR word rectangles and source pixels.
                page.insert_font(fontname="OCRSource", fontbuffer=font.buffer)
                bounds = page.rect * page.derotation_matrix
                scale = pymupdf.Matrix(
                    bounds.width / ocr[0].rect.width,
                    bounds.height / ocr[0].rect.height,
                )
                words = [
                    (*list(pymupdf.Rect(w[:4]) * scale), *w[4:])
                    for w in ocr[0].get_text("words")
                ]
                words, layouts[str(index)] = text_plan(page, words, recognize_cell)
                for word in words:
                    box = pymupdf.Rect(word[:4])
                    text = word[4]
                    size = box.height / (font.ascender - font.descender)
                    width = font.text_length(text, fontsize=size)
                    if size <= 0 or width <= 0:
                        continue
                    origin = pymupdf.Point(box.x0, box.y0 + size * font.ascender)
                    page.insert_text(
                        origin,
                        text,
                        fontname="OCRSource",
                        fontsize=size,
                        render_mode=3,
                        morph=(origin, pymupdf.Matrix(box.width / width, 1)),
                    )
            recognized_pages.append(index + 1)
        if not recognized_pages:
            if any(page.get_text().strip() for page in doc):
                # A native document can contain blank raster separator pages.
                # Keep its existing translation path when there is no OCR text.
                return {
                    "path": str(source),
                    "pages": [],
                    "empty_pages": empty_pages,
                }
            raise RuntimeError(
                "扫描页 OCR 未识别出文字，已停止翻译，请检查扫描清晰度和 OCR 语言"
            )
        doc.save(destination, garbage=3, deflate=True)
    destination.with_suffix(".ocr.json").write_text(
        json.dumps(layouts), encoding="utf-8"
    )
    return {
        "path": str(destination),
        "pages": recognized_pages,
        "empty_pages": empty_pages,
    }


async def prepare_pdf(
    source: Path, destination: Path, lang_in: str, on_progress, ocr_settings=None
) -> dict:
    # No OCR imports, downloads, rewrite, or changed settings for native PDFs.
    candidates = await asyncio.to_thread(image_only_pages, source)
    if not candidates:
        return {"path": str(source), "pages": [], "empty_pages": []}
    language = os.environ.get("PDF2ZH_OCR_LANGUAGE") or LANGUAGES.get(
        lang_in.lower(), lang_in or "eng"
    )
    options = ocr_settings or {"engine": "Tesseract"}
    if options.get("engine") not in {"Tesseract", "PP-OCRv6", "paddle-hybrid"}:
        raise ValueError("不支持的 OCR 引擎")
    worker_env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent)
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
        "PDF2ZH_OCR_ENGINE": options["engine"],
    }
    if options.get("token"):
        worker_env["PDF2ZH_PADDLEOCR_TOKEN"] = options["token"]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "custom_pdf2zh.workbench_ocr",
        str(source),
        str(destination),
        language,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=worker_env,
    )

    async def drain_errors():
        tail = b""
        while chunk := await proc.stderr.read(4096):
            tail = (tail + chunk)[-8192:]
        return tail.decode("utf-8", errors="replace")

    errors = asyncio.create_task(drain_errors())
    result = None
    worker_error = None
    try:
        while line := await proc.stdout.readline():
            event = json.loads(line)
            if "result" in event:
                result = event["result"]
            elif "error" in event:
                worker_error = event["error"]
            else:
                on_progress(event)
        await proc.wait()
        detail = await errors
        if proc.returncode or result is None:
            raise RuntimeError(
                f"扫描件 OCR 失败：{worker_error or detail[-1000:] or '未返回识别结果'}"
            )
        return result
    finally:
        if proc.returncode is None:
            if os.name == "nt":
                # Windows venv launchers can own a second Python process.
                # Kill the owned tree so OCR cannot outlive task cancellation.
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
        await errors


async def translation_events(
    settings, source: Path, prepared: Path, ocr_pages: list[int], translate
):
    """Keep OCR rendering flags off native pages, including mixed documents."""
    if not ocr_pages:
        async for event in translate(settings, source):
            yield event
        return
    with pymupdf.open(source) as doc:
        count = len(doc)
        native = [i + 1 for i, page in enumerate(doc) if page.get_text().strip()]
    if not native:
        settings.pdf.ocr_workaround = True
        async for event in scan_engine_events(settings, prepared, translate):
            yield event
        return

    # OCR workaround changes colors and disables rich text globally in BabelDOC.
    # Two page selections keep that global setting away from native pages. Each
    # pass retains all pages, so page indices and document geometry stay stable.
    output = Path(settings.translation.output)
    parts = prepared.parent / "translation-parts"
    results = {}
    usage = {}
    groups = [("native", native, source, False), ("ocr", ocr_pages, prepared, True)]
    for part_index, (name, pages, input_path, needs_ocr) in enumerate(groups):
        config = settings.model_copy(deep=True)
        config.pdf.ocr_workaround = needs_ocr
        config.pdf.pages = ",".join(map(str, pages))
        config.translation.output = str(parts / name)
        async for event in scan_engine_events(config, input_path, translate):
            if event.get("type") == "finish":
                results[name] = event.get("translate_result")
                for category, counters in (event.get("token_usage") or {}).items():
                    target = usage.setdefault(category, {})
                    for key, value in counters.items():
                        target[key] = target.get(key, 0) + value
            else:
                event = dict(event)
                progress = event.get("overall_progress")
                if isinstance(progress, (int, float)):
                    event["overall_progress"] = (part_index * 100 + progress) / 2
                yield event
                if event.get("type") == "error":
                    return
    if any(results.get(name) is None for name, *_ in groups):
        raise RuntimeError("混合 PDF 翻译未返回完整产物")

    def merge_outputs():
        merged = {}
        selected = set(ocr_pages)
        output.mkdir(parents=True, exist_ok=True)
        for kind in ("mono", "dual"):
            attr = f"{kind}_pdf_path"
            paths = [getattr(results[name], attr, None) for name in ("native", "ocr")]
            if not any(paths):
                continue
            if not all(paths):
                raise RuntimeError(f"混合 PDF 缺少 {kind} 产物")
            with (
                pymupdf.open(paths[0]) as a,
                pymupdf.open(paths[1]) as b,
                pymupdf.open() as target,
            ):
                stride = (
                    2
                    if kind == "dual" and settings.pdf.use_alternating_pages_dual
                    else 1
                )
                if len(a) != count * stride or len(b) != count * stride:
                    raise RuntimeError("混合 PDF 产物页数不符，已停止导出")
                for page in range(count):
                    origin = b if page + 1 in selected else a
                    target.insert_pdf(
                        origin, from_page=page * stride, to_page=(page + 1) * stride - 1
                    )
                target.set_metadata(a.metadata)
                target.set_toc(a.get_toc())
                path = (
                    output / f"{source.stem}.{settings.translation.lang_out}.{kind}.pdf"
                )
                target.save(path, garbage=3, deflate=True)
                merged[attr] = path
        return SimpleNamespace(**merged)

    result = await asyncio.to_thread(merge_outputs)
    if parts.resolve().parent != prepared.resolve().parent:
        raise RuntimeError("OCR 临时目录越界")
    await asyncio.to_thread(shutil.rmtree, parts)
    yield {"type": "finish", "translate_result": result, "token_usage": usage}


_native_translate_wrapper = None
_scan_wrapper_users = 0
_scan_wrapper_lock = threading.Lock()


def scan_translate_wrapper(settings, file, *args):
    # A native job that starts while another caller installs the scan target
    # must still execute the original kernel (Windows spawn / POSIX fork).
    if (
        not settings.pdf.ocr_workaround
        or not Path(file).with_suffix(".ocr.json").exists()
    ):
        from pdf2zh_next.high_level import _translate_wrapper

        native = _native_translate_wrapper or _translate_wrapper
        if native is scan_translate_wrapper:
            raise RuntimeError("原生 PDF 翻译入口不可用")
        return native(settings, file, *args)
    from custom_pdf2zh.scan_render import scan_worker

    return scan_worker(settings, file, *args)


async def scan_engine_events(settings, file, translate):
    global _native_translate_wrapper, _scan_wrapper_users
    if (
        not settings.pdf.ocr_workaround
        or not Path(file).with_suffix(".ocr.json").exists()
    ):
        async for event in translate(settings, file):
            yield event
        return
    from pdf2zh_next import high_level

    with _scan_wrapper_lock:
        if _scan_wrapper_users == 0:
            _native_translate_wrapper = high_level._translate_wrapper
            high_level._translate_wrapper = scan_translate_wrapper
        _scan_wrapper_users += 1
    try:
        async for event in translate(settings, file):
            yield event
    finally:
        with _scan_wrapper_lock:
            _scan_wrapper_users -= 1
            if (
                _scan_wrapper_users == 0
                and high_level._translate_wrapper is scan_translate_wrapper
            ):
                high_level._translate_wrapper = _native_translate_wrapper


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        emit(
            {
                "result": (
                    __import__(
                        "custom_pdf2zh.paddle_ocr", fromlist=["add_text_layer"]
                    ).add_text_layer(Path(sys.argv[1]), Path(sys.argv[2]), emit)
                    if os.environ.get("PDF2ZH_OCR_ENGINE")
                    in {"PP-OCRv6", "paddle-hybrid"}
                    else add_text_layer(
                        Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], emit
                    )
                )
            }
        )
    except Exception as exc:
        # Request exception strings can contain signed result URLs. Never put
        # those download credentials into workbench logs.
        message = (
            "PaddleOCR 网络请求失败，请检查连接后重试"
            if type(exc).__module__.startswith("requests")
            else str(exc)
        )
        emit({"error": message})
        sys.exit(1)
