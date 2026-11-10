"""Serving the archived tender documents behind a purchase-case reference.

When a procurement officer reads "PUR-MNTPLN-1005566" in a NAM or NiT
conversation, the useful next action is to open the notice and check it. That
is what makes a citation verifiable rather than merely quotable — and this
whole system's credibility rests on citations that can be checked.

The knowledge graph knows where every file lives (FileOccurrence.original_path,
19,000+ of them across the corpus); this module turns that into bytes. Nothing
is generated, converted or summarised: the officer gets the file NRL issued.

SAFETY
------
The case reference arrives from a URL, and the response is a file read from a
shared corpus. So the path is never built from the reference — it comes back
from the graph, and is then checked to be inside the corpus root before it is
opened. A reference that resolves outside the corpus is refused, whatever the
graph says.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

_TIMEOUT = 120.0

# Only the corpus is servable. Overridable because the corpus root is
# deployment-specific, but never empty: an unset root would make every path
# "inside" it.
CORPUS_ROOTS = tuple(
    Path(p).resolve() for p in
    os.getenv("NRL_CORPUS_ROOTS", "/data2/nrl_dev/nrl_kg_data").split(",")
    if p.strip()
)

_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

# Never serve more than this in one response. The largest notice in the corpus
# is around 10 MB; a 200 MB archive would be a denial of service, not a preview.
MAX_BYTES = 64 * 1024 * 1024


def _get(kg_url: str, path: str, params: dict) -> dict:
    url = f"{kg_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


def inside_corpus(path: Path) -> bool:
    """True only if `path` really sits under a configured corpus root.

    Resolved first, so a path containing .. cannot climb out of the corpus and
    a symlink cannot point somewhere else entirely.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in CORPUS_ROOTS)


def documents(case_key: str, kg_url: str) -> dict:
    """What this case holds, for the preview pane's file list."""
    if not case_key:
        return {"error": "case_key is required", "documents": []}
    if not kg_url:
        return {"case_key": case_key, "documents": [],
                "error": "No knowledge graph configured (NAM_KG_URL)."}
    try:
        data = _get(kg_url, "/api/kg/documents", {"case_key": case_key})
    except Exception as exc:  # noqa: BLE001
        return {"case_key": case_key, "documents": [],
                "error": f"Knowledge graph unreachable: {str(exc)[:120]}"}

    # Paths are internal. The UI gets names, types and sizes — enough to choose
    # a file — and fetches bytes back through this service.
    out = []
    for doc in data.get("documents") or []:
        out.append({
            "document_type": doc.get("document_type"),
            "filename": doc.get("filename"),
            "extension": doc.get("extension"),
            "byte_size": doc.get("byte_size"),
            "available": bool(doc.get("readable")) and inside_corpus(Path(str(doc.get("path")))),
        })
    best = data.get("best_nit")
    return {
        "case_key": case_key,
        "count": len(out),
        "documents": out,
        "has_notice": bool(best),
        "notice": ({"filename": best.get("filename"),
                    "document_type": best.get("document_type"),
                    "extension": best.get("extension"),
                    "byte_size": best.get("byte_size")} if best else None),
    }


def best_notice(case_key: str, kg_url: str) -> tuple[bytes, str, str] | None:
    """(bytes, content_type, filename) for this case's notice, or None.

    None covers every way this can legitimately not produce a document — no
    such case, no NIT among its files, the file moved off the share, or a path
    that does not sit inside the corpus. The caller renders one honest "no
    notice on file" state for all of them.
    """
    if not case_key or not kg_url:
        return None
    try:
        data = _get(kg_url, "/api/kg/documents", {"case_key": case_key})
    except Exception:  # noqa: BLE001
        return None

    best = data.get("best_nit")
    if not best or not best.get("path"):
        return None

    path = Path(str(best["path"]))
    if not inside_corpus(path) or not path.is_file():
        return None
    if path.stat().st_size > MAX_BYTES:
        return None

    extension = (best.get("extension") or path.suffix.lstrip(".")).lower()
    return (path.read_bytes(),
            _CONTENT_TYPES.get(extension, "application/octet-stream"),
            best.get("filename") or path.name)
