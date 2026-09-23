from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.cancellation import CancellationToken
from voice.compound import CommandSplitter
from voice.contracts import ActionResult, RouteDecision
from voice.engine import VoiceEngine
from voice.events import MemoryEventSink
from voice.session_state import VoiceSession


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Permission:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls = 0

    def allowed(self) -> bool:
        self.calls += 1
        return self.enabled


class Router:
    def __init__(self, decisions: list[RouteDecision] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.commands: list[str] = []

    def route(self, command: str) -> RouteDecision:
        self.commands.append(command)
        if self.decisions:
            return self.decisions.pop(0)
        return RouteDecision(True, "open_app", "notepad", 0.99)


class Executor:
    def __init__(self) -> None:
        self.decisions: list[RouteDecision] = []

    def execute(self, decision: RouteDecision) -> ActionResult:
        self.decisions.append(decision)
        return ActionResult(True, f"{decision.kind}:{decision.target}", "ok")


class Speaker:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.stops = 0

    def speak(self, text: str) -> None:
        self.texts.append(text)

    def stop(self) -> None:
        self.stops += 1


class Recognizer:
    def transcribe(self, audio):
        raise AssertionError("not used")


def engine_with(*, clock=None, router=None):
    permission = Permission()
    route = router or Router()
    executor = Executor()
    speaker = Speaker()
    events = MemoryEventSink()
    session = VoiceSession(window_seconds=8.0, clock=clock or Clock())
    engine = VoiceEngine(
        permission=permission,
        recognizer=Recognizer(),
        router=route,
        executor=executor,
        speaker=speaker,
        events=events,
        session=session,
    )
    return engine, permission, route, executor, speaker, events, session


class CommandSplitterTests(unittest.TestCase):
    def test_splits_explicit_sequential_commands(self):
        splitter = CommandSplitter()
        self.assertEqual(
            splitter.split("打开记事本，然后打开浏览器，再搜索控制论"),
            ["打开记事本", "打开浏览器", "搜索控制论"],
        )

    def test_does_not_split_inside_quotes(self):
        splitter = CommandSplitter()
        self.assertEqual(
            splitter.split('输入“先打开浏览器然后搜索”然后打开记事本'),
            ['输入“先打开浏览器然后搜索”', "打开记事本"],
        )

    def test_plain_again_phrase_is_not_forced_to_split(self):
        self.assertEqual(CommandSplitter().split("再见"), ["再见"])


class SessionStateTests(unittest.TestCase):
    def test_window_expires_after_eight_seconds(self):
        clock = Clock()
        session = VoiceSession(window_seconds=8, clock=clock)
        self.assertFalse(session.is_active())
        session.activate()
        clock.advance(7.9)
        self.assertTrue(session.is_active())
        clock.advance(0.2)
        self.assertFalse(session.is_active())


class VoiceM1OrchestrationTests(unittest.TestCase):
    def test_wake_command_opens_session_and_followup_needs_no_wake_word(self):
        clock = Clock()
        engine, _, router, executor, speaker, events, session = engine_with(clock=clock)
        first = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(first.status, "executed")
        self.assertTrue(session.is_active())
        clock.advance(2)
        second = engine.process_transcript("再打开记事本")
        self.assertEqual(second.status, "executed")
        self.assertEqual(router.commands, ["打开记事本", "打开记事本"])
        self.assertEqual(len(executor.decisions), 2)
        finals = [event.payload for event in events.events if event.type == "voice.final"]
        self.assertEqual(finals[0]["source"], "wake_phrase")
        self.assertEqual(finals[1]["source"], "active_session")

    def test_followup_after_timeout_is_wake_miss_with_zero_new_route(self):
        clock = Clock()
        engine, _, router, executor, _, _, _ = engine_with(clock=clock)
        engine.process_transcript("幕僚幕僚，打开记事本")
        clock.advance(8.1)
        result = engine.process_transcript("再打开记事本")
        self.assertEqual(result.status, "wake_miss")
        self.assertEqual(router.commands, ["打开记事本"])
        self.assertEqual(len(executor.decisions), 1)

    def test_compound_commands_route_and_execute_in_order(self):
        engine, _, router, executor, speaker, events, _ = engine_with()
        result = engine.process_transcript("幕僚幕僚，打开记事本，然后打开记事本")
        self.assertEqual(result.status, "executed")
        self.assertEqual(router.commands, ["打开记事本", "打开记事本"])
        self.assertEqual(len(result.decisions), 2)
        self.assertEqual(len(result.actions), 2)
        self.assertEqual(len(executor.decisions), 2)
        self.assertEqual(speaker.texts, ["好的，已经完成了。"])
        self.assertTrue(any(e.type == "voice.state" and e.payload.get("state") == "compound" for e in events.events))

    def test_compound_stops_after_rejected_step(self):
        decisions = [
            RouteDecision(True, "open_app", "notepad", 0.99),
            RouteDecision(False, "none", "none", 0.0, reason="unsupported"),
            RouteDecision(True, "open_app", "notepad", 0.99),
        ]
        router = Router(decisions)
        engine, _, _, executor, speaker, _, session = engine_with(router=router)
        result = engine.process_transcript(
            "幕僚幕僚，打开记事本，然后删除文件，然后打开记事本"
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(router.commands, ["打开记事本", "删除文件"])
        self.assertEqual(len(executor.decisions), 1)
        self.assertEqual(speaker.texts, [])
        # 唤醒本身会开启窗口，但失败步骤不刷新窗口。
        self.assertTrue(session.is_active())

    def test_pre_cancelled_operation_has_zero_route_and_action(self):
        token = CancellationToken()
        token.cancel()
        engine, _, router, executor, speaker, events, _ = engine_with()
        result = engine.process_transcript(
            "幕僚幕僚，打开记事本",
            cancellation=token,
        )
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(router.commands, [])
        self.assertEqual(executor.decisions, [])
        self.assertEqual(speaker.texts, [])
        self.assertTrue(any(e.payload.get("state") == "cancelled" for e in events.events))

    def test_cancel_current_stops_speaker_and_emits_event(self):
        engine, _, _, _, speaker, events, _ = engine_with()
        token = CancellationToken()
        engine._current_cancel = token
        engine.cancel_current()
        self.assertTrue(token.cancelled)
        self.assertEqual(speaker.stops, 1)
        self.assertTrue(any(e.payload.get("state") == "cancelled" for e in events.events))


if __name__ == "__main__":
    unittest.main()
