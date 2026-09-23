from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import unittest
from typing import Any


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from swarm_models import BeeSpec
from swarm_runtime import (
    BEE_INPUT_PROTOCOL,
    BEE_ROLE_PROMPT_VERSION,
    BeePermissionError,
    BeeRuntime,
    BeeUpstreamError,
)


def sse_chunk(
    *,
    reasoning: str | None = None,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    payload: dict[str, Any] = {
        "choices": [{"delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return "data: " + json.dumps(payload, ensure_ascii=False)


class FakeResponse:
    def __init__(
        self,
        lines: list[str],
        *,
        status_code: int = 200,
        pause_after_line: int | None = None,
        pause_event: asyncio.Event | None = None,
    ) -> None:
        self.status_code = status_code
        self.lines = lines
        self.pause_after_line = pause_after_line
        self.pause_event = pause_event

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for index, line in enumerate(self.lines):
            if self.pause_after_line == index and self.pause_event is not None:
                await self.pause_event.wait()
            await asyncio.sleep(0)
            yield line


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra HTTP request")
        return self.responses.pop(0)


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up one value.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret",
            "description": "A provider-visible but bee-disallowed tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class BeeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def make_spec(self, *, allowed_tools: tuple[str, ...] = ()) -> BeeSpec:
        return BeeSpec(
            bee_id="investigator",
            role_prompt="Collect evidence and distinguish observation from inference.",
            allowed_tools=allowed_tools,
            version="7",
        )

    def make_runtime(
        self,
        client: FakeClient,
        *,
        tool_specs_provider=None,
        tool_executor=None,
        system_prompt: str = "SYSTEM-PREFIX\nkeep-this-byte-stable",
    ) -> BeeRuntime:
        return BeeRuntime(
            llm_base="https://llm.example/v1/",
            key="test-key",
            model="test-model",
            system_prompt=system_prompt,
            tool_specs_provider=tool_specs_provider,
            tool_executor=tool_executor,
            http_client_factory=lambda: client,
        )

    async def test_pure_text_stream_aggregates_events_usage_and_cache(self):
        client = FakeClient(
            [
                FakeResponse(
                    [
                        sse_chunk(reasoning="先核验。"),
                        sse_chunk(content="结论"),
                        sse_chunk(
                            content="完整。",
                            finish_reason="stop",
                            usage={
                                "prompt_tokens": 100,
                                "completion_tokens": 12,
                                "total_tokens": 112,
                                "prompt_tokens_details": {"cached_tokens": 90},
                                "completion_tokens_details": {"reasoning_tokens": 4},
                            },
                        ),
                        "data: [DONE]",
                    ]
                )
            ]
        )
        events: list[dict[str, Any]] = []
        runtime = self.make_runtime(client)

        result = await runtime.run(
            self.make_spec(),
            "验证结论",
            {"task_id": "tsk_1", "constraints": ["引用证据"]},
            [{"capsule_id": "cap_1", "facts": [{"value": "A"}]}],
            events.append,
            asyncio.Event(),
        )

        self.assertEqual(result["text"], "结论完整。")
        self.assertEqual(result["reasoning"], "先核验。")
        self.assertEqual(result["tool_usage"], [])
        self.assertEqual(result["usage"]["prompt_tokens"], 100)
        self.assertEqual(result["usage"]["completion_tokens"], 12)
        self.assertEqual(result["usage"]["reasoning_tokens"], 4)
        self.assertEqual(result["cache"]["cached"], 90)
        self.assertAlmostEqual(result["cache"]["rate"], 0.9)
        self.assertEqual(
            [event["type"] for event in events],
            ["bee.reasoning", "bee.delta", "bee.delta"],
        )
        self.assertEqual(
            [event["payload"]["text"] for event in events],
            ["先核验。", "结论", "完整。"],
        )

    async def test_one_tool_round_executes_and_commits_complete_history(self):
        first = FakeResponse(
            [
                sse_chunk(
                    reasoning="需要查证。",
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{\"query\":"},
                        }
                    ],
                ),
                sse_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "function": {"arguments": "\"alpha\"}"},
                        }
                    ],
                    finish_reason="tool_calls",
                    usage={
                        "prompt_tokens": 80,
                        "completion_tokens": 5,
                        "total_tokens": 85,
                        "prompt_tokens_details": {"cached_tokens": 70},
                    },
                ),
                "data: [DONE]",
            ]
        )
        second = FakeResponse(
            [
                sse_chunk(
                    content="工具证据已确认。",
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": 120,
                        "completion_tokens": 9,
                        "total_tokens": 129,
                        "prompt_tokens_details": {"cached_tokens": 110},
                    },
                ),
                "data: [DONE]",
            ]
        )
        client = FakeClient([first, second])
        executed: list[tuple[str, dict[str, Any]]] = []

        def execute(name: str, args: dict[str, Any]):
            executed.append((name, args))
            return {"value": 42, "query": args["query"]}

        events: list[dict[str, Any]] = []
        runtime = self.make_runtime(
            client,
            tool_specs_provider=lambda: TOOL_SPECS,
            tool_executor=execute,
        )

        result = await runtime.run(
            self.make_spec(allowed_tools=("lookup",)),
            "查证 alpha",
            {"task_id": "tsk_tool"},
            [],
            events.append,
            asyncio.Event(),
        )

        self.assertEqual(result["text"], "工具证据已确认。")
        self.assertEqual(executed, [("lookup", {"query": "alpha"})])
        self.assertEqual(result["usage"]["prompt_tokens"], 200)
        self.assertEqual(result["usage"]["cached_tokens"], 180)
        self.assertEqual(result["usage"]["tool_rounds"], 1)
        self.assertEqual(result["usage"]["tool_calls"], 1)
        self.assertEqual(result["tool_usage"][0]["name"], "lookup")
        self.assertFalse(result["tool_usage"][0]["denied"])
        self.assertEqual(
            [event["type"] for event in events],
            ["bee.reasoning", "bee.tool_call", "bee.tool_result", "bee.delta"],
        )
        tool_result = next(
            event["payload"] for event in events if event["type"] == "bee.tool_result"
        )
        self.assertIn('"value":42', tool_result["result"])

        first_tools = client.requests[0]["json"]["tools"]
        self.assertEqual([tool["function"]["name"] for tool in first_tools], ["lookup"])
        second_messages = client.requests[1]["json"]["messages"]
        assistant = second_messages[-2]
        tool_message = second_messages[-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")

    async def test_tool_not_in_allowed_set_is_never_exposed_or_executed(self):
        client = FakeClient(
            [
                FakeResponse(
                    [
                        sse_chunk(content="无需工具。", finish_reason="stop"),
                        "data: [DONE]",
                    ]
                )
            ]
        )
        executed: list[str] = []
        runtime = self.make_runtime(
            client,
            tool_specs_provider=lambda: TOOL_SPECS,
            tool_executor=lambda name, args: executed.append(name),
        )

        result = await runtime.run(
            self.make_spec(allowed_tools=("lookup",)),
            "完成任务",
            {},
            [],
            None,
            asyncio.Event(),
        )

        exposed = client.requests[0]["json"]["tools"]
        self.assertEqual([tool["function"]["name"] for tool in exposed], ["lookup"])
        self.assertNotIn("secret", json.dumps(exposed))
        self.assertEqual(executed, [])
        self.assertEqual(result["text"], "无需工具。")

    async def test_model_request_for_disallowed_tool_aborts_without_execution(self):
        client = FakeClient(
            [
                FakeResponse(
                    [
                        sse_chunk(
                            tool_calls=[
                                {
                                    "index": 0,
                                    "id": "call_secret",
                                    "function": {"name": "secret", "arguments": "{}"},
                                }
                            ],
                            finish_reason="tool_calls",
                        ),
                        "data: [DONE]",
                    ]
                )
            ]
        )
        executed: list[str] = []
        events: list[dict[str, Any]] = []
        runtime = self.make_runtime(
            client,
            tool_specs_provider=lambda: TOOL_SPECS,
            tool_executor=lambda name, args: executed.append(name),
        )

        with self.assertRaises(BeePermissionError):
            await runtime.run(
                self.make_spec(allowed_tools=("lookup",)),
                "不要越权",
                {},
                [],
                events.append,
                asyncio.Event(),
            )

        self.assertEqual(executed, [])
        self.assertEqual(
            [event["type"] for event in events],
            ["bee.tool_call", "bee.tool_result"],
        )
        self.assertTrue(events[-1]["payload"]["denied"])

    async def test_cancellation_interrupts_blocked_stream_and_returns_no_result(self):
        cancel_event = asyncio.Event()
        never_release = asyncio.Event()
        client = FakeClient(
            [
                FakeResponse(
                    [sse_chunk(content="partial"), "data: [DONE]"],
                    pause_after_line=0,
                    pause_event=never_release,
                )
            ]
        )
        runtime = self.make_runtime(client)

        task = asyncio.create_task(
            runtime.run(
                self.make_spec(),
                "可取消任务",
                {},
                [],
                None,
                cancel_event,
            )
        )
        await asyncio.sleep(0.02)
        cancel_event.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    async def test_upstream_error_raises_and_returns_no_partial_result(self):
        client = FakeClient([FakeResponse([], status_code=503)])
        runtime = self.make_runtime(client)

        with self.assertRaisesRegex(BeeUpstreamError, "HTTP 503"):
            await runtime.run(
                self.make_spec(),
                "上游失败",
                {},
                [],
                None,
                asyncio.Event(),
            )

    async def test_stable_message_prefix_order_and_deterministic_input_json(self):
        system_prompt = "EXACT SYSTEM\n  preserve spacing  "
        responses = [
            FakeResponse([sse_chunk(content="A", finish_reason="stop"), "data: [DONE]"]),
            FakeResponse([sse_chunk(content="B", finish_reason="stop"), "data: [DONE]"]),
        ]
        client = FakeClient(responses)
        runtime = self.make_runtime(client, system_prompt=system_prompt)
        spec = self.make_spec()
        contract_a = {"z": 9, "a": {"y": 2, "x": 1}}
        contract_b = {"a": {"x": 1, "y": 2}, "z": 9}
        capsule_a = {"facts": [{"b": 2, "a": 1}], "capsule_id": "cap"}
        capsule_b = {"capsule_id": "cap", "facts": [{"a": 1, "b": 2}]}

        await runtime.run(
            spec,
            "same goal",
            contract_a,
            [capsule_a],
            None,
            asyncio.Event(),
        )
        await runtime.run(
            spec,
            "same goal",
            contract_b,
            [capsule_b],
            None,
            asyncio.Event(),
        )

        first_messages = client.requests[0]["json"]["messages"]
        second_messages = client.requests[1]["json"]["messages"]
        self.assertEqual(first_messages, second_messages)
        self.assertEqual(first_messages[0], {"role": "system", "content": system_prompt})
        self.assertEqual(first_messages[1]["role"], "system")
        self.assertTrue(first_messages[1]["content"].startswith(BEE_ROLE_PROMPT_VERSION))
        self.assertIn(spec.role_prompt, first_messages[1]["content"])
        self.assertEqual(first_messages[2]["role"], "user")
        parsed_input = json.loads(first_messages[2]["content"])
        self.assertEqual(parsed_input["protocol"], BEE_INPUT_PROTOCOL)
        self.assertEqual(parsed_input["goal"], "same goal")
        self.assertEqual(parsed_input["contract"], contract_b)
        self.assertEqual(parsed_input["input_capsules"], [capsule_b])


    def test_reused_stream_index_keeps_later_argument_fragments_on_new_call(self):
        accumulator: list[dict[str, str]] = []
        BeeRuntime._merge_tool_call_delta(accumulator, {
            "index": 0, "id": "call_a", "function": {"name": "first", "arguments": '{"a":'},
        })
        BeeRuntime._merge_tool_call_delta(accumulator, {
            "index": 0, "id": "call_b", "function": {"name": "second", "arguments": '{"b":'},
        })
        BeeRuntime._merge_tool_call_delta(accumulator, {
            "index": 0, "function": {"arguments": "2}"},
        })
        calls = BeeRuntime._finalize_tool_calls(accumulator, 1)
        self.assertEqual(calls[0]["arguments"], '{"a":')
        self.assertEqual(calls[1]["arguments"], '{"b":2}')


if __name__ == "__main__":
    unittest.main()
