"""独立 M0 命令行入口。

默认 dry-run；只有显式 ``--act`` 才真正打开记事本。
"""
from __future__ import annotations

import argparse
import json
import sys
import wave

from .config import VoiceSettings
from .contracts import AudioSegment
from .runtime import build_capture, build_engine, build_runtime


def _read_wave(path: str) -> AudioSegment:
    with wave.open(path, "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        pcm = source.readframes(source.getnframes())
    if channels != 1 or sample_width != 2:
        raise ValueError("M0 WAV input must be 16-bit mono PCM")
    return AudioSegment(
        pcm=pcm,
        sample_rate=sample_rate,
        sample_width=sample_width,
        channels=channels,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="幕僚言出法随 M0 独立验收")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--microphone", action="store_true", help="从真实麦克风采一条命令")
    source.add_argument("--audio-file", help="读取一条 16-bit mono WAV")
    source.add_argument("--text", help="跳过 ASR，用已转写文本验收后半链路")
    parser.add_argument("--fast", action="store_true", help="显式启用 M1 FAST 动作；默认保持 M0 仅记事本")
    parser.add_argument("--act", action="store_true", help="真正执行当前模式白名单动作；默认 dry-run")
    parser.add_argument("--no-tts", action="store_true", help="不播放确认语")
    args = parser.parse_args(argv)

    settings = VoiceSettings.load()
    engine = lifecycle = None
    try:
        if args.fast:
            engine, lifecycle = build_runtime(
                settings,
                mode="fast",
                act=args.act,
                speak=not args.no_tts,
            )
        else:
            engine, lifecycle = build_engine(settings, act=args.act, speak=not args.no_tts)
        if args.text is not None:
            result = engine.process_transcript(args.text)
        elif args.audio_file:
            result = engine.process_audio(_read_wave(args.audio_file))
        else:
            result = engine.run_once(build_capture(settings))
        print(json.dumps({
            "status": result.status,
            "command": result.command,
            "detail": result.detail,
            "action": result.action.action if result.action else None,
            "action_ok": result.action.ok if result.action else None,
        }, ensure_ascii=False))
        return 0 if result.status in {"executed", "wake_only", "wake_miss"} else 2
    except Exception as exc:
        print(json.dumps({
            "type": "voice.error",
            "payload": {"code": type(exc).__name__, "detail": str(exc)},
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if lifecycle is not None:
            close_speaker = getattr(getattr(engine, "speaker", None), "close", None)
            if callable(close_speaker) and not hasattr(lifecycle, "speaker"):
                close_speaker()
            lifecycle.close()


if __name__ == "__main__":
    raise SystemExit(main())
