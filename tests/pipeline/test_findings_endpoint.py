"""Tests for GET /pipeline/packs/{device_slug}/findings."""

from __future__ import annotations

from pathlib import Path


def _write_report(
    memory_root: Path,
    *,
    slug: str,
    report_id: str,
    refdes: str,
    symptom: str,
    confirmed_cause: str,
    created_at: str,
    session_id: str | None = None,
) -> None:
    reports_dir = memory_root / slug / "field_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"report_id: {report_id}",
        f"device_slug: {slug}",
        f"refdes: {refdes}",
        f'symptom: "{symptom}"',
        f'confirmed_cause: "{confirmed_cause}"',
    ]
    if session_id:
        lines.append(f"session_id: {session_id}")
    lines.append(f"created_at: {created_at}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {refdes} — {confirmed_cause}")
    lines.append("")
    lines.append(f"**Symptom observed:** {symptom}")
    lines.append("")
    (reports_dir / f"{report_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_findings_returns_empty_list_for_unknown_device(memory_root, client):
    resp = client.get("/pipeline/packs/does-not-exist/findings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_findings_returns_empty_list_when_no_reports(memory_root, client):
    (memory_root / "demo-device").mkdir(parents=True)
    resp = client.get("/pipeline/packs/demo-device/findings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_findings_returns_reports_newest_first(memory_root, client):
    slug = "demo-device"
    _write_report(
        memory_root,
        slug=slug,
        report_id="2026-03-01-u12",
        refdes="U12",
        symptom="no-boot",
        confirmed_cause="cold joint",
        created_at="2026-03-01T10:00:00+00:00",
    )
    _write_report(
        memory_root,
        slug=slug,
        report_id="2026-03-02-q7",
        refdes="Q7",
        symptom="brownout",
        confirmed_cause="gate short",
        created_at="2026-03-02T10:00:00+00:00",
        session_id="abc12345",
    )

    resp = client.get(f"/pipeline/packs/{slug}/findings")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Newest first — Q7 was created 2026-03-02, U12 on 2026-03-01.
    assert body[0]["refdes"] == "Q7"
    assert body[0]["session_id"] == "abc12345"
    assert body[0]["confirmed_cause"] == "gate short"
    assert body[1]["refdes"] == "U12"
    assert body[1]["session_id"] is None


def test_findings_rejects_bad_slug(memory_root, client):
    resp = client.get("/pipeline/packs/bad..slug/findings")
    assert resp.status_code in (400, 422)
