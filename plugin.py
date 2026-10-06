"""QQ 点赞插件入口。"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .action import PraiseUserAction
from .config import QQPraiseConfig

logger = get_logger("qq_praise_plugin")


@register_plugin
class QQPraisePlugin(BasePlugin):
    """QQ 名片点赞插件：提供 praise_user 动作，由 bot 自主决定是否点赞。"""

    plugin_name: str = "qq_praise"
    plugin_description: str = (
        "QQ 名片点赞插件：bot 自主决定给 @ 求赞的人或想宠的人点赞"
    )
    plugin_version: str = "1.0.0"

    configs: ClassVar[list[type]] = [QQPraiseConfig]
    dependent_components: ClassVar[list[str]] = []

    def get_components(self) -> list[type]:
        """返回插件组件类。"""

        if (
            isinstance(self.config, QQPraiseConfig)
            and not self.config.plugin.enabled
        ):
            logger.info("qq_praise 已在配置中禁用")
            return []

        return [PraiseUserAction]
