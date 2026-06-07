"""Exception classes for the bench generator.

Split from the main module so they can be imported without triggering
the Pydantic + Anthropic import graph in downstream consumers (e.g. a
CLI that only wants to pretty-print a precondition failure).
"""


class BenchGeneratorError(Exception):
    """Base class. Catch this to catch all generator failures."""


class BenchGeneratorPreconditionError(BenchGeneratorError):
    """Raised before any LLM call when the pack inputs are insufficient.
    Exit code 2 in the CLI."""


class BenchGeneratorLLMError(BenchGeneratorError):
    """Raised after max_attempts retries on a malformed LLM response.
    Exit code 3 in the CLI."""
