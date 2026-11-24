# 返工补登扫码流向核对服务

FastAPI + SQLite。质检员补登漏扫后，系统按修正后的扫码顺序自动维护工位间
**流向索引**，支持按 UTC 日期区间核对每个流向经手的工件数。
老班次明细可按 UTC 日期**封存**：逐条扫码删除，历史流向与每件最后的
历史工位保留，新班次的扫码自动接续。

## 规则

1. 每次扫码含：唯一编号 `scan_id`、工件号 `part_id`、工位 `station`、UTC 时刻 `ts_utc`。
2. 同一工件的扫码按 **(UTC 时刻, 扫码编号)** 排序；相邻两次扫码工位不同，
   才产生一条 `前站 -> 后站` 的流向，归入**后一次扫码的 UTC 日期**。
3. 同工位相邻不产生流向。
4. 支持改时刻、改工位、撤回；更新时**只定位改动位置前后的相邻扫码**做增量修复，
   不做全表重算。
5. 扫码明细与流向索引在**同一事务**内提交，任一步失败整体回滚，保留原状。
6. 流向索引按 `(工件, 日期, 前站, 后站)` 主键去重，重复出现只累加 `ref_count`：
   - 同件同日重复走同一流向，索引行数不随次数增长；
   - 区间查询只读索引，同件在区间内走同一流向只算一个工件
     （`COUNT(DISTINCT part_id)`），另给 `event_count` 反映实际发生次数。
7. **封存**：`POST /seal` 指定 UTC 封存日 D，删除 `day_utc < D` 的逐条扫码：
   - 流向索引一行不动：历史流向（含被删末条到幸存首条的接续边）原样保留，
     `/flows` 可跨封存日查任意区间，口径不变；
   - 每件被删明细中**最后一条的工位**记入 `part_state`，作为该件后续
     首条未封存扫码的"虚拟前驱"（接续工位）；
   - 封存日**可重复提交**（幂等，无新明细可封时一切不变），也可**往后推**
     （只封新旧封存日之间的明细；没有新明细可封的工件沿用上次的工位）；
     回退到更早的封存日返回 409；
   - 明细删除、接续工位、封存日在同一事务内提交，封存失败全部保留原状。
8. 封存后未封存扫码仍可增改撤，但**拒绝**把扫码补登/改到封存日前（409）；
   封存日当天及之后不受限。增改撤若改变了某件的首条未封存扫码，用保留的
   接续工位重算接续流向，仍只查改动处的相邻扫码。

## 启动

### Docker Compose（交付方式）

```bash
docker compose up --build
# 服务: http://localhost:8000  交互文档: http://localhost:8000/docs
```

SQLite 数据存于命名卷 `scanflow-data`（容器内 `/data/scanflow.db`）。

### 本机直接运行（无 Docker 时）

```bash
python3 -m venv --without-pip .venv        # 若 ensurepip 缺失, 需先引导 pip
.venv/bin/pip install -r requirements.txt
SCANFLOW_DB_PATH=./data/scanflow.db \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## API（/docs 可直接录入和查数）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/scans` | 录入扫码（编号重复 409；早于封存日 409） |
| PATCH | `/scans/{scan_id}` | 改时刻 / 改工位（只传要改的字段；改到封存日前 409） |
| DELETE | `/scans/{scan_id}` | 撤回扫码并修复相邻流向 |
| GET | `/scans?part_id=` | 查询扫码明细（按件/时刻/编号排序） |
| GET | `/flows?start_day=&end_day=` | **按日期区间统计各流向工件数（只读索引，可跨封存日）** |
| GET | `/flow-index?part_id=` | 查看索引明细与引用计数（核对用） |
| POST | `/seal` | **封存：删除指定 UTC 日期前的扫码明细，保留历史流向与接续工位** |
| GET | `/seal` | 查看当前封存日 |
| GET | `/part-state?part_id=` | 查看每件保留的历史工位（封存接续用，核对用） |
| GET | `/health` | 健康检查 |

时刻接受 ISO8601（`...Z` 或带偏移量如 `+08:00`），内部统一转 UTC 存储；
日期参数格式为 `YYYY-MM-DD`。

### 示例

```bash
curl -X POST localhost:8000/scans -H 'Content-Type: application/json' \
  -d '{"scan_id":"S1","part_id":"P1","station":"A","ts_utc":"2026-11-24T08:00:00Z"}'
curl -X PATCH localhost:8000/scans/S1 -H 'Content-Type: application/json' \
  -d '{"station":"B"}'
curl -X DELETE localhost:8000/scans/S1
curl "localhost:8000/flows?start_day=2026-11-24&end_day=2026-11-25"
```

`/flows` 返回：

```json
[
  {"from_station": "A", "to_station": "B", "part_count": 3, "event_count": 5}
]
```

## 封存演示（可在 /docs 逐步执行）

以下 `curl` 与 /docs 各接口的请求体一一对应，演示**封存后补登**与**跨日查数**：

```bash
# 1) 老班次明细: PX 在 11-23 经 A -> B, 11-24 到 C
curl -X POST localhost:8000/scans -H 'Content-Type: application/json' \
  -d '{"scan_id":"D1","part_id":"PX","station":"A","ts_utc":"2026-11-23T08:00:00Z"}'
curl -X POST localhost:8000/scans -H 'Content-Type: application/json' \
  -d '{"scan_id":"D2","part_id":"PX","station":"B","ts_utc":"2026-11-23T20:00:00Z"}'
curl -X POST localhost:8000/scans -H 'Content-Type: application/json' \
  -d '{"scan_id":"D3","part_id":"PX","station":"C","ts_utc":"2026-11-24T09:00:00Z"}'

# 2) 封存 11-24: 删除 D1/D2, 索引保留 A->B(11-23) 与接续边 B->C(11-24),
#    PX 的接续工位记为 B
curl -X POST localhost:8000/seal -H 'Content-Type: application/json' \
  -d '{"seal_day":"2026-11-24"}'
curl localhost:8000/part-state?part_id=PX
# -> [{"part_id":"PX","last_station":"B","sealed_day":"2026-11-24"}]

# 3) 补登到封存日前 -> 409; 封存日当天及之后 -> 正常
curl -X POST localhost:8000/scans -H 'Content-Type: application/json' \
  -d '{"scan_id":"D0","part_id":"PX","station":"A","ts_utc":"2026-11-23T10:00:00Z"}'
# -> 409 {"detail":"扫码 D0 的日期 2026-11-23 早于封存日 2026-11-24, 拒绝写入"}

# 4) 新班次补登: PX 11-25 到 D, 链为 B(接续工位) -> C(11-24) -> D(11-25)
curl -X POST localhost:8000/scans -H 'Content-Type: application/json' \
  -d '{"scan_id":"D4","part_id":"PX","station":"D","ts_utc":"2026-11-25T08:00:00Z"}'
curl "localhost:8000/flow-index?part_id=PX"
# -> A->B(11-23), B->C(11-24), C->D(11-25)  (历史边与接续边都在)

# 5) 跨封存日查数: 区间跨 11-24 封存线, 口径不变
curl "localhost:8000/flows?start_day=2026-11-20&end_day=2026-11-30"
# -> A->B part_count=1, B->C part_count=1, C->D part_count=1

# 6) 封存日可重复提交 (幂等), 也可往后推; 回退 -> 409
curl -X POST localhost:8000/seal -H 'Content-Type: application/json' \
  -d '{"seal_day":"2026-11-24"}'    # deleted_scans=0, 状态不变
curl -X POST localhost:8000/seal -H 'Content-Type: application/json' \
  -d '{"seal_day":"2026-11-26"}'    # 往后推: D3/D4 被封, PX 接续工位更新为 D
curl -X POST localhost:8000/seal -H 'Content-Type: application/json' \
  -d '{"seal_day":"2026-11-25"}'    # -> 409 封存日不能回退
```

## 存储结构

- `scans`：扫码明细，主键 `scan_id`，索引 `(part_id, ts_utc, scan_id)` 支撑相邻定位；
  只保留未封存的扫码。
- `flow_index`：流向索引，主键 `(part_id, day_utc, from_station, to_station)`，
  `ref_count` 记录该件当天该流向的出现次数；**封存不删索引**。
- `part_state`：封存接续状态，主键 `part_id`，`last_station` 为该件封存时
  最后一条历史扫码的工位，`sealed_day` 为最近一次改写本记录的封存日。
- `seal_state`：单行表，当前封存日（无行表示尚未封存）。

## 测试

```bash
.venv/bin/python -m pytest tests/test_service.py -q       # 服务层: 增量/去重/封存/回滚
.venv/bin/python -I tests/e2e_http.py http://127.0.0.1:8000  # HTTP 端到端 + 随机化对照
```

端到端脚本内置独立的全量重算"对照器"（含封存语义：冻结历史边 + 虚拟前驱），
在封存前后共 180 轮随机增/改/撤后逐行核对索引与接续工位，
并验证区间统计的去重语义与跨封存日"同件同流向只算一件"。

