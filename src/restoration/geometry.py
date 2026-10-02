"""GeoJSON 多边形几何：面积、有效环、相交判定（扫描线 + Sutherland–Hodgman）。

只实现项目所需的最小二维几何，不依赖外部库。坐标统一为 GeoJSON
``[lon, lat]``；面积用等距近似投影（每度折合米）得到平方米，足够
用于面积比例的核证计算，不作为勘界法律依据。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]
Ring = Sequence[Point]

_METERS_PER_DEGREE_LAT = 111_320.0


def extract_polygon_rings(geometry: dict) -> list[list[Point]]:
    """从 GeoJSON Polygon/MultiPolygon 取出坐标环（点为 (x, y)）。"""
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Polygon":
        return [[tuple(p) for p in ring] for ring in coords]
    if kind == "MultiPolygon":
        rings: list[list[Point]] = []
        for polygon in coords:
            rings.extend([tuple(p) for p in ring] for ring in polygon)
        return rings
    raise ValueError("仅支持 Polygon 或 MultiPolygon 边界")


def project_points(rings: Iterable[Ring]) -> list[list[Point]]:
    """把经纬度环投影到局部米制平面（按环的平均纬度）。"""
    rings = [list(ring) for ring in rings]
    mean_lat = sum(p[1] for ring in rings for p in ring) / sum(len(r) for r in rings)
    scale = _METERS_PER_DEGREE_LAT * math.cos(math.radians(mean_lat))
    return [[(x * scale, y * _METERS_PER_DEGREE_LAT) for x, y in ring] for ring in rings]


def ring_signed_area(ring: Ring) -> float:
    """鞋带公式的有向面积。"""
    total = 0.0
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_area_m2(geometry: dict) -> float:
    """外环面积减去各孔洞面积（投影后，单位平方米）。"""
    projected = project_points(extract_polygon_rings(geometry))
    if not projected:
        return 0.0
    outer = abs(ring_signed_area(projected[0]))
    holes = sum(abs(ring_signed_area(ring)) for ring in projected[1:])
    return max(outer - holes, 0.0)


def _ring_segments(ring: Ring) -> Iterable[tuple[Point, Point]]:
    for i in range(len(ring)):
        yield ring[i], ring[(i + 1) % len(ring)]


def _bbox(ring: Ring) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    def cross(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point, b: Point, c: Point) -> bool:
        return (
            min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
            and abs(cross(a, b, c)) < 1e-9
        )

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 1e-9 and d2 < -1e-9) or (d1 < -1e-9 and d2 > 1e-9)) and (
        (d3 > 1e-9 and d4 < -1e-9) or (d3 < -1e-9 and d4 > 1e-9)
    ):
        return True
    if abs(d1) < 1e-9 and on_segment(p3, p4, p1):
        return True
    if abs(d2) < 1e-9 and on_segment(p3, p4, p2):
        return True
    if abs(d3) < 1e-9 and on_segment(p1, p2, p3):
        return True
    if abs(d4) < 1e-9 and on_segment(p1, p2, p4):
        return True
    return False


def _point_in_ring(point: Point, ring: Ring) -> bool:
    """射线法判定点是否在环内（含边界）。"""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in _ring_segments(ring):
        if (min(y1, y2) - 1e-9) <= y <= (max(y1, y2) + 1e-9):
            if abs(y1 - y2) < 1e-12:
                continue
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if abs(x - x_intersect) < 1e-9:
                return True
            if x_intersect > x:
                inside = not inside
    return inside


def _point_in_polygon(point: Point, rings: Sequence[Ring]) -> bool:
    if not rings or not _point_in_ring(point, rings[0]):
        return False
    return not any(_point_in_ring(point, hole) for hole in rings[1:])


def _clip(subject: list[Point], clip_ring: Ring) -> list[Point]:
    """Sutherland–Hodgman 多边形裁剪。clip_ring 须为逆时针（内部朝左）。"""
    ring = list(clip_ring)
    if ring_signed_area(ring) < 0:
        ring = list(reversed(ring))

    def inside(p: Point, a: Point, b: Point) -> bool:
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= -1e-9

    def intersection(p1: Point, p2: Point, a: Point, b: Point) -> Point:
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = a
        x4, y4 = b
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-12:
            return p2
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        return x1 + t * (x2 - x1), y1 + t * (y2 - y1)

    output = list(subject)
    for a, b in _ring_segments(ring):
        if not output:
            return []
        inputs = output
        output = []
        prev = inputs[-1]
        prev_in = inside(prev, a, b)
        for current in inputs:
            cur_in = inside(current, a, b)
            if cur_in:
                if not prev_in:
                    output.append(intersection(prev, current, a, b))
                output.append(current)
            elif prev_in:
                output.append(intersection(prev, current, a, b))
            prev, prev_in = current, cur_in
    return output


def polygons_overlap(geom_a: dict, geom_b: dict, *, min_overlap_m2: float = 1.0) -> bool:
    """两个 GeoJSON 面几何是否存在实质重叠（超过 min_overlap_m2 平方米）。

    外环先做包围盒/线段/点包含的快速判定；只有外环真正相交时才做
    裁剪求交面积，孔洞在面积计算中扣除。
    """
    rings_a = project_points(extract_polygon_rings(geom_a))
    rings_b = project_points(extract_polygon_rings(geom_b))
    if not rings_a or not rings_b:
        return False
    outer_a, outer_b = rings_a[0], rings_b[0]

    if not _bboxes_overlap(_bbox(outer_a), _bbox(outer_b)):
        return False
    if not _rings_touch(outer_a, outer_b):
        return False

    intersection_outer = _clip(outer_a, outer_b)
    if len(intersection_outer) < 3 or abs(ring_signed_area(intersection_outer)) < min_overlap_m2:
        return False

    # 扣除孔洞（简化处理：孔洞若完全落在交集中则减去其面积）。
    overlap = abs(ring_signed_area(intersection_outer))
    for hole in rings_a[1:] + rings_b[1:]:
        if all(_point_in_polygon(p, [intersection_outer]) for p in (hole[0], hole[len(hole) // 2])):
            overlap -= abs(ring_signed_area(hole))
    return overlap >= min_overlap_m2


def _bboxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _rings_touch(a: Ring, b: Ring) -> bool:
    """线段相交，或一个环有点落在另一个环内（包含关系）。"""
    for p1, p2 in _ring_segments(a):
        for p3, p4 in _ring_segments(b):
            if _segments_intersect(p1, p2, p3, p4):
                return True
    return _point_in_ring(a[0], b) or _point_in_ring(b[0], a)
