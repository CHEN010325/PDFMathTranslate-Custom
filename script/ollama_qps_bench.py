"""本地 Ollama 翻译并发对照实验:qps=1 vs qps=8,相近文本量单页计时。

前提:本机 Ollama 运行中,含 s2021008840/hy-mt2:7b-q4_k_m。
流程:起沙箱服务 → 预热一单(模型加载)→ A 组 qps=1 → B 组 qps=8 → 输出耗时。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pymupdf  # noqa: E402
import requests  # noqa: E402

PORT = 7873
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "s2021008840/hy-mt2:7b-q4_k_m"
SRC = next(
    p
    for p in sorted(Path(r"C:\Users\chenshulin\Downloads\LLM_Papers_1000").glob("*.pdf"))
    if p.stat().st_size < 800_000
)


def page_pdf(page_idx: int) -> bytes:
    doc = pymupdf.open(SRC)
    doc.select([page_idx])
    data = doc.tobytes()
    doc.close()
    return data


def run_case(label: str, page_idx: int, qps: int, sandbox: Path) -> float:
    """翻译一单并返回从提交到完成的墙钟秒数。"""
    doc = pymupdf.open(SRC)
    chars = len(doc[page_idx].get_text())
    doc.close()
    data = page_pdf(page_idx)
    up = requests.post(
        f"{BASE}/api/upload", files={"file": (f"q{qps}-{label}.pdf", data)}, timeout=60
    ).json()
    body = {
        "path": up["uploaded"],
        "engine": "Ollama",
        "engine_fields": {"ollama_host": "http://localhost:11434", "ollama_model": MODEL},
        "lang_in": "en",
        "lang_out": "zh",
        "auto_extract_glossary": False,
        "no_dual": False,
        "qps": qps,
    }
    requests.post(f"{BASE}/api/translate", json=body, timeout=30)
    name = up["name"]
    t0 = time.time()
    while time.time() - t0 < 1200:
        d = requests.get(f"{BASE}/api/tasks", timeout=10).json()
        for t in d["tasks"]:
            if t.get("name") == name and not t.get("task_id") and t.get("status") in ("done", "failed"):
                dt = time.time() - t0
                print(f"{label}: qps={qps} page={page_idx + 1}({chars} chars) -> {t['status']} in {dt:.0f}s", flush=True)
                if t["status"] != "done":
                    raise RuntimeError(f"case {label} failed: {t}")
                return dt
        time.sleep(2)
    raise TimeoutError(label)


def main() -> None:
    sandbox = Path(tempfile.mkdtemp(prefix="pdf2zh_speed_"))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PDF2ZH_NO_BROWSER"] = "1"
    venv_python = REPO_ROOT / "pdf2zh/kernel/PDFMathTranslate-next.git/.venv/Scripts/python.exe"
    proc = subprocess.Popen(
        [str(venv_python), "-m", "custom_pdf2zh.webapp.server", str(PORT)],
        cwd=sandbox, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/settings", timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.5)
        print("server ready", flush=True)

        print("=== 预热(Ollama 模型加载,不计时)===", flush=True)
        run_case("warmup", 1, 4, sandbox)
        print("=== 对照实验 ===", flush=True)
        dt1 = run_case("A-serial", 5, 1, sandbox)   # 论文第 6 页, qps=1
        dt8 = run_case("B-parallel", 6, 8, sandbox) # 论文第 7 页, qps=8
        print(f"\n===== 结果: qps=1 用时 {dt1:.0f}s | qps=8 用时 {dt8:.0f}s | 加速比 {dt1 / dt8:.2f}x =====", flush=True)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
