# -*- coding: utf-8 -*-
"""Disabled-by-default OCR adapter interface.

No OCR engine, cloud call or automatic crop is implemented or claimed in the
first usable version.  Callers can replace ``NullOcrAdapter`` later without
changing the content pipeline or UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class OcrResult:
    text: str = ""
    boxes: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    engine: str = "disabled"
    reason: str = ""


class OcrAdapter(Protocol):
    enabled: bool

    def extract(self, image_path: str, *, max_pixels: int = 40_000_000) -> OcrResult:
        ...


class NullOcrAdapter:
    enabled = False

    def extract(self, image_path: str, *, max_pixels: int = 40_000_000) -> OcrResult:
        return OcrResult(reason="OCR 默认禁用；本版本未安装或调用任何 OCR 引擎")


ADAPTER: OcrAdapter = NullOcrAdapter()


def get_adapter() -> OcrAdapter:
    return ADAPTER
