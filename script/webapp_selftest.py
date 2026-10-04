"""工作台自测:单元级(engine 直测)+ API 级(起真实服务)+ 可选端到端翻译。

用法(必须用部署 venv 的 python,从仓库任意目录):
    .venv/Scripts/python.exe script/webapp_selftest.py            # 单元 + API
    .venv/Scripts/python.exe script/webapp_selftest.py --e2e      # 再加真实翻译一单
    .venv/Scripts/python.exe script/webapp_selftest.py --e2e --keep  # 测试产物保留在 --keep 给的目录

全部在临时目录沙箱里跑(LIBRARY_ROOT 跟随 cwd),不碰真实 pdf2zh_files。
退出码 0 = 全部通过;任何 FAIL 退出码 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests  # noqa: E402  (部署 venv 自带)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_pdf(title: str, text: str, pages: int = 2) -> bytes:
    """生成测试 PDF:正常文字密度的多段文档(文字太少会被内核
    当成扫描件拒译——那是正确的产品行为,测试件要像真文档)。"""
    import pymupdf

    doc = pymupdf.open()
    filler = " ".join([text] * 12)
    for i in range(pages):
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(72, 72, page.rect.width - 72, 140),
            f"{title} - Page {i + 1}",
            fontsize=18,
        )
        page.insert_textbox(
            pymupdf.Rect(72, 150, page.rect.width - 72, page.rect.height - 72),
            "\n\n".join(
                f"Section {j + 1}. {filler}" for j in range(6)
            ),
            fontsize=11,
        )
    data = doc.tobytes()
    doc.close()
    return data


def make_e2e_pdf() -> bytes | None:
    """端到端用真实论文 PDF 的前 3 页。生成的合成 PDF 会被内核正确地
    判为扫描件拒译(产品行为正常),所以真翻译流程必须用真文档。"""
    import pymupdf

    candidates = sorted(
        Path(r"C:\Users\chenshulin\Downloads\LLM_Papers_1000").glob("*.pdf")
    )
    for cand in candidates:
        try:
            if cand.stat().st_size > 800_000:
                continue
            doc = pymupdf.open(cand)
            if doc.page_count < 2:
                doc.close()
                continue
            doc.select(list(range(min(3, doc.page_count))))
            data = doc.tobytes()
            doc.close()
            return data
        except Exception:
            continue
    return None


def multipart_upload(url: str, filename: str, data: bytes) -> dict:
    r = requests.post(
        url,
        files={"file": (filename, data, "application/pdf")},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Part A:engine 单元级(无服务,cwd = 沙箱)
# ---------------------------------------------------------------------------

def part_a(sandbox: Path) -> None:
    print("\n=== Part A:engine 单元级 ===")
    os.chdir(sandbox)
    import custom_pdf2zh.webapp.engine as engine

    # A1 扫描缓存:TTL 内复用,失效钩子生效
    orig_impl = engine._scan_library_impl
    calls = {"n": 0}

    def counting_impl():
        calls["n"] += 1
        return orig_impl()

    engine._scan_library_impl = counting_impl
    try:
        engine.scan_library()
        engine.scan_library()
        check("A1a TTL 内扫描只执行一次", calls["n"] == 1, f"calls={calls['n']}")
        engine.invalidate_scan_cache()
        engine.scan_library()
        check("A1b 失效后重新扫描", calls["n"] == 2, f"calls={calls['n']}")
    finally:
        engine._scan_library_impl = orig_impl
        engine.invalidate_scan_cache()

    # A2 上传哈希去重
    pdf_a = make_pdf("Doc A", "The quick brown fox jumps over the lazy dog.")
    up1 = engine._store_upload("a.pdf", pdf_a)
    up2 = engine._store_upload("a-again.pdf", pdf_a)
    up3 = engine._store_upload("b.pdf", make_pdf("Doc B", "Completely different content."))
    check("A2a 相同内容复用同一文件", up2["uploaded"] == up1["uploaded"] and up2["dedup"] is True, str(up2))
    check("A2b 不同内容各存一份", up3["uploaded"] != up1["uploaded"] and up3["dedup"] is False, str(up3))
    # 删除被复用的文件后,同内容再上传要能重新落盘(索引失效自愈)
    (engine.LIBRARY_ROOT / up1["uploaded"]).unlink()
    up4 = engine._store_upload("a-retry.pdf", pdf_a)
    check("A2c 文件丢失后哈希索引自愈", up4["dedup"] is False and (engine.LIBRARY_ROOT / up4["uploaded"]).exists(), str(up4))

    # A3 重译成功清理旧一轮产物 + 幽灵登记一并移除
    job = {
        "id": "testjob", "name": "Doc A", "input": up4["uploaded"], "status": "running",
        "time": "2026-10-04 00:00", "mono": "webapp-old/Doc A.zh.mono.pdf",
        "dual": "webapp-old/Doc A.zh.dual.pdf", "model": "m",
    }
    ghost = {
        "id": "ghost1", "name": "Doc A", "input": None, "status": "done",
        "time": "2026-10-04 00:00", "mono": "webapp-old/Doc A.zh.mono.pdf", "dual": None, "model": None,
    }
    engine.JOBS[:] = [job, ghost]
    engine._jobs_loaded = True
    old_dir = engine.LIBRARY_ROOT / "webapp-old"
    old_dir.mkdir(exist_ok=True)
    (old_dir / "Doc A.zh.mono.pdf").write_bytes(pdf_a)
    (old_dir / "Doc A.zh.dual.pdf").write_bytes(pdf_a)
    engine.complete_job("testjob", "webapp-new/Doc A.zh.mono.pdf", "webapp-new/Doc A.zh.dual.pdf", "m", "done")
    check("A3a 旧产物文件被删除", not (old_dir / "Doc A.zh.mono.pdf").exists())
    check("A3b 旧会话目录被清空", not old_dir.exists())
    check("A3c 幽灵登记被移除", all(j["id"] != "ghost1" for j in engine.JOBS))
    check("A3d 新指针落位", next(j for j in engine.JOBS if j["id"] == "testjob")["mono"] == "webapp-new/Doc A.zh.mono.pdf")

    # A3- 失败路径:失败不动旧成品,且有成品的历史不被失败污染
    keep_dir = engine.LIBRARY_ROOT / "webapp-keep"
    keep_dir.mkdir(exist_ok=True)
    (keep_dir / "Doc B.zh.mono.pdf").write_bytes(pdf_a)
    job2 = {
        "id": "testjob2", "name": "Doc B", "input": up3["uploaded"], "status": "running",
        "time": "2026-10-04 00:00", "mono": "webapp-keep/Doc B.zh.mono.pdf", "dual": None, "model": "m",
    }
    engine.JOBS.append(job2)
    engine.complete_job("testjob2", None, None, "m", "failed")
    check("A3e 失败时旧成品保留", (keep_dir / "Doc B.zh.mono.pdf").exists())
    check("A3f 有成品的历史不被失败污染", job2["status"] == "done", job2["status"])
    job3 = {
        "id": "testjob3", "name": "Doc C", "input": None, "status": "running",
        "time": "2026-10-04 00:00", "mono": None, "dual": None, "model": "m",
    }
    engine.JOBS.append(job3)
    engine.complete_job("testjob3", None, None, "m", "failed")
    check("A3g 无成品的失败落为 failed", job3["status"] == "failed", job3["status"])

    # A4 同名孤儿产物不再收编成重复卡
    orphan_dir = engine.LIBRARY_ROOT / "webapp-legacy"
    orphan_dir.mkdir(exist_ok=True)
    orphan = orphan_dir / "Doc A.zh.mono.pdf"
    orphan.write_bytes(pdf_a)
    old_ts = time.time() - 86400  # 绕过 600 秒进行中豁免
    os.utime(orphan, (old_ts, old_ts))
    before = len(engine.JOBS)
    engine._rebuild_orphan_jobs()
    check("A4 同名任务已登记时不再收编", len(engine.JOBS) == before and not any(j["input"] is None and j["name"] == "Doc A" for j in engine.JOBS))

    # A5 页面缓存 LRU 清理
    cache_dir = engine.PAGE_CACHE_DIR
    bucket = cache_dir / "bucket-x"
    bucket.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(5):
        f = bucket / f"p{i}.jpg"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (time.time() - 1000 + i, time.time() - 1000 + i))
        files.append(f)
    removed = engine.prune_page_cache(max_bytes=2500)
    left = [f for f in files if f.exists()]
    check("A5a 超限删除最旧的", removed == 3 and len(left) == 2, f"removed={removed} left={len(left)}")
    check("A5b 未超限不动", engine.prune_page_cache(max_bytes=10**9) == 0)

    # A6 预览图渲染:JPEG 编码
    (engine.LIBRARY_ROOT / up4["uploaded"]).write_bytes(pdf_a)
    data, media = engine.render_page(up4["uploaded"], 1)
    check("A6 预览图为 JPEG", media == "image/jpeg" and data[:2] == b"\xff\xd8", f"{media} head={data[:2]!r}")

    # A7 本地引擎一律串行:qps 强制规则
    check("A7a 本地引擎强制 qps=1", engine.resolve_qps({"qps": 8}, "Ollama") == 1)
    check("A7b 本地引擎无视非法值", engine.resolve_qps({"qps": "abc"}, "Ollama") == 1)
    check("A7c 云引擎按用户设置", engine.resolve_qps({"qps": 8}, "SiliconFlow") == 8)
    check("A7d 云引擎留空走默认", engine.resolve_qps({"qps": None}, "SiliconFlow") is None)
    check("A7e 云引擎字符串数字", engine.resolve_qps({"qps": "16"}, "SiliconFlow") == 16)

    # A8 设置保存语义:面板替换式(以表单全集为准,清空即删),翻译合并式(缺失不碰)
    engine.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if engine.SETTINGS_FILE.exists():
        engine.SETTINGS_FILE.unlink()
    s = engine.save_settings({"engine": "Ollama", "engine_fields": {"a_key": "v1", "b_key": "v2"}},
                             full_replace=True)
    check("A8a 面板保存写入两字段", s["engine_fields"].get("a_key") == "v1" and s["engine_fields"].get("b_key") == "v2")
    # 真实前端总是发全集:清空 a_key 时 b_key 会原样带上
    s = engine.save_settings({"engine": "Ollama", "engine_fields": {"a_key": "", "b_key": "v2"}},
                             full_replace=True)
    check("A8b 面板清空字段即删除", "a_key" not in s["engine_fields"] and s["engine_fields"].get("b_key") == "v2")
    s = engine.save_settings({"engine": "Ollama", "engine_fields": {"c_key": "v3"}})
    check("A8c 合并式不碰缺失字段", s["engine_fields"].get("b_key") == "v2" and s["engine_fields"].get("c_key") == "v3")
    engine.SETTINGS_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Part B:API 级(起真实服务子进程,cwd = 沙箱)
# ---------------------------------------------------------------------------

def wait_ready(base: str, proc: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early: {proc.returncode}")
        try:
            requests.get(base + "/api/settings", timeout=2)
            return
        except requests.RequestException:
            time.sleep(0.4)
    raise RuntimeError("server not ready in time")


def wait_task(base: str, task_id: str, name: str, timeout: float) -> dict:
    """轮询等待任务收尾。运行/排队中卡片带 task_id;结束后任务从内存清理,
    只剩历史卡片(status done/failed),据此判定。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            data = requests.get(base + "/api/tasks", timeout=30).json()
        except requests.RequestException as e:  # 服务偶发卡顿:重试继续等
            print(f"        (wait_task poll error: {e.__class__.__name__})", flush=True)
            time.sleep(2)
            continue
        for t in data["running"]:
            if t.get("task_id") == task_id and t.get("status") in ("done", "failed"):
                return t
        for t in data["tasks"]:
            last = t
            if t.get("task_id") == task_id and t.get("status") == "failed":
                return t
            if t.get("name") == name and not t.get("task_id") and t.get("status") in ("done", "failed"):
                return t
        time.sleep(2)
    raise TimeoutError(f"task {task_id} not finished in {timeout}s, last={last}")


def part_b(sandbox: Path, e2e: bool) -> None:
    print("\n=== Part B:API 级 ===")
    port = 7871
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PDF2ZH_NO_BROWSER"] = "1"
    venv_python = REPO_ROOT / "pdf2zh/kernel/PDFMathTranslate-next.git/.venv/Scripts/python.exe"
    # 服务日志直写文件:stdout 用 PIPE 且无人消费时,日志洪水(如坏引擎
    # 的报错循环)会写满管道缓冲,logging 阻塞服务事件循环,整个服务假死
    server_log = sandbox / "server.log"
    log_fp = open(server_log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(venv_python), "-m", "custom_pdf2zh.webapp.server", str(port)],
        cwd=sandbox, env=env,
        stdout=log_fp, stderr=subprocess.STDOUT,
    )
    try:
        wait_ready(base, proc)
        check("B0 服务在沙箱 cwd 启动", True)

        # B1 基础端点
        html = requests.get(base + "/", timeout=10).text
        check("B1a 首页可访问", "PDF 翻译工作台" in html and "app.mjs?v=" in html)
        st = requests.get(base + "/api/settings", timeout=10).json()
        check("B1b 设置含新字段(qps/no_dual)", "qps" in st and "no_dual" in st, str(st)[:200])
        r = requests.post(base + "/api/settings", json={
            "engine": "Ollama", "engine_fields": {}, "lang_in": "en", "lang_out": "zh",
            "auto_extract_glossary": False, "no_dual": True, "qps": 8,
        }, timeout=10).json()
        check("B1c 设置保存 qps/no_dual", r.get("qps") == 8 and r.get("no_dual") is True, str(r)[:200])
        r = requests.post(base + "/api/settings", json={"engine": "Ollama", "engine_fields": {}, "qps": 0},
                          timeout=10).json()
        check("B1d qps 置 0 归位 None", r.get("qps") is None, str(r.get("qps")))
        requests.post(base + "/api/settings", json={"no_dual": False}, timeout=10)

        # B2 上传去重 + 预览图
        pdf_a = make_pdf("Doc A", "The quick brown fox jumps over the lazy dog.")
        pdf_b = make_pdf("Doc B", "Completely different content for queue tests.")
        up1 = multipart_upload(base + "/api/upload", "Doc A.pdf", pdf_a)
        up2 = multipart_upload(base + "/api/upload", "Doc A.pdf", pdf_a)
        check("B2a 相同内容上传去重", up2["uploaded"] == up1["uploaded"] and up2.get("dedup") is True, str(up2))
        up3 = multipart_upload(base + "/api/upload", "Doc B.pdf", pdf_b)
        info = requests.get(base + "/api/pdf-info", params={"file": up1["uploaded"]}, timeout=10).json()
        check("B2b pdf-info 页数正确", info["pages"] == 2, str(info))
        pg = requests.get(base + "/api/page", params={"file": up1["uploaded"], "page": 1}, timeout=30)
        check("B2c 预览图为 JPEG", pg.headers.get("content-type", "").startswith("image/jpeg") and pg.content[:2] == b"\xff\xd8",
              pg.headers.get("content-type", "?"))
        pg2 = requests.get(base + "/api/page", params={"file": up1["uploaded"], "page": 1}, timeout=30)
        check("B2d 二次请求走缓存(同尺寸)", len(pg2.content) == len(pg.content))

        # B3 任务列表出现待翻译卡片
        tasks = requests.get(base + "/api/tasks", timeout=10).json()["tasks"]
        card = next((t for t in tasks if t["name"] == "Doc A"), None)
        check("B3 上传后出现待翻译卡片", card is not None and card["status"] == "pending", str(card)[:150])

        # B4 ollama-models:Ollama 在跑应有列表;坏 host 快速返回空
        models = requests.get(base + "/api/ollama-models", timeout=10).json()["models"]
        print(f"        (本机 Ollama 模型: {models})")
        requests.post(base + "/api/settings", json={"engine_fields": {"ollama_host": "http://127.0.0.1:9"}},
                      timeout=10)
        t0 = time.time()
        empty = requests.get(base + "/api/ollama-models", timeout=15).json()["models"]
        dt = time.time() - t0
        check("B4 Ollama 不可达快速返回空(不阻塞)", empty == [] and dt < 6, f"dt={dt:.1f}s models={empty}")
        requests.post(base + "/api/settings", json={"engine_fields": {"ollama_host": "http://localhost:11434"}},
                      timeout=10)

        # B8 本地引擎翻译时 qps 归空(固定串行语义,并发数不持久化)。
        # 用坏 host 的任务验证保存语义即可,不等它失败重试完,立即取消,
        # 避免占用串行队列挤乱 B5 的位次断言
        b8 = requests.post(base + "/api/translate", json={
            "path": up1["uploaded"], "engine": "Ollama",
            "engine_fields": {"ollama_host": "http://127.0.0.1:9", "ollama_model": "x"},
            "lang_in": "en", "lang_out": "zh", "qps": 8,
        }, timeout=30).json()["task_id"]
        st2 = requests.get(base + "/api/settings", timeout=10).json()
        check("B8 本地引擎翻译时 qps 归空", st2.get("qps") is None, str(st2.get("qps")))
        requests.post(base + "/api/cancel", json={"task_id": b8}, timeout=10)
        deadline = time.time() + 60
        while time.time() < deadline:
            d = requests.get(base + "/api/tasks", timeout=10).json()
            busy = sum(1 for t in d["running"]) + sum(
                1 for t in d["tasks"] if t.get("running") or t.get("queued")
            )
            if busy == 0:
                break
            time.sleep(1)

        if not e2e:
            print("\n(未指定 --e2e,跳过真实翻译流程测试)")
            return

        # B5 端到端:SiliconFlow 翻译真实论文(前 3 页),验证排队位次/取消/孤儿清理
        api_key = os.environ.get("SILICONFLOW_API_KEY")
        real = make_e2e_pdf()
        if not api_key:
            print("  SKIP e2e: 未设置 SILICONFLOW_API_KEY")
            return
        if not real:
            print("  SKIP e2e: 找不到可用的真实 PDF 测试件")
            return
        pdf_c = make_pdf("Doc C", "Third document for queue position checks.")
        up4 = multipart_upload(base + "/api/upload", "Doc C.pdf", pdf_c)
        up_real = multipart_upload(base + "/api/upload", "Paper.pdf", real)
        body = {
            "path": up_real["uploaded"],
            "engine": "SiliconFlow",
            "engine_fields": {"siliconflow_api_key": api_key, "siliconflow_model": "tencent/Hunyuan-MT-7B"},
            "lang_in": "en", "lang_out": "zh",
            "auto_extract_glossary": False, "no_dual": False, "qps": 8,
        }
        t1 = requests.post(base + "/api/translate", json=body, timeout=30).json()["task_id"]
        t2 = requests.post(base + "/api/translate", json={**body, "path": up3["uploaded"]}, timeout=30).json()["task_id"]
        t3 = requests.post(base + "/api/translate", json={**body, "path": up4["uploaded"]}, timeout=30).json()["task_id"]

        # B5a 排队位次:t1 出队后 t2=1、t3=2(不同文档,各自独立卡片)
        pos = {}
        deadline = time.time() + 30
        while time.time() < deadline:
            data = requests.get(base + "/api/tasks", timeout=10).json()
            loose = {t["id"]: t for t in data["running"]}
            for t in data["tasks"]:
                if t.get("task_id") in (t2, t3) and t.get("queued"):
                    loose[t["task_id"]] = t
            if t2 in loose and t3 in loose:
                pos = {t2: loose[t2].get("queue_position"), t3: loose[t3].get("queue_position")}
                break
            time.sleep(1)
        check("B5a 排队位次正确(1,2)", pos.get(t2) == 1 and pos.get(t3) == 2, str(pos))

        # B5b 取消排队的两个任务
        c2 = requests.post(base + "/api/cancel", json={"task_id": t2}, timeout=10).json()["cancelled"]
        c3 = requests.post(base + "/api/cancel", json={"task_id": t3}, timeout=10).json()["cancelled"]
        check("B5b 取消排队任务", c2 and c3)

        # B5c 等第一单完成(3 页真实论文,云引擎 10 分钟内)
        res = wait_task(base, t1, "Paper", timeout=600)
        check("B5c 翻译完成", res.get("status") == "done", str(res)[:200])

        data = requests.get(base + "/api/tasks", timeout=10).json()
        card = next((t for t in data["tasks"] if t["name"] == "Paper"), None)
        check("B5d 卡片有 mono/dual 产物", bool(card and card.get("mono") and card.get("dual")), str(card)[:200])
        exports = list((sandbox / "pdf2zh_files" / "_exports").glob("Paper*"))
        check("B5e 成品导出到成品文件夹", len(exports) >= 2, str(exports))
        webapp_dirs = [d for d in (sandbox / "pdf2zh_files").glob("webapp-*")]

        # B5f 重译同一文件:完成后旧会话目录应被清理,不新增卡片
        t4 = requests.post(base + "/api/translate", json=body, timeout=30).json()["task_id"]
        res4 = wait_task(base, t4, "Paper", timeout=600)
        check("B5f 重译完成", res4.get("status") == "done", str(res4)[:200])
        webapp_dirs_after = [d for d in (sandbox / "pdf2zh_files").glob("webapp-*")]
        check("B5g 重译后旧会话目录被清理", len(webapp_dirs_after) <= max(1, len(webapp_dirs)),
              f"before={len(webapp_dirs)} after={len(webapp_dirs_after)}")
        data = requests.get(base + "/api/tasks", timeout=10).json()
        n_cards = sum(1 for t in data["tasks"] if t["name"] == "Paper")
        check("B5h 同文档仍然只有一张卡", n_cards == 1, f"cards={n_cards}")
        exports_after = list((sandbox / "pdf2zh_files" / "_exports").glob("Paper*"))
        check("B5i 成品文件夹不重复堆积", len(exports_after) == len(exports), f"{len(exports)}→{len(exports_after)}")

        # B6 对照视图(走线程池路径)
        card = next(t for t in requests.get(base + "/api/tasks", timeout=10).json()["tasks"] if t["name"] == "Paper")
        sbs = requests.post(base + "/api/side-by-side", json={"path": card["dual"]}, timeout=120).json()
        check("B6a 左右对照生成", bool(sbs.get("path")) and (sandbox / "pdf2zh_files" / sbs["path"]).exists(), str(sbs))
        pv1 = requests.post(base + "/api/prepare-view", json={"path": card["dual"], "which": "原文"}, timeout=120).json()
        pv2 = requests.post(base + "/api/prepare-view", json={"path": card["dual"], "which": "译文"}, timeout=120).json()
        check("B6b 原文/译文视图提取", bool(pv1.get("path")) and bool(pv2.get("path")), f"{pv1} {pv2}")

        # B7 删除:文件/登记/成品一起清
        del_body = {"paths": [card["original"], card["mono"], card["dual"], sbs["path"], pv1["path"], pv2["path"]],
                    "job_ids": [card["id"]]}
        n = requests.post(base + "/api/delete", json=del_body, timeout=30).json()["deleted"]
        tasks = requests.get(base + "/api/tasks", timeout=10).json()["tasks"]
        check("B7 删除生效且列表即时反映", n >= 3 and not any(t["name"] == "Paper" for t in tasks), f"deleted={n}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        try:
            log_fp.close()
            tail = "\n".join(server_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-15:])
            if tail:
                print(f"--- server log tail ---\n{tail}")
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2e", action="store_true", help="包含真实翻译端到端(需要可用的翻译服务)")
    ap.add_argument("--keep", help="保留沙箱目录到指定路径(调试用)")
    args = ap.parse_args()

    sandbox = Path(tempfile.mkdtemp(prefix="pdf2zh_selftest_"))
    print(f"沙箱: {sandbox}")
    try:
        (sandbox / "unit").mkdir()
        (sandbox / "api").mkdir()
        part_a(sandbox / "unit")
        part_b(sandbox / "api", args.e2e)
    finally:
        if args.keep:
            shutil.copytree(sandbox, args.keep, dirs_exist_ok=True)
            print(f"沙箱已保留: {args.keep}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
    print(f"\n===== 结果: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
