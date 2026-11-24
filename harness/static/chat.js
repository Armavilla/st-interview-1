"use strict";

const $ = (id) => document.getElementById(id);
const input = $("message"),
  turns = $("turns"),
  viewport = $("viewport");
let conversation =
  location.hash.slice(1) || localStorage.getItem("ops.conversation") || "";
let busy = false,
  waiting = false,
  poll;
const toolLabels = {
  get_service_status: "查询服务状态",
  search_knowledge_base: "检索运维手册",
  create_incident: "创建工单",
};

function add(parent, tag, text = "", css = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (css) node.className = css;
  parent.append(node);
  return node;
}
function status(text = "", error = false) {
  $("status").textContent = text;
  $("status").className = error ? "error" : "";
}
function controls() {
  $("send").disabled = busy || waiting || !input.value.trim();
  input.disabled = waiting;
  $("newChat").disabled = busy;
  for (const b of document.querySelectorAll(
    "nav button, .approval button, .suggestions button",
  ))
    b.disabled = busy;
}
function remember(id) {
  conversation = id;
  if (id) localStorage.setItem("ops.conversation", id);
  else localStorage.removeItem("ops.conversation");
  history.replaceState(
    null,
    "",
    location.pathname + (id ? "#" + encodeURIComponent(id) : ""),
  );
}
function recent() {
  try {
    return JSON.parse(localStorage.getItem("ops.recent")) || [];
  } catch {
    return [];
  }
}
function sidebar(open) {
  document.body.classList.toggle("nav-open", open);
  $("shade").hidden = !open;
  $("openNav").setAttribute("aria-expanded", String(open));
  if (open) $("closeNav").focus();
}
function menu() {
  $("recent").replaceChildren();
  for (const c of recent()) {
    const button = add($("recent"), "button", c.title);
    button.title = c.title;
    if (c.id === conversation) button.setAttribute("aria-current", "page");
    button.onclick = () =>
      work(async () => {
        sidebar(false);
        await refresh(c.id);
        input.value = "";
        bottom();
      });
  }
}
function bottom() {
  viewport.scrollTop = viewport.scrollHeight;
}
function nearBottom() {
  return (
    viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80
  );
}
function inline(node, text) {
  for (const part of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)) {
    if (part.startsWith("**") && part.endsWith("**"))
      add(node, "strong", part.slice(2, -2));
    else if (part.startsWith("`") && part.endsWith("`") && part.length > 2)
      add(node, "code", part.slice(1, -1));
    else node.append(document.createTextNode(part));
  }
}
function markdown(node, text) {
  let list, table, code;
  for (const line of text.split("\n")) {
    const s = line.trim();
    if (s.startsWith("```")) {
      code = code ? null : add(add(node, "pre"), "code");
      list = table = null;
      continue;
    }
    if (code) {
      code.append(document.createTextNode(line + "\n"));
      continue;
    }
    if (s.startsWith("|") && s.endsWith("|")) {
      const cells = s
        .slice(1, -1)
        .split("|")
        .map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      const first = !table;
      if (first) table = add(add(node, "div", "", "table-wrap"), "table");
      const row = add(table, "tr");
      for (const c of cells) inline(add(row, first ? "th" : "td"), c);
      list = null;
      continue;
    }
    table = null;
    const li = /^([-*]|\d+[.)])\s+(.+)$/.exec(s);
    if (li) {
      const tag = /^\d/.test(li[1]) ? "ol" : "ul";
      if (!list || list.localName !== tag) list = add(node, tag);
      inline(add(list, "li"), li[2]);
      continue;
    }
    list = null;
    if (s)
      inline(
        add(node, /^#{1,6} /.test(s) ? "h3" : "p"),
        s.replace(/^#{1,6} /, ""),
      );
  }
}
function articleFor(objective) {
  $("welcome").hidden = true;
  const article = add(turns, "article");
  add(article, "p", objective, "user");
  const label = add(article, "div", "", "assistant-label");
  label.append(
    $("welcome").querySelector(".signal").cloneNode(true),
    document.createTextNode("运维助手"),
  );
  return article;
}
function observation(item) {
  const o = item.output;
  if (item.name === "get_service_status") {
    const labels = { healthy: "健康", degraded: "降级", down: "不可用" };
    return (
      o.service_name +
      " · " +
      (labels[o.status] || o.status) +
      " · 已持续 " +
      o.duration_minutes +
      " 分钟"
    );
  }
  if (item.name === "create_incident") return "已创建工单 " + o.incident_id;
  return o.matches.length
    ? o.matches.map((m) => m.doc_id + " · " + m.title).join("；")
    : "未找到匹配的手册";
}
function show(run) {
  const article = articleFor(run.objective);
  if (run.reply) markdown(add(article, "div", "", "reply"), run.reply);
  if (run.stop_reason === "approval_rejected")
    add(article, "p", "已拒绝建单申请。");
  if (["failed", "limit_reached"].includes(run.status))
    add(article, "p", "本轮已停止 · " + run.stop_reason, "error");
  if (run.status === "running") add(article, "div", "正在执行…", "progress");
  const evidence = run.observations || [];
  if (evidence.length || ["failed", "limit_reached"].includes(run.status)) {
    const details = add(article, "details");
    add(
      details,
      "summary",
      evidence.length
        ? "工具核实的结果（" + evidence.length + "）"
        : "执行详情",
    );
    for (const item of evidence)
      add(details, "p", observation(item), "observation");
    const a = add(details, "a", "查看执行记录 ↗", "trace");
    a.href = "/runs/" + encodeURIComponent(run.run_id);
    a.target = "_blank";
    a.rel = "noopener";
    details.addEventListener("toggle", () => {
      if (details.open) details.scrollIntoView({ block: "nearest" });
    });
  }
  if (run.pending_action) {
    const box = add(article, "div", "", "approval"),
      args = run.pending_action.arguments;
    add(box, "small", "待批准工单 · " + args.severity);
    add(box, "strong", args.title);
    add(box, "p", args.description);
    for (const [decision, label] of [
      ["approve", "批准建单"],
      ["reject", "拒绝"],
    ]) {
      const button = add(
        box,
        "button",
        label,
        decision === "approve" ? "primary" : "action",
      );
      button.onclick = () =>
        work(async () => {
          const path =
            "/runs/" +
            run.run_id +
            "/approvals/" +
            run.pending_action.action_id;
          try {
            await consume(path, { decision }, article);
          } finally {
            await refresh(conversation);
          }
        });
    }
  }
}
async function check(response) {
  if (response.ok) return;
  const data = await response.json().catch(() => ({}));
  throw Object.assign(
    new Error(
      typeof data.detail === "string"
        ? data.detail
        : "请求失败 (" + response.status + ")",
    ),
    { code: response.status },
  );
}
async function refresh(id) {
  clearTimeout(poll);
  const response = await fetch("/conversations/" + encodeURIComponent(id));
  await check(response);
  const data = await response.json();
  remember(id);
  const chats = recent().filter((c) => c.id !== id);
  chats.unshift({ id, title: data.turns[0].objective.slice(0, 36) });
  localStorage.setItem("ops.recent", JSON.stringify(chats.slice(0, 20)));
  menu();
  turns.replaceChildren();
  data.turns.forEach(show);
  const last = data.turns.at(-1);
  waiting = ["running", "waiting_approval"].includes(last.status);
  status(last.status === "waiting_approval" ? "请先处理上方工单申请" : "");
  if (last.status === "running")
    poll = setTimeout(() => {
      if (!busy && id === conversation) work(() => refresh(id));
    }, 1000);
  controls();
}
async function consume(path, body, article) {
  const progress = add(article, "div", "正在连接…", "progress");
  const draft = add(article, "div", "", "draft reply");
  let draftText = "";
  let frame = 0;
  function renderDraft() {
    frame = 0;
    if (!draft.isConnected) return;
    const follow = nearBottom();
    const fragment = document.createDocumentFragment();
    markdown(fragment, draftText);
    draft.replaceChildren(fragment);
    if (follow) bottom();
  }
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/x-ndjson",
    },
    body: JSON.stringify(body),
  });
  await check(response);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "",
    done = false;
  for (;;) {
    const part = await reader.read();
    if (part.done) break;
    buffer += part.value;
    let end;
    while ((end = buffer.indexOf("\n")) !== -1) {
      const event = JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
      const follow = nearBottom();
      if (event.type === "started") remember(event.conversation_id);
      if (event.type === "model" || event.type === "tool") {
          draftText = "";
        draft.replaceChildren();
        progress.hidden = false;
        progress.textContent =
          event.type === "model"
            ? "正在生成回复…"
            : (toolLabels[event.name] || "执行工具") + "…";
      }
      if (event.type === "delta") {
        progress.hidden = true;
          draftText += event.text;
          if (!frame) frame = requestAnimationFrame(renderDraft);
      }
      if (event.type === "done") {
        remember(event.run.conversation_id);
        done = true;
      }
      if (event.type === "error") throw new Error("执行中断，请刷新查看记录。");
      if (follow) bottom();
    }
  }
  if (!done) throw new Error("连接已断开，请刷新查看执行结果。");
}
async function work(fn) {
  if (busy) return;
  busy = true;
  controls();
  status();
  try {
    await fn();
  } catch (e) {
    status(e.message, true);
  } finally {
    busy = false;
    controls();
  }
}
function send(message) {
  if (!message || busy || waiting) return;
  work(async () => {
    const id = conversation,
      article = articleFor(message);
    input.value = "";
    bottom();
    try {
      await consume(
        id ? "/conversations/" + encodeURIComponent(id) + "/messages" : "/runs",
        id ? { message } : { objective: message },
        article,
      );
    } catch (e) {
      if (conversation) await refresh(conversation);
      else {
        article.remove();
        $("welcome").hidden = false;
      }
      if (!input.value) input.value = message;
      throw e;
    }
    const follow = nearBottom();
    await refresh(conversation);
    if (follow) bottom();
  });
}
$("newChat").onclick = () => {
  clearTimeout(poll);
  sidebar(false);
  remember("");
  waiting = false;
  turns.replaceChildren();
  $("welcome").hidden = false;
  input.value = "";
  status();
  menu();
  controls();
  input.focus();
};
$("openNav").onclick = () => sidebar(true);
$("closeNav").onclick = $("shade").onclick = () => {
  sidebar(false);
  $("openNav").focus();
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("nav-open"))
    $("closeNav").click();
});
document.querySelector("form").onsubmit = (e) => {
  e.preventDefault();
  send(input.value.trim());
};
input.oninput = controls;
input.onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    input.form.requestSubmit();
  }
};
document
  .querySelectorAll("[data-prompt]")
  .forEach((b) => (b.onclick = () => send(b.dataset.prompt)));
work(async () => {
  menu();
  const response = await fetch("/info");
  await check(response);
  const info = await response.json();
  $("backend").textContent =
    info.backend === "deepseek" ? "DeepSeek · " + info.model : "离线演示";
  if (conversation) {
    try {
      await refresh(conversation);
      bottom();
    } catch (e) {
      if (e.code !== 404) throw e;
      remember("");
      menu();
    }
  }
});
