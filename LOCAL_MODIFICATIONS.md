# Local Modifications

This repository carries local customizations on top of
[Byaidu/PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate).
Rebase this branch onto `upstream/main` after pulling updates, then re-check
the items below (upstream changes may conflict or make a patch obsolete).

## 2026-10-03 — 修复:旋转页(横向大表格)译文渲染错乱 + LLM 占位符残留

- **现象**:FMDS0729(148 页)译文中,源 PDF 全部 23 个 `/Rotate 90`
  横向表格页(11-14、22、25-29、31-32、38、40-43、45-49、54)渲染
  错乱:内容挤压、文字竖排乱流、表格裁切。另:第 11/25 页出现
  `{v1}°F {v2}.5加仑` 式占位符乱码、单元格错位。
- **根因(两层)**:
  1. BabelDOC 内核不处理 `/Rotate` 标记——MediaBox 竖版 612×792 +
     Rotate 90 的页,内核按竖版坐标解析重排,但输出保留旋转标记,
     视觉与坐标空间错位,横向页全毁;
  2. 内核发给 LLM 的提示词只示例了 `{1}` 占位符,而 babeldoc 实际
     掩码是 `{v0}/{v1}` 风格,提示词与实际不符,7B 级翻译模型
     (Hunyuan-MT-7B)在密集表格行上偶发改写/丢失掩码(全文档 4 处)。
- **修复(三项)**:
  1. `engine.py` 新增 `bake_page_rotation()`:翻译前用 pymupdf
     `remove_rotation()` 把旋转烘进页面内容(去掉 /Rotate,页面变成
     无标记真横向页),内核即可正常解析;输出文件名不变,工作台预览
     仍用原文件;失败退回原文件不阻塞。烘焙副本放
     `_sidecache/rotbake/<task_id>/`(scan_library 跳过该目录,否则
     散件扫描会把副本误显示成第二条任务),`_run` 的 finally 清理;
  2. 新补丁 `patches/pdf2zh-next-llm-placeholder-prompt.patch`:
     `base_translator.prompt()` 增补"占位符({v0}/{v1}/{v2}/{1})必须
     原样保留、不得改写合并丢弃"指令,所有 LLM 引擎受益。子模块升级
     后需随其他补丁一并重放(`patches/apply_patches.sh`);
  3. 烘焙副本入 `_sidecache` + 用后即删,杜绝任务列表幽灵行。
- **实测**:烘焙 4 页样本经内核翻译后,横向布局/表格结构/中文横排
  全部正确(对照修复前同页全毁);提示词补丁后 `{v` 残留 4 处 → 0,
  `>6.5加仑 (25升)` 等数字正确回填。148 页全文重译 16 分钟,视觉
  验收旋转页代表页(p11/p22/p25 + 对照版 p11)通过。
- **残留已知限制**:BabelDOC 对超复杂表格(跨页续表、多级表头)偶发
  单元格错位(如 p25 "D.2.1.8" 错入闪点列),属内核表格还原精度
  上限,非本层可修;密集表格文档建议开"自动术语表提取"或换更强模型。
- **终审补充(同日)**:
  1. finally 清理烘焙副本在 Windows 下会被内核残留句柄顶掉
     (ignore_errors 静默失败,实测留下 rotbake/<task_id> 残目录),
     新增 `engine.purge_rotbake()` 在服务启动、单实例锁之后整目录
     兜底清理——此刻必然无在跑任务,删除安全;
  2. 密集表格缺陷的运维处置实录:对问题页单页重翻(ignore_cache
     重掷,批次组合变化可消 {v} 残留与错位)+ pymupdf 红除清理
     2pt 微缩残片(只删文字不动表格线)+ 按页合并回成品,全程
     Hunyuan-MT-7B 实测有效,无需更换模型。

## 2026-10-03 — 修复:拖放一次却上传两遍(任务列表出现两个相同任务)

- **现象**:把 PDF 拖进"选择文档开始翻译"区,一次操作在"最近任务"出
  现两条记录,第二份输入文件带 `-2` 后缀;两个会话目录同秒创建,各自
  完整翻译一遍。两份上传落盘时间相差 2 毫秒,排除人工双击。
- **根因**:`index.html` 里空状态拖放区 `#source-empty` 嵌套在预览面板
  `#source-viewer` 内部,而 `app.mjs` 给两层都绑了 `drop` 事件——drop
  在内层触发后冒泡到外层再触发一遍,`handleFiles` 被调用两次,同一
  文件并发上传两份。点"选择文件"按钮走文件对话框只有单绑定,所以
  9-25 的端到端测试(用对话框)未暴露此问题。
- **修复(两层防御)**:
  1. 拖放只绑最外层 `#source-viewer`,内层 drop 冒泡自然覆盖;
  2. `handleFiles` 加 `uploadBusy` 互斥锁,上传进行中忽略重复触发,
     今后任何事件层双触发都只会传一份。
- **验证**:浏览器内对 `#source-empty` 连续派发两次带文件的 drop
  事件(复现旧场景),服务端 `_uploads` 仅落盘一份、任务表仅新增
  一条;测试任务随后经 /api/cancel + /api/delete 清理,无复活。
  index.html 脚本版本 v=5 → v=6。
- **教训**:同一事件处理器绑到嵌套元素而不 stopPropagation,等于隐式
  双绑定;UI 事件触发的写操作应在函数层加互斥,不能只信绑定层唯一。

## 2026-10-02 — 版面解析启用 GPU（DirectML），单页 0.53s → 0.08s

- **根因**:babeldoc 上游在 doclayout.py 里**故意只选 CPU provider**
  (注释: "directml/cuda may encounter problems under special
  circumstances"),DocLayout-YOLO 全程跑 CPU。
- **改动**:
  1. 内核 venv 卸载 `onnxruntime` 换装 `onnxruntime-directml 1.24.4`
     (Windows 免 CUDA 工具包即可用 N 卡,TITAN V 实测可用);
  2. venv `babeldoc/docvision/doclayout.py` 打补丁:provider 选择改为
     GPU 优先(DML/CUDA,正则 dml|cuda)+ CPU 兜底;上游只保留 CoreML 分支
     (macOS)不变。
- **实测**:1024 输入单页推理 CPU 均值 0.53s → DML 均值 0.082s(约 6.5×);
  `get_providers()` 确认 DmlExecutionProvider 生效。
- **注意**:
  - `script/setup_windows.ps1` 重建 venv 时会装回 CPU 版 onnxruntime,
    且 pip 升级 babeldoc 会覆盖 doclayout.py——重部署后需重放本补丁;
  - GPU 显存与 Ollama 常驻模型共用(TITAN V 12GB):云端引擎+本地解析
    无冲突;若同时跑本地 LLM 翻译,DML 约需数百 MB,注意余量;
  - rapidocr(OCR)走自己的会话,数字原生 PDF 不受影响;扫描件检测
    (0.3s 级)仍在 CPU,占比可忽略。
- 主仓库 Gradio GUI 用的 Anaconda 环境 babeldoc 未动(主力 UI 是工作台,
  走 venv);需要时同样方法可再加。

## 2026-09-25 — 修复:前端启动 ReferenceError("引擎检测中"真根因)

- **根因**:fb7b81e 给 initSettings 加 try/catch 时,把 fetch 解构出的
  `settings`/`langs` 留在 try 块内,块外的语言下拉渲染循环仍引用这两个
  块级变量 → 每次启动必然 `ReferenceError: settings is not defined`,
  main() 无 catch,错误静默吞掉,下拉永不填充、pill 永远停在初始占位
  "引擎检测中…"。后端一切正常(curl 全 200),纯前端作用域 bug;
  开发机同样中招(当时未刷新页面未察觉)。
- **修复**:渲染段改用 `state.settings`/`state.langs`;main() 捕获启动
  异常,状态栏显示"启动失败: 原因"不再静默;index.html 脚本版本
  v=4 → v=5。客户机用同方案实测通过(pill 显示"Ollama（本地）",
  引擎 23 项、语言 39 项)。
- **同轮附带**:静态服务对 .js/.mjs 强制 `text/javascript` MIME(3e62a73,
  防 Windows 注册表变异导致 module 被拒执行,防御性保留);启动器改
  %~dp0 相对路径可跨机器同步(86580bb);bat 注入 NO_PROXY 绕开客户机
  死代理(a2743d9)。
- **教训**:远程排障时后端 curl 全 200 而页面卡死,应第一时间索取
  浏览器 F12 Console 截图等直接证据,而非用间接证据叠理论
  (本次先后误判网络代理与 MIME,多绕了数轮)。

## 2026-09-25 — 修复:成品文件夹一次翻译出现三个 PDF

- **现象**:一次翻译后 `_exports` 出现两份"纯译文"——干净命名的
  `<文档名>-纯译文.pdf` 之外多出一份 `<文档名>.no_watermark-纯译文.pdf`
  (内容是内核收尾前 2 秒的中间版 mono,比最终版小 99 字节)。
- **根因**:`server.py` 把 `ensure_all_exports()`(启动回填)放在模块
  导入期执行。翻译收尾时若工作台被再次启动(双击图标/bat 等),新进程
  会在 import 期间抢跑回填:`_rebuild_orphan_jobs` 扫描库目录,把**运行中
  任务刚写进会话目录的半成品** `…no_watermark.zh.mono.pdf` 误登记为
  "无主产物"(任务要等完成时才登记 mono/dual 路径),按该幽灵任务名导出;
  随后新进程因 7860 端口被占而退出,只留下重复文件。dual 当时还没写出,
  所以只多一份纯译文。
- **修复(三层)**:
  1. `server.py` 新增 `_acquire_single_instance_lock()`——独占锁定
     `pdf2zh_files/_server.lock`(msvcrt/fcntl,句柄存模块级变量防 GC
     释放),拿不到锁打印"已在运行"并顺手打开网页后退出;
  2. `ensure_all_exports()`/`purge_orphan_cache()` 移到 `main()` 拿锁
     之后,import 不再有副作用;
  3. `engine.py` 的 `_rebuild_orphan_jobs`/`scan_library` 散件扫描跳过
     10 分钟内新产出的 `webapp-*` 会话文件(进行中任务会自行登记),
     恢复旧任务时剥掉名称里的 `.no_watermark` 中缀。
- **验证**:小文件端到端翻译 → 成品文件夹恰好两件(纯译文+中英对照);
  翻译中途再启动第二实例 → 打印"已在运行"立即退出,无任何多余导出。
  另:venv 的 `python.exe` 是启动器,每个实例表现为"启动器+真实解释器
  (Anaconda base)"两个 PID,属正常现象,排查时勿误判为双实例。

## 2026-09-25 — 从零重部署实测 + 预热误报修复 + 桌面图标静默启动

- **全量删除后从零重部署实测通过**:按用户要求删除本地全部代码与资源
  (仓库、venv、BabelDOC 资产缓存、翻译缓存、hy-mt2 两个模型;用户数据
  先备份到仓库外 `PDFMathTranslate-数据备份-20260925/`),按 README 流程
  `git clone --recursive` + `script\setup_windows.bat` 一键重装,端到端
  翻译验证通过(7b 默认模型,单页约 30 秒,术语提取默认跳过)。
- **修复预热误报**:`pdf2zh_next --warmup` 在 2.9.0 先下载资产、再因
  "At least one input file is required" 断言非零退出(资产实际已下载,
  但部署日志误报"预下载失败");改为直接调
  `python -c "from babeldoc.assets import assets; assets.warmup()"`,
  干净退出。
- **桌面图标静默启动**:桌面「PDF翻译工作台」快捷方式改指
  `start_workbench_hidden.vbs`(内容纯 ASCII,规避 wscript 系统代码页
  乱码坑):服务在跑 → 直接打开网页;未跑 → 无黑窗隐藏启动
  (`start_workbench.bat` 保留作控制台调试用)。停止服务用网页右上角
  「关闭服务」按钮。

## 2026-09-25 — 工作台第三轮:全引擎接入 / 删除级联 / 残影治理 / 优雅停机

- **接入官方全部 23 种翻译服务**:`engine.py` 从内核
  `TRANSLATION_ENGINE_METADATA_MAP` 自省派生服务注册表(内核升级自动跟上),
  `/api/services` 下发;前端设置面板按 服务→动态字段 渲染(密钥用密码框,
  留空走官方默认值),Ollama 模型字段在本地模型列表可用时渲染真下拉;
  设置结构升级为 `engine + engine_fields`,旧版扁平配置自动迁移,
  已存 API Key 可从界面清除(空值=删除)。
- **删除/清空级联清理缓存**:`delete_entries` 删除任务时同步清
  `_sidecache` 的派生视图(左原文右译文/仅译文/仅原文)与页面渲染缓存;
  页面渲染缓存改为**按源文件路径分桶**(`pages/<sha1(path)[:16]>/`),
  启动时 `purge_orphan_cache()` 清旧版平铺缓存与源文件已消失的孤儿视图
  (实测清出 153MB)。
- **前端"残影"治理**:引入视图代际(viewSeq/nextRenderGen),在途的
  `renderPdfList`/`openTask`/`startTranslate` 被更新的视图切换取代后自动
  丢弃,修复"点重新翻译后旧预览回填到进度卡后面"的竞态;
  运行日志改为"贴底才跟随"滚动,并给面板顶部留 12px 空隙,
  滚动到中间位置时日志行不再贴着页签条渲染。
- **Ollama 富文本占位标签泄漏修复**(子模块 + patch):
  小模型会把 babeldoc 的 `<style id='N'>` 标签打碎(如 `<style id="5>`),
  回贴正则认不出导致标签原样漏进译文;`ollama.py` 输出侧把碎形归一化回
  规范形(`patches/pdf2zh-next-ollama-style-tag-normalize.patch`)。
- **自动术语表提取开关**(默认关):对应官方 `no_auto_extract_glossary`;
  本地小模型下术语提取(块数×1 次 LLM 调用)耗时近半且质量有限,默认关闭,
  用云端大模型时可打开提升术语一致性;非 LLM 引擎强制跳过不受开关影响。
- **关闭服务**:右上角按钮 → `/api/shutdown` 优雅停机(uvicorn
  `timeout_graceful_shutdown=3` 兜底 SSE 长连接);有任务运行时默认拦截,
  force 时先取消任务;状态均已落盘,停机不丢数据。
- 移除 `num_predict`(最大输出 token)表单项:翻译器按输入长度自动放大,
  手动设置会被覆盖、设小会截断译文;仍可通过 settings JSON 高级配置。

## 2026-09-25 — 子模块补丁持久化到自有 fork

- 子模块 `pdf2zh_next/translator/translator_impl/ollama.py` 的三组补丁
  (num_predict 封顶 / token 统计 None 保护 / 重试 100→5)已提交到
  **https://github.com/CHEN010325/PDFMathTranslate-next** 的 `custom` 分支
  (commit 75d8b47,基于上游 61a6b68);
- `.gitmodules` 的 url 改为该 fork、branch 指定 `custom`——
  **`git clone --recursive` 现在直接得到带补丁的引擎**,不再依赖本地重放;
- `apply_all_patches.py` 仍然保留:它还负责向 venv 的 gui.py 注入
  📚 历史页签(该改动在 venv 里,无法随子模块分发),ollama 部分
  对已打补丁的子模块自动跳过(幂等);
- 本地子模块已切到 `custom` 分支跟踪。

## 2026-09-25 — 一键部署脚本 + README 部署指南

- 新增 `script/setup_windows.bat`(ASCII 存根)+ `script/setup_windows.ps1`
  (UTF-8 BOM 主逻辑):全新克隆后一键完成 子模块 → 内核 venv
  (pdf2zh-next 2.9.0 两步安装 + babeldoc 0.6.4)→ 补丁 → Ollama/模型 →
  桌面快捷方式 → 启动工作台;幂等可续跑。
  坑位记录:①UTF-8 的 .bat 在中文 Windows 被 cmd 按 GBK 解析会乱码崩溃,
  必须用"ASCII 存根 + BOM 的 PS1"模式;②pdf2zh-next 2.9.0 依赖声明与
  babeldoc 0.6.4 冲突,必须分两条 pip install(第二条的冲突警告属预期)。
- README.md 顶部新增中文一键部署指南(已在全新克隆上端到端实测:
  部署后工作台启动、补丁齐全、版本正确)。

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
   from the GUI) failed with "File does not exist". Additionally (2026-10-02),
   v1's `SILICON_API_KEY`/`SILICON_MODEL` are aliased to the v2 engine's
   `SILICONFLOW_*` env names, so the SiliconFlow key configured via the GUI
   works through the precise kernel (实测 tencent/Hunyuan-MT-7B 5 页 102s)。
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

   **更正(2026-10-03 审查发现)**:2026-09-23 曾把 venv 升级到 PyPI 的
   pdf2zh-next 2.9.0 并以为这就是运行版本——**实际不然**:precise 内核的
   worker 以 cwd=子模块目录 + `PYTHONPATH=子模块` 运行,工作台启动器也
   cd 进子模块,`import pdf2zh_next` 一律解析到**子模块里的源码**
   (custom 分支,基于上游 61a6b68 + 全部补丁),venv site-packages 的
   2.9.0 从未真正执行过翻译。真正的引擎版本以子模块 custom 分支为准;
   子模块本地 checkout 于 2026-10-03 归位到 fork 的 custom 分支
   (e1eb6eb = num_predict 封顶×2 + token 统计 None 保护×2 + 重试
   100→5×2 + style 标签归一化×2,此前本地漂移在 61a6b68 且只贴了
   封顶补丁的一半)。venv 中的 2.9.0 副本与 babeldoc/onnxruntime 等
   依赖仍被使用,ollama.py 补丁需同时存在于两处
   (由 `script/apply_all_patches.py` 幂等维护)。

## Environment notes

- The precise kernel lives in an isolated venv at
  `pdf2zh/kernel/PDFMathTranslate-next.git/.venv`
  (pdf2zh-next **2.9.0** from PyPI + babeldoc 0.6.2). The submodule checkout
  is no longer the runtime source; it only satisfies `PreciseKernel`
  availability checks (dir + pyproject.toml).
- Layout parsing (DocLayout-YOLO) and OCR (PaddleOCR v4 det) run locally
  via ONNX Runtime; models cached under `~/.cache/babeldoc/models/`.
- Keep `.gradio/`, `pdf2zh_files/` out of commits (runtime artifacts).
