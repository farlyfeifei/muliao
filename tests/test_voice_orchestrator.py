from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.cancellation import CancellationToken
from voice.context import AppContext, VoiceContext
from voice.contracts import ActionResult, RouteDecision
from voice.goal import GoalLoop
from voice.orchestrator import FAST, GOAL, VoiceOrchestrator
from voice.perception import Snapshot, UIElement


class AllowPermission:
    def __init__(self, allowed=True):
        self.enabled = allowed
        self.calls = 0

    def allowed(self):
        return self.enabled

    def run_if_allowed(self, callback):
        self.calls += 1
        if not self.enabled:
            return False, None
        return True, callback()


class RevokeAfterWorkPermission(AllowPermission):
    def run_if_allowed(self, callback):
        self.calls += 1
        if not self.enabled:
            return False, None
        value = callback()
        self.enabled = False
        return True, value


class RecordingRouter:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def route(self, command, *, state):
        self.calls.append((command, state))
        return self.decision


class RecordingExecutor:
    def __init__(self, result=None, *, cancel=None):
        self.result = result or ActionResult(True, "open_app:notepad", "done")
        self.cancel = cancel
        self.calls = []

    def execute(self, decision):
        self.calls.append(decision)
        if self.cancel is not None:
            self.cancel.cancel()
        return self.result


class StaticPerception:
    def __init__(self, snapshots, *, foreground=None):
        self.snapshots = list(snapshots)
        self.foreground = foreground
        self.calls = 0

    def capture(self):
        self.calls += 1
        if len(self.snapshots) > 1:
            snapshot = self.snapshots.pop(0)
        else:
            snapshot = self.snapshots[0]
        if self.foreground is None:
            return snapshot
        return {"elements": snapshot.elements, "source": snapshot.source, "foreground_app": self.foreground}


def one_button(label="打开", *, sensitive=False):
    return Snapshot(
        (
            UIElement(
                text=label,
                role="Button",
                bounds=(1, 2, 30, 40),
                sensitive=sensitive,
            ),
        ),
        source="uia",
    )


class FastOrchestrationTests(unittest.TestCase):
    def test_fast_success_records_action_and_refreshes_foreground_elements(self):
        context = VoiceContext()
        context.set_foreground_app(AppContext("browser.exe", "旧页面"))
        router = RecordingRouter(RouteDecision(True, "open_app", "notepad", 0.99))
        executor = RecordingExecutor()
        perception = StaticPerception(
            [one_button("保存")],
            foreground={"name": "notepad.exe", "title": "新建 文本文档"},
        )
        orchestrator = VoiceOrchestrator(
            router=router,
            executor=executor,
            context=context,
            perception=perception,
            permission=AllowPermission(),
        )

        result = orchestrator.process("打开记事本")

        self.assertTrue(result.ok)
        self.assertEqual(result.mode, FAST)
        state = context.build_state("在里面保存")
        self.assertEqual(state["foreground_app"]["name"], "notepad.exe")
        self.assertEqual(state["context"]["previous"]["app"], "browser.exe")
        self.assertEqual(state["context"]["last_target"], "notepad")
        self.assertEqual(state["context"]["recent_actions"][-1]["outcome"], "ok")
        self.assertEqual(state["elements"], ['e001 Button "保存"'])

    def test_fast_failure_is_recorded_as_failure_not_success(self):
        context = VoiceContext()
        executor = RecordingExecutor(ActionResult(False, "screenshot:screen", "adapter failed"))
        orchestrator = VoiceOrchestrator(
            router=RecordingRouter(RouteDecision(True, "screenshot", "screen", 0.99)),
            executor=executor,
            context=context,
            perception=StaticPerception([one_button("重试")]),
            permission=AllowPermission(),
        )

        result = orchestrator.process("截图")

        self.assertEqual(result.status, "action_failed")
        action = context.build_state("重试")["context"]["recent_actions"][-1]
        self.assertEqual(action["outcome"], "failed")
        self.assertEqual(action["detail"], "adapter failed")

    def test_reference_state_is_passed_to_injected_router(self):
        context = VoiceContext()
        context.set_foreground_app(AppContext("notepad.exe", "笔记"))
        context.record_success(said="打开记事本", action="open_app:notepad", target="notepad")
        router = RecordingRouter(RouteDecision(False, "none", "none", reason="no command"))
        orchestrator = VoiceOrchestrator(
            router=router,
            executor=RecordingExecutor(),
            context=context,
            perception=StaticPerception([one_button()]),
            permission=AllowPermission(),
        )

        orchestrator.process("在里面继续")

        passed = router.calls[0][1]
        self.assertEqual(passed["foreground_app"]["name"], "notepad.exe")
        self.assertEqual(passed["context"]["last_target"], "notepad")

    def test_foreground_provider_updates_router_state_without_early_context_commit(self):
        context = VoiceContext()
        context.set_foreground_app(AppContext("browser.exe", "旧页面"))
        router = RecordingRouter(RouteDecision(False, "none", "none", reason="no command"))
        orchestrator = VoiceOrchestrator(
            router=router,
            executor=RecordingExecutor(),
            context=context,
            perception=StaticPerception([one_button()]),
            foreground_provider=lambda: {"name": "notepad.exe", "title": "新笔记"},
            permission=AllowPermission(),
        )

        result = orchestrator.process("在这里继续")

        self.assertEqual(result.status, "rejected")
        passed = router.calls[0][1]
        self.assertEqual(passed["foreground_app"]["name"], "notepad.exe")
        self.assertEqual(passed["context"]["previous"]["app"], "browser.exe")
        self.assertEqual(context.snapshot().foreground_app.name, "browser.exe")

    def test_goal_route_is_honoured_even_when_fast_router_rejects_it(self):
        chooser_states = []

        def choose(goal, state, step):
            chooser_states.append(state)
            return {"id": "e001", "action": "click"}

        loop = GoalLoop(StaticPerception([one_button("下载")]), choose)
        raw = {
            "answers": {
                "addressed": {"noul": 0.99},
                "complete": {"noul": 0.99},
                "destructive": {"noul": 0.01},
                "kind": {"choice": "goal", "confidence": 0.98},
            }
        }
        router = RecordingRouter(
            RouteDecision(False, "goal", "none", 0.0, reason="outside FAST allowlist", raw=raw)
        )
        orchestrator = VoiceOrchestrator(
            router=router,
            executor=RecordingExecutor(),
            goal_loop=loop,
            perception=loop.perception,
            permission=AllowPermission(),
        )

        result = orchestrator.process("点下载按钮")

        self.assertEqual(result.mode, GOAL)
        self.assertEqual(result.status, "dry_run")
        self.assertIn("elements", chooser_states[0])

    def test_malformed_or_unsafe_jev_goal_decision_fails_closed(self):
        perception = StaticPerception([one_button("下载")])
        chooser_calls = []
        loop = GoalLoop(
            perception,
            lambda goal, state, step: chooser_calls.append(goal) or "e001",
        )
        for answers in (
            {"kind": {"choice": "goal", "confidence": 0.1}},
            {
                "addressed": {"noul": 0.99},
                "complete": {"noul": 0.99},
                "destructive": {"noul": 0.9},
                "kind": {"choice": "goal", "confidence": 0.99},
            },
        ):
            with self.subTest(answers=answers):
                router = RecordingRouter(
                    RouteDecision(
                        False,
                        "goal",
                        "none",
                        reason="outside FAST allowlist",
                        raw={"answers": answers},
                    )
                )
                orchestrator = VoiceOrchestrator(
                    router=router,
                    goal_loop=loop,
                    perception=perception,
                    permission=AllowPermission(),
                )
                self.assertEqual(orchestrator.process("点下载").status, "rejected")
        self.assertEqual(chooser_calls, [])
        self.assertEqual(perception.calls, 0)

    def test_needs_screen_attribute_routes_to_goal(self):
        @dataclass
        class ScreenDecision:
            accepted: bool = False
            kind: str = "none"
            target: str = "none"
            needs_screen: bool = True
            reason: str = "screen required"

        loop = GoalLoop(
            StaticPerception([one_button("继续")]),
            lambda goal, state, step: "e001",
        )
        orchestrator = VoiceOrchestrator(
            router=RecordingRouter(ScreenDecision()),
            executor=RecordingExecutor(),
            goal_loop=loop,
            perception=loop.perception,
            permission=AllowPermission(),
        )
        self.assertEqual(orchestrator.process("继续").mode, GOAL)


class GoalOrchestrationTests(unittest.TestCase):
    def test_goal_defaults_to_dry_run_and_act_must_be_explicit(self):
        actions = []
        loop = GoalLoop(
            StaticPerception([one_button("确认")]),
            lambda goal, state, step: {"id": "e001", "action": "click"},
            action=lambda element, action: actions.append((element.id, action)),
            dry_run=False,
        )
        context = VoiceContext()
        orchestrator = VoiceOrchestrator(
            goal_loop=loop,
            context=context,
            perception=loop.perception,
            permission=AllowPermission(),
        )

        result = orchestrator.process_goal("点确认")

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(actions, [])
        recent = context.build_state("继续")["context"]["recent_actions"]
        self.assertEqual(recent[-1]["outcome"], "dry_run")
        self.assertEqual(recent[-1]["target"], "e001")

    def test_goal_act_success_records_each_step_and_final_result(self):
        before = one_button("下一步")
        after = one_button("完成")
        perception = StaticPerception(
            [before, after],
            foreground={"name": "browser.exe", "title": "流程页"},
        )
        choices = iter(({"id": "e001", "action": "click"}, {"complete": True}))
        actions = []
        chooser_states = []

        def choose(goal, state, step):
            chooser_states.append(state)
            return next(choices)

        context = VoiceContext()
        context.set_foreground_app(AppContext("notepad.exe", "之前"))
        loop = GoalLoop(
            perception,
            choose,
            action=lambda element, action: actions.append((element.text, action)) or {"ok": True},
            dry_run=True,
        )
        orchestrator = VoiceOrchestrator(
            goal_loop=loop,
            context=context,
            perception=perception,
            permission=AllowPermission(),
        )

        result = orchestrator.process_goal("完成流程", act=True)

        self.assertEqual(result.status, "completed")
        self.assertEqual(actions, [("下一步", "click")])
        state = context.build_state("刚才那个")
        self.assertEqual(state["foreground_app"]["name"], "browser.exe")
        self.assertEqual(state["context"]["previous"]["app"], "notepad.exe")
        self.assertEqual(state["context"]["last_target"], "e001")
        outcomes = [item["outcome"] for item in state["context"]["recent_actions"]]
        self.assertIn("changed", outcomes)
        self.assertEqual(outcomes[-1], "completed")
        self.assertIn('"last_target":null', chooser_states[0])
        self.assertIn("e001", chooser_states[1])

    def test_goal_stall_records_step_and_terminal_stall(self):
        unchanged = one_button("等待")
        perception = StaticPerception([unchanged, unchanged])
        loop = GoalLoop(
            perception,
            lambda goal, state, step: "e001",
            action=lambda element: {"ok": True},
            dry_run=True,
        )
        context = VoiceContext()
        orchestrator = VoiceOrchestrator(
            goal_loop=loop,
            context=context,
            perception=perception,
            permission=AllowPermission(),
        )

        result = orchestrator.process_goal("点后等待", act=True)

        self.assertEqual(result.status, "stalled")
        recent = context.build_state("继续")["context"]["recent_actions"]
        self.assertEqual([item["outcome"] for item in recent][-2:], ["stalled", "stalled"])
        self.assertEqual(recent[-1]["target"], "e001")

    def test_sensitive_goal_and_elements_are_filtered_from_context(self):
        context = VoiceContext()
        safe_and_sensitive = Snapshot(
            (
                UIElement(text="保存", role="Button", bounds=(1, 1, 2, 2)),
                UIElement(text="API token hunter2", role="Edit", bounds=(3, 3, 4, 4)),
                UIElement(text="123456", role="Password", bounds=(5, 5, 6, 6), password=True),
            ),
            source="uia",
        )
        loop = GoalLoop(
            StaticPerception([safe_and_sensitive]),
            lambda goal, state, step: "e001",
        )
        orchestrator = VoiceOrchestrator(
            goal_loop=loop,
            context=context,
            perception=loop.perception,
            permission=AllowPermission(),
        )

        orchestrator.process_goal("把密码 hunter2 填进去")
        encoded = str(context.build_state("继续")).lower()

        self.assertIn("保存", encoded)
        self.assertNotIn("hunter2", encoded)
        self.assertNotIn("123456", encoded)
        self.assertIn("[redacted]", encoded)


class SafetyAndLifecycleTests(unittest.TestCase):
    def test_sensitive_fast_payload_is_not_stored(self):
        context = VoiceContext()
        secret = "sk-live-super-secret"
        decision = RouteDecision(True, "type_text", "ignored", 0.99, raw={"text": secret})
        orchestrator = VoiceOrchestrator(
            router=RecordingRouter(decision),
            executor=RecordingExecutor(ActionResult(True, "type_text", f"typed {secret}")),
            context=context,
            perception=StaticPerception([one_button()]),
            permission=AllowPermission(),
        )

        orchestrator.process(f"输入 {secret}")
        encoded = str(context.build_state("继续")).lower()

        self.assertNotIn(secret, encoded)
        self.assertIn("[redacted]", encoded)
        self.assertIsNone(context.snapshot().last_target)

    def test_clear_and_purge_delegate_to_context(self):
        now = [100.0]
        context = VoiceContext(ttl_seconds=1, clock=lambda: now[0])
        context.record_success(said="打开", action="open_app", target="notepad")
        orchestrator = VoiceOrchestrator(context=context, permission=AllowPermission())

        now[0] += 2
        self.assertTrue(orchestrator.purge())
        self.assertEqual(context.snapshot().actions, ())
        context.record_success(said="打开", action="open_app", target="notepad")
        orchestrator.clear()
        self.assertEqual(context.snapshot().actions, ())

    def test_unauthorized_operation_makes_zero_router_executor_goal_and_perception_calls(self):
        permission = AllowPermission(False)
        router = RecordingRouter(RouteDecision(True, "open_app", "notepad", 0.99))
        executor = RecordingExecutor()
        perception = StaticPerception([one_button()])
        chooser_calls = []
        loop = GoalLoop(
            perception,
            lambda goal, state, step: chooser_calls.append(goal) or "e001",
            action=lambda element: None,
        )
        orchestrator = VoiceOrchestrator(
            router=router,
            executor=executor,
            goal_loop=loop,
            perception=perception,
            permission=permission,
        )

        fast = orchestrator.process("打开记事本")
        goal = orchestrator.process_goal("点按钮", act=True)

        self.assertEqual(fast.status, "permission_denied")
        self.assertEqual(goal.status, "permission_denied")
        self.assertEqual(router.calls, [])
        self.assertEqual(executor.calls, [])
        self.assertEqual(chooser_calls, [])
        self.assertEqual(perception.calls, 0)

    def test_cancellation_after_fast_execution_does_not_record_or_refresh_context(self):
        token = CancellationToken()
        context = VoiceContext()
        context.set_foreground_app(AppContext("old.exe", "旧窗口"))
        executor = RecordingExecutor(cancel=token)
        perception = StaticPerception([one_button("新控件")], foreground={"name": "new.exe"})
        orchestrator = VoiceOrchestrator(
            router=RecordingRouter(RouteDecision(True, "open_app", "notepad", 0.99)),
            executor=executor,
            context=context,
            perception=perception,
            permission=AllowPermission(),
        )

        result = orchestrator.process("打开记事本", cancellation=token)

        self.assertEqual(result.status, "cancelled")
        snapshot = context.snapshot()
        self.assertEqual(snapshot.actions, ())
        self.assertEqual(snapshot.foreground_app.name, "old.exe")
        self.assertEqual(snapshot.elements, ())
        self.assertEqual(perception.calls, 0)

    def test_permission_revoked_before_commit_records_nothing(self):
        context = VoiceContext()
        permission = RevokeAfterWorkPermission()
        orchestrator = VoiceOrchestrator(
            router=RecordingRouter(RouteDecision(True, "open_app", "notepad", 0.99)),
            executor=RecordingExecutor(),
            context=context,
            perception=StaticPerception([one_button("保存")], foreground={"name": "notepad.exe"}),
            permission=permission,
        )

        result = orchestrator.process("打开记事本")

        self.assertEqual(result.status, "permission_denied")
        snapshot = context.snapshot()
        self.assertEqual(snapshot.actions, ())
        self.assertIsNone(snapshot.foreground_app)
        self.assertEqual(snapshot.elements, ())


if __name__ == "__main__":
    unittest.main()
