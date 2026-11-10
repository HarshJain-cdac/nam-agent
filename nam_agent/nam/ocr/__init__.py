"""Which OCR engine the ocr_document tool uses, if any (plan D8).

NAM_OCR_BACKEND names it as "module:Class", for example
"mypkg.ocr:TesseractBackend". The class is created once, with no arguments.
Unset, there is no OCR: a document whose pages have no usable text is still
stored, and those pages are marked "no usable text". No engine is built in.
"""

from __future__ import annotations

import importlib
import logging
import os
from functools import cache

from nam_agent.nam.ocr.base import OcrBackend, OcrPage, OcrResult, OcrUnavailable, Region

__all__ = ["OcrBackend", "OcrPage", "OcrResult", "OcrUnavailable", "Region", "configured"]

log = logging.getLogger("nam_agent.nam.ocr")


@cache
def _load(spec: str) -> OcrBackend:
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise OcrUnavailable(f"NAM_OCR_BACKEND must be 'module:Class', not {spec!r}")
    try:
        backend = getattr(importlib.import_module(module_name), class_name)()
    except Exception as exc:  # noqa: BLE001 - a broken engine is "OCR unavailable", loudly
        log.error("OCR backend %s could not be loaded: %s", spec, exc)
        raise OcrUnavailable(f"the OCR backend {spec} could not be loaded "
                             f"({type(exc).__name__})") from None
    if not callable(getattr(backend, "recognise", None)):
        raise OcrUnavailable(f"the OCR backend {spec} has no recognise() method")
    return backend


def configured(spec: str | None = None) -> OcrBackend | None:
    """The configured backend, or None. Raises OcrUnavailable when one is
    named but cannot be loaded, so a typo is never taken for "no OCR"."""
    spec = (spec if spec is not None else os.getenv("NAM_OCR_BACKEND", "")).strip()
    return _load(spec) if spec else None
