"""轨迹几何工具：重采样、归一化、拐角（分叉）检测。

坐标约定：输入轨迹为任意坐标系下的折线点列 [[x, y], ...]，
校验前会按"整字"做归一化（等比缩放并平移到单位正方形内），
因此用户画布的大小、偏移不影响匹配结果。
"""

from __future__ import annotations

import math
from typing import Sequence

Point = tuple[float, float]
Stroke = list[Point]


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def path_length(points: Sequence[Point]) -> float:
    return sum(distance(points[i - 1], points[i]) for i in range(1, len(points)))


def resample(points: Sequence[Point], n: int) -> Stroke:
    """把折线按弧长等距重采样为 n 个点（$1 recognizer 的经典算法）。"""
    pts: Stroke = [(float(p[0]), float(p[1])) for p in points]
    if not pts:
        raise ValueError("stroke must contain at least one point")
    if len(pts) == 1:
        return [pts[0]] * n

    interval = path_length(pts) / (n - 1)
    if interval <= 0:  # 所有点重合（如"点"画写成一个点）
        return [pts[0]] * n

    new_points: Stroke = [pts[0]]
    accumulated = 0.0
    i = 1
    while i < len(pts):
        d = distance(pts[i - 1], pts[i])
        if d == 0:
            i += 1
            continue
        if accumulated + d >= interval:
            t = (interval - accumulated) / d
            q = (
                pts[i - 1][0] + t * (pts[i][0] - pts[i - 1][0]),
                pts[i - 1][1] + t * (pts[i][1] - pts[i - 1][1]),
            )
            new_points.append(q)
            pts.insert(i, q)  # q 成为新的起点，下轮从 q 继续
            accumulated = 0.0
            i += 1
        else:
            accumulated += d
            i += 1

    while len(new_points) < n:  # 浮点误差兜底
        new_points.append(pts[-1])
    return new_points[:n]


def fit_character_transform(strokes: Sequence[Sequence[Point]], size: float = 1.0):
    """计算整字归一化变换：等比缩放（保持宽高比）+ 包围盒中心平移到单位正方形中心。

    返回 (scale, cx, cy, half)：p' = ((p - c) * scale + half)。
    """
    all_points = [p for stroke in strokes for p in stroke]
    if not all_points:
        return 1.0, 0.0, 0.0, size / 2.0

    min_x = min(p[0] for p in all_points)
    max_x = max(p[0] for p in all_points)
    min_y = min(p[1] for p in all_points)
    max_y = max(p[1] for p in all_points)
    span = max(max_x - min_x, max_y - min_y)
    if span <= 0:  # 整字只有一个点
        span = 1.0
    return size / span, (min_x + max_x) / 2.0, (min_y + max_y) / 2.0, size / 2.0


def apply_transform(strokes: Sequence[Sequence[Point]], transform) -> list[Stroke]:
    scale, cx, cy, half = transform
    return [
        [((p[0] - cx) * scale + half, (p[1] - cy) * scale + half) for p in stroke]
        for stroke in strokes
    ]


def normalize_character(strokes: Sequence[Sequence[Point]], size: float = 1.0) -> list[Stroke]:
    """整字归一化：等比缩放（保持宽高比）并把包围盒中心平移到单位正方形中心。"""
    return apply_transform(strokes, fit_character_transform(strokes, size))


def angle_between(v1: Point, v2: Point) -> float:
    """两个向量的夹角（度，0~180）。"""
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 <= 1e-12 or n2 <= 1e-12:
        return 0.0
    cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    cos = max(-1.0, min(1.0, cos))
    return math.degrees(math.acos(cos))


def corner_mask(points: Sequence[Point], angle_threshold_deg: float, window: int) -> list[bool]:
    """标记重采样折线上的拐角/分叉点（转折角 >= 阈值）及其邻域。

    端点不参与拐角判定。window 表示拐角两侧各放宽多少个采样点。
    """
    n = len(points)
    mask = [False] * n
    for i in range(1, n - 1):
        v1 = (points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
        v2 = (points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
        if angle_between(v1, v2) >= angle_threshold_deg:
            lo = max(1, i - window)
            hi = min(n - 2, i + window)
            for j in range(lo, hi + 1):
                mask[j] = True
    return mask
