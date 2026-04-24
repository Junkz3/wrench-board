# SPDX-License-Identifier: Apache-2.0
from api.pipeline.bench_generator.errors import (
    BenchGeneratorError,
    BenchGeneratorLLMError,
    BenchGeneratorPreconditionError,
)


def test_precondition_error_is_subclass():
    assert issubclass(BenchGeneratorPreconditionError, BenchGeneratorError)


def test_llm_error_is_subclass():
    assert issubclass(BenchGeneratorLLMError, BenchGeneratorError)


def test_precondition_error_carries_reason():
    exc = BenchGeneratorPreconditionError("no electrical_graph.json")
    assert "electrical_graph" in str(exc)
