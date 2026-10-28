from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from app.analysis.llm import _validation_failure_diagnostic


@pytest.mark.parametrize(
    "message", ["private-secret", "解释实体缺少原文依据private-secret", "JSON 不允许重复字段 private-secret"]
)
def test_unknown_or_prefix_matching_business_errors_remain_generic(message: str) -> None:
    diagnostic = _validation_failure_diagnostic(ValueError(message), {})
    assert diagnostic.summary == "模型输出未通过结构或证据引用校验。"
    assert "private-secret" not in diagnostic.model_dump_json()


def test_unknown_union_labels_and_custom_error_types_are_not_echoed() -> None:
    error = ValidationError.from_exception_data(
        "contract",
        [
            {
                "type": PydanticCustomError("private-secret", "private-secret", {"private-secret": "private-secret"}),
                "loc": ("answer", "private-secret", "value"),
                "input": "private-secret",
            }
        ],
    )
    schema = {"properties": {"answer": {"anyOf": [{"properties": {"value": {"type": "string"}}}]}}}
    summary = _validation_failure_diagnostic(error, schema).summary
    assert summary == "模型输出校验失败：answer.<结构>: schema_invalid"
    assert "private-secret" not in summary


def test_recursive_schema_paths_and_many_errors_are_bounded() -> None:
    schema = {
        "$defs": {"Node": {"properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "properties": {"answer": {"$ref": "#/$defs/Node"}},
    }
    error = ValidationError.from_exception_data(
        "contract",
        [
            {"type": "missing", "loc": ("answer", "child", "private-secret", str(index)), "input": "private-secret"}
            for index in range(100)
        ],
    )
    diagnostic = _validation_failure_diagnostic(error, schema)
    assert diagnostic.summary == "模型输出校验失败：answer.child.<结构>: missing"
    assert len(diagnostic.summary) <= 240
