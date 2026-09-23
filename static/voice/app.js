(() => {
  "use strict";

  const apiBase = "/api/voice";
  const byId = (id) => document.getElementById(id);

  const ui = {
    console: byId("voiceConsole"),
    connection: byId("connectionState"),
    permission: byId("permissionState"),
    permissionLabel: byId("permissionLabel"),
    permissionHint: byId("permissionHint"),
    runtimeState: byId("runtimeState"),
    generation: byId("generationValue"),
    subscribers: byId("subscriberValue"),
    start: byId("startVoice"),
    stop: byId("stopVoice"),
    testForm: byId("testForm"),
    testCommand: byId("testCommand"),
    testButton: byId("testCommandButton"),
    testResult: byId("testResult"),
    transcriptTitle: byId("transcriptTitle"),
    transcript: byId("transcriptText"),
    partial: byId("partialText"),
    lastEventTime: byId("lastEventTime"),
    latestDecision: byId("latestDecision"),
    eventList: byId("eventList"),
    emptyLedger: byId("emptyLedger"),
    clearEvents: byId("clearEvents"),
  };

  const stateLabels = {
    booting: "初始化",
    stopped: "已停止",
    denied: "权限关闭",
    starting: "启动中",
    running: "监听中",
    listening: "等待语音",
    recognizing: "识别中",
    deciding: "判断中",
    compound: "复合命令",
    completed: "已完成",
    rejected: "已拒绝",
    cancelled: "已取消",
    stopping: "停止中",
    sleeping: "等待唤醒",
    wake_detected: "已唤醒",
    error: "运行错误",
  };

  let source = null;
  let reconnectTimer = null;
  let retryDelay = 800;
  let status = {
    state: "booting",
    running: false,
    authorized: false,
    generation: 0,
    subscribers: 0,
  };

  const text = (value, fallback = "—") => {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  };

  const formatTime = (seconds) => {
    const date = seconds ? new Date(Number(seconds) * 1000) : new Date();
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  };

  const setConnection = (connected, label) => {
    ui.connection.dataset.connected = connected ? "true" : "false";
    ui.connection.querySelector("span:last-child").textContent = label;
  };

  const readError = async (response) => {
    let body = null;
    try {
      body = await response.json();
    } catch (_) {
      body = null;
    }
    const detail = body && body.detail ? body.detail : body;
    if (detail && typeof detail === "object") {
      return detail.message || detail.detail || detail.code || `请求失败 (${response.status})`;
    }
    return typeof detail === "string" ? detail : `请求失败 (${response.status})`;
  };

  const request = async (path, options = {}) => {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${apiBase}${path}`, { ...options, headers });
    if (!response.ok) throw new Error(await readError(response));
    return response.json();
  };

  const renderStatus = (next) => {
    status = { ...status, ...next };
    const state = status.state || "stopped";
    const busy = ["starting", "running", "listening", "recognizing", "deciding", "compound", "stopping"].includes(state);
    const stopping = state === "stopping";
    const authorized = Boolean(status.authorized);

    ui.console.dataset.state = state;
    ui.permission.dataset.authorized = authorized ? "true" : "false";
    ui.permissionLabel.textContent = authorized ? "语音控制已授权" : "语音控制未授权";
    ui.permissionHint.textContent = authorized
      ? "可以手动启动监听；服务不会自行打开麦克风。"
      : "请先在幕僚权限页开启 voice_control。";
    ui.runtimeState.textContent = stateLabels[state] || state;
    ui.generation.textContent = text(status.generation, "0");
    ui.subscribers.textContent = text(status.subscribers, "0");
    ui.start.disabled = !authorized || busy;
    ui.stop.disabled = !busy || stopping;
    ui.testButton.disabled = !authorized;

    if (!authorized) {
      ui.transcriptTitle.textContent = "授权闸门关闭";
      ui.transcript.textContent = "麦克风、Jev 与动作链均未启动。";
      ui.partial.textContent = "";
    } else if (state === "running" || state === "listening") {
      ui.transcriptTitle.textContent = "正在监听";
      if (!ui.transcript.dataset.final) {
        ui.transcript.textContent = "请说“幕僚幕僚”，再下达命令。";
      }
    } else if (state === "recognizing") {
      ui.transcriptTitle.textContent = "正在识别";
    } else if (state === "deciding") {
      ui.transcriptTitle.textContent = "Jev 正在判断";
    } else if (state === "stopped") {
      ui.transcriptTitle.textContent = "等待启动";
    }
  };

  const eventKind = (eventType) => {
    const leaf = eventType.replace(/^voice\./, "");
    if (["decision", "confirmation"].includes(leaf)) return "decision";
    if (["action", "result"].includes(leaf)) return "action";
    if (leaf === "tts") return "tts";
    if (leaf === "metric") return "metric";
    if (leaf === "error") return "error";
    return "state";
  };

  const summarize = (event) => {
    const payload = event.payload || {};
    switch (event.type) {
      case "voice.state":
        return `状态：${stateLabels[payload.state] || text(payload.state)}`;
      case "voice.partial":
        return `临时字幕：${text(payload.text)}`;
      case "voice.final":
        return `最终命令：${text(payload.text)}`;
      case "voice.decision":
        return `${payload.accepted ? "接受" : "拒绝"} · ${text(payload.kind)} / ${text(payload.target)} · 置信度 ${formatConfidence(payload.confidence)}`;
      case "voice.action":
        return `${payload.ok ? "动作完成" : "动作失败"} · ${text(payload.action)} · ${text(payload.detail, "无详情")}`;
      case "voice.tts":
        return `播报${payload.state === "finished" ? "结束" : "开始"} · ${text(payload.text, payload.backend)}`;
      case "voice.metric":
        return `${text(payload.name)} = ${text(payload.value)}`;
      case "voice.error":
        return `${text(payload.code, "voice_error")} · ${text(payload.detail, "未提供详情")}`;
      case "voice.result":
        return `${payload.dry_run ? "dry-run" : "运行结果"} · ${text(payload.status, payload.result && payload.result.status)}`;
      case "voice.status":
        return `服务快照 · ${stateLabels[payload.state] || text(payload.state)}`;
      default:
        return compactPayload(payload);
    }
  };

  const compactPayload = (payload) => {
    const entries = Object.entries(payload || {}).slice(0, 4);
    if (!entries.length) return "无附加数据";
    return entries.map(([key, value]) => {
      const shown = typeof value === "object" ? JSON.stringify(value) : value;
      return `${key}: ${text(shown)}`;
    }).join(" · ");
  };

  const formatConfidence = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? `${Math.round(number * 100)}%` : "—";
  };

  const addEvent = (event) => {
    if (!event || typeof event.type !== "string" || !event.type.startsWith("voice.")) return;
    if (ui.emptyLedger) ui.emptyLedger.hidden = true;

    const item = document.createElement("li");
    item.className = "event-item";
    item.dataset.kind = eventKind(event.type);

    const meta = document.createElement("div");
    meta.className = "event-meta";

    const typeName = document.createElement("span");
    typeName.className = "event-type";
    typeName.textContent = `${text(event.seq, "?")} · ${event.type}`;

    const time = document.createElement("time");
    time.dateTime = event.ts ? new Date(event.ts * 1000).toISOString() : "";
    time.textContent = formatTime(event.ts);

    const summary = document.createElement("p");
    summary.className = "event-summary";
    summary.textContent = summarize(event);

    meta.append(typeName, time);
    item.append(meta, summary);
    ui.eventList.prepend(item);

    while (ui.eventList.querySelectorAll(".event-item").length > 120) {
      ui.eventList.lastElementChild.remove();
    }
    ui.lastEventTime.textContent = `最近事件 ${formatTime(event.ts)}`;
  };

  const handleEvent = (event) => {
    if (!event || typeof event.type !== "string" || !event.type.startsWith("voice.")) return;
    const payload = event.payload || {};
    addEvent(event);

    if (event.type === "voice.status") {
      renderStatus(payload);
      return;
    }
    if (event.type === "voice.state") {
      renderStatus({ state: payload.state, running: !["stopped", "cancelled", "error"].includes(payload.state) });
      return;
    }
    if (event.type === "voice.partial") {
      ui.partial.textContent = text(payload.text, payload.raw_text);
      ui.transcriptTitle.textContent = "识别中";
      return;
    }
    if (event.type === "voice.final") {
      ui.transcript.dataset.final = "true";
      ui.transcript.textContent = text(payload.text, "命令为空");
      ui.partial.textContent = "";
      ui.transcriptTitle.textContent = payload.source === "active_session" ? "连续命令" : "已捕获命令";
      return;
    }
    if (event.type === "voice.decision") {
      const verdict = payload.accepted ? "已接受" : "已拒绝";
      ui.latestDecision.querySelector("strong").textContent = `${verdict} · ${text(payload.kind)} / ${text(payload.target)}`;
      ui.latestDecision.querySelector("p").textContent = `命令：${text(payload.command)}；置信度：${formatConfidence(payload.confidence)}${payload.destructive ? "；需要确认" : ""}`;
      return;
    }
    if (event.type === "voice.error") {
      ui.transcriptTitle.textContent = "语音链路错误";
      ui.partial.textContent = `${text(payload.code, "voice_error")}：${text(payload.detail, "请检查事件卷宗")}`;
    }
  };

  const connectEvents = () => {
    if (source) source.close();
    clearTimeout(reconnectTimer);
    setConnection(false, "正在接入事件流");
    source = new EventSource(`${apiBase}/events`);

    source.onopen = () => {
      retryDelay = 800;
      setConnection(true, "voice.* 事件流已连接");
    };

    const types = [
      "voice.status",
      "voice.state",
      "voice.partial",
      "voice.final",
      "voice.decision",
      "voice.confirmation",
      "voice.action",
      "voice.tts",
      "voice.metric",
      "voice.error",
      "voice.result",
    ];
    types.forEach((type) => {
      source.addEventListener(type, (message) => {
        try {
          handleEvent(JSON.parse(message.data));
        } catch (error) {
          handleEvent({
            type: "voice.error",
            seq: "local",
            ts: Date.now() / 1000,
            payload: { code: "invalid_sse", detail: error.message },
          });
        }
      });
    });

    source.onerror = () => {
      setConnection(false, "事件流中断，准备重连");
      source.close();
      source = null;
      reconnectTimer = window.setTimeout(connectEvents, retryDelay);
      retryDelay = Math.min(retryDelay * 1.8, 8000);
    };
  };

  const refreshStatus = async () => {
    try {
      renderStatus(await request("/status"));
    } catch (error) {
      renderStatus({ state: "error" });
      ui.testResult.dataset.error = "true";
      ui.testResult.textContent = error.message;
    }
  };

  ui.start.addEventListener("click", async () => {
    ui.start.disabled = true;
    ui.transcript.dataset.final = "";
    try {
      renderStatus(await request("/start", { method: "POST", body: "{}" }));
    } catch (error) {
      ui.testResult.dataset.error = "true";
      ui.testResult.textContent = error.message;
      await refreshStatus();
    }
  });

  ui.stop.addEventListener("click", async () => {
    ui.stop.disabled = true;
    try {
      renderStatus(await request("/stop", { method: "POST", body: "{}" }));
      ui.partial.textContent = "当前操作已取消，运行资源已请求释放。";
    } catch (error) {
      ui.testResult.dataset.error = "true";
      ui.testResult.textContent = error.message;
      await refreshStatus();
    }
  });

  ui.testForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const command = ui.testCommand.value.trim();
    if (!command) return;
    ui.testButton.disabled = true;
    ui.testResult.dataset.error = "false";
    ui.testResult.textContent = "正在验证，不会执行系统动作……";
    try {
      const result = await request("/command/test", {
        method: "POST",
        body: JSON.stringify({ text: command }),
      });
      const voiceResult = result.result || {};
      ui.testResult.textContent = `dry-run 完成：${text(voiceResult.status, "已返回")}`;
    } catch (error) {
      ui.testResult.dataset.error = "true";
      ui.testResult.textContent = error.message;
    } finally {
      ui.testButton.disabled = !status.authorized;
    }
  });

  ui.clearEvents.addEventListener("click", () => {
    ui.eventList.querySelectorAll(".event-item").forEach((item) => item.remove());
    if (ui.emptyLedger) ui.emptyLedger.hidden = false;
  });

  window.addEventListener("beforeunload", () => {
    clearTimeout(reconnectTimer);
    if (source) source.close();
  });

  renderStatus(status);
  refreshStatus();
  connectEvents();
})();
