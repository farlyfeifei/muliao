"""现有权限模块的线性化适配器。"""
from __future__ import annotations

from typing import Callable, TypeVar


T = TypeVar("T")


class ExistingVoicePermission:
    scope = "voice_control"

    def allowed(self) -> bool:
        import permissions

        return permissions.is_granted(self.scope)

    def run_if_allowed(self, callback: Callable[[], T]) -> tuple[bool, T | None]:
        """在 permissions 的同一把锁内检查并启动副作用。

        这样 ``set_scope(False)`` 一旦返回，之后不可能再启动新的 Jev、动作或 TTS；
        已经在线性化点之前开始的回调会先完成，撤权随后提交。
        """

        import permissions

        with permissions._lock:  # 复用共享模块的 RLock；is_granted/_load 可重入。
            if not permissions.is_granted(self.scope):
                return False, None
            return True, callback()
