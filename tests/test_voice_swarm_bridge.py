"""voice.swarm_bridge 语音→蜂群桥测试（纯注入，无需 Jev/网络）。"""
from __future__ import annotations

import asyncio
import unittest

from voice.swarm_bridge import SwarmBridge, SwarmSubmitResult, looks_swarm_worthy


class HeuristicTests(unittest.TestCase):
    def test_single_action_not_swarm_worthy(self):
        self.assertFalse(looks_swarm_worthy("打开记事本"))
        self.assertFalse(looks_swarm_worthy("调大音量"))

    def test_multi_step_signals_detected(self):
        self.assertTrue(looks_swarm_worthy("分别调研甲乙丙再汇总"))
        self.assertTrue(looks_swarm_worthy("先扫描再评估然后排期"))
        # 足够长的复合指令也算
        self.assertTrue(looks_swarm_worthy("把项目里所有 TODO 注释整理成一份清单并评估工作量"))


def _plan_result(swarm_worthy, recipe="research", run_id="run_x", requires_confirmation=True):
    return {"ok": True, "plan": {
        "swarm_worthy": swarm_worthy, "recipe": recipe, "run_id": run_id,
        "requires_confirmation": requires_confirmation, "status": "planned",
    }}


class SubmitTests(unittest.TestCase):
    def test_disabled_bridge_reports_unavailable(self):
        b = SwarmBridge(plan_callable=lambda g, s: _plan_result(True), enabled=False)
        r = b.submit("分别调研甲乙丙再汇总")
        self.assertFalse(r.ok)
        self.assertIn("未启用", r.detail)

    def test_no_plan_callable_unavailable(self):
        b = SwarmBridge(plan_callable=None)
        self.assertFalse(b.available)
        r = b.submit("分别调研甲乙丙再汇总")
        self.assertFalse(r.ok)

    def test_heuristic_skips_single_action(self):
        called = {"n": 0}
        def plan(g, s): called["n"] += 1; return _plan_result(True)
        b = SwarmBridge(plan_callable=plan)
        r = b.submit("打开记事本")
        self.assertFalse(r.escalated)
        self.assertEqual(called["n"], 0, "单动作不该花 Jev 规划调用")

    def test_force_bypasses_heuristic(self):
        called = {"n": 0}
        def plan(g, s): called["n"] += 1; return _plan_result(False)
        b = SwarmBridge(plan_callable=plan)
        r = b.submit("打开记事本", force=True)
        self.assertEqual(called["n"], 1, "force 应直接问 Jev")
        self.assertTrue(r.ok)
        self.assertFalse(r.escalated)
        self.assertEqual(r.status, "single")

    def test_swarm_worthy_plans_and_waits_confirm(self):
        b = SwarmBridge(plan_callable=lambda g, s: _plan_result(True), auto_run=False)
        r = b.submit("分别调研甲乙丙再汇总复核")
        self.assertTrue(r.ok and r.escalated and r.swarm_worthy)
        self.assertEqual(r.status, "planned")
        self.assertEqual(r.run_id, "run_x")

    def test_auto_run_invokes_run_callable(self):
        ran = {"called": False}
        def run(plan, goal, sid):
            ran["called"] = True
            return {"ok": True, "run_id": "run_started"}
        b = SwarmBridge(plan_callable=lambda g, s: _plan_result(True),
                        run_callable=run, auto_run=True)
        r = b.submit("分别调研甲乙丙再汇总复核")
        self.assertTrue(ran["called"])
        self.assertEqual(r.status, "running")
        self.assertEqual(r.run_id, "run_started")

    def test_plan_failure_collected(self):
        def plan(g, s): raise RuntimeError("jev down")
        b = SwarmBridge(plan_callable=plan)
        r = b.submit("分别调研甲乙丙再汇总复核")
        self.assertFalse(r.ok)
        self.assertIn("规划失败", r.detail)

    def test_plan_not_ok_collected(self):
        b = SwarmBridge(plan_callable=lambda g, s: {"ok": False, "err": "budget"})
        r = b.submit("分别调研甲乙丙再汇总复核")
        self.assertFalse(r.ok)
        self.assertIn("未通过", r.detail)

    def test_empty_goal_rejected(self):
        b = SwarmBridge(plan_callable=lambda g, s: _plan_result(True))
        r = b.submit("   ")
        self.assertFalse(r.ok)


class SubmitAsyncTests(unittest.TestCase):
    def test_async_plan_swarm_worthy(self):
        async def plan(g, s):
            await asyncio.sleep(0)
            return _plan_result(True, recipe="build", run_id="run_async")
        b = SwarmBridge(plan_callable=plan)
        r = asyncio.run(b.submit_async("分别调研甲乙丙再汇总复核"))
        self.assertTrue(r.ok and r.escalated and r.swarm_worthy)
        self.assertEqual(r.recipe, "build")
        self.assertEqual(r.run_id, "run_async")

    def test_async_single_action_skips_plan(self):
        called = {"n": 0}
        async def plan(g, s):
            called["n"] += 1
            return _plan_result(True)
        b = SwarmBridge(plan_callable=plan)
        r = asyncio.run(b.submit_async("打开记事本"))
        self.assertFalse(r.escalated)
        self.assertEqual(called["n"], 0)

    def test_async_force_jev_says_single(self):
        async def plan(g, s):
            await asyncio.sleep(0)
            return _plan_result(False)
        b = SwarmBridge(plan_callable=plan)
        r = asyncio.run(b.submit_async("打开记事本", force=True))
        self.assertTrue(r.ok)
        self.assertFalse(r.escalated)
        self.assertEqual(r.status, "single")


if __name__ == "__main__":
    unittest.main()
