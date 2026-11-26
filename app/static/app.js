"use strict";
const $ = (id) => document.getElementById(id);
const make = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const statuses = {
  queued: "排队中",
  running: "入库中",
  ready: "就绪",
  failed: "失败",
};
const stages = {
  queued: "排队",
  start: "开始",
  original: "保存原 PDF",
  mineru: "MinerU 解析",
  artifacts: "保存解析文件",
  split: "按页切分",
  embed_index: "向量化与索引",
  publish: "发布索引",
  metadata: "拟定 Metadata",
  load: "读取已保存的解析结果",
  reindexed: "新索引已启用",
  rollback: "索引已恢复",
  ready: "就绪",
  failed: "失败",
  interrupted: "中断恢复",
};
let active = "",
  tokenVersion = 0,
  selectedDoc = "",
  poll = null,
  notice = null,
  editingDoc = null,
  detailRequest = 0,
  conversation = [],
  currentFields = [],
  editingDefinitions = {};
function message(v) {
  $("note").textContent = v;
  clearTimeout(notice);
  notice = setTimeout(() => ($("note").textContent = ""), 6500);
}
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...opts.headers } };
  if (o.body && !(o.body instanceof FormData)) {
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(o.body);
  }
  const res = await fetch(path, o);
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401 && path !== "/api/login") showLogin();
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : Array.isArray(data.detail)
          ? data.detail.map((e) => (e.loc || []).join(".") + "：" + e.msg).join("；")
        : "请求未通过，请核对输入。",
    );
  }
  return data;
}
function showLogin() {
  clearTimeout(poll);
  active = "";
  conversation = [];
  currentFields = [];
  selectedDoc = "";
  tokenVersion++;
  $("loginScreen").hidden = false;
  $("desk").hidden = true;
  $("logout").hidden = true;
  $("identity").textContent = "";
  $("answer").replaceChildren();
  $("checks").replaceChildren();
  $("metadataDialog").close();
}
function busy(form, on) {
  for (const btn of form.querySelectorAll("button")) btn.disabled = on;
}
function link(text, url) {
  const a = make("a", text);
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  return a;
}
async function enter() {
  const user = await api("/api/me");
  $("identity").textContent = user.username;
  $("loginScreen").hidden = true;
  $("desk").hidden = false;
  $("logout").hidden = false;
  await loadKB();
}
async function loadKB(prefer) {
  const items = await api("/api/kbs");
  $("kb").replaceChildren();
  if (!items.length) {
    const opt = make("option", "先创建一个知识库");
    opt.value = "";
    $("kb").append(opt);
  }
  for (const k of items) {
    const opt = make("option", k.name);
    opt.value = k.id;
    $("kb").append(opt);
  }
  if (prefer) $("kb").value = prefer;
  switchKB();
}
function switchKB() {
  clearTimeout(poll);
  active = $("kb").value;
  conversation = [];
  currentFields = [];
  tokenVersion++;
  selectedDoc = "";
  $("answer").replaceChildren();
  $("detail").replaceChildren();
  $("indexJobs").replaceChildren();
  $("indexSummary").textContent = "";
  $("reindexKB").disabled = !active;
  for (const id of ["fieldChoice", "fieldValues", "filterConditions"]) $(id).value = "";
  $("fieldOperator").replaceChildren();
  $("fieldDescription").textContent = "";
  $("documents").replaceChildren();
  busy($("askForm"), !active);
  busy($("uploadForm"), !active);
  if (active) refreshDocs().catch((e) => message(e.message));
}
$("kb").onchange = switchKB;
$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  busy(e.target, true);
  try {
    await api("/api/login", {
      method: "POST",
      body: { username: $("username").value, password: $("password").value },
    });
    $("password").value = "";
    await enter();
  } catch (err) {
    message(err.message);
  } finally {
    busy(e.target, false);
  }
};
$("logout").onclick = async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showLogin();
  } catch (e) {
    message(e.message);
  }
};
$("kbForm").onsubmit = async (e) => {
  e.preventDefault();
  busy(e.target, true);
  try {
    const k = await api("/api/kbs", {
      method: "POST",
      body: { name: $("kbName").value },
    });
    $("kbName").value = "";
    await loadKB(k.id);
  } catch (err) {
    message(err.message);
  } finally {
    busy(e.target, false);
  }
};
$("uploadForm").onsubmit = async (e) => {
  e.preventDefault();
  const v = tokenVersion;
  busy(e.target, true);
  try {
    const f = $("pdf").files[0];
    if (f.size > 50 * 1024 * 1024) throw Error("每份 PDF 最多 50 MiB。");
    const form = new FormData();
    form.append("file", f);
    const r = await api("/api/kbs/" + active + "/documents", {
      method: "POST",
      body: form,
    });
    if (v !== tokenVersion) return;
    message(
      r.duplicate
        ? "该文件已在本库，已显示现有任务。"
        : "已加入队列，可以在详情中查看每个阶段。",
    );
    selectedDoc = r.id;
    $("pdf").value = "";
    await refreshDocs();
  } catch (err) {
    message(err.message);
  } finally {
    if (v === tokenVersion) busy(e.target, false);
  }
};
async function refreshDocs() {
  clearTimeout(poll);
  const kb = active,
    v = tokenVersion;
  if (!kb) return;
  const [docs, jobs, meta] = await Promise.all([
    api("/api/kbs/" + kb + "/documents"),
    api("/api/kbs/" + kb + "/index-jobs"),
    api("/api/kbs/" + kb + "/metadata"),
  ]);
  if (v !== tokenVersion) return;
  const pending = jobs.some((j) => ["queued", "running"].includes(j.status));
  $("reindexKB").disabled = pending || !docs.some((d) => d.status === "ready");
  $("indexSummary").textContent = meta.agent_documents + " / " + meta.ready_documents + " 份就绪文档已更新字段目录";
  updateFieldChoices(meta.filterable_fields || []);
  $("indexJobs").replaceChildren();
  for (const job of jobs.filter((j) => j.status !== "ready").slice(0, 6)) {
    const el = make("div", job.filename + " · " + statuses[job.status] + " · " + (stages[job.stage] || job.stage), "job");
    if (job.error) el.append(make("p", job.error, "fail"));
    if (job.status === "failed") {
      const retry = make("button", "重试索引", "quiet");
      retry.onclick = async () => {
        retry.disabled = true;
        try { await api("/api/index-jobs/" + job.id + "/retry", {method: "POST"}); await refreshDocs(); }
        catch (e) {message(e.message); retry.disabled = false;}
      };
      el.append(retry);
    }
    $("indexJobs").append(el);
  }
  const panel = $("documents");
  panel.replaceChildren();
  if (!docs.length) panel.append(make("small", "这个知识库还没有文档。"));
  for (const d of docs) {
    const row = make("div", undefined, "doc"),
      left = make("div");
    left.append(
      make("strong", d.filename),
      make("br"),
      make("span", statuses[d.status], "pill"),
      make(
        "small",
        (stages[d.stage] || d.stage) + " · " + d.n_chunks + " 个片段 · 索引 v" + d.index_version,
      ),
    );
    const b = make("button", "详情", "quiet");
    b.onclick = () => {
      selectedDoc = d.id;
      $("detail").replaceChildren(make("small", "正在读取文档详情…"));
      detail(d.id, v).catch((e) => message(e.message));
    };
    row.append(left, b);
    panel.append(row);
  }
  if (selectedDoc) await detail(selectedDoc, v);
  if (
    v === tokenVersion &&
    (pending || docs.some((d) => ["queued", "running"].includes(d.status)))
  )
    poll = setTimeout(
      () => refreshDocs().catch((e) => message(e.message)),
      2500,
    );
}
async function detail(id, v) {
  const requestNo = ++detailRequest;
  const d = await api("/api/documents/" + id);
  if (v !== tokenVersion || selectedDoc !== id || requestNo !== detailRequest) return;
  const panel = make("div", undefined, "trace");
  panel.append(make("h3", d.filename));
  const last = d.events.slice(-12);
  panel.append(
    make(
      "small",
      last
        .map((x) => "#" + x.attempt + " " + (stages[x.stage] || x.stage))
        .join(" → "),
    ),
  );
  if (d.error) panel.append(make("p", d.error, "fail"));
  if (d.status === "failed") {
    const b = make("button", "重试这个任务", "second");
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api("/api/documents/" + id + "/retry", { method: "POST" });
        await refreshDocs();
      } catch (e) {
        message(e.message);
        b.disabled = false;
      }
    };
    panel.append(b);
  }
  if (d.status === "ready") {
    const meta = make("button", "查看 / 编辑 Metadata", "second");
    meta.onclick = () => openMetadata(d);
    panel.append(meta);
  }
  if (d.files.length) {
    const group = make("div", undefined, "links");
    for (const n of d.files) {
      const dest =
        "/api/documents/" +
        id +
        "/files/" +
        n.split("/").map(encodeURIComponent).join("/");
      group.append(link(n, dest));
    }
    panel.append(group);
  }
  $("detail").replaceChildren(panel);
}
for (const [id, models] of [
  ["check", false],
  ["warm", true],
])
  $(id).onclick = async () => {
    $("check").disabled = $("warm").disabled = true;
    $("checks").replaceChildren(
      make(
        "small",
        models ? "正在计算，首次下载模型需要时间…" : "正在测试服务…",
      ),
    );
    try {
      const result = await api("/api/checks?models=" + models, {
        method: "POST",
      });
      $("checks").replaceChildren();
      for (const [key, val] of Object.entries(result))
        $("checks").append(
          make(
            "div",
            key + " · " + val.detail,
            val.ok === false ? "check fail" : "check",
          ),
        );
    } catch (e) {
      message(e.message);
    } finally {
      $("check").disabled = $("warm").disabled = false;
    }
  };
$("askForm").onsubmit = async (e) => {
  e.preventDefault();
  const v = tokenVersion,
    mode = e.submitter?.value || "search";
  busy(e.target, true);
  const block = $("answer");
  block.replaceChildren(make("small", "正在解析意图、筛选与检索…"));
  try {
    const r = await api("/api/kbs/" + active + "/ask", {
      method: "POST",
      body: { mode, answer_engine: $("answerEngine").value, question: $("question").value, filters: queryFilters(), planner: $("planner").value, chat_history: conversation },
    });
    if (v !== tokenVersion) return;
    conversation.push({role: "user", content: $("question").value.slice(0,2000)});
    if (r.answer) conversation.push({role: "assistant", content: r.answer.slice(0,2000)});
    conversation = conversation.slice(-10);
    block.replaceChildren(
      make("h3", r.status === "retrieved" ? "检索片段" : "回答"),
    );
    if (r.intent) block.append(planView(r.intent, r.retrieval_trace));
    if (r.agent_trace) {
      const trace = make("details");
      trace.append(make("summary", "Agent 执行步骤"), make("pre", JSON.stringify({steps:r.agent_trace, crew:r.crew_trace || [], review:r.review || "未启用独立核验"}, null, 2)));
      block.append(trace);
    }
    if (r.answer) block.append(make("div", r.answer, "answertext"));
    r.sources.forEach((s, i) => {
      const el = make("div", undefined, "cite");
      el.append(
        make(
          "strong",
          "[" +
            (s.number || i + 1) +
            "] " +
            s.filename +
            " · " +
            (s.page ? "第 " + s.page + " 页" : "页码未知"),
        ),
        make("div", s.quote || s.text, "quote"),
      );
      const links = make("div", undefined, "links");
      links.append(link("打开原 PDF ↗", s.source_url));
      s.image_urls.forEach((url, n) =>
        links.append(link("同页图片 " + (n + 1), url)),
      );
      el.append(
        links,
        make(
          "small",
          "片段 " +
            s.id.split("_").at(-1) +
            " · 向量分 " +
            s.distance.toFixed(3) +
            " · 重排分 " +
            s.rerank.toFixed(3),
        ),
      );
      if (s.metadata) el.append(make("small", "类型 " + s.metadata.doc_type + " · 版本 " + (s.metadata.version || "未知") + " · " + (s.metadata.section || s.metadata.title)));
      block.append(el);
    });
  } catch (err) {
    if (v === tokenVersion)
      block.replaceChildren(make("p", err.message, "fail"));
  } finally {
    if (v === tokenVersion) busy(e.target, false);
  }
};
function updateFieldChoices(fields) {
  const selected = $("fieldChoice").value;
  currentFields = fields;
  $("fieldChoice").replaceChildren();
  const any = make("option", "不添加手动条件"); any.value = ""; $("fieldChoice").append(any);
  for (const f of fields) {const o = make("option", (f.label || f.field) + " · " + f.field); o.value=f.field; $("fieldChoice").append(o);}
  if (fields.some(f=>f.field===selected)) $("fieldChoice").value=selected;
  updateOperators();
}
function updateOperators() {
  const previous = $("fieldOperator").value;
  const field = currentFields.find(f=>f.field===$("fieldChoice").value);
  $("fieldOperator").replaceChildren();
  for (const op of field?.operators || []) {const o=make("option",op);o.value=op;$("fieldOperator").append(o);}
  if (field?.operators.includes(previous)) $("fieldOperator").value=previous;
  $("fieldDescription").textContent=field ? field.description + "；示例：" + field.examples.slice(0,6).join("、") : "";
}
$("fieldChoice").onchange=updateOperators;
$("clearHistory").onclick=()=>{conversation=[];message("已清空本次对话上下文。");};
function queryFilters() {
  let f={logic:"and",conditions:[]};
  const raw=$("filterConditions").value.trim();
  if(raw) {
    try {f=JSON.parse(raw);} catch {throw new Error("高级条件需为有效 JSON。");}
    if(!f || !Array.isArray(f.conditions)) throw new Error("高级条件需包含 conditions 数组。");
  }
  if($("fieldChoice").value) {
    const values=$("fieldValues").value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
    if(!values.length) throw new Error("请填写匹配值。");
    f.conditions.push({field:$("fieldChoice").value,operator:$("fieldOperator").value,values});
  }
  return f.conditions.length ? f : {};
}
const intentLabels = {lookup: "资料查询", comparison: "对比", calculation: "计算", policy_lookup: "制度适用查询", general: "一般检索"};
function planView(plan, trace) {
  const box = make("details", undefined, "intent");
  box.append(make("summary", "检索计划 · " + (intentLabels[plan.intent] || plan.intent) + " · " + plan.planner));
  if (plan.metadata_catalog) box.append(make("small", "字段来源：Milvus · " + (plan.metadata_catalog.cache_hit ? "缓存命中" : "已读取当前索引") + " · " + plan.metadata_catalog.indexed_documents + " 份文档"));
  if (plan.search_query) box.append(make("p", "理解后的问题：" + plan.search_query));
  if (plan.needs_clarification) box.append(make("p", plan.clarifying_question, "hint"));
  box.append(make("p", "手动条件：" + JSON.stringify(plan.explicit_filters)));
  for (const r of plan.routes) box.append(make("p", r.name + "：" + JSON.stringify(r.inferred)));
  for (const text of plan.warnings || []) box.append(make("p", text, "hint"));
  if (trace) {
    const details = make("details");
    details.append(make("summary", "Milvus 筛选条件与候选数"), make("pre", JSON.stringify(trace, null, 2)));
    box.append(details);
  }
  return box;
}
$("previewIntent").onclick = async () => {
  if (!$("question").value.trim() || !active) return message("先选择知识库并输入问题。");
  const v = tokenVersion;
  busy($("askForm"), true);
  try {
    const p = await api("/api/kbs/" + active + "/intent", {method: "POST", body: {question: $("question").value, filters: queryFilters(), planner: $("planner").value, chat_history: conversation}});
    if (v !== tokenVersion) return;
    const view = planView(p); view.open = true; $("answer").replaceChildren(view);
  } catch(e) {message(e.message);} finally {if (v === tokenVersion) busy($("askForm"), false);}
};
$("reindexKB").onclick = async () => {
  const v = tokenVersion;
  $("reindexKB").disabled = true;
  try {
    const r = await api("/api/kbs/" + active + "/reindex", {method: "POST"});
    if (v !== tokenVersion) return;
    message(r.jobs.length + " 份文档已加入索引队列，旧索引在完成前继续可用。");
    await refreshDocs();
  } catch(e) {message(e.message); $("reindexKB").disabled = false;}
};
async function openMetadata(d) {
  editingDoc = d;
  editingDefinitions = d.metadata_provenance?.field_definitions || {};
  $("metadataTitle").textContent = d.filename;
  $("metadataJSON").value = JSON.stringify(d.metadata, null, 2);
  $("metadataEvidence").textContent = JSON.stringify(d.metadata_provenance, null, 2);
  $("rollbackControls").replaceChildren();
  if (d.index_history.length) {
    const label = make("label", "恢复已保留的索引");
    const select = make("select");
    for (const h of d.index_history.filter((h) => h.revision !== d.index_revision)) {
      const opt = make("option", "v" + h.index_version + " · " + (h.revision.slice(0, 8) || "初始版本") + " · " + h.n_chunks + " 个片段");
      opt.value = h.revision; select.append(opt);
    }
    const b = make("button", "恢复所选索引", "quiet"); b.type = "button";
    b.disabled = !select.options.length;
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api("/api/documents/" + d.id + "/index/rollback", {method: "POST", body: {revision: select.value, expected_revision: d.index_revision}});
        $("metadataDialog").close(); message("已恢复所选索引。"); await refreshDocs();
      } catch(e) {message(e.message); b.disabled = false;}
    };
    label.append(select); $("rollbackControls").append(label, b);
  }
  $("metadataDialog").showModal();
}
$("closeMetadata").onclick = () => $("metadataDialog").close();
async function proposeMetadata(method) {
  busy($("metadataForm"), true);
  try {
    const p = await api("/api/documents/" + editingDoc.id + "/metadata/propose", {method: "POST", body: {method}});
    editingDefinitions = p.field_definitions || {};
    $("metadataJSON").value = JSON.stringify(p.metadata, null, 2);
    $("metadataEvidence").textContent = JSON.stringify(p, null, 2);
    message("提案已显示，请检查后保存。");
  } catch(e) {message(e.message);} finally {busy($("metadataForm"), false);}
}
$("proposeLLM").onclick = () => proposeMetadata("llm");
$("metadataForm").onsubmit = async (e) => {
  e.preventDefault(); busy(e.target, true);
  try {
    const metadata = JSON.parse($("metadataJSON").value);
    await api("/api/documents/" + editingDoc.id + "/metadata", {method: "PUT", body: {metadata, field_definitions: editingDefinitions, expected_revision: editingDoc.index_revision}});
    $("metadataDialog").close(); message("Metadata 已加入重新索引队列。"); await refreshDocs();
  } catch(e) {message(e.message);} finally {busy(e.target, false);}
};
enter().catch(() => showLogin());

$("refreshMetadata").onclick = async () => {
  if (!active) return message("先选择知识库。");
  const kb = active, version = tokenVersion;
  try {
    const data = await api("/api/kbs/" + kb + "/metadata?refresh=true");
    if (version !== tokenVersion || kb !== active) return;
    updateFieldChoices(data.filterable_fields || []);
    message("已从 Milvus 刷新 " + data.filterable_fields.length + " 个字段。");
  } catch(e) { message(e.message); }
};
