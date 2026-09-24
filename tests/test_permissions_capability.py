from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import permissions
import collectors


class ComputerControlCapabilityTests(unittest.TestCase):
    """computer_control 能力 scope 的隔离测试。

    复用 tests/test_permissions.py 的隔离手法：用 tempfile 把 permissions 的
    存储目录 / consent 路径指向临时路径，测试结束还原，绝不碰真实 %APPDATA%。
    """

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

    def test_computer_control_is_a_capability_and_in_all_scopes(self):
        self.assertIn("computer_control", permissions.CAPABILITY_SCOPES)
        self.assertIn("computer_control", permissions.ALL_SCOPES)
        # voice_control 在前，computer_control 其次，file_content（读文件正文）最后
        self.assertEqual(
            permissions.CAPABILITY_SCOPES,
            ["voice_control", "computer_control", "file_content"],
        )

    def test_computer_control_defaults_off(self):
        self.assertFalse(permissions.is_granted("computer_control"))
        status = permissions.status()
        self.assertIn("computer_control", status["scopes"])
        self.assertFalse(status["scopes"]["computer_control"])

    def test_agree_all_does_not_enable_computer_control(self):
        status = permissions.agree(agree_all=True)
        # 全选只打开七类只读数据，能力绝不随全选打开
        self.assertTrue(all(status["scopes"][s] for s in permissions.DATA_SCOPES))
        self.assertFalse(status["scopes"]["computer_control"])
        self.assertFalse(permissions.is_granted("computer_control"))

    def test_set_scope_enables_computer_control_and_shows_in_granted(self):
        permissions.set_scope("computer_control", True)
        self.assertTrue(permissions.is_granted("computer_control"))
        self.assertIn("computer_control", permissions.granted_scopes())

    def test_revoke_all_resets_computer_control(self):
        permissions.set_scope("computer_control", True)
        permissions.revoke_all()
        self.assertFalse(permissions.is_granted("computer_control"))
        self.assertNotIn("computer_control", permissions.granted_scopes())

    def test_purge_all_resets_computer_control(self):
        permissions.set_scope("computer_control", True)
        permissions.purge_all()
        self.assertFalse(permissions.is_granted("computer_control"))
        self.assertNotIn("computer_control", permissions.granted_scopes())


class CollectorsCapabilitiesTests(unittest.TestCase):
    """collectors.capabilities() 的结构与 granted 跟随性测试。"""

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

    def _find(self, items, scope_id):
        for it in items:
            if it.get("id") == scope_id:
                return it
        return None

    def test_capabilities_lists_computer_control_with_full_fields(self):
        items = collectors.capabilities()
        self.assertIsInstance(items, list)
        cc = self._find(items, "computer_control")
        self.assertIsNotNone(cc)
        for field in ("name", "desc", "granted", "available", "kind"):
            self.assertIn(field, cc)
        self.assertEqual(cc["name"], "电脑控制")
        self.assertEqual(cc["kind"], "capability")
        self.assertIsInstance(cc["available"], bool)
        self.assertIsInstance(cc["granted"], bool)
        self.assertTrue(cc["desc"])  # 非空一句话说明

    def test_capabilities_also_lists_voice_control(self):
        items = collectors.capabilities()
        vc = self._find(items, "voice_control")
        self.assertIsNotNone(vc)
        self.assertEqual(vc["name"], "语音控制")
        self.assertEqual(vc["kind"], "capability")
        for field in ("desc", "granted", "available", "detail"):
            self.assertIn(field, vc)
        self.assertIsInstance(vc["available"], bool)
        # 语音子系统已实装；available 只取决于本机是否下载了本地 ASR 模型，
        # 是环境相关的，不能硬断言 True/False。真正的不变量是：不可用时必须
        # 给出可操作的缺失说明（缺哪个模型、该放哪），不能像旧版那样只写「未实装」。
        if not vc["available"]:
            self.assertIn("模型", vc["detail"])
            self.assertNotIn("未实装", str(vc["detail"]) + str(vc["note"]))

    def test_capabilities_lists_file_content_with_secret_guard_note(self):
        """file_content 是敏感度最高的能力：必须默认关、且明确告知密钥文件被拦。"""
        items = collectors.capabilities()
        fc = self._find(items, "file_content")
        self.assertIsNotNone(fc)
        self.assertEqual(fc["kind"], "capability")
        self.assertTrue(fc["sensitive"])
        self.assertIsInstance(fc["available"], bool)
        self.assertIsInstance(fc["granted"], bool)
        # 说明里必须点出「密钥/凭据文件拒读」，这是该能力的安全承诺。
        self.assertIn("拒读", str(fc["detail"]) + str(fc["note"]))
        # 默认关：全新隔离环境下绝不该是 granted
        self.assertFalse(fc["granted"])

    def test_capabilities_granted_follows_set_scope(self):
        self.assertFalse(self._find(collectors.capabilities(), "computer_control")["granted"])
        permissions.set_scope("computer_control", True)
        self.assertTrue(self._find(collectors.capabilities(), "computer_control")["granted"])
        permissions.set_scope("computer_control", False)
        self.assertFalse(self._find(collectors.capabilities(), "computer_control")["granted"])


if __name__ == "__main__":
    unittest.main()
