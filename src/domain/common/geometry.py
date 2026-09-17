"""Geometric value objects shared by vision and spatial reasoning.

Coordinates are pixels in the *original* image's coordinate system. Adapters that work on
a resized copy must map back before constructing a :class:`BoundingBox`, so that overlays
drawn on the original image always line up.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["BoundingBox", "Point", "distance", "iou"]


class Point(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


class BoundingBox(BaseModel):
    """Axis-aligned box in original-image pixel coordinates."""

    model_config = ConfigDict(frozen=True)

    x1: float = Field(description="Left edge")
    y1: float = Field(description="Top edge")
    x2: float = Field(description="Right edge")
    y2: float = Field(description="Bottom edge")

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBox:
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError(f"invalid bounding box: ({self.x1},{self.y1})-({self.x2},{self.y2})")
        return self

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Point:
        return Point(x=(self.x1 + self.x2) / 2.0, y=(self.y1 + self.y2) / 2.0)

    def contains(self, point: Point) -> bool:
        return self.x1 <= point.x <= self.x2 and self.y1 <= point.y <= self.y2

    def expanded(self, margin: float) -> BoundingBox:
        return BoundingBox(
            x1=self.x1 - margin, y1=self.y1 - margin, x2=self.x2 + margin, y2=self.y2 + margin
        )

    def intersection_area(self, other: BoundingBox) -> float:
        dx = min(self.x2, other.x2) - max(self.x1, other.x1)
        dy = min(self.y2, other.y2) - max(self.y1, other.y1)
        return dx * dy if dx > 0 and dy > 0 else 0.0

    def overlaps(self, other: BoundingBox) -> bool:
        return self.intersection_area(other) > 0.0

    def distance_to_point(self, point: Point) -> float:
        """Euclidean distance from ``point`` to the nearest point of the box (0 if inside)."""
        dx = max(self.x1 - point.x, 0.0, point.x - self.x2)
        dy = max(self.y1 - point.y, 0.0, point.y - self.y2)
        return math.hypot(dx, dy)

    def scaled(self, factor_x: float, factor_y: float | None = None) -> BoundingBox:
        fy = factor_x if factor_y is None else factor_y
        return BoundingBox(
            x1=self.x1 * factor_x, y1=self.y1 * fy, x2=self.x2 * factor_x, y2=self.y2 * fy
        )


def iou(a: BoundingBox, b: BoundingBox) -> float:
    """Intersection over union. Returns 0.0 when both boxes are degenerate."""
    intersection = a.intersection_area(b)
    union = a.area + b.area - intersection
    return intersection / union if union > 0 else 0.0


def distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)
