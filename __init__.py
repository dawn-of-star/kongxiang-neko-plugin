"""空想增量（Kongxiang Incremental）插件壳。

职责（MVP）：
  · 用 ``register_static_ui("static")`` 把游戏页面挂到 ``/plugin/kongxiang_incremental/ui/``；
  · 页面每几秒把 ``nekoSnapshotForHost()`` 快照 POST 进来（``sync_state``）；
  · 猫咪通过 ``@llm_tool`` 读快照 / 排动作（``kongxiang_state`` / ``kongxiang_action``）；
  · 页面轮询 ``poll_actions`` 执行动作，再 ``report_result`` 回报执回执。

⚠️ 这不是 minigame SDK：插件页面里没有 ``NekoMiniGame``，所以游戏页要用
``src/neko/plugin-bridge.js`` 走 ``POST /runs`` + ``/runs/{id}/export`` 这条链路。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from plugin.sdk.plugin import (
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)


@neko_plugin
class KongxiangPlugin(NekoPluginBase):
    """空想增量的插件后端（只做状态与动作中转，不跑游戏逻辑）。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._snapshot: dict[str, Any] = {}
        self._snapshot_at = 0.0
        self._actions: deque[dict[str, Any]] = deque(maxlen=64)
        self._seq = 0
        self._results: deque[dict[str, Any]] = deque(maxlen=64)

    def _enqueue_action(
        self,
        action_type: str,
        action_id: str = "",
        max_flag: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        """把动作放进队列；页面轮询到之后才真正执行。"""
        self._seq += 1
        action = {
            "action_id": f"a{self._seq}",
            "type": action_type,
            "payload": {
                "id": str(action_id or ""),
                "max": bool(max_flag),
            },
            "reason": str(reason or ""),
            "queued_at": time.time(),
        }
        self._actions.append(action)
        return action

    @lifecycle(id="startup")
    async def startup(self, **_):
        ok = False
        try:
            ok = self.register_static_ui("static")
        except Exception as exc:  # 静态页失败不应阻止插件启动
            self.logger.warning("静态 UI 注册失败: {}", exc)
        self.logger.info("空想增量插件启动，static_ui={}", ok)
        return Ok({"status": "ready", "static_ui": ok})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._actions.clear()
        self.logger.info("空想增量插件停止")
        return Ok({"status": "stopped"})

    # ── 页面 → 插件 ──────────────────────────────────────────
    @plugin_entry(
        id="sync_state",
        name="同步游戏快照",
        description="游戏页把当前状态快照交给插件（页面自动调用）。",
    )
    async def sync_state(self, snapshot: dict[str, Any] | None = None, **_):
        self._snapshot = snapshot if isinstance(snapshot, dict) else {}
        self._snapshot_at = time.time()
        return Ok({"ok": True, "at": self._snapshot_at})

    @plugin_entry(
        id="poll_actions",
        name="领取待执行动作",
        description="游戏页轮询这里拿猫咪排队的动作（页面自动调用）。",
    )
    async def poll_actions(self, limit: int = 8, **_):
        max_items = max(1, min(16, int(limit or 8)))
        items: list[dict[str, Any]] = []
        while self._actions and len(items) < max_items:
            items.append(self._actions.popleft())
        return Ok({"actions": items, "pending": len(self._actions)})

    @plugin_entry(
        id="queue_action",
        name="排一个动作",
        description="手动/调试用：把动作放进队列，等游戏页轮询执行。",
    )
    async def queue_action(
        self,
        type: str,
        id: str = "",
        max: bool = False,
        reason: str = "",
        **_,
    ):
        action_type = str(type or "").strip()
        if not action_type:
            return Ok({"queued": False, "error": "type is required"})
        action = self._enqueue_action(action_type, id, max, reason)
        return Ok(
            {
                "queued": True,
                "action_id": action["action_id"],
                "pending": len(self._actions),
                "note": "页面轮询到之后才会真正执行",
            }
        )

    @plugin_entry(
        id="report_result",
        name="回报动作结果",
        description="游戏页执行完动作后回执（页面自动调用）。",
    )
    async def report_result(
        self,
        action_id: str = "",
        status: str = "",
        reason: str = "",
        **_,
    ):
        self._results.append(
            {
                "action_id": str(action_id or ""),
                "status": str(status or ""),
                "reason": str(reason or ""),
                "at": time.time(),
            }
        )
        return Ok({"ok": True, "received": len(self._results)})

    @plugin_entry(
        id="status",
        name="插件状态",
        description="给插件面板/调试看的摘要。",
    )
    async def status(self, **_):
        return Ok(
            {
                "ready": bool(self._snapshot_at),
                "snapshot_age_seconds": (
                    round(time.time() - self._snapshot_at, 1) if self._snapshot_at else None
                ),
                "pending_actions": len(self._actions),
                "results": len(self._results),
            }
        )

    # ── 猫咪的工具 ────────────────────────────────────────────
    @llm_tool(
        name="kongxiang_state",
        description="读取《空想增量》当前游戏状态快照（资源、进度、最近日志）。",
        parameters={"type": "object", "properties": {}},
    )
    async def kongxiang_state(self, **_):
        if not self._snapshot_at:
            return {"ready": False, "message": "游戏页还没有上报快照（可能没打开插件面板）"}
        return {
            "ready": True,
            "age_seconds": round(time.time() - self._snapshot_at, 1),
            "snapshot": self._snapshot,
        }

    @llm_tool(
        name="kongxiang_action",
        description=(
            "给《空想增量》排一个操作，由游戏页执行。"
            "type 可用：buyRepeatable（id: particleBoost=计数频率 / matterBoost=物质速率 / "
            "entropyCoeff=熵凝聚；max=true 一次买满）/ buyTrap / buyVoidUpgrade / buyDeUpgrade / "
            "buyInfinityUpgrade / buyIpDoubleMax / buyEpMult / buyZpeEngineLevel / "
            "buyDarkMatter / doBigCrunch / doEternity。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "动作类型"},
                "id": {"type": "string", "description": "升级 id（可空）"},
                "max": {"type": "boolean", "description": "是否一次买满"},
                "reason": {"type": "string", "description": "给玩家看的一句理由"},
            },
            "required": ["type"],
        },
    )
    async def kongxiang_action(
        self,
        type: str,
        id: str = "",
        max: bool = False,
        reason: str = "",
        **_,
    ):
        action_type = str(type or "").strip()
        if not action_type:
            return {"queued": False, "error": "type is required"}
        action = self._enqueue_action(action_type, id, max, reason)
        return {
            "queued": True,
            "action_id": action["action_id"],
            "pending": len(self._actions),
            "note": "页面轮询到之后才会真正执行",
        }
