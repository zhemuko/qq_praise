"""PraiseUserAction：给指定用户的 QQ 名片点赞。

本模块把点赞能力暴露为 LLM 可调用的 Action 工具，是否点赞、何时点赞
完全由模型自主决定（如有人 @ 求赞 / 互赞，或模型自己想宠某人时），
插件不做任何关键词匹配或自动触发。

点赞执行逻辑（自原 EventHandler 迁移）：

- 黑名单 / bot 自己跳过，冷却期内跳过
- 按好友关系选择赞数（好友 friend_times / 非好友 stranger_times），
  非好友点赞受 stranger_like_enabled 开关约束
- 分批调用 QQ 适配器的 ``send_onebot_api("send_like", ...)``，
  单批上限 20（被服务端拒时自动降级 10 重试），批间随机等待模拟真人连点
- 冷却仅在至少成功一批后记录，避免失败也锁 24 小时
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Annotated, Any, ClassVar

from src.app.plugin_system.api.adapter_api import (
    get_all_adapters,
    get_bot_info_by_platform,
)
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from .config import QQPraiseConfig

logger = get_logger("qq_praise_action")

# bot 信息缓存时长（秒），避免每次点赞都查适配器
_BOT_INFO_CACHE_TTL = 300.0
# 好友列表缓存时长（秒），好友关系变化低频
_FRIEND_LIST_CACHE_TTL = 600.0


def _get_cooldown_state(plugin: Any) -> dict[str, float]:
    """获取挂在插件实例上的点赞冷却表。

    Args:
        plugin: 插件实例，冷却表以动态属性挂在其上，跨 Action 实例共享

    Returns:
        dict[str, float]: QQ 号到上次点赞时间戳（秒）的映射
    """

    state = getattr(plugin, "_qq_praise_cooldown", None)
    if isinstance(state, dict):
        return state

    state = {}
    plugin._qq_praise_cooldown = state
    return state


class PraiseUserAction(BaseAction):
    """给指定用户的 QQ 名片点赞，是否点赞由模型自主决定。"""

    name = "praise_user"
    description = (
        "给某人的 QQ 名片点赞。当有人 @ 你求赞、求互赞、让你给他点赞时，"
        "或者你自己想宠一宠、感谢某个人时，可以调用这个工具；"
        "给不给、什么时候给由你自己决定，不想给时完全可以婉拒，不必每次都点。"
        "点赞个数由系统按好友关系自动决定，你无需指定。"
        "调用后会返回真实点赞结果（成功个数、失败原因或冷却状态），"
        "请基于真实结果用你自己的口吻回应，不要假装没点或编造结果。"
    )

    chatter_allow: list[str] = ["default_chatter"]
    associated_types = ["text"]

    # 单次 send_like 批量上限：PC 端协议实测 50 会被拒（OIDB 1001 点赞数无效），
    # 社区经验（AstrBot 插件）单次最多 20；被拒时自动降级到 10 重试。
    _LIKE_BATCH_LIMIT: ClassVar[int] = 20
    _LIKE_FALLBACK_BATCH: ClassVar[int] = 10

    def _get_config(self) -> QQPraiseConfig:
        """读取插件配置，不可用时回退默认值。"""

        if isinstance(self.plugin.config, QQPraiseConfig):
            return self.plugin.config
        return QQPraiseConfig()

    # ── 适配器与发送 ──────────────────────────────────────

    async def _get_bot_qq(self) -> str | None:
        """获取 bot 自身 QQ 号（带短缓存）。"""

        cache = getattr(self.plugin, "_qq_praise_bot_cache", None)
        now = time.time()
        if isinstance(cache, tuple) and now - cache[1] < _BOT_INFO_CACHE_TTL:
            return cache[0]

        bot_qq: str | None = None
        try:
            bot_info = await get_bot_info_by_platform("qq")
            if bot_info:
                bot_id = bot_info.get("bot_id") or bot_info.get("user_id")
                if bot_id:
                    bot_qq = str(bot_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"获取 bot 信息失败: {e}")

        self.plugin._qq_praise_bot_cache = (bot_qq, now)
        return bot_qq

    def _get_qq_adapter(self) -> Any | None:
        """从适配器管理器中定位 QQ 适配器实例。"""

        for adapter in get_all_adapters().values():
            if getattr(adapter, "platform", None) == "qq":
                return adapter
        return None

    async def _is_friend(self, adapter: Any, qq: str) -> bool:
        """判断目标是否为 bot 好友（好友列表带缓存）。

        好友列表查询失败时按好友处理（取较小赞数），避免误发大额。
        """

        cache = getattr(self.plugin, "_qq_praise_friend_cache", None)
        now = time.time()
        if isinstance(cache, tuple) and now - cache[1] < _FRIEND_LIST_CACHE_TTL:
            friend_ids: set[str] = cache[0]
            return qq in friend_ids

        friend_ids = set()
        try:
            response = await adapter.send_onebot_api("get_friend_list", {})
            if isinstance(response, dict) and response.get("status") == "ok":
                data = response.get("data")
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("user_id"):
                            friend_ids.add(str(item["user_id"]))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"获取好友列表失败，按好友处理: {e}")
            return True

        if not friend_ids:
            logger.warning("好友列表为空，按好友处理")
            return True

        self.plugin._qq_praise_friend_cache = (friend_ids, now)
        return qq in friend_ids

    async def _send_like(self, qq: str) -> dict[str, Any]:
        """冷却/黑名单检查后，按好友关系选择赞数并分批调用 send_like。

        QQ 单次点赞上限 10 个，超过会整单被拒（OIDB 1001 点赞数无效），
        因此按 20 个一批发送，被拒时自动降级为 10 重试，直到发满配置
        数量或服务端拒绝（如当日上限）。
        冷却仅在至少成功一批后记录，避免失败也锁 24 小时。

        Args:
            qq: 目标用户 QQ 号

        Returns:
            dict[str, Any]: 结果字典，status 取值：
                success（全部点满）/ partial（部分成功，服务端截断）/
                failed（一个都没点出去）/ cooldown（冷却中）/
                skipped（黑名单、bot 自己、适配器缺失、非好友开关关闭，
                附 reason 字段说明跳过原因）
        """

        config = self._get_config()

        if qq in {str(item).strip() for item in config.behavior.blacklist}:
            logger.debug(f"{qq} 在黑名单中，跳过点赞")
            return {"status": "skipped", "reason": f"{qq} 在点赞黑名单中"}

        bot_qq = await self._get_bot_qq()
        if bot_qq and qq == bot_qq:
            return {"status": "skipped", "reason": "那是 bot 自己，不能给自己点赞"}

        cooldown_state: dict[str, float] | None = None
        cooldown_seconds = config.behavior.cooldown_hours * 3600
        if cooldown_seconds > 0:
            cooldown_state = _get_cooldown_state(self.plugin)
            last = cooldown_state.get(qq, 0.0)
            now = time.time()
            if now - last < cooldown_seconds:
                remain = int((cooldown_seconds - (now - last)) / 3600) + 1
                logger.debug(f"{qq} 冷却中（约剩 {remain}h），跳过点赞")
                return {"status": "cooldown", "remain_hours": remain}

        adapter = self._get_qq_adapter()
        if adapter is None:
            logger.warning("未找到运行中的 QQ 适配器，无法点赞")
            return {"status": "skipped", "reason": "QQ 适配器未运行"}

        is_friend = await self._is_friend(adapter, qq)
        if not is_friend and not config.behavior.stranger_like_enabled:
            logger.info(f"{qq}（非好友）跳过点赞：已关闭非好友点赞开关")
            return {"status": "skipped", "reason": "对方不是好友，且未开启非好友点赞"}

        total = int(
            config.behavior.friend_times
            if is_friend
            else config.behavior.stranger_times
        )
        relation = "好友" if is_friend else "非好友"

        sent = 0
        last_failure: str | None = None
        batch_limit = self._LIKE_BATCH_LIMIT
        while sent < total:
            if sent > 0 and total > batch_limit:
                # 模拟真人连点：批间随机等待，避免一秒内打满 50 赞的机器指纹
                lo, hi = config.behavior.like_interval_seconds
                if hi > 0:
                    await asyncio.sleep(random.uniform(max(0.0, lo), hi))
            batch = min(batch_limit, total - sent)
            try:
                response = await adapter.send_onebot_api(
                    "send_like",
                    {"user_id": int(qq), "times": int(batch)},
                )
            except Exception as e:  # noqa: BLE001
                last_failure = f"调用异常: {e}"
                break

            if isinstance(response, dict) and response.get("status") == "ok":
                sent += batch
                continue

            wording = (
                str(response.get("wording", "")) if isinstance(response, dict) else ""
            )
            last_failure = wording or repr(response)[:120]

            # 大批被拒（如 times>10 的 1001）→ 降级到小批重试一次
            if batch > self._LIKE_FALLBACK_BATCH:
                batch_limit = self._LIKE_FALLBACK_BATCH
                logger.debug(f"{qq} 批量 {batch} 被拒，降级为 {batch_limit} 重试")
                continue
            break

        if sent > 0:
            if cooldown_state is not None:
                cooldown_state[qq] = time.time()
            if sent >= total:
                logger.info(f"已给 {qq}（{relation}）点赞 x{sent}")
                return {"status": "success", "sent": sent}
            logger.info(
                f"已给 {qq}（{relation}）点赞 x{sent}/{total}"
                f"（服务端截断：{last_failure}）"
            )
            return {"status": "partial", "sent": sent, "total": total}

        logger.warning(f"点赞未成功（{qq}）: {last_failure}")
        return {
            "status": "failed",
            "sent": 0,
            "total": total,
            "reason": last_failure or "服务端拒绝",
        }

    # ── LLM 工具入口 ─────────────────────────────────────

    async def execute(
        self,
        target: Annotated[
            str, "要点赞的对象，写对方的 QQ 号；不记得 QQ 号时写对方的昵称或群名片"
        ],
    ) -> tuple[bool, str]:
        """执行点赞动作，并把真实结果以可转述的文本返回给模型。

        Args:
            target: 要点赞的对象，纯数字按 QQ 号处理，
                否则按昵称/群名片在同平台用户中解析

        Returns:
            tuple[bool, str]: (是否成功点赞, 供模型转述的真实结果描述)
        """

        config = self._get_config()
        if not config.plugin.enabled:
            return False, "点赞插件未启用，无法点赞"

        keyword = str(target or "").strip()
        if not keyword:
            return False, "未指定要点赞的对象"

        # 解析目标：纯数字直接当 QQ 号，否则按昵称/群名片解析
        if keyword.isdigit():
            qq = keyword
        else:
            from src.core.utils.user_query_helper import get_user_query_helper

            qq = await get_user_query_helper().resolve_user_id(
                self.chat_stream.platform, keyword
            )
        if not qq:
            return False, f"无法定位用户 {keyword}"

        result = await self._send_like(qq)
        status = str(result.get("status", ""))
        sent = int(result.get("sent", 0))

        if status == "success":
            return True, f"已给 {qq} 点赞 x{sent}"
        if status == "partial":
            total = int(result.get("total", sent))
            return True, f"只给 {qq} 点上了 {sent}/{total} 个赞（服务端截断）"
        if status == "cooldown":
            return False, f"今天已经给 {qq} 点过赞了（冷却中）"
        if status == "failed":
            return False, f"点赞失败：{result.get('reason', '服务端拒绝')}"

        # skipped：黑名单 / bot 自己 / 适配器缺失 / 非好友开关关闭
        reason = str(result.get("reason", "条件不满足"))
        return False, f"没有给 {qq} 点赞：{reason}"
