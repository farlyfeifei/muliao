/* 幕僚 Ghost 蜂群控制器
   只负责 API / SSE / 会话桥接；所有蜂群过程可视化交给 swarm-ui.js。 */
(() => {
  "use strict";

  const UI = () => window.MuliaoSwarmUI;
  const RunControl = () => window.MuliaoRunControl;
  let planning = false;
  let planningRequest = null;
  let pending = null;
  let active = null;
  let singleFallback = null;
  let lifecycleOperation = null;

  function beginLifecycle(kind, onStop) {
    const control = RunControl();
    const operation = control
      ? control.begin(kind, onStop)
      : { kind: String(kind || "running"), onStop, localFallback: true };
    if (operation) lifecycleOperation = operation;
    return operation;
  }

  function updateLifecycle(operation, kind, onStop) {
    const control = RunControl();
    if (!operation) return false;
    if (!control) {
      operation.kind = String(kind || operation.kind);
      if (typeof onStop === "function") operation.onStop = onStop;
      return true;
    }
    return control.update(operation, { kind, onStop });
  }

  function finishLifecycle(operation = lifecycleOperation) {
    const control = RunControl();
    if (control && operation) control.finish(operation);
    if (lifecycleOperation === operation) lifecycleOperation = null;
  }

  function syncLifecycleControls(message) {
    const canResumeSingle = !!(
      pending?.mode === "single" && cur()?.id === pending.sessionId
    );
    const blocked = !!(planning || active || singleFallback || (pending && !canResumeSingle));
    $("send").disabled = RunControl()?.mode === "stopping" || (!RunControl() && blocked);
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

  async function jsonPost(url, body, signal) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal,
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
        const keepReading = onEvent(event);
        if (keepReading === false) {
          try { await reader.cancel(); } catch {}
          return;
        }
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

  async function stopPlanning(request = planningRequest) {
    if (!request || request.stopped) return false;
    request.stopped = true;
    request.controller.abort();
    window.MuliaoDrafts?.recover?.(request.sessionId, request.goal);
    setBusy(true, "正在暂停蜂群规划…");
    return true;
  }

  async function cancelPending({ restoreGoal = false } = {}) {
    const request = pending;
    if (!request || active || request.cancelPromise) return request?.cancelPromise || false;
    const runId = request.plan?.run_id || request.plan?.runId;
    request.cancelPromise = (async () => {
      try {
        if (runId) {
          const result = await jsonPost(`/api/swarm/${encodeURIComponent(runId)}/cancel`, {});
          const terminalStatus = ["cancelled", "cancelling", "completed", "failed", "skipped"].includes(String(result.status || ""));
          if (result.ok !== true || (result.cancel_requested !== true && !terminalStatus)) {
            throw new Error(result.err || "服务端未接受取消请求");
          }
        }
        if (pending !== request) return true;
        pending = null;
        if (restoreGoal) window.MuliaoDrafts?.recover?.(request.sessionId, request.goal);
        UI()?.reset();
        UI()?.setConnection("idle");
        finishLifecycle(request.operation);
        syncLifecycleControls(restoreGoal ? "蜂群计划已暂停" : "蜂群计划已取消");
        $("input").focus();
        return true;
      } catch (error) {
        if (pending === request) delete request.cancelPromise;
        UI()?.handleEvent({
          type: "swarm.notice",
          run_id: runId,
          payload: { message: `取消计划失败：${error.message}` },
        });
        syncLifecycleControls("取消计划失败，仍等待处理");
        return false;
      }
    })();
    return request.cancelPromise;
  }

  async function startSingleForOrigin(request) {
    if (!request || singleFallback || request.cancelPromise) return false;
    const operation = request.operation || lifecycleOperation;
    const origin = sessionById(request.sessionId);
    if (!origin) {
      pending = null;
      finishLifecycle(operation);
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
      pending = { ...request, operation, sessionId: origin.id, mode: "single" };
      updateLifecycle(operation, "pending", () => cancelPending({ restoreGoal: true }));
      UI()?.handleEvent({
        type: "swarm.notice",
        payload: { message: "该任务来自其他会话，请返回发起会话后再改用单 Agent。" },
      });
      syncLifecycleControls("等待返回发起会话");
      return false;
    }
    const fallback = { sessionId: origin.id, goal: request.goal, operation };
    pending = null;
    singleFallback = fallback;
    UI()?.reset();
    gotoTab("insp");
    updateLifecycle(operation, "chat", () => window.MuliaoChat?.cancelActive?.());
    syncLifecycleControls("正在改用单 Agent…");
    let completed = false;
    try {
      completed = await sendTurn(request.goal, { operation });
      return completed;
    } finally {
      if (singleFallback === fallback) singleFallback = null;
      if (!window.MuliaoChat?.active) finishLifecycle(operation);
      syncLifecycleControls(completed ? "就绪" : "本次运行已暂停");
    }
  }

  async function planGoal(goal, options = {}) {
    const text = String(goal || "").trim();
    if (!text) return false;
    if (planning || active || singleFallback || pending || RunControl()?.current) {
      $("input").value = text;
      autoGrow($("input"));
      $("input").focus();
      syncLifecycleControls(
        active ? "蜂群运行中，请先暂停或等待完成"
          : singleFallback ? "单 Agent 正在处理，请先暂停或等待完成"
            : "已有任务等待处理"
      );
      return false;
    }
    if (!cur()) newSession();
    const plannedSessionId = cur()?.id || null;
    if (!plannedSessionId) {
      $("input").value = text;
      autoGrow($("input"));
      UI()?.handleEvent({ type: "swarm.notice", payload: { message: "无法建立发起会话，请重试。" } });
      syncLifecycleControls("无法建立发起会话");
      return false;
    }

    const controller = new AbortController();
    const request = {
      goal: text,
      sessionId: plannedSessionId,
      source: options.source || "chat",
      sourceRef: options.sourceRef || null,
      controller,
      stopped: false,
      operation: null,
    };
    request.operation = beginLifecycle("planning", () => stopPlanning(request));
    if (!request.operation) {
      $("input").value = text;
      autoGrow($("input"));
      return false;
    }
    planningRequest = request;
    planning = true;
    syncLifecycleControls("Jev 正在编排蜂群…");
    UI()?.reset();
    UI()?.setConnection("connecting");
    document.querySelector(".app")?.classList.remove("no-insp");
    gotoTab("swarm");
    try {
      const data = await jsonPost("/api/swarm/plan", {
        goal: text,
        session_id: plannedSessionId,
        source: request.source,
        source_ref: request.sourceRef,
      }, controller.signal);
      if (planningRequest !== request || request.stopped) return false;
      const plan = data.plan || data;
      planning = false;
      if (!shouldUseSwarm(plan)) {
        UI()?.setConnection("connected");
        return startSingleForOrigin({ ...request, plan });
      }
      pending = { ...request, plan };
      planningRequest = null;
      updateLifecycle(request.operation, "pending", () => cancelPending());
      UI()?.renderPlan(plan);
      UI()?.setConnection("connected");
      syncLifecycleControls(plan.requires_confirmation === false ? "蜂群计划已就绪" : "等待确认派蜂");
      return true;
    } catch (error) {
      if (error?.name === "AbortError" && request.stopped) {
        UI()?.reset();
        UI()?.setConnection("idle");
        finishLifecycle(request.operation);
        setBusy(false, "蜂群规划已暂停");
        return false;
      }
      UI()?.handleEvent({ type: "swarm.notice", payload: { message: `蜂群规划失败：${error.message}` } });
      UI()?.setConnection("error");
      planning = false;
      return startSingleForOrigin({ ...request });
    } finally {
      if (planningRequest === request) planningRequest = null;
      planning = false;
      if (!pending && !singleFallback && !active && !window.MuliaoChat?.active) finishLifecycle(request.operation);
      syncLifecycleControls();
      $("input").focus();
    }
  }

  async function runPending(detail) {
    if (!pending || pending.cancelPromise || active || singleFallback) return false;
    const request = pending;
    const operation = request.operation || lifecycleOperation;
    const goal = request.goal;
    const plannedSessionId = request.sessionId;
    const plan = detail?.plan || request.plan;
    const originSession = sessionById(plannedSessionId);
    const historySnapshot = originSession ? {
      length: originSession.msgs.length,
      title: originSession.title,
    } : null;
    const session = addUserGoal(goal, plannedSessionId);
    if (!session) {
      pending = null;
      finishLifecycle(operation);
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
      operation,
      historySnapshot,
      answerCommitted: false,
    };
    active = run;
    updateLifecycle(operation, "swarm", cancelActive);
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
        if (["swarm.done", "swarm.error", "swarm.cancelled", "swarm.waiting_user", "swarm.skipped", "swarm.paused"].includes(event.type)) {
          run.terminal = event;
        }
        if (event.type === "swarm.done") {
          const payload = event.payload || {};
          const integrator = payload.results?.integrator || payload.results?.integration || {};
          run.answerCommitted = true;
          addFinalAnswer(payload.final_text || payload.summary || integrator.text || integrator.output || integrator.summary, payload, run);
        }
        return !run.terminal;
      });
      const terminal = run.terminal;
      if (!terminal) throw new Error("蜂群事件流未返回终态");
      if (terminal.type === "swarm.done") {
        UI()?.setConnection("connected");
        run.finishMessage = "蜂群任务完成";
      } else if (terminal.type === "swarm.cancelled") {
        run.cancelled = true;
        UI()?.setConnection("disconnected");
        run.finishMessage = "蜂群任务已取消";
      } else if (terminal.type === "swarm.waiting_user") {
        // 可恢复状态：保留 pending 句柄供续跑/取消；当前里程碑无续跑 API，回滚目标进草稿。
        UI()?.setConnection("connected");
        run.finishMessage = "蜂群等待用户输入（已保留目标草稿）";
        window.MuliaoDrafts?.recover?.(run.sessionId, run.goal);
      } else if (terminal.type === "swarm.paused") {
        UI()?.setConnection("connected");
        run.finishMessage = "蜂群已暂停";
      } else if (terminal.type === "swarm.skipped") {
        // 后端判定不适用蜂群：回滚目标并放回草稿，不虚报「已改用单 Agent」。
        run.cancelled = true;
        UI()?.setConnection("idle");
        run.finishMessage = "蜂群不适用，目标已放回草稿";
      } else if (terminal.type === "swarm.error") {
        const message = terminal.payload?.message || terminal.payload?.error || "蜂群任务失败";
        run.finishMessage = "蜂群任务失败";
        UI()?.setConnection("error");
        throw new Error(message);
      }
    } catch (error) {
      if (run.finishMessage) {
        // 终态权威；终态后的连接关闭不能反转结果。
      } else if (error.name !== "AbortError") {
        run.finishMessage = "蜂群任务失败";
        UI()?.handleEvent({ type: "swarm.error", payload: { message: error.message } });
        UI()?.setConnection("error");
      }
    } finally {
      if (run.cancelled && !run.answerCommitted && run.historySnapshot) {
        const origin = sessionById(run.sessionId);
        if (origin) {
          origin.msgs.splice(run.historySnapshot.length);
          if (origin.title === titleFrom(run.goal)) origin.title = run.historySnapshot.title;
          saveSessions();
          renderSessions($("search").value);
          updateCounts();
          if (cur()?.id === run.sessionId) renderThread();
        }
        window.MuliaoDrafts?.recover?.(run.sessionId, run.goal);
      }
      if (active === run) active = null;
      finishLifecycle(operation);
      syncLifecycleControls(run.finishMessage);
      $("input").focus();
    }
    return true;
  }

  async function cancelActive() {
    if (pending && !active) return cancelPending();
    const run = active;
    if (!run || run.cancelState === "requesting") return false;
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
      return true;
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
      return false;
    }
  }

  async function useSingle() {
    if (!pending) return;
    await startSingleForOrigin({ ...pending });
  }

  function discardPlan() {
    const request = pending;
    if (!request) return false;
    return cancelPending();
  }

  $("sessions")?.addEventListener?.("click", () => {
    setTimeout(() => {
      syncLifecycleControls();
      if (pending?.mode === "single" && cur()?.id === pending.sessionId && !singleFallback) {
        useSingle();
      }
    }, 0);
  });

  window.addEventListener("muliao-swarm-confirm", (event) => runPending(event.detail));
  window.addEventListener("muliao-swarm-single", useSingle);
  window.addEventListener("muliao-swarm-cancel-plan", discardPlan);
  window.addEventListener("muliao-swarm-cancel-run", cancelActive);

  window.MuliaoSwarm = {
    planGoal,
    runPending,
    cancelActive,
    cancelPending,
    stopPlanning,
    get active() { return active; },
    get pending() { return pending; },
    get planning() { return planning; },
  };
})();
