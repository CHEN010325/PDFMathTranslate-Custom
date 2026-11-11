const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const source = readFileSync(
  join(__dirname, "../custom_pdf2zh/webapp/static/app.mjs"), "utf8"
);

function harness() {
  const nodes = new Map();
  const listeners = new Map();
  const history = {
    id: "history", name: "TS-RAG", original: "source.pdf",
    mono: "translation.pdf", translated: true,
  };
  const running = {
    id: "other", task_id: "running", name: "2608.05571v1",
    original: "other.pdf", running: true, progress: 20,
  };
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: true, textContent: "", dataset: {}, style: {},
      replaceChildren() { this.childElementCount = 0; },
    });
    return nodes.get(id);
  };
  let respond;
  const context = {
    window: { addEventListener() {} },
    document: { getElementById: element, querySelectorAll: () => [] },
    EventSource: class {
      addEventListener(name, fn) { listeners.set(name, fn); }
    },
    history, running,
    request: new Promise(resolve => { respond = resolve; }),
    setTimeout: () => 1,
  };
  assert.match(source, /\bmain\(\);\s*$/);
  const app = runInNewContext(source.replace(/\bmain\(\);\s*$/, "") + `
    currentSettings = () => ({});
    fetchJSON = () => request;
    refreshTasks = async () => { state.tasks = [history, running]; };
    renderTaskList = () => {};
    toast = () => {};
    renderPdfList = async (node, path) => {
      node.childElementCount = 1;
      node.dataset.file = path;
    };
    connectSSE();
    ({ openTask, focusRunning, startTranslate, state });
  `, context);
  const emit = (type, payload) => listeners.get(type)({data: JSON.stringify(payload)});
  return { ...app, element, history, running, emit, respond };
}

test("background progress, completion and logs preserve the selected task view", async () => {
  const h = harness();
  h.focusRunning(h.running);
  await h.openTask(h.history);
  h.emit("task_update", {task: {id: "running", progress: 42}});
  h.emit("task_done", {task: {id: "running", status: "done"}});
  h.emit("log", {task_id: "running", lines: ["background tail"]});
  assert.equal(h.element("progress-card").hidden, true);
  assert.equal(h.element("translated-preview").dataset.file, "translation.pdf");
  assert.equal(h.element("translated-preview").hidden, false);
  assert.doesNotMatch(h.element("log").textContent, /background tail/);
  h.focusRunning(h.running);
  assert.equal(h.element("source-title").textContent, "2608.05571v1");
  assert.equal(h.element("open-mono").disabled, true);
  assert.equal(h.element("translated-preview").childElementCount, 0);
  h.emit("task_done", {task: {id: "unrelated", status: "done"}});
  assert.equal(h.element("progress-card").hidden, false);
});

test("completion and tail log arriving before POST response are applied to the new task", async () => {
  const h = harness();
  const pending = h.startTranslate("source.pdf", "TS-RAG");
  h.emit("task_done", {task: {
    id: "fast", name: "TS-RAG", status: "done", mono: h.history.mono,
  }});
  h.emit("log", {task_id: "fast", lines: ["fast task tail"]});
  h.emit("log", {task_id: "unrelated", lines: ["other task tail"]});
  h.respond({task_id: "fast"});
  await pending;
  assert.equal(h.state.currentTaskId, null);
  assert.equal(h.element("progress-card").hidden, true);
  assert.equal(h.element("translated-preview").dataset.file, "translation.pdf");
  assert.match(h.element("log").textContent, /fast task tail/);
  assert.doesNotMatch(h.element("log").textContent, /other task tail/);
  h.emit("log", {task_id: "fast", lines: ["late task tail"]});
  assert.match(h.element("log").textContent, /late task tail/);
  h.focusRunning(h.running);
  await h.openTask(h.history);
  assert.match(h.element("log").textContent, /late task tail/);
});
