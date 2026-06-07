from api.pipeline.pack_lint import lint_pack
from api.pipeline.schemas import DeviceTaxonomy, Registry


def test_lint_flags_mixed_kind_rule_text():
    findings = lint_pack(
        registry=Registry(device_label="x", taxonomy=DeviceTaxonomy(device_kind="gpu_card")),
        rules_text="Probe VIN-19V at the barrel-jack (laptop) or 12V PCIe input (GPU).",
        graph_rails={"12V_PEX"},
    )
    assert any(f.code == "mixed_kind_rule" for f in findings)


def test_lint_flags_phantom_rail():
    findings = lint_pack(
        registry=Registry(device_label="x", taxonomy=DeviceTaxonomy(device_kind="gpu_card")),
        rules_text="Verify VAUX_1V8 is present.",
        graph_rails={"12V_PEX", "NVVDD"},
    )
    assert any(f.code == "phantom_rail" and "VAUX_1V8" in f.detail for f in findings)


def test_lint_flags_unknown_kind_with_graph():
    findings = lint_pack(
        registry=Registry(device_label="x", taxonomy=DeviceTaxonomy(device_kind="unknown")),
        rules_text="", graph_rails={"NVVDD"},
    )
    assert any(f.code == "unknown_kind_with_graph" for f in findings)


def test_lint_clean_pack_no_findings():
    findings = lint_pack(
        registry=Registry(device_label="x", taxonomy=DeviceTaxonomy(device_kind="gpu_card")),
        rules_text="Probe 12V_PEX at the PCIe connector.",
        graph_rails={"12V_PEX"},
    )
    assert findings == []


def test_lint_no_graph_rails_yields_no_phantom_or_graph_findings():
    findings = lint_pack(
        registry=Registry(device_label="x", taxonomy=DeviceTaxonomy(device_kind="unknown")),
        rules_text="Verify VAUX_1V8 is present.",
        graph_rails=None,
    )
    # phantom_rail requires a graph; unknown_kind_with_graph requires a graph.
    assert not any(f.code in ("phantom_rail", "unknown_kind_with_graph") for f in findings)
