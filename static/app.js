/* SysAgent 控制台前端（无依赖） */
"use strict";

const S = {
  ws: null,
  view: "overview",
  status: null,
  config: null,
  projects: [],
  logs: {},            // project -> [{t, file, line}]
  currentProject: null,
  logPaused: false,
  logFilter: "",
  alerts: [],
  alertUnseen: 0,
  pendingApprovals: {}, // id -> item
  pendingCaps: {},      // id -> item（待审批能力提案）
  appliedCaps: [],
  memory: { items: [], stats: {}, kind: "", query: "" },
  models: { active: "", providers: [] },
  hist: { cpu: [], mem: [], rx: [], tx: [] },
  statusTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const MAX_HIST = 180;
const MAX_LOG = 3000;

/* ================= 工具 ================= */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function mdLite(text) {
  const parts = String(text).split("```");
  return parts.map((p, i) => {
    if (i % 2 === 1) {
      const body = p.replace(/^[^\n]*\n/, "");
      return "<pre>" + esc(body.replace(/\n$/, "")) + "</pre>";
    }
    let h = esc(p);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    return h;
  }).join("");
}
function fmtBytes(n, digits = 1) {
  if (n == null) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : digits) + " " + u[i];
}
function fmtRate(n) {
  if (n < 1024) return n.toFixed(0) + " B/s";
  return fmtBytes(n, 1) + "/s";
}
function fmtUptime(sec) {
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (d > 0) return `${d}天${h}小时`;
  if (h > 0) return `${h}小时${m}分`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}
function fmtTs(t) {
  if (!t) return "";
  // ISO 本地时间字符串截取到时分秒
  const str = String(t);
  const m = str.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : str;
}
function lineLevel(line) {
  if (/\b(ERROR|FATAL|Traceback|Exception)\b/i.test(line)) return "err";
  if (/\bWARN(ING)?\b/i.test(line)) return "warn";
  return "";
}

/* ================= WebSocket ================= */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => setConn(true);
  ws.onclose = () => {
    setConn(false);
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
}
function setConn(ok) {
  $("#conn-dot").classList.toggle("ok", ok);
  $("#conn-text").textContent = ok ? "已连接" : "重连中…";
}
function send(obj) {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
  else alert("连接已断开，正在重连，请稍后重试");
}

/* ================= 事件处理 ================= */
function handle(ev) {
  switch (ev.type) {
    case "hello": onHello(ev); break;
    case "metrics": onMetrics(ev); break;
    case "line": onLine(ev); break;
    case "alert": onAlert(ev); break;
    case "chat": onChat(ev); break;
    case "tool": onTool(ev); break;
    case "chat_error": onChat(ev); break;
    case "approval": onApproval(ev); break;
    case "capability": onCapability(ev); break;
    case "llm_switched":
      $("#foot-llm").textContent = `LLM: ${ev.name || ev.active} · ${ev.model || ""}`;
      if (S.view === "settings") loadModels();
      break;
    case "logmon": if (ev.action === "project_added") refreshProjects(); break;
    case "log_error": addLogLine(ev.project, { t: ev.ts, file: ev.file || "?", line: "[监听错误] " + ev.message }, true); break;
  }
}

function onCapability(ev) {
  const a = ev.capability;
  if (ev.action === "added") {
    S.pendingCaps[a.id] = a;
    renderCapabilityProposal(a);
    updateBadges();
    switchView("chat");
  } else {
    delete S.pendingCaps[a.id];
    updateBadges();
    const card = $(`.capability[data-id="${cssEscape(a.id)}"]`);
    if (card) {
      const label = { applied: "✓ 已批准，能力已生效", rejected: "✗ 已拒绝" }[ev.action] || ev.action;
      card.querySelector(".row").innerHTML = `<span class="done">${label}</span>`;
    }
    if (ev.action === "applied") {
      S.appliedCaps.push({ ...a.payload, kind: a.kind });
      refreshProjects();
      loadMemoryView();
      appendChat("assistant", `✅ 新能力已生效：${a.description || ""}。现在你可以让我使用它了。`);
    }
  }
}

function onHello(ev) {
  S.status = ev.status;
  S.config = ev.config;
  S.projects = ev.projects || [];
  S.alerts = ev.alerts || [];
  // 用状态历史填充趋势图
  (ev.status.history || []).forEach((h) => {
    pushHist("cpu", h.cpu); pushHist("mem", h.mem);
    pushHist("rx", h.net_rx); pushHist("tx", h.net_tx);
  });
  // 对话历史回放
  if ((ev.history || []).length > 0) {
    $("#chat-list").innerHTML = "";
    ev.history.forEach((m) => appendChat(m.role, m.content));
  }
  // 待审批
  (ev.pending_approvals || []).forEach((a) => renderApproval(a));
  (ev.pending_capabilities || []).forEach((a) => renderCapabilityProposal(a));
  S.appliedCaps = ev.applied_capabilities || [];
  S.memory.stats = ev.memory_stats || {};
  // 各项目近况
  S.projects.forEach((name) => {
    fetch(`/api/projects/${encodeURIComponent(name)}/lines?n=200`)
      .then((r) => r.json())
      .then((rows) => {
        S.logs[name] = rows.map((r) => ({ t: r.ts, file: r.file, line: r.line }));
        renderProjects();
        if (S.currentProject === name) renderLogView();
      })
      .catch(() => {});
  });
  renderAll();
}

function onMetrics(ev) {
  pushHist("cpu", ev.cpu); pushHist("mem", ev.mem);
  pushHist("rx", ev.net_rx); pushHist("tx", ev.net_tx);
  renderKpis(ev);
  renderTopbar(ev);
  drawAllCharts();
}

function onLine(ev) {
  addLogLine(ev.project, { t: ev.ts, file: ev.file, line: ev.line });
}

function onAlert(ev) {
  S.alerts.push(ev);
  if (S.alerts.length > 500) S.alerts.shift();
  if (S.view !== "alerts") {
    S.alertUnseen++;
    updateBadges();
  }
  $("#tb-alerts-last").innerHTML =
    `<span style="color:${ev.level === "error" ? "var(--err)" : "var(--warn)"}">` +
    `⚠ ${esc(ev.project)}: ${esc((ev.line || "").slice(0, 80))}</span>`;
  if (S.view === "alerts") renderAlerts();
  renderProjects();
}

function onChat(ev) {
  if (ev.type === "chat_error") {
    appendChat("error", ev.content || "未知错误");
    return;
  }
  appendChat(ev.role, ev.content);
}

function onTool(ev) {
  if (ev.action === "start") {
    const el = document.createElement("details");
    el.className = "toolrow";
    el.dataset.id = ev.id;
    el.innerHTML =
      `<summary>🔧 ${esc(ev.name)} <span class="mono" style="color:var(--ink-faint)">${esc(JSON.stringify(ev.args || {}).slice(0, 80))}</span>` +
      `<span class="st run">执行中…</span></summary><div class="body"></div>`;
    $("#chat-list").appendChild(el);
    scrollChat();
  } else if (ev.action === "done") {
    const el = $(`.toolrow[data-id="${cssEscape(ev.id)}"]`);
    if (el) {
      const st = el.querySelector(".st");
      st.className = "st " + (ev.status === "ok" ? "ok" : "bad");
      st.textContent = ev.status === "ok" ? "完成" : "失败";
      el.querySelector(".body").textContent = ev.output || "";
      el.open = ev.status !== "ok";
    }
    scrollChat();
  } else if (ev.action === "await_approval") {
    const el = $(`.toolrow[data-id="${cssEscape(ev.id)}"]`);
    if (el) {
      const st = el.querySelector(".st");
      st.className = "st";
      st.style.color = "var(--warn)";
      st.textContent = "等待审批";
    }
  }
}

function onApproval(ev) {
  const a = ev.approval;
  if (ev.action === "added") {
    S.pendingApprovals[a.id] = a;
    renderApproval(a);
    updateBadges();
    switchView("chat");
  } else {
    delete S.pendingApprovals[a.id];
    updateBadges();
    const card = $(`.approval[data-id="${cssEscape(a.id)}"]`);
    if (card) {
      const label = { approved: "✓ 已批准，执行中…", rejected: "✗ 已拒绝", used: "✓ 已执行", }[a.status] || a.status;
      card.querySelector(".row").innerHTML = `<span class="done">${label}</span>`;
    }
  }
}

/* ================= 渲染：概览 ================= */
function pushHist(key, v) {
  const arr = S.hist[key];
  arr.push(v);
  if (arr.length > MAX_HIST) arr.shift();
}
function renderAll() {
  if (S.status) { renderFullStatus(S.status); }
  renderProjects();
  renderAlerts();
  renderSettings();
  updateBadges();
  drawAllCharts();
}
function renderFullStatus(s) {
  const mem = s.memory;
  const topDisk = (s.disks || []).slice().sort((a, b) => b.percent - a.percent)[0];
  setKpi("cpu", s.cpu.percent, s.cpu.percent);
  $("#kpi-cpu-sub").textContent = `逻辑核 ${s.cpu.cores_logical}` +
    (s.cpu.load_avg ? ` · 负载 ${s.cpu.load_avg.map((x) => x.toFixed(2)).join(" ")}` : "");
  setKpi("mem", mem.percent, mem.percent);
  $("#kpi-mem-sub").textContent = `可用 ${fmtBytes(mem.available)} / 共 ${fmtBytes(mem.total)}`;
  if (topDisk) {
    setKpi("disk", topDisk.percent, topDisk.percent);
    $("#kpi-disk-sub").textContent = `${topDisk.mountpoint} · 余 ${fmtBytes(topDisk.free)}`;
  }
  renderKpis({ net_rx: s.network.recv_rate, net_tx: s.network.send_rate });
  renderTopbar({ cpu: s.cpu.percent, mem: mem.percent });
  $("#foot-host").textContent = `${s.platform.hostname} · ${s.platform.system}`;
  renderProcs(s.top_processes);
  renderProjectChips();
}
function setKpi(id, val, pct) {
  $("#kpi-" + id).innerHTML = `${val == null ? "—" : Number(val).toFixed(1)}<small>%</small>`;
  const bar = $("#bar-" + id);
  bar.style.width = (pct || 0) + "%";
  bar.classList.toggle("hot", pct >= 85);
}
function renderKpis(ev) {
  if (ev.net_rx != null) {
    $("#kpi-net").innerHTML =
      `<span style="font-size:18px">↓${fmtRate(ev.net_rx)}</span> <span style="font-size:18px;color:var(--ink-dim)">↑${fmtRate(ev.net_tx)}</span>`;
  }
}
function renderTopbar(ev) {
  if (ev.cpu != null) $("#tb-cpu").textContent = ev.cpu.toFixed(0) + "%";
  if (ev.mem != null) $("#tb-mem").textContent = ev.mem.toFixed(0) + "%";
  if (S.status) $("#tb-uptime").textContent = fmtUptime(S.status.uptime_seconds);
}
function renderProcs(list) {
  const body = $("#proc-body");
  if (!list || !list.length) { body.innerHTML = `<tr><td colspan="4" class="empty">无数据</td></tr>`; return; }
  body.innerHTML = list.map((p) =>
    `<tr><td class="mono">${p.pid}</td><td>${esc(p.name)}</td>` +
    `<td class="num">${p.cpu_percent.toFixed(1)}</td><td class="num">${p.memory_percent.toFixed(1)}</td></tr>`
  ).join("");
}
function renderProjects() {
  const el = $("#proj-list");
  if (!S.projects.length) {
    el.innerHTML = `<div class="empty">未配置监控项目（config.yaml → monitor.projects）</div>`;
    return;
  }
  el.innerHTML = S.projects.map((name) => {
    const rows = S.logs[name] || [];
    const errs = rows.filter((r) => lineLevel(r.line) === "err").length;
    const last = rows[rows.length - 1];
    const dotCls = errs > 0 ? "err" : (rows.length ? "" : "idle");
    const spec = (S.config && S.config.projects || []).find((p) => p.name === name) || {};
    const desc = spec.description || (spec.log_files || []).map((f) => f.split(/[\\/]/).pop()).join(", ");
    return `<div class="proj-card">
      <span class="dot ${dotCls}"></span>
      <div><div class="name">${esc(name)}</div><div class="desc">${esc(desc)}</div></div>
      <div class="meta">${rows.length ? `末行 ${fmtTs(last.t)}` : "无日志"}<br>` +
      (errs ? `<span style="color:var(--err)">近期错误 ${errs} 行</span>` : `<span style="color:var(--ok)">近期无错误</span>`) +
      `</div></div>`;
  }).join("");
}

/* ================= 图表 ================= */
function drawChart(canvas, series, opts = {}) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const padL = 4, padR = 4, padT = 22, padB = 4;
  const iw = w - padL - padR, ih = h - padT - padB;

  let max = opts.max || 0;
  series.forEach((s) => s.data.forEach((v) => { if (v > max) max = v; }));
  if (!opts.max) max = max * 1.15 || 1;
  if (opts.max && max < opts.max) max = opts.max;

  // 网格
  ctx.strokeStyle = "rgba(148,163,184,.12)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = padT + (ih * i) / 3;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
  }

  series.forEach((s) => {
    const data = s.data;
    if (data.length < 2) return;
    const n = data.length;
    // 序列右对齐：最后一个点在最右
    const startX = padL + iw - (iw * (n - 1)) / (MAX_HIST - 1);
    const yAt = (v) => padT + ih - (ih * v) / max;
    // 面积
    ctx.beginPath();
    ctx.moveTo(startX, yAt(data[0]));
    for (let i = 1; i < n; i++) {
      const x = startX + (iw * i) / (MAX_HIST - 1);
      ctx.lineTo(x, yAt(data[i]));
    }
    ctx.lineTo(startX + (iw * (n - 1)) / (MAX_HIST - 1), padT + ih);
    ctx.lineTo(startX, padT + ih);
    ctx.closePath();
    ctx.fillStyle = s.color.replace(/^rgb\((.+)\)$/, "rgba($1,0.13)");
    ctx.fill();
    // 线
    ctx.beginPath();
    ctx.moveTo(startX, yAt(data[0]));
    for (let i = 1; i < n; i++) {
      const x = startX + (iw * i) / (MAX_HIST - 1);
      ctx.lineTo(x, yAt(data[i]));
    }
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // 最新值
    const lastY = yAt(data[n - 1]);
    ctx.beginPath();
    ctx.arc(startX + (iw * (n - 1)) / (MAX_HIST - 1), lastY, 2.5, 0, Math.PI * 2);
    ctx.fillStyle = s.color;
    ctx.fill();
  });
  // 顶部当前值
  const last = series[0].data[series[0].data.length - 1];
  if (last != null) {
    ctx.font = "600 11px ui-monospace, monospace";
    ctx.fillStyle = series[0].color;
    ctx.textAlign = "right";
    ctx.fillText(opts.fmt ? opts.fmt(last) : last.toFixed(1), w - padR, 12);
  }
}
function drawAllCharts() {
  drawChart($("#chart-cpu"), [{ data: S.hist.cpu, color: "rgb(56,189,248)" }], { max: 100 });
  drawChart($("#chart-mem"), [{ data: S.hist.mem, color: "rgb(167,139,250)" }], { max: 100 });
  drawChart($("#chart-net"), [
    { data: S.hist.rx, color: "rgb(52,211,153)" },
    { data: S.hist.tx, color: "rgb(251,191,36)" },
  ], { fmt: (v) => fmtRate(v) });
}

/* ================= 渲染：对话 ================= */
function appendChat(role, content) {
  const list = $("#chat-list");
  const el = document.createElement("div");
  if (role === "user") {
    el.className = "msg user";
    el.innerHTML = `<div class="who">你</div><div class="bubble">${esc(content)}</div>`;
  } else if (role === "error") {
    el.className = "msg error";
    el.innerHTML = `<div class="who">错误</div><div class="bubble">${esc(content)}</div>`;
  } else {
    el.className = "msg assistant";
    el.innerHTML = `<div class="who">SysAgent</div><div class="bubble">${mdLite(content)}</div>`;
  }
  list.appendChild(el);
  scrollChat();
}
function scrollChat() {
  const box = $("#chat-scroll");
  box.scrollTop = box.scrollHeight;
}
function renderApproval(a) {
  const list = $("#chat-list");
  if ($(`.approval[data-id="${cssEscape(a.id)}"]`)) return;
  const el = document.createElement("div");
  el.className = "approval";
  el.dataset.id = a.id;
  el.innerHTML =
    `<div class="t">⚠ 需要你批准</div>` +
    `<div class="d">${esc(a.description || a.tool)}</div>` +
    `<div class="row">` +
    `<button class="btn primary" data-act="approve">✓ 批准执行</button>` +
    `<button class="btn danger" data-act="reject">✗ 拒绝</button>` +
    `</div>`;
  el.addEventListener("click", (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    el.querySelectorAll("button").forEach((b) => (b.disabled = true));
    send({ type: act, id: a.id });
  });
  list.appendChild(el);
  scrollChat();
}
function renderCapabilityProposal(a) {
  const list = $("#chat-list");
  if ($(`.capability[data-id="${cssEscape(a.id)}"]`)) return;
  const el = document.createElement("div");
  el.className = "approval capability";
  el.dataset.id = a.id;
  el.innerHTML =
    `<div class="t">🧬 能力进化提案 · ${esc((a.kind || "").replace("_", " "))}</div>` +
    `<div class="d">${esc(a.description || "")}` +
    (a.reason ? `<br><span style="color:var(--ink-faint);font-size:12px">理由：${esc(a.reason)}</span>` : "") +
    `</div>` +
    `<div class="row">` +
    `<button class="btn primary" data-act="cap_approve">✓ 批准进化</button>` +
    `<button class="btn danger" data-act="cap_reject">✗ 拒绝</button>` +
    `</div>`;
  el.addEventListener("click", (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    el.querySelectorAll("button").forEach((b) => (b.disabled = true));
    send({ type: act, id: a.id });
  });
  list.appendChild(el);
  scrollChat();
}
function updateBadges() {
  const nAppr = Object.keys(S.pendingApprovals).length;
  const b1 = $("#badge-approval");
  b1.hidden = nAppr === 0;
  b1.textContent = nAppr;
  const b2 = $("#badge-alert");
  b2.hidden = S.alertUnseen === 0;
  b2.textContent = S.alertUnseen;
  const nCap = Object.keys(S.pendingCaps).length;
  const b3 = $("#badge-cap");
  b3.hidden = nCap === 0;
  b3.textContent = nCap;
}

/* ================= 渲染：记忆 / 能力库 ================= */
function refreshProjects() {
  fetch("/api/config").then((r) => r.json()).then((c) => {
    S.config = c;
    if (Array.isArray(c.projects)) {
      // 服务端 projects 为对象列表；同步监听名列表
      const names = c.projects.map((p) => p.name);
      if (names.length) S.projects = names;
    }
    renderProjects();
    renderProjectChips();
  }).catch(() => {});
}
function loadMemoryView() {
  const q = S.memory.query ? `&q=${encodeURIComponent(S.memory.query)}` : "";
  const kind = S.memory.kind ? `kind=${S.memory.kind}&` : "";
  Promise.all([
    fetch(`/api/memory?${kind}limit=100${q}`).then((r) => r.json()),
    fetch("/api/capabilities").then((r) => r.json()),
  ]).then(([items, caps]) => {
    S.memory.items = items || [];
    S.memory.stats = caps.memory_stats || {};
    S.appliedCaps = caps.applied || [];
    S.pendingCaps = {};
    (caps.pending || []).forEach((p) => { S.pendingCaps[p.id] = p; });
    renderMemoryView();
    renderCapLists();
    updateBadges();
  }).catch(() => {});
}
function renderMemoryView() {
  const st = S.memory.stats || {};
  $("#mem-fact").textContent = st.fact || 0;
  $("#mem-lesson").textContent = st.lesson || 0;
  $("#mem-playbook").textContent = st.playbook || 0;
  $("#mem-evolved").textContent = (S.appliedCaps || []).length;

  const el = $("#mem-list");
  const items = S.memory.items;
  if (!items.length) {
    el.innerHTML = `<div class="empty">暂无记忆。智能体在排障、弄清业务后会自动沉淀；也可以在对话里直接说「记住：……」。</div>`;
    return;
  }
  const KIND_CLS = { fact: "warn", lesson: "error", playbook: "ok" };
  const KIND_TEXT = { fact: "事实", lesson: "经验", playbook: "剧本" };
  el.innerHTML = items.slice().reverse().map((e) =>
    `<div class="alert-item">
      <span class="lv ${KIND_CLS[e.kind] || ""}" style="min-width:44px;text-align:center">${KIND_TEXT[e.kind] || e.kind}</span>
      <div class="info">
        <div class="src">${esc(e.title || "")} · ${(e.tags || []).map((t) => "#" + esc(t)).join(" ")}</div>
        <div class="txt" style="font-family:var(--sans);color:var(--ink-dim)">${esc(e.content || "")}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
        <span class="ts">${esc((e.created || "").slice(5, 16))}</span>
        <button class="chip" data-del="${esc(e.id)}">删除</button>
      </div>
    </div>`).join("");
}
function renderCapLists() {
  const pend = Object.values(S.pendingCaps);
  $("#cap-pending").innerHTML = pend.length
    ? pend.map((p) => `<div class="kv"><span class="k">${esc(p.kind)}</span><span class="v">${esc(p.description)}</span></div>`).join("")
    : `<div class="empty" style="padding:8px">暂无待审批提案</div>`;
  const applied = S.appliedCaps || [];
  $("#cap-applied").innerHTML = applied.length
    ? applied.map((a) => {
        const label = a.command || (a.log_files || []).join(", ") || a.name || "";
        return `<div class="kv"><span class="k">${esc(a.kind || "")}</span><span class="v">${esc(a.name || "")} ${esc(label)}</span></div>`;
      }).join("")
    : `<div class="empty" style="padding:8px">暂无进化能力</div>`;
}

/* ================= 渲染：日志 ================= */
function renderProjectChips() {
  const box = $("#log-projects");
  box.innerHTML = S.projects.map((p) =>
    `<button class="chip ${p === S.currentProject ? "active" : ""}" data-p="${esc(p)}">${esc(p)}</button>`
  ).join("") || `<span class="empty" style="padding:0">未配置项目</span>`;
}
function selectProject(name) {
  S.currentProject = name;
  renderProjectChips();
  fetch(`/api/projects/${encodeURIComponent(name)}/lines?n=500`)
    .then((r) => r.json())
    .then((rows) => {
      S.logs[name] = rows.map((r) => ({ t: r.ts, file: r.file, line: r.line }));
      renderLogView();
    })
    .catch(() => renderLogView());
}
function addLogLine(project, row, isErr) {
  if (!S.logs[project]) S.logs[project] = [];
  S.logs[project].push(row);
  if (S.logs[project].length > MAX_LOG) S.logs[project].splice(0, S.logs[project].length - MAX_LOG);
  if (project === S.currentProject && S.view === "logs" && !S.logPaused && !isErr) {
    appendLogRow(row);
  }
  renderProjects();
}
function renderLogView() {
  const box = $("#log-box");
  const rows = (S.logs[S.currentProject] || []).filter(
    (r) => !S.logFilter || (r.line || "").toLowerCase().includes(S.logFilter.toLowerCase())
  );
  if (!rows.length) {
    box.innerHTML = `<div class="empty">${S.currentProject ? "暂无匹配日志" : "请选择项目"}</div>`;
    return;
  }
  box.innerHTML = rows.slice(-1000).map(logRowHtml).join("");
  box.scrollTop = box.scrollHeight;
}
function logRowHtml(r) {
  const lv = lineLevel(r.line || "");
  return `<div class="log-line ${lv}"><span class="lt">${fmtTs(r.t)}</span>${esc(r.file || "")} │ ${esc(r.line || "")}</div>`;
}
function appendLogRow(r) {
  const box = $("#log-box");
  if (box.querySelector(".empty")) box.innerHTML = "";
  const div = document.createElement("div");
  div.innerHTML = logRowHtml(r);
  const node = div.firstChild;
  box.appendChild(node);
  while (box.children.length > 1500) box.removeChild(box.firstChild);
  if (!S.logFilter || (r.line || "").toLowerCase().includes(S.logFilter.toLowerCase())) {
    box.scrollTop = box.scrollHeight;
  }
}

/* ================= 渲染：告警 / 设置 ================= */
function renderAlerts() {
  const el = $("#alert-list");
  if (!S.alerts.length) {
    el.innerHTML = `<div class="empty">暂无告警 — 一切正常</div>`;
    return;
  }
  el.innerHTML = S.alerts.slice().reverse().map((a) =>
    `<div class="alert-item">
      <span class="lv ${a.level}">${a.level === "error" ? "ERROR" : "WARN"}</span>
      <div class="info">
        <div class="src">项目 ${esc(a.project)} · 文件 ${esc(a.file || "-")}${a.line_no ? " · 行 " + a.line_no : ""}</div>
        <div class="txt">${esc(a.line || "")}</div>
        <div style="margin-top:7px">
          <button class="btn" data-ai="${esc(a.id || "")}" data-txt="${esc((a.project || "") + " | " + (a.file || "") + " | " + (a.line || ""))}">🤖 让 AI 分析此告警</button>
        </div>
      </div>
      <span class="ts">${fmtTs(a.ts)}</span>
    </div>`).join("");
}
function renderSettings() {
  if (!S.config) return;
  $("#set-white").innerHTML = (S.config.whitelist || []).map((c) =>
    `<div><span class="k">${esc(c.name)}</span><span class="v">${esc(c.command)}</span>` +
    ` <span style="color:${c.needs_approval ? "var(--warn)" : "var(--ok)"};font-size:11px">` +
    `${c.needs_approval ? "需审批" : "直接执行"}</span></div>`
  ).join("") || `<div class="empty">未配置白名单命令</div>`;
  $("#set-projects").innerHTML = (S.config.projects || []).map((n) =>
    `<div><span class="v">${esc(n.name)}</span> <span style="color:var(--ink-faint);font-size:11px">${esc(n.description || "")}</span></div>`
  ).join("") || `<div class="empty">未配置</div>`;
  loadModels();
}

/* ================= 模型管理 ================= */
function loadModels() {
  fetch("/api/llm/providers").then((r) => r.json()).then((data) => {
    S.models = data;
    renderLlmPanel();
    const act = (data.providers || []).find((p) => p.active);
    if (act) $("#foot-llm").textContent = `LLM: ${act.name} · ${act.model}`;
  }).catch((e) => { console.error("loadModels:", e); });
}
function renderLlmPanel() {
  const el = $("#llm-list");
  if (!el) return;
  const list = (S.models && S.models.providers) || [];
  if (!list.length) {
    el.innerHTML = `<div class="empty">暂无模型配置</div>`;
    return;
  }
  el.innerHTML = list.map((p) => {
    const kindCls = p.kind === "ollama" ? "local" : "cloud";
    const kindText = p.kind === "ollama" ? "本地" : "云端";
    const keyHint = p.has_key ? ` · key ${esc(p.api_key)}` : "";
    return `<div class="llm-card ${p.active ? "active" : ""}">
      <span class="tag ${kindCls}">${kindText}</span>
      <div class="meta">
        <div class="nm">${esc(p.name)} ${p.active ? '<span style="color:var(--accent);font-size:11px">默认</span>' : ""}</div>
        <div class="sub">${esc(p.model)} @ ${esc(p.base_url)}${keyHint}</div>
      </div>
      <div class="acts">
        ${p.active ? "" : `<button class="chip" data-llm-act="active" data-id="${esc(p.id)}">设为默认</button>`}
        <button class="chip" data-llm-act="test" data-id="${esc(p.id)}">测试</button>
        ${p.builtin ? "" : `<button class="chip" data-llm-act="del" data-id="${esc(p.id)}">删除</button>`}
      </div>
    </div>`;
  }).join("");
}
function llmMsg(text, ok) {
  const el = $("#llm-msg");
  if (!el) return;
  el.textContent = text;
  el.className = "note " + (ok ? "ok" : "err");
}

/* ================= 视图切换 ================= */
const VIEW_TITLES = { overview: "概览", chat: "对话", logs: "日志", alerts: "告警", memory: "记忆与能力", settings: "设置" };
function switchView(name) {
  S.view = name;
  $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  $("#view-title").textContent = VIEW_TITLES[name] || name;
  if (name === "alerts") { S.alertUnseen = 0; updateBadges(); renderAlerts(); }
  if (name === "memory") { loadMemoryView(); }
  if (name === "settings") { renderSettings(); }
  if (name === "logs") {
    renderProjectChips();
    if (!S.currentProject && S.projects.length) selectProject(S.projects[0]);
    else renderLogView();
  }
  if (name === "overview") { refreshStatus(); drawAllCharts(); }
  if (name === "chat") scrollChat();
}
function cssEscape(id) { return String(id).replace(/"/g, '\\"'); }

/* ================= 数据刷新 ================= */
function refreshStatus() {
  fetch("/api/status").then((r) => r.json()).then((s) => {
    S.status = s;
    renderFullStatus(s);
    drawAllCharts();
  }).catch(() => {});
}

/* ================= 事件绑定 ================= */
function bind() {
  $$(".nav button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

  $("#chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const text = input.value.trim();
    if (!text) return;
    send({ type: "chat", text });
    input.value = "";
    input.focus();
  });
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });

  $("#log-projects").addEventListener("click", (e) => {
    const btn = e.target.closest(".chip[data-p]");
    if (btn) selectProject(btn.dataset.p);
  });
  $("#log-search").addEventListener("input", (e) => {
    S.logFilter = e.target.value;
    renderLogView();
  });
  $("#log-pause").addEventListener("click", () => {
    S.logPaused = !S.logPaused;
    const btn = $("#log-pause");
    btn.textContent = S.logPaused ? "▶ 恢复滚动" : "⏸ 暂停滚动";
    btn.classList.toggle("active", !S.logPaused);
    if (!S.logPaused) renderLogView();
  });
  $("#log-clear").addEventListener("click", () => {
    if (S.currentProject) S.logs[S.currentProject] = [];
    renderLogView();
  });

  $("#alert-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-txt]");
    if (!btn) return;
    switchView("chat");
    const msg = `请分析这条日志告警，给出根因定位和解决步骤：\n${btn.dataset.txt}`;
    send({ type: "chat", text: msg });
  });

  // 记忆视图
  $("#view-memory").addEventListener("click", (e) => {
    const mk = e.target.closest("button[data-mk]");
    if (mk) {
      S.memory.kind = mk.dataset.mk;
      $$("#view-memory button[data-mk]").forEach((b) => b.classList.toggle("active", b === mk));
      loadMemoryView();
      return;
    }
    if (e.target.id === "mem-refresh") { loadMemoryView(); return; }
    const del = e.target.closest("button[data-del]");
    if (del) {
      fetch(`/api/memory/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" })
        .then(() => loadMemoryView());
    }
  });
  $("#mem-search").addEventListener("input", (e) => {
    S.memory.query = e.target.value;
    clearTimeout(S.memory._t);
    S.memory._t = setTimeout(loadMemoryView, 300);
  });

  // 模型管理
  $("#llm-list").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-llm-act]");
    if (!btn) return;
    const act = btn.dataset.llmAct;
    const id = btn.dataset.id;
    btn.disabled = true;
    try {
      if (act === "active") {
        const r = await fetch(`/api/llm/providers/${encodeURIComponent(id)}/active`, { method: "POST" });
        const d = await r.json();
        if (d.ok) { llmMsg(`已切换默认模型：${d.name} · ${d.model}`, true); loadModels(); }
        else llmMsg(d.detail || "切换失败", false);
      } else if (act === "test") {
        llmMsg("测试中…", true);
        const r = await fetch("/api/llm/test", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        });
        const d = await r.json();
        if (d.ok) llmMsg(`连通正常，模型回复：${d.reply}`, true);
        else llmMsg(`连接失败：${d.error || d.detail || ""}`, false);
      } else if (act === "del") {
        if (!confirm("确定删除该模型配置？")) { btn.disabled = false; return; }
        const r = await fetch(`/api/llm/providers/${encodeURIComponent(id)}`, { method: "DELETE" });
        const d = await r.json();
        if (d.ok) { llmMsg("已删除", true); loadModels(); }
        else llmMsg(d.detail || "删除失败", false);
      }
    } catch (err) {
      llmMsg("请求失败: " + err, false);
    }
    btn.disabled = false;
  });
  $("#llm-add").addEventListener("click", async () => {
    const body = {
      name: $("#llm-name").value.trim(),
      base_url: $("#llm-base").value.trim(),
      api_key: $("#llm-key").value.trim(),
      model: $("#llm-model").value.trim(),
    };
    if (!body.base_url || !body.model) { llmMsg("请填写 Base URL 与模型 ID", false); return; }
    const r = await fetch("/api/llm/providers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) {
      llmMsg(`已添加：${body.name || body.model}（可点「设为默认」切换）`, true);
      $("#llm-name").value = ""; $("#llm-key").value = "";
      loadModels();
    } else llmMsg(d.detail || "添加失败", false);
  });
  $("#llm-test-form").addEventListener("click", async () => {
    const body = {
      kind: "openai_compatible",
      base_url: $("#llm-base").value.trim(),
      api_key: $("#llm-key").value.trim(),
      model: $("#llm-model").value.trim(),
    };
    if (!body.base_url || !body.model) { llmMsg("先填写 Base URL 与模型 ID", false); return; }
    llmMsg("测试中…", true);
    const r = await fetch("/api/llm/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) llmMsg(`连通正常，模型回复：${d.reply}`, true);
    else llmMsg(`连接失败：${d.error || d.detail || ""}`, false);
  });

  window.addEventListener("resize", () => drawAllCharts());
  setInterval(() => { if (S.status) { S.status.uptime_seconds++; renderTopbar({}); } }, 1000);
  S.statusTimer = setInterval(refreshStatus, 10000);
}

/* ================= 启动 ================= */
bind();
connect();
refreshStatus();
