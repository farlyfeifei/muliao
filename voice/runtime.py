"""构建独立语音运行时，不接触聊天或蜂群运行时。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .actions import DryRunExecutor, WindowsNotepadExecutor
from .asr_local import SenseVoiceRecognizer
from .audio_player import CancellableAudioPlayer, PyAudioPcmSink
from .capture import PyAudioVADCapture
from .config import VoiceSettings
from .engine import VoiceEngine
from .events import JsonLineEventSink
from .fast_actions import DryRunFastAdapter, FastActionExecutor, WindowsFastAdapter
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

    def close(self) -> None:
        errors: list[BaseException] = []
        seen: set[int] = set()
        for resource in (self.speaker, self.router, self.player, self.sink):
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
    """构建显式 M1+ runtime；当前仅支持 ``mode='fast'``。"""

    if mode != "fast":
        raise ValueError("build_runtime currently supports only mode='fast'")
    settings.validate_m0()
    router = JevFastRouter(
        url=settings.jev_url,
        api_key=settings.jev_key,
        model=settings.jev_model,
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
    recognizer: Any = (
        SenseVoiceRecognizer(settings.sensevoice_dir)
        if enable_asr
        else _TranscriptOnlyRecognizer()
    )
    engine = VoiceEngine(
        permission=ExistingVoicePermission(),
        recognizer=recognizer,
        router=router,
        executor=FastActionExecutor(adapter),
        speaker=speaker,
        events=JsonLineEventSink(),
        echo_guard=echo_guard,
    )
    return engine, VoiceRuntimeResources(
        router=router,
        speaker=speaker,
        player=player,
        sink=sink,
        echo_guard=echo_guard,
    )


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
