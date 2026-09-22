"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Annotated, Optional

from pydantic import BaseModel, Field

Point = Annotated[list[float], Field(min_length=2, max_length=2, description="[x, y] 坐标")]
Stroke = Annotated[list[Point], Field(min_length=1, description="一条笔画的折线点列")]


class MatchConfig(BaseModel):
    """匹配容差配置（均作用于归一化后的单位坐标系，可按提交覆盖）。"""

    resample_points: int = Field(48, ge=8, le=256, description="每条笔画重采样点数")
    direction_tolerance_deg: float = Field(
        50.0, ge=0, le=180, description="运笔方向允许的最大角度偏差（度）"
    )
    start_end_tolerance: float = Field(
        0.18, ge=0, le=1, description="起点/终点允许的最大偏移（归一化单位）"
    )
    shape_tolerance: float = Field(
        0.15, ge=0, le=1, description="形状平均偏差容忍度（归一化单位）"
    )
    shape_max_factor: float = Field(
        2.5, ge=1, le=10, description="单点最大偏差允许超过 shape_tolerance 的倍数"
    )
    fork_angle_deg: float = Field(
        55.0, ge=0, le=180, description="判定拐角/分叉的最小转折角（度）"
    )
    fork_window: int = Field(3, ge=0, le=10, description="拐角两侧放宽的采样点数")
    fork_extra_tolerance: float = Field(
        0.12, ge=0, le=1, description="分叉/拐角处额外允许的偏差（归一化单位）"
    )
    alignment_inlier_threshold: float = Field(
        0.12, ge=0.01, le=1, description="对齐时判定离群笔画的残差阈值（归一化单位）"
    )
    pass_stroke_score: float = Field(
        0.6, ge=0, le=1, description="单笔合格分数线（0~1）"
    )


class ReferenceUpsert(BaseModel):
    """登记/覆盖一个字的参考笔画序列。"""

    strokes: Annotated[list[Stroke], Field(min_length=1, description="按正确笔顺排列的笔画")]


class ReferenceOut(BaseModel):
    char: str
    strokes: list[Stroke]
    stroke_count: int
    updated_at: str


class ValidateRequest(BaseModel):
    """一次书写校验提交。submission_id 为幂等键：同一 id 重复提交返回首个结果。"""

    submission_id: Annotated[str, Field(min_length=1, max_length=128, description="幂等键，由客户端生成")]
    char: Annotated[str, Field(min_length=1, max_length=4, description="目标汉字")]
    strokes: Annotated[list[Stroke], Field(min_length=1, description="用户书写轨迹，按书写顺序")]
    config: MatchConfig = Field(default_factory=MatchConfig)


class StrokeMetrics(BaseModel):
    start_distance: float = Field(description="起点偏差（归一化单位）")
    end_distance: float = Field(description="终点偏差（归一化单位）")
    direction_error_deg: Optional[float] = Field(description="整体运笔方向角度偏差（度），点画为 null")
    mean_shape_distance: float = Field(description="平均形状偏差（已含分叉放宽）")
    max_shape_distance: float = Field(description="最大单点形状偏差（已含分叉放宽）")


class StrokeResult(BaseModel):
    index: int = Field(description="笔画序号（从 1 开始）")
    score: float = Field(description="单笔得分（0~1）")
    passed: bool
    reasons: list[str] = Field(description="不合格原因代码，空表示合格")
    metrics: Optional[StrokeMetrics] = None


class ValidateResponse(BaseModel):
    submission_id: str
    char: str
    passed: bool = Field(description="整字是否合格")
    score: float = Field(description="整字得分（0~100）")
    first_failed_stroke: Optional[int] = Field(description="首个不合格笔画序号（从 1 开始），全部合格为 null")
    deviation_reasons: list[str] = Field(description="首个不合格笔画的偏差原因代码")
    expected_strokes: int
    received_strokes: int
    stroke_results: list[StrokeResult]
    replayed: bool = Field(description="是否为幂等重放（未重新计算）")
    created_at: str


class ErrorResponse(BaseModel):
    detail: str
