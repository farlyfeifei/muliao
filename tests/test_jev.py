from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _AsyncClient:
    responses = []
    calls = 0

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        type(self).calls += 1
        value = type(self).responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class JevRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _AsyncClient.responses = []
        _AsyncClient.calls = 0

    async def test_rate_limit_retries_once_then_succeeds(self):
        _AsyncClient.responses = [
            _Response(429, text="busy"),
            _Response(200, {"model": "jev-test", "answers": {"ok": {"type": "noul", "noul": 0.9}}, "usage": {}}),
        ]
        with (
            patch.object(server.httpx, "AsyncClient", _AsyncClient),
            patch.object(server, "_log_jev_failure"),
            patch.object(server.asyncio, "sleep", new=asyncio.sleep),
        ):
            result = await server.jev_ask("state", {"ok": {"type": "noul", "instructions": "Proceed?"}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(_AsyncClient.calls, 2)

    async def test_quota_failure_is_not_retried(self):
        _AsyncClient.responses = [_Response(402, text="insufficient credits")]
        with (
            patch.object(server.httpx, "AsyncClient", _AsyncClient),
            patch.object(server, "_log_jev_failure"),
        ):
            result = await server.jev_ask("state", {"ok": {"type": "noul", "instructions": "Proceed?"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "quota")
        self.assertTrue(result["need_topup"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(_AsyncClient.calls, 1)

    async def test_read_timeout_is_not_retried_to_avoid_duplicate_billing(self):
        _AsyncClient.responses = [server.httpx.ReadTimeout("slow")]
        with (
            patch.object(server.httpx, "AsyncClient", _AsyncClient),
            patch.object(server, "_log_jev_failure"),
        ):
            result = await server.jev_ask("state", {"ok": {"type": "noul", "instructions": "Proceed?"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "timeout")
        self.assertFalse(result["need_topup"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(_AsyncClient.calls, 1)
        self.assertIn("避免重复计费", result["err"])

    async def test_connect_error_retries_once_then_succeeds(self):
        _AsyncClient.responses = [
            server.httpx.ConnectError("not connected"),
            _Response(200, {"model": "jev-test", "answers": {"ok": {"type": "noul", "noul": 0.9}}, "usage": {}}),
        ]
        with (
            patch.object(server.httpx, "AsyncClient", _AsyncClient),
            patch.object(server, "_log_jev_failure"),
            patch.object(server.asyncio, "sleep", new=asyncio.sleep),
        ):
            result = await server.jev_ask("state", {"ok": {"type": "noul", "instructions": "Proceed?"}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["answers"]["ok"]["noul"], 0.9)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(_AsyncClient.calls, 2)

    async def test_partial_answers_fail_closed(self):
        _AsyncClient.responses = [
            _Response(200, {"model": "jev-test", "answers": {"first": {"type": "noul", "noul": 0.9}}}),
        ]
        questions = {
            "first": {"type": "noul", "instructions": "First?"},
            "second": {"type": "noul", "instructions": "Second?"},
        }
        with (
            patch.object(server.httpx, "AsyncClient", _AsyncClient),
            patch.object(server, "_log_jev_failure"),
        ):
            result = await server.jev_ask("state", questions)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "bad_response")
        self.assertIn("second", result["err"])
        self.assertEqual(_AsyncClient.calls, 1)

    async def test_bad_success_payload_fails_closed(self):
        _AsyncClient.responses = [_Response(200, {"model": "jev-test"})]
        with (
            patch.object(server.httpx, "AsyncClient", _AsyncClient),
            patch.object(server, "_log_jev_failure"),
        ):
            result = await server.jev_ask("state", {"ok": {"type": "noul", "instructions": "Proceed?"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "bad_response")
        self.assertEqual(_AsyncClient.calls, 1)


class JudgeContractTests(unittest.TestCase):
    def test_judge_exposes_error_code_status_and_attempts(self):
        async def fake_jev(_state, _questions):
            return {
                "ok": False,
                "answers": {},
                "err": "HTTP 429: busy",
                "code": "rate_limit",
                "status": 429,
                "attempts": 2,
                "need_topup": False,
                "ms": 501,
            }

        with patch.object(server, "jev_ask", fake_jev):
            with TestClient(server.app, base_url="http://localhost") as client:
                response = client.post("/api/judge", json={"state": "hello", "kind": "judge"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "rate_limit")
        self.assertEqual(payload["status"], 429)
        self.assertEqual(payload["attempts"], 2)
        self.assertFalse(payload["need_topup"])


if __name__ == "__main__":
    unittest.main()
