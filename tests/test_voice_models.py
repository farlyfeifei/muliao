from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.models import (
    ModelManifestError,
    load_model_inventory,
    plan_model_download,
    validate_model_assets,
    warmup_sensevoice,
)


class ModelManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.assets = self.base / "assets"
        self.repo.mkdir()
        self.assets.mkdir()
        self.manifest = self.repo / "voice-models.json"

    def write_manifest(
        self,
        *,
        files: list[dict[str, object]],
        default_path: Path | str | None = None,
        schema_version: int = 1,
        extra_model: dict[str, object] | None = None,
    ) -> None:
        model: dict[str, object] = {
            "purpose": "test model",
            "runtime": "test-runtime",
            "env": "MULIAO_TEST_MODEL_DIR",
            "default_path": str(default_path or self.assets),
            "files": files,
            "source": "https://example.invalid/reviewed-model",
        }
        if extra_model:
            model.update(extra_model)
        self.manifest.write_text(
            json.dumps(
                {"schema_version": schema_version, "models": {"test": model}}
            ),
            encoding="utf-8",
        )

    def load(self, *, env: dict[str, str] | None = None):
        return load_model_inventory(
            self.manifest,
            repository_root=self.repo,
            env={} if env is None else env,
        )

    def test_valid_manifest_and_files(self):
        payload = b"local-model"
        (self.assets / "model.onnx").write_bytes(payload)
        self.write_manifest(
            files=[
                {
                    "name": "model.onnx",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "purpose": "test graph",
                }
            ]
        )

        inventory = self.load()
        report = validate_model_assets(inventory)

        self.assertTrue(report.ok)
        self.assertEqual(report.checked_files, 1)
        self.assertEqual(inventory.require("test").root, self.assets)
        self.assertEqual(inventory.require("test").files[0].purpose, "test graph")

    def test_rejects_unknown_schema_version(self):
        self.write_manifest(files=[{"name": "model.onnx"}], schema_version=2)
        with self.assertRaisesRegex(ModelManifestError, "schema_version"):
            self.load()

    def test_reports_missing_file(self):
        self.write_manifest(files=[{"name": "missing.onnx", "size_bytes": 3}])

        report = validate_model_assets(self.load())

        self.assertFalse(report.ok)
        self.assertEqual(report.issues[0].code, "missing")
        self.assertIn(str(self.assets / "missing.onnx"), report.issues[0].message)

    def test_reports_size_mismatch_without_hashing(self):
        (self.assets / "model.onnx").write_bytes(b"abc")
        self.write_manifest(
            files=[
                {
                    "name": "model.onnx",
                    "size_bytes": 99,
                    "sha256": hashlib.sha256(b"abc").hexdigest(),
                }
            ]
        )

        report = validate_model_assets(self.load())

        self.assertEqual([issue.code for issue in report.issues], ["size_mismatch"])

    def test_reports_sha256_mismatch(self):
        (self.assets / "model.onnx").write_bytes(b"abc")
        self.write_manifest(
            files=[
                {
                    "name": "model.onnx",
                    "size_bytes": 3,
                    "sha256": hashlib.sha256(b"different").hexdigest(),
                }
            ]
        )

        report = validate_model_assets(self.load())

        self.assertEqual([issue.code for issue in report.issues], ["sha256_mismatch"])

    def test_rejects_non_ascii_model_root(self):
        self.write_manifest(
            files=[{"name": "model.onnx"}],
            default_path=self.base / "模型",
        )
        with self.assertRaisesRegex(ModelManifestError, "ASCII-only"):
            self.load()

    def test_rejects_model_root_inside_repository(self):
        self.write_manifest(
            files=[{"name": "model.onnx"}],
            default_path=self.repo / "models",
        )
        with self.assertRaisesRegex(ModelManifestError, "outside repository"):
            self.load()

    def test_rejects_path_traversal_and_absolute_file_names(self):
        unsafe_names = (
            "../outside.onnx",
            "sub/../../outside.onnx",
            "/absolute/model.onnx",
            "C:\\models\\model.onnx",
        )
        for name in unsafe_names:
            with self.subTest(name=name):
                self.write_manifest(files=[{"name": name}])
                with self.assertRaisesRegex(ModelManifestError, "unsafe model file path"):
                    self.load()

    def test_environment_variable_overrides_default_path(self):
        override = self.base / "override"
        override.mkdir()
        (override / "model.onnx").write_bytes(b"ok")
        self.write_manifest(
            files=[{"name": "model.onnx", "size_bytes": 2}],
            default_path=self.base / "default",
        )

        inventory = self.load(env={"MULIAO_TEST_MODEL_DIR": str(override)})

        self.assertEqual(inventory.require("test").root, override)
        self.assertTrue(validate_model_assets(inventory).ok)

    def test_download_policy_returns_plan_without_network(self):
        self.write_manifest(files=[{"name": "model.onnx"}])

        with mock.patch("urllib.request.urlopen") as urlopen:
            plan = plan_model_download(self.load(), "test")

        urlopen.assert_not_called()
        self.assertFalse(plan.allowed)
        self.assertIn("automatic downloads are disabled", plan.message)
        self.assertEqual(plan.destination, self.assets)

    def test_optional_model_files_may_be_absent(self):
        self.write_manifest(
            files=[{"name": "model.onnx", "optional": True}],
            extra_model={"optional": True},
        )

        report = validate_model_assets(self.load())

        self.assertTrue(report.ok)
        self.assertEqual(report.skipped_optional_files, 1)

    def test_manifest_rejects_api_key_fields_and_values(self):
        self.write_manifest(
            files=[{"name": "model.onnx"}],
            extra_model={"api_key": "not-even-a-real-key"},
        )
        with self.assertRaisesRegex(ModelManifestError, "must not contain API keys"):
            self.load()

        fake_credential = "sk-" + ("x" * 24)
        self.write_manifest(
            files=[{"name": "model.onnx"}],
            extra_model={"metadata": fake_credential},
        )
        with self.assertRaisesRegex(ModelManifestError, "credential-like"):
            self.load()


class SenseVoiceWarmupTests(unittest.TestCase):
    def test_injected_factory_decodes_short_silence_and_reports_timings(self):
        events: list[object] = []

        class Stream:
            def accept_waveform(self, sample_rate, samples):
                events.append(("accept", sample_rate, len(samples), float(sum(samples))))

        class Recognizer:
            def create_stream(self):
                events.append("create")
                return Stream()

            def decode_stream(self, stream):
                events.append(("decode", stream.__class__.__name__))

        def factory(path: Path):
            events.append(("factory", path))
            return Recognizer()

        ticks = iter((10.0, 10.25, 10.75))
        result = warmup_sensevoice(
            "C:/Models/SenseVoice",
            recognizer_factory=factory,
            silence_samples=8,
            clock=lambda: next(ticks),
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.loaded)
        self.assertTrue(result.warmed)
        self.assertEqual(result.load_seconds, 0.25)
        self.assertEqual(result.warmup_seconds, 0.5)
        self.assertEqual(result.total_seconds, 0.75)
        self.assertEqual(events[0], ("factory", Path("C:/Models/SenseVoice")))
        self.assertEqual(events[1], "create")
        self.assertEqual(events[2], ("accept", 16_000, 8, 0.0))
        self.assertEqual(events[3][0], "decode")

    def test_factory_failure_returns_error_status(self):
        ticks = iter((1.0, 1.1))

        def broken_factory(path: Path):
            raise FileNotFoundError(path)

        result = warmup_sensevoice(
            "C:/Models/Missing",
            recognizer_factory=broken_factory,
            clock=lambda: next(ticks),
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.loaded)
        self.assertFalse(result.warmed)
        self.assertIn("FileNotFoundError", result.error or "")
        self.assertAlmostEqual(result.total_seconds, 0.1)


class CheckedInManifestTests(unittest.TestCase):
    def test_streaming_inventory_lists_required_assets_and_purposes(self):
        manifest = json.loads((ROOT / "voice-models.json").read_text(encoding="utf-8"))
        streaming = manifest["models"]["streaming_zipformer"]
        files = {item["name"]: item for item in streaming["files"]}

        self.assertEqual(
            set(files),
            {
                "tokens.txt",
                "encoder-epoch-99-avg-1.onnx",
                "decoder-epoch-99-avg-1.onnx",
                "joiner-epoch-99-avg-1.onnx",
                "bpe.model",
            },
        )
        for item in files.values():
            self.assertGreater(item["size_bytes"], 0)
            self.assertTrue(item["purpose"])
        self.assertTrue(manifest["models"]["vits_tts"]["optional"])

    def test_manifest_and_models_module_contain_no_api_credentials(self):
        patterns = (
            re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
            re.compile(r"apikey_[A-Za-z0-9_-]{20,}"),
            re.compile(r"ghp_[A-Za-z0-9]{20,}"),
            re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        )
        for path in (ROOT / "voice-models.json", ROOT / "voice" / "models.py"):
            text = path.read_text(encoding="utf-8")
            self.assertFalse(
                any(pattern.search(text) for pattern in patterns),
                f"credential-like value found in {path.name}",
            )

    def test_import_does_not_load_native_recognizer(self):
        script = (
            "import sys; import voice.models; "
            "assert 'sherpa_onnx' not in sys.modules"
        )
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
