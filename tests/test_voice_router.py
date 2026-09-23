from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.jev_router import JevFastRouter


def response(
    *,
    kind: str,
    kind_conf: float = 0.98,
    app: str = "none",
    app_conf: float = 0.97,
    media: str = "none",
    media_conf: float = 0.97,
    shortcut: str = "none",
    shortcut_conf: float = 0.97,
    addressed: float = 0.99,
    complete: float = 0.99,
    destructive: float = 0.01,
):
    return {
        "model": "jev-test",
        "answers": {
            "addressed": {"noul": addressed},
            "complete": {"noul": complete},
            "destructive": {"noul": destructive},
            "kind": {"choice": kind, "confidence": kind_conf},
            "app": {"choice": app, "confidence": app_conf},
            "media": {"choice": media, "confidence": media_conf},
            "shortcut": {"choice": shortcut, "confidence": shortcut_conf},
        },
    }


class JevFastRouterTests(unittest.TestCase):
    @staticmethod
    def router(payload):
        def handler(request: httpx.Request):
            return httpx.Response(200, json=payload)

        return JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_open_app_uses_allowlisted_app_choice(self):
        decision = self.router(response(kind="open_app", app="browser")).route("打开浏览器")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.kind, "open_app")
        self.assertEqual(decision.target, "browser")

    def test_search_query_is_selected_verbatim_from_command(self):
        command = "搜索 幕僚 SendInput & x=1"
        decision = self.router(response(kind="search")).route(command)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.kind, "search")
        self.assertEqual(decision.raw["query"], "幕僚 SendInput & x=1")
        self.assertEqual(command[decision.raw["span"]["start"]:decision.raw["span"]["end"]], decision.raw["query"])

    def test_type_text_and_url_are_not_generated_by_jev(self):
        typed = self.router(response(kind="type_text")).route('输入“你好，世界”')
        opened = self.router(response(kind="open_url")).route("打开 https://example.com/a?q=1")
        self.assertEqual(typed.raw["text"], "你好，世界")
        self.assertEqual(opened.raw["url"], "https://example.com/a?q=1")

    def test_volume_media_shortcut_and_screenshot_targets_are_normalized(self):
        up = self.router(response(kind="volume_up")).route("调大音量")
        media = self.router(response(kind="media", media="next")).route("下一首")
        shortcut = self.router(response(kind="shortcut", shortcut="save")).route("保存")
        screenshot = self.router(response(kind="screenshot")).route("截图")
        self.assertEqual((up.kind, up.target, up.raw["steps"]), ("volume", "up", 2))
        self.assertEqual((media.kind, media.target), ("media", "next"))
        self.assertEqual((shortcut.kind, shortcut.target), ("shortcut", "save"))
        self.assertEqual((screenshot.kind, screenshot.target), ("screenshot", "screen"))

    def test_missing_span_low_confidence_and_destructive_routes_are_rejected(self):
        missing = self.router(response(kind="search")).route("搜索")
        low = self.router(response(kind="open_app", app="notepad", kind_conf=0.2)).route("打开记事本")
        destructive = self.router(response(kind="type_text", destructive=0.9)).route("输入密码")
        self.assertFalse(missing.accepted)
        self.assertFalse(low.accepted)
        self.assertFalse(destructive.accepted)
        self.assertTrue(destructive.destructive)

    def test_goal_and_none_are_never_accepted_by_fast_router(self):
        self.assertFalse(self.router(response(kind="goal")).route("点下载按钮").accepted)
        self.assertFalse(self.router(response(kind="none")).route("今天天气不错").accepted)

    def test_request_contains_no_candidate_generated_text(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=response(kind="search"))

        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        router.route("搜索 控制论")
        self.assertEqual(json.loads(seen[0]["state"]), {"utterance": "搜索 控制论"})
        self.assertNotIn("query", seen[0]["state"])

    def test_bounded_state_is_forwarded_to_jev_request(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=response(kind="open_app", app="notepad"))

        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        state = {
            "foreground_app": {"name": "notepad.exe", "window_title": "无标题"},
            "context": {"recent_actions": [{"said": "打开记事本", "action": "open_app:notepad"}]},
            "elements": ["e01 button 文件"],
        }
        router.route("打开记事本", state)

        sent = json.loads(seen[0]["state"])
        # utterance 始终来自命令；受限上下文按原样进入请求，供 Jev 做指代判断。
        self.assertEqual(sent["utterance"], "打开记事本")
        self.assertEqual(sent["foreground_app"]["name"], "notepad.exe")
        self.assertEqual(sent["context"]["recent_actions"][0]["action"], "open_app:notepad")
        self.assertEqual(sent["elements"], ["e01 button 文件"])

    def test_state_utterance_is_not_overwritten_when_already_present(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=response(kind="open_app", app="notepad"))

        router = JevFastRouter(
            url="https://example.test/systemone",
            api_key="test-only",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        router.route("忽略我", {"utterance": "打开记事本"})
        self.assertEqual(json.loads(seen[0]["state"])["utterance"], "打开记事本")

    def test_goal_kind_sets_needs_screen_stable_field(self):
        goal = self.router(response(kind="goal")).route("点下载按钮")
        plain = self.router(response(kind="open_app", app="notepad")).route("打开记事本")
        self.assertTrue(goal.raw["needs_screen"])
        self.assertFalse(goal.accepted, "FAST router never accepts goal; orchestrator routes it")
        self.assertFalse(plain.raw["needs_screen"])


if __name__ == "__main__":
    unittest.main()
