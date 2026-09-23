/* 幕僚 Ghost 蜂群控制器
   只负责 API / SSE / 会话桥接；所有蜂群过程可视化交给 swarm-ui.js。 */
(() => {
  "use strict";

  const UI = () => window.MuliaoSwarmUI;
  let planning = false;
  let pending = null;
  let active = null;
  let singleFallback = null;

  function syncLifecycleControls(message) {
    const canResumeSingle = !!(
      pending?.mode === "single" && cur()?.id === pending.sessionId
    );
    const blocked = !!(planning || active || singleFallback || (pending && !canResumeSingle));
    $("send").disabled = blocked;
    $("swarmCancel").disabled = !active || !!active.terminal || active.cancelState !== "idle";
    if (active) {
      setBusy(true, message || (active.cancelState === "requesting" ? "正在取消蜂群…" : "蜂群运行中…"));
    } else if (singleFallback) {
      setBusy(true, message || "正在改用单 Agent…");
    } else if (planning) {
      setBusy(true, message || "Jev 正在编排蜂群…");
    } else if (pending) {
      setBusy(true, message || (
        pending.mode === "single"
          ? (canResumeSingle ? "可改用单 Agent 继续" : "等待返回发起会话")
          : "等待确认派蜂"
      ));
    } else {
      setBusy(false, message || "就绪");
    }
  }

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
        let event;
        try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }
        onEvent(event);
      }
    }
  }

  function acceptRunEnvelope(run, event) {
    if (!event || typeof event !== "object") return false;
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    const eventRunId = String(event.run_id || event.runId || payload.run_id || payload.runId || "");
    if (eventRunId && run.runId && eventRunId !== run.runId) return false;
    if (eventRunId && !run.runId) run.runId = eventRunId;
    const eventId = String(event.event_id || event.eventId || payload.event_id || payload.eventId || "");
    if (eventId && run.seenEventIds.has(eventId)) return false;
    const rawSeq = event.seq ?? event.sequence ?? payload.seq ?? payload.sequence;
    const seq = Number(rawSeq);
    if (Number.isFinite(seq)) {
      if (run.lastSeq !== null && seq <= run.lastSeq) return false;
      run.lastSeq = seq;
    }
    if (eventId) run.seenEventIds.add(eventId);
    return true;
  }

  function addUserGoal(goal, sessionId) {
    if (!cur() && !sessionId) newSession();
    const session = sessionId ? sessionById(sessionId) : cur();
    if (!session) {
      UI()?.handleEvent({ type: "swarm.notice", payload: { message: "发起会话已不存在，已停止写入。" } });
      return null;
    }
    if (session.msgs.length === 0) {
      session.title = titleFrom(goal);
      if (cur()?.id === session.id) $("topTitle").textContent = session.title;
    }
    session.msgs.push({ role: "user", text: goal });
    saveSessions();
    renderSessions($("search").value);
    updateCounts();
    if (cur()?.id === session.id) {
      $("empty").style.display = "none";
      $("thread").appendChild(msgEl("user", goal));
      scrollBottom();
    }
    return session;
  }

  function sessionById(sessionId) {
    return sessions.find((session) => session.id === sessionId) || null;
  }

  function addFinalAnswer(text, meta, run) {
    const session = sessionById(run?.sessionId);
    const value = String(text || "蜂群任务已完成，详情与产物见右侧蜂群面板。");
    if (session) {
      session.msgs.push({ role: "assistant", text: value, think: "", thinkMs: meta?.duration_ms || 0, swarmRunId: run?.runId });
      saveSessions();
      renderSessions($("search").value);
      updateCounts();
    }
    // 用户可能在蜂群运行时切换会话；只在原会话仍可见时追加到当前线程。
    if (cur()?.id === run?.sessionId) {
      $("thread").appendChild(msgEl("assistant", value));
      scrollBottom();
    }
  }

  function shouldUseSwarm(plan) {
    if (!plan) return false;
    if (plan.recipe_id === "single" || plan.recipe === "single") return false;
    if (plan.swarm_worthy === false) return false;
    const p = Number(plan.swarm_worthy_probability ?? plan.jev_answers?.swarm_worthy?.noul ?? 1);
    return p > 0.6;
  }

  async function startSingleForOrigin(request) {
    if (!request || singleFallback) return false;
    const origin = sessionById(request.sessionId);
    if (!origin) {
      pending = null;
      UI()?.reset();
      UI()?.handleEvent({
        type: "swarm.notice",
        payload: { message: "发起会话已删除，已清除待处理任务。" },
      });
      UI()?.setConnection("idle");
      syncLifecycleControls("发起会话已删除，待处理任务已清除");
      return false;
    }
    if (cur()?.id !== origin.id) {
      pending = { ...request, sessionId: origin.id, mode: "single" };
      UI()?.handleEvent({
        type: "swarm.notice",
        payload: { message: "该任务来自其他会话，请返回发起会话后再改用单 Agent。" },
      });
      syncLifecycleControls("等待返回发起会话");
      return false;
    }
    const fallback = { sessionId: origin.id, goal: request.goal };
    pending = null;
    singleFallback = fallback;
    UI()?.reset();
    gotoTab("insp");
    syncLifecycleControls("正在改用单 Agent…");
    try {
      await sendTurn(request.goal);
      return true;
    } finally {
      if (singleFallback === fallback) singleFallback = null;
      syncLifecycleControls("就绪");
    }
  }

  async function planGoal(goal, options = {}) {
    const text = String(goal || "").trim();
    if (!text) return;
    if (planning || active || singleFallback || pending) {
      $("input").value = text;
      autoGrow($("input"));
      $("input").focus();
      syncLifecycleControls(
        active ? "蜂群运行中，请先取消或等待完成"
          : singleFallback ? "单 Agent 正在处理，请等待完成"
            : "已有蜂群计划等待处理"
      );
      return;
    }
    if (!cur()) newSession();
    const plannedSessionId = cur()?.id || null;
    if (!plannedSessionId) {
      $("input").value = text;
      autoGrow($("input"));
      UI()?.handleEvent({ type: "swarm.notice", payload: { message: "无法建立发起会话，请重试。" } });
      syncLifecycleControls("无法建立发起会话");
      return;
    }
    planning = true;
    syncLifecycleControls("Jev 正在编排蜂群…");
    UI()?.reset();
    UI()?.setConnection("connecting");
    document.querySelector(".app")?.classList.remove("no-insp");
    gotoTab("swarm");
    setBusy(true, "Jev 正在编排蜂群…");
    try {
      const data = await jsonPost("/api/swarm/plan", {
        goal: text,
        session_id: plannedSessionId,
        source: options.source || "chat",
        source_ref: options.sourceRef || null,
      });
      const plan = data.plan || data;
      if (!shouldUseSwarm(plan)) {
        UI()?.setConnection("connected");
        planning = false;
        await startSingleForOrigin({
          goal: text,
          sessionId: plannedSessionId,
          source: options.source || "chat",
          sourceRef: options.sourceRef || null,
        });
        return;
      }
      pending = { goal: text, plan, source: options.source || "chat", sourceRef: options.sourceRef || null, sessionId: plannedSessionId };
      UI()?.renderPlan(plan);
      UI()?.setConnection("connected");
      syncLifecycleControls(plan.requires_confirmation === false ? "蜂群计划已就绪" : "等待确认派蜂");
      // 即使低风险也展示计划并由用户确认，避免复杂任务在用户不知情时消耗多模型调用。
    } catch (error) {
      UI()?.handleEvent({ type: "swarm.notice", payload: { message: `蜂群规划失败：${error.message}` } });
      UI()?.setConnection("error");
      planning = false;
      await startSingleForOrigin({
        goal: text,
        sessionId: plannedSessionId,
        source: options.source || "chat",
        sourceRef: options.sourceRef || null,
      });
    } finally {
      planning = false;
      syncLifecycleControls();
      $("input").focus();
    }
  }

  async function runPending(detail) {
    if (!pending || active || singleFallback) return;
    const goal = pending.goal;
    const plannedSessionId = pending.sessionId;
    const plan = detail?.plan || pending.plan;
    const session = addUserGoal(goal, plannedSessionId);
    if (!session) {
      pending = null;
      UI()?.reset();
      UI()?.handleEvent({
        type: "swarm.notice",
        payload: { message: "发起会话已删除，蜂群计划已清除。" },
      });
      UI()?.setConnection("idle");
      syncLifecycleControls("发起会话已删除，蜂群计划已清除");
      return;
    }
    pending = null;
    const controller = new AbortController();
    const run = {
      runId: plan.run_id || plan.runId || null,
      sessionId: session.id,
      goal,
      plan,
      terminal: null,
      cancelled: false,
      cancelState: "idle",
      lastSeq: null,
      seenEventIds: new Set(),
      controller,
    };
    active = run;
    UI()?.markConfirmed?.(run.runId);
    UI()?.setConnection("connecting");
    gotoTab("swarm");
    syncLifecycleControls("蜂群运行中…");
    try {
      const response = await fetch("/api/swarm/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, plan, session_id: session.id, confirmed: true }),
        signal: controller.signal,
      });
      await consumeSSE(response, (event) => {
        // 取消、被后续状态替换、旧 run、重复或倒序事件一律丢弃。
        if (active !== run || run.cancelled || run.terminal) return;
        if (!acceptRunEnvelope(run, event)) return;
        UI()?.handleEvent(event);
        if (["swarm.done", "swarm.error", "swarm.cancelled", "swarm.waiting_user", "swarm.skipped"].includes(event.type)) {
          run.terminal = event;
        }
        if (event.type === "swarm.done") {
          const payload = event.payload || {};
          const integrator = payload.results?.integrator || payload.results?.integration || {};
          addFinalAnswer(payload.final_text || payload.summary || integrator.text || integrator.output || integrator.summary, payload, run);
        }
      });
      if (run.cancelled) return;
      const terminal = run.terminal;
      if (!terminal) throw new Error("蜂群事件流未返回终态");
      if (terminal.type === "swarm.done") {
        UI()?.setConnection("connected");
        run.finishMessage = "蜂群任务完成";
      } else if (terminal.type === "swarm.cancelled") {
        UI()?.setConnection("disconnected");
        run.finishMessage = "蜂群任务已取消";
      } else if (terminal.type === "swarm.waiting_user") {
        UI()?.setConnection("connected");
        run.finishMessage = "蜂群等待确认";
      } else if (terminal.type === "swarm.skipped") {
        UI()?.setConnection("connected");
        run.finishMessage = "已改用单 Agent";
      } else {
        const message = terminal.payload?.message || terminal.payload?.error || "蜂群任务失败";
        throw new Error(message);
      }
    } catch (error) {
      if (error.name !== "AbortError") {
        run.finishMessage = "蜂群任务失败";
        UI()?.handleEvent({ type: "swarm.error", payload: { message: error.message } });
        UI()?.setConnection("error");
      }
    } finally {
      if (active === run) active = null;
      syncLifecycleControls(run.finishMessage);
      $("input").focus();
    }
  }

  async function cancelActive() {
    if (pending && !active) {
      discardPlan();
      return;
    }
    const run = active;
    if (!run || run.cancelState === "requesting") return;
    const runId = run.runId;
    run.cancelState = "requesting";
    syncLifecycleControls("正在取消蜂群…");
    try {
      if (!runId) throw new Error("任务尚未建立运行 ID");
      const result = await jsonPost(`/api/swarm/${encodeURIComponent(runId)}/cancel`, {});
      if (active !== run || run.terminal) return;
      if (result.ok !== true || result.cancel_requested !== true) {
        throw new Error(result.err || "服务端未接受取消请求");
      }
      run.cancelled = true;
      run.cancelState = "accepted";
      run.finishMessage = "蜂群任务已取消";
      run.controller.abort();
      UI()?.handleEvent({ type: "swarm.cancelled", run_id: runId, payload: { reason: "用户取消" } });
      UI()?.setConnection("disconnected");
      syncLifecycleControls("蜂群任务已取消");
    } catch (error) {
      if (active === run) {
        run.cancelState = "idle";
        UI()?.handleEvent({
          type: "swarm.notice",
          run_id: runId,
          payload: { message: `取消失败，蜂群仍在运行：${error.message}` },
        });
        syncLifecycleControls("取消失败，蜂群仍在运行");
      }
    }
  }

  async function useSingle() {
    if (!pending) return;
    await startSingleForOrigin({ ...pending });
  }

  function discardPlan() {
    pending = null;
    UI()?.reset();
    UI()?.setConnection("idle");
    syncLifecycleControls("就绪");
    $("input").focus();
  }

  function installSendInterceptors() {
    $("sessions")?.addEventListener?.("click", () => {
      setTimeout(() => syncLifecycleControls(), 0);
    });
    $("send").onclick = () => {
      if (pending?.mode === "single" && cur()?.id === pending.sessionId) {
        useSingle();
        return;
      }
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

  installSendInterceptors();
  window.MuliaoSwarm = { planGoal, runPending, cancelActive, get active() { return active; } };
})();
