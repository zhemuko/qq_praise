"""qq_praise 插件配置。"""

from __future__ import annotations

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class QQPraiseConfig(BaseConfig):
    """QQ 点赞插件配置。"""

    name = "config"
    description = "QQ 名片点赞插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件主配置。"""

        enabled: bool = Field(default=True, description="是否启用点赞插件", label="启用插件")

    @config_section("behavior")
    class BehaviorSection(SectionBase):
        """点赞行为配置。"""

        stranger_like_enabled: bool = Field(
            default=True,
            description="尝试给非好友点赞（QQ 规则：需 SVIP 账号；普通账号会被服务端拒绝并记录日志）",
            label="非好友点赞",
        )
        friend_times: int = Field(
            default=20,
            description="给好友每次点赞的个数（SVIP 每人每天上限 20；非 SVIP 账号服务端按 10 截断/拒绝，超出自动分批）",
            label="好友赞数",
            ge=1,
            le=50,
        )
        stranger_times: int = Field(
            default=50,
            description="给非好友每次点赞的个数（需开启非好友点赞且 bot 为 SVIP 才生效）",
            label="非好友赞数",
            ge=1,
            le=50,
        )
        like_interval_seconds: tuple[float, float] = Field(
            default=(2.0, 5.0),
            description="分批点赞之间的随机间隔秒数范围（模拟真人连点，两个值相同则固定间隔，最小值设 0 关闭等待）",
            label="批间间隔（秒）",
        )
        cooldown_hours: int = Field(
            default=24,
            description="对同一个人的点赞冷却时间（小时），0 表示不冷却",
            label="冷却（小时）",
            ge=0,
            le=168,
        )
        blacklist: list[str] = Field(
            default_factory=list,
            description="不点赞的 QQ 号黑名单",
            label="黑名单",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
