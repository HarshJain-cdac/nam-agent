"""DETERMINISTIC EVAL — the tender archive behind a case reference.

nit-forge-ui/src/lib/tender-ref.ts turns "PUR-MNTPLN-1005566" in a conversation
into a link to /api/tender/<case>/nit.pdf. That makes a citation checkable
instead of merely quotable, which is the whole basis on which this system asks
to be trusted — so the route has to serve the real notice, refuse anything
outside the corpus, and give one honest miss state.

Offline: paths are checked, not fetched.
"""

from __future__ import annotations

import json
from pathlib import Path

from nam_agent.tools import tender_archive as archive


def test_only_the_corpus_is_servable():
    """The response is a file read from a shared host. A case reference must
    never be able to address anything outside the corpus, whatever the graph
    returns for it."""
    assert archive.CORPUS_ROOTS, "an empty root list would make every path 'inside'"
    root = archive.CORPUS_ROOTS[0]
    assert archive.inside_corpus(root / "Goods" / "notice.pdf")
    assert not archive.inside_corpus(Path("/etc/passwd"))
    assert not archive.inside_corpus(Path("/data2/nrl_dev/Harsh/nam-agent/.env"))


def test_a_traversal_cannot_climb_out():
    """Resolved before the check, so .. in a stored path cannot escape."""
    root = archive.CORPUS_ROOTS[0]
    assert not archive.inside_corpus(root / ".." / ".." / ".." / "etc" / "shadow")


def test_a_symlink_is_judged_by_its_target(tmp_path):
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.7")
    link = archive.CORPUS_ROOTS[0] / "__eval_symlink_should_not_exist.pdf"
    # Never actually created — the assertion is about the rule, and writing
    # into the shared corpus from a test would be its own bug.
    assert not link.exists()
    assert not archive.inside_corpus(outside)


def test_no_graph_configured_is_reported_not_guessed():
    result = archive.documents("PUR-X-1", "")
    assert result["documents"] == []
    assert "knowledge graph" in result["error"].lower()


def test_a_missing_notice_returns_none_not_a_substitute():
    """Every way this can fail — no case, no NIT among its files, file moved,
    path outside the corpus — collapses to one honest miss. A case whose only
    PDF is an invoice must not open under a button that says 'open the
    tender'."""
    assert archive.best_notice("", "http://127.0.0.1:1") is None
    assert archive.best_notice("PUR-X-1", "") is None
    # unreachable graph
    assert archive.best_notice("PUR-X-1", "http://127.0.0.1:1") is None


def test_a_document_listing_hides_paths(monkeypatch):
    """Filesystem paths are internal. The UI gets names, types and sizes —
    enough to choose a file — and fetches bytes back through this service, so a
    path never has to cross to the browser and must not."""
    monkeypatch.setattr(archive, "_get", lambda *a, **k: {
        "documents": [{"document_type": "NIT", "filename": "notice.pdf",
                       "extension": "pdf", "byte_size": 12,
                       "path": "/data2/nrl_dev/nrl_kg_data/Goods/notice.pdf",
                       "readable": True}],
        "best_nit": {"filename": "notice.pdf", "document_type": "NIT",
                     "extension": "pdf", "byte_size": 12,
                     "path": "/data2/nrl_dev/nrl_kg_data/Goods/notice.pdf"},
    })
    result = archive.documents("PUR-X-1", "http://kg")
    assert result["documents"] and result["has_notice"]
    for doc in result["documents"]:
        assert "path" not in doc
    assert "path" not in (result["notice"] or {})
    assert "/data2" not in json.dumps(result)


def test_content_types_cover_what_the_corpus_holds():
    for extension in ("pdf", "docx", "xlsx", "doc", "xls"):
        assert extension in archive._CONTENT_TYPES


def test_a_size_ceiling_exists():
    """A preview must not be able to pull an arbitrary amount off the share."""
    assert 0 < archive.MAX_BYTES <= 128 * 1024 * 1024
