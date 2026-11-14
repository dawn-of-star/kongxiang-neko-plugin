"""空想增量插件的纯队列逻辑（只依赖标准库）。

动作校验、租约（lease）重投、结果回执与快照裁剪都在这里完成，方便单测直接
构造；``__init__.py`` 只做插件 SDK 适配。不要在此 import ``plugin.sdk``。

队列约定：
  · 动作被领取后**不删除**，而是打上 ``lease_until``；页面刷新导致未回执的
    动作会在租约过期后重新可领取，页面侧用 ``action_id`` 去重；
  · ``action_id`` 带本进程 generation 前缀，插件重启后旧 id 不会撞车；
  · 排队超过 ``QUEUE_TTL_SECONDS`` 仍未执行的动作记为 expired；
  · 连续领取 ``MAX_ATTEMPTS`` 次仍没有回执的动作记为 abandoned。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import deque
from typing import Any, Callable

ACTION_TYPES: frozenset[str] = frozenset(
    {
        "buyRepeatable",
        "buyVoidUpgrade",
        "buyDeUpgrade",
        "buyTrap",
        "buyDarkMatter",
        "buyIpDoubleMax",
        "buyEpMult",
        "buyZpeEngineLevel",
        "buyInfinityUpgrade",
        "doBigCrunch",
        "doEternity",
        "setAuto",
    }
)
RESET_ACTION_TYPES: frozenset[str] = frozenset({"doBigCrunch", "doEternity"})

QUEUE_LIMIT = 64
RESULTS_LIMIT = 64
QUEUE_TTL_SECONDS = 120.0
LEASE_SECONDS = 20.0
MAX_ATTEMPTS = 3
POLL_LIMIT_MIN = 1
POLL_LIMIT_MAX = 16
DEFAULT_POLL_LIMIT = 8
ACTION_ID_MAX = 64
REASON_MAX = 200
# actions.js 里 buyDarkMatter / buyEpMult 吃 payload.k：null = 买满，1 = 买一个。
K_PAYLOAD_ACTIONS: frozenset[str] = frozenset({"buyDarkMatter", "buyEpMult"})
# 这两个状态说明动作已不在队列，页面迟到的回执可以覆盖它们。
TERMINAL_RESULT_STATUSES: frozenset[str] = frozenset({"expired", "abandoned"})

SNAPSHOT_MAX_BYTES = 16 * 1024
SNAPSHOT_MAX_LOG = 12
SNAPSHOT_LOG_TEXT_MAX = 300
# 空想增量快照里可由页面重算的可选字段，超限时优先丢弃。
SNAPSHOT_OPTIONAL_FIELDS = ("rates", "multipliers")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


def clamp_limit(limit: Any) -> int:
    """把页面传来的 limit 夹到 1..16。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_POLL_LIMIT
    return max(POLL_LIMIT_MIN, min(POLL_LIMIT_MAX, value))


def json_size(value: Any) -> int:
    """紧凑 JSON 的 UTF-8 字节数（无法序列化视为超限）。"""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return SNAPSHOT_MAX_BYTES + 1
    return len(text.encode("utf-8"))


def _clean_log_text(value: Any) -> str:
    """去掉 log 文本里的控制字符（换行等替换为空格），并截到 300 字符。"""
    text = value if isinstance(value, str) else str(value)
    return _CONTROL_CHARS.sub(" ", text)[:SNAPSHOT_LOG_TEXT_MAX]


def _trim_log(raw_log: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_log, list):
        return []
    entries: list[dict[str, Any]] = []
    # 保留最近 12 条；拷贝条目，避免改到调用方对象。
    for item in raw_log[-SNAPSHOT_MAX_LOG:]:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        if "text" in entry:
            entry["text"] = _clean_log_text(entry["text"])
        entries.append(entry)
    return entries


def sanitize_snapshot(raw: Any) -> tuple[dict[str, Any], str | None]:
    """限制页面快照大小。

    返回 ``(要存储的快照, 错误码)``。正常时错误码为 ``None``；裁剪后仍超过
    16KB 时返回最小摘要 + ``"snapshot_too_large"``，调用方据此回 ``ok: false``
    而不是抛异常。非 dict 输入按空快照处理。
    """
    if not isinstance(raw, dict):
        return {}, None

    snapshot = dict(raw)
    if "log" in snapshot:
        snapshot["log"] = _trim_log(snapshot["log"])
    if json_size(snapshot) <= SNAPSHOT_MAX_BYTES:
        return snapshot, None

    # 第二刀：丢掉可由游戏页重算的可选字段。
    for field in SNAPSHOT_OPTIONAL_FIELDS:
        snapshot.pop(field, None)
    if json_size(snapshot) <= SNAPSHOT_MAX_BYTES:
        return snapshot, None

    summary = {"schema": 1, "t": time.time(), "error": "snapshot_too_large"}
    return summary, "snapshot_too_large"


class ActionQueue:
    """动作队列 + 结果回执（无 SDK / 无 IO，时钟可注入）。"""

    def __init__(
        self,
        generation: str | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._generation = str(generation) if generation else uuid.uuid4().hex[:8]
        self._clock = clock
        self._actions: deque[dict[str, Any]] = deque()
        self._results: deque[dict[str, Any]] = deque(maxlen=RESULTS_LIMIT)
        self._seq = 0

    # ── 查询 ────────────────────────────────────────────────
    @property
    def generation(self) -> str:
        return self._generation

    @property
    def pending(self) -> int:
        return self.pending_count()

    def pending_count(self) -> int:
        """读队列长度前先清理过期动作，避免页面没开时队列“假满”。"""
        self.purge_expired()
        return len(self._actions)

    @property
    def result_count(self) -> int:
        return len(self._results)

    def recent_results(self, limit: int = 5) -> list[dict[str, Any]]:
        """最近 N 条结果（按发生顺序，旧 → 新）。"""
        if limit <= 0:
            return []
        return [dict(item) for item in list(self._results)[-limit:]]

    def purge_expired(self, now: float | None = None) -> int:
        """把排队超过 TTL 的动作记为 expired 并移除，返回移除数量。"""
        before = len(self._actions)
        self._expire_queued(self._now() if now is None else float(now))
        return before - len(self._actions)

    # ── 写入 ────────────────────────────────────────────────
    def enqueue(
        self,
        action_type: str,
        action_id: str = "",
        max_flag: bool = False,
        reason: str = "",
        confirm: bool = False,
        on: bool = True,
    ) -> dict[str, Any]:
        """校验并入队；queue_action 与 kongxiang_action 共用这一条路径。"""
        self.purge_expired()
        kind = str(action_type or "").strip()
        if kind not in ACTION_TYPES:
            return {"queued": False, "error": "unknown_action"}
        if kind in RESET_ACTION_TYPES and not bool(confirm):
            return {"queued": False, "error": "reset_requires_confirm"}
        if len(self._actions) >= QUEUE_LIMIT:
            return {"queued": False, "error": "queue_full"}

        self._seq += 1
        payload: dict[str, Any] = {
            "id": str(action_id or "")[:ACTION_ID_MAX],
            "max": bool(max_flag),
        }
        if kind in K_PAYLOAD_ACTIONS:
            payload["k"] = None if bool(max_flag) else 1
        if kind == "setAuto":
            payload["on"] = bool(on)
        record = {
            "action_id": f"{self._generation}:a{self._seq}",
            "type": kind,
            "payload": payload,
            "reason": str(reason or "")[:REASON_MAX],
            "queued_at": self._now(),
            "attempts": 0,
            "lease_until": 0.0,
        }
        self._actions.append(record)
        return {
            "queued": True,
            "action_id": record["action_id"],
            "pending": len(self._actions),
        }

    def poll(self, limit: int = DEFAULT_POLL_LIMIT) -> dict[str, Any]:
        """领取动作：先清理过期/失效租约，再按 limit 标记租约返回。"""
        now = self._now()
        self.purge_expired(now)
        self._recover_leases(now)

        max_items = clamp_limit(limit)
        actions: list[dict[str, Any]] = []
        for record in self._actions:
            if len(actions) >= max_items:
                break
            if record["lease_until"] > now:
                continue
            record["attempts"] += 1
            record["lease_until"] = now + LEASE_SECONDS
            actions.append(self._public(record))
        return {
            "actions": actions,
            "pending": len(self._actions),
            "now": now,
            "generation": self._generation,
        }

    def report(self, action_id: str, status: str = "", reason: str = "") -> dict[str, Any]:
        """页面回执：按 id 出队；迟到/重复回执更新结果而不刷屏。"""
        target = str(action_id or "")
        acked = False
        kept: deque[dict[str, Any]] = deque()
        for record in self._actions:
            if record["action_id"] == target:
                acked = True
                continue
            kept.append(record)
        if acked:
            self._actions = kept
            self._record(target, str(status or ""), str(reason or ""))
        else:
            self._record_late(target, str(status or ""), str(reason or ""))
        return {"ok": True, "received": len(self._results), "acked": acked}

    def clear(self) -> None:
        self._actions.clear()
        self._results.clear()

    # ── 内部 ────────────────────────────────────────────────
    def _now(self) -> float:
        return time.time() if self._clock is None else float(self._clock())

    def _record(self, action_id: str, status: str, reason: str) -> None:
        self._results.append(
            {
                "action_id": action_id,
                "status": status,
                "reason": reason,
                "at": self._now(),
            }
        )

    def _record_late(self, action_id: str, status: str, reason: str) -> None:
        """记录页面迟到的回执。

        动作已不在队列（expired/abandoned/插件重启）时不要静默丢弃：没有结果
        就追加一条并标 ``late=true``；已有结果就在原条目上更新（不重复追加）。
        已完成动作的重复回执保持原结果不变，避免刷屏。
        """
        now = self._now()
        for item in self._results:
            if item["action_id"] != action_id:
                continue
            if item.get("late") or item["status"] in TERMINAL_RESULT_STATUSES:
                item["status"] = status
                item["reason"] = reason
                item["at"] = now
                item["late"] = True
            return
        self._results.append(
            {
                "action_id": action_id,
                "status": status,
                "reason": reason,
                "at": now,
                "late": True,
            }
        )

    def _expire_queued(self, now: float) -> None:
        """排队超过 TTL 且没被领取的动作 → expired。"""
        kept: deque[dict[str, Any]] = deque()
        for record in self._actions:
            if record["lease_until"] <= 0.0 and now - record["queued_at"] > QUEUE_TTL_SECONDS:
                self._record(record["action_id"], "expired", "queued_too_long")
                continue
            kept.append(record)
        self._actions = kept

    def _recover_leases(self, now: float) -> None:
        """租约过期：未到最大尝试次数则重新可领取，否则 abandoned。"""
        kept: deque[dict[str, Any]] = deque()
        for record in self._actions:
            lease_until = record["lease_until"]
            if 0.0 < lease_until < now:
                if record["attempts"] >= MAX_ATTEMPTS:
                    self._record(record["action_id"], "abandoned", "max_attempts")
                    continue
                record["lease_until"] = 0.0
            kept.append(record)
        self._actions = kept

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_id": record["action_id"],
            "type": record["type"],
            "payload": dict(record["payload"]),
            "reason": record["reason"],
            "attempts": record["attempts"],
            "lease_until": record["lease_until"],
        }
