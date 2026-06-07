import io

import pytest
from fastapi import HTTPException, UploadFile

from api.pipeline.routes.documents import persist_upload


async def test_persist_upload_writes_timestamped_kind_file(tmp_path):
    uploads = tmp_path / "uploads"
    up = UploadFile(filename="My Schematic.pdf", file=io.BytesIO(b"%PDF-1.4 fake"))
    target, total = await persist_upload(uploads, "schematic_pdf", up)
    assert target.parent == uploads
    assert "-schematic_pdf-" in target.name
    assert target.name.endswith("-My-Schematic.pdf")
    assert target.read_bytes() == b"%PDF-1.4 fake"
    assert total == len(b"%PDF-1.4 fake")


async def test_persist_upload_enforces_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("api.pipeline.routes.documents._MAX_UPLOAD_BYTES", 4)
    up = UploadFile(filename="big.pdf", file=io.BytesIO(b"way too many bytes"))

    with pytest.raises(HTTPException) as ei:
        await persist_upload(tmp_path / "uploads", "schematic_pdf", up)
    assert ei.value.status_code == 413
