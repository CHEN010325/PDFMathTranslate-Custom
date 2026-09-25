"""PDF 翻译工作台 — FastAPI 服务(照抄 mineru-pdf-translate 的 UI,包 pdf2zh-next 引擎)。

启动:在 PDFMathTranslate-next.git 目录下运行
    .venv/Scripts/python.exe -m custom_pdf2zh.webapp.server
(桌面 PDF翻译.bat 已配置好)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import engine

app = FastAPI(title="PDF 翻译工作台")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_RENDER_SEM = asyncio.Semaphore(4)


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


class NoCacheStaticFiles(StaticFiles):
    """静态资源禁用启发式缓存,保证前端改动刷新即生效。

    另:强制 .js/.mjs 的 Content-Type 为 text/javascript —— module 脚本
    对 MIME 强校验, 而 Windows 上 Python 的 mimetypes 会读注册表, 个别
    客户机注册表把 .mjs 注册成错误类型时, 前端模块会被浏览器整体拒绝
    执行, 页面永远停留在初始态("引擎检测中…")。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        full_path = str(args[0]).lower() if args else ""
        if full_path.endswith((".mjs", ".js")):
            resp.headers["content-type"] = "text/javascript; charset=utf-8"
        return resp


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

# 启动回填/清理挪到 main()(拿到单实例锁之后)执行:
# 若放在模块导入期,第二个实例被误启动时会在翻译进行中抢跑回填,
# 把内核刚写出的会话半成品当"无主产物"导出成重复文件,再因端口
# 被占而退出 —— 单实例锁保证回填只有一个进程会做。


# ---------------------------------------------------------------------------
# 历史与文件
# ---------------------------------------------------------------------------


@app.get("/api/tasks")
def list_tasks() -> dict:
    entries = engine.scan_library()
    loose_running = []
    for t in engine.TASKS.values():
        if t["status"] != "running":
            continue
        # 运行状态叠到同一条登记上,避免同文档出现两张卡片
        hit = next(
            (e for e in entries if e.get("id") and e["id"] == t.get("job_id")), None
        )
        if hit is None and t.get("input"):
            inp = str(Path(t["input"]).resolve()).lower()
            hit = next(
                (
                    e
                    for e in entries
                    if e.get("original")
                    and str((engine.LIBRARY_ROOT / e["original"]).resolve()).lower() == inp
                ),
                None,
            )
        payload = engine.public_task(t)
        if hit is not None:
            hit["running"] = True
            hit["task_id"] = t["id"]
            hit["progress"] = payload["progress"]
            hit["stage"] = payload["stage"]
            hit["status"] = "running"
        else:
            payload["running"] = True
            loose_running.append(payload)
    return {"tasks": entries, "running": loose_running}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    engine.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = engine.UPLOAD_DIR / Path(file.filename).name
    # 防重名:同名加序号
    n = 2
    while dest.exists():
        dest = engine.UPLOAD_DIR / f"{dest.stem}-{n}{dest.suffix}"
        n += 1
    dest.write_bytes(await file.read())
    return {"uploaded": engine.to_rel(dest), "name": dest.stem}


@app.get("/api/pdf-info")
def pdf_info(file: str) -> dict:
    try:
        return engine.pdf_info(file)
    except Exception as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/page")
async def page(file: str, page: int, zoom: float = 2.0) -> Response:
    from starlette.concurrency import run_in_threadpool

    async with _RENDER_SEM:  # 大 PDF 并发渲染限流,避免请求风暴
        try:
            data = await run_in_threadpool(engine.render_page, file, page, zoom)
        except Exception as exc:
            raise HTTPException(404, str(exc)) from exc
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/api/file")
def serve_file(path: str, download: int = 0) -> FileResponse:
    try:
        p = engine._resolve(path)
    except Exception as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        p,
        filename=p.name if download else None,
        media_type="application/pdf",
    )


@app.post("/api/side-by-side")
async def side_by_side(request: Request) -> dict:
    body = await request.json()
    try:
        rel = engine.make_side_by_side(body["path"])
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"path": rel, "url": f"/api/file?path={quote(rel)}"}


@app.post("/api/prepare-view")
async def prepare_view(request: Request) -> dict:
    """which ∈ 原文 / 译文 / 原样。返回可直接预览的库内相对路径。"""
    body = await request.json()
    try:
        which = body.get("which", "原样")
        if which == "原样":
            rel = body["path"]
            engine._resolve(rel)  # 校验存在
        else:
            rel = engine.extract_view(body["path"], which)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"path": rel}


@app.post("/api/delete")
async def delete_files(request: Request) -> dict:
    body = await request.json()
    n = engine.delete_entries(body.get("paths", []), body.get("job_ids", []))
    return {"deleted": n}


@app.post("/api/cancel")
async def cancel(request: Request) -> dict:
    body = await request.json()
    ok = engine.cancel_translation(body.get("task_id", ""))
    return {"cancelled": ok}


@app.post("/api/shutdown")
async def shutdown(request: Request) -> dict:
    """优雅关闭工作台服务。

    有任务在跑时默认拒绝;force=true 会先取消任务再退出。
    状态都已落盘(登记表/设置),停机不丢数据。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    running = list(engine.RUNNING)
    if running and not body.get("force"):
        return {
            "ok": False,
            "running": len(running),
            "message": f"有 {len(running)} 个任务正在运行，无法关闭",
        }
    for task_id in running:
        engine.cancel_translation(task_id)

    async def _exit_later():
        await asyncio.sleep(0.3)  # 先让本响应送达浏览器
        if _server is not None:
            _server.should_exit = True
        else:
            os._exit(0)

    asyncio.create_task(_exit_later())
    return {"ok": True}


@app.post("/api/open-library")
def open_library() -> dict:
    engine.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer", str(engine.EXPORT_DIR)])
    return {"ok": True}


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


@app.get("/api/settings")
def get_settings() -> dict:
    return engine.load_settings()


@app.post("/api/settings")
async def set_settings(request: Request) -> dict:
    body = await request.json()
    return engine.save_settings(body)


@app.get("/api/langs")
def langs() -> dict:
    return engine.LANGS


@app.get("/api/services")
def services() -> dict:
    """全部支持的翻译服务及其表单字段定义(与官方 pdf2zh-next 对齐)。"""
    return {"services": engine.service_registry(), "groups": engine.GROUP_LABELS}


@app.get("/api/ollama-models")
async def ollama_models() -> dict:
    """读取本地 Ollama 已装模型,失败时返回空列表(前端回退为手填)。

    Ollama 对无标签名默认按 :latest 处理,这里统一剥掉后缀避免下拉框重名。
    """
    import urllib.request

    host = engine.load_settings().get("engine_fields", {}).get(
        "ollama_host", "http://localhost:11434"
    )
    try:
        req = urllib.request.Request(
            host.rstrip("/") + "/api/tags", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        seen: dict[str, None] = {}
        for m in data.get("models", []):
            name = re.sub(r":latest$", "", m["name"], flags=re.IGNORECASE)
            seen.setdefault(name, None)
        return {"models": list(seen)}
    except Exception:
        return {"models": []}


# ---------------------------------------------------------------------------
# 翻译
# ---------------------------------------------------------------------------


@app.post("/api/translate")
async def translate(request: Request) -> dict:
    body = await request.json()
    pdf_path = engine._resolve(body["path"])
    settings = engine.load_settings()
    for key in ("engine", "lang_in", "lang_out"):
        if body.get(key):
            settings[key] = body[key]
    if "auto_extract_glossary" in body:
        settings["auto_extract_glossary"] = bool(body["auto_extract_glossary"])
    if isinstance(body.get("engine_fields"), dict):
        for key, value in body["engine_fields"].items():
            if value is None or (isinstance(value, str) and not value.strip()):
                settings["engine_fields"].pop(key, None)
            else:
                settings["engine_fields"][key] = value
    task_id = engine.start_translation(pdf_path, settings)
    return {"task_id": task_id}


@app.get("/api/events")
async def events(request: Request) -> EventSourceResponse:
    q = engine.BUS.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield {"event": ev.get("type", "message"), "data": json.dumps(ev, ensure_ascii=False)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            engine.BUS.unsubscribe(q)

    return EventSourceResponse(gen())


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

# uvicorn.Server 实例,/api/shutdown 通过它触发优雅退出
_server = None


_lock_fp = None  # 持有锁文件句柄;局部变量会被 GC 关闭导致锁失效


def _acquire_single_instance_lock() -> bool:
    """单实例守卫:独占锁定 _server.lock,进程存活期间持有(崩溃由 OS 释放)。

    不管用户从桌面图标、bat 还是别的入口再启动一个实例,拿不到锁的
    都会立即退出,避免两个进程竞争 _jobs.json / 成品文件夹。
    """
    global _lock_fp
    engine.LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    fp = open(engine.LIBRARY_ROOT / "_server.lock", "a+")
    try:
        fp.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.close()
        return False
    _lock_fp = fp  # 进程存活期间保持引用,锁不释放
    return True


def main() -> None:
    global _server
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7860
    url = f"http://127.0.0.1:{port}"
    if not _acquire_single_instance_lock():
        print(f"PDF 翻译工作台已在运行: {url}", flush=True)
        webbrowser.open(url)
        return
    # 启动回填:历史已完成任务的成品补齐到成品文件夹
    engine.ensure_all_exports()
    # 启动清理:旧版平铺页面缓存 + 源文件已消失的派生视图缓存
    engine.purge_orphan_cache()
    print(f"PDF 翻译工作台: {url}", flush=True)
    threading_timer(port, url)
    # SSE 是长连接,优雅停机最多等 3 秒就强制断开,保证"关闭服务"能退干净
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=3,
    )
    _server = uvicorn.Server(config)
    _server.run()


def threading_timer(port: int, url: str) -> None:
    import threading

    def _open():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    main()
