"""笔画匹配与评分。

流程：
1. 每条笔画按弧长重采样为 N 个点；
2. 用共有笔画最小二乘估计用户轨迹 → 参考轨迹的相似变换（等比缩放 + 平移），
   消除画布偏移/缩放差异，且漏笔时不会扭曲其余笔画的位置判定；
3. 统一套参考字的整字归一化变换，进入单位坐标系；
4. 按笔顺逐笔比较：方向、起点、终点、形状（分叉/拐角处放宽）；
5. 汇总首个不合格笔画、偏差原因与整字得分。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .geometry import (
    Point,
    Stroke,
    apply_transform,
    corner_mask,
    distance,
    fit_character_transform,
    resample,
)
from .models import MatchConfig

# 偏差原因代码
REVERSED_STROKE = "REVERSED_STROKE"        # 运笔方向写反
WRONG_DIRECTION = "WRONG_DIRECTION"        # 运笔方向偏差超限
START_POINT_OFFSET = "START_POINT_OFFSET"  # 起笔位置偏移超限
END_POINT_OFFSET = "END_POINT_OFFSET"      # 收笔位置偏移超限
SHAPE_DEVIATION = "SHAPE_DEVIATION"        # 笔画形状偏差超限
LOW_STROKE_SCORE = "LOW_STROKE_SCORE"      # 单笔综合得分低于合格线
MISSING_STROKE = "MISSING_STROKE"          # 缺少笔画
EXTRA_STROKE = "EXTRA_STROKE"              # 多出笔画

# 方向偏差超过该角度视为"写反"
_REVERSED_DEG = 135.0
# 位移短于该值视为"点画"，不做整体方向角判定
_MIN_DISPLACEMENT = 0.03

# 单笔得分权重
_W_SHAPE = 0.35
_W_DIRECTION = 0.25
_W_START = 0.20
_W_END = 0.20


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class StrokeComparison:
    score: float
    passed: bool
    reasons: list[str]
    metrics: dict


@dataclass
class ValidationResult:
    passed: bool
    score: float
    first_failed_stroke: Optional[int]  # 1-based
    deviation_reasons: list[str]
    stroke_results: list[dict] = field(default_factory=list)


def _direction_metrics(ref: Sequence[Point], user: Sequence[Point]) -> tuple[Optional[float], float]:
    """返回 (整体方向角偏差°, 对应采样段平均方向余弦)。"""
    rd = (ref[-1][0] - ref[0][0], ref[-1][1] - ref[0][1])
    ud = (user[-1][0] - user[0][0], user[-1][1] - user[0][1])
    rl, ul = math.hypot(*rd), math.hypot(*ud)

    angle_err: Optional[float] = None
    if rl > _MIN_DISPLACEMENT and ul > _MIN_DISPLACEMENT:
        cos = (rd[0] * ud[0] + rd[1] * ud[1]) / (rl * ul)
        angle_err = math.degrees(math.acos(max(-1.0, min(1.0, cos))))

    cosines = []
    for i in range(1, len(ref)):
        rv = (ref[i][0] - ref[i - 1][0], ref[i][1] - ref[i - 1][1])
        uv = (user[i][0] - user[i - 1][0], user[i][1] - user[i - 1][1])
        rn, un = math.hypot(*rv), math.hypot(*uv)
        if rn > 1e-9 and un > 1e-9:
            cosines.append((rv[0] * uv[0] + rv[1] * uv[1]) / (rn * un))
    tangent_cos = sum(cosines) / len(cosines) if cosines else 1.0
    return angle_err, tangent_cos


def _fit_similarity(user_pts: Sequence[Point], ref_pts: Sequence[Point]) -> tuple[float, float, float]:
    """最小二乘估计用户 → 参考的相似变换（等比缩放 + 平移，不旋转）。

    对应关系为同序号笔画的重采样点一一对应。返回 (s, tx, ty)。
    """
    n = len(user_pts)
    ux = sum(p[0] for p in user_pts) / n
    uy = sum(p[1] for p in user_pts) / n
    rx = sum(p[0] for p in ref_pts) / n
    ry = sum(p[1] for p in ref_pts) / n

    num = sum(
        (u[0] - ux) * (r[0] - rx) + (u[1] - uy) * (r[1] - ry)
        for u, r in zip(user_pts, ref_pts)
    )
    den = sum((u[0] - ux) ** 2 + (u[1] - uy) ** 2 for u in user_pts)
    s = num / den if den > 1e-12 else 1.0
    if s <= 1e-6:  # 退化或整体镜像：退化为仅平移
        s = 1.0
    return s, rx - s * ux, ry - s * uy


def _estimate_similarity(
    user_res: Sequence[Stroke],
    ref_res: Sequence[Stroke],
    n_common: int,
    ref_span: float,
    inlier_threshold: float,
) -> tuple[float, float, float]:
    """稳健对齐：全局拟合后剔除残差过大的离群笔画再重新拟合。

    避免个别写错/写偏的笔画把整体对齐拉歪，导致无辜笔画被误判。
    """
    u_flat = [p for stroke in user_res[:n_common] for p in stroke]
    r_flat = [p for stroke in ref_res[:n_common] for p in stroke]
    s, tx, ty = _fit_similarity(u_flat, r_flat)
    if n_common <= 1 or ref_span <= 0:
        return s, tx, ty

    def stroke_residual(k: int) -> float:
        errs = [
            distance(
                (s * p[0] + tx, s * p[1] + ty),
                ref_res[k][j],
            )
            for j, p in enumerate(user_res[k])
        ]
        return sum(errs) / len(errs) / ref_span

    residuals = [stroke_residual(k) for k in range(n_common)]
    inliers = [k for k in range(n_common) if residuals[k] <= inlier_threshold]
    # 无内点（如仅两笔且一笔整体写偏，归属本质 ambiguous）：保持全局拟合
    if not inliers or len(inliers) == n_common:
        return s, tx, ty

    u_in = [p for k in inliers for p in user_res[k]]
    r_in = [p for k in inliers for p in ref_res[k]]
    return _fit_similarity(u_in, r_in)


def compare_strokes(ref_pts: Stroke, user_pts: Stroke, cfg: MatchConfig) -> StrokeComparison:
    """比较两条已归一化、已重采样的笔画（各 cfg.resample_points 个点）。"""
    n = len(ref_pts)
    start_dist = distance(ref_pts[0], user_pts[0])
    end_dist = distance(ref_pts[-1], user_pts[-1])

    angle_err, tangent_cos = _direction_metrics(ref_pts, user_pts)

    # 形状偏差：分叉/拐角处按配置放宽
    fork_mask = corner_mask(ref_pts, cfg.fork_angle_deg, cfg.fork_window)
    dists = []
    for i in range(n):
        d = distance(ref_pts[i], user_pts[i])
        if fork_mask[i]:
            d = max(0.0, d - cfg.fork_extra_tolerance)
        dists.append(d)
    mean_dist = sum(dists) / n
    max_dist = max(dists)

    # ---- 硬性判定 ----
    reasons: list[str] = []
    if angle_err is not None and angle_err >= _REVERSED_DEG:
        reasons.append(REVERSED_STROKE)
    elif angle_err is not None and angle_err > cfg.direction_tolerance_deg:
        reasons.append(WRONG_DIRECTION)
    elif angle_err is None and tangent_cos < math.cos(math.radians(cfg.direction_tolerance_deg)):
        reasons.append(WRONG_DIRECTION)

    if start_dist > cfg.start_end_tolerance:
        reasons.append(START_POINT_OFFSET)
    if end_dist > cfg.start_end_tolerance:
        reasons.append(END_POINT_OFFSET)
    if mean_dist > cfg.shape_tolerance or max_dist > cfg.shape_tolerance * cfg.shape_max_factor:
        reasons.append(SHAPE_DEVIATION)

    # ---- 连续得分（0~1） ----
    start_score = _clamp01(1 - start_dist / cfg.start_end_tolerance) if cfg.start_end_tolerance > 0 else 1.0
    end_score = _clamp01(1 - end_dist / cfg.start_end_tolerance) if cfg.start_end_tolerance > 0 else 1.0
    shape_score = _clamp01(1 - mean_dist / cfg.shape_tolerance) if cfg.shape_tolerance > 0 else 1.0
    tangent_score = (tangent_cos + 1.0) / 2.0
    if angle_err is not None:
        disp_score = (math.cos(math.radians(angle_err)) + 1.0) / 2.0
        direction_score = 0.6 * disp_score + 0.4 * tangent_score
    else:
        direction_score = tangent_score

    score = (
        _W_SHAPE * shape_score
        + _W_DIRECTION * direction_score
        + _W_START * start_score
        + _W_END * end_score
    )

    if not reasons and score < cfg.pass_stroke_score:
        reasons.append(LOW_STROKE_SCORE)

    return StrokeComparison(
        score=round(score, 4),
        passed=not reasons,
        reasons=reasons,
        metrics={
            "start_distance": round(start_dist, 4),
            "end_distance": round(end_dist, 4),
            "direction_error_deg": round(angle_err, 2) if angle_err is not None else None,
            "mean_shape_distance": round(mean_dist, 4),
            "max_shape_distance": round(max_dist, 4),
        },
    )


def validate(
    ref_strokes: Sequence[Sequence[Point]],
    user_strokes: Sequence[Sequence[Point]],
    cfg: MatchConfig,
) -> ValidationResult:
    """整字校验：对齐 → 归一化 → 按笔顺逐笔比对。"""
    n = cfg.resample_points
    n_ref, n_user = len(ref_strokes), len(user_strokes)
    n_common = min(n_ref, n_user)

    # 1) 重采样（弧长等距，与坐标尺度无关）
    ref_res = [resample(s, n) for s in ref_strokes]
    user_res = [resample(s, n) for s in user_strokes]

    # 2) 用共有笔画稳健估计用户 → 参考的相似变换（缩放 + 平移）
    transform = fit_character_transform(ref_strokes)
    ref_span = 1.0 / transform[0] if transform[0] > 0 else 1.0
    s, tx, ty = _estimate_similarity(
        user_res, ref_res, n_common, ref_span, cfg.alignment_inlier_threshold
    )
    aligned: list[Stroke] = [
        [(s * p[0] + tx, s * p[1] + ty) for p in stroke] for stroke in user_res
    ]

    # 3) 统一套参考字的归一化变换，进入单位坐标系
    ref_norm = apply_transform(ref_res, transform)
    user_norm = apply_transform(aligned, transform)

    stroke_results: list[dict] = []
    score_sum = 0.0
    first_failed: Optional[int] = None
    first_reasons: list[str] = []

    for i in range(n_common):
        comp = compare_strokes(ref_norm[i], user_norm[i], cfg)
        score_sum += comp.score
        stroke_results.append(
            {
                "index": i + 1,
                "score": comp.score,
                "passed": comp.passed,
                "reasons": comp.reasons,
                "metrics": comp.metrics,
            }
        )
        if not comp.passed and first_failed is None:
            first_failed = i + 1
            first_reasons = comp.reasons

    if n_user < n_ref:
        # 缺失的笔画从 0 分计，并标记首个缺失位置
        for i in range(n_user, n_ref):
            stroke_results.append(
                {"index": i + 1, "score": 0.0, "passed": False, "reasons": [MISSING_STROKE], "metrics": None}
            )
        if first_failed is None:
            first_failed = n_user + 1
            first_reasons = [MISSING_STROKE]
    elif n_user > n_ref:
        if first_failed is None:
            first_failed = n_ref + 1
            first_reasons = [EXTRA_STROKE]

    denominator = max(n_ref, n_user)
    total = round(100.0 * score_sum / denominator, 2) if denominator else 0.0
    passed = first_failed is None

    return ValidationResult(
        passed=passed,
        score=total,
        first_failed_stroke=first_failed,
        deviation_reasons=first_reasons,
        stroke_results=stroke_results,
    )
