"""pdf2zh-next 原生 GUI 的自定义"翻译历史"页签。

由 venv 里 gui.py 的注入补丁(script/apply_all_patches.py)调用本模块的
build_history_tab(),在左侧边栏增加 📚 页签。

库根目录与 GUI 输出、gradio allowed_paths 保持一致(运行目录下 pdf2zh_files):
  pdf2zh_files/<session>/<name>.<lang>.dual.pdf   交替页双语(奇数页原文、偶数页译文)
  pdf2zh_files/<session>/<name>.<lang>.mono.pdf   纯译文
  pdf2zh_files/_imported/                          手动导入的历史文件
  pdf2zh_files/_sidecache/                         对照/提取视图的生成缓存

BabelDOC 的双语 PDF 是交替页格式:把奇偶页成对重拼成双倍宽页面,
即得到"每页左原文右译文"的对照视图,左右天然按页对齐。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import gradio as gr
import pymupdf
from gradio_pdf import PDF

LIBRARY_ROOT = Path("pdf2zh_files").resolve()
CACHE_DIR = LIBRARY_ROOT / "_sidecache"

MODE_SBS = "双面对照(左原文右译文)"
MODE_DUAL = "双语原版(交替页)"
MODE_MONO = "纯译文"


def _classify(pdf: Path) -> str:
    stem = pdf.stem.lower()
    if "dual" in stem or "双语对照" in pdf.stem:
        return "双语对照"
    if "mono" in stem:
        return "纯译文"
    return "其他"


def scan_library() -> list[tuple[str, str]]:
    """[(下拉框标签, 文件路径)],按修改时间倒序。"""
    if not LIBRARY_ROOT.exists():
        return []
    items: list[tuple[float, Path]] = []
    for pdf in LIBRARY_ROOT.rglob("*.pdf"):
        if CACHE_DIR in pdf.parents:
            continue  # 生成缓存不算历史
        try:
            items.append((pdf.stat().st_mtime, pdf))
        except OSError:
            continue
    items.sort(reverse=True)
    return [
        (
            f"{datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')} ｜ {pdf.stem} ｜ {_classify(pdf)}",
            str(pdf),
        )
        for mtime, pdf in items
    ]


def _zh_chars(page: pymupdf.Page) -> int:
    return sum(1 for ch in page.get_text() if "\u4e00" <= ch <= "\u9fff")


def _alternating_order(doc: pymupdf.Document) -> tuple[int, int] | None:
    """返回 (原文页偏移, 译文页偏移):奇数页原文 → (0, 1);译文在前 → (1, 0)。

    判定依据:交替页双语的第一页是纯原文(目标语言字符为 0),
    第二页含大量目标语言字符。无法判定(非交替格式)返回 None。
    """
    if doc.page_count < 2:
        return None
    first, second = _zh_chars(doc[0]), _zh_chars(doc[1])
    if first == 0 and second > 20:
        return (0, 1)
    if second == 0 and first > 20:
        return (1, 0)
    return None


def _cache_path(src: Path, suffix: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{src.stem}{suffix}.pdf"


def _is_fresh(cache: Path, src: Path) -> bool:
    return cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime


def make_side_by_side(dual: Path | str) -> Path:
    """奇偶页成对重拼为"每页左原文右译文"。非交替格式时原样返回。"""
    dual = Path(dual)
    doc = pymupdf.open(dual)
    order = _alternating_order(doc)
    if order is None:
        return dual
    out = _cache_path(dual, ".左原文右译文")
    if _is_fresh(out, dual):
        return out
    off_orig, off_tr = order
    merged = pymupdf.open()
    for i in range(0, doc.page_count, 2):
        left = doc.load_page(i + off_orig)
        right_idx = i + off_tr
        right = doc.load_page(right_idx) if right_idx < doc.page_count else None
        width = left.rect.width + (right.rect.width if right else 0)
        height = max(left.rect.height, right.rect.height if right else 0)
        page = merged.new_page(width=width, height=height)
        page.show_pdf_page(
            pymupdf.Rect(0, 0, left.rect.width, left.rect.height), doc, i + off_orig
        )
        if right is not None:
            page.show_pdf_page(
                pymupdf.Rect(
                    left.rect.width, 0, left.rect.width + right.rect.width, right.rect.height
                ),
                doc,
                right_idx,
            )
    merged.save(out, garbage=3, deflate=True)
    return out


def extract_view(pdf: Path | str, which: str) -> Path:
    """从交替页双语 PDF 中只取译文页(which="译文")或原文页。非交替格式原样返回。"""
    pdf = Path(pdf)
    doc = pymupdf.open(pdf)
    order = _alternating_order(doc)
    if order is None:
        return pdf
    off = order[1] if which == "译文" else order[0]
    out = _cache_path(pdf, f".仅{which}")
    if _is_fresh(out, pdf):
        return out
    picked = pymupdf.open(pdf)
    picked.select(list(range(off, picked.page_count, 2)))
    picked.save(out, garbage=3, deflate=True)
    return out


def _resolve(path_str: str | None, mode: str) -> tuple[str | None, str | None]:
    if not path_str:
        return None, None
    pdf = Path(path_str)
    if not pdf.exists():
        return None, None
    kind = _classify(pdf)
    if kind == "双语对照":
        if mode.startswith("双面对照"):
            shown = make_side_by_side(pdf)
        elif mode == MODE_MONO:
            shown = extract_view(pdf, "译文")
        else:
            shown = pdf
    else:
        shown = pdf  # 纯译文/其他文件没有可拆分的原文页
    return str(shown), str(shown)


def build_history_tab() -> dict:
    """在当前 gr.Blocks 上下文中构建 📚 翻译历史 页签并接好事件。"""

    gr.Markdown("## 📚 翻译历史", elem_classes=["tab-title"])
    with gr.Row():
        file_list = gr.Dropdown(
            label="选择文件(时间 ｜ 名称 ｜ 类型)",
            choices=scan_library(),
            interactive=True,
            scale=4,
        )
        refresh_btn = gr.Button("🔄 刷新", variant="secondary", scale=1)

    mode = gr.Radio(
        choices=[MODE_SBS, MODE_DUAL, MODE_MONO],
        value=MODE_SBS,
        label="查看模式",
    )
    viewer = PDF(label="预览")
    download = gr.File(label="下载当前视图")

    def _pick(path_str: str | None, mode_value: str):
        return _resolve(path_str, mode_value)

    file_list.change(_pick, [file_list, mode], [viewer, download])
    mode.change(_pick, [file_list, mode], [viewer, download])

    def _refresh():
        return gr.update(choices=scan_library())

    refresh_btn.click(_refresh, None, [file_list])

    return {"file_list": file_list, "viewer": viewer}
