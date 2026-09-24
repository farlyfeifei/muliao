"""Agent 会话溯源 + 文件内容读取工具的测试。

覆盖三件事：
1) list_agent_sessions 能从真实 Agent 日志里拿到 cwd（真实项目路径），让模型不必猜文件位置。
2) read_file / list_folder 正常工作，且返回值永远是合法 JSON（_trim 不得切断 JSON）。
3) 安全防线：密钥/凭据路径拒读、私钥正文兜底拦截、二进制拒读、路径规范、撤权丢弃。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collectors  # noqa: E402
import machine_tools as mt  # noqa: E402
import permissions  # noqa: E402


class _IsolatedConsent(unittest.TestCase):
    """把权限存储隔离到临时目录，避免污染开发机的真实授权状态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = permissions._STORE_DIR
        self._consent = permissions._CONSENT
        permissions._STORE_DIR = self._tmp.name
        permissions._CONSENT = str(Path(self._tmp.name) / "consent.json")

    def tearDown(self) -> None:
        permissions._STORE_DIR = self._store
        permissions._CONSENT = self._consent
        self._tmp.cleanup()

    def grant(self, *scopes: str) -> None:
        for s in scopes:
            permissions.set_scope(s, True)


def _json(result: str) -> dict:
    """工具返回值必须是合法 JSON——这条本身就是断言（回归 _trim 切断 JSON 的 bug）。"""
    return json.loads(result)


class ToolRegistrationTests(_IsolatedConsent):
    def test_new_tools_registered_with_expected_scopes(self):
        self.assertEqual(mt.tool_scope("list_agent_sessions"), "ai_logs")
        self.assertEqual(mt.tool_scope("list_folder"), "file_content")
        self.assertEqual(mt.tool_scope("read_file"), "file_content")

    def test_file_tools_are_not_control_tools(self):
        """读文件是采集不是控制：不该被当成 computer_control（否则 Jev 掉线时会被误拒）。"""
        self.assertFalse(mt.is_control_tool("read_file"))
        self.assertFalse(mt.is_control_tool("list_folder"))
        self.assertFalse(mt.is_control_tool("list_agent_sessions"))

    def test_specs_are_valid_openai_shape(self):
        for name in ("list_agent_sessions", "list_folder", "read_file"):
            spec = mt.TOOL_DEFS[name]["spec"]
            self.assertEqual(spec["type"], "function")
            fn = spec["function"]
            self.assertEqual(fn["name"], name)
            self.assertTrue(fn["description"].strip())
            self.assertEqual(fn["parameters"]["type"], "object")

    def test_read_file_requires_path(self):
        self.assertEqual(mt.TOOL_DEFS["read_file"]["spec"]["function"]["parameters"]["required"],
                         ["path"])

    def test_denied_when_scope_not_granted(self):
        """未授权 file_content 时，读文件必须直接拒绝（不碰磁盘）。"""
        with mock.patch("builtins.open", side_effect=AssertionError("不该读盘")):
            r = _json(mt.execute_tool("read_file", {"path": str(ROOT / "README.md")}))
        self.assertEqual(r["error"], "unavailable_tool")


class SecretPathGuardTests(_IsolatedConsent):
    """安全防线：file_content 绝不能把密钥/凭据读进上游模型上下文。"""

    def setUp(self):
        super().setUp()
        self.grant("file_content")

    def test_blocks_project_config_json(self):
        """本项目存 API key 的 config.json 必须拒读，即使文件不存在也先按路径拦。"""
        cfg = os.path.join(os.environ.get("APPDATA", ""), "Muliao", "config.json")
        r = _json(mt.execute_tool("read_file", {"path": cfg}))
        self.assertEqual(r["error"], "path_not_allowed")

    def test_blocks_dot_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            Path(p).write_text("API_KEY=secret\n", encoding="utf-8")
            r = _json(mt.execute_tool("read_file", {"path": p}))
        self.assertEqual(r["error"], "path_not_allowed")

    def test_blocks_ssh_directory_entirely(self):
        """自定义名字的私钥（无扩展名）也必须被挡——只按文件名挡是不够的。"""
        with tempfile.TemporaryDirectory() as home:
            ssh = os.path.join(home, ".ssh")
            os.makedirs(ssh)
            key = os.path.join(ssh, "creator_city_deploy")
            Path(key).write_text(
                "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n"
                "-----END OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
            r_list = _json(mt.execute_tool("list_folder", {"path": ssh}))
            r_read = _json(mt.execute_tool("read_file", {"path": key}))
        self.assertEqual(r_list["error"], "path_not_allowed")
        self.assertEqual(r_read["error"], "path_not_allowed")

    def test_blocks_by_content_marker_even_if_path_looks_normal(self):
        """路径规则的最后一道防线：文件名/位置骗过了名单，正文特征仍要拦住。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "notes.txt")
            Path(p).write_text(
                "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n",
                encoding="utf-8")
            r = _json(mt.execute_tool("read_file", {"path": p}))
        self.assertEqual(r["error"], "path_not_allowed")
        self.assertNotIn("MIIEowIBAAKCAQEA", json.dumps(r))

    def test_blocks_key_and_pem_extensions(self):
        for name in ("server.key", "cert.pem", "bundle.p12"):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, name)
                Path(p).write_text("x", encoding="utf-8")
                r = _json(mt.execute_tool("read_file", {"path": p}))
            self.assertEqual(r["error"], "path_not_allowed", name)

    def test_looks_like_private_key_helper(self):
        self.assertTrue(mt._looks_like_private_key("-----BEGIN OPENSSH PRIVATE KEY-----\nabc"))
        self.assertTrue(mt._looks_like_private_key("ssh-ed25519 AAAAC3Nza rest"))
        self.assertFalse(mt._looks_like_private_key("def main():\n    print('hello')"))

    def test_blocked_secret_path_allows_normal_source(self):
        self.assertEqual(mt._blocked_secret_path(str(ROOT / "collectors.py")), "")


class ReadFileTests(_IsolatedConsent):
    def setUp(self):
        super().setUp()
        self.grant("file_content")

    def test_reads_text_with_line_range(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            Path(p).write_text("\n".join(f"line{i}" for i in range(1, 51)), encoding="utf-8")
            r = _json(mt.execute_tool("read_file", {"path": p, "start_line": 5, "max_lines": 3}))
        self.assertEqual(r["total_lines"], 50)
        self.assertEqual(r["returned_lines"], 3)
        self.assertIn("line5", r["content"])
        self.assertNotIn("line8", r["content"])
        self.assertTrue(r["truncated"])

    def test_result_is_always_valid_json_even_when_content_is_huge(self):
        """回归：正文超限时曾把 JSON 从中间切断，模型收到断尾 JSON 读不出内容。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "big.py")
            Path(p).write_text("\n".join("# 一行中文注释 padding padding padding"
                                        for _ in range(4000)), encoding="utf-8")
            raw = mt.execute_tool("read_file", {"path": p, "max_lines": 600})
        d = json.loads(raw)          # 必须能解析
        self.assertIn("content", d)
        self.assertGreater(len(d["content"]), 100)

    def test_rejects_binary_extension(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.png")
            Path(p).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
            r = _json(mt.execute_tool("read_file", {"path": p}))
        self.assertEqual(r["error"], "binary_file")

    def test_rejects_oversize_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "huge.txt")
            Path(p).write_bytes(b"a" * (mt._MAX_READ_BYTES + 10))
            r = _json(mt.execute_tool("read_file", {"path": p}))
        self.assertEqual(r["error"], "file_too_large")

    def test_rejects_relative_and_missing_paths(self):
        r1 = _json(mt.execute_tool("read_file", {"path": "collectors.py"}))
        self.assertEqual(r1["error"], "invalid_args")
        r2 = _json(mt.execute_tool("read_file", {"path": str(ROOT / "no_such_file_xyz.txt")}))
        self.assertEqual(r2["error"], "invalid_args")

    def test_not_a_file_error(self):
        r = _json(mt.execute_tool("read_file", {"path": str(ROOT)}))
        self.assertEqual(r["error"], "not_a_file")

    def test_result_discarded_if_scope_revoked_midway(self):
        """读取途中撤权：结果不得回灌给模型（与采集类工具同一语义）。"""
        p = str(ROOT / "README.md")
        with mock.patch.object(permissions, "is_granted",
                               side_effect=[True, False]):
            r = _json(mt.execute_tool("read_file", {"path": p}))
        self.assertEqual(r["error"], "unavailable_tool")


class ListFolderTests(_IsolatedConsent):
    def setUp(self):
        super().setUp()
        self.grant("file_content")

    def test_lists_directory_with_kinds_and_sizes(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("print(1)\n", encoding="utf-8")
            os.makedirs(os.path.join(d, "sub"))
            r = _json(mt.execute_tool("list_folder", {"path": d}))
        self.assertEqual(r["count"], 2)
        joined = " ".join(r["entries"])
        self.assertIn("[D] sub", joined)
        self.assertIn("a.py", joined)

    def test_recursive_skips_noise_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "node_modules", "pkg"))
            os.makedirs(os.path.join(d, "src"))
            Path(d, "node_modules", "pkg", "index.js").write_text("x", encoding="utf-8")
            Path(d, "src", "main.py").write_text("x", encoding="utf-8")
            r = _json(mt.execute_tool("list_folder", {"path": d, "recursive": True}))
        joined = " ".join(r["entries"])
        self.assertIn("src/main.py", joined)
        self.assertNotIn("node_modules", joined)

    def test_respects_limit(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(20):
                Path(d, f"f{i:02d}.txt").write_text("x", encoding="utf-8")
            r = _json(mt.execute_tool("list_folder", {"path": d, "limit": 5}))
        self.assertEqual(len(r["entries"]), 5)
        self.assertTrue(r["truncated"])

    def test_not_a_directory(self):
        r = _json(mt.execute_tool("list_folder", {"path": str(ROOT / "README.md")}))
        self.assertEqual(r["error"], "not_a_directory")


class AgentSessionTests(_IsolatedConsent):
    """会话溯源：从 Agent 日志拿真实 cwd，这是「不必猜文件位置」的关键。"""

    def _make_claude_log(self, root: Path, project_dir: str, cwd: str,
                         session_id: str = "abc-123", branch: str = "main") -> None:
        d = root / project_dir
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "type": "user", "sessionId": session_id, "cwd": cwd, "gitBranch": branch,
            "timestamp": "2026-09-24T10:00:00.000Z",
            "message": {"role": "user", "content": "帮我修一下这个报错"},
        }, ensure_ascii=False)
        (d / f"{session_id}.jsonl").write_text(line + "\n", encoding="utf-8")

    def test_ai_sessions_extracts_cwd_and_project(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
            logroot = Path(home) / ".claude" / "projects"
            self._make_claude_log(logroot, "D--proj", proj, session_id="s1", branch="dev")
            with mock.patch.object(collectors, "_AI_LOG_DIRS", {"claude": (".claude/projects",)}), \
                 mock.patch("os.path.expanduser", return_value=home):
                permissions.set_scope("ai_logs", True)
                r = collectors.ai_sessions(limit=10)
        self.assertTrue(r["granted"])
        self.assertEqual(len(r["items"]), 1)
        s = r["items"][0]
        self.assertEqual(s["cwd"], proj)
        self.assertEqual(s["git_branch"], "dev")
        self.assertEqual(s["tool"], "claude")
        self.assertEqual(s["first_user_message"], "帮我修一下这个报错")
        self.assertEqual(s["message_count"], 1)

    def test_tool_returns_cwd_so_model_need_not_guess(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
            logroot = Path(home) / ".claude" / "projects"
            self._make_claude_log(logroot, "D--proj", proj, session_id="s2")
            with mock.patch.object(collectors, "_AI_LOG_DIRS", {"claude": (".claude/projects",)}), \
                 mock.patch("os.path.expanduser", return_value=home):
                self.grant("ai_logs")
                r = _json(mt.execute_tool("list_agent_sessions", {"limit": 5}))
        self.assertEqual(r["count"], 1)
        sess = r["sessions"][0]
        self.assertEqual(sess["cwd"], proj)
        self.assertIn("不要猜", r["note"])

    def test_filter_narrows_sessions(self):
        with tempfile.TemporaryDirectory() as home, \
             tempfile.TemporaryDirectory() as p1, tempfile.TemporaryDirectory() as p2:
            logroot = Path(home) / ".claude" / "projects"
            self._make_claude_log(logroot, "D--alpha", p1, session_id="sa", branch="alpha-br")
            self._make_claude_log(logroot, "D--beta", p2, session_id="sb", branch="beta-br")
            with mock.patch.object(collectors, "_AI_LOG_DIRS", {"claude": (".claude/projects",)}), \
                 mock.patch("os.path.expanduser", return_value=home):
                self.grant("ai_logs")
                r = _json(mt.execute_tool("list_agent_sessions", {"filter": "alpha-br"}))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["sessions"][0]["git_branch"], "alpha-br")

    def test_denied_without_ai_logs_scope(self):
        r = _json(mt.execute_tool("list_agent_sessions", {"limit": 5}))
        self.assertEqual(r["error"], "unavailable_tool")

    def test_ai_logs_items_now_carry_cwd(self):
        """回归：ai_logs 早先丢掉了 cwd，模型只能猜路径。现在必须带上。"""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
            logroot = Path(home) / ".claude" / "projects"
            self._make_claude_log(logroot, "D--proj", proj, session_id="s3")
            with mock.patch.object(collectors, "_AI_LOG_DIRS", {"claude": (".claude/projects",)}), \
                 mock.patch("os.path.expanduser", return_value=home):
                permissions.set_scope("ai_logs", True)
                r = collectors.ai_logs(limit=5)
        self.assertTrue(r["granted"])
        self.assertTrue(r["items"])
        self.assertEqual(r["items"][0]["cwd"], proj)


class TrimNeverBreaksJsonTests(unittest.TestCase):
    """_trim 的回归：任何形状的输出都必须是合法 JSON（不得中间截断）。"""

    def test_short_string_list_stays_within_budget(self):
        s = mt._trim({"items": [f"x{i}" for i in range(5)]})
        self.assertEqual(json.loads(s)["items"][-1], "x4")
        self.assertLessEqual(len(s), 1500 * 2)

    def test_huge_single_string_is_cut_inside_value_not_json(self):
        s = mt._trim({"content": "中文" * 5000})
        d = json.loads(s)                       # 关键：可解析
        self.assertLess(len(d["content"]), 5000 * 2)
        self.assertIn("已截断", d["content"])

    def test_many_elements_degrade_to_valid_json(self):
        s = mt._trim({"entries": ["条目内容" * 10 for _ in range(400)]})
        json.loads(s)                           # 不抛即通过

    def test_nested_structure_handled(self):
        s = mt._trim({"a": {"b": [{"c": "z" * 3000}]}})
        d = json.loads(s)
        self.assertIn("已截断", d["a"]["b"][0]["c"])

    def test_custom_limit_respected(self):
        s = mt._trim({"content": "y" * 9000}, limit=8000)
        d = json.loads(s)
        self.assertGreater(len(d["content"]), 1000)


if __name__ == "__main__":
    unittest.main()
