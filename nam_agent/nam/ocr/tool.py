"""The ocr_document tool: text for the pages of a stored document that
normal extraction could not read (plan D8).

It is a read tool like the KG contracts (nam_agent/nam/tools.py): checked input
and output, retried once while the engine is unavailable, then
ocr_unavailable, and one audit row per call. It reads the bytes by their
hash from the acting tenant's own store, so it cannot reach another tenant's
file. Its text is marked untrusted, like any document text.
"""

from __future__ import annotations

from nam_agent.nam import store
from nam_agent.nam.contracts.base import READ, Unavailable

_FRACTION = {"type": "number", "minimum": 0, "maximum": 1}
_CONFIDENCE = {"type": ["number", "null"], "minimum": 0, "maximum": 1}

INPUT = {
    "type": "object", "additionalProperties": False, "required": ["sha256", "mime", "pages"],
    "properties": {
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "mime": {"enum": ["application/pdf", "image/png", "image/jpeg", "image/tiff"]},
        "pages": {"type": "array", "minItems": 1, "maxItems": 500,
                  "items": {"type": "integer", "minimum": 1, "maximum": 500}},
    },
}
OUTPUT = {
    "type": "object", "additionalProperties": False, "required": ["engine", "untrusted", "pages"],
    "properties": {
        "engine": {"type": "string", "minLength": 1, "maxLength": 100},
        "untrusted": {"const": True},
        "pages": {"type": "array", "maxItems": 500, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["number", "text", "confidence", "regions"],
            "properties": {
                "number": {"type": "integer", "minimum": 1},
                "text": {"type": "string", "maxLength": 100000},
                "confidence": _CONFIDENCE,
                "regions": {"type": "array", "maxItems": 5000, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["text", "box", "confidence"],
                    "properties": {
                        "text": {"type": "string", "maxLength": 5000},
                        # x0, y0, x1, y1 as fractions of the page
                        "box": {"type": "array", "minItems": 4, "maxItems": 4,
                                "items": _FRACTION},
                        "confidence": _CONFIDENCE}}},
            }}},
    },
}

DESCRIPTION = ("Read the text of document pages that have no usable text layer (a scan, a "
               "photo), with page and region metadata. Called only for those pages.")


def make(ctx):
    from nam_agent.nam import ocr
    from nam_agent.nam.docs import store as docs
    from nam_agent.nam.tools import NamTool

    def ocr_document(sha256: str, mime: str, pages: list[int]) -> dict:
        try:
            backend = ctx.ocr if getattr(ctx, "ocr", None) is not None else ocr.configured()
        except ocr.OcrUnavailable as exc:
            raise Unavailable(str(exc)) from None
        if backend is None:
            raise Unavailable("no OCR backend is configured (NAM_OCR_BACKEND)")
        data = docs.blob_for(ctx, store.current().tenant, sha256)
        try:
            result = backend.recognise(data, mime, list(pages))
        except ocr.OcrUnavailable as exc:
            raise Unavailable(f"the OCR engine could not run: {exc}") from None
        wanted = set(pages)
        return {
            "engine": str(result.engine or backend.name)[:100], "untrusted": True,
            "pages": [{"number": p.number, "text": p.text, "confidence": p.confidence,
                       "regions": [{"text": r.text, "box": list(r.box),
                                    "confidence": r.confidence} for r in p.regions]}
                      for p in result.pages if p.number in wanted],
        }

    return NamTool("ocr_document", DESCRIPTION, READ, INPUT, OUTPUT, ocr_document,
                   timeout_s=300.0, retries=1, unavailable_code="ocr_unavailable")
