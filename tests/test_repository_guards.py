from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SYSTEM_PROMPT_SHA256 = "50e07b3b31dc5ca87420135484c4002cbce6878ece982defb026ce36e12e7ea7"


def _static_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_string(node.left) + _static_string(node.right)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, int)
    ):
        return _static_string(node.left) * node.right.value
    raise TypeError(ast.dump(node))


def _system_prompt() -> str:
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT" for target in node.targets):
            return _static_string(node.value)
    raise AssertionError("SYSTEM_PROMPT not found")


class RepositoryGuardTests(unittest.TestCase):
    def test_system_prompt_bytes_are_frozen(self):
        digest = hashlib.sha256(_system_prompt().encode("utf-8")).hexdigest()
        self.assertEqual(digest, EXPECTED_SYSTEM_PROMPT_SHA256)

    def test_tracked_source_contains_no_embedded_api_credentials(self):
        patterns = [
            re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
            re.compile(r"apikey_[A-Za-z0-9_-]{20,}"),
            re.compile(r"ghp_[A-Za-z0-9]{20,}"),
            re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        ]
        ignored = {"build", "dist", ".git", ".venv", "__pycache__", ".pytest_cache"}
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in ignored for part in path.parts):
                continue
            if path.name == "test_repository_guards.py":
                continue
            if path.suffix.lower() not in {".py", ".js", ".html", ".css", ".md", ".json", ".bat", ".iss", ".spec"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(text) for pattern in patterns):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
