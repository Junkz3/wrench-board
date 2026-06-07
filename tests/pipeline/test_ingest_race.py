"""T9 — sérialisation per-slug de l'ingestion managée (cache-miss).

Le bug (revue qualité) : en managé cache-miss, deux uploads de PDF *différents*
sur le MÊME slug utilisent la racine (`memory/{slug}/schematic.pdf`) comme
scratch d'ingestion partagé, sans aucune garde. La séquence est
clear_in_place + copyfile(PDF→racine) + create_task(_reingest_and_cache(hash)).

Sans sérialisation, le tenant A (PDF-A, hash hA) et le tenant B (PDF-B, hash hB)
peuvent s'entrelacer : le moment où la tâche de fond de hA *lit* enfin
`racine/schematic.pdf` pour l'ingérer, B l'a déjà écrasé avec PDF-B →
`write_through_cache(hA)` snapshote le graphe de B dans le slot hA.
Corruption cross-tenant silencieuse.

Ces tests verrouillent l'invariant :
  write_through_cache(H) ne snapshote QUE les artefacts du PDF de hash H.
"""

import asyncio
import json
from pathlib import Path

import pytest

from api.pipeline import sources
from api.pipeline.routes import documents as docs


def _seed_upload(pack: Path, filename: str, content: bytes) -> str:
    (pack / "uploads").mkdir(parents=True, exist_ok=True)
    (pack / "uploads" / filename).write_bytes(content)
    return sources.hash_pdf(pack / "uploads" / filename)


@pytest.mark.asyncio
async def test_managed_concurrent_cache_miss_no_cross_tenant_corruption(
    tmp_path, monkeypatch
):
    """Deux cache-miss managés concurrents (PDF différents, même slug) ne doivent
    PAS contaminer mutuellement leurs slots de cache hashés.

    On force l'entrelacement le plus hostile : on lâche A en premier mais on le
    bloque AVANT qu'il lise la racine ; B passe alors entièrement (clear+copy),
    écrasant la racine avec PDF-B. Si l'ingestion de A n'est pas sérialisée, A
    lira PDF-B → corruption. Avec la garde, B attend que A termine.
    """
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    settings = docs._pkg.get_settings()
    monkeypatch.setattr(settings, "memory_root", str(tmp_path), raising=False)
    # ingest_schematic exige une clé API ; on stube tout, donc une valeur factice suffit.
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)

    hA = _seed_upload(pack, "20260529-schematic_pdf-A.pdf", b"%PDF AAAA")
    hB = _seed_upload(pack, "20260529-schematic_pdf-B.pdf", b"%PDF BBBB")
    assert hA != hB

    gate_a = asyncio.Event()  # le test libère A pour qu'il finisse de lire la racine
    a_reached = asyncio.Event()  # A signale qu'il est entré dans l'ingest et attend

    async def fake_ingest(*, device_slug, pdf_path, client, memory_root, **kw):
        # Lit l'octet de la racine AU MOMENT de l'ingestion (comme le vrai pipeline)
        root_pdf = Path(memory_root) / device_slug / "schematic.pdf"
        read_bytes = root_pdf.read_bytes()
        # Écrit le graphe dérivé à la racine en se basant sur ce qu'on a *réellement* lu.
        (Path(memory_root) / device_slug / "schematic_graph.json").write_text("{}")
        (Path(memory_root) / device_slug / "electrical_graph.json").write_text(
            json.dumps({"read": read_bytes.decode("latin-1")})
        )
        # Premier appel (A) : on attend que le test ait laissé B s'exécuter.
        if read_bytes == b"%PDF AAAA" or not a_reached.is_set():
            a_reached.set()
            await gate_a.wait()
        return None

    monkeypatch.setattr(docs._pkg, "ingest_schematic", fake_ingest)

    # Évite un vrai client réseau.
    class _FakeClient:
        pass

    monkeypatch.setattr(docs, "AsyncAnthropic", lambda **kw: _FakeClient())

    # Lance A (cache-miss). Le helper démarre une tâche de fond sérialisée.
    docs._apply_schematic_pin(
        "iphone-x", pack, "20260529-schematic_pdf-A.pdf", owner_ref="tenant-A"
    )
    # Laisse A entrer dans l'ingest et se bloquer sur gate_a.
    await asyncio.wait_for(a_reached.wait(), timeout=2.0)

    # Maintenant B (cache-miss du même slug). Avec la garde, B est CHAÎNÉ après A :
    # il ne doit PAS écraser la racine tant que A n'a pas fini.
    docs._apply_schematic_pin(
        "iphone-x", pack, "20260529-schematic_pdf-B.pdf", owner_ref="tenant-B"
    )
    # Laisse l'event loop tourner un peu — sans garde, B clobbe la racine ici.
    await asyncio.sleep(0.05)

    # Libère A : il lit la racine et fait write_through_cache(hA).
    gate_a.set()

    # Attend la fin de TOUTES les tâches de fond (les ingests). On draine via
    # asyncio.all_tasks pour rester agnostique de l'implémentation de la garde
    # (fonctionne contre le code actuel SANS garde comme contre le code corrigé).
    me = asyncio.current_task()
    for _ in range(40):
        others = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not others:
            break
        await asyncio.wait_for(asyncio.gather(*others, return_exceptions=True), timeout=5.0)

    # Invariant : chaque slot de hash contient le graphe de SON propre PDF.
    cache_a = sources.cache_dir_for(pack, hA) / "electrical_graph.json"
    cache_b = sources.cache_dir_for(pack, hB) / "electrical_graph.json"
    assert cache_a.exists(), "le slot hash de A doit avoir été écrit"
    assert cache_b.exists(), "le slot hash de B doit avoir été écrit"
    assert json.loads(cache_a.read_text())["read"] == "%PDF AAAA", (
        "CORRUPTION : le slot de A contient les octets d'un autre PDF"
    )
    assert json.loads(cache_b.read_text())["read"] == "%PDF BBBB", (
        "CORRUPTION : le slot de B contient les octets d'un autre PDF"
    )
