/* 幕僚 Muliáo · 蜂群任务右栏可视化
   纯渲染模块：不发起网络请求，不持有 API 凭据，不修改宿主标签逻辑。 */
(function initMuliaoSwarmUI(global) {
  "use strict";

  const MAX_EVENTS = 180;
  const MAX_STREAM_TEXT = 1800;

  const EVENT_LABELS = {
    "swarm.plan": "蜂群计划",
    "contract.created": "契约建立",
    "swarm.waiting_user": "等待用户",
    "bee.queued": "蜂已排队",
    "bee.start": "蜂开始执行",
    "bee.reasoning": "蜂推演",
    "bee.delta": "蜂输出",
    "bee.tool_call": "工具调用",
    "bee.tool_result": "工具结果",
    "bee.check": "Jev 检查",
    "bee.correct": "纠偏执行",
    "handoff.created": "创建交接",
    "handoff.ack": "交接确认",
    "fact.conflicted": "事实冲突",
    "fact.invalidated": "事实失效",
    "artifact.created": "产物生成",
    "artifact.stale": "产物过期",
    "swarm.paused": "蜂群暂停",
    "swarm.cancelled": "蜂群取消",
    "swarm.done": "蜂群完成",
    "swarm.skipped": "改用单 Agent",
    "swarm.error": "蜂群异常",
  };

  const STATUS_LABELS = {
    idle: "待命",
    planning: "规划中",
    planned: "待执行",
    pending: "待处理",
    queued: "排队中",
    running: "执行中",
    reasoning: "推演中",
    waiting: "等你确认",
    blocked: "受阻",
    paused: "已暂停",
    correcting: "纠偏中",
    ready: "已生成",
    acknowledged: "已确认",
    done: "已完成",
    error: "异常",
    cancelled: "已取消",
    conflict: "有冲突",
    invalidated: "已失效",
    stale: "已过期",
    unknown: "未知",
  };

  const STATUS_ALIASES = {
    plan: "planned",
    planning: "planning",
    planned: "planned",
    pending: "pending",
    queue: "queued",
    queued: "queued",
    start: "running",
    started: "running",
    active: "running",
    working: "running",
    in_progress: "running",
    "in-progress": "running",
    reasoning: "reasoning",
    wait: "waiting",
    waiting: "waiting",
    waiting_user: "waiting",
    requires_confirmation: "waiting",
    needs_input: "waiting",
    blocked: "blocked",
    pause: "paused",
    paused: "paused",
    correct: "correcting",
    correcting: "correcting",
    corrected: "running",
    created: "ready",
    ready: "ready",
    ack: "acknowledged",
    acked: "acknowledged",
    acknowledged: "acknowledged",
    complete: "done",
    completed: "done",
    success: "done",
    succeeded: "done",
    done: "done",
    fail: "error",
    failed: "error",
    error: "error",
    cancel: "cancelled",
    canceled: "cancelled",
    cancelled: "cancelled",
    conflict: "conflict",
    conflicted: "conflict",
    invalid: "invalidated",
    invalidated: "invalidated",
    stale: "stale",
    idle: "idle",
  };

  const CONNECTION_LABELS = {
    unknown: "连接未知",
    connecting: "连接中",
    connected: "已连接",
    disconnected: "未连接",
    error: "连接异常",
  };

  function initialState(connection) {
    return {
      version: 1,
      plan: null,
      connection: connection || { status: "unknown", detail: "", updatedAt: null },
      swarm: {
        id: "",
        title: "蜂群任务",
        summary: "",
        objective: "",
        status: "idle",
        message: "",
        error: "",
        phaseId: "",
        startedAt: null,
        updatedAt: null,
        finishedAt: null,
      },
      contracts: [],
      phases: [],
      bees: [],
      checks: [],
      handoffs: [],
      conflicts: [],
      artifacts: [],
      budget: {
        limit: null,
        used: null,
        unit: "tokens",
        costLimit: null,
        currency: "USD",
      },
      usage: {
        inputTokens: 0,
        outputTokens: 0,
        reasoningTokens: 0,
        totalTokens: 0,
        cost: null,
        elapsedMs: null,
        toolCalls: 0,
      },
      events: [],
    };
  }

  let state = initialState();
  let domReadyHooked = false;

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function valueAt(source, keys, fallback) {
    if (!source) return fallback;
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null && source[key] !== "") return source[key];
    }
    return fallback;
  }

  function text(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback || "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }

  function short(value, limit) {
    const result = text(value, "").replace(/\s+/g, " ").trim();
    const max = limit || 160;
    return result.length > max ? result.slice(0, max - 1) + "…" : result;
  }

  function numberOf(value) {
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function firstNumber(source, keys) {
    for (const key of keys) {
      const result = numberOf(source && source[key]);
      if (result !== null) return result;
    }
    return null;
  }

  function toArray(value) {
    if (Array.isArray(value)) return value;
    if (isObject(value)) return Object.values(value);
    if (value === undefined || value === null || value === "") return [];
    return [value];
  }

  function timestampOf(value) {
    if (value === undefined || value === null || value === "") return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.getTime();
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) return numeric < 1e12 ? numeric * 1000 : numeric;
    const parsed = Date.parse(String(value));
    return Number.isNaN(parsed) ? null : parsed;
  }

  function eventTime(event, data) {
    return timestampOf(valueAt(data, ["ts", "timestamp", "time", "created_at", "createdAt", "updated_at", "updatedAt"], null))
      || timestampOf(valueAt(event, ["ts", "timestamp", "time", "created_at", "createdAt"], null))
      || Date.now();
  }

  function normalizeStatus(value, fallback) {
    const raw = text(value, "").trim().toLowerCase().replace(/\s+/g, "_");
    return STATUS_ALIASES[raw] || fallback || "unknown";
  }

  function statusClass(value) {
    const status = normalizeStatus(value, "unknown");
    if (["running", "reasoning", "planning", "correcting"].includes(status)) return "running";
    if (["done", "ready", "acknowledged"].includes(status)) return "done";
    if (["waiting", "paused", "queued", "planned", "pending"].includes(status)) return "waiting";
    if (["error", "blocked", "conflict", "invalidated", "stale"].includes(status)) return "issue";
    if (status === "cancelled") return "muted";
    return "neutral";
  }

  function statusLabel(value) {
    const status = normalizeStatus(value, "unknown");
    return STATUS_LABELS[status] || STATUS_LABELS.unknown;
  }

  function clone(value) {
    if (typeof structuredClone === "function") {
      try { return structuredClone(value); } catch (_) {}
    }
    return JSON.parse(JSON.stringify(value));
  }

  function create(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function append(parent) {
    for (let index = 1; index < arguments.length; index += 1) {
      const child = arguments[index];
      if (child !== undefined && child !== null && child !== false) parent.appendChild(child);
    }
    return parent;
  }

  function setOptionalTitle(node, value) {
    const title = short(value, 400);
    if (title) node.setAttribute("title", title);
    return node;
  }

  function formatInteger(value) {
    const numeric = numberOf(value);
    if (numeric === null) return "—";
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(numeric);
  }

  function formatCompact(value) {
    const numeric = numberOf(value);
    if (numeric === null) return "—";
    if (Math.abs(numeric) < 1000) return String(Math.round(numeric));
    return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(numeric);
  }

  function formatDuration(value) {
    const ms = numberOf(value);
    if (ms === null) return "—";
    if (ms < 1000) return Math.round(ms) + "ms";
    if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + "s";
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return minutes + "m " + seconds + "s";
  }

  function formatClock(value) {
    const ts = timestampOf(value);
    if (!ts) return "—";
    const date = new Date(ts);
    const pad = (part) => String(part).padStart(2, "0");
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function formatCost(value, currency) {
    const numeric = numberOf(value);
    if (numeric === null) return "—";
    const code = text(currency, "USD").toUpperCase();
    try {
      return new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: code,
        maximumFractionDigits: numeric < 1 ? 4 : 2,
      }).format(numeric);
    } catch (_) {
      return numeric.toFixed(numeric < 1 ? 4 : 2) + " " + code;
    }
  }

  function percent(value) {
    const numeric = numberOf(value);
    if (numeric === null) return "";
    const normalized = numeric <= 1 ? numeric * 100 : numeric;
    return Math.max(0, Math.min(100, normalized)).toFixed(normalized < 10 ? 1 : 0) + "%";
  }

  function payloadOf(event) {
    if (!isObject(event)) return {};
    const payload = isObject(event.payload) ? event.payload : {};
    const data = isObject(event.data) ? event.data : {};
    return Object.assign({}, event, payload, data);
  }

  function sourceEntity(data, singular) {
    const candidate = data && data[singular];
    return isObject(candidate) ? Object.assign({}, data, candidate) : data || {};
  }

  function entityId(entity, prefix, fallbackIndex) {
    return text(valueAt(entity, ["id", prefix + "_id", prefix + "Id", "key", "slug"], ""), "")
      || prefix + "-" + String((fallbackIndex || 0) + 1);
  }

  function upsert(list, item) {
    const index = list.findIndex((entry) => entry.id === item.id);
    if (index >= 0) {
      list[index] = Object.assign({}, list[index], item, { order: list[index].order });
      return list[index];
    }
    const next = Object.assign({ order: list.length }, item);
    list.push(next);
    return next;
  }

  function resetTask(preserveConnection) {
    const connection = preserveConnection ? state.connection : undefined;
    state = initialState(connection ? Object.assign({}, connection) : undefined);
  }

  function normalizeContract(raw, index) {
    const item = isObject(raw) ? raw : { title: raw };
    const acceptance = toArray(valueAt(item, ["acceptance", "acceptance_criteria", "acceptance_tests", "criteria", "checks"], []));
    const constraints = toArray(valueAt(item, ["constraints", "required_constraints", "rules", "guardrails"], []));
    return {
      id: entityId(item, "contract", index),
      title: text(valueAt(item, ["title", "name", "deliverable", "output"], "契约 " + (index + 1))),
      description: text(valueAt(item, ["description", "summary", "scope", "objective"], "")),
      owner: text(valueAt(item, ["owner", "assignee", "bee", "agent"], "")),
      status: normalizeStatus(valueAt(item, ["status", "state"], "planned"), "planned"),
      acceptance: acceptance.map((entry) => short(valueAt(entry, ["title", "name", "text", "description"], entry), 120)).filter(Boolean),
      constraints: constraints.map((entry) => short(valueAt(entry, ["title", "name", "text", "description"], entry), 120)).filter(Boolean),
    };
  }

  function normalizePhase(raw, index) {
    const item = isObject(raw) ? raw : { title: raw };
    return {
      id: entityId(item, "phase", index),
      title: text(valueAt(item, ["title", "name", "label"], "阶段 " + (index + 1))),
      summary: text(valueAt(item, ["summary", "description", "goal", "objective"], "")),
      status: normalizeStatus(valueAt(item, ["status", "state"], "pending"), "pending"),
      dependsOn: toArray(valueAt(item, ["depends_on", "dependsOn", "dependencies"], [])).map((entry) => short(entry, 80)),
      startedAt: timestampOf(valueAt(item, ["started_at", "startedAt"], null)),
      finishedAt: timestampOf(valueAt(item, ["finished_at", "finishedAt", "completed_at", "completedAt"], null)),
    };
  }

  function normalizeBee(raw, index, phaseId) {
    const item = typeof raw === "string" ? { id: raw, name: raw, role: raw } : isObject(raw) ? raw : { name: raw };
    const nested = isObject(item.bee) ? Object.assign({}, item, item.bee) : item;
    return {
      id: entityId(nested, "bee", index),
      name: text(valueAt(nested, ["name", "title", "label", "bee_name", "agent_name"], "蜂 " + (index + 1))),
      role: text(valueAt(nested, ["role", "specialty", "persona", "type"], "执行蜂")),
      task: text(valueAt(nested, ["task", "objective", "goal", "assignment", "description"], "")),
      status: normalizeStatus(valueAt(nested, ["status", "state"], "queued"), "queued"),
      phaseId: text(valueAt(nested, ["phase_id", "phaseId", "stage_id", "stageId", "stage"], phaseId || "")),
      model: text(valueAt(nested, ["model", "engine"], "")),
      reasoning: text(valueAt(nested, ["reasoning", "thought", "thinking"], "")),
      output: text(valueAt(nested, ["output", "delta", "text", "result"], "")),
      activeTool: null,
      lastTool: null,
      checkCount: numberOf(valueAt(nested, ["check_count", "checkCount"], 0)) || 0,
      correctionCount: numberOf(valueAt(nested, ["correction_count", "correctionCount"], 0)) || 0,
      toolCalls: numberOf(valueAt(nested, ["tool_calls", "toolCalls"], 0)) || 0,
      startedAt: timestampOf(valueAt(nested, ["started_at", "startedAt"], null)),
      updatedAt: timestampOf(valueAt(nested, ["updated_at", "updatedAt"], null)),
      finishedAt: timestampOf(valueAt(nested, ["finished_at", "finishedAt", "completed_at", "completedAt"], null)),
    };
  }

  function normalizeHandoff(raw, index) {
    const item = isObject(raw) ? raw : { summary: raw };
    return {
      id: text(valueAt(item, ["capsule_id", "capsuleId", "message_id", "messageId", "id", "handoff_id", "handoffId", "key", "slug"], "")) || "handoff-" + String(index + 1),
      from: text(valueAt(item, ["from", "from_bee", "fromBee", "source", "sender"], "")),
      to: text(valueAt(item, ["to", "to_bee", "toBee", "target", "receiver"], "")),
      summary: text(valueAt(item, ["summary", "description", "message", "context"], "")),
      artifact: text(valueAt(item, ["artifact", "artifact_id", "artifactId", "output"], "")),
      status: normalizeStatus(valueAt(item, ["status", "state"], "pending"), "pending"),
      createdAt: timestampOf(valueAt(item, ["created_at", "createdAt", "ts"], null)),
      ackAt: timestampOf(valueAt(item, ["ack_at", "ackAt", "acknowledged_at", "acknowledgedAt"], null)),
    };
  }

  function normalizeConflict(raw, index, fallbackStatus) {
    const item = isObject(raw) ? raw : { statement: raw };
    return {
      id: entityId(item, "fact", index),
      statement: text(valueAt(item, ["statement", "fact", "title", "claim", "name"], "事实 " + (index + 1))),
      detail: text(valueAt(item, ["detail", "description", "reason", "message"], "")),
      sources: toArray(valueAt(item, ["sources", "evidence", "candidates"], [])).map((entry) => short(valueAt(entry, ["title", "name", "value", "text"], entry), 100)).filter(Boolean),
      status: normalizeStatus(valueAt(item, ["status", "state"], fallbackStatus || "conflict"), fallbackStatus || "conflict"),
      updatedAt: timestampOf(valueAt(item, ["updated_at", "updatedAt", "ts"], null)),
    };
  }

  function normalizeArtifact(raw, index, fallbackStatus) {
    const item = isObject(raw) ? raw : { name: raw };
    return {
      id: entityId(item, "artifact", index),
      name: text(valueAt(item, ["name", "title", "filename", "label"], "产物 " + (index + 1))),
      type: text(valueAt(item, ["type", "kind", "mime", "format"], "")),
      path: text(valueAt(item, ["path", "url", "uri", "location"], "")),
      summary: text(valueAt(item, ["summary", "description", "message"], "")),
      producer: text(valueAt(item, ["producer", "bee", "bee_id", "beeId", "agent"], "")),
      status: normalizeStatus(valueAt(item, ["status", "state"], fallbackStatus || "ready"), fallbackStatus || "ready"),
      createdAt: timestampOf(valueAt(item, ["created_at", "createdAt", "ts"], null)),
      updatedAt: timestampOf(valueAt(item, ["updated_at", "updatedAt", "ts"], null)),
    };
  }

  function normalizeCheck(raw, index, kind) {
    const item = isObject(raw) ? raw : { message: raw };
    const confidence = firstNumber(item, ["confidence", "score", "probability", "p"]);
    const verdictRaw = text(valueAt(item, ["verdict", "result", "status", "state", "decision", "action"], kind === "correction" ? "correcting" : "pending"));
    let verdict = normalizeStatus(verdictRaw, "pending");
    const action = verdictRaw.toLowerCase();
    if (["accept", "pass", "passed", "ok", "valid", "approved"].includes(action)) verdict = "done";
    if (["retry", "correct"].includes(action)) verdict = "correcting";
    if (["need_context", "escalate"].includes(action)) verdict = "blocked";
    if (action === "pause") verdict = "paused";
    if (["flag", "failed", "reject", "rejected", "invalid"].includes(action)) verdict = "error";
    return {
      id: entityId(item, "check", index),
      kind: kind || "check",
      label: text(valueAt(item, ["label", "name", "check", "rule", "title"], kind === "correction" ? "纠偏" : "Jev 检查")),
      message: text(valueAt(item, ["message", "summary", "description", "reason", "correction"], "")),
      verdict,
      confidence,
      beeId: text(valueAt(item, ["bee_id", "beeId", "agent_id", "agentId", "worker_id", "workerId"], "")),
      ts: timestampOf(valueAt(item, ["ts", "timestamp", "created_at", "createdAt"], null)) || Date.now(),
    };
  }

  function mergeBudget(raw) {
    if (!isObject(raw)) return;
    const limit = firstNumber(raw, ["limit", "token_limit", "tokenLimit", "total", "total_tokens", "totalTokens", "max_tokens", "maxTokens", "budget_tokens", "budgetTokens"]);
    const used = firstNumber(raw, ["used", "used_tokens", "usedTokens", "spent", "spent_tokens", "spentTokens", "consumed"]);
    const costLimit = firstNumber(raw, ["cost_limit", "costLimit", "max_cost", "maxCost", "budget_usd", "budgetUsd"]);
    if (limit !== null) state.budget.limit = limit;
    if (used !== null) state.budget.used = used;
    if (costLimit !== null) state.budget.costLimit = costLimit;
    state.budget.unit = text(valueAt(raw, ["unit", "type"], state.budget.unit), state.budget.unit);
    state.budget.currency = text(valueAt(raw, ["currency"], state.budget.currency), state.budget.currency).toUpperCase();
  }

  function mergeUsage(raw) {
    if (!isObject(raw)) return;
    const input = firstNumber(raw, ["input_tokens", "inputTokens", "prompt_tokens", "promptTokens"]);
    const output = firstNumber(raw, ["output_tokens", "outputTokens", "completion_tokens", "completionTokens"]);
    const reasoning = firstNumber(raw, ["reasoning_tokens", "reasoningTokens", "thinking_tokens", "thinkingTokens"]);
    const total = firstNumber(raw, ["total_tokens", "totalTokens", "tokens"]);
    const cost = firstNumber(raw, ["cost", "cost_usd", "costUsd", "spent"]);
    const elapsed = firstNumber(raw, ["elapsed_ms", "elapsedMs", "duration_ms", "durationMs", "ms"]);
    const toolCalls = firstNumber(raw, ["tool_calls", "toolCalls"]);
    if (input !== null) state.usage.inputTokens = input;
    if (output !== null) state.usage.outputTokens = output;
    if (reasoning !== null) state.usage.reasoningTokens = reasoning;
    if (total !== null) state.usage.totalTokens = total;
    else if (input !== null || output !== null || reasoning !== null) {
      state.usage.totalTokens = state.usage.inputTokens + state.usage.outputTokens + state.usage.reasoningTokens;
    }
    if (cost !== null) state.usage.cost = cost;
    if (elapsed !== null) state.usage.elapsedMs = elapsed;
    if (toolCalls !== null) state.usage.toolCalls = toolCalls;
  }

  function applyPlan(plan, replace) {
    const raw = isObject(plan) ? plan : Array.isArray(plan) ? { phases: plan } : { summary: plan };
    const nested = isObject(raw.plan) ? Object.assign({}, raw, raw.plan) : raw;
    if (replace) resetTask(true);

    state.swarm.id = text(valueAt(nested, ["swarm_id", "swarmId", "task_id", "taskId", "id"], state.swarm.id));
    state.swarm.title = text(valueAt(nested, ["title", "name", "task", "mission"], state.swarm.title || "蜂群任务"));
    state.swarm.summary = text(valueAt(nested, ["summary", "description", "brief"], state.swarm.summary));
    state.swarm.objective = text(valueAt(nested, ["objective", "goal", "instruction", "request"], state.swarm.objective));
    state.swarm.status = normalizeStatus(valueAt(nested, ["status", "state"], state.swarm.status === "idle" ? "planned" : state.swarm.status), "planned");
    state.swarm.phaseId = text(valueAt(nested, ["phase_id", "phaseId", "current_phase", "currentPhase"], state.swarm.phaseId));
    state.swarm.startedAt = timestampOf(valueAt(nested, ["started_at", "startedAt"], state.swarm.startedAt));
    state.swarm.updatedAt = Date.now();

    const rawContracts = valueAt(nested, ["contracts", "contract"], []);
    toArray(rawContracts).forEach((item, index) => upsert(state.contracts, normalizeContract(item, index)));

    const rawPhases = valueAt(nested, ["phases", "stages", "steps"], []);
    toArray(rawPhases).forEach((item, index) => {
      const phase = upsert(state.phases, normalizePhase(item, index));
      const phaseBees = valueAt(item, ["bees", "agents", "workers"], []);
      toArray(phaseBees).forEach((bee, beeIndex) => upsert(state.bees, normalizeBee(bee, state.bees.length + beeIndex, phase.id)));
    });

    const rawBees = valueAt(nested, ["bees", "agents", "workers", "executors"], []);
    toArray(rawBees).forEach((item, index) => upsert(state.bees, normalizeBee(item, index, "")));

    toArray(valueAt(nested, ["handoffs"], [])).forEach((item, index) => upsert(state.handoffs, normalizeHandoff(item, index)));
    toArray(valueAt(nested, ["conflicts", "facts"], [])).forEach((item, index) => upsert(state.conflicts, normalizeConflict(item, index, "conflict")));
    toArray(valueAt(nested, ["artifacts", "outputs", "deliverables"], [])).forEach((item, index) => upsert(state.artifacts, normalizeArtifact(item, index, "ready")));

    mergeBudget(valueAt(nested, ["budget", "limits"], {}));
    mergeUsage(valueAt(nested, ["usage", "metrics"], {}));
  }

  function beeIdFrom(data) {
    const nested = isObject(data.bee) ? data.bee : {};
    const explicit = valueAt(data, ["bee_id", "beeId", "agent_id", "agentId", "worker_id", "workerId"], "")
      || valueAt(nested, ["id", "bee_id", "beeId", "agent_id", "agentId"], "");
    if (explicit) return text(explicit);
    const named = text(valueAt(data, ["bee_name", "beeName", "agent_name", "agentName", "name"], ""));
    if (named) {
      const found = state.bees.find((bee) => bee.name === named);
      if (found) return found.id;
    }
    if (state.bees.length === 1) return state.bees[0].id;
    return "bee-unknown";
  }

  function ensureBee(data, fallbackStatus) {
    const id = beeIdFrom(data);
    const nested = isObject(data.bee) ? Object.assign({}, data, data.bee) : data;
    const existing = state.bees.find((bee) => bee.id === id);
    if (existing) return existing;
    const normalized = normalizeBee(Object.assign({}, nested, { id, status: fallbackStatus || "queued" }), state.bees.length, "");
    return upsert(state.bees, normalized);
  }

  function phaseIdFrom(data) {
    return text(valueAt(data, ["phase_id", "phaseId", "stage_id", "stageId", "stage"], ""));
  }

  function touchPhase(data, status) {
    const phaseId = phaseIdFrom(data);
    if (!phaseId) return;
    let phase = state.phases.find((entry) => entry.id === phaseId);
    if (!phase) {
      phase = upsert(state.phases, normalizePhase({ id: phaseId, title: valueAt(data, ["phase_name", "phaseName", "stage_name", "stageName"], phaseId) }, state.phases.length));
    }
    phase.status = normalizeStatus(status, phase.status);
    if (phase.status === "running" && !phase.startedAt) phase.startedAt = Date.now();
    if (phase.status === "done") phase.finishedAt = Date.now();
    state.swarm.phaseId = phaseId;
  }

  function appendStream(previous, delta) {
    const combined = text(previous, "") + text(delta, "");
    return combined.length > MAX_STREAM_TEXT ? "…" + combined.slice(-(MAX_STREAM_TEXT - 1)) : combined;
  }

  function describeEvent(type, data) {
    const beeName = text(valueAt(data, ["bee_name", "beeName", "agent_name", "agentName", "name"], ""));
    const message = valueAt(data, ["message", "summary", "description", "reason", "question", "text", "delta"], "");
    switch (type) {
      case "swarm.plan": return short(valueAt(data, ["summary", "title", "objective", "goal"], "计划已载入"), 150);
      case "contract.created": return short(valueAt(data, ["title", "name", "description"], "执行契约已建立"), 150);
      case "swarm.waiting_user": return short(message || "需要用户确认后继续", 150);
      case "bee.queued": return short((beeName ? beeName + " · " : "") + text(valueAt(data, ["task", "objective"], "进入队列")), 150);
      case "bee.start": return short((beeName ? beeName + " · " : "") + text(valueAt(data, ["task", "objective"], "开始执行")), 150);
      case "bee.reasoning": return short((beeName ? beeName + " · " : "") + text(message, "正在推演"), 150);
      case "bee.delta": return short((beeName ? beeName + " · " : "") + text(message, "输出更新"), 150);
      case "bee.tool_call": return short((beeName ? beeName + " · " : "") + "调用 " + text(valueAt(data, ["tool", "tool_name", "toolName"], "工具"), "工具"), 150);
      case "bee.tool_result": return short((beeName ? beeName + " · " : "") + text(valueAt(data, ["result", "summary", "message", "status"], "工具返回结果")), 150);
      case "bee.check": return short((beeName ? beeName + " · " : "") + text(message || valueAt(data, ["verdict", "result"], "检查完成")), 150);
      case "bee.correct": return short((beeName ? beeName + " · " : "") + text(message || "按检查结果纠偏"), 150);
      case "handoff.created": return short(text(valueAt(data, ["from", "from_bee", "fromBee"], "?")) + " → " + text(valueAt(data, ["to", "to_bee", "toBee"], "?")) + (message ? " · " + text(message) : ""), 150);
      case "handoff.ack": return short(text(valueAt(data, ["to", "to_bee", "toBee"], beeName || "接收方")) + " 已确认交接", 150);
      case "fact.conflicted": return short(valueAt(data, ["statement", "fact", "claim", "title", "message"], "发现事实冲突"), 150);
      case "fact.invalidated": return short(valueAt(data, ["statement", "fact", "claim", "title", "message"], "事实已失效"), 150);
      case "artifact.created": return short(valueAt(data, ["name", "title", "filename", "path"], "新产物已生成"), 150);
      case "artifact.stale": return short(valueAt(data, ["name", "title", "filename", "path"], "产物已过期"), 150);
      case "swarm.paused": return short(message || "蜂群执行已暂停", 150);
      case "swarm.cancelled": return short(message || "蜂群任务已取消", 150);
      case "swarm.done": return short(message || "蜂群任务已完成", 150);
      case "swarm.error": return short(valueAt(data, ["error", "message", "detail"], "蜂群执行异常"), 150);
      default: return short(message || type, 150);
    }
  }

  function eventStatus(type, data) {
    if (type === "swarm.error") return "error";
    if (type === "swarm.cancelled") return "cancelled";
    if (type === "swarm.done" || type === "artifact.created" || type === "handoff.ack") return "done";
    if (type === "fact.conflicted") return "conflict";
    if (type === "fact.invalidated") return "invalidated";
    if (type === "artifact.stale") return "stale";
    if (type === "swarm.waiting_user") return "waiting";
    if (type === "swarm.paused") return "paused";
    if (type.startsWith("bee.")) return normalizeStatus(valueAt(data, ["status", "state", "verdict", "result"], "running"), "running");
    return normalizeStatus(valueAt(data, ["status", "state"], "pending"), "pending");
  }

  function recordEvent(type, event, data, ts) {
    const beeId = type.startsWith("bee.") ? beeIdFrom(data) : "";
    const entry = {
      id: text(valueAt(event, ["event_id", "eventId"], "")) || "event-" + String(ts) + "-" + String(state.events.length + 1),
      type,
      label: EVENT_LABELS[type] || type,
      description: describeEvent(type, data),
      status: eventStatus(type, data),
      beeId,
      ts,
    };
    const last = state.events[state.events.length - 1];
    if (last && ["bee.delta", "bee.reasoning"].includes(type) && last.type === type && last.beeId === beeId) {
      state.events[state.events.length - 1] = entry;
    } else {
      state.events.push(entry);
      if (state.events.length > MAX_EVENTS) state.events.splice(0, state.events.length - MAX_EVENTS);
    }
  }

  function updateUsageFromEvent(data) {
    mergeUsage(isObject(data.usage) ? data.usage : data);
    mergeBudget(isObject(data.budget) ? data.budget : {});
  }

  function handleEvent(event) {
    if (!isObject(event)) return getState();
    const type = text(valueAt(event, ["type", "event", "name"], ""));
    if (!type) return getState();
    const data = payloadOf(event);
    const ts = eventTime(event, data);
    let bee;

    switch (type) {
      case "swarm.plan": {
        const incoming = isObject(data.plan) ? data.plan : data;
        const incomingId = text(valueAt(incoming, ["swarm_id", "swarmId", "task_id", "taskId", "id"], ""));
        const replace = !state.swarm.id || (incomingId && incomingId !== state.swarm.id);
        applyPlan(incoming, replace);
        state.plan = clone(incoming);
        state.swarm.status = normalizeStatus(valueAt(incoming, ["status", "state"], "planned"), "planned");
        break;
      }
      case "contract.created": {
        const contract = normalizeContract(sourceEntity(data, "contract"), state.contracts.length);
        contract.status = normalizeStatus(valueAt(data, ["status", "state"], contract.status), contract.status);
        upsert(state.contracts, contract);
        break;
      }
      case "swarm.waiting_user":
        state.swarm.status = "waiting";
        state.swarm.message = text(valueAt(data, ["question", "message", "summary", "reason"], "需要你确认后继续"));
        break;
      case "bee.queued":
        bee = ensureBee(data, "queued");
        Object.assign(bee, normalizeBee(Object.assign({}, data, { id: bee.id, status: "queued" }), bee.order || 0, bee.phaseId));
        bee.status = "queued";
        bee.updatedAt = ts;
        touchPhase(data, "pending");
        if (["idle", "planned", "pending"].includes(state.swarm.status)) state.swarm.status = "running";
        break;
      case "bee.start":
        bee = ensureBee(data, "running");
        bee.status = "running";
        bee.name = text(valueAt(data, ["bee_name", "beeName", "agent_name", "agentName", "name"], bee.name));
        bee.role = text(valueAt(data, ["role", "specialty"], bee.role));
        bee.task = text(valueAt(data, ["task", "objective", "goal", "assignment"], bee.task));
        bee.phaseId = phaseIdFrom(data) || bee.phaseId;
        bee.startedAt = bee.startedAt || ts;
        bee.updatedAt = ts;
        state.swarm.status = "running";
        state.swarm.startedAt = state.swarm.startedAt || ts;
        touchPhase(data, "running");
        break;
      case "bee.reasoning":
        bee = ensureBee(data, "reasoning");
        bee.status = "reasoning";
        bee.reasoning = appendStream(bee.reasoning, valueAt(data, ["reasoning", "text", "delta", "message"], ""));
        bee.updatedAt = ts;
        state.swarm.status = "running";
        touchPhase(data, "running");
        break;
      case "bee.delta":
        bee = ensureBee(data, "running");
        bee.status = "running";
        bee.output = appendStream(bee.output, valueAt(data, ["delta", "text", "output", "message"], ""));
        bee.updatedAt = ts;
        state.swarm.status = "running";
        touchPhase(data, "running");
        break;
      case "bee.tool_call":
        bee = ensureBee(data, "running");
        bee.status = "running";
        bee.activeTool = {
          name: text(valueAt(data, ["tool", "tool_name", "toolName", "name"], "工具")),
          callId: text(valueAt(data, ["tool_call_id", "toolCallId", "call_id", "callId"], "")),
          startedAt: ts,
        };
        bee.toolCalls += 1;
        bee.updatedAt = ts;
        state.usage.toolCalls += 1;
        break;
      case "bee.tool_result":
        bee = ensureBee(data, "running");
        bee.lastTool = {
          name: text(valueAt(data, ["tool", "tool_name", "toolName", "name"], bee.activeTool && bee.activeTool.name || "工具")),
          result: short(valueAt(data, ["result", "summary", "message", "output"], "已返回"), 200),
          ok: !data.denied && !data.error && valueAt(data, ["ok", "success"], true) !== false,
          ms: firstNumber(data, ["ms", "duration_ms", "durationMs"]),
          finishedAt: ts,
        };
        bee.activeTool = null;
        bee.updatedAt = ts;
        break;
      case "bee.check": {
        bee = ensureBee(data, "running");
        const check = normalizeCheck(Object.assign({}, data, { bee_id: bee.id, ts }), state.checks.length, "check");
        upsert(state.checks, check);
        bee.checkCount += 1;
        bee.updatedAt = ts;
        if (check.verdict === "error") bee.status = "blocked";
        break;
      }
      case "bee.correct": {
        bee = ensureBee(data, "correcting");
        const correction = normalizeCheck(Object.assign({}, data, { bee_id: bee.id, ts }), state.checks.length, "correction");
        correction.verdict = normalizeStatus(valueAt(data, ["status", "state", "verdict"], "correcting"), "correcting");
        upsert(state.checks, correction);
        bee.correctionCount += 1;
        bee.status = correction.verdict === "done" ? "running" : "correcting";
        bee.updatedAt = ts;
        break;
      }
      case "handoff.created": {
        const handoff = normalizeHandoff(sourceEntity(data, "handoff"), state.handoffs.length);
        handoff.status = "pending";
        handoff.createdAt = handoff.createdAt || ts;
        upsert(state.handoffs, handoff);
        break;
      }
      case "handoff.ack": {
        const raw = sourceEntity(data, "handoff");
        const id = text(valueAt(raw, ["capsule_id", "capsuleId", "message_id", "messageId", "handoff_id", "handoffId", "id"], ""));
        let handoff = id ? state.handoffs.find((entry) => entry.id === id) : null;
        if (!handoff) handoff = upsert(state.handoffs, normalizeHandoff(raw, state.handoffs.length));
        handoff.status = "acknowledged";
        handoff.ackAt = ts;
        break;
      }
      case "fact.conflicted": {
        const conflict = normalizeConflict(sourceEntity(data, "fact"), state.conflicts.length, "conflict");
        conflict.status = "conflict";
        conflict.updatedAt = ts;
        upsert(state.conflicts, conflict);
        break;
      }
      case "fact.invalidated": {
        const raw = sourceEntity(data, "fact");
        const id = text(valueAt(raw, ["fact_id", "factId", "id"], ""));
        let conflict = id ? state.conflicts.find((entry) => entry.id === id) : null;
        if (!conflict) conflict = upsert(state.conflicts, normalizeConflict(raw, state.conflicts.length, "invalidated"));
        conflict.status = "invalidated";
        conflict.detail = text(valueAt(raw, ["reason", "message", "detail"], conflict.detail));
        conflict.updatedAt = ts;
        break;
      }
      case "artifact.created": {
        const artifact = normalizeArtifact(sourceEntity(data, "artifact"), state.artifacts.length, "ready");
        artifact.status = normalizeStatus(valueAt(data, ["status", "state"], "ready"), "ready");
        artifact.createdAt = artifact.createdAt || ts;
        artifact.updatedAt = ts;
        upsert(state.artifacts, artifact);
        break;
      }
      case "artifact.stale": {
        const raw = sourceEntity(data, "artifact");
        const id = text(valueAt(raw, ["artifact_id", "artifactId", "id"], ""));
        const path = text(valueAt(raw, ["path", "url", "uri"], ""));
        let artifact = id ? state.artifacts.find((entry) => entry.id === id) : null;
        if (!artifact && path) artifact = state.artifacts.find((entry) => entry.path === path);
        if (!artifact) artifact = upsert(state.artifacts, normalizeArtifact(raw, state.artifacts.length, "stale"));
        artifact.status = "stale";
        artifact.summary = text(valueAt(raw, ["reason", "message", "summary"], artifact.summary));
        artifact.updatedAt = ts;
        break;
      }
      case "swarm.paused":
        state.swarm.status = "paused";
        state.swarm.message = text(valueAt(data, ["message", "reason", "summary"], "蜂群执行已暂停"));
        break;
      case "swarm.cancelled":
        state.swarm.status = "cancelled";
        state.swarm.message = text(valueAt(data, ["message", "reason", "summary"], "蜂群任务已取消"));
        state.swarm.finishedAt = ts;
        state.bees.forEach((entry) => {
          if (["queued", "running", "reasoning", "correcting", "waiting"].includes(entry.status)) entry.status = "cancelled";
        });
        break;
      case "swarm.done":
        state.swarm.status = "done";
        state.swarm.message = text(valueAt(data, ["message", "summary", "result"], "蜂群任务已完成"));
        state.swarm.finishedAt = ts;
        state.bees.forEach((entry) => {
          if (["queued", "running", "reasoning", "correcting"].includes(entry.status)) {
            entry.status = "done";
            entry.finishedAt = entry.finishedAt || ts;
          }
        });
        state.phases.forEach((entry) => {
          if (["pending", "running", "planned"].includes(entry.status)) entry.status = "done";
        });
        break;
      case "swarm.skipped":
        state.swarm.status = "done";
        state.swarm.message = text(valueAt(data, ["message", "reason", "summary"], "该任务已改用单 Agent"));
        state.swarm.finishedAt = ts;
        break;
      case "swarm.error":
        state.swarm.status = "error";
        state.swarm.error = text(valueAt(data, ["error", "message", "detail"], "蜂群执行异常"));
        state.swarm.finishedAt = ts;
        break;
      default:
        break;
    }

    updateUsageFromEvent(data);
    state.swarm.updatedAt = ts;
    recordEvent(type, event, data, ts);
    render();
    return getState();
  }

  function section(title, count, id) {
    const node = create("section", "swarm-section");
    node.setAttribute("aria-labelledby", id);
    const head = create("div", "swarm-section-head");
    const heading = create("h3", "swarm-section-title", title);
    heading.id = id;
    append(head, heading);
    if (count !== undefined && count !== null) append(head, create("span", "swarm-section-count", String(count)));
    append(node, head);
    return node;
  }

  function emptyRow(message) {
    return create("div", "swarm-empty-row", message);
  }

  function statusPill(status, label) {
    const normalized = normalizeStatus(status, "unknown");
    const node = create("span", "swarm-status swarm-status--" + statusClass(normalized));
    append(node, create("i", "swarm-status-dot"), create("span", "", label || statusLabel(normalized)));
    return node;
  }

  function metaItem(label, value, mono) {
    const node = create("span", "swarm-meta-item" + (mono ? " is-mono" : ""));
    append(node, create("span", "swarm-meta-label", label), create("b", "", value));
    return node;
  }

  function progressBar(value, label) {
    const normalized = Math.max(0, Math.min(100, Number(value) || 0));
    const wrap = create("div", "swarm-progress");
    wrap.setAttribute("role", "progressbar");
    wrap.setAttribute("aria-valuemin", "0");
    wrap.setAttribute("aria-valuemax", "100");
    wrap.setAttribute("aria-valuenow", String(Math.round(normalized)));
    if (label) wrap.setAttribute("aria-label", label);
    const fill = create("i", "swarm-progress-fill");
    fill.style.width = normalized + "%";
    append(wrap, fill);
    return wrap;
  }

  function findBeeName(id) {
    if (!id) return "";
    const bee = state.bees.find((entry) => entry.id === id);
    return bee ? bee.name : id;
  }

  function dispatchHostEvent(name, detail) {
    if (typeof document === "undefined" || typeof CustomEvent !== "function") return false;
    return document.dispatchEvent(new CustomEvent(name, {
      detail: clone(detail),
      bubbles: false,
      cancelable: true,
    }));
  }

  function actionButton(label, className, eventName, detailFactory) {
    const button = create("button", "swarm-action " + className, label);
    button.type = "button";
    button.addEventListener("click", () => {
      const detail = typeof detailFactory === "function" ? detailFactory() : detailFactory;
      dispatchHostEvent(eventName, detail || {});
    });
    return button;
  }

  function renderOverview() {
    const node = section("概览", null, "swarm-sec-overview");
    const command = create("div", "swarm-command is-" + statusClass(state.swarm.status));
    const top = create("div", "swarm-command-top");
    append(top, statusPill(state.swarm.status));

    const connection = create("span", "swarm-connection is-" + state.connection.status);
    append(connection, create("i", "swarm-connection-dot"), create("span", "", CONNECTION_LABELS[state.connection.status] || CONNECTION_LABELS.unknown));
    if (state.connection.detail) setOptionalTitle(connection, state.connection.detail);
    append(top, connection);

    const title = create("h2", "swarm-command-title", state.swarm.title || "蜂群任务");
    const objective = state.swarm.objective || state.swarm.summary;
    const desc = create("p", "swarm-command-desc", objective || "计划载入后，这里显示蜂群目标、进度与执行证据。");
    setOptionalTitle(desc, objective);
    append(command, top, title, desc);

    const noticeText = state.swarm.error || state.swarm.message;
    if (noticeText) {
      const notice = create("div", "swarm-notice is-" + statusClass(state.swarm.status));
      append(notice, create("i", "swarm-notice-mark"), create("span", "", short(noticeText, 240)));
      setOptionalTitle(notice, noticeText);
      append(command, notice);
    }

    const finishedPhases = state.phases.filter((phase) => phase.status === "done").length;
    const completedBees = state.bees.filter((bee) => bee.status === "done").length;
    const activeBees = state.bees.filter((bee) => ["running", "reasoning", "correcting"].includes(bee.status)).length;
    const totalUnits = state.phases.length || state.bees.length;
    const doneUnits = state.phases.length ? finishedPhases : completedBees;
    const completion = totalUnits ? doneUnits / totalUnits * 100 : state.swarm.status === "done" ? 100 : 0;

    const metrics = create("div", "swarm-overview-metrics");
    append(metrics,
      metaItem("阶段", state.phases.length ? finishedPhases + "/" + state.phases.length : "—", true),
      metaItem("蜂", state.bees.length ? activeBees + " 活跃 / " + state.bees.length : "—", true),
      metaItem("更新", state.swarm.updatedAt ? formatClock(state.swarm.updatedAt) : "—", true)
    );
    append(command, metrics, progressBar(completion, "蜂群任务完成度"));

    if (["planning", "planned", "pending"].includes(normalizeStatus(state.swarm.status, "idle")) && state.plan) {
      const actions = create("div", "swarm-plan-actions");
      append(actions,
        actionButton("确认蜂群", "is-primary", "muliao-swarm-confirm", () => ({ plan: clone(state.plan) })),
        actionButton("改为单蜂", "is-secondary", "muliao-swarm-single", () => ({ plan: clone(state.plan) })),
        actionButton("取消计划", "is-ghost", "muliao-swarm-cancel-plan", () => ({ plan: clone(state.plan) }))
      );
      append(command, actions);
    }

    append(node, command);
    return node;
  }

  function renderContracts() {
    const node = section("契约", state.contracts.length, "swarm-sec-contracts");
    if (!state.contracts.length) {
      append(node, emptyRow("尚未收到执行契约"));
      return node;
    }
    const list = create("div", "swarm-ledger-list");
    state.contracts.forEach((contract) => {
      const row = create("article", "swarm-ledger-row is-" + statusClass(contract.status));
      const head = create("div", "swarm-row-head");
      append(head, create("b", "swarm-row-title", contract.title), statusPill(contract.status));
      append(row, head);
      if (contract.description) append(row, setOptionalTitle(create("p", "swarm-row-desc", short(contract.description, 220)), contract.description));
      const meta = create("div", "swarm-chip-line");
      if (contract.owner) append(meta, create("span", "swarm-chip", "负责人 · " + contract.owner));
      if (contract.acceptance.length) append(meta, create("span", "swarm-chip", "验收 " + contract.acceptance.length + " 条"));
      if (contract.constraints.length) append(meta, create("span", "swarm-chip", "约束 " + contract.constraints.length + " 条"));
      if (meta.childNodes.length) append(row, meta);
      append(list, row);
    });
    append(node, list);
    return node;
  }

  function renderPhases() {
    const node = section("阶段", state.phases.length, "swarm-sec-phases");
    if (!state.phases.length) {
      append(node, emptyRow("计划尚未拆分阶段"));
      return node;
    }
    const timeline = create("div", "swarm-phase-list");
    state.phases.slice().sort((a, b) => a.order - b.order).forEach((phase, index) => {
      const row = create("article", "swarm-phase is-" + statusClass(phase.status));
      const rail = create("div", "swarm-phase-rail");
      append(rail, create("i", "swarm-phase-marker"));
      if (index < state.phases.length - 1) append(rail, create("i", "swarm-phase-line"));
      const body = create("div", "swarm-phase-body");
      const head = create("div", "swarm-row-head");
      append(head, create("b", "swarm-row-title", phase.title), statusPill(phase.status));
      append(body, head);
      if (phase.summary) append(body, setOptionalTitle(create("p", "swarm-row-desc", short(phase.summary, 180)), phase.summary));
      const bees = state.bees.filter((bee) => bee.phaseId === phase.id);
      const meta = create("div", "swarm-inline-meta");
      if (bees.length) append(meta, create("span", "", bees.length + " 只蜂"));
      if (phase.dependsOn.length) append(meta, create("span", "", "依赖 " + phase.dependsOn.length + " 项"));
      if (phase.startedAt) append(meta, create("span", "", formatClock(phase.startedAt)));
      if (meta.childNodes.length) append(body, meta);
      append(row, rail, body);
      append(timeline, row);
    });
    append(node, timeline);
    return node;
  }

  function beeSort(a, b) {
    const rank = { running: 0, reasoning: 0, correcting: 0, blocked: 1, waiting: 2, queued: 3, done: 4, cancelled: 5 };
    return (rank[a.status] ?? 6) - (rank[b.status] ?? 6) || a.order - b.order;
  }

  function renderBees() {
    const active = state.bees.filter((bee) => ["running", "reasoning", "correcting"].includes(bee.status)).length;
    const node = section("蜂列表", state.bees.length ? active + " / " + state.bees.length : 0, "swarm-sec-bees");
    if (!state.bees.length) {
      append(node, emptyRow("等待蜂群编队"));
      return node;
    }
    const list = create("div", "swarm-bee-list");
    state.bees.slice().sort(beeSort).forEach((bee) => {
      const card = create("article", "swarm-bee is-" + statusClass(bee.status));
      const head = create("div", "swarm-bee-head");
      const identity = create("div", "swarm-bee-identity");
      const live = create("i", "swarm-bee-signal" + (["running", "reasoning", "correcting"].includes(bee.status) ? " is-live" : ""));
      const names = create("div", "swarm-bee-names");
      append(names, create("b", "", bee.name), create("span", "", bee.role || "执行蜂"));
      append(identity, live, names);
      append(head, identity, statusPill(bee.status));
      append(card, head);

      if (bee.task) append(card, setOptionalTitle(create("p", "swarm-bee-task", short(bee.task, 210)), bee.task));

      if (bee.activeTool) {
        const tool = create("div", "swarm-tool-line is-active");
        append(tool, create("i", "swarm-tool-pulse"), create("span", "", "调用 " + bee.activeTool.name), create("time", "", formatClock(bee.activeTool.startedAt)));
        append(card, tool);
      } else if (bee.lastTool) {
        const tool = create("div", "swarm-tool-line");
        append(tool, create("i", "swarm-tool-check"), create("span", "", bee.lastTool.name + " · " + (bee.lastTool.ok ? "已返回" : "异常")));
        if (bee.lastTool.ms !== null) append(tool, create("time", "", formatDuration(bee.lastTool.ms)));
        setOptionalTitle(tool, bee.lastTool.result);
        append(card, tool);
      }

      const previewText = bee.output || bee.reasoning;
      if (previewText) {
        const preview = create("p", "swarm-stream-preview", short(previewText, 240));
        setOptionalTitle(preview, previewText);
        append(card, preview);
      }

      const foot = create("div", "swarm-bee-foot");
      if (bee.phaseId) append(foot, create("span", "", "阶段 · " + (state.phases.find((phase) => phase.id === bee.phaseId)?.title || bee.phaseId)));
      if (bee.checkCount) append(foot, create("span", "", "检查 " + bee.checkCount));
      if (bee.correctionCount) append(foot, create("span", "", "纠偏 " + bee.correctionCount));
      if (bee.toolCalls) append(foot, create("span", "", "工具 " + bee.toolCalls));
      if (foot.childNodes.length) append(card, foot);
      append(list, card);
    });
    append(node, list);
    return node;
  }

  function renderChecks() {
    const node = section("Jev 检查", state.checks.length, "swarm-sec-checks");
    if (!state.checks.length) {
      append(node, emptyRow("尚无检查或纠偏记录"));
      return node;
    }
    const timeline = create("div", "swarm-audit-list");
    state.checks.slice(-14).reverse().forEach((check) => {
      const row = create("article", "swarm-audit-row is-" + statusClass(check.verdict));
      const mark = create("i", "swarm-audit-mark");
      const body = create("div", "swarm-audit-body");
      const head = create("div", "swarm-row-head");
      const label = check.kind === "correction" ? "纠偏 · " + check.label : check.label;
      append(head, create("b", "swarm-row-title", label), create("time", "swarm-row-time", formatClock(check.ts)));
      append(body, head);
      const source = check.beeId ? findBeeName(check.beeId) : "Jev";
      const verdictLine = create("div", "swarm-check-verdict");
      append(verdictLine, statusPill(check.verdict), create("span", "", source));
      if (check.confidence !== null) append(verdictLine, create("span", "swarm-confidence", percent(check.confidence)));
      append(body, verdictLine);
      if (check.message) append(body, setOptionalTitle(create("p", "swarm-row-desc", short(check.message, 180)), check.message));
      append(row, mark, body);
      append(timeline, row);
    });
    append(node, timeline);
    return node;
  }

  function renderHandoffs() {
    const node = section("交接", state.handoffs.length, "swarm-sec-handoffs");
    if (!state.handoffs.length) {
      append(node, emptyRow("尚无蜂间交接"));
      return node;
    }
    const list = create("div", "swarm-compact-list");
    state.handoffs.slice().reverse().forEach((handoff) => {
      const row = create("article", "swarm-transfer is-" + statusClass(handoff.status));
      const route = create("div", "swarm-transfer-route");
      append(route,
        create("span", "swarm-transfer-party", findBeeName(handoff.from) || "来源蜂"),
        create("i", "swarm-transfer-arrow", "→"),
        create("span", "swarm-transfer-party", findBeeName(handoff.to) || "接收蜂"),
        statusPill(handoff.status)
      );
      append(row, route);
      if (handoff.summary) append(row, setOptionalTitle(create("p", "swarm-row-desc", short(handoff.summary, 180)), handoff.summary));
      if (handoff.artifact) append(row, create("div", "swarm-path", "产物 · " + handoff.artifact));
      append(list, row);
    });
    append(node, list);
    return node;
  }

  function renderConflicts() {
    const unresolved = state.conflicts.filter((item) => item.status === "conflict").length;
    const node = section("冲突", state.conflicts.length ? unresolved + " 未决" : 0, "swarm-sec-conflicts");
    if (!state.conflicts.length) {
      append(node, emptyRow("未发现事实冲突"));
      return node;
    }
    const list = create("div", "swarm-compact-list");
    state.conflicts.slice().reverse().forEach((conflict) => {
      const row = create("article", "swarm-conflict is-" + statusClass(conflict.status));
      const head = create("div", "swarm-row-head");
      append(head, create("b", "swarm-row-title", short(conflict.statement, 140)), statusPill(conflict.status));
      append(row, head);
      if (conflict.detail) append(row, setOptionalTitle(create("p", "swarm-row-desc", short(conflict.detail, 180)), conflict.detail));
      if (conflict.sources.length) append(row, create("div", "swarm-inline-meta", "证据源 " + conflict.sources.length + " 项"));
      append(list, row);
    });
    append(node, list);
    return node;
  }

  function renderArtifacts() {
    const stale = state.artifacts.filter((item) => item.status === "stale").length;
    const node = section("产物", state.artifacts.length ? state.artifacts.length + (stale ? " · " + stale + " 过期" : "") : 0, "swarm-sec-artifacts");
    if (!state.artifacts.length) {
      append(node, emptyRow("尚未生成产物"));
      return node;
    }
    const list = create("div", "swarm-artifact-list");
    state.artifacts.slice().reverse().forEach((artifact) => {
      const row = create("article", "swarm-artifact is-" + statusClass(artifact.status));
      const icon = create("span", "swarm-artifact-icon", artifact.type ? artifact.type.slice(0, 3).toUpperCase() : "OUT");
      const body = create("div", "swarm-artifact-body");
      const head = create("div", "swarm-row-head");
      append(head, create("b", "swarm-row-title", artifact.name), statusPill(artifact.status));
      append(body, head);
      if (artifact.summary) append(body, setOptionalTitle(create("p", "swarm-row-desc", short(artifact.summary, 160)), artifact.summary));
      if (artifact.path) append(body, setOptionalTitle(create("div", "swarm-path", short(artifact.path, 110)), artifact.path));
      append(row, icon, body);
      append(list, row);
    });
    append(node, list);
    return node;
  }

  function renderBudget() {
    const node = section("预算 / 用量", null, "swarm-sec-budget");
    const used = state.budget.used !== null ? state.budget.used : state.usage.totalTokens;
    const limit = state.budget.limit;
    const ratio = limit && used !== null ? used / limit * 100 : 0;
    const card = create("div", "swarm-budget-card");
    const numbers = create("div", "swarm-budget-numbers");
    append(numbers,
      metaItem("输入", formatCompact(state.usage.inputTokens), true),
      metaItem("输出", formatCompact(state.usage.outputTokens), true),
      metaItem("思考", formatCompact(state.usage.reasoningTokens), true)
    );
    append(card, numbers);

    const budgetHead = create("div", "swarm-budget-head");
    append(budgetHead,
      create("span", "", "总用量"),
      create("b", "", formatInteger(used) + (limit !== null ? " / " + formatInteger(limit) : " tok"))
    );
    append(card, budgetHead, progressBar(ratio, "蜂群预算使用率"));

    const foot = create("div", "swarm-budget-foot");
    append(foot,
      create("span", "", "工具 " + formatInteger(state.usage.toolCalls)),
      create("span", "", "耗时 " + formatDuration(state.usage.elapsedMs))
    );
    if (state.usage.cost !== null) append(foot, create("span", "", "费用 " + formatCost(state.usage.cost, state.budget.currency)));
    if (state.budget.costLimit !== null) append(foot, create("span", "", "上限 " + formatCost(state.budget.costLimit, state.budget.currency)));
    append(card, foot);
    append(node, card);
    return node;
  }

  function renderEvents() {
    const node = section("事件时间线", state.events.length, "swarm-sec-events");
    if (!state.events.length) {
      append(node, emptyRow("等待蜂群事件"));
      return node;
    }
    const list = create("ol", "swarm-event-list");
    state.events.slice(-60).reverse().forEach((event) => {
      const item = create("li", "swarm-event is-" + statusClass(event.status));
      const time = create("time", "swarm-event-time", formatClock(event.ts));
      if (event.ts) time.dateTime = new Date(event.ts).toISOString();
      const rail = create("span", "swarm-event-rail");
      append(rail, create("i", "swarm-event-marker"));
      const body = create("div", "swarm-event-body");
      append(body, create("b", "swarm-event-label", event.label));
      if (event.description) append(body, setOptionalTitle(create("p", "swarm-event-desc", event.description), event.description));
      append(item, time, rail, body);
      append(list, item);
    });
    append(node, list);
    return node;
  }

  function resolveDom() {
    if (typeof document === "undefined") return null;
    const pane = document.getElementById("pane-swarm");
    const content = document.getElementById("swarmPane") || pane;
    if (!content) return null;
    let root = content.querySelector("[data-muliao-swarm-root]");
    if (!root) {
      root = create("div", "muliao-swarm-root");
      root.setAttribute("data-muliao-swarm-root", "true");
      content.appendChild(root);
    }
    return {
      pane,
      content,
      root,
      summary: document.getElementById("swarmSummary"),
      cancel: document.getElementById("swarmCancel"),
      pause: document.getElementById("swarmPause"),
      tab: document.querySelector('[data-tab="swarm"]'),
    };
  }

  function updateHost(dom) {
    const swarmStatus = normalizeStatus(state.swarm.status, "idle");
    const active = ["planning", "planned", "pending", "queued", "running", "reasoning", "waiting", "paused", "correcting"].includes(swarmStatus);
    const activeBees = state.bees.filter((bee) => ["running", "reasoning", "correcting"].includes(bee.status)).length;
    const doneBees = state.bees.filter((bee) => bee.status === "done").length;

    if (dom.summary) {
      const parts = [statusLabel(swarmStatus)];
      if (state.bees.length) parts.push(doneBees + "/" + state.bees.length + " 完成");
      if (activeBees) parts.push(activeBees + " 活跃");
      dom.summary.textContent = parts.join(" · ");
      dom.summary.setAttribute("aria-live", "polite");
      dom.summary.setAttribute("data-status", statusClass(swarmStatus));
    }

    if (dom.cancel) {
      dom.cancel.disabled = !active || swarmStatus === "cancelled";
      dom.cancel.setAttribute("aria-disabled", String(dom.cancel.disabled));
      dom.cancel.setAttribute("data-status", statusClass(swarmStatus));
      if (!dom.cancel.getAttribute("title")) dom.cancel.setAttribute("title", "取消当前蜂群任务");
      if (dom.cancel.dataset.swarmBound !== "true") {
        dom.cancel.dataset.swarmBound = "true";
        dom.cancel.addEventListener("click", () => {
          dispatchHostEvent("muliao-swarm-cancel-run", {
            swarmId: state.swarm.id,
            status: state.swarm.status,
          });
        });
      }
    }

    if (dom.pause) {
      const pauseable = ["running", "reasoning", "correcting", "paused", "waiting"].includes(swarmStatus);
      dom.pause.disabled = !pauseable;
      dom.pause.setAttribute("aria-disabled", String(dom.pause.disabled));
      dom.pause.setAttribute("aria-pressed", String(swarmStatus === "paused"));
      dom.pause.setAttribute("data-status", statusClass(swarmStatus));
      if (!dom.pause.getAttribute("title")) dom.pause.setAttribute("title", "暂停或继续蜂群任务");
      if (dom.pause.dataset.swarmBound !== "true") {
        dom.pause.dataset.swarmBound = "true";
        dom.pause.addEventListener("click", () => {
          dispatchHostEvent("muliao-swarm-toggle-pause", {
            swarmId: state.swarm.id,
            paused: state.swarm.status === "paused",
            status: state.swarm.status,
          });
        });
      }
    }

    if (dom.tab) {
      dom.tab.setAttribute("data-swarm-status", statusClass(swarmStatus));
      dom.tab.setAttribute("data-swarm-active", String(active));
      const baseLabel = dom.tab.getAttribute("data-base-label") || dom.tab.getAttribute("aria-label") || dom.tab.getAttribute("title") || "蜂群任务";
      if (!dom.tab.getAttribute("data-base-label")) dom.tab.setAttribute("data-base-label", baseLabel);
      dom.tab.setAttribute("aria-label", baseLabel + " · " + statusLabel(swarmStatus));
    }

    if (dom.pane) {
      dom.pane.setAttribute("aria-busy", String(["planning", "running", "reasoning", "correcting"].includes(swarmStatus)));
      dom.pane.setAttribute("data-swarm-status", statusClass(swarmStatus));
    }
  }

  function render() {
    const dom = resolveDom();
    if (!dom) {
      if (!domReadyHooked && typeof document !== "undefined" && document.readyState === "loading") {
        domReadyHooked = true;
        document.addEventListener("DOMContentLoaded", () => render(), { once: true });
      }
      return;
    }

    const fragment = document.createDocumentFragment();
    append(fragment,
      renderOverview(),
      renderContracts(),
      renderPhases(),
      renderBees(),
      renderChecks(),
      renderHandoffs(),
      renderConflicts(),
      renderArtifacts(),
      renderBudget(),
      renderEvents()
    );
    dom.root.replaceChildren(fragment);
    updateHost(dom);
  }

  function normalizeConnection(value) {
    const source = isObject(value) ? value : { status: value };
    const raw = text(valueAt(source, ["status", "state"], "unknown")).toLowerCase();
    let status = "unknown";
    if (["connected", "online", "open", "ready", "ok"].includes(raw)) status = "connected";
    else if (["connecting", "reconnecting", "pending"].includes(raw)) status = "connecting";
    else if (["disconnected", "offline", "closed", "close"].includes(raw)) status = "disconnected";
    else if (["error", "failed", "failure"].includes(raw)) status = "error";
    return {
      status,
      detail: text(valueAt(source, ["detail", "message", "reason"], "")),
      updatedAt: Date.now(),
    };
  }

  function reset() {
    resetTask(false);
    render();
    return getState();
  }

  function renderPlan(plan) {
    applyPlan(plan, true);
    state.plan = clone(plan);
    const ts = Date.now();
    const data = isObject(plan) ? plan : { summary: plan };
    recordEvent("swarm.plan", { type: "swarm.plan" }, data, ts);
    state.swarm.updatedAt = ts;
    render();
    return getState();
  }

  function setConnection(status) {
    state.connection = normalizeConnection(status);
    render();
    return getState();
  }

  function getState() {
    return clone(state);
  }

  const api = Object.freeze({
    reset,
    renderPlan,
    handleEvent,
    setConnection,
    getState,
  });

  global.MuliaoSwarmUI = api;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      domReadyHooked = true;
      document.addEventListener("DOMContentLoaded", render, { once: true });
    } else {
      render();
    }
  }
})(window);
