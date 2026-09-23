from __future__ import annotations

from collections import defaultdict
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.contracts import (
    ActionResult,
    AudioSegment,
    RouteDecision,
    Transcript,
    VoiceResult,
)
from voice.engine import VoiceEngine
from voice.events import JsonLineEventSink, MemoryEventSink
from voice.session_state import VoiceSession


WAIT_SECONDS = 3.0
AUDIO = AudioSegment(b"\0\0" * 160)
OPEN_NOTEPAD = RouteDecision(True, "open_app", "notepad", 0.99)
STOP = RouteDecision(True, "stop", "current", 0.99)
TERMINAL_STATES = {
    "cancelled",
    "completed",
    "confirmation_required",
    "rejected",
    "sleeping",
    "wake_detected",
}


def start_call(function: Callable[[], Any], *, name: str) -> tuple[threading.Thread, threading.Event, dict[str, Any]]:
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def invoke() -> None:
        try:
            outcome["result"] = function()
        except BaseException as exc:  # Preserve worker failures for the test thread.
            outcome["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=invoke, name=name, daemon=True)
    thread.start()
    return thread, done, outcome


def finish_call(
    case: unittest.TestCase,
    thread: threading.Thread,
    done: threading.Event,
    outcome: dict[str, Any],
) -> Any:
    case.assertTrue(done.wait(WAIT_SECONDS), f"worker {thread.name!r} did not finish")
    thread.join(timeout=0)
    case.assertFalse(thread.is_alive(), f"worker {thread.name!r} is still alive")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def terminal_events(events: MemoryEventSink, operation_id: int) -> list[Any]:
    return [
        event
        for event in events.events
        if event.payload.get("operation_id") == operation_id
        and (
            event.type == "voice.error"
            or (event.type == "voice.state" and event.payload.get("state") in TERMINAL_STATES)
        )
    ]


class AlwaysPermission:
    """Permission without run_if_allowed, useful for stale-operation races."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def allowed(self) -> bool:
        return self.enabled


class LinearPermission:
    """The production permission contract reduced to one explicit RLock."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._lock = threading.RLock()

    def allowed(self) -> bool:
        with self._lock:
            return self._enabled

    def run_if_allowed(self, callback: Callable[[], Any]) -> tuple[bool, Any | None]:
        with self._lock:
            if not self._enabled:
                return False, None
            return True, callback()

    def revoke(self) -> None:
        with self._lock:
            self._enabled = False


class BoundaryPermission(LinearPermission):
    """Pause immediately before a selected run_if_allowed linearization point."""

    def __init__(self, boundary_call: int) -> None:
        super().__init__(True)
        self.boundary_call = boundary_call
        self.boundary_entered = threading.Event()
        self.release_boundary = threading.Event()
        self._calls_lock = threading.Lock()
        self.run_calls = 0

    def run_if_allowed(self, callback: Callable[[], Any]) -> tuple[bool, Any | None]:
        with self._calls_lock:
            self.run_calls += 1
            call_number = self.run_calls
        if call_number == self.boundary_call:
            self.boundary_entered.set()
            if not self.release_boundary.wait(WAIT_SECONDS):
                raise TimeoutError("permission boundary was not released")
        return super().run_if_allowed(callback)


class PostCommitPermission(LinearPermission):
    """Force revocation to win immediately after the action callback commits."""

    def __init__(self) -> None:
        super().__init__(True)
        self._calls_lock = threading.Lock()
        self.run_calls = 0
        self.action_callback_returned = threading.Event()
        self.revocation_finished = threading.Event()

    def run_if_allowed(self, callback: Callable[[], Any]) -> tuple[bool, Any | None]:
        with self._calls_lock:
            self.run_calls += 1
            call_number = self.run_calls
        with self._lock:
            if not self._enabled:
                return False, None
            value = callback()
        if call_number == 3:  # activate, router, action
            self.action_callback_returned.set()
            if not self.revocation_finished.wait(WAIT_SECONDS):
                raise TimeoutError("post-action revocation did not finish")
        return True, value

    def revoke(self) -> None:
        with self._lock:
            self._enabled = False
        self.revocation_finished.set()


class ThreadBoundaryPermission:
    """Block one run_if_allowed call in one named operation thread."""

    def __init__(self, *, thread_name: str, call_number: int) -> None:
        self.thread_name = thread_name
        self.call_number = call_number
        self.boundary_entered = threading.Event()
        self.release_boundary = threading.Event()
        self._lock = threading.Lock()
        self._calls_by_thread: dict[str, int] = defaultdict(int)

    def allowed(self) -> bool:
        return True

    def run_if_allowed(self, callback: Callable[[], Any]) -> tuple[bool, Any | None]:
        name = threading.current_thread().name
        with self._lock:
            self._calls_by_thread[name] += 1
            current_call = self._calls_by_thread[name]
        if name == self.thread_name and current_call == self.call_number:
            self.boundary_entered.set()
            if not self.release_boundary.wait(WAIT_SECONDS):
                raise TimeoutError("stale-operation boundary was not released")
        return True, callback()


class UnusedRecognizer:
    def transcribe(self, audio: AudioSegment, *, cancellation: Any | None = None) -> Transcript:
        raise AssertionError("recognizer should not be called")


class RecordingRecognizer:
    def __init__(self, text: str = "幕僚幕僚，打开记事本") -> None:
        self.text = text
        self.calls = 0
        self._lock = threading.Lock()

    def transcribe(self, audio: AudioSegment, *, cancellation: Any | None = None) -> Transcript:
        with self._lock:
            self.calls += 1
        return Transcript(self.text)


class BlockingRecognizer(RecordingRecognizer):
    def __init__(self, text: str = "幕僚幕僚，打开记事本") -> None:
        super().__init__(text)
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio: AudioSegment, *, cancellation: Any | None = None) -> Transcript:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if not self.release.wait(WAIT_SECONDS):
            raise TimeoutError("recognizer was not released")
        return Transcript(self.text)


class BlockingCapture:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.stop_calls = 0
        self._lock = threading.Lock()

    def capture_utterance(self, *, cancellation: Any | None = None) -> AudioSegment:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if not self.release.wait(WAIT_SECONDS):
            raise TimeoutError("capture was not stopped")
        return AUDIO

    def stop(self) -> None:
        with self._lock:
            self.stop_calls += 1
        self.release.set()


class RecordingRouter:
    def __init__(self, decision: RouteDecision = OPEN_NOTEPAD) -> None:
        self.decision = decision
        self.commands: list[str] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return len(self.commands)

    def route(self, command: str) -> RouteDecision:
        with self._lock:
            self.commands.append(command)
        return self.decision


class CommandAwareRouter(RecordingRouter):
    def route(self, command: str) -> RouteDecision:
        with self._lock:
            self.commands.append(command)
        if command == "停止":
            return STOP
        return OPEN_NOTEPAD


class BlockingFirstRouter:
    def __init__(self, old_decision: RouteDecision) -> None:
        self.old_decision = old_decision
        self.commands: list[str] = []
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self._lock = threading.Lock()

    def route(self, command: str) -> RouteDecision:
        with self._lock:
            index = len(self.commands)
            self.commands.append(command)
        if index == 0:
            self.first_entered.set()
            if not self.release_first.wait(WAIT_SECONDS):
                raise TimeoutError("old router call was not released")
            return self.old_decision
        return OPEN_NOTEPAD


class RecordingExecutor:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.decisions: list[RouteDecision] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return len(self.decisions)

    def execute(self, decision: RouteDecision) -> ActionResult:
        with self._lock:
            self.decisions.append(decision)
        return ActionResult(
            self.ok,
            f"{decision.kind}:{decision.target}",
            "executed" if self.ok else "executor failed",
        )


class CommitMarkingExecutor(RecordingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.committed = threading.Event()

    def execute(self, decision: RouteDecision) -> ActionResult:
        result = super().execute(decision)
        self.committed.set()
        return result


class BlockingUncommittedExecutor(RecordingExecutor):
    """Return from a cancelled call without ever marking its side effect committed."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.committed = threading.Event()

    def execute(self, decision: RouteDecision) -> ActionResult:
        with self._lock:
            self.decisions.append(decision)
        self.entered.set()
        if not self.release.wait(WAIT_SECONDS):
            raise TimeoutError("uncommitted executor was not released")
        return ActionResult(
            True,
            f"{decision.kind}:{decision.target}",
            "cancelled before commit",
            {"committed": False},
        )


class BlockingCommittedExecutor(RecordingExecutor):
    """Expose a deterministic commit point before allowing execute() to return."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.allow_commit = threading.Event()
        self.committed = threading.Event()
        self.allow_return = threading.Event()

    def execute(self, decision: RouteDecision) -> ActionResult:
        with self._lock:
            self.decisions.append(decision)
        self.entered.set()
        if not self.allow_commit.wait(WAIT_SECONDS):
            raise TimeoutError("executor commit was not authorized")
        self.committed.set()
        if not self.allow_return.wait(WAIT_SECONDS):
            raise TimeoutError("committed executor was not released")
        return ActionResult(
            True,
            f"{decision.kind}:{decision.target}",
            "committed",
            {"committed": True},
        )


class RecordingSpeaker:
    def __init__(self) -> None:
        self.spoken: list[tuple[int | None, str]] = []
        self.stop_calls: list[int | None] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return len(self.spoken)

    def speak(
        self,
        text: str,
        *,
        cancellation: Any | None = None,
        operation_id: int | None = None,
    ) -> None:
        with self._lock:
            self.spoken.append((operation_id, text))

    def stop(self, operation_id: int | None = None) -> None:
        with self._lock:
            self.stop_calls.append(operation_id)


class BlockingSpeaker(RecordingSpeaker):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active_operation_id: int | None = None

    def speak(
        self,
        text: str,
        *,
        cancellation: Any | None = None,
        operation_id: int | None = None,
    ) -> None:
        with self._lock:
            self.spoken.append((operation_id, text))
            self.active_operation_id = operation_id
        self.entered.set()
        if not self.release.wait(WAIT_SECONDS):
            raise TimeoutError("speaker was not stopped")

    def stop(self, operation_id: int | None = None) -> None:
        super().stop(operation_id)
        with self._lock:
            active = self.active_operation_id
        if operation_id is None or operation_id == active:
            self.release.set()


class TrackingSession(VoiceSession):
    def __init__(self) -> None:
        super().__init__(window_seconds=60.0)
        self.activations: list[int | None] = []
        self.touches: list[int | None] = []
        self.closes: list[tuple[int | None, bool]] = []
        self._history_lock = threading.Lock()

    def activate(self, owner: int | None = None) -> bool:
        result = super().activate(owner)
        with self._history_lock:
            self.activations.append(owner)
        return result

    def touch(self, owner: int | None = None) -> bool:
        result = super().touch(owner)
        with self._history_lock:
            self.touches.append(owner)
        return result

    def close(self, owner: int | None = None) -> bool:
        result = super().close(owner)
        with self._history_lock:
            self.closes.append((owner, result))
        return result


class SerializationProbeStream(io.TextIOBase):
    """Block the first write so concurrent write calls become observable."""

    def __init__(self) -> None:
        super().__init__()
        self.first_write_entered = threading.Event()
        self.release_first_write = threading.Event()
        self.writes: list[str] = []
        self.overlapped = False
        self._active_writes = 0
        self._first = True
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._active_writes += 1
            if self._active_writes > 1:
                self.overlapped = True
            is_first = self._first
            if is_first:
                self._first = False
                self.first_write_entered.set()
        if is_first and not self.release_first_write.wait(WAIT_SECONDS):
            raise TimeoutError("first JSON write was not released")
        with self._lock:
            self.writes.append(text)
            self._active_writes -= 1
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        with self._lock:
            return "".join(self.writes)


def build_engine(
    *,
    permission: Any | None = None,
    recognizer: Any | None = None,
    router: Any | None = None,
    executor: Any | None = None,
    speaker: Any | None = None,
    events: MemoryEventSink | None = None,
    session: VoiceSession | None = None,
) -> VoiceEngine:
    return VoiceEngine(
        permission=permission or LinearPermission(),
        recognizer=recognizer or UnusedRecognizer(),
        router=router or RecordingRouter(),
        executor=executor or RecordingExecutor(),
        speaker=speaker or RecordingSpeaker(),
        events=events or MemoryEventSink(),
        session=session or VoiceSession(window_seconds=60.0),
    )


class VoiceCancellationConcurrencyTests(unittest.TestCase):
    def test_cancel_during_capture_stops_pipeline_and_emits_one_cancelled_terminal(self) -> None:
        capture = BlockingCapture()
        recognizer = RecordingRecognizer()
        router = RecordingRouter()
        executor = RecordingExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(
            recognizer=recognizer,
            router=router,
            executor=executor,
            speaker=speaker,
            events=events,
        )
        self.addCleanup(capture.release.set)

        thread, done, outcome = start_call(lambda: engine.run_once(capture), name="capture-operation")
        self.assertTrue(capture.entered.wait(WAIT_SECONDS), "capture did not block")
        self.assertTrue(engine.cancel_current())
        result = finish_call(self, thread, done, outcome)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(capture.stop_calls, 1)
        self.assertEqual(recognizer.calls, 0)
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload.get("state"), "cancelled")

    def test_cancel_during_transcribe_skips_router_action_and_tts_with_one_terminal(self) -> None:
        recognizer = BlockingRecognizer()
        router = RecordingRouter()
        executor = RecordingExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(
            recognizer=recognizer,
            router=router,
            executor=executor,
            speaker=speaker,
            events=events,
        )
        self.addCleanup(recognizer.release.set)

        thread, done, outcome = start_call(lambda: engine.process_audio(AUDIO), name="transcribe-operation")
        self.assertTrue(recognizer.entered.wait(WAIT_SECONDS), "recognizer did not block")
        self.assertTrue(engine.cancel_current())
        recognizer.release.set()
        result = finish_call(self, thread, done, outcome)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload.get("state"), "cancelled")

    def _assert_stale_blocked_router_is_harmless(self, old_decision: RouteDecision) -> None:
        router = BlockingFirstRouter(old_decision)
        executor = RecordingExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        session = VoiceSession(window_seconds=60.0)
        engine = build_engine(
            permission=AlwaysPermission(),
            router=router,
            executor=executor,
            speaker=speaker,
            events=events,
            session=session,
        )
        self.addCleanup(router.release_first.set)

        old_thread, old_done, old_outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，旧命令"),
            name="old-router-operation",
        )
        self.assertTrue(router.first_entered.wait(WAIT_SECONDS), "old router call did not block")

        new_result = engine.process_transcript("幕僚幕僚，新命令")
        self.assertEqual(new_result.status, "executed")
        self.assertEqual(session.owner, 2)
        self.assertTrue(session.is_active())

        router.release_first.set()
        old_result = finish_call(self, old_thread, old_done, old_outcome)

        self.assertEqual(old_result.status, "cancelled")
        self.assertEqual(executor.calls, 1, "the stale router result must not execute")
        self.assertEqual(speaker.stop_calls, [1], "only superseding operation 1 may be stopped")
        self.assertEqual(session.owner, 2, "stale operation must not close the new owner")
        self.assertTrue(session.is_active())
        old_completed = [
            event
            for event in events.events
            if event.payload.get("operation_id") == 1
            and event.type == "voice.state"
            and event.payload.get("state") == "completed"
        ]
        self.assertEqual(old_completed, [])
        new_terminals = terminal_events(events, 2)
        self.assertEqual(len(new_terminals), 1)
        self.assertEqual(new_terminals[0].payload.get("state"), "completed")

    def test_stale_blocked_router_returning_stop_cannot_stop_or_close_new_operation(self) -> None:
        self._assert_stale_blocked_router_is_harmless(STOP)

    def test_stale_blocked_router_returning_action_cannot_execute_or_complete_old_operation(self) -> None:
        self._assert_stale_blocked_router_is_harmless(OPEN_NOTEPAD)

    def test_cancel_during_uncommitted_executor_is_cancelled_not_executed(self) -> None:
        executor = BlockingUncommittedExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(executor=executor, speaker=speaker, events=events)
        self.addCleanup(executor.release.set)

        thread, done, outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，打开记事本"),
            name="uncommitted-action-operation",
        )
        self.assertTrue(executor.entered.wait(WAIT_SECONDS), "executor did not block")
        self.assertFalse(executor.committed.is_set())
        self.assertTrue(engine.cancel_current())
        executor.release.set()
        result = finish_call(self, thread, done, outcome)

        self.assertFalse(executor.committed.is_set())
        self.assertEqual(
            result.status,
            "cancelled",
            "an executor that never committed its side effect must not be reported executed",
        )
        self.assertEqual(speaker.calls, 0)
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload.get("state"), "cancelled")

    def test_cancel_after_executor_commit_returns_executed_and_skips_tts(self) -> None:
        executor = BlockingCommittedExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(executor=executor, speaker=speaker, events=events)
        self.addCleanup(executor.allow_commit.set)
        self.addCleanup(executor.allow_return.set)

        thread, done, outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，打开记事本"),
            name="committed-action-operation",
        )
        self.assertTrue(executor.entered.wait(WAIT_SECONDS), "executor did not block")
        executor.allow_commit.set()
        self.assertTrue(executor.committed.wait(WAIT_SECONDS), "executor did not mark commit")
        self.assertTrue(engine.cancel_current())
        executor.allow_return.set()
        result = finish_call(self, thread, done, outcome)

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.detail, "tts_skipped_cancelled")
        self.assertEqual(speaker.calls, 0)
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload.get("state"), "completed")

    def test_cancel_during_blocking_speaker_unblocks_and_has_one_completed_terminal(self) -> None:
        speaker = BlockingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(speaker=speaker, events=events)
        self.addCleanup(speaker.release.set)

        thread, done, outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，打开记事本"),
            name="speaking-operation",
        )
        self.assertTrue(speaker.entered.wait(WAIT_SECONDS), "speaker did not block")
        self.assertTrue(engine.cancel_current())
        result = finish_call(self, thread, done, outcome)

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.detail, "tts_skipped_cancelled")
        self.assertEqual(speaker.calls, 1)
        self.assertEqual(speaker.stop_calls, [1])
        tts_states = [
            event.payload.get("state")
            for event in events.events
            if event.type == "voice.tts" and event.payload.get("operation_id") == 1
        ]
        self.assertEqual(tts_states, ["started"])
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload.get("state"), "completed")


class PermissionLinearizationTests(unittest.TestCase):
    def _run_at_boundary(self, boundary_call: int) -> tuple[
        VoiceResult,
        BoundaryPermission,
        RecordingRouter,
        RecordingExecutor,
        RecordingSpeaker,
        MemoryEventSink,
    ]:
        permission = BoundaryPermission(boundary_call)
        router = RecordingRouter()
        executor = RecordingExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(
            permission=permission,
            router=router,
            executor=executor,
            speaker=speaker,
            events=events,
        )
        self.addCleanup(permission.release_boundary.set)

        thread, done, outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，打开记事本"),
            name=f"permission-boundary-{boundary_call}",
        )
        self.assertTrue(permission.boundary_entered.wait(WAIT_SECONDS), "permission boundary was not reached")
        permission.revoke()
        permission.release_boundary.set()
        result = finish_call(self, thread, done, outcome)
        return result, permission, router, executor, speaker, events

    def test_revocation_at_router_boundary_prevents_router_side_effect(self) -> None:
        result, _, router, executor, speaker, events = self._run_at_boundary(2)

        self.assertEqual(result.status, "permission_denied")
        self.assertIn("before routing", result.detail)
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].type, "voice.error")

    def test_revocation_at_action_boundary_prevents_action_side_effect(self) -> None:
        result, _, router, executor, speaker, events = self._run_at_boundary(3)

        self.assertEqual(result.status, "permission_denied")
        self.assertIn("before action", result.detail)
        self.assertEqual(router.calls, 1)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].type, "voice.error")

    def test_revocation_at_tts_boundary_keeps_committed_action_and_skips_tts(self) -> None:
        result, _, router, executor, speaker, events = self._run_at_boundary(5)

        self.assertEqual(router.calls, 1)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(speaker.calls, 0)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.detail, "tts_skipped_permission_revoked")
        self.assertFalse(any(event.type == "voice.error" for event in events.events))
        terminals = terminal_events(events, 1)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload.get("state"), "completed")

    def test_revocation_immediately_after_action_commit_is_executed_not_permission_denied(self) -> None:
        permission = PostCommitPermission()
        executor = CommitMarkingExecutor()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(
            permission=permission,
            executor=executor,
            speaker=speaker,
            events=events,
        )
        self.addCleanup(permission.revocation_finished.set)

        thread, done, outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，打开记事本"),
            name="post-commit-revocation",
        )
        self.assertTrue(executor.committed.wait(WAIT_SECONDS), "action did not commit")
        permission.revoke()
        result = finish_call(self, thread, done, outcome)

        self.assertTrue(permission.action_callback_returned.is_set())
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.detail, "tts_skipped_permission_revoked")
        self.assertEqual(executor.calls, 1)
        self.assertEqual(speaker.calls, 0)
        self.assertFalse(any(event.type == "voice.error" for event in events.events))


class StaleSessionOwnershipTests(unittest.TestCase):
    def test_stale_operation_blocked_before_activate_cannot_activate_or_close_new_owner(self) -> None:
        thread_name = "stale-before-activate"
        permission = ThreadBoundaryPermission(thread_name=thread_name, call_number=1)
        session = TrackingSession()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(
            permission=permission,
            speaker=speaker,
            events=events,
            session=session,
        )
        self.addCleanup(permission.release_boundary.set)

        old_thread, old_done, old_outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，旧命令"),
            name=thread_name,
        )
        self.assertTrue(permission.boundary_entered.wait(WAIT_SECONDS), "old activate boundary was not reached")

        new_result = engine.process_transcript("幕僚幕僚，新命令")
        self.assertEqual(new_result.status, "executed")
        permission.release_boundary.set()
        old_result = finish_call(self, old_thread, old_done, old_outcome)

        self.assertEqual(old_result.status, "cancelled")
        self.assertEqual(session.activations, [2])
        self.assertEqual(session.touches, [2])
        self.assertEqual(session.owner, 2)
        self.assertTrue(session.is_active())
        self.assertIn((1, False), session.closes)
        self.assertEqual(speaker.stop_calls, [1])

    def test_stale_operation_blocked_before_touch_cannot_touch_or_close_new_owner(self) -> None:
        thread_name = "stale-before-touch"
        permission = ThreadBoundaryPermission(thread_name=thread_name, call_number=4)
        session = TrackingSession()
        speaker = RecordingSpeaker()
        events = MemoryEventSink()
        engine = build_engine(
            permission=permission,
            speaker=speaker,
            events=events,
            session=session,
        )
        self.addCleanup(permission.release_boundary.set)

        old_thread, old_done, old_outcome = start_call(
            lambda: engine.process_transcript("幕僚幕僚，旧命令"),
            name=thread_name,
        )
        self.assertTrue(permission.boundary_entered.wait(WAIT_SECONDS), "old touch boundary was not reached")

        new_result = engine.process_transcript("幕僚幕僚，新命令")
        self.assertEqual(new_result.status, "executed")
        permission.release_boundary.set()
        old_result = finish_call(self, old_thread, old_done, old_outcome)

        self.assertEqual(old_result.status, "executed")
        self.assertEqual(old_result.detail, "tts_skipped_cancelled")
        self.assertEqual(session.activations, [1, 2])
        self.assertEqual(session.touches, [2], "stale operation 1 must not touch the new session")
        self.assertEqual(session.owner, 2)
        self.assertTrue(session.is_active())
        self.assertIn((1, False), session.closes)
        old_completed = [
            event
            for event in events.events
            if event.payload.get("operation_id") == 1
            and event.type == "voice.state"
            and event.payload.get("state") == "completed"
        ]
        self.assertEqual(old_completed, [])

    def test_unwoken_utterance_after_owner_closes_session_is_wake_miss_without_router(self) -> None:
        router = CommandAwareRouter()
        executor = RecordingExecutor()
        speaker = RecordingSpeaker()
        session = VoiceSession(window_seconds=60.0)
        engine = build_engine(router=router, executor=executor, speaker=speaker, session=session)

        first = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(first.status, "executed")
        stopped = engine.process_transcript("停止")
        self.assertEqual(stopped.status, "executed")
        self.assertFalse(session.is_active())
        self.assertIsNone(session.owner)
        routed_before_miss = router.calls

        missed = engine.process_transcript("打开记事本")

        self.assertEqual(missed.status, "wake_miss")
        self.assertEqual(router.calls, routed_before_miss)
        self.assertEqual(executor.calls, 1)


class EventSinkConcurrencyTests(unittest.TestCase):
    def test_memory_event_sink_concurrent_sequences_are_unique_and_strictly_increasing(self) -> None:
        workers = 8
        events_per_worker = 40
        sink = MemoryEventSink()
        start = threading.Barrier(workers + 1)
        calls: list[tuple[threading.Thread, threading.Event, dict[str, Any]]] = []

        def emit_batch(worker: int) -> None:
            start.wait(timeout=WAIT_SECONDS)
            for index in range(events_per_worker):
                sink.emit("voice.metric", {"worker": worker, "index": index})

        for worker in range(workers):
            calls.append(start_call(lambda worker=worker: emit_batch(worker), name=f"memory-events-{worker}"))
        start.wait(timeout=WAIT_SECONDS)
        for call in calls:
            finish_call(self, *call)

        expected = workers * events_per_worker
        sequences = [event.seq for event in sink.events]
        self.assertEqual(len(sequences), expected)
        self.assertEqual(sequences, list(range(1, expected + 1)))
        payloads = {(event.payload["worker"], event.payload["index"]) for event in sink.events}
        self.assertEqual(len(payloads), expected)

    def test_json_line_event_sink_serializes_concurrent_writes_and_sequences(self) -> None:
        workers = 8
        events_per_worker = 30
        stream = SerializationProbeStream()
        sink = JsonLineEventSink(stream)
        start = threading.Barrier(workers + 1)
        attempted_lock = threading.Lock()
        all_attempted = threading.Event()
        attempted = 0
        calls: list[tuple[threading.Thread, threading.Event, dict[str, Any]]] = []

        def emit_batch(worker: int) -> None:
            nonlocal attempted
            start.wait(timeout=WAIT_SECONDS)
            with attempted_lock:
                attempted += 1
                if attempted == workers:
                    all_attempted.set()
            for index in range(events_per_worker):
                sink.emit("voice.metric", {"worker": worker, "index": index})

        for worker in range(workers):
            calls.append(start_call(lambda worker=worker: emit_batch(worker), name=f"json-events-{worker}"))
        start.wait(timeout=WAIT_SECONDS)
        self.assertTrue(all_attempted.wait(WAIT_SECONDS), "not all JSON writers attempted emit")
        self.assertTrue(stream.first_write_entered.wait(WAIT_SECONDS), "JSON stream was not entered")
        stream.release_first_write.set()
        for call in calls:
            finish_call(self, *call)

        expected = workers * events_per_worker
        self.assertFalse(stream.overlapped, "JsonLineEventSink allowed overlapping stream.write calls")
        self.assertEqual(len(stream.writes), expected)
        self.assertTrue(all(write.endswith("\n") and write.count("\n") == 1 for write in stream.writes))
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), expected)
        self.assertEqual([row["seq"] for row in rows], list(range(1, expected + 1)))
        payloads = {(row["payload"]["worker"], row["payload"]["index"]) for row in rows}
        self.assertEqual(len(payloads), expected)


class VoiceResultCompatibilityTests(unittest.TestCase):
    def test_voice_result_keeps_legacy_six_positional_arguments(self) -> None:
        decision = OPEN_NOTEPAD
        action = ActionResult(True, "open_app:notepad", "opened")

        result = VoiceResult("executed", "legacy transcript", "legacy command", decision, action, "legacy detail")

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.transcript, "legacy transcript")
        self.assertEqual(result.command, "legacy command")
        self.assertIs(result.decision, decision)
        self.assertIs(result.action, action)
        self.assertEqual(result.detail, "legacy detail")
        self.assertEqual(result.decisions, ())
        self.assertEqual(result.actions, ())

    def test_success_result_restores_original_transcript(self) -> None:
        engine = build_engine()
        text = "幕僚幕僚，打开记事本"

        result = engine.process_transcript(text)

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.transcript, text)

    def test_confirmation_result_restores_original_transcript(self) -> None:
        decision = RouteDecision(
            accepted=False,
            kind="delete",
            target="all",
            confidence=0.99,
            destructive=True,
        )
        engine = build_engine(router=RecordingRouter(decision))
        text = "幕僚幕僚，删除全部文件"

        result = engine.process_transcript(text)

        self.assertEqual(result.status, "confirmation_required")
        self.assertEqual(result.transcript, text)

    def test_action_failure_result_restores_original_transcript(self) -> None:
        engine = build_engine(executor=RecordingExecutor(ok=False))
        text = "幕僚幕僚，打开记事本"

        result = engine.process_transcript(text)

        self.assertEqual(result.status, "action_failed")
        self.assertEqual(result.transcript, text)


if __name__ == "__main__":
    unittest.main()
