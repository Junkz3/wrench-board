"""Unit tests for the .env → os.environ bootstrap loader.

The board parsers (notably the XZZ engine) read their decryption keys straight
from os.environ at import time, but pydantic-settings only populates the Settings
object, not os.environ. `api.env_bootstrap.load_env_file` bridges that gap.
"""

from __future__ import annotations

import os

import pytest

from api.env_bootstrap import _parse_env, load_env_file


def test_parse_basic_key_value():
    assert _parse_env("FOO=bar\nBAZ=qux") == {"FOO": "bar", "BAZ": "qux"}


def test_parse_ignores_comments_and_blank_lines():
    text = "# a comment\n\nFOO=bar\n   \n# another\nBAZ=qux\n"
    assert _parse_env(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_keeps_value_with_equals_sign():
    # Split on the FIRST '=' only — base64 / padded keys often contain '='.
    assert _parse_env("KEY=aGVsbG8=world=")["KEY"] == "aGVsbG8=world="


def test_parse_strips_surrounding_quotes():
    assert _parse_env('A="quoted"\nB=\'single\'\nC=bare') == {
        "A": "quoted",
        "B": "single",
        "C": "bare",
    }


def test_parse_handles_export_prefix():
    assert _parse_env("export FOO=bar") == {"FOO": "bar"}


def test_load_setdefaults_without_overriding_real_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env = tmp_path / ".env"
    env.write_text("WB_TEST_NEW=fromfile\nWB_TEST_EXISTING=fromfile\n", encoding="utf-8")
    # One key already set in the real environment must NOT be overridden.
    monkeypatch.setenv("WB_TEST_EXISTING", "fromenv")
    monkeypatch.delenv("WB_TEST_NEW", raising=False)

    applied = load_env_file(env)

    assert os.environ["WB_TEST_NEW"] == "fromfile"      # new key applied
    assert os.environ["WB_TEST_EXISTING"] == "fromenv"  # real env wins
    assert applied == 1


def test_load_missing_file_is_noop(tmp_path):
    assert load_env_file(tmp_path / "does-not-exist.env") == 0
