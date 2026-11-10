"""The one interface an OCR engine implements (plan D8).

OCR is not a pipeline stage. nam_agent/nam/docs/parse.py runs normal extraction
first and calls the ocr_document tool (nam_agent/nam/tools.py) only for the pages
that gave no usable text: a scanned PDF, an image, or machine text that
cannot be read. The tool calls whichever backend is configured.

A backend is any class with a `name` and a `recognise()` method:

    class MyEngine:
        name = "my-engine 1.2"
        def recognise(self, data: bytes, mime: str, pages: list[int]) -> OcrResult: ...

`pages` are 1-based page numbers (an image is page 1). It returns the text of
each page it read, and the regions (text, box, confidence) where the engine
gives them. It raises OcrUnavailable when the engine cannot run (not
installed, a service down): the tool retries that, then answers
ocr_unavailable, and the document is kept with those pages marked "no usable
text". An engine's own dependency goes behind an extra (hard rule 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class OcrUnavailable(Exception):
    """The engine could not run. Not the same as a page with no text on it."""


@dataclass(frozen=True)
class Region:
    text: str
    # (x0, y0, x1, y1) as fractions of the page's width and height, 0..1
    box: tuple[float, float, float, float]
    confidence: float | None = None       # 0..1, when the engine gives one


@dataclass(frozen=True)
class OcrPage:
    number: int                           # 1-based
    text: str
    regions: tuple[Region, ...] = ()
    confidence: float | None = None


@dataclass(frozen=True)
class OcrResult:
    engine: str
    pages: tuple[OcrPage, ...] = field(default_factory=tuple)


class OcrBackend(Protocol):
    name: str

    def recognise(self, data: bytes, mime: str, pages: list[int]) -> OcrResult: ...
