# Local Modifications

This repository carries local customizations on top of
[Byaidu/PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate).
Rebase this branch onto `upstream/main` after pulling updates, then re-check
the items below (upstream changes may conflict or make a patch obsolete).

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

## Submodule patch (applied via `patches/apply_patches.sh`)

4. **`pdf2zh_next/translator/translator_impl/ollama.py`** — cap
   `num_predict` at 8192. Upstream sets it to `len(text) * 5`, which lets a
   single huge paragraph (dense tables/formulas) generate for ~15 minutes,
   possibly stuck in a repetition loop (temperature 0) until the budget is
   exhausted — the GUI progress bar appears frozen meanwhile.

## Environment notes

- The precise kernel lives in an isolated venv at
  `pdf2zh/kernel/PDFMathTranslate-next.git/.venv` (pdf2zh_next 2.7.1,
  babeldoc 0.5.24). Rebuild with `pdf2zh-setup-precise` if needed.
- Layout parsing (DocLayout-YOLO) and OCR (PaddleOCR v4 det) run locally
  via ONNX Runtime; models cached under `~/.cache/babeldoc/models/`.
- Keep `.gradio/`, `pdf2zh_files/` out of commits (runtime artifacts).
