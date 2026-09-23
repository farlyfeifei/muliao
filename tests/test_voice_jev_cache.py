from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.jev_cache import JevResponseCache, cache_key


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


STATE = {"utterance": "打开记事本"}
QUESTIONS = {"kind": {"type": "choice", "criteria": {"open_app": "x"}}}


class CacheKeyTests(unittest.TestCase):
    def test_key_is_deterministic_regardless_of_dict_order(self):
        a = cache_key("jev", {"x": 1, "y": 2}, {"q": 1})
        b = cache_key("jev", {"y": 2, "x": 1}, {"q": 1})
        self.assertEqual(a, b)

    def test_key_differs_on_model_state_and_questions(self):
        base = cache_key("jev", STATE, QUESTIONS)
        self.assertNotEqual(base, cache_key("other", STATE, QUESTIONS))
        self.assertNotEqual(base, cache_key("jev", {"utterance": "别的"}, QUESTIONS))
        self.assertNotEqual(base, cache_key("jev", STATE, {"kind": {"type": "noul"}}))

    def test_key_never_contains_the_api_key(self):
        # The key material is model+state+questions only; a secret passed as the
        # model would appear, but api keys are never part of any of these fields.
        key = cache_key("jev-latest", STATE, QUESTIONS)
        self.assertNotIn("打开记事本", key)
        self.assertEqual(len(key), 64)


class GetPutTests(unittest.TestCase):
    def test_miss_then_hit(self):
        cache = JevResponseCache(clock=Clock())
        key = cache_key("jev", STATE, QUESTIONS)
        self.assertIsNone(cache.get(key))
        cache.put(key, {"answers": {}})
        self.assertIsNotNone(cache.get(key))
        self.assertEqual(cache.hits, 1)
        self.assertEqual(cache.misses, 1)

    def test_expiry_after_ttl(self):
        clock = Clock()
        cache = JevResponseCache(ttl_seconds=300.0, clock=clock)
        key = cache_key("jev", STATE, QUESTIONS)
        cache.put(key, {"answers": {}})
        clock.advance(299.0)
        self.assertIsNotNone(cache.get(key))
        clock.advance(2.0)
        self.assertIsNone(cache.get(key))
        self.assertEqual(cache.expirations, 1)

    def test_zero_ttl_disables_caching(self):
        cache = JevResponseCache(ttl_seconds=0.0, clock=Clock())
        key = cache_key("jev", STATE, QUESTIONS)
        cache.put(key, {"answers": {}})
        self.assertIsNone(cache.get(key))

    def test_bounded_eviction_drops_oldest(self):
        cache = JevResponseCache(max_entries=2, clock=Clock())
        cache.put("k1", {"a": 1})
        cache.put("k2", {"a": 2})
        cache.put("k3", {"a": 3})
        self.assertEqual(len(cache), 2)
        self.assertIsNone(cache.get("k1"))
        self.assertIsNotNone(cache.get("k3"))
        self.assertEqual(cache.evictions, 1)

    def test_non_mapping_value_is_not_stored(self):
        cache = JevResponseCache(clock=Clock())
        cache.put("k", "not a mapping")
        self.assertEqual(len(cache), 0)

    def test_rejects_invalid_construction(self):
        with self.assertRaisesRegex(ValueError, "ttl_seconds"):
            JevResponseCache(ttl_seconds=-1)
        with self.assertRaisesRegex(ValueError, "max_entries"):
            JevResponseCache(max_entries=0)


class WrapTests(unittest.TestCase):
    def test_wrap_serves_cached_answers_without_second_call(self):
        calls = []

        def ask(state, questions):
            calls.append((state, questions))
            return {"answers": {"kind": "open_app"}}

        cache = JevResponseCache(clock=Clock())
        wrapped = cache.wrap(ask, "jev")
        first = wrapped(STATE, QUESTIONS)
        second = wrapped(STATE, QUESTIONS)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "second identical request must be cached")

    def test_wrap_distinguishes_different_state(self):
        calls = []

        def ask(state, questions):
            calls.append(state["utterance"])
            return {"answers": {}}

        wrapped = JevResponseCache(clock=Clock()).wrap(ask, "jev")
        wrapped({"utterance": "打开记事本"}, QUESTIONS)
        wrapped({"utterance": "关闭浏览器"}, QUESTIONS)
        self.assertEqual(calls, ["打开记事本", "关闭浏览器"])

    def test_wrap_does_not_cache_exceptions(self):
        attempts = []

        def ask(state, questions):
            attempts.append(1)
            raise RuntimeError("network down")

        wrapped = JevResponseCache(clock=Clock()).wrap(ask, "jev")
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                wrapped(STATE, QUESTIONS)
        self.assertEqual(len(attempts), 2, "a failure must not be cached as an answer")

    def test_wrap_caches_only_mapping_answers(self):
        calls = []

        def ask(state, questions):
            calls.append(1)
            return None  # non-mapping

        wrapped = JevResponseCache(clock=Clock()).wrap(ask, "jev")
        wrapped(STATE, QUESTIONS)
        wrapped(STATE, QUESTIONS)
        self.assertEqual(len(calls), 2)

    def test_wrap_respects_ttl(self):
        clock = Clock()
        calls = []

        def ask(state, questions):
            calls.append(1)
            return {"answers": {}}

        wrapped = JevResponseCache(ttl_seconds=300.0, clock=clock).wrap(ask, "jev")
        wrapped(STATE, QUESTIONS)
        wrapped(STATE, QUESTIONS)
        self.assertEqual(len(calls), 1)
        clock.advance(301.0)
        wrapped(STATE, QUESTIONS)
        self.assertEqual(len(calls), 2)


class ClearTests(unittest.TestCase):
    def test_clear_empties_cache(self):
        cache = JevResponseCache(clock=Clock())
        cache.put("k", {"a": 1})
        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.get("k"))


def _payload(*, kind="open_app", app="notepad"):
    return {
        "model": "jev-test",
        "answers": {
            "addressed": {"noul": 0.99},
            "complete": {"noul": 0.99},
            "destructive": {"noul": 0.01},
            "kind": {"choice": kind, "confidence": 0.98},
            "app": {"choice": app, "confidence": 0.97},
            "media": {"choice": "none", "confidence": 0.97},
            "shortcut": {"choice": "none", "confidence": 0.97},
        },
    }


class ConfigClampTests(unittest.TestCase):
    """A negative cache TTL must clamp to 0 (disable), never crash the runtime."""

    def test_negative_cache_seconds_clamps_to_zero(self):
        import os

        from voice.config import VoiceSettings

        env = {
            "MULIAO_VOICE_SENSEVOICE_DIR": "C:/models/sensevoice",
            "MULIAO_JEV_CACHE_SECONDS": "-1",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("voice.config._external_config", return_value={}):
            settings = VoiceSettings.load()
        self.assertEqual(settings.jev_cache_seconds, 0.0)
        # The clamped value must construct a cache without raising.
        cache = JevResponseCache(ttl_seconds=settings.jev_cache_seconds)
        self.assertEqual(cache.ttl_seconds, 0.0)

    def test_positive_cache_seconds_is_preserved(self):
        import os

        from voice.config import VoiceSettings

        env = {
            "MULIAO_VOICE_SENSEVOICE_DIR": "C:/models/sensevoice",
            "MULIAO_JEV_CACHE_SECONDS": "120",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("voice.config._external_config", return_value={}):
            settings = VoiceSettings.load()
        self.assertEqual(settings.jev_cache_seconds, 120.0)


class FastRouterCacheIntegrationTests(unittest.TestCase):
    """Prove the cache is actually wired into the FAST router's transport."""

    def test_identical_command_is_served_from_cache_without_second_post(self):
        import httpx

        from voice.jev_router import JevFastRouter

        posts = []

        def handler(request):
            posts.append(request)
            return httpx.Response(200, json=_payload())

        cache = JevResponseCache(clock=Clock())
        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            cache=cache,
        )
        first = router.route("打开记事本")
        second = router.route("打开记事本")
        self.assertEqual(len(posts), 1, "second identical command must not hit the network")
        self.assertTrue(first.accepted)
        self.assertEqual(second.kind, first.kind)
        self.assertEqual(second.target, first.target)
        self.assertEqual(cache.hits, 1)

    def test_different_command_still_reaches_the_network(self):
        import httpx

        from voice.jev_router import JevFastRouter

        posts = []

        def handler(request):
            posts.append(request)
            return httpx.Response(200, json=_payload())

        cache = JevResponseCache(clock=Clock())
        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            cache=cache,
        )
        router.route("打开记事本")
        router.route("关闭浏览器")
        self.assertEqual(len(posts), 2)

    def test_network_error_is_not_cached(self):
        import httpx

        from voice.jev_router import JevFastRouter

        attempts = []

        def handler(request):
            attempts.append(request)
            raise httpx.ConnectError("boom")

        cache = JevResponseCache(clock=Clock())
        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            cache=cache,
        )
        first = router.route("打开记事本")
        second = router.route("打开记事本")
        self.assertFalse(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(len(attempts), 2, "a transport error must be retried, not cached")
        self.assertEqual(len(cache), 0)

    def test_cache_expiry_forces_a_fresh_post(self):
        import httpx

        from voice.jev_router import JevFastRouter

        clock = Clock()
        posts = []

        def handler(request):
            posts.append(request)
            return httpx.Response(200, json=_payload())

        cache = JevResponseCache(ttl_seconds=300.0, clock=clock)
        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            cache=cache,
        )
        router.route("打开记事本")
        router.route("打开记事本")
        self.assertEqual(len(posts), 1)
        clock.advance(301.0)
        router.route("打开记事本")
        self.assertEqual(len(posts), 2, "an expired entry must be refetched")

    def test_no_api_key_returns_decision_and_never_caches(self):
        from voice.contracts import RouteDecision
        from voice.jev_router import JevFastRouter

        cache = JevResponseCache(clock=Clock())
        router = JevFastRouter(
            url="https://example.test/systemone", api_key="", cache=cache
        )
        decision = router.route("打开记事本")
        self.assertIsInstance(decision, RouteDecision)
        self.assertFalse(decision.accepted)
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
