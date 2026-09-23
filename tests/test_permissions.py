from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import permissions


class PermissionCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_store = permissions._STORE_DIR
        self.previous_consent = permissions._CONSENT
        permissions._STORE_DIR = self.temporary.name
        permissions._CONSENT = str(Path(self.temporary.name) / "consent.json")

    def tearDown(self) -> None:
        permissions._STORE_DIR = self.previous_store
        permissions._CONSENT = self.previous_consent
        self.temporary.cleanup()

    def test_voice_control_is_separate_and_default_off(self):
        status = permissions.status()
        self.assertEqual(len(permissions.DATA_SCOPES), 7)
        self.assertEqual(permissions.CAPABILITY_SCOPES, ["voice_control"])
        self.assertFalse(status["scopes"]["voice_control"])

    def test_agree_all_enables_only_data_scopes(self):
        status = permissions.agree(agree_all=True)
        self.assertTrue(all(status["scopes"][scope] for scope in permissions.DATA_SCOPES))
        self.assertFalse(status["scopes"]["voice_control"])

    def test_resaving_data_permissions_preserves_voice_capability(self):
        permissions.set_scope("voice_control", True)
        status = permissions.agree(scopes=["files", "system"])
        self.assertTrue(status["scopes"]["voice_control"])
        self.assertTrue(status["scopes"]["files"])
        self.assertTrue(status["scopes"]["system"])
        self.assertFalse(status["scopes"]["notifications"])

    def test_old_consent_is_migrated_with_voice_default_off(self):
        Path(permissions._CONSENT).write_text(
            json.dumps({
                "agreed": True,
                "agreed_at": 1.0,
                "version": "1",
                "scopes": {"system": True},
                "granted_at": {"system": 1.0},
            }),
            encoding="utf-8",
        )
        status = permissions.status()
        self.assertTrue(status["scopes"]["system"])
        self.assertFalse(status["scopes"]["voice_control"])


if __name__ == "__main__":
    unittest.main()
