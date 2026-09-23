"""Jev 响应 TTL 缓存（M5）。

doc 13 §7.4 与语音控制缓存策略：相同 utterance/state 在 5 分钟内不重复请求，
省额度也降延迟。本模块把缓存做成**包裹 ask 传输**的装饰器，因此 FAST router、
桌面 GOAL chooser、WEB-GOAL chooser 都能复用同一实现，而不改动各自的问答逻辑。

安全与正确性：

- key = sha256(model + 规范化 state JSON + 规范化 questions JSON)。key 里**不含
  API key**（questions/state 本就不带密钥），日志也不打印 key 原文。
- 只缓存**成功**的 answers；网络/HTTP 异常不进缓存（由调用方抛错处理）。
- 有界：超过 ``max_entries`` 时按插入顺序淘汰最旧项，避免长会话无限增长。
- 线程安全；TTL 过期项在读取与写入时惰性清理。
- 缓存的是 Jev 的选择题答案（校准概率），不缓存任何自由文本生成——与
  select-not-generate 一致。
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading
import time
from typing import Any, Callable, Mapping

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 512

AskCallable = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cache_key(model: str, state: Mapping[str, Any], questions: Mapping[str, Any]) -> str:
    """Deterministic digest of a Jev request; never contains the API key."""

    material = _canonical(
        {"model": model, "state": dict(state), "questions": dict(questions)}
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class JevResponseCache:
    """Bounded TTL cache of Jev answers, usable standalone or as a transport wrapper."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, tuple[float, Mapping[str, Any]]]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _purge_expired_locked(self) -> None:
        # ttl_seconds == 0 means "cache disabled": now - at >= 0 is always true,
        # so every stored entry is immediately expired and get() always misses.
        now = self._clock()
        expired = [key for key, (at, _) in self._entries.items() if now - at >= self.ttl_seconds]
        for key in expired:
            del self._entries[key]
            self.expirations += 1

    def get(self, key: str) -> Mapping[str, Any] | None:
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[1]

    def put(self, key: str, answers: Mapping[str, Any]) -> None:
        if not isinstance(answers, Mapping):
            return
        with self._lock:
            self._purge_expired_locked()
            self._entries[key] = (self._clock(), answers)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def wrap(self, ask: AskCallable, model: str) -> AskCallable:
        """Return a transport that serves cached answers and stores fresh ones.

        Only successful (Mapping) answers are cached; an exception from the
        underlying transport propagates untouched and is not cached.
        """

        def cached_ask(state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
            key = cache_key(model, state, questions)
            hit = self.get(key)
            if hit is not None:
                return hit
            answers = ask(state, questions)
            if isinstance(answers, Mapping):
                self.put(key, answers)
            return answers

        return cached_ask


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "JevResponseCache",
    "cache_key",
]
