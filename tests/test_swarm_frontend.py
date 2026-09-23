from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for Ghost frontend contract tests")
class SwarmFrontendContractTests(unittest.TestCase):
    def run_node(self, body: str) -> None:
        script = textwrap.dedent(
            f"""
            const assert = require("assert");
            const fs = require("fs");
            const vm = require("vm");
            const controllerSource = fs.readFileSync({json.dumps(str(ROOT / 'static' / 'swarm-controller.js'))}, "utf8");

            function makeElement(id) {{
              return {{
                id,
                value: "",
                disabled: false,
                style: {{}},
                children: [],
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                appendChild(value) {{ this.children.push(value); return value; }},
                focus() {{}},
              }};
            }}

            const elements = Object.fromEntries([
              "send", "input", "thread", "empty", "topTitle", "search", "swarmCancel"
            ].map((id) => [id, makeElement(id)]));
            elements.empty.style.display = "block";

            const uiEvents = [];
            const ui = {{
              reset() {{}},
              setConnection(value) {{ uiEvents.push(["connection", value]); }},
              renderPlan(value) {{ uiEvents.push(["plan", value]); }},
              handleEvent(value) {{ uiEvents.push(["event", value]); }},
            }};
            const listeners = {{}};
            const windowObject = {{
              MuliaoSwarmUI: ui,
              addEventListener(name, handler) {{ listeners[name] = handler; }},
            }};
            const documentObject = {{
              querySelector(selector) {{
                return selector === ".app" ? {{ classList: {{ remove() {{}} }} }} : null;
              }},
              querySelectorAll() {{ return []; }},
            }};

            const sessions = [
              {{ id: "session-a", title: "A", msgs: [], created: 1 }},
              {{ id: "session-b", title: "B", msgs: [], created: 2 }},
            ];
            let curId = "session-a";
            const busyStates = [];
            let fetchCalls = [];
            let fetchImpl = null;

            const context = vm.createContext({{
              console,
              TextDecoder,
              AbortController,
              Date,
              JSON,
              Promise,
              setTimeout,
              clearTimeout,
              window: windowObject,
              document: documentObject,
              sessions,
              $: (id) => elements[id],
              cur: () => sessions.find((session) => session.id === curId) || null,
              newSession: () => {{ throw new Error("unexpected newSession"); }},
              titleFrom: (text) => String(text).slice(0, 18),
              msgEl: (role, text) => ({{ role, text }}),
              saveSessions: () => {{}},
              renderSessions: () => {{}},
              updateCounts: () => {{}},
              scrollBottom: () => {{}},
              gotoTab: () => {{}},
              setBusy: (busy, text) => busyStates.push([busy, text]),
              autoGrow: () => {{}},
              sendTurn: async () => {{ throw new Error("unexpected single-agent fallback"); }},
              fetch: async (...args) => {{
                fetchCalls.push(args);
                return fetchImpl(...args);
              }},
            }});
            vm.runInContext(controllerSource, context, {{ filename: "swarm-controller.js" }});
            const swarm = windowObject.MuliaoSwarm;

            async function jsonResponse(payload, ok = true, status = 200) {{
              return {{ ok, status, json: async () => payload }};
            }}

            function sseResponse(events) {{
              const chunks = events.map((event) => Buffer.from(`data: ${{JSON.stringify(event)}}\\n\\n`, "utf8"));
              let index = 0;
              return {{
                ok: true,
                status: 200,
                body: {{
                  getReader() {{
                    return {{
                      async read() {{
                        if (index >= chunks.length) return {{ done: true, value: undefined }};
                        return {{ done: false, value: chunks[index++] }};
                      }},
                    }};
                  }},
                }},
              }};
            }}

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
            self.fail(f"Node contract failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}")

    def test_plan_and_result_stay_bound_to_origin_session(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-1" };
              let releasePlan;
              const delayedPlan = new Promise((resolve) => { releasePlan = resolve; });
              fetchImpl = async (url) => {
                if (url === "/api/swarm/plan") return delayedPlan;
                if (url === "/api/swarm/run") {
                  return sseResponse([{
                    event_id: "evt-done",
                    seq: 1,
                    run_id: "run-1",
                    task_id: "task-1",
                    ts: 1,
                    type: "swarm.done",
                    payload: { final_text: "final answer" },
                  }]);
                }
                throw new Error(`unexpected URL ${url}`);
              };

              const planningRequest = swarm.planGoal("origin goal");
              await new Promise((resolve) => setTimeout(resolve, 0));
              curId = "session-b";
              releasePlan(await jsonResponse({ plan }));
              await planningRequest;
              assert.equal(fetchCalls.length, 1);

              await swarm.planGoal("must remain in input");
              assert.equal(fetchCalls.length, 1, "pending plan must block another planner request");
              assert.equal(elements.input.value, "must remain in input");

              curId = "session-b";
              await swarm.runPending({ plan });

              assert.deepEqual(sessions[0].msgs.map((item) => [item.role, item.text]), [
                ["user", "origin goal"],
                ["assistant", "final answer"],
              ]);
              assert.deepEqual(sessions[1].msgs, []);
              assert.equal(elements.thread.children.length, 0, "background session must not write into visible thread");
              assert.equal(swarm.active, null);
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_cancelled_run_ignores_late_terminal_event(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-cancel" };
              let releaseRead;
              const delayedRead = new Promise((resolve) => { releaseRead = resolve; });
              fetchImpl = async (url) => {
                if (url === "/api/swarm/plan") return jsonResponse({ plan });
                if (url === "/api/swarm/run") {
                  let readCount = 0;
                  return {
                    ok: true,
                    status: 200,
                    body: { getReader() { return { async read() {
                      if (readCount++ === 0) return delayedRead;
                      return { done: true, value: undefined };
                    } }; } },
                  };
                }
                if (url === "/api/swarm/run-cancel/cancel") return jsonResponse({ ok: true, cancelled: true });
                throw new Error(`unexpected URL ${url}`);
              };

              await swarm.planGoal("cancel goal");
              const running = swarm.runPending({ plan });
              await new Promise((resolve) => setTimeout(resolve, 0));
              await swarm.cancelActive();
              releaseRead({
                done: false,
                value: Buffer.from(`data: ${JSON.stringify({
                  event_id: "evt-late",
                  seq: 1,
                  run_id: "run-cancel",
                  task_id: "task-cancel",
                  ts: 1,
                  type: "swarm.done",
                  payload: { final_text: "must not appear" },
                })}\\n\\n`, "utf8"),
              });
              await running;

              assert.deepEqual(sessions[0].msgs.map((item) => [item.role, item.text]), [
                ["user", "cancel goal"],
              ]);
              assert.equal(swarm.active, null);
              assert.equal(
                uiEvents.filter(([kind, event]) => kind === "event" && event.type === "swarm.done").length,
                0,
                "late completion must be discarded after cancellation",
              );
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )


if __name__ == "__main__":
    unittest.main()
