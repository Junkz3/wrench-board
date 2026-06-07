"""T8 — PackSanitizer : tests unitaires regex+listes.

Module pur. Pas d'I/O. Un test par pattern (positif + négatif + multi-occ).
"""

import pytest

from api.pipeline.pack_sanitizer import PackSanitizer


@pytest.fixture
def sanitizer():
    return PackSanitizer()


# ---- Email -------------------------------------------------------------


def test_email_simple_redacted(sanitizer):
    text, actions = sanitizer.sanitize_text("contact alice@example.com pour", field_name="notes")
    assert "alice@example.com" not in text
    assert "[REDACTED:email]" in text
    assert any(a.action == "redacted_email" and a.field == "notes" for a in actions)


def test_email_multiple_occurrences(sanitizer):
    text, actions = sanitizer.sanitize_text("a@b.fr et c@d.com", field_name="notes")
    assert text.count("[REDACTED:email]") == 2
    email_action = next(a for a in actions if a.action == "redacted_email")
    assert email_action.count == 2


def test_no_email_no_action(sanitizer):
    text, actions = sanitizer.sanitize_text("PP3V0 rail measured at 3.3V", field_name="notes")
    assert text == "PP3V0 rail measured at 3.3V"
    assert not any(a.action == "redacted_email" for a in actions)


# ---- Téléphone FR + international --------------------------------------


def test_phone_fr_format_redacted(sanitizer):
    cases = ["06 12 34 56 78", "+33 6 12 34 56 78", "0612345678", "06.12.34.56.78", "06-12-34-56-78"]
    for ph in cases:
        text, actions = sanitizer.sanitize_text(f"appeler le {ph}", field_name="notes")
        assert ph not in text, f"phone {ph!r} survived"
        assert "[REDACTED:phone]" in text


def test_phone_negative_does_not_match_refdes(sanitizer):
    """U1300 ne doit pas être pris pour un téléphone."""
    text, actions = sanitizer.sanitize_text("check U1300 pin 5", field_name="notes")
    assert "U1300" in text
    assert not any(a.action == "redacted_phone" for a in actions)


# ---- IMEI / numéro de série --------------------------------------------


def test_imei_15_digits_with_keyword(sanitizer):
    text, actions = sanitizer.sanitize_text("IMEI 350123456789012 défectueux", field_name="notes")
    assert "350123456789012" not in text
    assert any(a.action == "redacted_serial" for a in actions)


def test_serial_keyword_then_alphanum(sanitizer):
    text, actions = sanitizer.sanitize_text("S/N: F2LMQ1ABXY7G observed", field_name="notes")
    assert "F2LMQ1ABXY7G" not in text
    assert any(a.action == "redacted_serial" for a in actions)


def test_serial_negative_no_keyword(sanitizer):
    """Pas de mot-clé serial/imei/sn → pas de redact (on évite de redact U1300 ou PP3V0)."""
    text, actions = sanitizer.sanitize_text("check PP3V0 then U1300A", field_name="notes")
    assert "PP3V0" in text and "U1300A" in text
    assert not any(a.action == "redacted_serial" for a in actions)


# ---- IBAN --------------------------------------------------------------


def test_iban_fr_redacted(sanitizer):
    text, actions = sanitizer.sanitize_text("IBAN FR7630006000011234567890189", field_name="notes")
    assert "FR7630006000011234567890189" not in text
    assert any(a.action == "redacted_iban" for a in actions)


# ---- IP ----------------------------------------------------------------


def test_ipv4_redacted(sanitizer):
    text, _ = sanitizer.sanitize_text("server at 192.168.1.42", field_name="notes")
    assert "192.168.1.42" not in text
    assert "[REDACTED:ip]" in text


def test_ipv6_redacted(sanitizer):
    text, _ = sanitizer.sanitize_text("at 2001:0db8:85a3::8a2e:0370:7334 listening", field_name="notes")
    assert "2001:0db8" not in text


# ---- Mention client ----------------------------------------------------


def test_customer_mention_with_proper_noun(sanitizer):
    text, actions = sanitizer.sanitize_text(
        "Le client Dupont à Lyon dit que ça crashe.",
        field_name="symptoms[0]",
    )
    assert "Dupont" not in text
    assert any(a.action == "redacted_customer_mention" for a in actions)


def test_customer_keyword_alone_does_not_match(sanitizer):
    """Le mot 'client' seul (sans nom propre derrière) n'est pas redacté."""
    text, actions = sanitizer.sanitize_text("the client reports no charge", field_name="symptoms[0]")
    assert "client" in text.lower()
    assert not any(a.action == "redacted_customer_mention" for a in actions)


# ---- TI SN74xxx false-positive (Fix 1) ----------------------------------


def test_serial_does_not_redact_TI_SN_chip_prefix(sanitizer):
    """SN74HC595PWR (Texas Instruments) ne doit PAS être pris pour un SN+serial."""
    cases = [
        "check IC U3 (SN74HC595PWR) and measure VCC",
        "SN74AHCT1G08DCKR routes the clock",
        "Use SN74LVC8T245PW for level shifting.",
    ]
    for c in cases:
        text, actions = sanitizer.sanitize_text(c, field_name="notes")
        assert "SN74" in text, f"TI chip prefix was redacted in {c!r}"
        assert not any(a.action == "redacted_serial" for a in actions), f"false positive in {c!r}"


def test_serial_still_redacts_keyword_with_separator(sanitizer):
    """Vérifie que les vraies forms 'S/N: X' ou 'SN: X' restent redactées."""
    cases = [
        "S/N: F2LMQ1ABXY7G",
        "SN: F2LMQ1ABXY7G",
        "sn = F2LMQ1ABXY7G",
        "Serial F2LMQ1ABXY7G observed",
        "IMEI 350123456789012",
    ]
    for c in cases:
        text, actions = sanitizer.sanitize_text(c, field_name="notes")
        assert "F2LMQ1ABXY7G" not in text and "350123456789012" not in text, (
            f"true positive missed in {c!r}, got {text!r}"
        )


# ---- None handling (Fix 2) ----------------------------------------------


def test_sanitize_text_handles_none_explicitly(sanitizer):
    """sanitize_text(None) → (None, []) sans crasher."""
    out, actions = sanitizer.sanitize_text(None, field_name="optional_field")
    assert out is None
    assert actions == []


# ---- Long-string safety net --------------------------------------------


def test_long_string_truncated(sanitizer):
    """Champ libre > 500 chars : tronqué avec marqueur '[…truncated]'."""
    long = "abc " * 200  # 800 chars
    text, actions = sanitizer.sanitize_text(long, field_name="description")
    assert len(text) <= 500 + len("[…truncated]")
    assert text.endswith("[…truncated]")


# ---- sanitize_many -----------------------------------------------------


def test_sanitize_many_returns_list_and_combined_actions(sanitizer):
    inputs = ["alice@x.com a vu ça", "rien à signaler"]
    results, actions = sanitizer.sanitize_many(inputs, field_name="focus_symptoms")
    assert len(results) == 2
    assert "alice@x.com" not in results[0]
    assert results[1] == "rien à signaler"


# ---- Composition --------------------------------------------------------


def test_multiple_redactions_in_same_string(sanitizer):
    """Email + téléphone dans le même champ → 2 actions distinctes."""
    text, actions = sanitizer.sanitize_text(
        "écrire à alice@x.com ou appeler 06 12 34 56 78",
        field_name="notes",
    )
    assert "[REDACTED:email]" in text
    assert "[REDACTED:phone]" in text
    assert any(a.action == "redacted_email" for a in actions)
    assert any(a.action == "redacted_phone" for a in actions)
