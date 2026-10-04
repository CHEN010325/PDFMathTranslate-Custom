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
import logging
import multiprocessing
import os
import re
import shutil
import threading
import time
import types
import typing
from datetime import datetime
from pathlib import Path

import pymupdf
from starlette.concurrency import run_in_threadpool

LIBRARY_ROOT = Path("pdf2zh_files").resolve()
UPLOAD_DIR = LIBRARY_ROOT / "_uploads"
CACHE_DIR = LIBRARY_ROOT / "_sidecache"
EXPORT_DIR = LIBRARY_ROOT / "_exports"  # 用户可见的成品文件夹(干净命名)
PAGE_CACHE_DIR = CACHE_DIR / "pages"
SETTINGS_FILE = LIBRARY_ROOT / "_webapp_settings.json"
JOBS_FILE = LIBRARY_ROOT / "_jobs.json"


def _prepend_cuda_dll_dirs() -> None:
    """版面识别 GPU 加速前置:把 pip 安装的 CUDA/cuDNN 运行库目录注入 DLL 搜索路径。

    必须在 onnxruntime 首次导入前执行(本模块导入时即满足;内核在 _run 里
    才惰性 import babeldoc→onnxruntime)。系统装没装 CUDA 都无所谓——
    优先用 venv 里 pip 拉下来的 nvidia-* 轮子自带 DLL,自包含、免客户配置;
    一个目录都没找到就静默跳过(纯 CPU onnxruntime 构建本来也用不上)。
    """
    if os.name != "nt":
        return  # os.add_dll_directory 仅 Windows 有;非 Windows 无 DLL 搜索路径问题
    try:
        import sysconfig

        nvidia_root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
        if not nvidia_root.is_dir():
            return
        dll_dirs = sorted(p for p in nvidia_root.glob("*/bin") if p.is_dir())
        if not dll_dirs:
            return
        # PATH 前插,抢占系统里可能存在的旧版 CUDA(如 CUDA 11)
        os.environ["PATH"] = ";".join(str(p) for p in dll_dirs) + ";" + os.environ.get("PATH", "")
        for p in dll_dirs:
            os.add_dll_directory(str(p))
        logging.getLogger("custom_pdf2zh.engine").info(
            "CUDA DLL dirs injected: %s", ", ".join(p.name for p in dll_dirs)
        )
    except Exception:
        pass  # GPU 属于增强项,任何失败都不能影响启动


_prepend_cuda_dll_dirs()


def _lower_process_priority() -> None:
    """整体降优先级:工作台进程设为"低于正常",内核子进程在 Windows 上继承
    该优先级——翻译重活不抢用户前台应用的 CPU,电脑保持流畅。
    (配合串行队列"同时只跑一个任务"+ GPU 版面识别,整体占用压到最低)
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000):  # BELOW_NORMAL_PRIORITY_CLASS
            raise OSError("SetPriorityClass failed")
        logging.getLogger("custom_pdf2zh.engine").info(
            "process priority set to BELOW_NORMAL (children inherit)"
        )
    except Exception:
        pass  # 降优先级失败不影响功能


_lower_process_priority()


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
    # 段落翻译并发上限。本地引擎由 resolve_qps 强制串行(qps=1);
    # 此项仅对云服务生效,调到 8~16 往往近线性提速。None = 云服务走内核默认(4)
    "qps": None,
    # 仅输出纯译文:跳过双语对照排版,后处理明显更快
    "no_dual": False,
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


def resolve_qps(settings: dict, engine_type: str) -> int | None:
    """段落翻译并发上限:本地引擎一律串行(qps=1),其他引擎取用户设置。

    本地 GPU 推理实测(qps=1 vs qps=8,相近文本量)加速比仅 1.06x——
    瓶颈在显存带宽,批处理换不来墙钟时间;小显存机器高并发还会撑爆
    KV cache 触发模型卸载,大幅负优化。云服务瓶颈在客户端并发数,
    调高才有收益。返回 None = 跟随内核默认(qps=4)。
    """
    if SERVICE_META.get(engine_type, {}).get("group") == "local":
        return 1
    qps = settings.get("qps")
    if isinstance(qps, str) and qps.strip().isdigit():
        qps = int(qps)
    return qps if isinstance(qps, int) and qps >= 1 else None


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

# BabelDOC 阶段名 → 中文(前缀匹配)。
# 覆盖内核全部 stage_name(见 babeldoc/format/pdf/high_level.py 的阶段权重表),
# 用户可见的进度文案不允许残留英文;长名放前面,避免 "Parse Page" 抢先吃掉
# "Parse Page Layout" 这类前缀重叠的键。
STAGE_ZH: dict[str, str] = {
    "Parse PDF and Create Intermediate Representation": "解析 PDF 并重建文档结构",
    "Automatic Term Extraction": "自动提取术语",
    "Generate drawing instructions": "组装 PDF 页面",
    "Parse Formulas and Styles": "解析公式与样式",
    "Parse Page Layout": "解析页面版面",
    "Translate Paragraphs": "翻译段落",
    "Parse Paragraphs": "解析段落",
    "Remove Char Descent": "清理字符下延",
    "DetectScannedFile": "检测扫描件",
    "Download assets": "下载排版资源",
    "Add Debug Information": "写入调试信息",
    "Loading fonts": "加载字体",
    "Parse Table": "解析表格",
    "Subset font": "字体子集化",
    "Warmup": "引擎预热",
    "Add Fonts": "嵌入字体",
    "Parse layout": "解析版面",
    "Parse Page": "解析页面",
    "Typesetting": "排版合成",
    "Save PDF": "保存 PDF",
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
        if "no_dual" in saved:
            data["no_dual"] = bool(saved["no_dual"])
        if "qps" in saved:
            try:
                qps = int(saved["qps"])
                data["qps"] = qps if qps >= 1 else None
            except (TypeError, ValueError):
                pass
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


def save_settings(settings: dict, full_replace: bool = False) -> dict:
    """保存设置。engine_fields 的合并语义分两种:

    - full_replace=True(设置面板保存):前端发的是所有引擎字段的**全集**,
      以表单为准整体替换——用户清空的字段(如删掉的 API Key)真正从文件
      里消失,否则"前端删了、后端缺失=保留"会让旧值复活。
    - full_replace=False(翻译时自动保存):body 只带当前引擎的字段,
      保持合并,不碰其他引擎已存的配置。
    """
    merged = load_settings()
    for key in ("engine", "lang_in", "lang_out"):
        if settings.get(key):
            merged[key] = settings[key]
    if "auto_extract_glossary" in settings:
        merged["auto_extract_glossary"] = bool(settings["auto_extract_glossary"])
    if "no_dual" in settings:
        merged["no_dual"] = bool(settings["no_dual"])
    if "qps" in settings:
        try:
            qps = int(settings["qps"] or 0)
            merged["qps"] = qps if qps >= 1 else None
        except (TypeError, ValueError):
            merged["qps"] = None
    if isinstance(settings.get("engine_fields"), dict):
        if full_replace:
            cleaned: dict = {}
            for key, value in settings["engine_fields"].items():
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue  # 空值 = 该字段删除
                cleaned[key] = value
            merged["engine_fields"] = cleaned
        else:
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
_jobs_lock = threading.Lock()  # 启动导出线程与请求线程可能并发首次加载


def _norm_base(name: str) -> tuple[str, str]:
    """文件名 → (基础名, 种类 dual/mono/plain)。"""
    lower = name.lower()
    if lower.endswith(".dual.pdf"):
        return name[: -len(".dual.pdf")], "dual"
    if lower.endswith(".mono.pdf"):
        return name[: -len(".mono.pdf")], "mono"
    return (name[: -4] if lower.endswith(".pdf") else name), "plain"


def _norm_rel(rel: str | None) -> str:
    return (rel or "").replace("\\", "/").lower()


def load_jobs() -> list[dict]:
    global JOBS, _jobs_loaded
    with _jobs_lock:
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
        # 同名任务已登记:这份产物多半是该任务旧一轮翻译的遗留副本
        # (旧版重译不清理旧会话目录),收编会变成同文档第二张卡,跳过
        if any(j.get("name") == name for j in JOBS):
            continue
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
    invalidate_scan_cache()
    return job


def _discard_stale_output(job_id: str, rel: str | None) -> None:
    """重译成功后清掉同任务上一轮的旧产物。

    旧产物不再被登记表引用,放着会变成两块赘肉:scan_library 把它当
    散件显示成同文档第二张卡;磁盘上每重译一次就多一整个会话目录。
    若它恰好被旧版"收编"成了幽灵登记(无 input 的同名条目),一并移除。
    只在 status=done 时调用——失败/取消不动旧成品。
    """
    if not rel:
        return
    norm = _norm_rel(rel)
    JOBS[:] = [
        j
        for j in JOBS
        if j["id"] == job_id
        or (_norm_rel(j.get("mono")) != norm and _norm_rel(j.get("dual")) != norm)
    ]
    try:
        p = _resolve(rel)
    except Exception:
        return
    try:
        purge_derived_cache([p])
        p.unlink()
    except OSError:
        return
    parent = p.parent
    if parent != LIBRARY_ROOT and parent.name.startswith("webapp-"):
        try:
            next(parent.iterdir())
        except StopIteration:
            shutil.rmtree(parent, ignore_errors=True)


def complete_job(
    job_id: str, mono: str | None, dual: str | None, model: str | None, status: str
) -> None:
    job = next((j for j in JOBS if j["id"] == job_id), None)
    if job is None:
        return
    if status == "done":
        # 新成品落位成功,旧一轮的同文档产物就是纯垃圾,先清再换指针
        for key, new in (("mono", mono), ("dual", dual)):
            old = job.get(key)
            if old and (not new or _norm_rel(old) != _norm_rel(new)):
                _discard_stale_output(job_id, old)
    if status == "failed" and (job.get("mono") or job.get("dual")):
        # 重译失败/取消,但历史成品仍在:卡片保持"完成",
        # 不能让一次失败的重译把有效历史打成"失败"
        status = "done"
    if mono:
        job["mono"] = mono
    if dual:
        job["dual"] = dual
    job["model"] = model
    job["status"] = status
    job["time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_jobs()
    invalidate_scan_cache()


# ---------------------------------------------------------------------------
# 历史库扫描:登记表优先,散文件(导入/无主产物)兜底
#
# /api/tasks 由前端 SSE 节流(1s)与轮询兜底(3s)高频调用,而扫描要
# rglob 整个库并逐文件 stat——历史越多越慢,Windows 上还伴随杀软扫描。
# 加 1.5s TTL 缓存:登记表/文件变更点主动失效,轮询命中缓存直接返回。
# ---------------------------------------------------------------------------

_SCAN_TTL = 1.5
_scan_lock = threading.Lock()
_scan_cache: tuple[float, list[dict]] | None = None


def invalidate_scan_cache() -> None:
    global _scan_cache
    with _scan_lock:
        _scan_cache = None


def scan_library() -> list[dict]:
    global _scan_cache
    now = time.monotonic()
    with _scan_lock:
        if _scan_cache is not None and now - _scan_cache[0] < _SCAN_TTL:
            return _scan_cache[1]
    items = _scan_library_impl()
    with _scan_lock:
        _scan_cache = (time.monotonic(), items)
    return items


def _scan_library_impl() -> list[dict]:
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


UPLOAD_HASH_FILE = UPLOAD_DIR / "_hashes.json"


def _store_upload(filename: str, data: bytes) -> dict:
    """上传落盘,返回 {uploaded, name, dedup}。

    同一内容的 PDF 反复上传是常见操作(改了设置想重译),按文件名加 -2
    后缀会制造重复卡片;改为按内容哈希复用既有文件,重译走"重新翻译"语义。
    哈希索引存 _uploads/_hashes.json,指向已消失文件的条目顺手清掉。
    """
    digest = hashlib.sha1(data).hexdigest()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}
    if UPLOAD_HASH_FILE.exists():
        try:
            index = json.loads(UPLOAD_HASH_FILE.read_text(encoding="utf-8"))
        except Exception:
            index = {}
    hit = index.get(digest)
    if hit:
        p = LIBRARY_ROOT / hit
        if p.exists():
            return {"uploaded": hit, "name": Path(hit).stem, "dedup": True}
    dest = UPLOAD_DIR / Path(filename).name
    n = 2
    while dest.exists():
        dest = UPLOAD_DIR / f"{dest.stem}-{n}{dest.suffix}"
        n += 1
    dest.write_bytes(data)
    rel = to_rel(dest)
    index[digest] = rel
    index = {k: v for k, v in index.items() if (LIBRARY_ROOT / v).exists()}
    UPLOAD_HASH_FILE.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return {"uploaded": rel, "name": dest.stem, "dedup": False}


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


# 页面渲染缓存总量上限:预览过的每一页都会落盘,不设上限迟早撑爆磁盘。
# 启动时超限按最旧优先清理(LRU)。
PAGE_CACHE_MAX_BYTES = 2 * 1024**3


def prune_page_cache(max_bytes: int = PAGE_CACHE_MAX_BYTES) -> int:
    """按总量清理页面渲染缓存,返回删除的文件数。"""
    if not PAGE_CACHE_DIR.exists():
        return 0
    files: list[Path] = []
    for f in PAGE_CACHE_DIR.rglob("*"):
        try:
            if f.is_file():
                files.append(f)
        except OSError:
            continue
    def _size(f: Path) -> int:
        try:
            return f.stat().st_size
        except OSError:
            return 0
    total = sum(_size(f) for f in files)
    if total <= max_bytes:
        return 0

    def _mtime(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=_mtime)
    freed = 0
    removed = 0
    for f in files:
        if total - freed <= max_bytes:
            break
        size = _size(f)
        try:
            f.unlink()
            freed += size
            removed += 1
        except OSError:
            continue
    for d in PAGE_CACHE_DIR.iterdir():
        if d.is_dir():
            try:
                next(d.iterdir())
            except StopIteration:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    return removed


def render_page(rel: str, page_no: int, zoom: float = 2.0) -> tuple[bytes, str]:
    """渲染单页预览图,返回 (图片字节, media_type)。

    预览图改用 JPEG(质量 85):扫描件/图片型页面的 PNG 动辄数 MB,
    JPEG 体积和编码时间都低一个量级,2 倍渲染精度下预览清晰度足够;
    纯文字页两者体积接近,JPEG 也无感知劣化。返回类型随实际编码回退。
    """
    src = _resolve(rel)
    bucket = page_cache_bucket(src)
    key = hashlib.sha1(
        f"{src.stat().st_mtime_ns}|{page_no}|{zoom}".encode()
    ).hexdigest()[:24]
    cache = bucket / f"{key}.jpg"
    if cache.exists():
        return cache.read_bytes(), "image/jpeg"
    doc = pymupdf.open(src)
    try:
        page = doc.load_page(page_no - 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        try:
            data = pix.tobytes("jpg", jpg_quality=85)
            media = "image/jpeg"
        except Exception:  # 个别色彩空间的 pixmap 编不了 JPEG,回退 PNG
            data = pix.tobytes("png")
            cache = bucket / f"{key}.png"
            media = "image/png"
    finally:
        doc.close()
    bucket.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data, media


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


def bake_page_rotation(pdf_path: Path, task_id: str) -> tuple[Path, int]:
    """烘焙 /Rotate 旋转页, 返回 (供内核翻译的文件路径, 烘焙页数)。

    BabelDOC 内核对带 /Rotate 标记的横向页(常见于横排大表格)重排错乱:
    保留旋转标记但内容坐标按未旋转处理, 导致译文挤压、竖排乱流。
    翻译前用 pymupdf 把旋转烘进页面内容(去掉 /Rotate, 页面变成无标记
    的真横向页), 内核即可正常解析; 输出文件名用原名保持不变, 工作台
    预览仍用原文件。烘焙副本放 _sidecache/rotbake/<task_id>/ ——
    scan_library 明确跳过 _sidecache, 否则散件扫描会把副本误显示成
    第二条任务卡片; 翻译结束后由 _run 的 finally 清理。
    烘焙失败时退回原文件, 不阻塞翻译。
    """
    try:
        doc = pymupdf.open(pdf_path)
        rotated = sum(1 for p in doc if p.rotation)
        if not rotated:
            doc.close()
            return pdf_path, 0
        for p in doc:
            p.remove_rotation()
        bake_dir = LIBRARY_ROOT / "_sidecache" / "rotbake" / task_id
        bake_dir.mkdir(parents=True, exist_ok=True)
        baked = bake_dir / pdf_path.name
        doc.save(baked, garbage=3, deflate=True)
        doc.close()
        return baked, rotated
    except Exception:
        return pdf_path, 0


def purge_rotbake() -> None:
    """启动兜底: 清掉旋转页烘焙副本目录(见 bake_page_rotation 文档)。

    _run 的 finally 已尽力即时清理, 但 Windows 上内核进程偶仍短暂持有
    副本句柄, rmtree(ignore_errors=True) 会静默失败留下残留。烘焙副本
    只在任务运行期有用, 而本函数在单实例锁之后调用——此刻必然没有
    在跑的任务, 整目录删除是安全的。
    """
    shutil.rmtree(LIBRARY_ROOT / "_sidecache" / "rotbake", ignore_errors=True)


def start_translation(pdf_path: Path, settings: dict) -> str:
    task_id = hashlib.sha1(f"{pdf_path}|{time.time_ns()}".encode()).hexdigest()[:12]
    input_rel = to_rel(pdf_path)
    job = upsert_job_for_input(input_rel, pdf_path.stem)
    entry = {
        "id": task_id,
        "job_id": job["id"],
        "name": pdf_path.stem,
        "status": "queued",
        "progress": 0,
        "stage": "排队中",
        "queue_position": None,
        "detail": "",
        "input": str(pdf_path),
        "mono": None,
        "dual": None,
        "error": None,
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": display_model(settings),
        # 排队参数暂存:_run 启动时由 worker 取出(public_task 已排除本字段)
        "queue": {"pdf": str(pdf_path), "settings": settings},
    }
    TASKS[task_id] = entry
    BUS.publish({"type": "task_update", "task": public_task(entry)})
    _enqueue_translation(task_id)
    return task_id


# ---------------------------------------------------------------------------
# 串行翻译队列:同一时刻只跑一个翻译,其余自动排队(产品约定——用户一次
# 提交多少个 PDF 都逐个处理,避免内存与翻译 API 被并发打爆)。
# ---------------------------------------------------------------------------

_QUEUE: asyncio.Queue | None = None
_WORKER: asyncio.Task | None = None
_QUEUE_ORDER: list[str] = []  # 排队中的 task_id,按入队顺序 → 前端显示"第 N 位"


def _publish_queue_positions() -> None:
    """把当前各排队任务的位次广播出去(出队/取消后位次前移)。"""
    for pos, task_id in enumerate(_QUEUE_ORDER, 1):
        entry = TASKS.get(task_id)
        if entry is None or entry["status"] != "queued":
            continue
        entry["queue_position"] = pos
        BUS.publish({"type": "task_update", "task": public_task(entry)})


def _enqueue_translation(task_id: str) -> None:
    global _QUEUE, _WORKER
    if _QUEUE is None:
        _QUEUE = asyncio.Queue()
    _QUEUE.put_nowait(task_id)
    _QUEUE_ORDER.append(task_id)
    _publish_queue_positions()
    if _WORKER is None or _WORKER.done():
        _WORKER = asyncio.get_running_loop().create_task(_translation_worker())


def _dequeue_order(task_id: str) -> None:
    try:
        _QUEUE_ORDER.remove(task_id)
    except ValueError:
        pass
    _publish_queue_positions()


async def _translation_worker() -> None:
    while True:
        task_id = await _QUEUE.get()
        _dequeue_order(task_id)
        entry = TASKS.get(task_id)
        if entry is None or entry["status"] != "queued":
            continue  # 排队期间被取消的任务,直接跳过
        args = entry.pop("queue", None) or {}
        entry["status"] = "running"
        BUS.publish({"type": "task_update", "task": public_task(entry)})
        # 真正的翻译仍包成独立任务:取消语义与旧版一致(cancel_translation
        # 按 task_id 取消当前这一个,worker 本身不受影响)
        runner = asyncio.get_running_loop().create_task(
            _run(task_id, Path(args.get("pdf", "")), args.get("settings") or {})
        )
        RUNNING[task_id] = runner
        try:
            await runner
        except asyncio.CancelledError:
            pass  # 用户取消:_run 的 CancelledError 分支已收尾
        except Exception:  # noqa: BLE001 — _run 已把错误落到任务上,兜底保证 worker 不死
            logging.getLogger("custom_pdf2zh.engine").exception(
                "translation worker: task %s crashed", task_id
            )


def cancel_translation(task_id: str) -> bool:
    # 还在排队的任务:直接标记取消,worker 取到时按状态跳过
    entry = TASKS.get(task_id)
    if entry is not None and entry["status"] == "queued":
        TASKS.pop(task_id, None)
        _dequeue_order(task_id)
        entry["stage"] = "已取消"
        entry["error"] = "用户取消"
        load_jobs()
        job = next((j for j in JOBS if j["id"] == entry["job_id"]), None)
        if job is not None and (job.get("mono") or job.get("dual")):
            job["status"] = "done"  # 已有成品:取消的只是这次重复翻译,历史不动
        else:
            complete_job(entry["job_id"], None, None, entry["model"], "failed")
        BUS.publish({"type": "task_done", "task": public_task(entry)})
        return True
    task = RUNNING.pop(task_id, None)
    if task is None:
        return False
    task.cancel()
    return True


async def _run(task_id: str, pdf_path: Path, settings: dict) -> None:
    entry = TASKS[task_id]
    log_lines: list[str] = []
    last_sig: tuple | None = None
    out_dir: Path | None = None

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

        # 旋转页烘焙(见 bake_page_rotation 文档): 含 /Rotate 页时改喂烘焙副本。
        # 整本 save 可能数秒,放线程池,避免卡住事件循环(进度推送会冻结)
        pdf_for_kernel, baked_pages = await run_in_threadpool(
            bake_page_rotation, pdf_path, task_id
        )

        # 按所选服务构造官方引擎设置(支持全部 translate_engine_type)
        engine_settings = build_engine_settings(settings)
        sm = SettingsModel(translate_engine_settings=engine_settings)
        sm.basic.input_files = {str(pdf_for_kernel)}
        sm.translation.lang_in = settings.get("lang_in", "en")
        sm.translation.lang_out = settings.get("lang_out", "zh")
        sm.translation.output = str(out_dir)
        sm.pdf.watermark_output_mode = "no_watermark"  # 商用交付:关闭 BabelDOC 水印行
        # 短行拆段:参考文献类区域条目粘连(编号嵌行中)、链接样式错乱
        # (删除线/孤立横线)的修复。实测(WeMM 论文 p3/p11/p17):
        # 参考文献页条目恢复独立成段,正文页与表格页与默认参数逐像素一致,
        # 无回退,故全局默认开启。
        sm.pdf.split_short_lines = True
        # 段落翻译并发:本地引擎一律串行,云服务按用户设置(留空 = 内核默认 4)
        engine_type = engine_settings.model_fields["translate_engine_type"].default
        qps = resolve_qps(settings, engine_type)
        if qps is not None:
            sm.translation.qps = qps
        # 仅输出纯译文:跳过双语对照排版
        if settings.get("no_dual"):
            sm.pdf.no_dual = True
        # 自动术语表提取:仅 LLM 引擎可开;非 LLM 引擎内核已强制关闭,勿覆盖
        supports_llm = _engine_supports_llm(engine_type)
        sm.translation.no_auto_extract_glossary = not (
            supports_llm and bool(settings.get("auto_extract_glossary", False))
        )

        entry["stage"] = "启动翻译内核"
        if baked_pages:
            log_lines.append(f"检测到 {baked_pages} 个旋转页, 已烘焙为横向页后翻译")
        publish_changed()

        async for ev in do_translate_async_stream(sm, pdf_for_kernel):
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
        # 成品拷贝可能上百 MB,放线程池,避免"完成"瞬间卡住所有请求
        exported = await run_in_threadpool(
            export_task, entry["name"], entry["mono"], entry["dual"]
        )
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
        # 内核取消路径只 join(timeout=2),子进程没退出就放任不管——取消的
        # 翻译会变成孤儿进程继续吃 CPU/GPU(把整份文档翻完),拖垮机器。
        # 串行队列保证此刻本进程的存活子进程必是当前任务的翻译进程,终止之。
        for child in multiprocessing.active_children():
            child.terminate()
        # 烘焙副本用完即弃, 防止散件扫描误认(见 bake_page_rotation 文档)
        shutil.rmtree(LIBRARY_ROOT / "_sidecache" / "rotbake" / task_id, ignore_errors=True)
        # 失败/取消的任务没有产物,空输出目录不留垃圾(成功的目录由登记表引用)
        if out_dir is not None and entry["status"] != "done":
            try:
                if not any(out_dir.iterdir()):
                    out_dir.rmdir()
            except OSError:
                pass
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
    invalidate_scan_cache()
    for d in sorted(parent_dirs):
        try:
            next(d.iterdir())
        except StopIteration:
            d.rmdir()
    return n
