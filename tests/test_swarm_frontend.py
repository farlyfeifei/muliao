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
              markConfirmed(value) {{ uiEvents.push(["confirmed", value]); }},
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
            let nextSessionId = 0;
            const busyStates = [];
            const singleTurns = [];
            let sendTurnImpl = async (text) => {{ singleTurns.push([curId, text]); }};
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
              newSession: () => {{
                const session = {{ id: `session-new-${{++nextSessionId}}`, title: "新会话", msgs: [], created: Date.now() }};
                sessions.unshift(session);
                curId = session.id;
              }},
              titleFrom: (text) => String(text).slice(0, 18),
              msgEl: (role, text) => ({{ role, text }}),
              saveSessions: () => {{}},
              renderSessions: () => {{}},
              updateCounts: () => {{}},
              scrollBottom: () => {{}},
              gotoTab: () => {{}},
              setBusy: (busy, text) => busyStates.push([busy, text]),
              autoGrow: () => {{}},
              sendTurn: async (text) => sendTurnImpl(text),
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
                if (url === "/api/swarm/run-cancel/cancel") return jsonResponse({ ok: true, cancel_requested: true });
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
    def test_single_agent_fallback_waits_for_origin_session(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "single", swarm_worthy: false, run_id: "run-single" };
              let releasePlan;
              const delayedPlan = new Promise((resolve) => { releasePlan = resolve; });
              fetchImpl = async (url) => {
                if (url === "/api/swarm/plan") return delayedPlan;
                throw new Error(`unexpected URL ${url}`);
              };

              const planningRequest = swarm.planGoal("single goal");
              await new Promise((resolve) => setTimeout(resolve, 0));
              curId = "session-b";
              releasePlan(await jsonResponse({ plan }));
              await planningRequest;

              assert.deepEqual(singleTurns, [], "fallback must not run in the wrong session");
              assert.equal(elements.send.disabled, true);
              assert.ok(
                uiEvents.some(([kind, event]) => kind === "event" && event.type === "swarm.notice"),
                "cross-session fallback should show a notice",
              );

              curId = "session-a";
              await listeners["muliao-swarm-single"]({ detail: {} });
              assert.deepEqual(singleTurns, [["session-a", "single goal"]]);
              assert.equal(elements.send.disabled, false);
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_cancel_rejection_keeps_run_alive_until_done(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-reject" };
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
                if (url === "/api/swarm/run-reject/cancel") {
                  return jsonResponse({ ok: true, cancel_requested: false });
                }
                throw new Error(`unexpected URL ${url}`);
              };

              await swarm.planGoal("keep running");
              const running = swarm.runPending({ plan });
              await new Promise((resolve) => setTimeout(resolve, 0));
              const originalRun = swarm.active;
              await swarm.cancelActive();
              assert.strictEqual(swarm.active, originalRun);
              assert.equal(originalRun.controller.signal.aborted, false);
              assert.ok(
                uiEvents.some(([kind, event]) => kind === "event" && event.type === "swarm.notice"),
              );
              assert.equal(
                uiEvents.filter(([kind, event]) => kind === "event" && event.type === "swarm.error").length,
                0,
              );

              releaseRead({
                done: false,
                value: Buffer.from(`data: ${JSON.stringify({
                  event_id: "evt-done-reject",
                  seq: 1,
                  run_id: "run-reject",
                  task_id: "task-reject",
                  ts: 1,
                  type: "swarm.done",
                  payload: { final_text: "completed after rejection" },
                })}\\n\\n`, "utf8"),
              });
              await running;
              assert.deepEqual(sessions[0].msgs.map((item) => [item.role, item.text]), [
                ["user", "keep running"],
                ["assistant", "completed after rejection"],
              ]);
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_empty_session_is_created_before_planning_and_single_fallback_stays_locked(self) -> None:
        self.run_node(
            """
            (async () => {
              sessions.splice(0, sessions.length);
              curId = null;
              const plan = { recipe: "single", swarm_worthy: false, run_id: "run-new-single" };
              let releaseSingle;
              const delayedSingle = new Promise((resolve) => { releaseSingle = resolve; });
              sendTurnImpl = async (text) => {
                singleTurns.push([curId, text]);
                await delayedSingle;
              };
              fetchImpl = async (url, options) => {
                assert.equal(url, "/api/swarm/plan");
                assert.equal(JSON.parse(options.body).session_id, "session-new-1");
                return jsonResponse({ plan });
              };

              const fallback = swarm.planGoal("empty session goal");
              await new Promise((resolve) => setTimeout(resolve, 0));
              assert.equal(singleTurns.length, 1);
              assert.deepEqual(singleTurns[0], ["session-new-1", "empty session goal"]);
              assert.equal(elements.send.disabled, true, "fallback must own lifecycle while sendTurn is pending");

              await swarm.planGoal("must wait");
              assert.equal(fetchCalls.length, 1, "fallback lock must block concurrent planning");
              assert.equal(elements.input.value, "must wait");

              releaseSingle();
              await fallback;
              assert.equal(elements.send.disabled, false);
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_deleted_origin_confirm_clears_pending_and_unlocks_send(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-deleted" };
              fetchImpl = async (url) => {
                if (url === "/api/swarm/plan") return jsonResponse({ plan });
                throw new Error(`unexpected URL ${url}`);
              };

              await swarm.planGoal("deleted origin goal");
              sessions.splice(sessions.findIndex((session) => session.id === "session-a"), 1);
              curId = "session-b";
              await swarm.runPending({ plan });

              assert.equal(elements.send.disabled, false);
              assert.equal(swarm.active, null);
              assert.ok(
                uiEvents.some(([kind, event]) => kind === "event" && event.type === "swarm.notice" && /已删除/.test(event.payload.message)),
                "deleted origin should emit cleanup notice",
              );

              await swarm.planGoal("new goal after cleanup");
              assert.equal(fetchCalls.length, 2, "stale pending must not permanently block planning");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_controller_owns_cancel_disabled_during_requesting(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-cancel-owner" };
              let releaseRead;
              const delayedRead = new Promise((resolve) => { releaseRead = resolve; });
              let releaseCancel;
              const delayedCancel = new Promise((resolve) => { releaseCancel = resolve; });
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
                if (url === "/api/swarm/run-cancel-owner/cancel") return delayedCancel;
                throw new Error(`unexpected URL ${url}`);
              };

              await swarm.planGoal("cancel ownership");
              const running = swarm.runPending({ plan });
              await new Promise((resolve) => setTimeout(resolve, 0));
              assert.equal(elements.swarmCancel.disabled, false);
              const cancelling = swarm.cancelActive();
              await new Promise((resolve) => setTimeout(resolve, 0));
              assert.equal(elements.swarmCancel.disabled, true, "requesting cancel must remain disabled");

              ui.handleEvent({ type: "bee.delta", run_id: "run-cancel-owner", seq: 1, payload: { text: "late render" } });
              assert.equal(elements.swarmCancel.disabled, true, "UI rendering must not seize disabled ownership");

              releaseCancel(await jsonResponse({ ok: true, cancel_requested: false }));
              await cancelling;
              assert.equal(elements.swarmCancel.disabled, false, "rejected cancel should re-enable controller action");

              releaseRead({
                done: false,
                value: Buffer.from(`data: ${JSON.stringify({
                  event_id: "evt-cancel-owner-done",
                  seq: 1,
                  run_id: "run-cancel-owner",
                  type: "swarm.done",
                  payload: { final_text: "done" },
                })}\\n\\n`, "utf8"),
              });
              await running;
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_stream_duplicate_out_of_order_and_wrong_run_are_ignored(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-envelope" };
              fetchImpl = async (url) => {
                if (url === "/api/swarm/plan") return jsonResponse({ plan });
                if (url === "/api/swarm/run") return sseResponse([
                  { event_id: "evt-one", seq: 1, run_id: "run-envelope", type: "bee.delta", payload: { bee_id: "b", text: "one" } },
                  { event_id: "evt-one", seq: 2, run_id: "run-envelope", type: "bee.delta", payload: { bee_id: "b", text: "duplicate id" } },
                  { event_id: "evt-old", seq: 1, run_id: "run-envelope", type: "bee.delta", payload: { bee_id: "b", text: "old seq" } },
                  { event_id: "evt-wrong", seq: 3, run_id: "other-run", type: "swarm.done", payload: { final_text: "wrong" } },
                  { event_id: "evt-done", seq: 4, run_id: "run-envelope", type: "swarm.done", payload: { final_text: "right" } },
                  { event_id: "evt-late", seq: 5, run_id: "run-envelope", type: "bee.delta", payload: { bee_id: "b", text: "late" } },
                ]);
                throw new Error(`unexpected URL ${url}`);
              };

              await swarm.planGoal("envelope filters");
              await swarm.runPending({ plan });
              const forwarded = uiEvents.filter(([kind]) => kind === "event").map(([, event]) => event.event_id);
              assert.deepEqual(forwarded, ["evt-one", "evt-done"]);
              assert.deepEqual(sessions[0].msgs.map((item) => item.text), ["envelope filters", "right"]);
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_wrong_run_terminal_event_is_ignored(self) -> None:
        self.run_node(
            """
            (async () => {
              const plan = { recipe: "research", swarm_worthy: true, run_id: "run-right" };
              fetchImpl = async (url) => {
                if (url === "/api/swarm/plan") return jsonResponse({ plan });
                if (url === "/api/swarm/run") {
                  return sseResponse([
                    {
                      event_id: "evt-wrong",
                      seq: 1,
                      run_id: "run-wrong",
                      task_id: "task-wrong",
                      ts: 1,
                      type: "swarm.done",
                      payload: { final_text: "wrong answer" },
                    },
                    {
                      event_id: "evt-right",
                      seq: 2,
                      run_id: "run-right",
                      task_id: "task-right",
                      ts: 2,
                      type: "swarm.done",
                      payload: { final_text: "right answer" },
                    },
                  ]);
                }
                throw new Error(`unexpected URL ${url}`);
              };

              await swarm.planGoal("right run only");
              await swarm.runPending({ plan });
              assert.deepEqual(sessions[0].msgs.map((item) => [item.role, item.text]), [
                ["user", "right run only"],
                ["assistant", "right answer"],
              ]);
              assert.equal(
                uiEvents.filter(([kind, event]) => kind === "event" && event.run_id === "run-wrong").length,
                0,
              );
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )


if __name__ == "__main__":
    unittest.main()
