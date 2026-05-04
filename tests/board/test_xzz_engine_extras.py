"""Tests for the XZZ post-v6 diagnostic extractor.

The extractor pulls manufacturer-tagged resistance / voltage / signal
expectations out of the GB2312-marked sections that follow the
`v6v6555v6v6===` pattern in manufacturer-tagged XZZ dumps. The
in-tree engine recognises the markers but its
`_parse_resistance_section` expects a different line shape than what's
actually in the wild — see the module docstring for the layout.
"""
from __future__ import annotations

import struct

from api.board.parser._xzz_engine_extras import (
    _parse_resistance_payload,
    _parse_voltage_payload,
    _parse_signal_payload,
    _parse_menu_payload,
    extract_post_v6_diagnostics,
)


def test_resistance_payload_parses_integer_ohms():
    text = "Net204=621\nNet205=613\nNet206=0\n"
    out = _parse_resistance_payload(text)
    assert out["Net204"] == {"expected_resistance_ohms": 621.0, "expected_open": False}
    # Zero is a meaningful value — short / ground rail — and must not
    # collapse into expected_open.
    assert out["Net206"] == {"expected_resistance_ohms": 0.0, "expected_open": False}


def test_resistance_payload_marks_open_for_OL_token():
    # Three OL conventions seen in manufacturer-tagged dumps.
    text = "Net1=>1000或OL\nNet2=OL\nNet3=开路\n"
    out = _parse_resistance_payload(text)
    for net in ("Net1", "Net2", "Net3"):
        assert out[net]["expected_open"] is True
        assert out[net]["expected_resistance_ohms"] is None


def test_resistance_payload_skips_empty_and_malformed():
    text = "Net1=\nNet2=garbage\nfoo=42\nNet3=12\n"
    out = _parse_resistance_payload(text)
    # Only Net3 has a valid numeric value; the others are skipped.
    assert set(out.keys()) == {"Net3"}
    assert out["Net3"]["expected_resistance_ohms"] == 12.0


def test_voltage_payload_extracts_floats():
    text = "Net100=3.3\nNet101=1.8\nNet102=0\n"
    out = _parse_voltage_payload(text)
    assert out == {"Net100": 3.3, "Net101": 1.8, "Net102": 0.0}


def test_signal_payload_resolves_placeholder_to_real_name():
    text = "Net204=PP_VBAT\nNet205=PP3V3_G3H\nbogus_line\n"
    out = _parse_signal_payload(text)
    assert out == {"Net204": "PP_VBAT", "Net205": "PP3V3_G3H"}


def test_menu_payload_parses_json_object():
    out = _parse_menu_payload('{"steps": ["check vbat", "probe pp3v3"]}')
    assert out == {"steps": ["check vbat", "probe pp3v3"]}


def test_menu_payload_falls_back_to_raw_string_on_invalid_json():
    out = _parse_menu_payload("not really json {still useful")
    assert out == "not really json {still useful"


def test_menu_payload_returns_none_for_empty():
    assert _parse_menu_payload("") is None
    assert _parse_menu_payload("   \n\n  ") is None


def _set_test_xzz_key(monkeypatch):
    """Set a synthetic 8-byte DES key so the XZZ decryptor can run.

    The key value is irrelevant for these synthesized buffers — they
    don't carry encrypted PART blocks, only the post-v6 plain section
    we care about. The DES key is loaded from `WRENCH_BOARD_XZZ_KEY`
    at parse time; without it the engine refuses to start.
    """
    monkeypatch.setenv("WRENCH_BOARD_XZZ_KEY", "0011223344556677")
    import api.board.parser._xzz_engine.xzz_file as xf
    monkeypatch.setattr(xf.XZZFile, "MASTER_KEY", "0011223344556677")


def test_extract_returns_empty_on_buffer_without_post_v6(monkeypatch):
    _set_test_xzz_key(monkeypatch)
    # Random bytes that don't contain the base pattern. Decryption may
    # still succeed (XZZ XOR descramble accepts arbitrary buffers); the
    # extractor must return an empty dict, not raise.
    buf = b"\x00" * 256
    out = extract_post_v6_diagnostics(buf)
    assert out == {}


def test_extract_handles_synthesized_resistance_block(monkeypatch):
    _set_test_xzz_key(monkeypatch)
    # Build a minimal buffer carrying the base pattern + resistance
    # marker + a few Net lines. We're testing the section-walker, not
    # the XOR decryptor, so the buffer just needs to survive the
    # decrypt path (zeros pass through untouched).
    base = b"v6v6555v6v6==="
    marker = bytes.fromhex("D7E8D6B5")  # 阻值
    body = "图\r\nNet1=621\r\nNet2=>1000或OL\r\nNet3=0\r\n".encode("gb2312")
    # Pad before and after the pattern so the buffer is large enough
    # for the XZZ decryptor's bounds checks (it indexes into known
    # offsets to detect the XOR scramble pattern). Real boards are
    # always >> 1 KB; this keeps the test buffer in that ballpark
    # without needing a real fixture.
    buf = b"\x00" * 4096 + base + marker + body + b"\x00" * 4096
    out = extract_post_v6_diagnostics(buf)
    assert "resistance" in out
    r = out["resistance"]
    assert r["Net1"]["expected_resistance_ohms"] == 621.0
    assert r["Net2"]["expected_open"] is True
    assert r["Net3"]["expected_resistance_ohms"] == 0.0
