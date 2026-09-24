"""pdf2zh-next 引擎封装:任务登记表、流式翻译、进度广播、页面渲染、历史扫描。

由 webapp/server.py(FastAPI)调用;翻译跑在 asyncio 任务里,
重活由 do_translate_async_stream 内部的 multiprocessing 子进程承担。

历史模型(对齐参考项目 mineru-pdf-translate 的 tasks.json):
  _jobs.json 登记每次翻译(以输入文件为键,重复翻译原位更新),
  扫描时与磁盘上的 mono/dual 产物合并成一张卡片,不再出现同文档多卡。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

import pymupdf

LIBRARY_ROOT = Path("pdf2zh_files").resolve()
UPLOAD_DIR = LIBRARY_ROOT / "_uploads"
CACHE_DIR = LIBRARY_ROOT / "_sidecache"
PAGE_CACHE_DIR = CACHE_DIR / "pages"
SETTINGS_FILE = LIBRARY_ROOT / "_webapp_settings.json"
JOBS_FILE = LIBRARY_ROOT / "_jobs.json"

DEFAULT_SETTINGS: dict = {
    "engine": "ollama",
    "ollama_model": "s2021008840/hy-mt2:7b-q8_0",
    "ollama_host": "http://localhost:11434",
    "lang_in": "en",
    "lang_out": "zh",
}

# 对齐 tencent/Hy-MT2 官方支持的语言表(33 语种 + 繁体/粤语等变体,共 38 条)
LANGS: dict[str, str] = {
    "zh": "中文(简体)",
    "en": "英语",
    "fr": "法语",
    "pt": "葡萄牙语",
    "es": "西班牙语",
    "ja": "日语",
    "tr": "土耳其语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "ko": "韩语",
    "th": "泰语",
    "it": "意大利语",
    "de": "德语",
    "vi": "越南语",
    "ms": "马来语",
    "id": "印尼语",
    "fil": "菲律宾语",
    "hi": "印地语",
    "zh-TW": "中文(繁体)",
    "pl": "波兰语",
    "cs": "捷克语",
    "nl": "荷兰语",
    "km": "高棉语",
    "my": "缅甸语",
    "fa": "波斯语",
    "gu": "古吉拉特语",
    "ur": "乌尔都语",
    "te": "泰卢固语",
    "mr": "马拉地语",
    "he": "希伯来语",
    "bn": "孟加拉语",
    "ta": "泰米尔语",
    "uk": "乌克兰语",
    "bo": "藏语",
    "kk": "哈萨克语",
    "mn": "蒙古语",
    "ug": "维吾尔语",
    "yue": "粤语",
}

# BabelDOC 阶段名 → 中文(前缀匹配)
STAGE_ZH: dict[str, str] = {
    "Download assets": "下载排版资源",
    "Loading fonts": "加载字体",
    "Parse layout": "解析版面",
    "Parse Page": "解析页面",
    "Parse Formulas and Styles": "解析公式与样式",
    "Translate Paragraphs": "翻译段落",
    "Typesetting": "排版合成",
    "Subset font": "字体子集化",
    "Save PDF": "保存 PDF",
    "Warmup": "引擎预热",
}

# ---------------------------------------------------------------------------
# SSE 广播
# ---------------------------------------------------------------------------


class TaskBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            q.put_nowait(event)


BUS = TaskBus()

TASKS: dict[str, dict] = {}
RUNNING: dict[str, asyncio.Task] = {}


def public_task(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "queue"}


# ---------------------------------------------------------------------------
# 设置持久化
# ---------------------------------------------------------------------------


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save_settings(settings: dict) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in settings:
            merged[key] = settings[key]
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return merged


# ---------------------------------------------------------------------------
# 任务登记表(以输入文件为键)
# ---------------------------------------------------------------------------

JOBS: list[dict] = []
_jobs_loaded = False


def _norm_base(name: str) -> tuple[str, str]:
    """文件名 → (基础名, 种类 dual/mono/plain)。"""
    lower = name.lower()
    if lower.endswith(".dual.pdf"):
        return name[: -len(".dual.pdf")], "dual"
    if lower.endswith(".mono.pdf"):
        return name[: -len(".mono.pdf")], "mono"
    return (name[: -4] if lower.endswith(".pdf") else name), "plain"


def load_jobs() -> list[dict]:
    global JOBS, _jobs_loaded
    if _jobs_loaded:
        return JOBS
    _jobs_loaded = True
    if JOBS_FILE.exists():
        try:
            JOBS = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            JOBS = []
    # 上次运行中途退出留下的僵尸 running 状态 → 按产物情况复位
    for j in JOBS:
        if j.get("status") == "running":
            j["status"] = "done" if (j.get("mono") or j.get("dual")) else "pending"
    _rebuild_orphan_jobs()
    return JOBS


def save_jobs() -> None:
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(
        json.dumps(JOBS, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _rebuild_orphan_jobs() -> None:
    """把磁盘上已存在但未登记的翻译产物(旧 gui 会话目录等)纳入登记表。"""
    if not LIBRARY_ROOT.exists():
        return
    known = {j.get("mono") for j in JOBS} | {j.get("dual") for j in JOBS}
    groups: dict[tuple, dict] = {}
    for pdf in LIBRARY_ROOT.rglob("*.pdf"):
        rel = pdf.relative_to(LIBRARY_ROOT)
        if rel.parts[0] in ("_uploads", "_imported", "_sidecache"):
            continue
        r = str(rel)
        if r in known:
            continue
        base, kind = _norm_base(pdf.name)
        g = groups.setdefault((str(rel.parent), base), {})
        if kind == "dual":
            g["dual"] = r
        elif kind == "mono":
            g["mono"] = r
    changed = False
    for (dir_, base), g in groups.items():
        if not (g.get("mono") or g.get("dual")):
            continue
        mtime = max(
            (LIBRARY_ROOT / v).stat().st_mtime for v in g.values() if v
        )
        name = re.sub(r"\.zh$", "", base, flags=re.IGNORECASE)
        # 尝试关联 _uploads 里的同名原件(登记表启用前完成的翻译)
        input_rel = None
        up = UPLOAD_DIR / f"{name}.pdf"
        if up.exists():
            input_rel = to_rel(up)
        JOBS.append(
            {
                "id": hashlib.sha1(f"{dir_}|{base}".encode()).hexdigest()[:12],
                "name": name,
                "input": input_rel,
                "status": "done",
                "time": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                "mono": g.get("mono"),
                "dual": g.get("dual"),
                "model": None,
            }
        )
        changed = True
    if changed:
        save_jobs()


def find_job_by_input(input_rel: str) -> dict | None:
    input_rel = input_rel.replace("\\", "/").lower()
    for j in JOBS:
        if (j.get("input") or "").replace("\\", "/").lower() == input_rel:
            return j
    return None


def upsert_job_for_input(input_rel: str, name: str) -> dict:
    job = find_job_by_input(input_rel)
    if job is None:
        job = {
            "id": hashlib.sha1(f"in|{input_rel}".encode()).hexdigest()[:12],
            "name": name,
            "input": input_rel,
            "status": "running",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "mono": None,
            "dual": None,
            "model": None,
        }
        JOBS.append(job)
    job["status"] = "running"
    save_jobs()
    return job


def complete_job(job_id: str, mono: str | None, dual: str | None, model: str | None, status: str) -> None:
    for j in JOBS:
        if j["id"] == job_id:
            if mono:
                j["mono"] = mono
            if dual:
                j["dual"] = dual
            j["model"] = model
            j["status"] = status
            j["time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            break
    save_jobs()


# ---------------------------------------------------------------------------
# 历史库扫描:登记表优先,散文件(导入/无主产物)兜底
# ---------------------------------------------------------------------------


def scan_library() -> list[dict]:
    load_jobs()
    items: list[dict] = []
    used: set[Path] = set()

    for j in sorted(JOBS, key=lambda x: x.get("time", ""), reverse=True):
        paths = []
        for k in ("input", "mono", "dual"):
            if j.get(k):
                p = (LIBRARY_ROOT / j[k])
                paths.append(p)
        used.update(p.resolve() for p in paths if p.exists())
        items.append(
            {
                "id": j["id"],
                "name": j["name"],
                "time": j.get("time", ""),
                "kind": "双语对照" if j.get("dual") else "纯译文",
                "mono": j.get("mono"),
                "dual": j.get("dual"),
                "original": j.get("input"),
                "translated": bool(j.get("mono") or j.get("dual")),
                "model": j.get("model"),
                "status": j.get("status", "done"),
            }
        )

    # 未登记的散文件(_imported 手工导入、无主产物等)
    groups: dict[tuple, dict] = {}
    for pdf in LIBRARY_ROOT.rglob("*.pdf"):
        ap = pdf.resolve()
        if ap in used:
            continue
        rel = pdf.relative_to(LIBRARY_ROOT)
        if rel.parts[0] == "_sidecache":
            continue
        base, kind = _norm_base(pdf.name)
        key = (str(rel.parent), base)
        g = groups.setdefault(
            key,
            {
                "name": base,
                "dir": str(rel.parent),
                "mtime": 0.0,
                "dual": None,
                "mono": None,
                "original": None,
            },
        )
        if kind == "dual":
            g["dual"] = str(rel)
        elif kind == "mono":
            g["mono"] = str(rel)
        elif rel.parts[0] in ("_uploads", "_imported"):
            g["original"] = str(rel)
        else:
            g["dual"] = g["dual"] or str(rel)
        g["mtime"] = max(g["mtime"], pdf.stat().st_mtime)

    loose = sorted(groups.values(), key=lambda x: x["mtime"], reverse=True)
    for g in loose:
        translated = bool(g["mono"] or g["dual"])
        items.append(
            {
                "id": None,
                "name": g["name"],
                "time": datetime.fromtimestamp(g["mtime"]).strftime("%Y-%m-%d %H:%M"),
                "kind": ("双语对照" if g["dual"] else "纯译文") if translated else "待翻译",
                "mono": g["mono"],
                "dual": g["dual"],
                "original": g["original"],
                "translated": translated,
                "model": None,
                "status": "done" if translated else "pending",
            }
        )
    return items


# ---------------------------------------------------------------------------
# 路径与视图
# ---------------------------------------------------------------------------


def _resolve(rel: str) -> Path:
    p = (LIBRARY_ROOT / rel).resolve()
    if LIBRARY_ROOT not in p.parents and p != LIBRARY_ROOT:
        raise ValueError(f"路径越界: {rel}")
    if not p.exists():
        raise FileNotFoundError(rel)
    return p


def to_rel(path: Path | str) -> str:
    return str(Path(path).resolve().relative_to(LIBRARY_ROOT))


def make_side_by_side(rel: str) -> str:
    from ..history_tab import make_side_by_side as _mbs

    return to_rel(_mbs(_resolve(rel)))


def extract_view(rel: str, which: str) -> str:
    from ..history_tab import extract_view as _ev

    return to_rel(_ev(_resolve(rel), which))


# ---------------------------------------------------------------------------
# 页面渲染(pymupdf → PNG,带缓存;并发由 server 层信号量限制)
# ---------------------------------------------------------------------------


def pdf_info(rel: str) -> dict:
    doc = pymupdf.open(_resolve(rel))
    try:
        page = doc.load_page(0)
        return {
            "pages": doc.page_count,
            "width": round(page.rect.width),
            "height": round(page.rect.height),
        }
    finally:
        doc.close()


def render_page(rel: str, page_no: int, zoom: float = 2.0) -> bytes:
    src = _resolve(rel)
    key = hashlib.sha1(
        f"{src}|{src.stat().st_mtime_ns}|{page_no}|{zoom}".encode()
    ).hexdigest()[:24]
    cache = PAGE_CACHE_DIR / f"{key}.png"
    if cache.exists():
        return cache.read_bytes()
    doc = pymupdf.open(src)
    try:
        page = doc.load_page(page_no - 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        data = pix.tobytes("png")
    finally:
        doc.close()
    PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# 翻译执行
# ---------------------------------------------------------------------------


def _zh_stage(ev: dict) -> str:
    stage = ev.get("stage") or ""
    for en, zh in STAGE_ZH.items():
        if stage.startswith(en):
            stage = zh + stage[len(en):]
            break
    cur, total = ev.get("stage_current"), ev.get("stage_total")
    if cur and total:
        stage = f"{stage} ({cur}/{total})" if stage else f"{cur}/{total}"
    return stage


def start_translation(pdf_path: Path, settings: dict) -> str:
    task_id = hashlib.sha1(f"{pdf_path}|{time.time_ns()}".encode()).hexdigest()[:12]
    input_rel = to_rel(pdf_path)
    job = upsert_job_for_input(input_rel, pdf_path.stem)
    entry = {
        "id": task_id,
        "job_id": job["id"],
        "name": pdf_path.stem,
        "status": "running",
        "progress": 0,
        "stage": "准备中",
        "detail": "",
        "input": str(pdf_path),
        "mono": None,
        "dual": None,
        "error": None,
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": settings.get("ollama_model", ""),
    }
    TASKS[task_id] = entry
    BUS.publish({"type": "task_update", "task": public_task(entry)})
    RUNNING[task_id] = asyncio.get_running_loop().create_task(
        _run(task_id, pdf_path, settings)
    )
    return task_id


def cancel_translation(task_id: str) -> bool:
    task = RUNNING.pop(task_id, None)
    if task is None:
        return False
    task.cancel()
    return True


async def _run(task_id: str, pdf_path: Path, settings: dict) -> None:
    entry = TASKS[task_id]
    log_lines: list[str] = []
    last_sig: tuple | None = None

    def publish_changed() -> None:
        nonlocal last_sig
        sig = (entry["stage"], entry["progress"])
        if sig != last_sig:
            last_sig = sig
            BUS.publish({"type": "task_update", "task": public_task(entry)})

    try:
        from pdf2zh_next.config.model import SettingsModel
        from pdf2zh_next.config.translate_engine_model import OllamaSettings
        from pdf2zh_next.high_level import do_translate_async_stream

        out_dir = LIBRARY_ROOT / f"webapp-{datetime.now():%Y%m%d-%H%M%S}-{task_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # translate_engine_settings 是必填字段,必须在构造时提供
        engine_settings = OllamaSettings(
            ollama_model=settings.get("ollama_model", DEFAULT_SETTINGS["ollama_model"]),
            ollama_host=settings.get("ollama_host", DEFAULT_SETTINGS["ollama_host"]),
        )
        sm = SettingsModel(translate_engine_settings=engine_settings)
        sm.basic.input_files = {str(pdf_path)}
        sm.translation.lang_in = settings.get("lang_in", "en")
        sm.translation.lang_out = settings.get("lang_out", "zh")
        sm.translation.output = str(out_dir)

        entry["stage"] = "启动翻译内核"
        publish_changed()

        async for ev in do_translate_async_stream(sm, pdf_path):
            etype = ev.get("type")
            if etype == "progress_start":
                txt = _zh_stage(ev)
                if txt:
                    entry["stage"] = txt
            elif etype == "progress_update":
                prog = ev.get("overall_progress")
                if isinstance(prog, (int, float)):
                    entry["progress"] = round(prog)
                txt = _zh_stage(ev)
                if txt:
                    entry["stage"] = txt
                part, parts = ev.get("part_index"), ev.get("total_parts")
                entry["detail"] = f"第 {part}/{parts} 部分" if part and parts and parts > 1 else ""
            elif etype == "finish":
                result = ev.get("translate_result")
                if result is not None:
                    if getattr(result, "mono_pdf_path", None):
                        entry["mono"] = to_rel(result.mono_pdf_path)
                    if getattr(result, "dual_pdf_path", None):
                        entry["dual"] = to_rel(result.dual_pdf_path)
                usage = ev.get("token_usage")
                if usage:
                    log_lines.append(f"token 用量: {usage}")
            elif etype == "error":
                raise RuntimeError(str(ev.get("message") or ev.get("error") or "翻译失败"))
            publish_changed()

        entry["status"] = "done"
        entry["progress"] = 100
        entry["stage"] = "完成"
        entry["detail"] = ""
        complete_job(entry["job_id"], entry["mono"], entry["dual"], entry["model"], "done")
        BUS.publish({"type": "task_done", "task": public_task(entry)})
    except asyncio.CancelledError:
        entry["status"] = "failed"
        entry["stage"] = "已取消"
        entry["error"] = "用户取消"
        complete_job(entry["job_id"], None, None, entry["model"], "failed")
        BUS.publish({"type": "task_done", "task": public_task(entry)})
    except Exception as exc:  # noqa: BLE001 — 错误要展示给用户
        entry["status"] = "failed"
        entry["stage"] = "失败"
        entry["error"] = str(exc)
        log_lines.append(f"错误: {exc}")
        complete_job(entry["job_id"], None, None, entry["model"], "failed")
        BUS.publish({"type": "task_done", "task": public_task(entry)})
    finally:
        RUNNING.pop(task_id, None)
        if log_lines:
            BUS.publish({"type": "log", "task_id": task_id, "lines": log_lines})
        TASKS.pop(task_id, None)  # 已落到登记表,内存表即时清理


def delete_entries(paths: list[str], job_ids: list[str]) -> int:
    """删除库内文件与登记表条目,返回成功删除的文件数。"""
    n = 0
    parent_dirs: set[Path] = set()
    deleted: set[str] = set()
    for rel in paths:
        try:
            p = _resolve(rel)
            parent_dirs.add(p.parent)
            p.unlink()
            deleted.add(str(p.relative_to(LIBRARY_ROOT)))
            n += 1
        except Exception:
            pass
    for jid in job_ids:
        JOBS[:] = [j for j in JOBS if j["id"] != jid]
    # 清空指向已删文件的产物指针,避免卡片残留失效路径
    for j in JOBS:
        for k in ("mono", "dual", "input"):
            if j.get(k) and j[k] in deleted:
                j[k] = None
        if j.get("status") == "done" and not (j.get("mono") or j.get("dual")):
            j["status"] = "pending" if j.get("input") else j["status"]
    save_jobs()
    for d in sorted(parent_dirs):
        try:
            next(d.iterdir())
        except StopIteration:
            d.rmdir()
    return n
