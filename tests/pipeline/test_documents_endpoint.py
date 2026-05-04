"""Tests for POST /pipeline/packs/{slug}/documents — multipart upload of
technician-supplied documents (schematic / boardview / datasheet / notes /
other) into memory/{slug}/uploads/."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_post_document_writes_file_to_uploads(memory_root: Path, client: TestClient) -> None:
    res = client.post(
        "/pipeline/packs/demo-board/documents",
        data={"kind": "datasheet", "description": "LM2677 datasheet"},
        files={"file": ("lm2677.pdf", b"%PDF-1.4 stub", "application/pdf")},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["device_slug"] == "demo-board"
    assert body["kind"] == "datasheet"
    assert body["filename"] == "lm2677.pdf"
    assert body["size_bytes"] == len(b"%PDF-1.4 stub")

    uploads_dir = memory_root / "demo-board" / "uploads"
    persisted = list(uploads_dir.glob("*-datasheet-lm2677.pdf"))
    assert len(persisted) == 1
    assert persisted[0].read_bytes() == b"%PDF-1.4 stub"

    # Description sidecar present.
    sidecars = list(uploads_dir.glob("*.description.txt"))
    assert len(sidecars) == 1
    assert sidecars[0].read_text(encoding="utf-8") == "LM2677 datasheet"


def test_post_document_rejects_unknown_kind(memory_root: Path, client: TestClient) -> None:
    res = client.post(
        "/pipeline/packs/demo-board/documents",
        data={"kind": "rogue"},
        files={"file": ("foo.pdf", b"x", "application/pdf")},
    )
    assert res.status_code == 422
    assert "rogue" in res.text


def test_post_document_rejects_invalid_slug(memory_root: Path, client: TestClient) -> None:
    """Slugs that don't match the canonical kebab-case pattern must 422
    before any disk write — defense in depth on the directory name we
    pass to `mkdir`."""
    # The validator allows `[a-z0-9][a-z0-9._-]*` and rejects `..` sequences.
    res = client.post(
        "/pipeline/packs/Bad..slug/documents",
        data={"kind": "notes"},
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert res.status_code == 422
    # No directory should have been created for the rogue slug.
    assert not (memory_root / "Bad..slug").exists()


def test_post_document_sanitizes_filename(memory_root: Path, client: TestClient) -> None:
    """A filename containing path separators or shell metacharacters must
    not escape the uploads dir."""
    res = client.post(
        "/pipeline/packs/demo-board/documents",
        data={"kind": "notes"},
        files={"file": ("../../etc/passwd", b"hostile", "text/plain")},
    )
    assert res.status_code == 201
    persisted = list((memory_root / "demo-board" / "uploads").iterdir())
    persisted_names = [p.name for p in persisted if p.is_file()]
    assert all(".." not in n for n in persisted_names)
    assert all("/" not in n for n in persisted_names)


def test_list_documents_returns_grouped_metadata(
    memory_root: Path, client: TestClient
) -> None:
    client.post(
        "/pipeline/packs/demo-board/documents",
        data={"kind": "schematic_pdf"},
        files={"file": ("a.pdf", b"%PDF a", "application/pdf")},
    )
    client.post(
        "/pipeline/packs/demo-board/documents",
        data={"kind": "datasheet"},
        files={"file": ("b.pdf", b"%PDF b", "application/pdf")},
    )
    res = client.get("/pipeline/packs/demo-board/documents")
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == "demo-board"
    assert {item["kind"] for item in body["uploads"]} == {"schematic_pdf", "datasheet"}
    # Sidecars must NOT appear in the listing.
    assert all(not item["name"].endswith(".description.txt") for item in body["uploads"])


def test_list_documents_empty_when_no_uploads(
    memory_root: Path, client: TestClient
) -> None:
    res = client.get("/pipeline/packs/never-uploaded/documents")
    assert res.status_code == 200
    assert res.json() == {"device_slug": "never-uploaded", "uploads": []}
