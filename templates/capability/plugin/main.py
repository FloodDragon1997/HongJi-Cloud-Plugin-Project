"""能力插件模板（type=capability）。

能力插件是普通类（不继承 StoragePlugin），通过同一注册表管理，用于扩展平台的
非存储能力（OCR、缩略图、预览等）。业务侧以 registry.create(plugin_id, config)
获取实例并调用自定义方法，调用受生命周期守卫与权限校验约束。

使用步骤：
1. 复制本目录到 plugins/<plugin_id>/；
2. 修改 manifest.json 的 id/name/version/permissions；
3. 同步本文件的 plugin_id、display_name、required_permissions、config_schema；
4. 按平台业务约定的方法签名实现能力方法（下方 run 仅为示例）。

开发前请先与平台确认调用协议：zip 能力插件不会被业务自动发现，方法名与返回结构
需要与调用方约定一致。
"""

from __future__ import annotations

import time

from app.sdk.store_plugin import HealthStatus


class MyCapabilityPlugin:
    # 以下三项必须与 manifest.json 保持一致
    plugin_id = "my_capability"
    display_name = "我的能力插件"
    required_permissions = ("fs.read",)

    config_schema = [
        {"key": "provider", "label": "提供方 Provider", "type": "text", "placeholder": "stub",
         "full": True, "hint": "默认 stub；接入真实服务时填写提供方标识"},
    ]

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.provider_name = self.config.get("provider", "stub")

    def capabilities(self) -> dict:
        """能力声明：供插件列表/业务按能力筛选。"""
        return {"example": True, "provider": self.provider_name}

    def health_check(self) -> HealthStatus:
        """健康检查必须可重复调用且无副作用；异常由平台统一视为不健康。"""
        start = time.time()
        try:
            self._ensure_provider()
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"provider 不可用: {exc}")
        return HealthStatus(True, False, f"provider={self.provider_name} ready",
                            int((time.time() - start) * 1000))

    # --- 自定义能力方法（按平台约定实现） ---
    def run(self, payload: bytes) -> dict:
        """示例：处理一段输入并返回结构化结果。"""
        self._ensure_provider()
        return {"provider": self.provider_name, "input_bytes": len(payload)}

    def _ensure_provider(self) -> None:
        if self.provider_name not in ("stub",):
            raise ValueError(f"未知 provider: {self.provider_name}")
