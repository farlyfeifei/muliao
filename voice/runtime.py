"""构建独立语音运行时，不接触聊天或蜂群运行时。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .actions import DryRunExecutor, WindowsNotepadExecutor
from .asr_fallback import FallbackRecognizer
from .asr_local import SenseVoiceRecognizer
from .asr_mimo import MiMoAsrClient
from .audio_player import CancellableAudioPlayer, PyAudioPcmSink
from .capture import PyAudioVADCapture
from .config import VoiceSettings
from .engine import VoiceEngine
from .events import JsonLineEventSink
from .fast_actions import DryRunFastAdapter, FastActionExecutor, WindowsFastAdapter
from .jev_cache import JevResponseCache
from .jev_router import JevFastRouter, JevM0Router
from .permission_gate import ExistingVoicePermission
from .safety import EchoAwareSpeaker, EchoGuard
from .tts_local import FallbackSpeaker, NullSpeaker, SapiSpeaker
from .tts_mimo import MiMoTtsClient


class _TranscriptOnlyRecognizer:
    def transcribe(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("audio recognition is disabled for this runtime")


@dataclass
class VoiceRuntimeResources:
    """统一释放 Jev、MiMo、播放器与声卡。"""

    router: Any
    speaker: object
    player: CancellableAudioPlayer | None = None
    sink: PyAudioPcmSink | None = None
    echo_guard: EchoGuard | None = None
    goal_client: Any | None = None
    jev_cache: JevResponseCache | None = None
    recognizer: Any | None = None

    def close(self) -> None:
        errors: list[BaseException] = []
        seen: set[int] = set()
        for resource in (
            self.speaker,
            self.router,
            self.player,
            self.sink,
            self.goal_client,
            self.recognizer,
        ):
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
        # The cache holds no OS resources; drop its entries so a later reuse
        # never serves a stale answer across runtime lifetimes.
        if self.jev_cache is not None:
            self.jev_cache.clear()
        if errors:
            raise ExceptionGroup("voice runtime resource close failed", errors)


def _local_speaker(speak: bool) -> object:
    return SapiSpeaker() if speak else NullSpeaker()


def build_engine(settings: VoiceSettings, *, act: bool = False, speak: bool = True):
    """保留 M0 兼容入口：只允许记事本，并返回 ``(engine, router)``。"""

    settings.validate_m0()
    router = JevM0Router(
        url=settings.jev_url,
        api_key=settings.jev_key,
        model=settings.jev_model,
    )
    engine = VoiceEngine(
        permission=ExistingVoicePermission(),
        recognizer=SenseVoiceRecognizer(settings.sensevoice_dir),
        router=router,
        executor=WindowsNotepadExecutor() if act else DryRunExecutor(),
        speaker=_local_speaker(speak),
        events=JsonLineEventSink(),
    )
    return engine, router


def build_runtime(
    settings: VoiceSettings,
    *,
    mode: str = "fast",
    act: bool = False,
    speak: bool = True,
    enable_asr: bool = True,
):
    """构建显式 M1+ runtime；支持 ``mode='fast'``（FAST 单通道）与 ``mode='goal'``
    （FAST/DESKTOP-GOAL 三通道分流，经 VoiceOrchestrator）。"""

    if mode not in {"fast", "goal"}:
        raise ValueError("build_runtime supports mode='fast' or mode='goal'")
    settings.validate_m0()
    jev_cache = (
        JevResponseCache(ttl_seconds=settings.jev_cache_seconds)
        if settings.jev_cache_enabled
        else None
    )
    router = JevFastRouter(
        url=settings.jev_url,
        api_key=settings.jev_key,
        model=settings.jev_model,
        cache=jev_cache,
    )
    echo_guard = EchoGuard() if speak else None
    local_speaker = _local_speaker(speak)
    player = None
    sink = None
    base_speaker: object = local_speaker
    if speak and settings.mimo_tts_enabled and settings.mimo_api_key:
        sink = PyAudioPcmSink()
        player = CancellableAudioPlayer(sink)
        cloud = MiMoTtsClient(
            api_key=settings.mimo_api_key,
            player=player,
            model=settings.mimo_tts_model,
            voice=settings.mimo_tts_voice,
            base_url=settings.mimo_base_url,
        )
        base_speaker = FallbackSpeaker(cloud, local_speaker)
    speaker: object = (
        EchoAwareSpeaker(base_speaker, echo_guard)
        if speak and echo_guard is not None
        else base_speaker
    )

    adapter = WindowsFastAdapter() if act else DryRunFastAdapter()
    recognizer = _build_recognizer(settings, enable_asr=enable_asr)
    permission = ExistingVoicePermission()
    events = JsonLineEventSink()

    if mode == "goal":
        engine, goal_client = _build_goal_engine(
            settings,
            router=router,
            recognizer=recognizer,
            permission=permission,
            speaker=speaker,
            events=events,
            echo_guard=echo_guard,
            act=act,
            jev_cache=jev_cache,
        )
        return engine, VoiceRuntimeResources(
            router=router,
            speaker=speaker,
            player=player,
            sink=sink,
            echo_guard=echo_guard,
            goal_client=goal_client,
            jev_cache=jev_cache,
            recognizer=recognizer,
        )

    engine = VoiceEngine(
        permission=permission,
        recognizer=recognizer,
        router=router,
        executor=FastActionExecutor(adapter),
        speaker=speaker,
        events=events,
        echo_guard=echo_guard,
    )
    return engine, VoiceRuntimeResources(
        router=router,
        speaker=speaker,
        player=player,
        sink=sink,
        echo_guard=echo_guard,
        jev_cache=jev_cache,
        recognizer=recognizer,
    )


def _build_recognizer(settings: VoiceSettings, *, enable_asr: bool) -> Any:
    """Local SenseVoice recognizer, optionally wrapped with a MiMo cloud fallback.

    The cloud ASR fallback is OFF by default: sending audio off-device is a
    privacy-sensitive action, so it only engages when explicitly enabled AND a
    MiMo key is present. Even then it fires solely on a missing/unloadable local
    model (see asr_fallback._is_missing_model_error) — never to paper over a real
    decode error — and a cloud failure re-raises the original local error so an
    empty transcript never reaches Jev. ``_get_recognizer()`` delegates to the
    local model, so warm-up and model validation still exercise SenseVoice.
    """

    if not enable_asr:
        return _TranscriptOnlyRecognizer()
    local = SenseVoiceRecognizer(settings.sensevoice_dir)
    cloud_enabled = settings.mimo_asr_enabled and bool(settings.mimo_api_key)
    if not cloud_enabled:
        return local
    cloud = MiMoAsrClient(
        api_key=settings.mimo_api_key,
        model=settings.mimo_asr_model,
        base_url=settings.mimo_base_url,
    )
    return FallbackRecognizer(local, cloud, api_key=settings.mimo_api_key)


def _build_goal_engine(
    settings: VoiceSettings,
    *,
    router: Any,
    recognizer: Any,
    permission: Any,
    speaker: object,
    events: Any,
    echo_guard: EchoGuard | None,
    act: bool,
    jev_cache: JevResponseCache | None = None,
):
    """Wire the FAST/DESKTOP-GOAL orchestrator behind the engine interface.

    Returns ``(engine, goal_client)`` where ``goal_client`` is the shared httpx
    client owned by the runtime resources so it is closed exactly once. The
    desktop GOAL action executor stays dry-run unless ``act`` is set; there is
    no real Windows UIA/OCR actuator yet (that is a later milestone), so an
    ``act=True`` desktop GOAL still plans through the dry-run executor.
    """

    import httpx

    from .goal import DryRunActionExecutor, GoalLoop
    from .goal_router import JevGoalChooser
    from .goal_runtime import GoalEngineAdapter, make_goal_ask
    from .orchestrator import VoiceOrchestrator

    # One direct, certificate-validated client shared by the chooser transport;
    # trust_env=False mirrors the FAST router's proxy workaround.
    goal_client = httpx.Client(timeout=15.0, trust_env=False)
    goal_ask = make_goal_ask(
        settings.jev_url, settings.jev_key, settings.jev_model, client=goal_client
    )
    if jev_cache is not None:
        goal_ask = jev_cache.wrap(goal_ask, settings.jev_model)
    chooser = JevGoalChooser(
        ask=goal_ask,
        api_key=settings.jev_key,
        model=settings.jev_model,
    )
    goal_loop = GoalLoop(
        perception=None,
        chooser=chooser,
        action=DryRunActionExecutor(),
        dry_run=not act,
    )
    orchestrator = VoiceOrchestrator(
        goal_loop=goal_loop,
        router=router,
        executor=FastActionExecutor(DryRunFastAdapter() if not act else WindowsFastAdapter()),
        permission=permission,
    )
    engine = GoalEngineAdapter(
        orchestrator=orchestrator,
        recognizer=recognizer,
        permission=permission,
        speaker=speaker,
        events=events,
    )
    engine.echo_guard = echo_guard
    return engine, goal_client


def build_capture(
    settings: VoiceSettings,
    *,
    echo_guard: EchoGuard | None = None,
    frame_consumer: Any | None = None,
    frame_resetter: Any | None = None,
) -> PyAudioVADCapture:
    capture = PyAudioVADCapture(
        sample_rate=settings.sample_rate,
        frame_ms=settings.frame_ms,
        vad_mode=settings.vad_mode,
        silence_ms=settings.silence_ms,
        pre_roll_ms=settings.pre_roll_ms,
        max_utterance_ms=settings.max_utterance_ms,
        listen_timeout_ms=settings.listen_timeout_ms,
        min_speech_ms=settings.min_speech_ms,
        input_device_index=settings.input_device_index,
        mute_gate=None if echo_guard is None else echo_guard.mute_gate,
    )
    capture.frame_consumer = frame_consumer
    capture.frame_resetter = frame_resetter
    return capture
