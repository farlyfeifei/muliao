/* 幕僚 Ghost 蜂群控制器
   只负责 API / SSE / 会话桥接；所有蜂群过程可视化交给 swarm-ui.js。 */
(() => {
  "use strict";

  const UI = () => window.MuliaoSwarmUI;
  let planning = false;
  let pending = null;
  let active = null;
  let aborter = null;

  async function jsonPost(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await response.json(); } catch {}
    if (!response.ok) throw new Error(data.err || data.message || `HTTP ${response.status}`);
    return data;
  }

  async function consumeSSE(response, onEvent) {
    if (!response.ok) {
      let detail = "";
      try { detail = (await response.json()).err || ""; } catch {}
      throw new Error(detail || `HTTP ${response.status}`);
    }
    if (!response.body) throw new Error("蜂群事件流不可用");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        try { onEvent(JSON.parse(line.slice(5).trim())); } catch {}
      }
    }
  }

  function addUserGoal(goal) {
    if (!cur()) newSession();
    const session = cur();
    if (!session) return null;
    if (session.msgs.length === 0) {
      session.title = titleFrom(goal);
      $("topTitle").textContent = session.title;
    }
    $("empty").style.display = "none";
    session.msgs.push({ role: "user", text: goal });
    $("thread").appendChild(msgEl("user", goal));
    saveSessions();
    renderSessions($("search").value);
    updateCounts();
    scrollBottom();
    return session;
  }

  function addFinalAnswer(text, meta) {
    const session = cur();
    const value = String(text || "蜂群任务已完成，详情与产物见右侧蜂群面板。");
    if (session) {
      session.msgs.push({ role: "assistant", text: value, think: "", thinkMs: meta?.duration_ms || 0, swarmRunId: active?.runId });
      saveSessions();
      renderSessions($("search").value);
      updateCounts();
    }
    $("thread").appendChild(msgEl("assistant", value));
    scrollBottom();
  }

  function shouldUseSwarm(plan) {
    if (!plan) return false;
    if (plan.recipe_id === "single" || plan.recipe === "single") return false;
    if (plan.swarm_worthy === false) return false;
    const p = Number(plan.swarm_worthy_probability ?? plan.jev_answers?.swarm_worthy?.noul ?? 1);
    return p > 0.6;
  }

  async function planGoal(goal, options = {}) {
    const text = String(goal || "").trim();
    if (!text || planning) return;
    planning = true;
    $("send").disabled = true;
    UI()?.reset();
    UI()?.setConnection("connecting");
    document.querySelector(".app")?.classList.remove("no-insp");
    gotoTab("swarm");
    setBusy(true, "Jev 正在编排蜂群…");
    try {
      const data = await jsonPost("/api/swarm/plan", {
        goal: text,
        session_id: cur()?.id || `s${Date.now()}`,
        source: options.source || "chat",
        source_ref: options.sourceRef || null,
      });
      const plan = data.plan || data;
      if (!shouldUseSwarm(plan)) {
        UI()?.setConnection("connected");
        gotoTab("insp");
        await sendTurn(text);
        return;
      }
      pending = { goal: text, plan, source: options.source || "chat" };
      UI()?.renderPlan(plan);
      UI()?.setConnection("connected");
      setBusy(false, plan.requires_confirmation === false ? "蜂群计划已就绪" : "等待确认派蜂");
      // 即使低风险也展示计划并由用户确认，避免复杂任务在用户不知情时消耗多模型调用。
    } catch (error) {
      UI()?.handleEvent({ type: "swarm.error", payload: { message: error.message } });
      UI()?.setConnection("error");
      gotoTab("insp");
      await sendTurn(text);
    } finally {
      planning = false;
      $("send").disabled = false;
      $("input").focus();
    }
  }

  async function runPending(detail) {
    if (!pending || active) return;
    const goal = pending.goal;
    const plan = detail?.plan || pending.plan;
    const session = addUserGoal(goal);
    if (!session) return;
    pending = null;
    active = { runId: plan.run_id || plan.runId || null, sessionId: session.id, goal, plan, terminal: null };
    aborter = new AbortController();
    UI()?.setConnection("connecting");
    gotoTab("swarm");
    setBusy(true, "蜂群运行中…");
    $("swarmCancel").disabled = false;
    try {
      const response = await fetch("/api/swarm/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, plan, session_id: session.id, confirmed: true }),
        signal: aborter.signal,
      });
      await consumeSSE(response, (event) => {
        if (event.run_id && !active.runId) active.runId = event.run_id;
        UI()?.handleEvent(event);
        if (["swarm.done", "swarm.error", "swarm.cancelled", "swarm.waiting_user", "swarm.skipped"].includes(event.type)) {
          active.terminal = event;
        }
        if (event.type === "swarm.done") {
          const payload = event.payload || {};
          const integrator = payload.results?.integrator || payload.results?.integration || {};
          addFinalAnswer(payload.final_text || payload.summary || integrator.text || integrator.output || integrator.summary, payload);
        }
      });
      const terminal = active?.terminal;
      if (!terminal) throw new Error("蜂群事件流未返回终态");
      if (terminal.type === "swarm.done") {
        UI()?.setConnection("connected");
        setBusy(false, "蜂群任务完成");
      } else if (terminal.type === "swarm.cancelled") {
        UI()?.setConnection("disconnected");
        setBusy(false, "蜂群任务已取消");
      } else if (terminal.type === "swarm.waiting_user") {
        UI()?.setConnection("connected");
        setBusy(false, "蜂群等待确认");
      } else if (terminal.type === "swarm.skipped") {
        UI()?.setConnection("connected");
        setBusy(false, "已改用单 Agent");
      } else {
        const message = terminal.payload?.message || terminal.payload?.error || "蜂群任务失败";
        throw new Error(message);
      }
    } catch (error) {
      if (error.name !== "AbortError") {
        UI()?.handleEvent({ type: "swarm.error", payload: { message: error.message } });
        UI()?.setConnection("error");
        setBusy(false, "蜂群任务失败");
      }
    } finally {
      active = null;
      aborter = null;
      $("swarmCancel").disabled = true;
      $("send").disabled = false;
      $("input").focus();
    }
  }

  async function cancelActive() {
    if (pending && !active) {
      discardPlan();
      return;
    }
    if (!active) return;
    const runId = active.runId;
    try {
      if (!runId) throw new Error("任务尚未建立运行 ID");
      const result = await jsonPost(`/api/swarm/${encodeURIComponent(runId)}/cancel`, {});
      if (result.ok === false || result.cancelled === false) throw new Error(result.err || "服务端未接受取消请求");
      aborter?.abort();
      UI()?.handleEvent({ type: "swarm.cancelled", run_id: runId, payload: { reason: "用户取消" } });
      UI()?.setConnection("disconnected");
      active = null;
      setBusy(false, "蜂群任务已取消");
    } catch (error) {
      UI()?.handleEvent({ type: "swarm.error", run_id: runId, payload: { message: `取消失败：${error.message}` } });
      setBusy(false, "取消请求失败");
    }
  }

  function useSingle() {
    if (!pending) return;
    const goal = pending.goal;
    pending = null;
    UI()?.reset();
    gotoTab("insp");
    sendTurn(goal);
  }

  function discardPlan() {
    pending = null;
    UI()?.reset();
    UI()?.setConnection("idle");
    setBusy(false, "就绪");
    $("input").focus();
  }

  function installSendInterceptors() {
    $("send").onclick = () => {
      const value = $("input").value;
      $("input").value = "";
      autoGrow($("input"));
      planGoal(value);
    };
    document.querySelectorAll(".seed").forEach((button) => {
      button.onclick = () => planGoal(button.querySelector("span")?.textContent || "");
    });
  }

  window.addEventListener("muliao-swarm-confirm", (event) => runPending(event.detail));
  window.addEventListener("muliao-swarm-single", useSingle);
  window.addEventListener("muliao-swarm-cancel-plan", discardPlan);
  window.addEventListener("muliao-swarm-cancel-run", cancelActive);
  window.addEventListener("muliao-swarm-toggle-pause", () => {
    UI()?.handleEvent({ type: "swarm.notice", payload: { message: "暂停将在下一里程碑接入；当前可取消任务。" } });
  });

  installSendInterceptors();
  window.MuliaoSwarm = { planGoal, runPending, cancelActive, get active() { return active; } };
})();
