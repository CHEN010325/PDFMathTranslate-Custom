# Local Modifications

This repository carries local customizations on top of
[Byaidu/PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate).
Rebase this branch onto `upstream/main` after pulling updates, then re-check
the items below (upstream changes may conflict or make a patch obsolete).

## 2026-09-25 — 工作台第二轮:去水印 / 页锚定同步 / 静态缓存治理 / 默认模型定档

- **默认翻译模型定档** `s2021008840/hy-mt2:7b-q4_k_m`:官方技术报告
  (arXiv:2605.22064)Table 5 实测 Q4_K_M 相比 BF16 仅 -0.16 XCOMET,
  速度/显存更优;工作台设置、engine.py 默认值、批量脚本三处一致。
- **关闭 BabelDOC 水印行**:`engine.py` 设
  `sm.pdf.watermark_output_mode = "no_watermark"`(正式配置项),
  此后所有翻译产物(单语/对照)不含"本文档由 funstory.ai…"横幅;
  存量交付文件用 pymupdf 按行矩形 redaction 精确抹除(正文零误伤)。
  注意:no_watermark 模式下输出文件名带 `.no_watermark.` 中缀,
  库扫描的 `_normalize` 分组规则已天然兼容。
- **双栏同步滚动修复**:原实现把滚动监听绑在外层 viewer 上,而实际滚动
  发生在内层 `.pdf-page-list`(#source-preview/#translated-preview),
  导致左栏滚动不触发同步。现绑定内层列表,并将"总高度比例同步"升级为
  **按页锚定同步**(第 N 页原文 ↔ 第 N 页译文 + 页内分数位置;两侧页数
  不一致时按比例映射页序号;90ms driver-lock 消除程序滚动回声)。
- **静态资源缓存治理**:浏览器曾长期缓存旧版 app.mjs(transferSize=0)
  导致前端改动不生效。`server.py` 增加 `NoCacheStaticFiles`
  (Cache-Control: no-cache),`index.html` 以 `app.mjs?v=2` 引用;
  **今后每次修改前端 JS/CSS 需递增该版本号**(或依赖 no-cache 头的强制复验)。
- 本地 Ollama 模型精简:仅保留 `hy-mt2:7b-q4_k_m`(默认)与 qwen/gemma 通用
  备选;已发布的 `s2021008840/hy-mt2` 全部 9 个标签在 ollama.com 上不受影响,
  `ollama pull` 随时可找回。

## 2026-09-24 — Single-environment switch + custom history tab + webapp UI

- The fork's main `.venv` (pdf2zh 1.9.12 GUI shell) is **deleted**.
- **Primary UI now: `custom_pdf2zh/webapp/`** — a FastAPI + vanilla-JS "PDF 翻译
  工作台" whose UI replicates the user's reference project
  (`D:\mineru-pdf-translate-node-json-export`, Electron app): topbar / sidebar
  (settings + task-history cards with ↻/×) / dual-pane workbench (original left,
  translation right) with proportional sync scrolling. Pages are rendered
  server-side by pymupdf (`/api/page` PNG cache) — no PDF.js/CDN dependency.
  Launch: Desktop `PDF翻译.bat` → `python -m custom_pdf2zh.webapp.server`
  (needs `PYTHONPATH=<repo root>`, port 7860). `PDF翻译-高级.bat` launches the
  upstream Gradio GUI on 7861 for advanced engine settings.
  Key gotcha: `SettingsModel(translate_engine_settings=OllamaSettings(...))` —
  the engine field is REQUIRED at construction (no default).
- The Gradio GUI also has the injected 📚 翻译历史 tab (see below); both UIs
  share the same `pdf2zh_files/` library.
- **New: 📚 翻译历史 tab** injected into the venv's `gui.py` by
  `script/apply_all_patches.py` (idempotent, re-run after any
  `pip install -U pdf2zh-next/babeldoc` in the kernel venv):
  - `custom_pdf2zh/history_tab.py` (in-repo, imported via sys.path injection):
    scans `pdf2zh_files/**` for past translations (`_imported/` for manual
    imports, `_sidecache/` for generated views), and re-composes alternating
    dual PDFs into per-page left-original/right-translation pages.
  - Three view modes: 双面对照 (merged side-by-side, default), 双语原版
    (raw alternating dual), 纯译文 (extracted translated pages).
  - Fallback: non-alternating dual PDFs are shown as-is (heuristic: page 1
    has no CJK, page 2 has >20 CJK chars).
- **Ollama translator patches** (both the submodule runtime copy AND the venv
  site-packages copy — the bridge sets `PYTHONPATH=<submodule>` so the
  submodule copy is what actually runs under the old fork shell; the venv
  copy is what runs under the native GUI):
  `num_predict = min(max_token, 8192)` ×2, None-guard on token counters ×2,
  `stop_after_attempt` 100→5 ×2. All applied by `apply_all_patches.py`.
- `patches/apply_patches.sh` is superseded (it never worked: `git -C` resolves
  the patch path inside the submodule dir).
- Kernel venv: pdf2zh-next 2.9.0 + BabelDOC 0.6.4 (pymupdf 1.28.2 exceeds
  pdf2zh-next's declared <1.25.3 but all smoke tests pass; rollback:
  `pip install babeldoc==0.6.2 "pymupdf<1.25.3"`).

## Main repo changes (committed on the `custom` branch)

1. **`pdf2zh/pdf2zh.py`** — `--mode` defaults to `precise` (highest quality,
   pdf2zh_next/BabelDOC kernel) instead of upstream's `fast`.
2. **`pdf2zh/gui.py`**
   - Translation Mode dropdown defaults to `precise`.
   - New **Ollama Model** dropdown: populated from the local Ollama server
     (`/api/tags`) on service selection *and* on page load (the initial
     service value never fires `.select()`). Falls back to the first
     installed model when the configured default (gemma2) is missing.
     Custom values are allowed (server unreachable → free-text input).
   - `NO_PROXY=localhost,127.0.0.1,::1` set before launching Gradio — a
     global-mode proxy otherwise breaks Gradio's localhost health check,
     which cascades into the share-tunnel fallback.
   - Windows binds `0.0.0.0` first (Windows `::` is IPv6-only, killing
     127.0.0.1 access); other platforms keep `[::]` first.
   - Removed the automatic `share=True` last-resort launch. Sharing
     downloads Gradio's bundled `frpc`, which antivirus software
     (e.g. AhnLab) flags and deletes on every launch. Use `--share`
     explicitly if you really want a public link.
   - `progress_bar` accepts both tqdm-like objects (legacy kernel) and the
     dict progress events of the precise kernel (`overall_progress` is on a
     0–100 scale); dict `error` events are surfaced as `gr.Error`.
3. **`pdf2zh/kernel/v2_bridge.py`** — input file paths are resolved to
   absolute paths: the precise-kernel worker subprocess runs with its own
   cwd (the submodule dir), so relative paths (e.g. `pdf2zh_files/x.pdf`
   from the GUI) failed with "File does not exist".
4. **`pdf2zh/gui.py`** — after translation, use the output paths reported
   by the kernel (`TranslateResult.mono_pdf/dual_pdf`) instead of assuming
   legacy naming (`-mono.pdf`). The precise kernel writes
   `.zh.mono.pdf`/`.zh.dual.pdf`; without this the GUI raised
   "Error: No output" *after* a successful translation.

## Submodule patch (applied via `patches/apply_patches.sh`)

4. **`pdf2zh_next/translator/translator_impl/ollama.py`** — cap
   `num_predict` at 8192. Upstream sets it to `len(text) * 5`, which lets a
   single huge paragraph (dense tables/formulas) generate for ~15 minutes,
   possibly stuck in a repetition loop (temperature 0) until the budget is
   exhausted — the GUI progress bar appears frozen meanwhile.

   **Since 2026-09-23 the precise kernel venv runs `pdf2zh-next==2.9.0`
   installed from PyPI** (not the editable submodule checkout, whose branch
   only had 2.8.2). The patch is applied to the *installed* file
   `.venv/Lib/site-packages/pdf2zh_next/translator/translator_impl/ollama.py`
   in **two** places (`do_translate` and `do_llm_translate`). After any
   `pip install -U pdf2zh-next` inside the venv, re-apply it:
   replace `self.options["num_predict"] = max_token` with
   `self.options["num_predict"] = min(max_token, 8192)` in both spots.

## Environment notes

- The precise kernel lives in an isolated venv at
  `pdf2zh/kernel/PDFMathTranslate-next.git/.venv`
  (pdf2zh-next **2.9.0** from PyPI + babeldoc 0.6.2). The submodule checkout
  is no longer the runtime source; it only satisfies `PreciseKernel`
  availability checks (dir + pyproject.toml).
- Layout parsing (DocLayout-YOLO) and OCR (PaddleOCR v4 det) run locally
  via ONNX Runtime; models cached under `~/.cache/babeldoc/models/`.
- Keep `.gradio/`, `pdf2zh_files/` out of commits (runtime artifacts).
