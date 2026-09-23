from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for swarm UI tests")
class SwarmUiVmTests(unittest.TestCase):
    def run_node(self, body: str) -> None:
        script = textwrap.dedent(
            f"""
            const assert = require("assert");
            const fs = require("fs");
            const vm = require("vm");
            const source = fs.readFileSync({json.dumps(str(ROOT / 'static' / 'swarm-ui.js'))}, "utf8");

            class Node {{
              constructor(tag = "") {{
                this.tagName = tag.toUpperCase();
                this.childNodes = [];
                this.parentNode = null;
                this.attributes = {{}};
                this.dataset = {{}};
                this.style = {{}};
                this.className = "";
                this.id = "";
                this.textContent = "";
                this.hidden = false;
                this.disabled = false;
                this.listeners = {{}};
              }}
              appendChild(child) {{
                if (child && child.isFragment) {{
                  for (const item of [...child.childNodes]) this.appendChild(item);
                  return child;
                }}
                this.childNodes.push(child);
                if (child) child.parentNode = this;
                return child;
              }}
              replaceChildren(...children) {{
                this.childNodes = [];
                for (const child of children) this.appendChild(child);
              }}
              setAttribute(name, value) {{
                this.attributes[name] = String(value);
                if (name === "id") this.id = String(value);
                if (name.startsWith("data-")) {{
                  const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
                  this.dataset[key] = String(value);
                }}
              }}
              getAttribute(name) {{ return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }}
              addEventListener(name, handler) {{ (this.listeners[name] ||= []).push(handler); }}
              dispatchEvent(event) {{ for (const handler of this.listeners[event.type] || []) handler(event); return true; }}
              querySelector(selector) {{ return find(this, selector); }}
            }}

            function matches(node, selector) {{
              if (!node) return false;
              if (selector.startsWith("#")) return node.id === selector.slice(1);
              if (selector.startsWith(".")) return String(node.className).split(/\\s+/).includes(selector.slice(1));
              const data = selector.match(/^\\[data-([^=\\]]+)(?:=\"([^\"]+)\")?\\]$/);
              if (data) {{
                const name = `data-${{data[1]}}`;
                return node.getAttribute(name) !== null && (data[2] === undefined || node.getAttribute(name) === data[2]);
              }}
              return node.tagName === selector.toUpperCase();
            }}

            function find(root, selector) {{
              for (const child of root.childNodes || []) {{
                if (matches(child, selector)) return child;
                const nested = find(child, selector);
                if (nested) return nested;
              }}
              return null;
            }}

            function all(root, predicate, values = []) {{
              if (predicate(root)) values.push(root);
              for (const child of root.childNodes || []) all(child, predicate, values);
              return values;
            }}

            function visibleText(root) {{
              return [root.textContent, ...(root.childNodes || []).map(visibleText)].filter(Boolean).join(" ");
            }}

            const document = new Node("document");
            document.readyState = "complete";
            document.createElement = (tag) => new Node(tag);
            document.createDocumentFragment = () => Object.assign(new Node("fragment"), {{ isFragment: true }});
            document.getElementById = (id) => find(document, `#${{id}}`);
            document.querySelector = (selector) => find(document, selector);
            document.dispatchEvent = () => true;

            function host(tag, id, parent = document) {{
              const node = new Node(tag);
              node.setAttribute("id", id);
              parent.appendChild(node);
              return node;
            }}

            const tab = host("button", "tab-swarm");
            tab.setAttribute("data-tab", "swarm");
            tab.setAttribute("title", "蜂群任务");
            const badge = host("span", "swarmBadge", tab);
            const pane = host("aside", "pane-swarm");
            const content = host("div", "swarmPane", pane);
            const summary = host("span", "swarmSummary", pane);
            const seq = host("span", "swarmSeq", pane);
            const pause = host("button", "swarmPause", pane);
            const cancel = host("button", "swarmCancel", pane);

            const windowListeners = {{}};
            const windowObject = {{
              addEventListener(name, handler) {{ (windowListeners[name] ||= []).push(handler); }},
              dispatchEvent(event) {{
                for (const handler of windowListeners[event.type] || []) handler(event);
                return true;
              }},
            }};
            const context = vm.createContext({{
              console,
              window: windowObject,
              document,
              CustomEvent: class CustomEvent {{ constructor(type, options = {{}}) {{ this.type = type; this.detail = options.detail; }} }},
              Intl,
              Date,
              JSON,
              Number,
              String,
              Array,
              Object,
              Math,
              structuredClone: global.structuredClone,
            }});
            vm.runInContext(source, context, {{ filename: "swarm-ui.js" }});
            const ui = windowObject.MuliaoSwarmUI;

            {textwrap.dedent(body)}
            """
        )
        completed = subprocess.run(
            [NODE, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if completed.returncode != 0:
            self.fail(f"Node VM test failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}")

    def test_event_consumption_terminal_guard_and_host_state(self) -> None:
        self.run_node(
            r"""
            assert.equal(seq.textContent, "seq —");
            assert.equal(badge.textContent, "");
            assert.equal(badge.hidden, true);
            assert.equal(pause.disabled, true);
            assert.match(pause.getAttribute("title"), /仅支持取消/);

            ui.handleEvent({ type: "swarm.confirmed", event_id: "evt-confirmed", run_id: "run-1", seq: 1, ts: 1, payload: { confirmation_reasons: ["high_risk"] } });
            let state = ui.getState();
            assert.equal(state.swarm.status, "running");
            assert.equal(state.events.at(-1).label, "执行已确认");
            assert.match(state.events.at(-1).description, /high_risk/);
            assert.equal(seq.textContent, "seq 1");
            assert.equal(badge.textContent, "运行");
            assert.equal(pause.disabled, true);

            ui.handleEvent({ type: "swarm.notice", event_id: "evt-notice", run_id: "run-1", seq: 2, ts: 2, payload: { message: "暂停未实现；仅支持取消。" } });
            state = ui.getState();
            assert.equal(state.swarm.status, "running");
            assert.equal(state.swarm.message, "暂停未实现；仅支持取消。");
            assert.equal(state.events.at(-1).label, "蜂群提示");
            assert.equal(seq.textContent, "seq 2");

            ui.handleEvent({ type: "future.unrecognised", event_id: "evt-future", run_id: "run-1", seq: 3, ts: 3, payload: { message: "future-safe" } });
            state = ui.getState();
            assert.equal(state.swarm.status, "running");
            assert.equal(state.events.at(-1).type, "future.unrecognised");
            assert.equal(seq.textContent, "seq 3");

            ui.handleEvent({ type: "swarm.done", event_id: "evt-done", run_id: "run-1", seq: 4, ts: 4, payload: { summary: "完成" } });
            state = ui.getState();
            assert.equal(state.swarm.status, "done");
            assert.equal(state.swarm.terminalType, "swarm.done");
            assert.equal(badge.textContent, "完成");

            ui.handleEvent({ type: "bee.delta", event_id: "evt-late", run_id: "run-1", seq: 5, ts: 5, payload: { bee_id: "late", text: "迟到输出" } });
            state = ui.getState();
            assert.equal(state.swarm.status, "done", "late non-terminal event must not reopen the run");
            assert.equal(state.swarm.terminalType, "swarm.done");
            assert.equal(state.bees.some((bee) => bee.id === "late"), false, "late event must not mutate task entities");
            assert.equal(seq.textContent, "seq 5");
            assert.equal(badge.textContent, "完成");

            ui.handleEvent({ type: "swarm.error", event_id: "evt-late-terminal", run_id: "run-1", seq: 6, ts: 6, payload: { error: "late terminal" } });
            state = ui.getState();
            assert.equal(state.swarm.status, "done", "first terminal event must remain authoritative");
            assert.equal(state.swarm.terminalType, "swarm.done");
            assert.equal(seq.textContent, "seq 6");
            assert.equal(badge.textContent, "完成");

            ui.reset();
            state = ui.getState();
            assert.equal(state.swarm.status, "idle");
            assert.equal(state.swarm.lastSeq, null);
            assert.equal(seq.textContent, "seq —");
            assert.equal(badge.textContent, "");
            assert.equal(badge.hidden, true);
            assert.equal(pause.disabled, true);
            """
        )

    def test_handoff_ack_and_sparse_usage_rendering(self) -> None:
        self.run_node(
            r"""
            let rendered = visibleText(content);
            assert.match(rendered, /尚无预算或用量数据/);
            assert.doesNotMatch(rendered, /输入 0/);

            ui.handleEvent({
              type: "handoff.ack",
              seq: 7,
              ts: 7,
              payload: {
                from: "compiler",
                to: "builder",
                capsule_id: "cap-1",
                ack: "need_context",
                acknowledgement: {
                  message_id: "msg-1",
                  capsule_id: "cap-1",
                  status: "need_context",
                  reasons: ["source unavailable"],
                  missing: ["source_ref:fact-1"],
                  duplicate: true,
                  checked_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                  checked_at: "2026-09-23T00:00:00Z",
                },
              },
            });
            const state = ui.getState();
            const handoff = state.handoffs[0];
            assert.equal(handoff.status, "blocked");
            assert.equal(handoff.ackStatus, "need_context");
            assert.deepEqual(Array.from(handoff.reasons), ["source unavailable"]);
            assert.deepEqual(Array.from(handoff.missing), ["source_ref:fact-1"]);
            assert.equal(handoff.duplicate, true);
            assert.match(handoff.checkedSha256, /^abcdef/);

            rendered = visibleText(content);
            assert.match(rendered, /ACK · need_context/);
            assert.match(rendered, /原因 1 项/);
            assert.match(rendered, /缺失 1 项/);
            assert.match(rendered, /重复回执/);
            assert.match(rendered, /SHA256/);
            assert.equal(state.events.at(-1).status, "blocked", "need_context must not render as success");
            assert.match(state.events.at(-1).description, /need_context/);
            assert.match(state.events.at(-1).description, /source unavailable/);
            assert.match(state.events.at(-1).description, /source_ref:fact-1/);
            assert.match(state.events.at(-1).description, /重复/);
            assert.match(state.events.at(-1).description, /abcdef/);

            ui.handleEvent({ type: "bee.delta", seq: 8, payload: { bee_id: "builder", text: "ok", usage: { input_tokens: 12, output_tokens: 3 } } });
            rendered = visibleText(content);
            assert.match(rendered, /输入 12/);
            assert.match(rendered, /输出 3/);
            assert.match(rendered, /总用量 15 tok/);
            """
        )

    def test_confirmed_plan_dedup_order_run_filter_and_cancel_ownership(self) -> None:
        self.run_node(
            r"""
            const plan = {
              run_id: "run-guard",
              status: "requires_confirmation",
              title: "高风险计划",
              swarm_worthy: true,
            };
            ui.renderPlan(plan);
            let state = ui.getState();
            assert.equal(state.swarm.status, "waiting");
            assert.match(visibleText(content), /确认蜂群/);
            assert.match(visibleText(content), /改为单蜂/);
            assert.match(visibleText(content), /取消计划/);

            cancel.disabled = false;
            ui.markConfirmed("run-guard");
            state = ui.getState();
            assert.equal(state.confirmed, true);
            assert.equal(state.swarm.status, "running");
            assert.doesNotMatch(visibleText(content), /确认蜂群/);
            assert.equal(cancel.disabled, false, "UI must not write controller-owned disabled state");

            cancel.disabled = true;
            ui.handleEvent({
              type: "swarm.plan",
              event_id: "evt-plan-after-confirm",
              run_id: "run-guard",
              seq: 1,
              payload: { plan: { ...plan, status: "requires_confirmation" } },
            });
            state = ui.getState();
            assert.equal(state.swarm.status, "running", "confirmed plan must not regress to planned/waiting");
            assert.doesNotMatch(visibleText(content), /确认蜂群/);
            assert.equal(cancel.disabled, true, "SSE render during cancel requesting must not re-enable button");

            ui.handleEvent({
              type: "bee.delta",
              event_id: "evt-gap",
              run_id: "run-guard",
              seq: 3,
              payload: { bee_id: "builder", text: "first" },
            });
            state = ui.getState();
            assert.equal(state.swarm.lastSeq, 3);
            assert.deepEqual(Array.from(state.stream.gaps, (gap) => [gap.from, gap.to]), [[2, 2]]);
            assert.match(seq.textContent, /缺 2/);

            ui.handleEvent({
              type: "bee.delta",
              event_id: "evt-gap",
              run_id: "run-guard",
              seq: 4,
              payload: { bee_id: "builder", text: "duplicate-event-id" },
            });
            ui.handleEvent({
              type: "bee.delta",
              event_id: "evt-old-seq",
              run_id: "run-guard",
              seq: 2,
              payload: { bee_id: "builder", text: "old-seq" },
            });
            ui.handleEvent({
              type: "bee.delta",
              event_id: "evt-wrong-run",
              run_id: "run-other",
              seq: 5,
              payload: { bee_id: "builder", text: "wrong-run" },
            });
            state = ui.getState();
            assert.equal(state.swarm.lastSeq, 3);
            assert.equal(state.bees.find((bee) => bee.id === "builder").output, "first");

            ui.handleEvent({
              type: "swarm.done",
              event_id: "evt-terminal",
              run_id: "run-guard",
              seq: 6,
              payload: { summary: "done" },
            });
            ui.handleEvent({
              type: "bee.delta",
              event_id: "evt-post-terminal-old",
              run_id: "run-guard",
              seq: 5,
              payload: { bee_id: "builder", text: "must-ignore" },
            });
            state = ui.getState();
            assert.equal(state.swarm.status, "done");
            assert.equal(state.swarm.lastSeq, 6, "late event after terminal must not move sequence backward");
            assert.equal(state.bees.find((bee) => bee.id === "builder").output, "first");
            """
        )

    def test_rendered_actions_dispatch_to_window_controller_listeners(self) -> None:
        self.run_node(
            r"""
            const received = [];
            for (const name of [
              "muliao-swarm-confirm",
              "muliao-swarm-single",
              "muliao-swarm-cancel-plan",
              "muliao-swarm-cancel-run",
            ]) {
              windowObject.addEventListener(name, (event) => received.push([name, event.detail]));
            }

            const plan = {
              run_id: "run-actions",
              status: "requires_confirmation",
              swarm_worthy: true,
            };
            ui.renderPlan(plan);
            const buttons = all(content, (node) => node.tagName === "BUTTON");
            const byText = (label) => buttons.find((button) => button.textContent === label);
            byText("确认蜂群").dispatchEvent({ type: "click" });
            byText("改为单蜂").dispatchEvent({ type: "click" });
            byText("取消计划").dispatchEvent({ type: "click" });

            cancel.disabled = false;
            cancel.dispatchEvent({ type: "click" });

            assert.deepEqual(received.map(([name]) => name), [
              "muliao-swarm-confirm",
              "muliao-swarm-single",
              "muliao-swarm-cancel-plan",
              "muliao-swarm-cancel-run",
            ]);
            assert.equal(received[0][1].plan.run_id, "run-actions");
            assert.equal(received[1][1].plan.run_id, "run-actions");
            assert.equal(received[2][1].plan.run_id, "run-actions");
            assert.equal(received[3][1].swarmId, "run-actions");
            """
        )

    def test_forbidden_handoff_ack_is_not_success(self) -> None:
        self.run_node(
            r"""
            ui.handleEvent({
              type: "handoff.ack",
              event_id: "evt-forbidden",
              run_id: "run-forbidden",
              seq: 1,
              payload: {
                from: "compiler",
                to: "builder",
                capsule_id: "cap-forbidden",
                ack_status: "forbidden",
                acknowledgement: {
                  message_id: "msg-forbidden",
                  capsule_id: "cap-forbidden",
                  status: "forbidden",
                  reasons: ["permission snapshot mismatch"],
                  missing: [],
                  checked_sha256: "deadbeef",
                  duplicate: false,
                },
              },
            });
            const state = ui.getState();
            assert.equal(state.handoffs[0].status, "error");
            assert.equal(state.handoffs[0].ackStatus, "forbidden");
            assert.equal(state.events.at(-1).status, "error");
            assert.match(state.events.at(-1).description, /forbidden/);
            """
        )

    def test_badge_waiting_and_failure_states(self) -> None:
        self.run_node(
            r"""
            ui.handleEvent({ type: "swarm.waiting_user", event_id: "evt-wait", run_id: "run-status", seq: 1, payload: { question: "确认？" } });
            assert.equal(badge.textContent, "等待");
            ui.handleEvent({ type: "swarm.error", event_id: "evt-error", run_id: "run-status", seq: 2, payload: { error: "boom" } });
            assert.equal(badge.textContent, "失败");
            assert.equal(ui.getState().swarm.status, "error");
            """
        )


if __name__ == "__main__":
    unittest.main()
