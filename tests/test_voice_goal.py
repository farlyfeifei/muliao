from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.goal import GoalLoop, validate_choice
from voice.perception import (
    MAX_ELEMENTS,
    MAX_STATE_CHARS,
    MAX_TEXT_CHARS,
    ScreenPerception,
    Snapshot,
    UIElement,
)


class StaticAdapter:
    def __init__(self, elements):
        self.elements = elements
        self.calls = 0

    def read_elements(self):
        self.calls += 1
        return self.elements


class SequencePerception:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def capture(self):
        self.calls += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


class PerceptionTests(unittest.TestCase):
    def test_uia_is_preferred_and_candidates_are_numbered(self):
        uia = StaticAdapter(
            [
                UIElement(text="打开", role="Button", bounds=(10, 20, 50, 60)),
                UIElement(text="取消", role="Button", bounds=(60, 20, 100, 60)),
            ]
        )
        ocr = StaticAdapter([UIElement(text="OCR 不应被调用", bounds=(1, 1, 2, 2))])

        snapshot = ScreenPerception(uia, ocr).capture()

        self.assertEqual(snapshot.source, "uia")
        self.assertFalse(snapshot.fallback_used)
        self.assertEqual([item.id for item in snapshot.elements], ["e001", "e002"])
        self.assertEqual(ocr.calls, 0)
        self.assertNotIn("screenshot", snapshot.state.casefold())

    def test_empty_uia_tree_uses_coordinate_bearing_ocr_candidates(self):
        uia = StaticAdapter([])
        ocr = StaticAdapter(
            [
                {"text": "下载", "role": "ocr_text", "bounds": (100, 200, 180, 240)},
                {"text": "无坐标", "role": "ocr_text"},
            ]
        )

        snapshot = ScreenPerception(uia, ocr).capture()

        self.assertEqual(snapshot.source, "ocr")
        self.assertTrue(snapshot.fallback_used)
        self.assertEqual(len(snapshot.elements), 1)
        self.assertEqual(snapshot.elements[0].center, (140, 220))
        self.assertIn("center=(140,220)", snapshot.state)

    def test_password_sensitive_and_browser_history_candidates_are_filtered(self):
        uia = StaticAdapter(
            [
                UIElement(text="hunter2", role="Edit", password=True),
                UIElement(text="123456", role="Edit", metadata={"is_password": True}),
                UIElement(text="example.com", role="browser history suggestion"),
                UIElement(text="", role="Button"),
                UIElement(text="确定", role="Button"),
            ]
        )
        snapshot = ScreenPerception(uia, StaticAdapter([])).capture()

        self.assertEqual([item.text for item in snapshot.elements], ["确定"])
        self.assertNotIn("hunter2", snapshot.state)
        self.assertNotIn("123456", snapshot.state)
        self.assertNotIn("example.com", snapshot.state)

    def test_limits_element_count_text_and_state(self):
        elements = [
            UIElement(
                text=("候选" + str(index)) * 100,
                role="Button",
                bounds=(index * 1000, index * 1000, index * 1000 + 999, index * 1000 + 999),
                metadata={"padding": "x" * 1000},
            )
            for index in range(MAX_ELEMENTS + 25)
        ]
        snapshot = Snapshot(tuple(elements), source="uia")

        self.assertEqual(len(snapshot.elements), MAX_ELEMENTS)
        self.assertTrue(all(len(item.text) <= MAX_TEXT_CHARS for item in snapshot.elements))
        self.assertLessEqual(len(snapshot.state), MAX_STATE_CHARS)

    def test_state_truncation_is_explicit(self):
        # Long roles are allowed as structural descriptors; the state itself must
        # still be capped even when candidate text is already bounded.
        role = "control-role-" + ("x" * 500)
        snapshot = Snapshot(
            tuple(
                UIElement(text=f"candidate {index}", role=role, bounds=(index, index, index + 1, index + 1))
                for index in range(MAX_ELEMENTS)
            ),
            source="uia",
        )
        self.assertEqual(len(snapshot.state), MAX_STATE_CHARS)
        self.assertTrue(snapshot.truncated)
        self.assertTrue(snapshot.state.endswith("...[state truncated]"))


class GoalLoopTests(unittest.TestCase):
    @staticmethod
    def snapshot(label="打开"):
        return Snapshot((UIElement(text=label, role="Button", bounds=(1, 2, 30, 40)),), source="uia")

    def test_illegal_id_is_rejected_before_action(self):
        calls = []
        loop = GoalLoop(
            SequencePerception([self.snapshot()]),
            lambda goal, state, step: {"id": "e999", "action": "click"},
            action=lambda element: calls.append(element),
            dry_run=False,
        )

        result = loop.run("点击打开")

        self.assertEqual(result.status, "invalid_choice")
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(ValueError, "unknown element id"):
            validate_choice("e999", self.snapshot())

    def test_dry_run_is_default_and_never_invokes_action(self):
        calls = []
        chooser_states = []

        def choose(goal, state, step):
            chooser_states.append(state)
            return {"id": "e001", "action": "click"}

        loop = GoalLoop(
            SequencePerception([self.snapshot()]),
            choose,
            action=lambda element: calls.append(element),
        )
        result = loop.run("点击打开")

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.steps[0].status, "dry_run")
        self.assertEqual(calls, [])
        self.assertIn("e001", chooser_states[0])
        self.assertNotIn("screenshot", chooser_states[0].casefold())

    def test_settle_then_changed_state_continues_until_complete(self):
        before = self.snapshot("下一步")
        after = self.snapshot("完成")
        sleeps = []
        choices = iter(({"id": "e001"}, {"complete": True}))
        actions = []
        loop = GoalLoop(
            SequencePerception([before, after]),
            lambda goal, state, step: next(choices),
            action=lambda element, action: actions.append((element.id, action)) or {"ok": True},
            dry_run=False,
            settle_seconds=0.25,
            settle=sleeps.append,
        )

        result = loop.run("完成流程")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps[0].status, "changed")
        self.assertTrue(result.steps[0].changed)
        self.assertEqual(actions, [("e001", "activate")])
        self.assertEqual(sleeps, [0.25])

    def test_unchanged_state_stalls(self):
        unchanged = self.snapshot()
        actions = []
        loop = GoalLoop(
            SequencePerception([unchanged, unchanged]),
            lambda goal, state, step: "e001",
            action=lambda element: actions.append(element.id),
            dry_run=False,
        )

        result = loop.run("点击后等待")

        self.assertEqual(result.status, "stalled")
        self.assertEqual(result.steps[0].status, "stalled")
        self.assertFalse(result.steps[0].changed)
        self.assertEqual(actions, ["e001"])

    def test_step_limit_is_hard_capped_at_eight(self):
        snapshots = [self.snapshot(str(index)) for index in range(9)]
        actions = []
        loop = GoalLoop(
            SequencePerception(snapshots),
            lambda goal, state, step: "e001",
            action=lambda element: actions.append(element.text) or {"ok": True, "changed": True},
            dry_run=False,
        )

        result = loop.run("持续下一步")

        self.assertEqual(result.status, "step_limit")
        self.assertEqual(len(result.steps), 8)
        self.assertEqual(len(actions), 8)
        with self.assertRaisesRegex(ValueError, "between 1 and 8"):
            GoalLoop(SequencePerception([self.snapshot()]), lambda state: "e001", max_steps=9)


if __name__ == "__main__":
    unittest.main()
