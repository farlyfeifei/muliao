"""现有权限模块的只读适配器。"""
from __future__ import annotations


class ExistingVoicePermission:
    scope = "voice_control"

    def allowed(self) -> bool:
        import permissions

        return permissions.is_granted(self.scope)
