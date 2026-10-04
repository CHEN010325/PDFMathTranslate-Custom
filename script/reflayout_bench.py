"""参考文献页排版参数对照实验:用不同内核参数重排同一页,渲染 PNG 对比。

用法:.venv/Scripts/python.exe script/reflayout_bench.py
输出:C:/Users/chenshulin/AppData/Local/Temp/reflayout_<case>.png
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pymupdf  # noqa: E402

SRC = Path(
    r"C:\Users\chenshulin\Downloads\PDFMathTranslate-Custom"
    r"\pdf2zh\kernel\PDFMathTranslate-next.git"
    r"\pdf2zh_files\_uploads\WeMM-Embedding WeChat Multi-Modal Embedding.pdf"
)
PAGE_IDX = int(__import__("os").environ.get("BENCH_PAGE", "16"))  # 0-based
OUT = Path(r"C:\Users\chenshulin\AppData\Local\Temp")

CASES = {
    "base": {},
    "split": {"split_short_lines": True},
}


def page_pdf() -> bytes:
    doc = pymupdf.open(SRC)
    doc.select([PAGE_IDX])
    data = doc.tobytes()
    doc.close()
    return data


async def run_case(case: str, overrides: dict) -> None:
    from pdf2zh_next.config.model import SettingsModel
    from pdf2zh_next.config.translate_engine_model import TRANSLATION_ENGINE_METADATA_MAP
    from pdf2zh_next.high_level import do_translate_async_stream

    workdir = OUT / f"reflayout_{case}"
    workdir.mkdir(exist_ok=True)
    pdf_path = workdir / "ref_page.pdf"
    pdf_path.write_bytes(page_pdf())

    ollama = TRANSLATION_ENGINE_METADATA_MAP["Ollama"].setting_model_type(
        ollama_model="s2021008840/hy-mt2:7b-q4_k_m",
        ollama_host="http://localhost:11434",
    )
    sm = SettingsModel(translate_engine_settings=ollama)
    sm.translation.lang_in = "en"
    sm.translation.lang_out = "zh"
    sm.translation.output = str(workdir)
    sm.translation.qps = 1  # 本地引擎固定串行
    sm.pdf.watermark_output_mode = "no_watermark"
    sm.translation.no_auto_extract_glossary = True
    for key, value in overrides.items():
        setattr(sm.pdf, key, value)

    mono = None
    async for ev in do_translate_async_stream(sm, pdf_path):
        if ev.get("type") == "finish":
            result = ev.get("translate_result")
            mono = result.mono_pdf_path if result else None
    if not mono or not Path(mono).exists():
        print(f"{case}: FAILED (no output)", flush=True)
        return
    doc = pymupdf.open(mono)
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6))
    png = OUT / f"reflayout_p{PAGE_IDX + 1}_{case}.png"
    pix.save(png)
    doc.close()
    print(f"{case}: OK -> {png}", flush=True)


async def main() -> None:
    for case, overrides in CASES.items():
        try:
            await run_case(case, overrides)
        except Exception as exc:
            print(f"{case}: ERROR {exc}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
