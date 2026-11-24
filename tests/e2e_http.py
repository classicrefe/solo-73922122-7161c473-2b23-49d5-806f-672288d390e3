"""端到端冒烟/随机化测试: 通过 HTTP 打真实服务, 用独立重算对照器核对索引。

仅用标准库。用法:
  .venv/bin/python tests/e2e_http.py http://127.0.0.1:8000

对照模型 (封存版):
- scans: 本地镜像的未封存明细;
- part_state: 封存时每件保留的接续工位 (虚拟前驱);
- frozen: 历次封存冻结的历史边 (被删明细之间的边 + 旧接续工位到被删首条的边)。
远端 flow_index 在任何时刻都应等于 frozen + 对 (scans + 虚拟前驱) 的全量重算。
"""
from __future__ import annotations

import json
import random
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
STATIONS = ["A", "B", "C", "D"]


def call(method: str, path: str, body: dict | None = None,
         expect: tuple[int, ...] = (200, 201)) -> tuple[int, dict | list | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        code = e.code
        payload = json.loads(e.read().decode())
    assert code in expect, f"{method} {path} -> {code} {payload}, 期望 {expect}"
    return code, payload


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def day_of(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 本地对照状态

scans: list[dict] = []                       # 未封存明细的本地镜像
part_state: dict[str, dict] = {}             # part -> {last_station, sealed_day}
frozen: dict[tuple, int] = defaultdict(int)  # 封存冻结的历史边


def track(scan_id: str, part_id: str, station: str, ts: datetime) -> dict:
    row = {"scan_id": scan_id, "part_id": part_id, "station": station,
           "ts_utc": fmt(ts), "day_utc": day_of(ts)}
    scans.append(row)
    return row


def local_seal(day: str) -> None:
    """模拟远端封存: 冻结涉及被删明细的边, 记录接续工位, 删除明细。"""
    global scans
    by_part: dict[str, list[dict]] = defaultdict(list)
    for s in scans:
        by_part[s["part_id"]].append(s)
    for part, items in by_part.items():
        items.sort(key=lambda x: (x["ts_utc"], x["scan_id"]))
        doomed = [s for s in items if s["day_utc"] < day]
        if not doomed:
            continue  # 没有新明细可封: 沿用上次的接续工位
        # 旧接续工位 -> 被删首条: 冻结 (之后由新接续工位接管, 不再变动)
        if part in part_state:
            first = doomed[0]
            if part_state[part]["last_station"] != first["station"]:
                frozen[(part, first["day_utc"],
                        part_state[part]["last_station"],
                        first["station"])] += 1
        # 被删明细之间的相邻边: 冻结
        for a, b in zip(doomed, doomed[1:]):
            if a["station"] != b["station"]:
                frozen[(part, b["day_utc"], a["station"], b["station"])] += 1
        part_state[part] = {"part_id": part,
                            "last_station": doomed[-1]["station"],
                            "sealed_day": day}
    scans = [s for s in scans if s["day_utc"] >= day]


def expected_index() -> dict[tuple, int]:
    """独立重算: frozen + 对 (未封存明细 + 虚拟前驱) 的全量相邻扫描。"""
    edges: dict[tuple, int] = dict(frozen)
    by_part: dict[str, list[dict]] = defaultdict(list)
    for s in scans:
        by_part[s["part_id"]].append(s)
    for part, items in by_part.items():
        items.sort(key=lambda x: (x["ts_utc"], x["scan_id"]))
        chain = items
        if part in part_state:
            chain = [{"station": part_state[part]["last_station"]}] + items
        for a, b in zip(chain, chain[1:]):
            if a["station"] != b["station"]:
                key = (part, b["day_utc"], a["station"], b["station"])
                edges[key] = edges.get(key, 0) + 1
    return edges


def sync_check():
    """全量对照: 扫码明细 / 流向索引 / 接续工位。"""
    _, remote_all = call("GET", "/scans")
    remote_sorted = sorted(remote_all,
                           key=lambda x: (x["part_id"], x["ts_utc"], x["scan_id"]))
    local_sorted = sorted(scans,
                          key=lambda x: (x["part_id"], x["ts_utc"], x["scan_id"]))
    assert remote_sorted == local_sorted, "扫码明细与本地状态不一致"
    _, idx = call("GET", "/flow-index")
    actual = {(r["part_id"], r["day_utc"], r["from_station"], r["to_station"]):
              r["ref_count"] for r in idx}
    expected = expected_index()
    assert actual == expected, (
        f"流向索引不一致\n期望={sorted(expected.items())}\n实际={sorted(actual.items())}")
    _, remote_ps = call("GET", "/part-state")
    assert sorted(remote_ps, key=lambda r: r["part_id"]) == \
        sorted(part_state.values(), key=lambda r: r["part_id"]), "接续工位不一致"
    return expected, actual


def random_ops(rng: random.Random, rounds: int, base: datetime,
               max_hours: int, parts: list[str], next_id) -> None:
    """在 [base, base+max_hours] 内随机增/改/撤, 每步全量对照。

    只操作 parts 池内工件的扫码, 不动固定场景 (E1/X1/K1/K2) 的数据。
    """
    global scans
    for _ in range(rounds):
        pool = [s for s in scans if s["part_id"] in parts]
        action = rng.choices(["add", "update_station", "update_time", "withdraw"],
                             weights=[4, 2, 2, 2])[0]
        if action == "add" or not pool:
            ts = base + timedelta(hours=rng.randint(0, max_hours),
                                  minutes=rng.randint(0, 59))
            sid, part, station = next_id(), rng.choice(parts), rng.choice(STATIONS)
            call("POST", "/scans", {"scan_id": sid, "part_id": part,
                                    "station": station,
                                    "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
            track(sid, part, station, ts)
        else:
            target = rng.choice(pool)
            if action == "withdraw":
                call("DELETE", f"/scans/{target['scan_id']}")
                scans = [s for s in scans if s["scan_id"] != target["scan_id"]]
            elif action == "update_station":
                new_station = rng.choice([s for s in STATIONS
                                          if s != target["station"]])
                call("PATCH", f"/scans/{target['scan_id']}",
                     {"station": new_station})
                target["station"] = new_station
            else:
                ts = base + timedelta(hours=rng.randint(0, max_hours),
                                      minutes=rng.randint(0, 59))
                call("PATCH", f"/scans/{target['scan_id']}",
                     {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
                target["ts_utc"] = fmt(ts)
                target["day_utc"] = day_of(ts)
        sync_check()


def main() -> int:
    global scans
    rng = random.Random(20261124)
    next_n = 0

    def new_id() -> str:
        nonlocal next_n
        next_n += 1
        return f"r{next_n:04d}"

    print("0. 健康检查/文档")
    call("GET", "/health")
    with urllib.request.urlopen(BASE + "/openapi.json", timeout=10) as r:
        spec = json.loads(r.read().decode())
    for p in ("/scans", "/flows", "/seal", "/part-state"):
        assert p in spec["paths"], f"openapi 缺少 {p}"
    _, seal0 = call("GET", "/seal")
    assert seal0 == {"seal_day": None}

    print("1. 固定场景: 录入 + 日期归属 + 查询")
    call("POST", "/scans", {"scan_id": "e1", "part_id": "E1", "station": "A",
                            "ts_utc": "2026-11-23T23:50:00Z"})
    call("POST", "/scans", {"scan_id": "e2", "part_id": "E1", "station": "B",
                            "ts_utc": "2026-11-24T00:10:00Z"})
    e2 = track("e2", "E1", "B", datetime(2026, 11, 24, 0, 10,
                                         tzinfo=timezone.utc))
    # 重复编号 -> 409 且数据不变
    _, before = call("GET", "/scans?part_id=E1")
    call("POST", "/scans", {"scan_id": "e1", "part_id": "E1", "station": "Z",
                            "ts_utc": "2026-11-24T03:00:00Z"}, expect=(409,))
    _, after = call("GET", "/scans?part_id=E1")
    assert before == after, "重复编号失败后数据发生变化"
    # 流向归入后一次扫码的 UTC 日期 11-24
    _, d23 = call("GET", "/flows?start_day=2026-11-23&end_day=2026-11-23")
    _, d24 = call("GET", "/flows?start_day=2026-11-24&end_day=2026-11-24")
    assert d23 == [] and len(d24) == 1 and d24[0]["from_station"] == "A" \
        and d24[0]["to_station"] == "B" and d24[0]["part_count"] == 1

    print("2. 改时刻/改工位 + 404/422")
    call("PATCH", "/scans/e2", {"station": "C"})
    e2["station"] = "C"
    call("PATCH", "/scans/e2", {"ts_utc": "2026-11-24T01:00:00Z"})
    e2["ts_utc"] = "2026-11-24T01:00:00.000000Z"
    e2["day_utc"] = "2026-11-24"
    call("PATCH", "/scans/missing", {"station": "X"}, expect=(404,))
    call("PATCH", "/scans/e1", {}, expect=(422,))
    call("POST", "/scans", {"scan_id": "bad", "part_id": "X", "station": "A",
                            "ts_utc": "nonsense"}, expect=(422,))
    call("GET", "/flows?start_day=2026-11-24&end_day=2026-11-01",
         expect=(422,))

    print("3. 撤回 + 不存在 -> 404")
    call("DELETE", "/scans/e1")
    call("DELETE", "/scans/e1", expect=(404,))

    print("4. 封存演示数据: X1(跨日同流向) / K1(沿用旧工位) / K2(更新工位)")
    track("x1", "X1", "SA", datetime(2026, 11, 21, 8, tzinfo=timezone.utc))
    track("x2", "X1", "SB", datetime(2026, 11, 21, 9, tzinfo=timezone.utc))
    track("k1", "K1", "Q", datetime(2026, 11, 21, 10, tzinfo=timezone.utc))
    track("k2a", "K2", "M1", datetime(2026, 11, 21, 11, tzinfo=timezone.utc))
    track("k2b", "K2", "M2", datetime(2026, 11, 23, 8, tzinfo=timezone.utc))
    for s in scans[-5:]:
        call("POST", "/scans", {"scan_id": s["scan_id"], "part_id": s["part_id"],
                                "station": s["station"], "ts_utc": s["ts_utc"]})
    sync_check()

    print("5. 随机化增/改/撤 (封存前), 每步与独立重算对照")
    base = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
    random_ops(rng, 120, base, 96, ["R1", "R2", "R3", "R4"], new_id)
    print(f"   120 轮后: 未封存扫码 {len(scans)} 条, 索引边 {len(expected_index())} 条")

    print("6. 区间统计语义: 同件同区间同流向只算一次")
    expected = expected_index()
    exp_agg: dict[tuple, set] = defaultdict(set)
    exp_events: dict[tuple, int] = defaultdict(int)
    for (part, _day, frm, to), cnt in expected.items():
        exp_agg[(frm, to)].add(part)
        exp_events[(frm, to)] += cnt
    _, flows = call("GET", "/flows?start_day=2026-11-19&end_day=2026-11-30")
    got = {(f["from_station"], f["to_station"]):
           (f["part_count"], f["event_count"]) for f in flows}
    for key, parts in exp_agg.items():
        assert got[key] == (len(parts), exp_events[key]), f"聚合不一致: {key}"

    print("7. 封存 2026-11-22: 明细删除, 索引/接续工位保留")
    _, idx_pre = call("GET", "/flow-index")
    _, out = call("POST", "/seal", {"seal_day": "2026-11-22"})
    assert out["previous_seal_day"] is None and out["deleted_scans"] > 0
    local_seal("2026-11-22")
    expected, _ = sync_check()
    # 封存前的历史边 (day < 11-22) 一行不少
    post = expected
    for r in idx_pre:
        if r["day_utc"] < "2026-11-22":
            key = (r["part_id"], r["day_utc"], r["from_station"], r["to_station"])
            assert post.get(key) == r["ref_count"], f"历史边丢失: {key}"
    _, seal_now = call("GET", "/seal")
    assert seal_now == {"seal_day": "2026-11-22"}
    _, k1 = call("GET", "/part-state?part_id=K1")
    assert k1[0]["last_station"] == "Q" and k1[0]["sealed_day"] == "2026-11-22"
    print(f"   删除 {out['deleted_scans']} 条明细, "
          f"记录 {out['parts_updated']} 件接续工位, 历史边全保留")

    print("8. 封存幂等/回退/越界写入")
    _, out2 = call("POST", "/seal", {"seal_day": "2026-11-22"})
    assert out2["deleted_scans"] == 0 and out2["parts_updated"] == 0
    sync_check()
    call("POST", "/seal", {"seal_day": "2026-11-21"}, expect=(409,))  # 回退
    call("POST", "/seal", {"seal_day": "2026/11/23"}, expect=(422,))  # 格式
    call("POST", "/scans", {"scan_id": "bad1", "part_id": "R1", "station": "A",
                            "ts_utc": "2026-11-21T23:00:00Z"},
         expect=(409,))  # 补登到封存日前
    call("PATCH", "/scans/e2", {"ts_utc": "2026-11-20T01:00:00Z"},
         expect=(409,))  # 改到封存日前
    # 封存日当天允许
    track("ok1", "R1", "A", datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc))
    call("POST", "/scans", {"scan_id": "ok1", "part_id": "R1", "station": "A",
                            "ts_utc": "2026-11-22T00:00:00Z"})
    sync_check()

    print("9. 封存后随机化增/改/撤 (首条接续重算), 每步对照")
    base7 = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    random_ops(rng, 40, base7, 120, ["R1", "R2", "R3", "R4", "R5"], new_id)
    print(f"   40 轮后: 未封存扫码 {len(scans)} 条, 索引边 {len(expected_index())} 条")

    print("10. 封存日后推 2026-11-24: 无新明细的工件沿用上次工位")
    _, out3 = call("POST", "/seal", {"seal_day": "2026-11-24"})
    assert out3["previous_seal_day"] == "2026-11-22"
    local_seal("2026-11-24")
    sync_check()
    _, k1 = call("GET", "/part-state?part_id=K1")
    assert k1[0]["last_station"] == "Q" and k1[0]["sealed_day"] == "2026-11-22", \
        "K1 没有新明细可封, 必须沿用上次工位"
    _, k2 = call("GET", "/part-state?part_id=K2")
    assert k2[0]["last_station"] == "M2" and k2[0]["sealed_day"] == "2026-11-24", \
        "K2 的 11-23 明细被本次封存, 工位应更新"
    # K2 新首条应接续更新后的工位 M2
    track("k2c", "K2", "M3", datetime(2026, 11, 25, 8, tzinfo=timezone.utc))
    call("POST", "/scans", {"scan_id": "k2c", "part_id": "K2", "station": "M3",
                            "ts_utc": "2026-11-25T08:00:00Z"})
    expected, _ = sync_check()
    assert expected.get(("K2", "2026-11-25", "M2", "M3")) == 1

    print("11. 再一轮随机化 (封存日 11-24 之后)")
    base9 = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    random_ops(rng, 20, base9, 96, ["R1", "R2", "R3", "R5"], new_id)

    print("12. 跨封存日查数: 同件同流向只算一件")
    # X1 封存前已走 SA->SB (11-21, 已冻结); 封存后再走一次
    track("x3", "X1", "SA", datetime(2026, 11, 25, 8, tzinfo=timezone.utc))
    track("x4", "X1", "SB", datetime(2026, 11, 25, 9, tzinfo=timezone.utc))
    for s in scans[-2:]:
        call("POST", "/scans", {"scan_id": s["scan_id"], "part_id": s["part_id"],
                                "station": s["station"], "ts_utc": s["ts_utc"]})
    sync_check()
    _, flows = call("GET", "/flows?start_day=2026-11-19&end_day=2026-11-30")
    got = {(f["from_station"], f["to_station"]):
           (f["part_count"], f["event_count"]) for f in flows}
    assert got[("SA", "SB")] == (1, 2), \
        f"同件跨封存日同流向应只算一件: {got.get(('SA', 'SB'))}"
    _, flows_post = call("GET", "/flows?start_day=2026-11-22&end_day=2026-11-30")
    got_post = {(f["from_station"], f["to_station"]): f["part_count"]
                for f in flows_post}
    assert got_post[("SA", "SB")] == 1
    print(f"   SA->SB 跨封存日: part_count=1, event_count=2 ✔")

    print("全部端到端检查通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
