from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.contracts import ActionResult, AudioSegment, RouteDecision, Transcript
from voice.goal_runtime import GoalEngineAdapter
from voice.orchestrator import FAST, GOAL, NONE, OrchestratorResult


class AlwaysPermission:
    def allowed(self) -> bool:
        return True


class DenyPermission:
    def __init__(self) -> None:
        self.run_if_allowed_calls = 0

    def allowed(self) -> bool:
        return False

    def run_if_allowed(self, callback):
        self.run_if_allowed_calls += 1
        return False, None


class FakeOrchestrator:
    """Records process() calls and replays a scripted OrchestratorResult."""

    def __init__(self, result: OrchestratorResult) -> None:
        self.result = result
        self.commands: list[str] = []
        self.cancel_calls = 0

    def process(self, command: str, **kwargs):
        self.commands.append(command)
        return self.result

    def cancel_current(self) -> bool:
        self.cancel_calls += 1
        return True


class CountingRecognizer:
    def __init__(self, text: str = "打开记事本") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return Transcript(self.text)


class FakeCapture:
    def __init__(self) -> None:
        self.calls = 0

    def capture_utterance(self, *, cancellation=None):
        self.calls += 1
        return AudioSegment(b"\0\0" * 160)


def adapter(result, *, permission=None, recognizer=None):
    orchestrator = FakeOrchestrator(result)
    return GoalEngineAdapter(
        orchestrator=orchestrator,
        recognizer=recognizer or CountingRecognizer(),
        permission=permission or AlwaysPermission(),
    ), orchestrator


class StatusMappingTests(unittest.TestCase):
    def test_fast_completed_maps_to_executed_with_channel(self):
        action = ActionResult(True, "open_app:notepad", "opened")
        decision = RouteDecision(True, "open_app", "notepad", 0.99)
        engine, _ = adapter(
            OrchestratorResult("completed", FAST, "打开记事本", decision, action)
        )
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "executed")
        self.assertTrue(result.detail.startswith("[fast]"))
        self.assertIs(result.action, action)
        self.assertIs(result.decision, decision)

    def test_goal_dry_run_maps_to_executed_desktop_channel(self):
        engine, _ = adapter(OrchestratorResult("dry_run", GOAL, "点保存"))
        result = engine.process_transcript("幕僚幕僚，点保存")
        self.assertEqual(result.status, "executed")
        self.assertIn("[desktop]", result.detail)

    def test_rejected_and_permission_denied_pass_through(self):
        engine, _ = adapter(OrchestratorResult("rejected", FAST, "删库"))
        self.assertEqual(engine.process_transcript("幕僚幕僚，删库").status, "rejected")
        engine2, _ = adapter(OrchestratorResult("permission_denied", FAST, "x"))
        self.assertEqual(engine2.process_transcript("幕僚幕僚，x").status, "permission_denied")

    def test_stuck_and_stalled_are_rejected_not_invalid_choice(self):
        for status in ("stuck", "stalled", "no_candidates", "invalid_choice"):
            with self.subTest(status=status):
                engine, _ = adapter(OrchestratorResult(status, GOAL, "g"))
                self.assertEqual(engine.process_transcript("幕僚幕僚，g").status, "rejected")

    def test_confirmation_required_passes_through(self):
        engine, _ = adapter(OrchestratorResult("confirmation_required", FAST, "危险"))
        self.assertEqual(
            engine.process_transcript("幕僚幕僚，危险").status, "confirmation_required"
        )

    def test_unknown_status_is_rejected(self):
        engine, _ = adapter(OrchestratorResult("some_new_status", NONE, "g"))
        self.assertEqual(engine.process_transcript("幕僚幕僚，g").status, "rejected")


class WakeAndEmptyTests(unittest.TestCase):
    def test_empty_transcript_is_wake_miss_without_orchestrator(self):
        engine, orchestrator = adapter(OrchestratorResult("completed", FAST, "x"))
        result = engine.process_transcript("   ")
        self.assertEqual(result.status, "wake_miss")
        self.assertEqual(orchestrator.commands, [])

    def test_cancelled_transcript_clears_command(self):
        engine, _ = adapter(OrchestratorResult("cancelled", FAST, "x"))
        result = engine.process_transcript("幕僚幕僚，x")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.command, "")


class PermissionGateTests(unittest.TestCase):
    def test_transcript_denied_without_orchestrator_call(self):
        engine, orchestrator = adapter(
            OrchestratorResult("completed", FAST, "x"), permission=DenyPermission()
        )
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "permission_denied")
        self.assertEqual(orchestrator.commands, [])

    def test_audio_denied_performs_zero_asr(self):
        recognizer = CountingRecognizer()
        engine, orchestrator = adapter(
            OrchestratorResult("completed", FAST, "x"),
            permission=DenyPermission(),
            recognizer=recognizer,
        )
        result = engine.process_audio(AudioSegment(b"\0\0" * 160))
        self.assertEqual(result.status, "permission_denied")
        self.assertEqual(recognizer.calls, 0, "ASR must not run without permission")
        self.assertEqual(orchestrator.commands, [])

    def test_run_once_denied_never_opens_microphone(self):
        capture = FakeCapture()
        engine, _ = adapter(
            OrchestratorResult("completed", FAST, "x"), permission=DenyPermission()
        )
        result = engine.run_once(capture)
        self.assertEqual(result.status, "permission_denied")
        self.assertEqual(capture.calls, 0)

    def test_run_once_authorized_captures_then_transcribes(self):
        capture = FakeCapture()
        recognizer = CountingRecognizer("打开记事本")
        engine, orchestrator = adapter(
            OrchestratorResult("completed", FAST, "打开记事本",
                               None, ActionResult(True, "open_app:notepad", "ok")),
            recognizer=recognizer,
        )
        result = engine.run_once(capture)
        self.assertEqual(capture.calls, 1)
        self.assertEqual(recognizer.calls, 1)
        self.assertEqual(result.status, "executed")
        self.assertEqual(orchestrator.commands, ["打开记事本"])


class CancellationBridgeTests(unittest.TestCase):
    def test_cancel_current_delegates_to_orchestrator(self):
        engine, orchestrator = adapter(OrchestratorResult("completed", FAST, "x"))
        self.assertTrue(engine.cancel_current())
        self.assertEqual(orchestrator.cancel_calls, 1)

    def test_missing_cancel_methods_report_false_not_crash(self):
        class Bare:
            def process(self, command, **kwargs):
                return OrchestratorResult("completed", FAST, command)

        engine = GoalEngineAdapter(Bare(), CountingRecognizer(), AlwaysPermission())
        self.assertFalse(engine.request_cancel())
        self.assertFalse(engine.stop_current_playback())

    def test_voice_cancelled_maps_to_cancelled_result(self):
        from voice.cancellation import VoiceCancelled

        class Cancelling:
            def process(self, command, **kwargs):
                raise VoiceCancelled("stop")

        engine = GoalEngineAdapter(Cancelling(), CountingRecognizer(), AlwaysPermission())
        self.assertEqual(engine.process_transcript("幕僚幕僚，x").status, "cancelled")


class GoalActionSummaryTests(unittest.TestCase):
    def test_goal_steps_are_summarised_into_an_action_result(self):
        from voice.goal import GoalResult, GoalStep

        goal = GoalResult(
            "dry_run", "保存", (GoalStep(index=1, element_id="e001", action="click",
                                        status="dry_run", dry_run=True, detail="would click"),)
        )
        engine, _ = adapter(OrchestratorResult("dry_run", GOAL, "保存", None, None, goal))
        result = engine.process_transcript("幕僚幕僚，保存")
        self.assertIsNotNone(result.action)
        self.assertEqual(result.action.action, "goal:click")
        self.assertEqual(result.action.metadata.get("channel"), "desktop")


class BuildRuntimeGoalModeTests(unittest.TestCase):
    @staticmethod
    def settings(**changes):
        base = __import__("voice.config", fromlist=["VoiceSettings"]).VoiceSettings(
            sensevoice_dir=Path("C:/models/sensevoice"),
            jev_url="https://example.test/systemone",
            jev_key="",
            jev_model="jev-latest",
        )
        return replace(base, **changes)

    def test_goal_mode_builds_adapter_and_closes_shared_client(self):
        from voice.runtime import build_runtime

        with mock.patch("voice.runtime.SapiSpeaker"), mock.patch(
            "voice.runtime.SenseVoiceRecognizer"
        ):
            engine, resources = build_runtime(
                self.settings(), mode="goal", speak=False, enable_asr=False
            )
        try:
            self.assertIsInstance(engine, GoalEngineAdapter)
            self.assertIsNotNone(resources.goal_client)
            for name in ("process_transcript", "run_once", "cancel_current",
                         "request_cancel", "stop_current_playback"):
                self.assertTrue(callable(getattr(engine, name)))
        finally:
            resources.close()

    def test_goal_mode_unauthorized_command_issues_zero_network(self):
        from voice.runtime import build_runtime

        with mock.patch("voice.runtime.SapiSpeaker"), mock.patch(
            "voice.runtime.SenseVoiceRecognizer"
        ):
            engine, resources = build_runtime(
                self.settings(), mode="goal", speak=False, enable_asr=False
            )
        try:
            # The real ExistingVoicePermission has no voice_control grant in this
            # isolated test, so a goal command must be denied BEFORE the chooser
            # transport runs — zero Jev network calls.
            with mock.patch.object(resources.goal_client, "post") as post:
                result = engine.process_transcript("幕僚幕僚，把文件保存到桌面")
            self.assertEqual(result.status, "permission_denied")
            post.assert_not_called()
        finally:
            resources.close()

    def test_unknown_mode_is_rejected(self):
        from voice.runtime import build_runtime

        with self.assertRaisesRegex(ValueError, "mode='fast' or mode='goal'"):
            build_runtime(self.settings(), mode="web")


if __name__ == "__main__":
    unittest.main()
