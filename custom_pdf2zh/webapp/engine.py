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
import types
import typing
from datetime import datetime
from pathlib import Path

import pymupdf
import shutil

LIBRARY_ROOT = Path("pdf2zh_files").resolve()
UPLOAD_DIR = LIBRARY_ROOT / "_uploads"
CACHE_DIR = LIBRARY_ROOT / "_sidecache"
EXPORT_DIR = LIBRARY_ROOT / "_exports"  # 用户可见的成品文件夹(干净命名)
PAGE_CACHE_DIR = CACHE_DIR / "pages"
SETTINGS_FILE = LIBRARY_ROOT / "_webapp_settings.json"
JOBS_FILE = LIBRARY_ROOT / "_jobs.json"

DEFAULT_SETTINGS: dict = {
    "engine": "Ollama",
    "engine_fields": {
        "ollama_model": "s2021008840/hy-mt2:7b-q4_k_m",
        "ollama_host": "http://localhost:11434",
    },
    "lang_in": "en",
    "lang_out": "zh",
    # 自动术语表提取(官方 no_auto_extract_glossary 的反向开关):
    # LLM 引擎默认开,保证术语全文一致;本地小模型耗时翻倍,工作台默认关
    "auto_extract_glossary": False,
}

# ---------------------------------------------------------------------------
# 翻译服务注册表:与官方 pdf2zh-next 的全部翻译引擎对齐,
# 表单字段直接从内核的 pydantic 设置模型自省派生,内核升级自动跟上。
# ---------------------------------------------------------------------------

# 服务中文展示名与分组;group: free=免配置 local=本地 llm=大模型云服务 trad=传统翻译
SERVICE_META: dict[str, dict] = {
    "Google": {"label": "Google 翻译", "group": "free"},
    "Bing": {"label": "必应翻译", "group": "free"},
    "SiliconFlowFree": {"label": "硅基流动（免费）", "group": "free"},
    "Ollama": {"label": "Ollama（本地）", "group": "local"},
    "Xinference": {"label": "Xinference（本地）", "group": "local"},
    "ClaudeCode": {"label": "Claude Code（本地 CLI）", "group": "local"},
    "OpenAI": {"label": "OpenAI", "group": "llm"},
    "DeepSeek": {"label": "DeepSeek", "group": "llm"},
    "Zhipu": {"label": "智谱 AI", "group": "llm"},
    "SiliconFlow": {"label": "硅基流动", "group": "llm"},
    "Gemini": {"label": "Google Gemini", "group": "llm"},
    "Grok": {"label": "xAI Grok", "group": "llm"},
    "Groq": {"label": "Groq", "group": "llm"},
    "ModelScope": {"label": "魔搭 ModelScope", "group": "llm"},
    "AliyunDashScope": {"label": "阿里云百炼", "group": "llm"},
    "AzureOpenAI": {"label": "Azure OpenAI", "group": "llm"},
    "OpenAICompatible": {"label": "OpenAI 兼容接口", "group": "llm"},
    "DeepL": {"label": "DeepL", "group": "trad"},
    "Azure": {"label": "Azure 翻译", "group": "trad"},
    "TencentMechineTranslation": {"label": "腾讯云翻译", "group": "trad"},
    "QwenMt": {"label": "阿里 QwenMT", "group": "trad"},
    "AnythingLLM": {"label": "AnythingLLM", "group": "trad"},
    "Dify": {"label": "Dify", "group": "trad"},
}
GROUP_LABELS = {
    "free": "免费 / 无需配置",
    "local": "本地部署",
    "llm": "大模型服务(填 API Key)",
    "trad": "传统翻译服务",
}

_FIELD_LABELS = (
    ("_api_key", "API Key"),
    ("apikey", "API Key"),
    ("auth_key", "Auth Key"),
    ("secret_id", "Secret ID"),
    ("secret_key", "Secret Key"),
    ("_model", "模型"),
    ("_base_url", "接口地址"),
    ("_host", "服务地址"),
    ("endpoint", "接口地址"),
    ("_url", "服务地址"),
    ("api_version", "API 版本"),
    ("_timeout", "超时(秒)"),
    ("claude_code_path", "claude 命令路径"),
)
# 不放进表单的高级开关,留空时走官方默认值
_SKIP_FIELD_SUFFIXES = (
    "_enable_json_mode",
    "_send_temperature",
    "_send_reasoning_effort",
    "_reasoning_effort",
    "_enable_thinking",
    "_send_enable_thinking_param",
    "ali_domains",
    # 内部调优参数:翻译器会按输入长度自动放大 num_predict,手动设置会被覆盖,
    # 设小了反而截断译文,不暴露给用户
    "num_predict",
)
_SECRET_HINTS = ("api_key", "apikey", "auth_key", "secret_key", "secret_id")

_service_cache: list[dict] | None = None


def _norm_engine(name: str) -> str:
    """引擎名大小写归一(旧配置里存的是小写 ollama),并校验存在性。"""
    from pdf2zh_next.config.translate_engine_model import (
        TRANSLATION_ENGINE_METADATA_MAP,
    )

    if name in TRANSLATION_ENGINE_METADATA_MAP:
        return name
    low = (name or "").lower()
    for key in TRANSLATION_ENGINE_METADATA_MAP:
        if key.lower() == low:
            return key
    return name or "Ollama"


def _field_label(name: str) -> str:
    for suffix, label in _FIELD_LABELS:
        if name == suffix or name.endswith(suffix):
            return label
    return name


def service_registry() -> list[dict]:
    """全部翻译服务的表单定义,按 免费→本地→大模型→传统 排序。"""
    global _service_cache
    if _service_cache is not None:
        return _service_cache
    from pdf2zh_next.config.translate_engine_model import TRANSLATION_ENGINE_METADATA

    out: list[dict] = []
    for m in TRANSLATION_ENGINE_METADATA:
        info = SERVICE_META.get(
            m.translate_engine_type,
            {"label": m.translate_engine_type, "group": "llm"},
        )
        fields: list[dict] = []
        for name, f in m.setting_model_type.model_fields.items():
            if name in ("translate_engine_type", "support_llm"):
                continue
            if any(name.endswith(s) for s in _SKIP_FIELD_SUFFIXES):
                continue
            default = f.default
            if default is None and f.default_factory is not None:
                try:
                    default = f.default_factory()
                except Exception:
                    default = None
            fields.append(
                {
                    "name": name,
                    "label": _field_label(name),
                    "default": "" if default is None else str(default),
                    "secret": any(h in name.lower() for h in _SECRET_HINTS),
                }
            )
        out.append(
            {
                "type": m.translate_engine_type,
                "label": info["label"],
                "group": info["group"],
                "llm": m.support_llm,
                "fields": fields,
            }
        )
    order = {"free": 0, "local": 1, "llm": 2, "trad": 3}
    out.sort(key=lambda s: (order.get(s["group"], 9), s["label"]))
    _service_cache = out
    return out


def _coerce_field_value(annotation, value):
    """按 pydantic 字段类型宽松归一表单值;解析失败返回 None(改用官方默认值)。"""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:  # Optional[X] / X | None
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _coerce_field_value(args[0], value)
        return value
    if annotation is bool:
        if isinstance(value, bool):
            return value
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            return True
        if low in ("false", "0", "no", "n", "off"):
            return False
        return None
    if annotation in (int, float):
        try:
            return annotation(value)
        except (TypeError, ValueError):
            return None
    return value


def build_engine_settings(settings: dict):
    """把工作台设置构造成官方内核的翻译引擎设置对象。"""
    from pdf2zh_next.config.translate_engine_model import (
        TRANSLATION_ENGINE_METADATA_MAP,
    )

    engine = _norm_engine(settings.get("engine") or "Ollama")
    meta = TRANSLATION_ENGINE_METADATA_MAP.get(engine)
    if meta is None:
        raise ValueError(f"不支持的翻译服务: {settings.get('engine')}")
    known = meta.setting_model_type.model_fields
    kwargs: dict = {}
    for key, value in (settings.get("engine_fields") or {}).items():
        if key not in known or key in ("translate_engine_type", "support_llm"):
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue  # 留空 = 用官方默认值
        if isinstance(value, str):
            value = value.strip()
        value = _coerce_field_value(known[key].annotation, value)
        if value is None:
            continue
        kwargs[key] = value
    return meta.setting_model_type(**kwargs)


def display_model(settings: dict) -> str:
    """任务卡片上展示的引擎标识:优先取 *_model 字段,否则用服务中文名。"""
    engine = _norm_engine(settings.get("engine") or "Ollama")
    fields = settings.get("engine_fields") or {}
    model = fields.get(f"{engine.lower()}_model") or ""
    if model:
        return str(model)
    return SERVICE_META.get(engine, {}).get("label", engine)


def _engine_supports_llm(engine_type: str) -> bool:
    from pdf2zh_next.config.translate_engine_model import (
        TRANSLATION_ENGINE_METADATA_MAP,
    )

    meta = TRANSLATION_ENGINE_METADATA_MAP.get(engine_type)
    return bool(meta and meta.support_llm)


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
    data["engine_fields"] = dict(DEFAULT_SETTINGS["engine_fields"])
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        for key in ("engine", "lang_in", "lang_out"):
            if saved.get(key):
                data[key] = saved[key]
        if "auto_extract_glossary" in saved:
            data["auto_extract_glossary"] = bool(saved["auto_extract_glossary"])
        if isinstance(saved.get("engine_fields"), dict):
            data["engine_fields"].update(
                {
                    k: v
                    for k, v in saved["engine_fields"].items()
                    if v is not None and (not isinstance(v, str) or v.strip())
                }
            )
        elif saved:
            # 旧版扁平结构(ollama_model/ollama_host 在顶层)迁移
            for legacy in ("ollama_model", "ollama_host"):
                if saved.get(legacy):
                    data["engine_fields"][legacy] = saved[legacy]
    data["engine"] = _norm_engine(data.get("engine", "Ollama"))
    return data


def save_settings(settings: dict) -> dict:
    merged = load_settings()
    for key in ("engine", "lang_in", "lang_out"):
        if settings.get(key):
            merged[key] = settings[key]
    if "auto_extract_glossary" in settings:
        merged["auto_extract_glossary"] = bool(settings["auto_extract_glossary"])
    if isinstance(settings.get("engine_fields"), dict):
        for key, value in settings["engine_fields"].items():
            # 空值 = 删除该字段(界面清空 key 保存即清除,留空项走官方默认值)
            if value is None or (isinstance(value, str) and not value.strip()):
                merged["engine_fields"].pop(key, None)
            else:
                merged["engine_fields"][key] = value
    merged["engine"] = _norm_engine(merged.get("engine", "Ollama"))
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
    known = {str(p).replace("\\", "/") for p in ({j.get("mono") for j in JOBS} | {j.get("dual") for j in JOBS})}
    groups: dict[tuple, dict] = {}
    for pdf in LIBRARY_ROOT.rglob("*.pdf"):
        rel = pdf.relative_to(LIBRARY_ROOT)
        if rel.parts[0] in ("_uploads", "_imported", "_sidecache", "_exports"):
            continue
        r = str(rel).replace("\\", "/")
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
        # 刚生成的会话产物多半属于正在运行的任务(它完成时会自行登记),
        # 此刻收编会把半成品当成无主产物,导出出带 .no_watermark 的重复件
        if time.time() - mtime < 600:
            continue
        name = re.sub(r"\.zh$", "", base, flags=re.IGNORECASE)
        name = re.sub(r"\.no_watermark$", "", name, flags=re.IGNORECASE)
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
        if rel.parts[0] in ("_sidecache", "_exports"):
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
        if (
            translated
            and g["dir"].startswith("webapp-")
            and time.time() - g["mtime"] < 600
        ):
            continue  # 进行中会话的半成品,等任务完成自行登记
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


def page_cache_bucket(src: Path) -> Path:
    """页面渲染缓存按源文件路径分桶,删除文件时可整桶清理。"""
    return PAGE_CACHE_DIR / hashlib.sha1(str(src).encode()).hexdigest()[:16]


def render_page(rel: str, page_no: int, zoom: float = 2.0) -> bytes:
    src = _resolve(rel)
    bucket = page_cache_bucket(src)
    key = hashlib.sha1(
        f"{src.stat().st_mtime_ns}|{page_no}|{zoom}".encode()
    ).hexdigest()[:24]
    cache = bucket / f"{key}.png"
    if cache.exists():
        return cache.read_bytes()
    doc = pymupdf.open(src)
    try:
        page = doc.load_page(page_no - 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        data = pix.tobytes("png")
    finally:
        doc.close()
    bucket.mkdir(parents=True, exist_ok=True)
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
        "model": display_model(settings),
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
        from pdf2zh_next.high_level import do_translate_async_stream

        out_dir = LIBRARY_ROOT / f"webapp-{datetime.now():%Y%m%d-%H%M%S}-{task_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 按所选服务构造官方引擎设置(支持全部 translate_engine_type)
        engine_settings = build_engine_settings(settings)
        sm = SettingsModel(translate_engine_settings=engine_settings)
        sm.basic.input_files = {str(pdf_path)}
        sm.translation.lang_in = settings.get("lang_in", "en")
        sm.translation.lang_out = settings.get("lang_out", "zh")
        sm.translation.output = str(out_dir)
        sm.pdf.watermark_output_mode = "no_watermark"  # 商用交付:关闭 BabelDOC 水印行
        # 自动术语表提取:仅 LLM 引擎可开;非 LLM 引擎内核已强制关闭,勿覆盖
        engine_type = engine_settings.model_fields["translate_engine_type"].default
        supports_llm = _engine_supports_llm(engine_type)
        sm.translation.no_auto_extract_glossary = not (
            supports_llm and bool(settings.get("auto_extract_glossary", False))
        )

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
        exported = export_task(entry["name"], entry["mono"], entry["dual"])
        if exported:
            log_lines.append("成品已导出到: " + str(EXPORT_DIR))
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


def export_task(name: str, mono_rel: str | None, dual_rel: str | None) -> list[str]:
    """把成品按干净命名拷入成品文件夹(同名覆盖 = 始终最新版)。"""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for rel, suffix in ((mono_rel, "纯译文"), (dual_rel, "中英对照")):
        if not rel:
            continue
        try:
            src_pdf = _resolve(rel)
            dst = EXPORT_DIR / f"{name}-{suffix}.pdf"
            shutil.copy2(src_pdf, dst)
            out.append(str(dst))
        except Exception:
            continue
    return out


def ensure_all_exports() -> int:
    """启动回填:历史上已完成的任务补齐成品文件夹(缺失才拷)。"""
    load_jobs()
    n = 0
    for j in JOBS:
        if j.get("status") == "done" and (j.get("mono") or j.get("dual")):
            name = j["name"]
            if not (EXPORT_DIR / f"{name}-纯译文.pdf").exists() and not (
                EXPORT_DIR / f"{name}-中英对照.pdf"
            ).exists():
                n += len(export_task(name, j.get("mono"), j.get("dual")))
    return n


# 对照/提取视图缓存的固定后缀(history_tab._cache_path 生成)
DERIVED_VIEW_SUFFIXES = (".左原文右译文", ".仅译文", ".仅原文")


def purge_derived_cache(sources: list[Path]) -> None:
    """删除文件后,同步清掉 _sidecache 里它的派生视图与页面渲染缓存。"""
    names: set[str] = set()
    buckets: list[Path] = []
    for src in sources:
        for suffix in DERIVED_VIEW_SUFFIXES:
            names.add(f"{src.stem}{suffix}.pdf")
        buckets.append(page_cache_bucket(src))
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            if f.is_file() and f.name in names:
                try:
                    f.unlink()
                except OSError:
                    pass
    for b in buckets:
        shutil.rmtree(b, ignore_errors=True)


def purge_orphan_cache() -> None:
    """启动清理:旧版平铺布局的页面渲染缓存,以及源文件已不存在的派生视图。

    前者是一次性迁移(新版 render_page 已按源文件分桶);
    后者兜底历史遗留——delete_entries 只能清"删文件时"的缓存,
    文件先于本机制被删掉的缓存要靠这里扫掉。
    """
    if not CACHE_DIR.exists():
        return
    if PAGE_CACHE_DIR.exists():
        for f in PAGE_CACHE_DIR.iterdir():
            if f.is_file() and f.suffix == ".png":
                try:
                    f.unlink()
                except OSError:
                    pass
    live_stems = {
        p.stem for p in LIBRARY_ROOT.rglob("*.pdf") if CACHE_DIR not in p.parents
    }
    for f in CACHE_DIR.iterdir():
        if not f.is_file() or not f.name.endswith(".pdf"):
            continue
        stem = f.name[:-4]
        for suffix in DERIVED_VIEW_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        else:
            continue
        if stem not in live_stems:
            try:
                f.unlink()
            except OSError:
                pass


def delete_entries(paths: list[str], job_ids: list[str]) -> int:
    """删除库内文件与登记表条目,返回成功删除的文件数。"""
    n = 0
    parent_dirs: set[Path] = set()
    deleted: set[str] = set()
    sources: list[Path] = []
    # 删除成品文件夹中同名导出件
    for jid in job_ids:
        for j in JOBS:
            if j["id"] == jid:
                for suffix in ("纯译文", "中英对照"):
                    p = EXPORT_DIR / f"{j['name']}-{suffix}.pdf"
                    try:
                        p.unlink()
                    except OSError:
                        pass
                break
    for rel in paths:
        try:
            p = _resolve(rel)
            parent_dirs.add(p.parent)
            p.unlink()
            deleted.add(str(p.relative_to(LIBRARY_ROOT)))
            sources.append(p)
            n += 1
        except Exception:
            pass
    purge_derived_cache(sources)
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
