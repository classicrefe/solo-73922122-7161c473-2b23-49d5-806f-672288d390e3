"""请求/响应模型 (OpenAPI 文档驱动 /docs 录入)。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ScanCreate(BaseModel):
    scan_id: str = Field(..., min_length=1, description="本次扫码的唯一编号",
                         examples=["S001"])
    part_id: str = Field(..., min_length=1, description="工件号", examples=["P100"])
    station: str = Field(..., min_length=1, description="扫码工位",
                         examples=["A"])
    ts_utc: str = Field(
        ...,
        description="UTC 时刻, ISO8601 (如 2026-11-24T01:30:00Z 或带偏移量)",
        examples=["2026-11-24T01:30:00Z"],
    )


class ScanUpdate(BaseModel):
    station: str | None = Field(
        None, min_length=1, description="新工位; 不传表示不改工位"
    )
    ts_utc: str | None = Field(
        None,
        description="新 UTC 时刻 (ISO8601); 不传表示不改时刻",
    )


class ScanOut(BaseModel):
    scan_id: str
    part_id: str
    station: str
    ts_utc: str
    day_utc: str


class WithdrawOut(ScanOut):
    withdrawn: bool


class FlowCount(BaseModel):
    from_station: str
    to_station: str
    part_count: int = Field(..., description="工件数 (同件同流向只计一次)")
    event_count: int = Field(..., description="流向发生次数 (含同件重复)")


class FlowIndexRow(BaseModel):
    part_id: str
    day_utc: str
    from_station: str
    to_station: str
    ref_count: int


class SealRequest(BaseModel):
    seal_day: str = Field(
        ...,
        description="封存日 (UTC, YYYY-MM-DD): 删除该日前的逐条扫码, "
                    "保留历史流向索引与每件最后的历史工位; 可重复提交或往后推",
        examples=["2026-11-24"],
    )


class SealOut(BaseModel):
    seal_day: str = Field(..., description="生效后的封存日")
    previous_seal_day: str | None = Field(..., description="之前的封存日 (首次为 null)")
    deleted_scans: int = Field(..., description="本次删除的扫码明细条数")
    parts_updated: int = Field(..., description="本次更新接续工位的工件数")


class SealStateOut(BaseModel):
    seal_day: str | None = Field(..., description="当前封存日; null 表示尚未封存")


class PartStateRow(BaseModel):
    part_id: str
    last_station: str = Field(..., description="封存时该件最后一条历史扫码的工位")
    sealed_day: str = Field(..., description="最近一次改写本记录的封存日")
