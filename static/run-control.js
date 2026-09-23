/* 幕僚主运行按钮控制器
   空闲时发送；运行时显示暂停图标，点击后安全中止当前操作。 */
(() => {
  "use strict";

  let current = null;
  let serial = 0;

  function button() { return document.getElementById("send"); }

  function render() {
    const el = button();
    if (!el) return;
    const running = !!current;
    const stopping = !!current?.stopping;
    el.dataset.mode = running ? (stopping ? "stopping" : "stop") : "send";
    el.disabled = stopping;
    el.title = running ? (stopping ? "正在暂停…" : "暂停当前运行") : "发送";
    el.setAttribute("aria-label", el.title);
    el.setAttribute("aria-pressed", running ? "true" : "false");
    const use = el.querySelector?.("use");
    use?.setAttribute("href", running ? "#i-pause" : "#i-up");
  }

  function begin(kind, onStop) {
    if (current) return null;
    const operation = {
      id: ++serial,
      kind: String(kind || "running"),
      onStop: typeof onStop === "function" ? onStop : null,
      stopping: false,
    };
    current = operation;
    render();
    return operation;
  }

  function update(operation, { kind, onStop } = {}) {
    if (current !== operation || operation.stopping) return false;
    if (kind) operation.kind = String(kind);
    if (typeof onStop === "function") operation.onStop = onStop;
    render();
    return true;
  }

  function finish(operation) {
    if (current !== operation) return false;
    current = null;
    render();
    return true;
  }

  async function stop() {
    const operation = current;
    if (!operation || operation.stopping) return false;
    operation.stopping = true;
    render();
    try {
      const accepted = operation.onStop ? await operation.onStop(operation) : false;
      if (current === operation && accepted === false) {
        operation.stopping = false;
        render();
      }
      return accepted !== false;
    } catch (error) {
      if (current === operation) {
        operation.stopping = false;
        render();
      }
      throw error;
    }
  }

  window.MuliaoRunControl = {
    begin,
    update,
    finish,
    stop,
    isActive: (operation) => current === operation,
    get current() { return current; },
    get mode() { return current ? (current.stopping ? "stopping" : "stop") : "send"; },
  };

  render();
})();
