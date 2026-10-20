"""Offline deployment smoke check. Never reads settings or submits OCR jobs."""

from __future__ import annotations

import asyncio
from importlib import import_module, metadata
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
KERNEL = ROOT / "pdf2zh/kernel/PDFMathTranslate-next.git"
# Match start_workbench.bat: it runs from the pinned source submodule while
# loading dependencies from its venv. Do not silently switch the native kernel.
sys.path[:0] = [str(KERNEL), str(ROOT)]


def check():
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Run this check with the workbench virtual environment")
    if not (3, 10) <= sys.version_info[:2] < (3, 14):
        raise RuntimeError("Python 3.10-3.13 is required")
    versions = {}
    for name, expected in {
        "pdf2zh-next": "2.9.0",
        "babeldoc": "0.6.4",
        "PyMuPDF": "1.28.2",
    }.items():
        versions[name] = metadata.version(name)
        if versions[name] != expected:
            raise RuntimeError(f"{name} must be {expected}; rerun setup")
    for name in (
        "bitstring",
        "requests",
        "numpy",
        "opencv-python-headless",
        "Pillow",
        "fastapi",
        "sse-starlette",
        "uvicorn",
    ):
        versions[name] = metadata.version(name)
    for name in (
        "cv2",
        "numpy",
        "requests",
        "PIL.Image",
        "uvicorn",
        "pdf2zh_next.high_level",
        "custom_pdf2zh.paddle_ocr",
        "custom_pdf2zh.scan_render",
        "custom_pdf2zh.webapp.server",
    ):
        import_module(name)

    import pymupdf as f
    from custom_pdf2zh import workbench_ocr
    from custom_pdf2zh.ocr_layout import text_plan
    from custom_pdf2zh.scan_render import compose_page

    if (
        Path(workbench_ocr.__file__).resolve()
        != ROOT / "custom_pdf2zh/workbench_ocr.py"
    ):
        raise RuntimeError("Workbench code is not loaded from this checkout")
    from pdf2zh_next.high_level import _translate_wrapper
    import pdf2zh_next

    if Path(pdf2zh_next.__file__).resolve() != KERNEL / "pdf2zh_next/__init__.py":
        raise RuntimeError("Precise kernel source does not match the launcher")

    if not callable(_translate_wrapper):
        raise RuntimeError("Precise kernel worker entry is unavailable")
    with tempfile.TemporaryDirectory(prefix="pdf2zh-deploy-check-") as tmp:
        source = Path(tmp) / "mixed.pdf"
        with f.open() as art, f.open() as doc:
            art.new_page().insert_text((72, 90), "Scan translation test", fontsize=18)
            page = doc.new_page()
            page.insert_text((72, 72), "Native text")
            scan = doc.new_page()
            scan.insert_image(scan.rect, stream=art[0].get_pixmap().tobytes("png"))
            doc.save(source)
            if workbench_ocr.image_only_pages(source) != [1]:
                raise RuntimeError("Image-only page detection failed")
            before = scan.get_pixmap().samples
            _, plan = text_plan(scan, art[0].get_text("words"))
            if not plan["regions"]:
                raise RuntimeError("Scan layout planning did not recover prose")
            compose_page(scan, plan, ["扫描翻译测试"] * len(plan["regions"]))
            if scan.get_pixmap().samples == before:
                raise RuntimeError("Scan composition did not write translated text")
            page = doc.new_page()
            if (
                page.insert_htmlbox(
                    f.Rect(30, 30, 400, 200), "公式 x<sub>1</sub> + y<sup>2</sup>"
                )[0]
                < 0
            ):
                raise RuntimeError("Rich text rendering is unavailable")
            source = Path(tmp) / "rendered.pdf"
            doc.save(source)
        if workbench_ocr.image_only_pages(source) != []:
            # The rendered scan now has Chinese text, and should not be OCR'd again.
            raise RuntimeError("Existing text pages should bypass OCR")
        original = source.read_bytes()
        result = asyncio.run(
            workbench_ocr.prepare_pdf(
                source, Path(tmp) / "unused.pdf", "en", lambda _: None
            )
        )
        if result["pages"] or source.read_bytes() != original:
            raise RuntimeError("Native/text PDF bypass changed the source")
    return {
        "ok": True,
        "python": sys.version.split()[0],
        "versions": versions,
        "source_kernel_version": pdf2zh_next.__version__,
        "scan_compositor": True,
        "native_bypass": True,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(check(), ensure_ascii=True))
    except Exception as exc:
        # No configuration or credential values are included in this check.
        print(
            f"Workbench check failed ({type(exc).__name__}); rerun setup. Details: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
