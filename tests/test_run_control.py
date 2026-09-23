from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for run-control tests")
class RunControlTests(unittest.TestCase):
    def run_node(self, body: str) -> None:
        script = textwrap.dedent(
            f"""
            const assert = require("assert");
            const fs = require("fs");
            const vm = require("vm");
            const source = fs.readFileSync({json.dumps(str(ROOT / 'static' / 'run-control.js'))}, "utf8");

            const use = {{ href: "#i-up", setAttribute(name, value) {{ if (name === "href") this.href = value; }} }};
            const send = {{
              dataset: {{}},
              disabled: false,
              title: "",
              attributes: {{}},
              setAttribute(name, value) {{ this.attributes[name] = String(value); }},
              querySelector(selector) {{ return selector === "use" ? use : null; }},
            }};
            const windowObject = {{}};
            const context = vm.createContext({{
              console,
              Promise,
              window: windowObject,
              document: {{ getElementById(id) {{ return id === "send" ? send : null; }} }},
            }});
            vm.runInContext(source, context, {{ filename: "run-control.js" }});
            const control = windowObject.MuliaoRunControl;

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

    def test_button_switches_between_send_stop_and_send(self):
        self.run_node(
            """
            (async () => {
              let stops = 0;
              const operation = control.begin("chat", async () => { stops += 1; return true; });
              assert.equal(control.mode, "stop");
              assert.equal(send.dataset.mode, "stop");
              assert.equal(send.disabled, false);
              assert.equal(use.href, "#i-pause");
              assert.equal(send.attributes["aria-label"], "暂停当前运行");

              assert.equal(await control.stop(), true);
              assert.equal(stops, 1);
              control.finish(operation);
              assert.equal(control.mode, "send");
              assert.equal(send.dataset.mode, "send");
              assert.equal(use.href, "#i-up");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_rejected_stop_returns_to_clickable_pause_state(self):
        self.run_node(
            """
            (async () => {
              const operation = control.begin("swarm", async () => false);
              assert.equal(await control.stop(), false);
              assert.equal(control.isActive(operation), true);
              assert.equal(control.mode, "stop");
              assert.equal(send.disabled, false);
              assert.equal(use.href, "#i-pause");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

    def test_old_operation_cannot_reset_newer_operation(self):
        self.run_node(
            """
            (() => {
              const first = control.begin("planning", async () => true);
              assert.ok(first);
              assert.equal(control.finish({ id: first.id }), false);
              assert.equal(control.isActive(first), true);
              control.finish(first);
              const second = control.begin("chat", async () => true);
              assert.ok(second);
              assert.equal(control.finish(first), false);
              assert.equal(control.isActive(second), true);
              assert.equal(use.href, "#i-pause");
            })();
            """
        )


if __name__ == "__main__":
    unittest.main()
