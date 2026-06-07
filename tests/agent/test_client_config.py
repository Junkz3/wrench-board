"""Tests that every AsyncAnthropic instantiation site carries max_retries>=5.

This ensures the codebase survives short Anthropic overload windows (HTTP 529)
without crashing the pipeline or diagnostic sessions.
"""

from unittest.mock import patch


def test_pipeline_client_has_elevated_max_retries():
    """Pipeline AsyncAnthropic client must have max_retries>=5 to survive Anthropic overload."""
    from api.pipeline.orchestrator import _get_client

    with patch("api.pipeline.orchestrator.get_settings") as m:
        m.return_value.anthropic_api_key = "sk-test"
        m.return_value.anthropic_max_retries = 5
        client = _get_client()
    assert client.max_retries >= 5, (
        f"expected max_retries >= 5 for overload resilience, got {client.max_retries}"
    )


def test_all_instantiation_sites_have_max_retries():
    """Grep-level sanity check: every AsyncAnthropic() call in api/ carries max_retries=."""
    import subprocess

    result = subprocess.run(
        [
            "grep",
            "-rn",
            "AsyncAnthropic(",
            "api/",
            "--include=*.py",
        ],
        capture_output=True,
        text=True,
    )
    lines = result.stdout.strip().splitlines()
    missing = [
        line
        for line in lines
        if "AsyncAnthropic(" in line and "max_retries" not in line
    ]
    assert not missing, (
        "The following AsyncAnthropic() instantiations are missing max_retries:\n"
        + "\n".join(missing)
    )
