#!/usr/bin/env python3
"""一键重放本地补丁(幂等,pip 升级 pdf2zh-next / babeldoc 之后运行一次)。

覆盖范围:
  1. venv gui.py  — 注入 📚 翻译历史页签(custom_pdf2zh.history_tab)
  2. ollama.py ×2 — num_predict 封顶 8192 / token 统计 None 保护 / 重试 100→5
     (子模块运行副本 + venv site-packages 副本都打)

用法(仓库根目录):
    python script/apply_all_patches.py

锚点缺失(上游改了代码)时报错退出,提示人工检查,不会盲改。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBMODULE = REPO / "pdf2zh" / "kernel" / "PDFMathTranslate-next.git"
VENV_SITE = SUBMODULE / ".venv" / "Lib" / "site-packages"

GUI_PY = VENV_SITE / "pdf2zh_next" / "gui.py"
OLLAMA_COPIIES = [
    SUBMODULE / "pdf2zh_next" / "translator" / "translator_impl" / "ollama.py",
    VENV_SITE / "pdf2zh_next" / "translator" / "translator_impl" / "ollama.py",
]

problems: list[str] = []


def patch_gui() -> None:
    if not GUI_PY.exists():
        problems.append(f"gui.py 不存在: {GUI_PY}")
        return
    src = GUI_PY.read_text(encoding="utf-8")
    changed = False

    # --- 注入 1:侧边栏加 📚 按钮 ---
    anchor_btn = (
        '                btn_settings_tab = gr.Button("⚙️", '
        'variant="secondary", elem_classes=["sidebar-btn"])'
    )
    if 'btn_history_tab = gr.Button("📚"' in src:
        pass  # 已应用
    elif anchor_btn in src:
        src = src.replace(
            anchor_btn,
            anchor_btn
            + '\n                btn_history_tab = gr.Button("📚", '
            'variant="secondary", elem_classes=["sidebar-btn"])',
            1,
        )
        changed = True
    else:
        problems.append("gui.py: 找不到侧边栏按钮锚点(上游结构可能已变)")

    # --- 注入 2:设置页之后插入历史页 Group ---
    anchor_group = (
        "                    tech_details = gr.Markdown(\n"
        "                        tech_details_string,\n"
        '                        elem_classes=["secondary-text"],\n'
        "                    )\n"
    )
    history_block = (
        "\n"
        "                with gr.Group(visible=False, elem_classes=[\"settings-container\"]) as tab_history:\n"
        "                    import sys as _custom_sys\n"
        "                    _custom_repo_root = str(Path(__file__).resolve().parents[7])\n"
        "                    if _custom_repo_root not in _custom_sys.path:\n"
        "                        _custom_sys.path.insert(0, _custom_repo_root)\n"
        "                    from custom_pdf2zh.history_tab import build_history_tab as _custom_build_history_tab\n"
        "\n"
        "                    _custom_build_history_tab()\n"
    )
    if "as tab_history:" in src:
        pass
    elif anchor_group in src:
        src = src.replace(anchor_group, anchor_group + history_block, 1)
        changed = True
    else:
        problems.append("gui.py: 找不到 tech_details 锚点(上游结构可能已变)")

    # --- 注入 3:页签切换从两态改三态 ---
    old_switch = (
        "        # Sidebar tab switching: 主界面 / 设置界面\n"
        "        def _show_main_tab():\n"
        "            return (\n"
        '                gr.update(variant="primary"),\n'
        '                gr.update(variant="secondary"),\n'
        '                gr.update(visible=True),\n'
        '                gr.update(visible=False),\n'
        "            )\n"
        "\n"
        "        def _show_settings_tab():\n"
        "            return (\n"
        '                gr.update(variant="secondary"),\n'
        '                gr.update(variant="primary"),\n'
        '                gr.update(visible=False),\n'
        '                gr.update(visible=True),\n'
        "            )\n"
        "\n"
        "        btn_main_tab.click(\n"
        "            _show_main_tab,\n"
        "            outputs=[btn_main_tab, btn_settings_tab, tab_main, tab_settings],\n"
        "        )\n"
        "        btn_settings_tab.click(\n"
        "            _show_settings_tab,\n"
        "            outputs=[btn_main_tab, btn_settings_tab, tab_main, tab_settings],\n"
        "        )\n"
    )
    new_switch = (
        "        # Sidebar tab switching: 主界面 / 设置界面 / 历史界面 (custom)\n"
        "        def _show_main_tab():\n"
        "            return (\n"
        '                gr.update(variant="primary"),\n'
        '                gr.update(variant="secondary"),\n'
        '                gr.update(variant="secondary"),\n'
        '                gr.update(visible=True),\n'
        '                gr.update(visible=False),\n'
        '                gr.update(visible=False),\n'
        "            )\n"
        "\n"
        "        def _show_settings_tab():\n"
        "            return (\n"
        '                gr.update(variant="secondary"),\n'
        '                gr.update(variant="primary"),\n'
        '                gr.update(variant="secondary"),\n'
        '                gr.update(visible=False),\n'
        '                gr.update(visible=True),\n'
        '                gr.update(visible=False),\n'
        "            )\n"
        "\n"
        "        def _show_history_tab():\n"
        "            return (\n"
        '                gr.update(variant="secondary"),\n'
        '                gr.update(variant="secondary"),\n'
        '                gr.update(variant="primary"),\n'
        '                gr.update(visible=False),\n'
        '                gr.update(visible=False),\n'
        '                gr.update(visible=True),\n'
        "            )\n"
        "\n"
        "        _custom_tab_outputs = [\n"
        "            btn_main_tab, btn_settings_tab, btn_history_tab,\n"
        "            tab_main, tab_settings, tab_history,\n"
        "        ]\n"
        "        btn_main_tab.click(_show_main_tab, outputs=_custom_tab_outputs)\n"
        "        btn_settings_tab.click(_show_settings_tab, outputs=_custom_tab_outputs)\n"
        "        btn_history_tab.click(_show_history_tab, outputs=_custom_tab_outputs)\n"
    )
    if "def _show_history_tab():" in src:
        pass
    elif old_switch in src:
        src = src.replace(old_switch, new_switch, 1)
        changed = True
    else:
        problems.append("gui.py: 找不到页签切换事件锚点(上游结构可能已变)")

    if changed:
        GUI_PY.write_text(src, encoding="utf-8")
    print(f"gui.py 注入: {'已更新' if changed else '已是最新(跳过)'}")


def patch_ollama(path: Path) -> None:
    if not path.exists():
        problems.append(f"ollama.py 不存在: {path}")
        return
    src = path.read_text(encoding="utf-8")
    changed = False

    # 1) num_predict 封顶(两处:do_translate / do_llm_translate)
    n_capped = len(re.findall(r'^\s*self\.options\["num_predict"\] = min\(max_token, 8192\)', src, re.M))
    if n_capped < 2:
        new_src, n = re.subn(
            r'^(?P<i>[ ]+)self\.options\["num_predict"\] = max_token$',
            lambda m: f'{m.group("i")}self.options["num_predict"] = min(max_token, 8192)',
            src,
            flags=re.M,
        )
        if n_capped + n != 2:
            problems.append(f"{path.name}: num_predict 期望 2 处,已有 {n_capped} 处,新匹配 {n} 处")
        src = new_src
        changed = changed or n > 0

    # 2) token 统计 None 保护(两个方法里各一组三行计数器,缩进自适应)
    guard = "response.prompt_eval_count is not None and response.eval_count is not None"
    counter_re = re.compile(
        r"^(?P<i>[ ]+)self\.token_count\.inc\(response\.prompt_eval_count \+ response\.eval_count\)\n"
        r"(?P=i)self\.prompt_token_count\.inc\(response\.prompt_eval_count\)\n"
        r"(?P=i)self\.completion_token_count\.inc\(response\.eval_count\)\n",
        re.M,
    )

    def _guard_repl(m: re.Match) -> str:
        i = m.group("i")
        body = m.group(0).rstrip("\n")
        indented = "\n".join(
            (i + "    " + line) if line.strip() else line
            for line in body.split("\n")
        )
        return f"{i}if {guard}:\n{indented}\n"

    n_guarded = src.count(f"if {guard}:")
    if n_guarded < 2:
        new_src, n = counter_re.subn(_guard_repl, src)
        if n_guarded + n != 2:
            problems.append(
                f"{path.name}: token 统计保护期望 2 处,已有 {n_guarded} 处,新匹配 {n} 处"
            )
        src = new_src
        changed = changed or n > 0

    # 3) 重试上限 100 → 5(两个 @retry 装饰器)
    if "stop_after_attempt(100)" in src:
        src = src.replace("stop_after_attempt(100)", "stop_after_attempt(5)")
        changed = True

    if changed:
        path.write_text(src, encoding="utf-8")
    rel = "子模块运行副本" if (SUBMODULE / "pdf2zh_next") in path.parents else "venv 副本"
    print(f"ollama.py({rel}) 补丁: {'已更新' if changed else '已是最新(跳过)'}")


def main() -> int:
    print(f"仓库: {REPO}\n")
    patch_gui()
    print()
    for ollama_path in OLLAMA_COPIIES:
        patch_ollama(ollama_path)

    print()
    if problems:
        print("⚠ 有锚点未命中,需人工检查:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("✅ 全部补丁就绪。启动:桌面《PDF翻译.bat》")
    return 0


if __name__ == "__main__":
    sys.exit(main())
