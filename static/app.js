/* 幕僚 Muliáo · 前端逻辑（仿 ZCode 设计语言）
   回合流水线：① 用户发消息 → ② Jev 快判断（输入框上方 + 右栏体检）
   → ③ 模型流式作答（思考流可折叠）→ ④ Jev 复核这次回复
   右栏三标签：本回合体检 / 系统通知 / 缓存命中率
   全部对话只经本机后端代理，前端不直连模型、不持 key。 */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 时间戳 → 相对时间（通知用）。兼容秒/毫秒；ts 为 0/空时显示「—」
function timeMeta(ts) {
  let value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return { text: "—", iso: "", title: "无时间" };
  if (value > 1e12) value /= 1000;
  const dt = new Date(value * 1000);
  if (Number.isNaN(dt.getTime())) return { text: "—", iso: "", title: "时间无效" };
  const delta = Date.now() / 1000 - value;
  let text;
  if (delta < -60) text = "未来时间";
  else if (delta < 60) text = "刚刚";
  else if (delta < 3600) text = Math.max(1, Math.floor(delta / 60)) + " 分钟前";
  else if (delta < 86400) text = Math.max(1, Math.floor(delta / 3600)) + " 小时前";
  else {
    const p = (n) => String(n).padStart(2, "0");
    const year = dt.getFullYear() !== new Date().getFullYear() ? dt.getFullYear() + "-" : "";
    text = `${year}${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
  }
  return { text, iso: dt.toISOString(), title: dt.toLocaleString("zh-CN", { hour12: false }) };
}

/* ================= 会话存储（localStorage，纯本机） ================= */
const LS = "muliao.sessions.v1";
let sessions = [];
let curId = null;

function loadSessions() {
  try { sessions = JSON.parse(localStorage.getItem(LS)) || []; } catch { sessions = []; }
}
function saveSessions() {
  try { localStorage.setItem(LS, JSON.stringify(sessions.slice(0, 60))); } catch {}
}
function cur() { return sessions.find((s) => s.id === curId); }
function syncComposerDraft(session = cur()) {
  const input = $("input");
  if (!input) return;
  input.value = session?.draft || "";
  autoGrow(input);
}
function saveVisibleDraft() {
  const session = cur();
  const input = $("input");
  if (session && input) session.draft = input.value;
}
function recoverSessionDraft(sessionId, text) {
  const session = sessions.find((item) => item.id === sessionId);
  const value = String(text || "");
  if (!session || !value.trim()) return false;
  const existing = String(session.draft || "");
  session.draft = existing.trim() && existing !== value ? `${value}\n\n${existing}` : value;
  saveSessions();
  if (cur()?.id === sessionId) syncComposerDraft(session);
  return true;
}
function newSession() {
  saveVisibleDraft();
  const s = { id: "s" + Date.now(), title: "新会话", msgs: [], draft: "", created: Date.now() };
  sessions.unshift(s); curId = s.id;
  saveSessions(); renderSessions(); clearThread(); syncComposerDraft(s);
  $("topTitle").textContent = "新会话";
  gotoTab("insp");
  $("input").focus();
}
function titleFrom(text) { return text.replace(/\s+/g, " ").slice(0, 18) || "新会话"; }

window.MuliaoDrafts = { recover: recoverSessionDraft };

/* ================= 左栏 ================= */
function renderSessions(filter = "") {
  const box = $("sessions");
  const f = filter.trim().toLowerCase();
  const list = f ? sessions.filter((s) => (s.title || "").toLowerCase().includes(f)) : sessions;
  box.innerHTML = list.map((s) =>
    `<button type="button" class="sess ${s.id === curId ? "on" : ""}" data-id="${s.id}">
       <span class="nm">${esc(s.title)}</span><span class="cnt">${s.msgs.length}</span>
     </button>`).join("") || `<div class="sess-none">${f ? "没有匹配的会话" : "暂无会话"}</div>`;
  box.querySelectorAll(".sess[data-id]").forEach((el) => {
    el.onclick = () => {
      saveVisibleDraft();
      curId = el.dataset.id;
      renderSessions($("search").value);
      renderThread();
      syncComposerDraft();
      gotoTab("insp");
    };
  });
}

function updateCounts() {
  const n = sessions.reduce((a, s) => a + s.msgs.length, 0);
  $("counts").textContent = `${sessions.length} 会话 · ${n} 消息`;
}

/* ================= 右栏标签 ================= */
const TAB_LABEL = { insp: "决策账本", swarm: "蜂群任务", notify: "通知收件箱", cache: "缓存账本" };
function gotoTab(which) {
  document.querySelectorAll(".itab").forEach((t) => {
    const on = t.dataset.tab === which;
    t.classList.toggle("on", on);
    t.setAttribute("aria-selected", String(on));
  });
  ["insp", "swarm", "notify", "cache"].forEach((k) => { $("pane-" + k).hidden = k !== which; });
  $("inspLabel").textContent = TAB_LABEL[which] || "";
  // 左栏导航高亮同步
  document.querySelectorAll(".nav-item[data-goto]").forEach((b) => {
    const g = b.dataset.goto;
    b.classList.toggle("on", g === which || (g === "chat" && which === "insp"));
  });
  const app = document.querySelector(".app");
  if (window.matchMedia("(max-width: 1180px)").matches) app.classList.add("mobile-insp-open");
  if (which === "notify") loadNotifications(false);
}
function initTabs() {
  document.querySelectorAll(".itab").forEach((t) => {
    t.setAttribute("role", "tab");
    t.setAttribute("aria-selected", String(t.classList.contains("on")));
    t.onclick = () => gotoTab(t.dataset.tab);
  });
  document.querySelectorAll(".nav-item[data-goto]").forEach((b) => {
    b.onclick = () => {
      const g = b.dataset.goto;
      document.querySelector(".app").classList.remove("no-insp");
      gotoTab(g === "chat" ? "insp" : g);
    };
  });
  // 收起/展开
  $("collapseSide").onclick = () => document.querySelector(".app").classList.add("no-side");
  $("reopenSide").onclick = () => document.querySelector(".app").classList.remove("no-side");
  $("inspClose").onclick = () => {
    const app = document.querySelector(".app");
    app.classList.add("no-insp");
    app.classList.remove("mobile-insp-open");
  };
  $("reopenInsp").onclick = () => {
    const app = document.querySelector(".app");
    app.classList.remove("no-insp");
    if (window.matchMedia("(max-width: 1180px)").matches) app.classList.add("mobile-insp-open");
  };
}

/* ================= 中栏：消息渲染 ================= */
function msgEl(role, text, opts = {}) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  const who = role === "user" ? "你" : `<img src="icon-glow.svg" alt=""/><span class="who">幕僚</span>`;
  div.innerHTML = `<div class="msg-role">${who}</div><div class="msg-body"></div>`;
  const body = div.querySelector(".msg-body");
  body.textContent = text;
  if (opts.cursor) body.classList.add("cursor");

  if (role === "assistant" && opts.think !== undefined) {
    const tk = document.createElement("div");
    tk.className = "think";
    tk.innerHTML =
      `<div class="think-head">
         <span class="tk-ico"><svg><use href="#i-spark"/></svg></span>
         <span class="tk-label">思考中…</span>
         <span class="tk-meta"></span>
         <span class="chev">›</span>
       </div>
       <div class="think-body"><div class="inner"></div></div>`;
    tk.querySelector(".think-head").onclick = () => { tk.dataset.userToggled = "1"; tk.classList.toggle("open"); };
    div.querySelector(".msg-body").before(tk);
    div._think = tk;
  }
  return div;
}

function clearThread() {
  const t = $("thread");
  t.querySelectorAll(".msg").forEach((m) => m.remove());
  $("empty").style.display = "";
  $("judgebar").innerHTML = "";
  $("insp").innerHTML = `<div class="insp-empty">发送消息后，这里显示这一回合的逐项判断与置信度，以及答完后的复核结论。</div>`;
  $("cachePane").innerHTML = `<div class="insp-empty">发送消息后，这里显示 prompt 缓存命中率（稳态 ≥90% 为达标）。</div>`;
  if (!window.MuliaoRunControl?.current) setBusy(false, "就绪");
}

function renderThread() {
  const s = cur(); clearThread();
  if (!s) return;
  $("topTitle").textContent = s.title;
  if (!s.msgs.length) {
    $("empty").style.display = "";
    return;
  }
  $("empty").style.display = "none";
  for (const m of s.msgs) {
    const el = msgEl(m.role, m.text, { think: m.role === "assistant" ? (m.think || "") : undefined });
    if (m.role === "assistant" && el._think) setThinkDone(el._think, m.think || "", m.thinkMs);
    $("thread").appendChild(el);
  }
  if (s.lastJudge) { renderJudgeBar(s.lastJudge); $("insp").appendChild(renderJudgePanel(s.lastJudge)); }
  if (s.lastReview) renderReview(s.lastReview, s.lastUsage);
  if (s.lastCache) renderCache(s.lastCache);
  scrollBottom();
}

function scrollBottom() { const t = $("thread"); t.scrollTop = t.scrollHeight; }

/* 状态栏忙碌指示 */
function setBusy(busy, text) {
  const d = $("sbDot");
  d.className = "sb-dot" + (busy ? " busy" : "");
  if (text) $("statusTxt").textContent = text;
}

/* ================= 思考流 ================= */
function setThinkStreaming(tk, text, ms) {
  tk.classList.add("streaming");
  tk.querySelector(".tk-ico").classList.add("spin");
  tk.querySelector(".tk-label").textContent = "思考中…";
  tk.querySelector(".tk-meta").textContent = ms ? (ms / 1000).toFixed(1) + "s" : "";
  const inner = tk.querySelector(".inner");
  inner.textContent = text;
  if (!tk.dataset.userToggled) tk.classList.add("open");
  inner.scrollTop = inner.scrollHeight;
}
function setThinkDone(tk, text, ms) {
  tk.classList.remove("streaming");
  tk.querySelector(".tk-ico").classList.remove("spin");
  const n = (text || "").length;
  tk.querySelector(".tk-label").textContent = n ? `已思考（${n} 字）` : "未展开思考";
  tk.querySelector(".tk-meta").textContent = ms ? (ms / 1000).toFixed(1) + "s" : "";
  tk.querySelector(".inner").textContent = text || "";
  if (!text) tk.style.display = "none";
}

/* ================= 门控与体检 ================= */
const GATE_CN = { auto: "放行", review: "复核", esc: "升级" };
const INTENT_CN = { chat: "闲聊", query: "查询", task: "任务", create: "创作", risky: "风险操作" };
const CHK_NAME = { intent: "意图归类", danger: "危险等级", external: "外泄风险", clarify: "需否澄清" };

function gateOf(id, kind, a) {
  if (kind === "noul") {
    const p = a.noul ?? 0;
    // 外泄 / 风险类：P 高就该升级，而不是「确定」就放行
    if (id === "external" || id === "risky") return p > 0.6 ? "esc" : p > 0.3 ? "review" : "auto";
    return p > 0.75 || p < 0.25 ? "auto" : "review";
  }
  if (kind === "score") { const s = a.score ?? 0; return s >= 6 ? "esc" : s >= 3.5 ? "review" : "auto"; }
  const c = a.confidence ?? 0;
  return c >= 0.6 ? "auto" : c >= 0.35 ? "review" : "esc";
}
function confFor(kind, a) {
  if (kind === "noul") { const p = a.noul ?? 0; return Math.abs(p - 0.5) * 2; }  // 离 0.5 越远越确定
  return a.confidence ?? 0;
}
function probRows(a) {
  const p = a.probabilities || {};
  const keys = Object.keys(p);
  if (!keys.length) return "";
  const win = a.type === "choice" ? a.choice : null;
  return `<div class="probs">` + keys.slice().sort((x, y) => p[y] - p[x]).slice(0, 5).map((k) => {
    const label = a.legend ? (a.legend[k] || k) : (INTENT_CN[k] || k);
    const v = p[k] || 0;
    return `<div class="prob-row ${k === win ? "win" : ""}">
      <span class="pl" title="${esc(label)}">${esc(String(label).slice(0, 10))}</span>
      <span class="pb"><i style="width:${Math.max(2, v * 100)}%"></i></span>
      <span class="pv">${(v * 100).toFixed(0)}%</span></div>`;
  }).join("") + `</div>`;
}
function answerText(name, a) {
  if (a.type === "noul") {
    const p = a.noul ?? 0;
    const lbl = name === "external" ? "外泄" : name === "clarify" ? "需澄清" : "是";
    return `<span class="k">${lbl}：</span>${p >= 0.5 ? "是" : "否"} · P=${p.toFixed(2)}`;
  }
  if (a.type === "score") {
    const idx = String(Math.min(Object.keys(a.legend || {}).length - 1, Math.round(a.score ?? 0)));
    const lv = a.legend ? (a.legend[idx] || "") : "";
    return `<span class="k">危险度：</span>${(a.score ?? 0).toFixed(2)} ${lv ? "· " + esc(String(lv)) : ""}`;
  }
  if (a.type === "choice") return `<span class="k">意图：</span>${esc(INTENT_CN[a.choice] || a.choice)}`;
  return "";
}

function renderJudgeBar(j) {
  const bar = $("judgebar");
  if (!j || !j.ok || !j.answers || !Object.keys(j.answers).length) { bar.innerHTML = ""; return; }
  bar.innerHTML = Object.entries(j.answers).map(([id, a]) => {
    const g = gateOf(id, a.type, a);
    const val = a.type === "choice" ? (INTENT_CN[a.choice] || a.choice)
      : a.type === "score" ? (a.score ?? 0).toFixed(1)
      : ((a.noul ?? 0) >= 0.5 ? "是" : "否");
    return `<span class="jchip ${g}"><span class="jk">${CHK_NAME[id] || id}</span><span class="jv">${esc(String(val))}</span><span class="jc">${confFor(a.type, a).toFixed(2)}</span></span>`;
  }).join("") + `<span class="jchip"><span class="jk">Jev</span><span class="jv" style="color:var(--acc)">${j.ms ?? "—"}ms</span></span>`;
}

function renderJudgePanel(j) {
  const sec = document.createElement("div");
  sec.className = "insp-section";
  if (!j || !j.ok) {
    const title = judgeFailure(j?.code, "Jev 判断未完成");
    sec.innerHTML = `<div class="sec-title">回合前 · 快判断</div>
      <div class="chk"><div class="ct"><b>${esc(title)}</b><span class="gate esc">降级</span></div>
      <div class="ans">${esc(judgeErrorText(j, "判断引擎暂时不可用"))}</div>
      <div class="conf-line" style="color:var(--dimmer);font-size:11px">本回合按「无判断」继续作答；瞬时错误会自动重试一次。</div></div>`;
    return sec;
  }
  sec.innerHTML = `<div class="sec-title">回合前 · 快判断 <span class="ms">${j.ms}ms · ${j.model || "jev"}</span></div>`;
  const answers = j.answers || {};
  for (const id of ["intent", "danger", "external", "clarify"]) {
    const a = answers[id]; if (!a) continue;
    const g = gateOf(id, a.type, a), conf = confFor(a.type, a);
    const el = document.createElement("div");
    el.className = "chk";
    el.innerHTML =
      `<div class="ct"><b>${CHK_NAME[id]}</b><span class="gate ${g}">${GATE_CN[g]} · ${conf.toFixed(2)}</span></div>
       <div class="ans">${answerText(id, a)}</div>
       <div class="conf-line"><span class="bar"><i class="${g}" style="width:${conf * 100}%"></i></span><span class="conf-val">${(conf * 100).toFixed(0)}%</span></div>
       ${probRows(a)}`;
    sec.appendChild(el);
  }
  return sec;
}

function renderReview(rv, usage) {
  let sec = $("insp").querySelector(".review-section");
  if (!sec) { sec = document.createElement("div"); sec.className = "insp-section review-section"; $("insp").appendChild(sec); }
  if (!rv || !rv.ok) {
    sec.innerHTML = `<div class="sec-title">回合后 · 复核</div>
      <div class="review-box"><div class="verdict flag">✕ ${esc(judgeErrorText(rv, "Jev 复核未完成"))}</div></div>`;
    return;
  }
  const a = rv.answers || {};
  let allPass = true;
  const rows = [{ id: "on_topic", label: "切题" }, { id: "safe", label: "安全" }, { id: "grounded", label: "克制" }]
    .map(({ id, label }) => {
      const x = a[id]; if (!x) return "";
      const p = x.noul ?? 0, pass = p >= 0.5;
      if (!pass) allPass = false;
      return `<div class="rv ${pass ? "rv-pass" : "rv-flag"}">
        <span class="ic">${pass ? "✓" : "✕"}</span><span class="lbl">${label}</span><span class="p">P=${p.toFixed(2)}</span></div>`;
    }).join("");
  const ms = rv.ms ? `<span class="ms">${rv.ms}ms · ${rv.model || "jev"}</span>` : "";
  sec.innerHTML = `<div class="sec-title">回合后 · 复核 ${ms}</div>
    <div class="review-box">${rows}
      <div class="verdict ${allPass ? "pass" : "flag"}">${allPass ? "✓ 复核通过 · 切题、安全、克制" : "✕ 复核存疑 · 建议人工再看一眼"}</div>
    </div>
    ${usage ? `<div class="usage-line"><span>输入 ${usage.prompt_tokens ?? "—"} tok</span><span>输出 ${usage.completion_tokens ?? "—"} tok</span>${usage.completion_tokens_details?.reasoning_tokens ? `<span>思考 ${usage.completion_tokens_details.reasoning_tokens} tok</span>` : ""}</div>` : ""}`;
}

function renderJudgeLoading() {
  $("judgebar").innerHTML = `<span class="jchip loading"><span class="spin-dot"></span><span class="jv">快判断中…</span></span>`;
}

/* ================= 缓存命中率（独立标签页） ================= */
// 达标看「稳态」(排除首轮冷启动)。实测：长稳定前缀 3238 tok → 稳态 94.9%~96%；
// 该端点 max_tokens 不生效，故命中率只能靠加大稳定前缀摊薄，不能靠限制回复长度。
function renderCache(c) {
  const pane = $("cachePane");
  const rate = c.rate ?? 0;
  const steady = (c.warm_turns ?? 0) >= 1 ? (c.warm_rate ?? 0) : rate;
  const ok = steady >= 0.9;
  const spct = (steady * 100).toFixed(1);

  const spark = (c.history || []).slice(-24).map((h) => {
    const hp = Math.round((h.rate || 0) * 100);
    const cls = hp >= 90 ? "hi" : hp >= 50 ? "mid" : "lo";
    return `<i class="sp ${cls}" style="height:${Math.max(3, hp)}%" title="第${h.turn}轮 ${hp}%"></i>`;
  }).join("");

  pane.innerHTML =
    `<div class="insp-section">
       <div class="sec-title">缓存命中率 <span class="ms">稳态 ≥ 90% 达标</span></div>
       <div class="cache-card">
         <div class="cc-big">
           <span class="cc-num ${ok ? "ok" : "bad"}">${spct}%</span>
           <span class="cc-lbl">稳态命中（第 2 轮起累计）</span>
         </div>
         <div class="cc-side">
           <div class="cc-row"><span>本轮</span><b class="${rate >= 0.9 ? "ok" : "bad"}">${(rate * 100).toFixed(1)}%</b></div>
           <div class="cc-row"><span>全程累计</span><b>${((c.cum_rate ?? 0) * 100).toFixed(1)}%</b></div>
           <div class="cc-row"><span>缓存 / 输入</span><b>${c.cached ?? 0} / ${c.prompt ?? 0}</b></div>
           <div class="cc-row"><span>轮次</span><b>${c.turn ?? 0}</b></div>
         </div>
         <div class="cc-bar"><i class="${ok ? "ok" : "bad"}" style="width:${Math.min(100, steady * 100)}%"></i><span class="cc-target" style="left:90%"></span></div>
         <div class="cc-spark">${spark || '<i class="sp" style="height:3px"></i>'}</div>
         <div class="cc-note ${ok ? "ok" : "bad"}">${ok ? "✓ 达标 · 长稳定前缀复用良好，对话未跑偏" : "○ 未达标 · 首轮冷启动属正常，第 2 轮起应升"}</div>
         <div class="cc-explain">
           <b>怎么读这个数</b><br />
           上游按「前缀逐字节匹配」命中缓存：每轮只有<b>上一轮回复 + 本轮提问</b>是新增，其余（固定系统提示 + 全部历史）都命中。
           所以对话越长命中率越接近 100%；首轮为 0% 是冷启动写缓存，属正常。<br />
           <b>保持达标的三条纪律</b>：系统提示逐字节不变 · 历史只增不删 · 静态在前动态在后。
         </div>
       </div>
     </div>`;

  // 左栏导航 + 底部状态栏同步
  const nav = $("navCachePct");
  nav.textContent = spct + "%";
  nav.classList.toggle("ok", ok);
  const sc = $("statusCache");
  sc.textContent = spct + "%";
  sc.className = ok ? "ok" : "bad";
}

/* ================= 状态轮询 ================= */
let statusPollInFlight = false;
async function pollStatus() {
  if (statusPollInFlight) return;
  statusPollInFlight = true;
  try {
    const s = await (await fetch("/api/status")).json();
    $("val-llm").textContent = s.model || "—";
    $("modelSelTxt").textContent = s.model || "—";
    $("dot-llm").className = "dot on";

    const dot = $("dot-judge"), val = $("val-judge"), pill = $("enginePill"), pt = $("enginePillTxt");
    if (s.jev_ok) {
      dot.className = "dot on"; val.textContent = "Jev 就绪";
      pill.className = "pill on"; pt.textContent = `${s.jev_model || "jev"} · ${s.jev_ms ?? "—"}ms`;
      $("modelSelJev").textContent = s.jev_model || "Jev";
      hideTopup();
    } else if (s.jev_need_topup) {
      dot.className = "dot bad"; val.textContent = "额度/鉴权";
      pill.className = "pill bad"; pt.textContent = "Jev 需充值";
      showTopup(s.jev_console, s.jev_err);
    } else {
      dot.className = "dot warn"; val.textContent = "连接中";
      pill.className = "pill"; pt.textContent = "Jev 连接中";
      // 普通网络抖动只显示连接中；只有后端明确判定额度/鉴权时才展示充值入口。
      hideTopup();
    }
  } catch {}
  finally { statusPollInFlight = false; }
}

function showTopup(url, err, isError) {
  let b = $("topup");
  if (!b) {
    b = document.createElement("div");
    b.id = "topup";
    b.className = "topup";
    document.querySelector(".main")?.prepend(b);
  }
  b.innerHTML =
    `<span class="tu-ic">${isError ? "⚠" : "◎"}</span>
     <span class="tu-tx">${isError ? "Jev 判断引擎连接异常" : "Jev 额度不足或鉴权失败"}<br><code>${esc(String(err || "").slice(0, 120))}</code></span>
     <a class="tu-btn" href="${url || "https://console.typesafe.ai/keys"}" target="_blank" rel="noopener">前往充值 / 管理 Key</a>
     <button class="tu-x" onclick="document.getElementById('topup').remove()">✕</button>`;
  b.style.display = "flex";
}
function hideTopup() { const b = $("topup"); if (b) b.remove(); }

/* ================= 系统通知 ================= */
let notifyTimer = null;
const CAT_CN = { agent: "Agent", system: "系统", message: "消息", app: "应用" };
// 工具名 → 中文（工具调用条上显示，让「读你的电脑」这件事看得见）
const TOOL_CN = {
  get_machine_snapshot: "读系统信息", get_foreground_window: "读前台窗口",
  get_running_processes: "读进程列表", get_recent_notifications: "读系统通知",
  get_browser_history: "读浏览器历史", get_recent_files: "读文件清单",
  search_ai_logs: "检索 AI 对话",
  // 电脑控制能力（执行动作）
  list_windows: "列出窗口", focus_window: "聚焦窗口", close_window: "关闭窗口",
  open_application: "打开应用", click_element: "点击控件", type_text: "输入文字",
  press_keys: "发送按键",
};

// 按 tool_call id 精确定位工具行（同名工具多次调用不会错行）；退化到 data-name。
function findToolRow(box, callId, name) {
  if (!box) return null;
  if (callId) {
    const byId = box.querySelector(`.toolrow[data-callid="${callId}"]`);
    if (byId) return byId;
  }
  const rows = box.querySelectorAll(`.toolrow[data-name="${name}"]`);
  return rows[rows.length - 1] || null;
}

/* ---- 高风险动作确认弹窗（Jev 门控判定 confirm 时弹出）---- */
let openConfirmId = null;          // 当前打开的 confirm_id，供暂停/断流时按拒绝收尾
let confirmCountdown = null;

function openConfirmDialog(ev) {
  const ov = $("confirmOverlay");
  if (!ov) return;
  openConfirmId = ev.confirm_id;
  $("confirmTool").textContent = ev.cn || TOOL_CN[ev.name] || ev.name || "未知动作";
  const risk = Number(ev.risk ?? 0);
  $("confirmRisk").textContent = `${risk.toFixed(1)} / 10（≥6 需确认）`;
  $("confirmReason").textContent = ev.reason || "Jev 判定为高风险动作";
  let argsText = "—";
  try { argsText = JSON.stringify(ev.args ?? {}, null, 2); } catch { argsText = String(ev.args ?? ""); }
  $("confirmArgs").textContent = argsText.slice(0, 1200);
  ov.hidden = false;
  // 倒计时（纯提示；真正的超时以服务端 _CONFIRM_TIMEOUT 为准）
  const totalMs = Number(ev.timeout_ms || 120000);
  let remain = Math.round(totalMs / 1000);
  const allowBtn = $("confirmAllow");
  const denyBtn = $("confirmDeny");
  allowBtn.disabled = false; denyBtn.disabled = false;
  allowBtn.textContent = `允许执行（${remain}s）`;
  if (confirmCountdown) clearInterval(confirmCountdown);
  confirmCountdown = setInterval(() => {
    remain -= 1;
    if (remain <= 0) { clearInterval(confirmCountdown); confirmCountdown = null; }
    else allowBtn.textContent = `允许执行（${remain}s）`;
  }, 1000);
  allowBtn.focus();
}

function closeConfirmDialog() {
  const ov = $("confirmOverlay");
  if (ov) ov.hidden = true;
  if (confirmCountdown) { clearInterval(confirmCountdown); confirmCountdown = null; }
  openConfirmId = null;
}

async function submitConfirm(decision) {
  const confirmId = openConfirmId;
  const allowBtn = $("confirmAllow"), denyBtn = $("confirmDeny");
  if (allowBtn) allowBtn.disabled = true;
  if (denyBtn) denyBtn.disabled = true;
  if (!confirmId) { closeConfirmDialog(); return; }
  try {
    await fetch("/api/chat/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_id: confirmId, decision }),
    });
  } catch { /* 流可能已断；服务端会按断流兜底拒绝 */ }
  closeConfirmDialog();
}

// 暂停/断流时，若有打开的确认框，按拒绝收掉（不静默放行控制动作）。
function denyOpenConfirm() {
  if (openConfirmId) { submitConfirm("deny"); }
}

async function loadNotifications(light) {
  const list = $("notifyList"), btn = $("notifyRefresh");
  // light=true（启动静默抓取）不分级，省 Jev 额度；点开标签/手动刷新才分级
  const triage = (!light && $("triageToggle")?.checked) ? "1" : "0";
  btn?.classList.add("spinning");
  if (!light) list.innerHTML = `<div class="notify-loading">抓取本机通知中…${triage === "1" ? "（Jev 分级多花 1-2 秒）" : ""}</div>`;
  try {
    const d = await (await fetch(`/api/notify?limit=40&triage=${triage}`)).json();
    renderNotifications(d, light);
  } catch (e) {
    if (!light) list.innerHTML = `<div class="notify-err">抓取失败：${esc(e.message)}</div>`;
  }
  btn?.classList.remove("spinning");
}

function actLevel(n) {
  const a = n.triage?.needs_action;
  if (a == null) return "";
  return a >= 0.7 ? "act-high" : a >= 0.4 ? "act-mid" : "";
}

function setNotifyBadge(n) {
  for (const id of ["notifyBadge", "navNotifyBadge"]) {
    const b = $(id); if (!b) continue;
    b.textContent = n > 0 ? String(n) : "";
    b.classList.toggle("show", n > 0);
  }
}

function renderNotifications(d, light) {
  const list = $("notifyList"), items = d.items || [];
  const sum = $("notifySummary");

  // 未授权通知采集：引导去权限页，不显示误导性的「暂无通知」
  if (d.granted === false) {
    if (sum) sum.textContent = "未授权通知访问";
    list.innerHTML = `<div class="notify-loading" style="padding:34px 16px">
      <div style="font-size:13px;color:var(--dim);margin-bottom:6px">通知采集未授权</div>
      <div style="font-size:11px;color:var(--dimmer);line-height:1.7;margin-bottom:14px">
        幕僚需要你逐项授权后才会抓取本机通知。<br />数据只存本机，可随时撤销或焚毁。</div>
      <button class="perm-link" id="notifyGoPerm" style="border:1px solid var(--line2);padding:7px 16px;border-radius:8px;color:var(--acc)">前往授权</button>
    </div>`;
    $("notifyGoPerm").onclick = openPermOverlay;
    setNotifyBadge(0);
    return;
  }

  if (sum) {
    const used = (d.platforms_used || []).join("/") || "无";
    sum.textContent = `${items.length} 条 · ${used}${d.triaged ? ` · Jev 分级 ${d.triaged}（${d.triage_ms}ms）` : ""}`;
  }
  if (!items.length) {
    if (!light) list.innerHTML = `<div class="notify-loading">暂无通知。<br><span style="font-size:11px">其他平台需在对应系统运行，或用 notify-bridge 回写。</span></div>`;
    setNotifyBadge(0);
    return;
  }

  const card = (n) => {
    const t = n.triage || {};
    const trivial = (t.needs_action != null && t.needs_action < 0.25) ? "trivial" : "";
    const isWx = n.kind === "wechat";
    const tm = timeMeta(n.ts);
    return `<div class="notif ${actLevel(n)} ${trivial}${isWx ? " wx" : ""}">
      <div class="nt">
        ${isWx ? `<span class="nkind wx">微信</span>` : ""}
        <span class="napp" title="${esc(n.app || "")}">${esc(n.app_name || "未知")}</span>
        ${t.category ? `<span class="ncat ${t.category}">${CAT_CN[t.category] || t.category}${t.category_conf != null ? " " + t.category_conf.toFixed(2) : ""}</span>` : ""}
        ${t.needs_action != null ? `<span class="nact">需处理 ${t.needs_action.toFixed(2)}</span>` : ""}
      </div>
      <div class="ntitle">${esc(n.title || "")}${n.messages ? ` <span class="nmsg">${n.messages} 条</span>` : ""}</div>
      ${n.body ? `<div class="nbody">${esc(n.body.slice(0, 200))}</div>` : ""}
      <div class="nfoot">
        <time class="ntime" datetime="${esc(tm.iso)}" title="${esc(tm.title)}">${esc(tm.text)}</time>
        <span class="notif-act"><button data-ask="${esc((n.title || "") + " " + (n.body || "").slice(0, 80))}">问幕僚</button></span>
      </div>
    </div>`;
  };

  // light 模式（无 Jev 分级）：平铺，不分组、不占角标
  if (light || !d.triaged) {
    list.innerHTML = `<div class="notif-group"><div class="ng-title">最近通知<span class="ng-n">${items.length}</span>
      <span style="margin-left:auto;font-size:9.5px">点开本标签可 Jev 分级</span></div>${items.map(card).join("")}</div>`;
    bindAsk(list);
    return;
  }

  const high = [], mid = [], rest = [];
  for (const n of items) {
    const a = n.triage?.needs_action;
    if (a != null && a >= 0.7) high.push(n);
    else if (a != null && a >= 0.4) mid.push(n);
    else rest.push(n);
  }
  setNotifyBadge(high.length);
  const group = (title, arr) => arr.length
    ? `<div class="notif-group"><div class="ng-title">${title}<span class="ng-n">${arr.length}</span></div>${arr.map(card).join("")}</div>` : "";
  list.innerHTML = group("🔴 需要处理", high) + group("🟡 可留意", mid) + group("⚪ 其余", rest);
  bindAsk(list);
}

function bindAsk(list) {
  list.querySelectorAll("[data-ask]").forEach((b) => {
    b.onclick = () => {
      $("input").value = "帮我看看这条系统通知该怎么处理：" + b.dataset.ask;
      autoGrow($("input")); $("input").focus();
      document.querySelector(".app").classList.remove("no-insp");
      gotoTab("insp");
    };
  });
}

function initNotify() {
  $("notifyRefresh").onclick = () => loadNotifications(false);
  $("triageToggle").onchange = () => loadNotifications(false);
  $("notifyAuto").onchange = (e) => { e.target.checked ? startNotifyAuto() : stopNotifyAuto(); };
  startNotifyAuto();
}
function startNotifyAuto() {
  stopNotifyAuto();
  notifyTimer = setInterval(() => { if (!$("pane-notify").hidden) loadNotifications(false); }, 20000);
}
function stopNotifyAuto() { if (notifyTimer) { clearInterval(notifyTimer); notifyTimer = null; } }

/* ================= 发送一个回合 ================= */
let busy = false;
let activeChat = null;

function judgeFailure(code, fallback = "Jev 判断暂时不可用") {
  const labels = {
    timeout: "Jev 请求超时",
    rate_limit: "Jev 暂时限流",
    auth: "Jev 鉴权失败",
    quota: "Jev 额度不足",
    network: "Jev 网络异常",
    bad_response: "Jev 返回格式异常",
    upstream: "Jev 上游异常",
  };
  return labels[code] || fallback;
}

function judgeErrorText(result, fallback) {
  const title = judgeFailure(result?.code, fallback);
  const detail = String(result?.err || "").trim();
  return detail && detail !== title ? `${title}：${detail}` : title;
}

function snapshotTurn(session) {
  return {
    length: session.msgs.length,
    title: session.title,
    lastJudge: session.lastJudge,
    lastReview: session.lastReview,
    lastCache: session.lastCache,
    lastUsage: session.lastUsage,
  };
}

function restoreTurn(session, snapshot) {
  session.msgs.splice(snapshot.length);
  session.title = snapshot.title;
  for (const key of ["lastJudge", "lastReview", "lastCache", "lastUsage"]) {
    if (snapshot[key] === undefined) delete session[key];
    else session[key] = snapshot[key];
  }
}

async function sendTurn(text, options = {}) {
  const value = String(text || "");
  if (busy || !value.trim()) return false;
  if (!cur()) newSession();
  const s = cur();
  if (!s) return false;

  const snapshot = snapshotTurn(s);
  const controller = new AbortController();
  const run = {
    controller,
    operation: options.operation || null,
    sessionId: s.id,
    stopRequested: false,
    serverDone: false,
  };
  const stopRun = async () => {
    if (activeChat !== run) return false;
    run.stopRequested = true;
    // 暂停时若确认框开着，按拒绝收掉——绝不静默放行控制动作。
    denyOpenConfirm();
    setBusy(true, "正在暂停本次运行…");
    controller.abort();
    return true;
  };
  run.stop = stopRun;

  if (run.operation) {
    if (!window.MuliaoRunControl?.isActive(run.operation)) return false;
    window.MuliaoRunControl.update(run.operation, { kind: "chat", onStop: stopRun });
  } else {
    run.operation = window.MuliaoRunControl?.begin("chat", stopRun) || null;
    if (!run.operation) return false;
  }

  busy = true;
  activeChat = run;
  if (s.msgs.length === 0) {
    s.title = titleFrom(value);
    if (cur()?.id === s.id) $("topTitle").textContent = s.title;
    renderSessions($("search").value);
  }
  s.draft = "";
  if (cur()?.id === s.id) syncComposerDraft(s);
  delete s.lastJudge;
  delete s.lastReview;
  delete s.lastCache;
  delete s.lastUsage;

  $("empty").style.display = "none";
  $("insp").innerHTML = "";
  setBusy(true, "幕僚处理中…");

  s.msgs.push({ role: "user", text: value });
  $("thread").appendChild(msgEl("user", value));
  scrollBottom();

  renderJudgeLoading();
  const judgeP = fetch("/api/judge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: value, kind: "judge" }),
    signal: controller.signal,
  }).then(async (response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }).then((judge) => {
    if (activeChat !== run || run.stopRequested) return null;
    s.lastJudge = judge;
    if (cur()?.id === run.sessionId) {
      renderJudgeBar(judge);
      $("insp").appendChild(renderJudgePanel(judge));
    }
    return judge;
  }).catch((error) => {
    if (error?.name === "AbortError") return null;
    const judge = { ok: false, code: "network", err: `本地判断请求失败：${error?.message || error}` };
    if (activeChat === run && !run.stopRequested) {
      s.lastJudge = judge;
      if (cur()?.id === run.sessionId) {
        renderJudgeBar(judge);
        $("insp").appendChild(renderJudgePanel(judge));
      }
    }
    return judge;
  });

  const aEl = msgEl("assistant", "", { think: "" });
  $("thread").appendChild(aEl);
  const body = aEl.querySelector(".msg-body");
  body.classList.add("cursor");
  const tk = aEl._think;
  tk.querySelector(".think-head").onclick = () => { tk.dataset.userToggled = "1"; tk.classList.toggle("open"); };

  const payload = { messages: s.msgs.map((m) => ({ role: m.role, content: m.text })), session_id: s.id };
  let acc = "", think = "", thinkStart = null, usage = null;
  let emptyResponse = false;
  let streamError = null;
  let toolBox = null;
  const ensureToolBox = () => {
    if (!toolBox) {
      toolBox = document.createElement("div");
      toolBox.className = "toolbox";
      tk.after(toolBox);
    }
    return toolBox;
  };

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    if (!resp.body) throw new Error("对话事件流不可用");
    const contentType = resp.headers?.get?.("content-type") || "";
    if (contentType && !contentType.includes("text/event-stream")) throw new Error("对话事件流格式错误");

    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value: chunk } = await reader.read();
      if (done) break;
      buf += dec.decode(chunk, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (activeChat !== run || run.stopRequested) continue;
        if (ev.type === "reasoning") {
          if (thinkStart === null) { thinkStart = performance.now(); setBusy(true, "模型思考中…"); }
          think += ev.text;
          setThinkStreaming(tk, think, performance.now() - thinkStart);
          scrollBottom();
        } else if (ev.type === "delta") {
          if (!thinkStart) thinkStart = performance.now();
          acc += ev.text;
          body.textContent = acc;
          scrollBottom();
          setBusy(true, "幕僚作答中…");
        } else if (ev.type === "tool_call") {
          const box = ensureToolBox();
          const row = document.createElement("div");
          row.className = "toolrow";
          row.dataset.name = ev.name;
          row.dataset.callid = ev.id || "";
          row.innerHTML = `<span class="tr-ico spin"><svg><use href="#i-bolt"/></svg></span>
            <span class="tr-name">${esc(TOOL_CN[ev.name] || ev.name)}</span>
            <span class="tr-state">Jev 门控中…</span>`;
          box.appendChild(row);
          setBusy(true, "读取本机数据…");
          scrollBottom();
        } else if (ev.type === "tool_gate") {
          // Jev 对这次动作的裁决——「Jev 参与每次行为」的可见证据
          const box = ensureToolBox();
          const row = findToolRow(box, ev.id, ev.name);
          if (row) {
            const st = row.querySelector(".tr-state");
            row.classList.remove("gated-allow", "gated-confirm", "gated-deny");
            const riskTxt = `风险 ${Number(ev.risk ?? 0).toFixed(1)}`;
            if (ev.action === "allow") {
              row.classList.add("gated-allow");
              if (st) st.textContent = ev.source === "fallback" ? `Jev 不可用 · 降级放行 · ${riskTxt}` : `Jev 放行 · ${riskTxt}`;
            } else if (ev.action === "confirm") {
              row.classList.add("gated-confirm");
              if (st) st.textContent = `Jev ${riskTxt} · 待确认`;
            } else {
              row.classList.add("gated-deny");
              row.querySelector(".tr-ico")?.classList.remove("spin");
              if (st) st.textContent = ev.source === "fail_closed" ? `Jev 不可用 · 已拒绝` : `Jev 拒绝`;
            }
          }
        } else if (ev.type === "tool_confirm") {
          openConfirmDialog(ev);
        } else if (ev.type === "tool_result") {
          const box = ensureToolBox();
          const row = findToolRow(box, ev.id, ev.name);
          if (row) {
            row.querySelector(".tr-ico").classList.remove("spin");
            const st = row.querySelector(".tr-state");
            st.textContent = ev.denied ? "未授权 · 已拒绝" : `${ev.ms}ms · ${(ev.bytes / 1024).toFixed(1)}KB`;
            if (!row.classList.contains("gated-confirm") && !row.classList.contains("gated-deny")) {
              row.classList.add(ev.denied ? "denied" : "ok");
            } else if (ev.denied) {
              row.classList.add("denied");
            }
          }
        } else if (ev.type === "notice") {
          const box = ensureToolBox();
          const notice = document.createElement("div");
          notice.className = "toolnotice";
          notice.textContent = "ⓘ " + ev.message;
          box.appendChild(notice);
        } else if (ev.type === "cache") {
          s.lastCache = ev.cache;
          if (cur()?.id === run.sessionId) renderCache(ev.cache);
        } else if (ev.type === "usage") {
          usage = ev.usage;
        } else if (ev.type === "empty_response") {
          emptyResponse = true;
          if (!acc.trim()) body.textContent = "（模型这次没有返回内容，请重发一次）";
        } else if (ev.type === "error") {
          streamError = new Error(ev.message || "对话上游出错");
          body.textContent = `[出错] ${streamError.message}`;
        } else if (ev.type === "done") {
          run.serverDone = true;
          try { await reader.cancel(); } catch {}
          buf = "";
          break;
        }
      }
      if (run.serverDone) break;
    }
    if (!run.serverDone) throw streamError || new Error("对话事件流意外中断");
  } catch (error) {
    if (!run.serverDone && !(error?.name === "AbortError" && run.stopRequested)) {
      streamError = error;
      body.textContent = `[连接失败] ${error?.message || error}`;
    }
  }

  body.classList.remove("cursor");
  const thinkMs = thinkStart ? Math.round(performance.now() - thinkStart) : 0;
  setThinkDone(tk, think, thinkMs);

  try {
  if (!run.serverDone) {
      controller.abort();
      restoreTurn(s, snapshot);
      recoverSessionDraft(run.sessionId, value);
      saveSessions();
      renderSessions($("search").value);
      updateCounts();
      if (cur()?.id === s.id) renderThread();
      setBusy(false, run.stopRequested ? "本次运行已暂停" : `运行中断：${streamError?.message || "未收到完成信号"}`);
      return false;
    }

    if (!acc.trim()) body.textContent = emptyResponse ? "（模型这次没有返回内容，请重发一次）" : "（无回复）";
    s.msgs.push({ role: "assistant", text: acc, think, thinkMs });
    s.lastUsage = usage;
    window.MuliaoRunControl?.update(run.operation, { kind: "chat-review", onStop: stopRun });

    await judgeP;
    if (!run.stopRequested && acc.trim() && value.trim()) {
      setBusy(true, "Jev 复核中…");
      try {
        const response = await fetch("/api/judge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ state: `用户问题：${value.slice(0, 500)}\n\n幕僚回复：${acc.slice(0, 1200)}`, kind: "review" }),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const review = await response.json();
        if (activeChat === run && !run.stopRequested) {
          s.lastReview = review;
          if (cur()?.id === run.sessionId) renderReview(review, usage);
        }
      } catch (error) {
        if (error?.name !== "AbortError" && activeChat === run) {
          const review = { ok: false, code: "network", err: `本地复核请求失败：${error?.message || error}` };
          s.lastReview = review;
          if (cur()?.id === run.sessionId) renderReview(review, usage);
        }
      }
    }

    saveSessions();
    renderSessions($("search").value);
    updateCounts();
    setBusy(false, run.stopRequested ? "回复已完成，复核已暂停" : "就绪");
    return true;
  } finally {
    if (activeChat === run) activeChat = null;
    busy = false;
    // 回合结束（成功/暂停/错误）都兜底关掉确认框，绝不留下悬挂弹窗。
    closeConfirmDialog();
    window.MuliaoRunControl?.finish(run.operation);
    $("input").focus();
  }
}

window.MuliaoChat = {
  sendTurn,
  cancelActive: () => activeChat?.stop?.() ?? false,
  get active() { return activeChat; },
};

/* ================= 输入框 ================= */
function autoGrow(el) { el.style.height = "auto"; el.style.height = Math.min(190, el.scrollHeight) + "px"; }

/* ================= 权限确认页 ================= */
const PERM_ICON = {
  notifications: "i-bell", processes: "i-cpu", windows: "i-monitor",
  browser: "i-globe", ai_logs: "i-brain", files: "i-doc", system: "i-gauge",
  computer_control: "i-cursor", voice_control: "i-bolt",
};
let permSources = [];
let permCaps = [];
let permSel = {};
let capSel = {};
let permWasAgreed = false;

async function openPermOverlay() {
  const ov = $("permOverlay");
  ov.hidden = false;
  const oldErr = $("permGrantErr");
  if (oldErr) oldErr.remove();
  const g = $("permGrant");
  if (g) { g.innerHTML = "授权并进入幕僚"; g.disabled = true; }
  $("permList").innerHTML = `<div class="insp-empty">正在检测本机可采集的数据源…</div>`;
  try {
    const d = await (await fetch("/api/permissions")).json();
    permSources = d.sources || [];
    permCaps = d.capabilities || [];
    permWasAgreed = !!d.consent?.agreed;
    $("permLater").innerHTML = permWasAgreed ? `取消<span class="hint">不保存本次改动</span>` : `稍后再说<span class="hint">仅用对话，不采集</span>`;
    permSel = {};
    for (const s of permSources) permSel[s.id] = !!s.granted;
    capSel = {};
    for (const c of permCaps) capSel[c.id] = !!c.granted;
    renderPermList();
    renderPermCaps();
    $("permAgree").checked = permWasAgreed;
    syncPermActions();
  } catch (e) {
    $("permList").innerHTML = `<div class="notify-err">读取权限状态失败：${esc(e.message)}</div>`;
  }
}

function renderPermList() {
  $("permList").innerHTML = permSources.map((s) => {
    const on = !!permSel[s.id];
    const avail = s.available !== false;
    const icon = PERM_ICON[s.id] || "i-folder";
    return `<button type="button" class="perm-item ${on ? "on" : ""} ${avail ? "" : "unavail"}" data-id="${s.id}"
      role="switch" aria-checked="${on}" ${avail ? "" : "disabled"}>
      <span class="pi-ico"><svg><use href="#${icon}"/></svg></span>
      <span class="pi-txt">
        <span class="pi-name">${esc(s.name)}
          ${s.sensitive ? `<i class="pi-tag">敏感</i>` : ""}
          ${avail ? `<i class="pi-tag ok">本机可用</i>` : `<i class="pi-tag">本机不可用</i>`}
        </span>
        <span class="pi-desc">${esc(s.desc)}</span>
        <span class="pi-detail">${esc(s.detail || "")}</span>
        ${s.note ? `<span class="pi-note">⚠ ${esc(s.note)}</span>` : ""}
      </span>
      <span class="switch" aria-hidden="true"></span>
    </button>`;
  }).join("");
  $("permList").querySelectorAll(".perm-item").forEach((el) => {
    el.onclick = () => {
      const s = permSources.find((x) => x.id === el.dataset.id);
      if (s && s.available === false) return;   // 本机不可用的源不让勾
      permSel[el.dataset.id] = !permSel[el.dataset.id];
      el.classList.toggle("on", permSel[el.dataset.id]);
      el.setAttribute("aria-checked", String(permSel[el.dataset.id]));
      syncPermActions();
    };
  });
  syncPermActions();
}

function renderPermCaps() {
  const list = $("permCapList");
  if (!list) return;
  if (!permCaps.length) { list.innerHTML = ""; return; }
  list.innerHTML = permCaps.map((c) => {
    const on = !!capSel[c.id];
    const avail = c.available !== false;
    const icon = PERM_ICON[c.id] || "i-bolt";
    return `<button type="button" class="perm-item cap ${on ? "on" : ""} ${avail ? "" : "unavail"}" data-id="${c.id}"
      role="switch" aria-checked="${on}" ${avail ? "" : "disabled"}>
      <span class="pi-ico"><svg><use href="#${icon}"/></svg></span>
      <span class="pi-txt">
        <span class="pi-name">${esc(c.name)}
          ${c.sensitive ? `<i class="pi-tag">敏感</i>` : ""}
          ${avail ? `<i class="pi-tag ok">本机可用</i>` : `<i class="pi-tag">本机不可用</i>`}
        </span>
        <span class="pi-desc">${esc(c.desc)}</span>
        <span class="pi-detail">${esc(c.detail || "")}</span>
        ${c.note ? `<span class="pi-note">⚠ ${esc(c.note)}</span>` : ""}
      </span>
      <span class="switch" aria-hidden="true"></span>
    </button>`;
  }).join("");
  list.querySelectorAll(".perm-item").forEach((el) => {
    el.onclick = async () => {
      const cap = permCaps.find((x) => x.id === el.dataset.id);
      if (!cap || cap.available === false) return;   // 本机不可用的能力不让开
      const next = !capSel[el.dataset.id];
      el.disabled = true;
      try {
        // 能力开关走 /scope（agree 只接受数据类 scope），即时生效。
        const r = await fetch("/api/permissions/scope", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scope: cap.id, on: next }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        capSel[el.dataset.id] = next;
        cap.granted = next;
        el.classList.toggle("on", next);
        el.setAttribute("aria-checked", String(next));
        pollStatus();
        updatePermHint();
      } catch (e) {
        alert(`切换失败：${e.message}`);
      } finally {
        el.disabled = !(cap.available !== false);
      }
    };
  });
}

function syncPermActions() {
  const n = Object.values(permSel).filter(Boolean).length;
  $("permCount").textContent = `已选 ${n} / ${permSources.length} 项`;
  const grant = $("permGrant");
  grant.textContent = n ? (permWasAgreed ? "保存授权设置" : "授权并进入幕僚") : "确认并仅使用对话";
  grant.disabled = !$("permAgree").checked || permSources.length === 0;
}

function closePermOverlay() { $("permOverlay").hidden = true; }

function initPerm() {
  $("permAgree").onchange = syncPermActions;
  $("permSelectAll").onclick = () => {
    for (const s of permSources) if (s.available !== false) permSel[s.id] = true;
    renderPermList();
  };
  $("permSelectNone").onclick = () => {
    for (const s of permSources) permSel[s.id] = false;
    renderPermList();
  };
  $("permGrant").onclick = async () => {
    const scopes = permSources.filter((s) => permSel[s.id]).map((s) => s.id);
    $("permGrant").disabled = true;
    $("permGrant").innerHTML = "授权中…";
    try {
      await fetch("/api/permissions/agree", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scopes }),
      });
      permWasAgreed = true;
      syncPermActions();
      closePermOverlay();
      afterGrant();
    } catch (e) {
      $("permGrant").disabled = false;
      $("permGrant").innerHTML = "授权并进入幕僚";
      alert("授权失败：" + e.message);
    }
  };
  $("permLater").onclick = async () => {
    if (permWasAgreed) {
      // 权限管理模式：取消只关闭，不改当前授权。
      closePermOverlay();
      return;
    }
    // 首次启动：只记录「看过条款」，不授权任何采集。
    try {
      await fetch("/api/permissions/agree", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scopes: [] }),
      });
    } catch {}
    closePermOverlay();
    afterGrant();
  };
  $("privacyBtn").onclick = openPermOverlay;

  // 高风险动作确认弹窗
  const allowBtn = $("confirmAllow"), denyBtn = $("confirmDeny"), cov = $("confirmOverlay");
  if (allowBtn) allowBtn.onclick = () => submitConfirm("allow");
  if (denyBtn) denyBtn.onclick = () => submitConfirm("deny");
  if (cov) cov.addEventListener("mousedown", (e) => { if (e.target === cov) submitConfirm("deny"); });
}

// Esc 拒绝确认弹窗（仅当弹窗打开时）
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("confirmOverlay") && !$("confirmOverlay").hidden) {
    e.preventDefault();
    submitConfirm("deny");
  }
});

// 授权完成后：刷新引擎状态、按授权情况决定通知面板能否抓
function afterGrant() {
  pollStatus();
  // 只 light 抓取（不打 Jev，省额度）；用户点开「系统通知」标签时自动做完整分级
  const scopes = permSel || {};
  if (scopes.notifications) loadNotifications(true);
  updatePermHint();
}

function updatePermHint() {
  fetch("/api/permissions").then((r) => r.json()).then((d) => {
    const granted = (d.consent?.scopes ? Object.entries(d.consent.scopes).filter(([, v]) => v).map(([k]) => k) : []);
    const tip = document.querySelector(".side-foot .tip");
    if (tip) {
      tip.textContent = granted.length
        ? `已授权 ${granted.length} 项采集 · 数据只在本机`
        : "未授权采集 · 仅对话与 Jev 判断";
    }
  }).catch(() => {});
}

/* ================= 对话模型选择 ================= */
// 模型来自 /api/models：上游实探（verified）+ 官方文档目录（未验证）。
// 点选 → POST /api/models/select 切运行时活动模型，立即对后续回合生效（不重启、不写盘）。
let modelMenuLoaded = false;
let modelMenuLoading = false;
let modelActiveId = "";

function closeModelMenu() {
  const menu = $("modelMenu"), btn = $("modelSel");
  if (menu) menu.hidden = true;
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function openModelMenu() {
  const menu = $("modelMenu"), btn = $("modelSel");
  if (!menu || !btn) return;
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  if (!modelMenuLoaded && !modelMenuLoading) loadModels(false);
}

function toggleModelMenu() {
  const menu = $("modelMenu");
  if (!menu) return;
  if (menu.hidden) openModelMenu(); else closeModelMenu();
}

function modelFootHtml(d) {
  if (d.probe_ok) {
    const n = (d.models || []).filter((m) => m.verified).length;
    return `<div class="mm-foot">已连上游 · ${n} 个授权模型实探可用</div>`;
  }
  const err = String(d.probe_err || "").slice(0, 140);
  // 区分「key 无效/额度」与「网络抖动」，给可操作提示，不笼统报错。
  const isAuth = /401|invalid[_ ]?api[_ ]?key|api-?key|unauthor|额度|quota|forbidden|403/i.test(err);
  const cls = isAuth ? "err" : "warn";
  const msg = isAuth
    ? "上游未授权该 Key（401 / invalid_api_key）：以下是官方文档目录，待有效 Key 后点「刷新」自动列出真实可用模型。"
    : "暂未能连上游探测模型，以下为官方文档目录；网络恢复后点「刷新」重试。";
  return `<div class="mm-foot ${cls}">${esc(msg)}${err ? `<br><code style="color:var(--dimmer)">${esc(err)}</code>` : ""}</div>`;
}

function renderModelMenu(d) {
  const menu = $("modelMenu");
  if (!menu) return;
  const models = Array.isArray(d.models) ? d.models : [];
  modelActiveId = d.active || "";
  const items = models.map((m) => {
    const active = m.id === modelActiveId;
    const badge = m.verified
      ? `<span class="mm-badge verified">实探</span>`
      : `<span class="mm-badge doc">文档</span>`;
    const check = active ? `<svg class="mm-check"><use href="#i-chev-r"/></svg>` : "";
    return `<button type="button" class="mm-item${active ? " active" : ""}" data-model="${esc(m.id)}" role="option" aria-selected="${active}">
        <span class="mm-row1">${check}<span class="mm-label">${esc(m.label || m.id)}</span>${badge}</span>
        <span class="mm-id">${esc(m.id)}</span>
        ${m.note ? `<span class="mm-note">${esc(m.note)}</span>` : ""}
      </button>`;
  }).join("");
  menu.innerHTML =
    `<div class="mm-head"><span class="mm-title">对话模型</span>` +
    `<button type="button" class="mm-refresh" id="mmRefresh" title="重新探测上游可用模型">刷新</button></div>` +
    (items || `<div class="mm-foot">没有可用模型</div>`) +
    modelFootHtml(d);
  const rb = $("mmRefresh");
  if (rb) rb.onclick = (e) => { e.stopPropagation(); loadModels(true); };
  menu.querySelectorAll(".mm-item").forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); selectModel(el.getAttribute("data-model")); };
  });
}

async function loadModels(force) {
  if (modelMenuLoading) return;
  modelMenuLoading = true;
  const rb = $("mmRefresh");
  if (rb) { rb.disabled = true; rb.textContent = "探测中…"; }
  try {
    const url = "/api/models" + (force ? "?refresh=1" : "");
    const d = await (await fetch(url)).json();
    modelMenuLoaded = true;
    renderModelMenu(d || {});
    if (d && d.active) { const t = $("modelSelTxt"); if (t) t.textContent = d.active; }
  } catch {
    const menu = $("modelMenu");
    if (menu && !menu.innerHTML) menu.innerHTML = `<div class="mm-foot err">加载模型列表失败</div>`;
  } finally {
    modelMenuLoading = false;
    const rb2 = $("mmRefresh");
    if (rb2) { rb2.disabled = false; rb2.textContent = "刷新"; }
  }
}

async function selectModel(id) {
  if (!id || id === modelActiveId) { closeModelMenu(); return; }
  try {
    const r = await fetch("/api/models/select", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: id }),
    });
    const d = await r.json();
    if (d && d.ok && d.model) {
      modelActiveId = d.model;
      const t = $("modelSelTxt"); if (t) t.textContent = d.model;
      const v = $("val-llm"); if (v) v.textContent = d.model;
      // 菜单里高亮迁移，不重新探测（省一次上游调用）
      const menu = $("modelMenu");
      if (menu) menu.querySelectorAll(".mm-item").forEach((el) => {
        const on = el.getAttribute("data-model") === d.model;
        el.classList.toggle("active", on);
        el.setAttribute("aria-selected", String(on));
        const row = el.querySelector(".mm-row1");
        if (row) {
          const old = row.querySelector(".mm-check"); if (old) old.remove();
          if (on) row.insertAdjacentHTML("afterbegin", `<svg class="mm-check"><use href="#i-chev-r"/></svg>`);
        }
      });
    }
  } catch {}
  closeModelMenu();
}

function initModelPicker() {
  const btn = $("modelSel");
  if (!btn) return;
  btn.onclick = (e) => { e.stopPropagation(); toggleModelMenu(); };
  document.addEventListener("click", (e) => {
    const picker = $("modelPicker");
    if (picker && !picker.contains(e.target)) closeModelMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModelMenu();
  });
  // 立即用「秒回」的 /api/models 把活动模型名填上，不必等较慢的 /api/status。
  // 同时预热一次菜单数据，点开即用。
  loadModels(false);
}

/* ================= 启动 ================= */
loadSessions();
if (!sessions.length) newSession(); else { curId = sessions[0].id; renderSessions(); renderThread(); }
pollStatus(); setInterval(pollStatus, 10000);
updateCounts(); setInterval(updateCounts, 3000);

initTabs();
initNotify();
initPerm();
initModelPicker();

$("newChat").onclick = newSession;
async function dispatchGoal(text) {
  const value = String(text || "");
  if (!value.trim()) return false;
  if (window.MuliaoSwarm?.planGoal) return window.MuliaoSwarm.planGoal(value);
  return sendTurn(value);
}
$("send").onclick = async () => {
  const control = window.MuliaoRunControl;
  if (control && control.mode !== "send") {
    try { await control.stop(); } catch (error) { setBusy(false, `暂停失败：${error?.message || error}`); }
    return;
  }
  const value = $("input").value;
  if (!value.trim()) return;
  const session = cur();
  if (session) session.draft = "";
  $("input").value = "";
  autoGrow($("input"));
  await dispatchGoal(value);
};
$("input").addEventListener("input", (e) => {
  autoGrow(e.target);
  if (cur()) cur().draft = e.target.value;
});
$("input").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!window.MuliaoRunControl || window.MuliaoRunControl.mode === "send") $("send").click();
  }
});
$("search").addEventListener("input", (e) => renderSessions(e.target.value));
$("topTitleBtn").onclick = () => {
  const t = prompt("重命名当前会话：", cur()?.title || "");
  if (t != null && t.trim() && cur()) { cur().title = t.trim().slice(0, 40); $("topTitle").textContent = cur().title; saveSessions(); renderSessions($("search").value); }
};
document.querySelectorAll(".seed").forEach((b) => {
  b.onclick = async () => {
    if (window.MuliaoRunControl && window.MuliaoRunControl.mode !== "send") return;
    await dispatchGoal(b.querySelector("span").textContent);
  };
});

// 快捷键：Ctrl/Cmd+N 新建会话，Ctrl/Cmd+K 聚焦搜索
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase();
  if (k === "n") { e.preventDefault(); newSession(); }
  else if (k === "k") { e.preventDefault(); $("search").focus(); $("search").select(); }
});

// 启动：先查权限状态。未同意条款 → 弹权限确认页；已同意 → 静默抓通知（light）
(async () => {
  try {
    const d = await (await fetch("/api/permissions")).json();
    if (!d.consent?.agreed) {
      openPermOverlay();          // 首次启动必须逐项授权，不授权不采集
      return;
    }
    updatePermHint();
    // 授权过通知才静默抓一次（light：不打 Jev、不切标签）
    const granted = d.consent.scopes || {};
    if (granted.notifications) loadNotifications(true).catch(() => {});
  } catch {
    loadNotifications(true).catch(() => {});
  }
})();
