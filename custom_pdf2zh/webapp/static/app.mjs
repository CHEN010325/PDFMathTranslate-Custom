/* PDF 翻译工作台前端 — 照抄 mineru-pdf-translate 的交互,对接本地 FastAPI。 */

const $ = (id) => document.getElementById(id);

/* 右上角模型 pill 只显示名字,不带命名空间前缀(s2021008840/hy-mt2 → hy-mt2) */
const shortModel = (m) => (m || "").includes("/") ? m.split("/").pop() : (m || "");

const state = {
  settings: {},
  langs: {},
  services: [],
  serviceGroups: {},
  draftFields: {},
  ollamaModels: [],
  tasks: [],
  activeTask: null,
  currentTaskId: null,
  filter: "all",
  search: "",
  logLines: [],
  lastLogSig: null,
  lastRender: "",
};

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------

/* 视图代际:切换视图的操作(startTranslate/openTask/resetActiveView)都会递增,
   在途的异步渲染醒来后发现代际已变就丢弃,防止旧内容"残影"回填。 */
let viewSeq = 0;

/* 单个容器的渲染代际:清空或发起新渲染时递增,旧的 renderPdfList 自动作废。 */
function nextRenderGen(container) {
  const gen = (Number(container.dataset.renderGen) || 0) + 1;
  container.dataset.renderGen = String(gen);
  return gen;
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

function toast(text, ms = 1800) {
  const el = $("status");
  el.textContent = text;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.textContent = "就绪"), ms);
}

// ---------------------------------------------------------------------------
// 设置面板
// ---------------------------------------------------------------------------

async function initSettings() {
  const [settings, langs, services, models] = await Promise.all([
    fetchJSON("/api/settings"),
    fetchJSON("/api/langs"),
    fetchJSON("/api/services").catch(() => ({ services: [], groups: {} })),
    fetchJSON("/api/ollama-models").catch(() => ({ models: [] })),
  ]);
  state.settings = settings;
  state.langs = langs;
  state.services = services.services || [];
  state.serviceGroups = services.groups || {};
  state.draftFields = { ...(settings.engine_fields || {}) };
  state.ollamaModels = models.models || [];

  for (const [id, val, withAuto] of [
    ["sourceLanguage", settings.lang_in, true],
    ["targetLanguage", settings.lang_out, false],
  ]) {
    const sel = $(id);
    sel.innerHTML = "";
    if (withAuto) {
      const o = document.createElement("option");
      o.value = "auto";
      o.textContent = "自动检测";
      sel.append(o);
    }
    for (const [code, label] of Object.entries(langs)) {
      const o = document.createElement("option");
      o.value = code;
      o.textContent = label;
      sel.append(o);
    }
    sel.value = val;
  }

  $("autoExtractGlossary").checked = !!settings.auto_extract_glossary;

  renderEngineSelect();
  renderEngineFields();
  updateServicePill();
}

function renderEngineSelect() {
  const sel = $("engine");
  sel.innerHTML = "";
  const byGroup = {};
  for (const s of state.services) (byGroup[s.group] ??= []).push(s);
  const order = ["free", "local", "llm", "trad"];
  for (const g of order) {
    if (!byGroup[g]?.length) continue;
    const og = document.createElement("optgroup");
    og.label = state.serviceGroups[g] || g;
    for (const s of byGroup[g]) {
      const o = document.createElement("option");
      o.value = s.type;
      o.textContent = s.label;
      og.append(o);
    }
    sel.append(og);
  }
  // 后端返回的服务里找不到已存引擎时(内核降级等),兜底加一项
  if (!state.services.some((s) => s.type === sel.value || s.type === state.settings.engine)) {
    const saved = state.settings.engine;
    if (saved) {
      const o = document.createElement("option");
      o.value = saved;
      o.textContent = saved;
      sel.append(o);
    }
  }
  sel.value = state.settings.engine || "Ollama";
  sel.onchange = () => {
    mergeDraftFromDom();
    renderEngineFields();
    updateServicePill();
    if (sel.value === "Ollama") refreshOllamaModels();
  };
}

function renderEngineFields() {
  const box = $("engine-fields");
  box.innerHTML = "";
  const svc = state.services.find((s) => s.type === $("engine").value);
  if (!svc) return;
  if (!svc.fields.length) {
    const hint = document.createElement("p");
    hint.className = "field-hint";
    hint.textContent = "该服务无需额外配置，选好后直接开始翻译。";
    box.append(hint);
    return;
  }
  for (const f of svc.fields) {
    const label = document.createElement("label");
    const span = document.createElement("span");
    span.textContent = f.label;
    const input = document.createElement("input");
    input.dataset.field = f.name;
    input.value = state.draftFields[f.name] ?? "";
    input.placeholder = f.default || (f.secret ? "sk-…" : "");
    input.autocomplete = "off";
    input.spellcheck = false;
    if (f.secret) input.type = "password";
    // Ollama 模型:本地模型列表可用时渲染真下拉,连不上才回退手填
    if (svc.type === "Ollama" && f.name === "ollama_model" && state.ollamaModels.length) {
      const norm = (s) => String(s).replace(/:latest$/i, "");
      const cur = state.draftFields[f.name] ?? "";
      const sel = document.createElement("select");
      sel.dataset.field = f.name;
      for (const m of state.ollamaModels) {
        const o = document.createElement("option");
        o.value = m; // 存全名:Ollama 解析模型需要完整命名空间名
        o.textContent = shortModel(m); // 显示短名,不带命名空间前缀
        sel.append(o);
      }
      const match = cur && state.ollamaModels.find((m) => norm(m) === norm(cur));
      if (cur && !match) {
        const o = document.createElement("option");
        o.value = cur;
        o.textContent = shortModel(cur);
        sel.append(o);
      }
      sel.value = match || cur || state.ollamaModels[0];
      label.append(span, sel);
      box.append(label);
      continue;
    }
    label.append(span, input);
    box.append(label);
  }
}

/* Ollama 服务下异步刷新本地模型列表,拿到了就重渲染成下拉 */
function refreshOllamaModels() {
  fetchJSON("/api/ollama-models")
    .then((m) => {
      const list = m.models || [];
      if (!list.length || $("engine").value !== "Ollama") return;
      if (list.join("|") === state.ollamaModels.join("|")) return;
      state.ollamaModels = list;
      mergeDraftFromDom();
      renderEngineFields();
    })
    .catch(() => {});
}

/* 服务切换前,把当前表单里已输入的值收进草稿,避免切回来丢失 */
function mergeDraftFromDom() {
  for (const el of $("engine-fields").querySelectorAll("input[data-field], select[data-field]")) {
    const v = el.value.trim();
    if (v) state.draftFields[el.dataset.field] = v;
    else delete state.draftFields[el.dataset.field];
  }
}

function collectEngineFields() {
  mergeDraftFromDom();
  return { ...state.draftFields };
}

function updateServicePill() {
  const pill = $("settings-state");
  const svc = state.services.find((s) => s.type === $("engine").value);
  pill.textContent = svc ? svc.label : "引擎检测中…";
  pill.classList.add("ok");
}

/* 与后端 display_model 对齐:任务卡片/翻译中标题上展示的引擎标识 */
function displayModelName(engine, fields) {
  const flag = String(engine || "Ollama").toLowerCase();
  const model = fields?.[`${flag}_model`] || "";
  if (model) return String(model);
  return state.services.find((s) => s.type === engine)?.label || engine || "";
}

function currentSettings() {
  return {
    engine: $("engine").value,
    engine_fields: collectEngineFields(),
    lang_in: $("sourceLanguage").value,
    lang_out: $("targetLanguage").value,
    auto_extract_glossary: $("autoExtractGlossary").checked,
  };
}

async function saveSettings() {
  state.settings = await fetchJSON("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentSettings()),
  });
  state.draftFields = { ...(state.settings.engine_fields || {}) };
  toast("设置已保存");
}

/* 关闭服务:有任务在跑先提示;关闭成功后冻结页面,提示用户关闭标签页 */
async function shutdownService() {
  const running = state.tasks.filter((t) => t.running).length;
  let force = false;
  if (running > 0) {
    if (!confirm(`有 ${running} 个任务正在运行，关闭服务会中断它们（已完成的任务不受影响）。\n确定关闭服务？`)) return;
    force = true;
  } else if (!confirm("确定关闭翻译服务？\n关闭后本页面无法继续操作；下次使用请重新运行 start_workbench.bat。")) return;
  const res = await fetchJSON("/api/shutdown", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  }).catch((e) => ({ ok: false, message: e.message }));
  if (!res.ok) {
    toast(res.message || "关闭失败", 4000);
    return;
  }
  toast("服务已关闭", 5000);
  $("status").textContent = "服务已关闭，可关闭本页面";
  $("settings-state").textContent = "已关闭";
  $("settings-state").classList.remove("ok");
  document.querySelectorAll("button").forEach((b) => (b.disabled = true));
}

// ---------------------------------------------------------------------------
// 任务列表:一条登记一张卡片(运行状态直接叠在卡片上,不再单开)
// ---------------------------------------------------------------------------

async function refreshTasks(force = false) {
  const data = await fetchJSON("/api/tasks");
  const sig = JSON.stringify([
    data.tasks.map((t) => [t.id, t.time, t.status, t.running ? t.progress : null]),
    data.running.map((t) => [t.id, t.progress, t.stage]),
  ]);
  state.tasks = data.tasks;
  if (sig !== state.lastRender || force) {
    state.lastRender = sig;
    renderTaskList();
  }
}

function renderTaskList() {
  const listEl = $("task-list");
  listEl.innerHTML = "";

  const items = state.tasks
    .filter((t) => t.name.toLowerCase().includes(state.search.toLowerCase()))
    .filter((t) => (state.filter === "done" ? t.status === "done" && t.translated : true));

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "task-empty";
    empty.textContent = state.search ? "没有匹配的文件。" : "上传文档后会出现在这里。";
    listEl.append(empty);
    return;
  }

  for (const it of items) {
    const running = !!it.running;
    const card = document.createElement("div");
    card.className = `task-item status-${running ? "running" : it.status === "failed" ? "failed" : it.translated ? "done" : "pending"}`;
    if (state.activeTask && it.id && state.activeTask.id === it.id) card.classList.add("active");

    const icon = document.createElement("div");
    icon.className = "task-icon";
    icon.textContent = "PDF";

    const main = document.createElement("div");
    main.className = "task-main";
    const nm = document.createElement("div");
    nm.className = "task-name";
    nm.textContent = it.name;
    nm.title = it.name;
    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.textContent = running
      ? `${it.stage || ""} · ${it.progress ?? 0}%`
      : `${it.time} · ${it.kind}`;
    main.append(nm, meta);

    const stateEl = document.createElement("div");
    stateEl.className = "task-state";
    stateEl.textContent = running ? "运行中" : it.status === "failed" ? "失败" : it.translated ? "完成" : "待处理";

    const actions = document.createElement("div");
    actions.className = "task-actions";
    if (!running) {
      const retry = document.createElement("button");
      retry.className = "task-action";
      retry.textContent = "↻";
      retry.title = "重新翻译";
      retry.disabled = !it.original;
      retry.onclick = (e) => { e.stopPropagation(); retranslate(it); };
      const del = document.createElement("button");
      del.className = "task-action danger";
      del.textContent = "×";
      del.title = "删除历史和输出文件";
      del.onclick = (e) => { e.stopPropagation(); deleteTask(it); };
      actions.append(retry, del);
    }

    card.append(icon, main, stateEl, actions);
    card.onclick = () => (running ? focusRunning(it) : openTask(it));
    listEl.append(card);
  }
}

// ---------------------------------------------------------------------------
// 页面流渲染 + 双栏同步滚动
// ---------------------------------------------------------------------------

async function renderPdfList(container, relPath) {
  const gen = nextRenderGen(container);
  const info = await fetchJSON(`/api/pdf-info?file=${encodeURIComponent(relPath)}`);
  if (container.dataset.renderGen !== String(gen)) return; // 已被更新的渲染/清空取代
  container.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (let i = 1; i <= info.pages; i++) {
    const page = document.createElement("article");
    page.className = "pdf-page";
    const img = document.createElement("img");
    img.loading = i <= 2 ? "eager" : "lazy";
    img.decoding = "async";
    img.style.aspectRatio = `${info.width}/${info.height}`;
    img.alt = `第 ${i} 页`;
    img.dataset.page = String(i);
    img.onerror = () => {
      if (!img.dataset.retried) {
        img.dataset.retried = "1";
        img.src = `/api/page?file=${encodeURIComponent(relPath)}&page=${i}&r=1`;
      }
    };
    img.src = `/api/page?file=${encodeURIComponent(relPath)}&page=${i}`;
    const num = document.createElement("div");
    num.className = "pdf-page-number";
    num.textContent = `${i} / ${info.pages}`;
    page.append(img, num);
    frag.append(page);
  }
  if (container.dataset.renderGen !== String(gen)) return; // append 前最后一道闸
  container.append(frag);
}

/* 按页锚定同步:src 滚到第 idx 页的第 frac 处,dst 对齐到同一页同一位置。
   两侧页数不一致时按比例映射页序号。 */
function pageAnchoredSync(src, dst) {
  const srcPages = src.querySelectorAll(".pdf-page");
  const dstPages = dst.querySelectorAll(".pdf-page");
  if (!srcPages.length || !dstPages.length) return;
  const top = src.scrollTop + 4;
  let idx = 0;
  for (let i = 0; i < srcPages.length; i++) {
    if (srcPages[i].offsetTop <= top) idx = i;
    else break;
  }
  const sp = srcPages[idx];
  const frac =
    sp.offsetHeight > 0
      ? Math.min(1, Math.max(0, (top - sp.offsetTop) / sp.offsetHeight))
      : 0;
  const di = Math.min(
    dstPages.length - 1,
    Math.round((idx * (dstPages.length - 1)) / Math.max(1, srcPages.length - 1))
  );
  const dp = dstPages[di];
  dst.scrollTop = Math.max(
    0,
    Math.min(dst.scrollHeight - dst.clientHeight, dp.offsetTop + frac * dp.offsetHeight - 4)
  );
}

function bindSyncScroll(a, b) {
  let driver = null;
  let timer = null;
  const make = (src, dst) => () => {
    if (driver && driver !== src) return; // 对方正在驱动,忽略自身被程序滚动的回声
    driver = src;
    pageAnchoredSync(src, dst);
    clearTimeout(timer);
    timer = setTimeout(() => (driver = null), 90);
  };
  a.addEventListener("scroll", make(a, b), { passive: true });
  b.addEventListener("scroll", make(b, a), { passive: true });
}

// ---------------------------------------------------------------------------
// 打开任务(历史条目)→ 双栏对照
// ---------------------------------------------------------------------------

async function openTask(entry) {
  const seq = ++viewSeq;
  state.activeTask = entry;
  renderTaskList();
  $("active-model-name").textContent = shortModel(entry.model || "");

  $("source-title").textContent = entry.name;
  $("result-title").textContent = entry.translated ? "已生成" : "尚未翻译";

  try {
    let srcRel = entry.original;
    if (!srcRel && entry.dual) {
      srcRel = (await fetchJSON("/api/prepare-view", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: entry.dual, which: "原文" }),
      })).path;
    }
    if (seq !== viewSeq) return; // 期间用户切换了视图,本次作废
    if (srcRel) {
      $("source-empty").hidden = true;
      $("source-preview").hidden = false;
      await renderPdfList($("source-preview"), srcRel);
      if (seq !== viewSeq) return;
      $("source-meta").textContent = "PDF · 原文";
    }
  } catch (e) {
    appendLog(`原文预览失败: ${e.message}`);
  }

  try {
    let outRel = entry.mono;
    if (!outRel && entry.dual) {
      outRel = (await fetchJSON("/api/prepare-view", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: entry.dual, which: "译文" }),
      })).path;
    }
    if (seq !== viewSeq) return; // 期间用户切换了视图,本次作废
    if (outRel) {
      showResultView("translation");
      await renderPdfList($("translated-preview"), outRel);
      if (seq !== viewSeq) return;
      $("result-empty").hidden = true;
      $("translated-preview").hidden = false;
    } else {
      $("result-empty").textContent = "尚未翻译。可在左侧任务卡片上点 ↻ 开始。";
      $("result-empty").hidden = false;
      $("translated-preview").hidden = true;
    }
  } catch (e) {
    appendLog(`译文预览失败: ${e.message}`);
  }

  if (seq !== viewSeq) return;
  $("open-mono").disabled = !(entry.mono || entry.dual);
  $("open-dual").disabled = !entry.dual;
  $("open-alt").disabled = !entry.dual;
  $("open-dual").dataset.rel = entry.dual || "";
  $("open-alt").dataset.rel = entry.dual || "";
  $("open-mono").dataset.mono = entry.mono || "";
  $("progress-card").hidden = true;
}

function focusRunning(entry) {
  state.currentTaskId = entry.task_id || null;
  showProgress({ name: entry.name, stage: entry.stage, progress: entry.progress ?? 0, detail: "" });
  showResultView("log");
}

// ---------------------------------------------------------------------------
// 上传与翻译
// ---------------------------------------------------------------------------

function pickFiles() {
  $("file-input").click();
}

async function handleFiles(fileList) {
  const files = [...fileList].filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  if (!files.length) return toast("请选择 PDF 文件");
  for (const f of files) {
    const form = new FormData();
    form.append("file", f);
    try {
      const up = await fetchJSON("/api/upload", { method: "POST", body: form });
      toast(`已上传 ${up.name},开始翻译…`, 3000);
      await startTranslate(up.uploaded, up.name);
    } catch (e) {
      appendLog(`上传失败 ${f.name}: ${e.message}`);
      toast(`上传失败: ${e.message}`, 3000);
    }
  }
}

async function startTranslate(relPath, name) {
  const seq = ++viewSeq;
  const body = { path: relPath, ...currentSettings() };
  const res = await fetchJSON("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (seq !== viewSeq) return; // 等待期间用户切换了视图,UI 交由最新操作接管
  state.currentTaskId = res.task_id;
  $("source-title").textContent = name;
  $("result-title").textContent = "翻译中…";
  $("active-model-name").textContent = shortModel(displayModelName(body.engine, body.engine_fields));
  const tp = $("translated-preview");
  nextRenderGen(tp); // 作废在途的预览渲染,防止旧内容回填成残影
  tp.innerHTML = "";
  tp.hidden = true;
  state.logLines = []; // 新任务日志从零开始,不混入上一轮
  $("log").textContent = "";
  state.lastLogSig = null;
  $("result-empty").hidden = true;
  showProgress({ name, stage: "准备中", progress: 0, detail: "" });
  showResultView("log");
  $("source-empty").hidden = true;
  $("source-preview").hidden = false;
  renderPdfList($("source-preview"), relPath).catch(() => {});
  refreshTasks();
}

async function retranslate(entry) {
  if (!entry.original) return;
  await startTranslate(entry.original, entry.name);
}

function resetActiveView() {
  ++viewSeq;
  nextRenderGen($("source-preview"));
  nextRenderGen($("translated-preview"));
  state.activeTask = null;
  $("source-title").textContent = "等待上传文档";
  $("source-meta").textContent = "PDF · 本地预览";
  $("source-preview").hidden = true;
  $("source-preview").innerHTML = "";
  $("source-empty").hidden = false;
  $("result-title").textContent = "等待任务";
  $("translated-preview").innerHTML = "";
  $("translated-preview").hidden = true;
  $("result-empty").hidden = false;
  document.querySelectorAll(".result-actions button").forEach((b) => (b.disabled = true));
  state.logLines = [];
  $("log").textContent = "";
  $("log").hidden = true;
  showResultView("translation");
}

async function deleteTask(entry) {
  const paths = [entry.original, entry.mono, entry.dual].filter(Boolean);
  if (!confirm(`删除「${entry.name}」的历史记录和输出文件？`)) return;
  await fetchJSON("/api/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, job_ids: entry.id ? [entry.id] : [] }),
  });
  if (state.activeTask && state.activeTask.id === entry.id) resetActiveView();
  toast("已删除");
  refreshTasks(true);
}

async function clearAll() {
  const entries = state.tasks;
  if (!entries.length) return;
  if (!confirm(`清空全部 ${entries.length} 条历史(含输出文件)?`)) return;
  const paths = entries.flatMap((e) => [e.original, e.mono, e.dual].filter(Boolean));
  const jobIds = entries.map((e) => e.id).filter(Boolean);
  await fetchJSON("/api/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, job_ids: jobIds }),
  });
  resetActiveView();
  toast("已清空");
  refreshTasks(true);
}

// ---------------------------------------------------------------------------
// 进度 / 日志 / 结果视图
// ---------------------------------------------------------------------------

function showProgress(t) {
  const card = $("progress-card");
  card.hidden = false;
  $("progress-title").textContent = `正在翻译 · ${t.name || ""}`;
  $("progress-detail").textContent = `${t.stage || ""}${t.detail ? " · " + t.detail : ""} · ${t.progress ?? 0}%`;
  let track = $("progress-track");
  if (!track) {
    track = document.createElement("div");
    track.id = "progress-track";
    track.className = "progress-track";
    const fill = document.createElement("div");
    fill.id = "progress-fill";
    fill.className = "progress-fill";
    track.append(fill);
    $("progress-detail").after(track); // 进度条放在文字下方,不再漂到右侧
  }
  $("progress-fill").style.width = `${t.progress ?? 0}%`;
}

function appendLog(line) {
  const stamp = new Date().toLocaleTimeString();
  state.logLines.push(`[${stamp}] ${line}`);
  if (state.logLines.length > 500) state.logLines.shift();
  const el = $("log");
  // 用户上翻查看历史时不强制拽回底部;贴近底部(40px 内)才跟随滚动
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent = state.logLines.join("\n");
  if (stick) el.scrollTop = el.scrollHeight;
}

function showResultView(view) {
  document.querySelectorAll(".view-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.resultView === view);
  });
  const logMode = view === "log";
  $("log").hidden = !logMode;
  const hasPages = $("translated-preview").childElementCount > 0;
  $("translated-preview").hidden = logMode || !hasPages;
  $("result-empty").hidden = logMode || hasPages || !$("progress-card").hidden;
}

// ---------------------------------------------------------------------------
// SSE(主通道)+ 轮询兜底
// ---------------------------------------------------------------------------

let refreshTimer = null;

function throttledRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refreshTasks(); }, 1000);
}

function onTaskUpdate(task) {
  if (task.id !== state.currentTaskId) return;
  showProgress(task);
  const sig = `${task.stage}|${task.progress}`;
  if (sig !== state.lastLogSig) {
    state.lastLogSig = sig;
    appendLog(`${task.stage} · ${task.progress}%`);
  }
  throttledRefresh();
}

function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("task_update", (ev) => onTaskUpdate(JSON.parse(ev.data).task));
  es.addEventListener("task_done", (ev) => {
    const task = JSON.parse(ev.data).task;
    appendLog(task.status === "done" ? `完成: ${task.name}` : `失败: ${task.error}`);
    toast(task.status === "done" ? `翻译完成: ${task.name}` : `翻译失败: ${task.error || "见运行日志"}`,
          task.status === "done" ? 3000 : 5000);
    $("progress-card").hidden = true;
    state.currentTaskId = null;
    state.lastLogSig = null;
    refreshTasks(true).then(() => {
      const hit = state.tasks.find(
        (t) => (task.mono && t.mono === task.mono) || (task.dual && t.dual === task.dual)
      );
      if (hit && $("translated-preview").childElementCount === 0) openTask(hit);
    });
  });
  es.addEventListener("log", (ev) => JSON.parse(ev.data).lines.forEach(appendLog));
  es.onerror = () => { /* EventSource 自动重连 */ };
}

// SSE 偶发断流时的兜底:每 3 秒对账一次运行状态
function startPolling() {
  setInterval(async () => {
    const data = await fetchJSON("/api/tasks").catch(() => null);
    if (!data) return;
    state.tasks = data.tasks;
    renderTaskList();

    const run = data.tasks.find((t) => t.running);
    if (run && run.task_id === state.currentTaskId) {
      showProgress(run);
      const sig = `${run.stage}|${run.progress}`;
      if (sig !== state.lastLogSig) {
        state.lastLogSig = sig;
        appendLog(`${run.stage} · ${run.progress}%`);
      }
    }
  }, 3000);
}

// ---------------------------------------------------------------------------
// 绑定与启动
// ---------------------------------------------------------------------------

function bindUI() {
  $("new-task").onclick = pickFiles;
  $("pick-pdf").onclick = pickFiles;
  $("file-input").onchange = (e) => { handleFiles(e.target.files); e.target.value = ""; };
  $("save-settings").onclick = saveSettings;
  $("open-output").onclick = () => fetchJSON("/api/open-library", { method: "POST" });
  $("clear-history").onclick = clearAll;
  $("shutdown-service").onclick = shutdownService;
  $("task-search").oninput = (e) => { state.search = e.target.value.trim(); renderTaskList(); };
  document.querySelectorAll(".task-tab").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll(".task-tab").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.filter = b.dataset.filter;
      renderTaskList();
    };
  });
  document.querySelectorAll(".view-tab").forEach((b) => {
    b.onclick = () => showResultView(b.dataset.resultView);
  });

  const zone = $("source-empty");
  const viewer = $("source-viewer");
  for (const el of [zone, viewer]) {
    el.addEventListener("dragover", (e) => e.preventDefault());
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
    });
  }

  $("open-mono").onclick = async () => {
    const target = $("open-mono").dataset.mono;
    if (target) window.open(`/api/file?path=${encodeURIComponent(target)}&download=1`);
  };
  $("open-dual").onclick = async () => {
    const rel = $("open-dual").dataset.rel;
    if (!rel) return;
    toast("正在生成对照版 PDF…", 3000);
    const res = await fetchJSON("/api/side-by-side", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: rel }),
    });
    window.open(`/api/file?path=${encodeURIComponent(res.path)}&download=1`);
  };
  $("open-alt").onclick = () => {
    const rel = $("open-alt").dataset.rel;
    if (rel) window.open(`/api/file?path=${encodeURIComponent(rel)}&download=1`);
  };

  // 关键:滚动发生在内层 .pdf-page-list(#source-preview / #translated-preview),
  // 外层 viewer 不会产生滚动事件
  bindSyncScroll($("source-preview"), $("translated-preview"));
}

async function main() {
  bindUI();
  await initSettings();
  await refreshTasks(true);
  connectSSE();
  startPolling();
  appendLog("工作台就绪。选择 PDF 文件开始翻译。");
}

main();
