"""Bound full-page preview allocations independently of PDF dimensions."""
import math

MAX_PREVIEW_PIXELS = 8_000_000
MAX_PREVIEW_SIDE = 16000


def bounded_preview_scale(width, height, requested):
    if not all(math.isfinite(value) and value > 0 for value in (width, height, requested)):
        raise ValueError("Invalid preview dimensions")
    return min(requested, math.sqrt(MAX_PREVIEW_PIXELS / (width * height)),
               MAX_PREVIEW_SIDE / width, MAX_PREVIEW_SIDE / height)
