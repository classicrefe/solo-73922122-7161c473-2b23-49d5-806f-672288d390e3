"""服务层单元测试: 增量流向维护、去重、修改/撤回、事务回滚。

运行: .venv/bin/python -m pytest tests/test_service.py -q
(无 pytest 时可直接执行: .venv/bin/python tests/test_service.py)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, services  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()
    c = db.get_conn()
    yield c
    c.close()
    monkeypatch.setattr(db, "_conn", None)


def _oracle(conn: sqlite3.Connection) -> set[tuple]:
    """从 scans 全量重算期望的流向索引 (含期望引用数)。"""
    rows = conn.execute(
        "SELECT part_id, station, ts_utc, scan_id, day_utc FROM scans"
        " ORDER BY part_id, ts_utc, scan_id"
    ).fetchall()
    by_part: dict[str, list] = {}
    for r in rows:
        by_part.setdefault(r["part_id"], []).append(r)
    expected: dict[tuple, int] = {}
    for scans in by_part.values():
        for a, b in zip(scans, scans[1:]):
            if a["station"] == b["station"]:
                continue
            key = (b["part_id"], b["day_utc"], a["station"], b["station"])
            expected[key] = expected.get(key, 0) + 1
    actual = {
        (r["part_id"], r["day_utc"], r["from_station"], r["to_station"]):
            r["ref_count"]
        for r in conn.execute("SELECT * FROM flow_index")
    }
    assert actual == expected
    return set(expected)


def _idx_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM flow_index").fetchone()[0]


def test_basic_flows_and_day_attribution(conn):
    # 同件 P1: A -> B -> B -> C, 中间同工位不产生流向
    services.add_scan("s1", "P1", "A", "2026-11-24T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-24T09:00:00Z")
    services.add_scan("s3", "P1", "B", "2026-11-24T10:00:00Z")
    services.add_scan("s4", "P1", "C", "2026-11-24T11:00:00Z")
    _oracle(conn)
    flows = {(f["from_station"], f["to_station"]): f["part_count"]
             for f in services.query_flows("2026-11-24", "2026-11-24")}
    assert flows == {("A", "B"): 1, ("B", "C"): 1}


def test_flow_attributed_to_later_scan_day(conn):
    # 23:50 在 A, 次日 00:10 到 B -> 流向归入 11-25
    services.add_scan("s1", "P1", "A", "2026-11-24T23:50:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-25T00:10:00Z")
    _oracle(conn)
    assert services.query_flows("2026-11-24", "2026-11-24") == []
    one = services.query_flows("2026-11-25", "2026-11-25")
    assert len(one) == 1 and one[0]["from_station"] == "A" \
        and one[0]["to_station"] == "B"


def test_same_flow_same_day_dedup_index_rows_do_not_grow(conn):
    # P2 当天两次走 A->B (中间经 C 返回 A: B->C, C->A 不影响 A->B 的去重判断)
    # 序列 A->B->C->A->B 产生 A->B 两次, 索引中 A->B 仅 1 行 ref_count=2
    services.add_scan("a1", "P2", "A", "2026-11-24T08:00:00Z")
    services.add_scan("b1", "P2", "B", "2026-11-24T09:00:00Z")
    services.add_scan("c1", "P2", "C", "2026-11-24T10:00:00Z")
    services.add_scan("a2", "P2", "A", "2026-11-24T11:00:00Z")
    services.add_scan("b2", "P2", "B", "2026-11-24T12:00:00Z")
    ab = conn.execute(
        "SELECT * FROM flow_index WHERE from_station='A' AND to_station='B'"
    ).fetchone()
    assert ab["ref_count"] == 2
    res = { (f["from_station"], f["to_station"]): f
            for f in services.query_flows("2026-11-24", "2026-11-24") }
    assert res[("A", "B")]["part_count"] == 1
    assert res[("A", "B")]["event_count"] == 2
    _oracle(conn)

    # 再来一次 A->B (经 C 回去), 该流向的索引行数仍不随次数增长
    services.add_scan("c2", "P2", "C", "2026-11-24T13:00:00Z")
    services.add_scan("a3", "P2", "A", "2026-11-24T14:00:00Z")
    services.add_scan("b3", "P2", "B", "2026-11-24T15:00:00Z")
    ab_rows = conn.execute(
        "SELECT COUNT(*) FROM flow_index WHERE from_station='A' AND to_station='B'"
    ).fetchone()[0]
    assert ab_rows == 1
    assert conn.execute(
        "SELECT ref_count FROM flow_index"
        " WHERE from_station='A' AND to_station='B'"
    ).fetchone()[0] == 3
    assert res[("A", "B")]["part_count"] == 1
    _oracle(conn)


def test_same_flow_on_two_days_counts_twice_in_range(conn):
    # 同一工件 P3 在两天各走一次 A->B:
    # 区间查询同件同流向只算一次 (part_count=1), 事件数仍为 2
    services.add_scan("a1", "P3", "A", "2026-11-24T08:00:00Z")
    services.add_scan("b1", "P3", "B", "2026-11-24T09:00:00Z")
    services.add_scan("a2", "P3", "A", "2026-11-25T08:00:00Z")
    services.add_scan("b2", "P3", "B", "2026-11-25T09:00:00Z")
    _oracle(conn)
    res = { (f["from_station"], f["to_station"]): f
            for f in services.query_flows("2026-11-24", "2026-11-25") }
    assert res[("A", "B")]["part_count"] == 1
    assert res[("A", "B")]["event_count"] == 2
    # 只查第一天时同样计 1 个工件
    day1 = services.query_flows("2026-11-24", "2026-11-24")
    assert day1[0]["part_count"] == 1
    # 两个不同工件同流向 -> 工件数 2
    services.add_scan("a3", "P4", "A", "2026-11-25T10:00:00Z")
    services.add_scan("b3", "P4", "B", "2026-11-25T11:00:00Z")
    _oracle(conn)
    res2 = { (f["from_station"], f["to_station"]): f
             for f in services.query_flows("2026-11-24", "2026-11-25") }
    assert res2[("A", "B")]["part_count"] == 2


def test_insert_in_middle_only_touches_neighbors(conn):
    services.add_scan("s1", "P1", "A", "2026-11-24T08:00:00Z")
    services.add_scan("s3", "P1", "C", "2026-11-24T12:00:00Z")
    assert _idx_rows(conn) == 1  # A->C
    # 中间插入 B: A->C 被替换为 A->B, B->C
    services.add_scan("s2", "P1", "B", "2026-11-24T10:00:00Z")
    _oracle(conn)
    assert _idx_rows(conn) == 2


def test_update_time_and_station(conn):
    services.add_scan("s1", "P1", "A", "2026-11-24T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-24T09:00:00Z")
    services.add_scan("s3", "P1", "C", "2026-11-24T10:00:00Z")

    # 改工位: s2 B->X
    services.update_scan("s2", station="X", ts_raw=None)
    _oracle(conn)
    flows = {(f["from_station"], f["to_station"]) for f in
             services.query_flows("2026-11-24", "2026-11-24")}
    assert flows == {("A", "X"), ("X", "C")}

    # 改时刻: s1 移到 s3 之后, 排序变为 s2(X)->s3(C)->s1(A)
    services.update_scan("s1", station=None, ts_raw="2026-11-24T11:00:00Z")
    _oracle(conn)
    ordered = [r["scan_id"] for r in services.list_scans("P1")]
    assert ordered == ["s2", "s3", "s1"]

    # 同时改时刻和工位
    services.update_scan("s1", station="D", ts_raw="2026-11-24T07:00:00Z")
    _oracle(conn)
    first = services.list_scans("P1")[0]
    assert first["scan_id"] == "s1" and first["station"] == "D"


def test_withdraw_repairs_edges(conn):
    services.add_scan("s1", "P1", "A", "2026-11-24T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-24T09:00:00Z")
    services.add_scan("s3", "P1", "C", "2026-11-24T10:00:00Z")
    services.withdraw_scan("s2")
    _oracle(conn)
    flows = {(f["from_station"], f["to_station"]) for f in
             services.query_flows("2026-11-24", "2026-11-24")}
    assert flows == {("A", "C")}

    # 撤完后再加回, 索引仍与全量重算一致
    services.add_scan("s2", "P1", "B", "2026-11-24T09:00:00Z")
    _oracle(conn)

    with pytest.raises(services.NotFoundError):
        services.withdraw_scan("missing")


def test_duplicate_scan_id_conflict_and_rollback(conn):
    services.add_scan("s1", "P1", "A", "2026-11-24T08:00:00Z")
    with pytest.raises(services.ConflictError):
        services.add_scan("s1", "P1", "B", "2026-11-24T09:00:00Z")
    # 失败保留原状: 仍只有 1 条扫码, 无流向
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
    assert _idx_rows(conn) == 0


def test_failure_rolls_back_scan_and_index(conn, monkeypatch):
    services.add_scan("s1", "P1", "A", "2026-11-24T08:00:00Z")
    services.add_scan("s3", "P1", "C", "2026-11-24T12:00:00Z")
    scans_before = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    idx_before = _idx_rows(conn)

    real_add_edge = services._add_edge
    calls = {"n": 0}

    def flaky_add_edge(c, a, b):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二条新边写入时失败
            raise RuntimeError("simulated index failure")
        return real_add_edge(c, a, b)

    monkeypatch.setattr(services, "_add_edge", flaky_add_edge)
    with pytest.raises(RuntimeError):
        services.add_scan("s2", "P1", "B", "2026-11-24T10:00:00Z")
    monkeypatch.setattr(services, "_add_edge", real_add_edge)

    # 扫码与索引一同回滚, 保留原状
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == scans_before
    assert _idx_rows(conn) == idx_before
    _oracle(conn)  # 回滚后索引仍与全量重算一致


def test_timezone_normalization_and_same_timestamp_tiebreak(conn):
    # +08:00 的 16:00 等于 UTC 08:00
    services.add_scan("s1", "P1", "A", "2026-11-24T16:00:00+08:00")
    out = services.list_scans("P1")[0]
    assert out["ts_utc"] == "2026-11-24T08:00:00.000000Z"
    assert out["day_utc"] == "2026-11-24"

    # 同一时刻按 scan_id 排序定前后
    services.add_scan("b2", "P1", "B", "2026-11-24T09:00:00Z")
    services.add_scan("b1", "P1", "C", "2026-11-24T09:00:00Z")
    _oracle(conn)
    ordered = [r["scan_id"] for r in services.list_scans("P1")]
    assert ordered == ["s1", "b1", "b2"]


def test_multiple_parts_are_independent(conn):
    services.add_scan("x1", "P1", "A", "2026-11-24T08:00:00Z")
    services.add_scan("x2", "P1", "B", "2026-11-24T09:00:00Z")
    services.add_scan("y1", "P2", "A", "2026-11-24T08:30:00Z")
    services.add_scan("y2", "P2", "C", "2026-11-24T09:30:00Z")
    _oracle(conn)
    flows = {(f["from_station"], f["to_station"]): f["part_count"]
             for f in services.query_flows("2026-11-24", "2026-11-24")}
    assert flows == {("A", "B"): 1, ("A", "C"): 1}


def test_invalid_inputs(conn):
    with pytest.raises(ValueError):
        services.add_scan("s9", "P1", "A", "not-a-timestamp")
    with pytest.raises(ValueError):
        services.query_flows("2026-11-24", "2026-13-99")
    with pytest.raises(ValueError):
        services.query_flows("2026-11-25", "2026-11-24")
    with pytest.raises(services.NotFoundError):
        services.update_scan("nope", station="X", ts_raw=None)


# ---------------------------------------------------------------- 封存

def _idx(conn) -> dict[tuple, int]:
    """当前 flow_index 全量内容: (part, day, from, to) -> ref_count。"""
    return {
        (r["part_id"], r["day_utc"], r["from_station"], r["to_station"]):
            r["ref_count"]
        for r in conn.execute("SELECT * FROM flow_index")
    }


def test_seal_deletes_details_keeps_index_and_last_station(conn):
    # P1: A(11-20) -> B(11-21) -> C(11-23); P2 全部在封存日前
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-21T08:00:00Z")
    services.add_scan("s3", "P1", "C", "2026-11-23T08:00:00Z")
    services.add_scan("t1", "P2", "X", "2026-11-19T08:00:00Z")
    services.add_scan("t2", "P2", "Y", "2026-11-21T09:00:00Z")
    idx_before = _idx(conn)

    out = services.seal("2026-11-22")
    assert out == {"seal_day": "2026-11-22", "previous_seal_day": None,
                   "deleted_scans": 4, "parts_updated": 2}
    # 明细: 只剩封存日及之后的 s3
    assert [r["scan_id"] for r in services.list_scans()] == ["s3"]
    # 索引一行不少: 历史边 A->B(11-21), X->Y(11-21) 与接续边 B->C(11-23) 全保留
    assert _idx(conn) == idx_before == {
        ("P1", "2026-11-21", "A", "B"): 1,
        ("P1", "2026-11-23", "B", "C"): 1,
        ("P2", "2026-11-21", "X", "Y"): 1,
    }
    # 每件保留最后的历史工位
    assert services.list_part_state() == [
        {"part_id": "P1", "last_station": "B", "sealed_day": "2026-11-22"},
        {"part_id": "P2", "last_station": "Y", "sealed_day": "2026-11-22"},
    ]
    assert services.get_seal()["seal_day"] == "2026-11-22"


def test_seal_idempotent_and_move_forward_keeps_stale_station(conn):
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-21T08:00:00Z")
    services.seal("2026-11-22")
    snap = (_idx(conn), services.list_part_state())

    # 重复提交同一封存日: 无新明细可封, 一切不变 (幂等)
    out = services.seal("2026-11-22")
    assert out["deleted_scans"] == 0 and out["parts_updated"] == 0
    assert (_idx(conn), services.list_part_state()) == snap

    # 封存后 P1 补登两条, 再往后推到 11-24: 只封 [11-22, 11-24) 的明细
    services.add_scan("s3", "P1", "C", "2026-11-23T08:00:00Z")
    services.add_scan("s4", "P1", "D", "2026-11-24T08:00:00Z")
    services.add_scan("u1", "P3", "M", "2026-11-23T10:00:00Z")
    out = services.seal("2026-11-24")
    assert out["previous_seal_day"] == "2026-11-22"
    assert out["deleted_scans"] == 2  # s3, u1 (s4 在封存日当天, 保留)
    # P1 的接续工位更新为被删明细最后一条的 C; P3 记为 M
    states = {r["part_id"]: r["last_station"]
              for r in services.list_part_state()}
    assert states == {"P1": "C", "P3": "M"}
    assert [r["scan_id"] for r in services.list_scans()] == ["s4"]
    # 索引: 历史边全保留, 含两次封存区间产生的边
    assert _idx(conn) == {
        ("P1", "2026-11-21", "A", "B"): 1,
        ("P1", "2026-11-23", "B", "C"): 1,
        ("P1", "2026-11-24", "C", "D"): 1,
    }


def test_seal_move_forward_keeps_station_for_parts_without_new_scans(conn):
    # P2 的历史工位在第一次封存时记录, 第二次封存无新明细, 必须沿用
    services.add_scan("t1", "P2", "X", "2026-11-19T08:00:00Z")
    services.add_scan("t2", "P2", "Y", "2026-11-20T08:00:00Z")
    services.add_scan("s1", "P1", "A", "2026-11-21T08:00:00Z")
    services.seal("2026-11-22")
    assert services.list_part_state("P2")[0]["last_station"] == "Y"

    services.add_scan("s2", "P1", "B", "2026-11-23T08:00:00Z")
    services.seal("2026-11-24")
    states = {r["part_id"]: r for r in services.list_part_state()}
    assert states["P1"]["last_station"] == "B"   # 被删明细最后一条
    assert states["P1"]["sealed_day"] == "2026-11-24"
    assert states["P2"]["last_station"] == "Y"   # 沿用上次
    assert states["P2"]["sealed_day"] == "2026-11-22"
    assert services.get_seal()["seal_day"] == "2026-11-24"


def test_seal_rejects_regression_and_bad_format(conn):
    services.add_scan("s1", "P1", "A", "2026-11-21T08:00:00Z")
    services.seal("2026-11-22")
    with pytest.raises(services.SealRegressionError):
        services.seal("2026-11-21")
    with pytest.raises(ValueError):
        services.seal("2026/11/23")
    # 回退与格式错误都不留痕迹: 封存日、接续工位不变
    assert services.get_seal()["seal_day"] == "2026-11-22"
    assert services.list_part_state() == [
        {"part_id": "P1", "last_station": "A", "sealed_day": "2026-11-22"},
    ]


def test_sealed_writes_rejected_but_day_of_seal_allowed(conn):
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.seal("2026-11-21")
    # 补登到封存日前 -> 拒绝
    with pytest.raises(services.SealedError):
        services.add_scan("s2", "P1", "B", "2026-11-20T23:59:59Z")
    # 封存日当天 -> 允许
    services.add_scan("s3", "P1", "B", "2026-11-21T00:00:00Z")
    # 改到封存日前 -> 拒绝, 且原扫码不动
    with pytest.raises(services.SealedError):
        services.update_scan("s3", None, "2026-11-20T10:00:00Z")
    assert services.list_scans("P1")[0]["ts_utc"] == "2026-11-21T00:00:00.000000Z"
    # 改工位不动日期 -> 允许; 撤回 -> 允许
    services.update_scan("s3", "C", None)
    services.withdraw_scan("s3")
    assert services.list_scans() == []
    # 唯一的未封存扫码被撤回后, 接续边也随之消失
    assert _idx(conn) == {}


def test_first_unsealed_scan_stitches_to_sealed_station(conn):
    # P1: A(11-20) -> B(11-21), 封存 11-22 后无未封存明细
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-21T08:00:00Z")
    services.seal("2026-11-22")
    assert _idx(conn) == {("P1", "2026-11-21", "A", "B"): 1}

    # 新班次首条: 接续 B -> C
    services.add_scan("s3", "P1", "C", "2026-11-23T08:00:00Z")
    assert _idx(conn) == {
        ("P1", "2026-11-21", "A", "B"): 1,
        ("P1", "2026-11-23", "B", "C"): 1,
    }
    # 补登更早的首条 D(11-22): 接续边重算为 B->D, 并新增 D->C
    services.add_scan("s4", "P1", "D", "2026-11-22T08:00:00Z")
    assert _idx(conn) == {
        ("P1", "2026-11-21", "A", "B"): 1,
        ("P1", "2026-11-22", "B", "D"): 1,
        ("P1", "2026-11-23", "D", "C"): 1,
    }
    # 首条 s4 改工位为同工位 B: 与虚拟前驱同工位, 接续边消失, 剩 B->C
    services.update_scan("s4", "B", None)
    assert _idx(conn) == {
        ("P1", "2026-11-21", "A", "B"): 1,
        ("P1", "2026-11-23", "B", "C"): 1,
    }
    # 首条 s4 改期到 s3 之后: s3 成为首条, 接续边回到 B->C(11-23)
    services.update_scan("s4", None, "2026-11-24T08:00:00Z")
    assert _idx(conn) == {
        ("P1", "2026-11-21", "A", "B"): 1,
        ("P1", "2026-11-23", "B", "C"): 1,
        ("P1", "2026-11-24", "C", "B"): 1,
    }
    # 撤回首条 s3: s4 成为首条, 接续边变为 B->B? 同工位无边; C->B 消失
    services.withdraw_scan("s3")
    assert _idx(conn) == {("P1", "2026-11-21", "A", "B"): 1}
    # 再撤回 s4: 该件无未封存明细, 只剩历史边
    services.withdraw_scan("s4")
    assert _idx(conn) == {("P1", "2026-11-21", "A", "B"): 1}


def test_seal_then_reseal_stitches_with_updated_station(conn):
    # 两次封存之间补登的明细被第二次封存删掉后, 接续工位用最新的
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.seal("2026-11-21")
    services.add_scan("s2", "P1", "B", "2026-11-22T08:00:00Z")
    services.add_scan("s3", "P1", "C", "2026-11-23T08:00:00Z")
    services.seal("2026-11-24")
    # s2/s3 被删, 接续工位更新为 C; 索引保留全部历史边
    assert services.list_part_state("P1")[0]["last_station"] == "C"
    assert _idx(conn) == {
        ("P1", "2026-11-22", "A", "B"): 1,
        ("P1", "2026-11-23", "B", "C"): 1,
    }
    # 新首条接续 C -> D
    services.add_scan("s4", "P1", "D", "2026-11-25T08:00:00Z")
    assert _idx(conn)[("P1", "2026-11-25", "C", "D")] == 1


def test_flows_across_seal_day_counts_part_once(conn):
    # 同件在封存日前后走同一流向 A->B, 区间查询只算一件
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-21T08:00:00Z")
    services.seal("2026-11-22")
    services.add_scan("s3", "P1", "A", "2026-11-23T08:00:00Z")
    services.add_scan("s4", "P1", "B", "2026-11-24T08:00:00Z")
    flows = services.query_flows("2026-11-19", "2026-11-30")
    got = {(f["from_station"], f["to_station"]):
           (f["part_count"], f["event_count"]) for f in flows}
    # A->B 两天各一次: 同件去重计 1, 事件计 2; 接续边 B->A 一次
    assert got[("A", "B")] == (1, 2)
    assert got[("B", "A")] == (1, 1)
    # 只查封存日后: 仍各一件
    flows2 = services.query_flows("2026-11-22", "2026-11-30")
    got2 = {(f["from_station"], f["to_station"]): f["part_count"]
            for f in flows2}
    assert got2 == {("B", "A"): 1, ("A", "B"): 1}


def test_seal_rolls_back_on_failure(conn):
    services.add_scan("s1", "P1", "A", "2026-11-20T08:00:00Z")
    services.add_scan("s2", "P1", "B", "2026-11-21T08:00:00Z")
    scans_before = services.list_scans()
    idx_before = _idx(conn)

    # 用触发器让 DELETE 在事务中段失败 (此时 part_state 已写入)
    conn.execute(
        """
        CREATE TRIGGER block_delete BEFORE DELETE ON scans
        BEGIN SELECT RAISE(ABORT, 'simulated seal failure'); END
        """
    )
    conn.commit()
    with pytest.raises(sqlite3.Error):
        services.seal("2026-11-22")
    conn.execute("DROP TRIGGER block_delete")
    conn.commit()

    # 明细、索引、接续工位、封存日均保留原状
    assert services.list_scans() == scans_before
    assert _idx(conn) == idx_before
    assert services.list_part_state() == []
    assert services.get_seal()["seal_day"] is None
    # 失败后仍可正常封存
    services.seal("2026-11-22")
    assert services.get_seal()["seal_day"] == "2026-11-22"
    assert services.list_part_state("P1")[0]["last_station"] == "B"


def test_seal_without_scans_and_empty_part(conn):
    # 空库封存: 正常生效
    out = services.seal("2026-11-20")
    assert out["deleted_scans"] == 0 and out["parts_updated"] == 0
    assert services.get_seal()["seal_day"] == "2026-11-20"
    # 新件直接在封存日后出现: 无虚拟前驱, 行为与未封存一致
    services.add_scan("s1", "P9", "A", "2026-11-21T08:00:00Z")
    services.add_scan("s2", "P9", "B", "2026-11-21T09:00:00Z")
    assert _idx(conn) == {("P9", "2026-11-21", "A", "B"): 1}
    assert services.list_part_state() == []


if __name__ == "__main__":
    # 无 pytest 环境下的简易回退执行器
    sys.exit(pytest.main([__file__, "-q"]))
