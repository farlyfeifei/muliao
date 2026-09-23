"""构建独立 M0 引擎，不接触聊天或蜂群运行时。"""
from __future__ import annotations

from .actions import DryRunExecutor, WindowsNotepadExecutor
from .asr_local import SenseVoiceRecognizer
from .capture import PyAudioVADCapture
from .config import VoiceSettings
from .engine import VoiceEngine
from .events import JsonLineEventSink
from .jev_router import JevM0Router
from .permission_gate import ExistingVoicePermission
from .tts_local import NullSpeaker, SapiSpeaker


def build_engine(settings: VoiceSettings, *, act: bool = False, speak: bool = True):
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
        speaker=SapiSpeaker() if speak else NullSpeaker(),
        events=JsonLineEventSink(),
    )
    return engine, router


def build_capture(settings: VoiceSettings) -> PyAudioVADCapture:
    return PyAudioVADCapture(
        sample_rate=settings.sample_rate,
        frame_ms=settings.frame_ms,
        vad_mode=settings.vad_mode,
        silence_ms=settings.silence_ms,
        pre_roll_ms=settings.pre_roll_ms,
        max_utterance_ms=settings.max_utterance_ms,
        listen_timeout_ms=settings.listen_timeout_ms,
        min_speech_ms=settings.min_speech_ms,
        input_device_index=settings.input_device_index,
    )
