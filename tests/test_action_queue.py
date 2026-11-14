"""ActionQueue 与快照裁剪的单元测试（不需要插件 ctx / SDK）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_MODULE_NAME = "kongxiang_queue_under_test"


def _load_queue_module():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PLUGIN_ROOT / "action_queue.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


queue = _load_queue_module()
ActionQueue = queue.ActionQueue


class FakeClock:
    """可注入时钟：不 patch 进程级 time，符合 tests/clock_guard.py 的要求。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_queue(generation: str = "testgen") -> tuple[ActionQueue, FakeClock]:
    clock = FakeClock()
    return ActionQueue(generation=generation, clock=clock), clock


def test_poll_limit_is_clamped() -> None:
    q, _ = make_queue()
    for _ in range(20):
        assert q.enqueue("buyTrap")["queued"] is True

    assert len(q.poll(0)["actions"]) == 1
    assert len(q.poll(999)["actions"]) == 16
    assert len(q.poll(-5)["actions"]) == 1
    assert q.pending == 20


def test_poll_returns_action_contract_fields() -> None:
    q, _ = make_queue()
    queued = q.enqueue("buyRepeatable", action_id="particleBoost", max_flag=True, reason="test")
    assert queued["queued"] is True
    action_id = queued["action_id"]
    assert action_id.startswith("testgen:a")

    polled = q.poll(8)
    assert polled["pending"] == 1
    assert polled["generation"] == "testgen"
    action = polled["actions"][0]
    assert set(action) == {
        "action_id",
        "type",
        "payload",
        "reason",
        "attempts",
        "lease_until",
    }
    assert action["action_id"] == action_id
    assert action["type"] == "buyRepeatable"
    assert action["payload"] == {"id": "particleBoost", "max": True}
    assert action["reason"] == "test"
    assert action["attempts"] == 1
    assert action["lease_until"] == polled["now"] + queue.LEASE_SECONDS


def test_k_payload_for_dark_matter_and_ep_mult() -> None:
    q, _ = make_queue()
    q.enqueue("buyDarkMatter", action_id="dm", max_flag=True)
    q.enqueue("buyDarkMatter", action_id="dm", max_flag=False)
    q.enqueue("buyEpMult", action_id="ep", max_flag=True)
    q.enqueue("buyEpMult", action_id="ep", max_flag=False)
    q.enqueue("buyTrap", max_flag=True)

    actions = q.poll(16)["actions"]
    assert actions[0]["payload"]["k"] is None
    assert actions[1]["payload"]["k"] == 1
    assert actions[2]["payload"]["k"] is None
    assert actions[3]["payload"]["k"] == 1
    # 其它动作不补 k
    assert "k" not in actions[4]["payload"]


def test_setauto_on_payload_and_other_actions_ignore_on() -> None:
    q, _ = make_queue()
    q.enqueue("setAuto", action_id="autoPhase", on=False)
    q.enqueue("setAuto", action_id="autoVacuum")
    q.enqueue("buyTrap", on=False)

    actions = q.poll(16)["actions"]
    assert actions[0]["payload"]["on"] is False
    assert actions[1]["payload"]["on"] is True
    assert "on" not in actions[2]["payload"]


def test_id_and_reason_are_truncated_on_enqueue() -> None:
    q, _ = make_queue()
    q.enqueue("buyRepeatable", action_id="i" * 100, reason="r" * 300)

    action = q.poll(8)["actions"][0]
    assert len(action["payload"]["id"]) == queue.ACTION_ID_MAX
    assert len(action["reason"]) == queue.REASON_MAX


def test_unknown_action_type_is_rejected() -> None:
    q, _ = make_queue()
    assert q.enqueue("rm -rf") == {"queued": False, "error": "unknown_action"}
    assert q.enqueue("") == {"queued": False, "error": "unknown_action"}
    assert q.pending == 0


def test_reset_actions_require_confirm() -> None:
    q, _ = make_queue()
    assert q.enqueue("doBigCrunch") == {"queued": False, "error": "reset_requires_confirm"}
    assert q.enqueue("doEternity") == {"queued": False, "error": "reset_requires_confirm"}
    assert q.enqueue("doBigCrunch", confirm=True)["queued"] is True
    assert q.enqueue("doEternity", confirm=True)["queued"] is True
    # 普通动作忽略 confirm
    assert q.enqueue("buyTrap", confirm=False)["queued"] is True


def test_queue_full_keeps_existing_actions() -> None:
    q, _ = make_queue()
    for _ in range(queue.QUEUE_LIMIT):
        assert q.enqueue("buyTrap")["queued"] is True
    assert q.pending == queue.QUEUE_LIMIT
    assert q.enqueue("buyTrap") == {"queued": False, "error": "queue_full"}
    assert q.pending == queue.QUEUE_LIMIT


def test_queued_action_expires_after_ttl() -> None:
    q, clock = make_queue()
    action_id = q.enqueue("buyTrap")["action_id"]

    clock.advance(queue.QUEUE_TTL_SECONDS + 1)
    polled = q.poll(8)
    assert polled["actions"] == []
    assert polled["pending"] == 0
    results = q.recent_results(5)
    assert [(item["action_id"], item["status"]) for item in results] == [(action_id, "expired")]
    assert results[0]["reason"] == "queued_too_long"


def test_purge_expired_works_without_poll() -> None:
    q, clock = make_queue()
    assert q.enqueue("buyTrap")["queued"] is True

    clock.advance(queue.QUEUE_TTL_SECONDS + 1)
    assert q.purge_expired() == 1
    assert q.pending_count() == 0
    assert q.recent_results(1)[0]["status"] == "expired"

    # pending 属性也会先 purge，页面没开时不会一直“假满”。
    q.enqueue("buyTrap")
    clock.advance(queue.QUEUE_TTL_SECONDS + 1)
    assert q.pending == 0
    assert q.result_count == 2

    # purge 的参数 now 可显式传入，便于测试/回放。
    q.enqueue("buyTrap")
    assert q.purge_expired(now=1e18) == 1
    assert q.pending_count() == 0


def test_enqueue_purges_expired_before_queue_full_check() -> None:
    q, clock = make_queue()
    for _ in range(queue.QUEUE_LIMIT):
        assert q.enqueue("buyTrap")["queued"] is True
    assert q.enqueue("buyTrap") == {"queued": False, "error": "queue_full"}

    clock.advance(queue.QUEUE_TTL_SECONDS + 1)
    # 过期动作先被清掉，队列不再“假满”。
    assert q.enqueue("buyTrap")["queued"] is True
    assert q.pending == 1
    assert q.result_count == queue.QUEUE_LIMIT


def test_late_report_updates_expired_result() -> None:
    q, clock = make_queue()
    action_id = q.enqueue("buyTrap")["action_id"]

    clock.advance(queue.QUEUE_TTL_SECONDS + 1)
    q.poll(8)  # 产生 expired 结果
    assert q.recent_results(1)[0]["status"] == "expired"

    late = q.report(action_id, "ok", "page came back")
    assert late == {"ok": True, "received": 1, "acked": False}
    assert q.result_count == 1
    item = q.recent_results(1)[0]
    assert (item["status"], item["reason"], item["late"]) == ("ok", "page came back", True)

    # 同一个 action_id 的迟到回执只更新，不重复追加。
    q.report(action_id, "rejected", "still late")
    assert q.result_count == 1
    item = q.recent_results(1)[0]
    assert (item["status"], item["reason"], item["late"]) == ("rejected", "still late", True)


def test_lease_expiry_redelivers_then_abandons() -> None:
    q, clock = make_queue()
    action_id = q.enqueue("buyTrap")["action_id"]

    first = q.poll(8)["actions"]
    assert [(item["action_id"], item["attempts"]) for item in first] == [(action_id, 1)]

    clock.advance(queue.LEASE_SECONDS + 1)
    second = q.poll(8)["actions"]
    assert [(item["action_id"], item["attempts"]) for item in second] == [(action_id, 2)]

    clock.advance(queue.LEASE_SECONDS + 1)
    third = q.poll(8)["actions"]
    assert [(item["action_id"], item["attempts"]) for item in third] == [(action_id, 3)]

    clock.advance(queue.LEASE_SECONDS + 1)
    polled = q.poll(8)
    assert polled["actions"] == []
    assert polled["pending"] == 0
    last = q.recent_results(5)[-1]
    assert (last["action_id"], last["status"], last["reason"]) == (action_id, "abandoned", "max_attempts")


def test_report_acks_known_action_once() -> None:
    q, _ = make_queue()
    action_id = q.enqueue("buyTrap")["action_id"]
    q.poll(8)

    first = q.report(action_id, "ok", "bought")
    assert first == {"ok": True, "received": 1, "acked": True}
    assert q.pending == 0
    result = q.recent_results(1)[0]
    assert (result["action_id"], result["status"], result["reason"]) == (action_id, "ok", "bought")

    # 页面重投回执不应该重复追加结果。
    second = q.report(action_id, "ok", "bought")
    assert second == {"ok": True, "received": 1, "acked": False}
    assert q.result_count == 1


def test_report_unknown_action_is_recorded_late() -> None:
    q, _ = make_queue()
    assert q.report("nope:a1", "ok") == {"ok": True, "received": 1, "acked": False}
    assert q.result_count == 1
    item = q.recent_results(1)[0]
    assert (item["action_id"], item["status"], item["late"]) == ("nope:a1", "ok", True)

    # 同一个 id 再回报一次：更新原结果，不重复追加。
    assert q.report("nope:a1", "rejected", "changed") == {
        "ok": True,
        "received": 1,
        "acked": False,
    }
    item = q.recent_results(1)[0]
    assert (item["status"], item["reason"], item["late"]) == ("rejected", "changed", True)


def test_recent_results_returns_last_five() -> None:
    q, _ = make_queue()
    action_ids = []
    for index in range(7):
        action_id = q.enqueue("buyTrap", reason=str(index))["action_id"]
        action_ids.append(action_id)
        q.poll(16)
        assert q.report(action_id, "ok")["acked"] is True

    recent = q.recent_results(5)
    assert len(recent) == 5
    assert [item["action_id"] for item in recent] == action_ids[-5:]
    assert set(recent[0]) == {"action_id", "status", "reason", "at"}


def test_results_are_capped() -> None:
    q, _ = make_queue()
    for _ in range(queue.RESULTS_LIMIT + 6):
        action_id = q.enqueue("buyTrap")["action_id"]
        q.poll(16)
        assert q.report(action_id, "ok")["acked"] is True
    assert q.result_count == queue.RESULTS_LIMIT


def test_snapshot_log_is_trimmed_and_sanitized() -> None:
    raw = {
        "schema": 1,
        "t": 1000,
        "resources": {"matter": 1},
        "progress": {"bigCrunchCount": 0},
        "log": [
            {"t": index, "kind": "system", "text": f"line\n{index}\x00" + "x" * 400}
            for index in range(40)
        ],
    }
    stored, error = queue.sanitize_snapshot(raw)
    assert error is None
    assert len(stored["log"]) == queue.SNAPSHOT_MAX_LOG
    for entry in stored["log"]:
        assert len(entry["text"]) <= queue.SNAPSHOT_LOG_TEXT_MAX
        assert not any(ord(char) < 0x20 for char in entry["text"])
    # 不修改调用方对象
    assert len(raw["log"]) == 40


def test_snapshot_drops_optional_fields_before_giving_up() -> None:
    raw = {
        "schema": 1,
        "t": 1000,
        "resources": {"matter": 1},
        "progress": {"bigCrunchCount": 0},
        "log": [],
        "rates": "x" * (queue.SNAPSHOT_MAX_BYTES + 100),
    }
    stored, error = queue.sanitize_snapshot(raw)
    assert error is None
    assert "rates" not in stored


def test_snapshot_too_large_returns_minimal_summary() -> None:
    raw = {"schema": 1, "t": 1000, "resources": {"blob": "x" * 20000}}
    stored, error = queue.sanitize_snapshot(raw)
    assert error == "snapshot_too_large"
    assert set(stored) == {"schema", "t", "error"}
    assert stored["schema"] == 1
    assert stored["error"] == "snapshot_too_large"
    assert isinstance(stored["t"], float)


def test_non_dict_snapshot_is_empty() -> None:
    assert queue.sanitize_snapshot(None) == ({}, None)
    assert queue.sanitize_snapshot("oops") == ({}, None)
    assert queue.sanitize_snapshot([]) == ({}, None)
