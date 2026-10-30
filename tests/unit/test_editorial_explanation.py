from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from app.analysis.editorial_explanation import ExplanationOutput, _grounding, explain, validate_explanation
from app.analysis.editorial_references import EvidenceReferences
from app.analysis.model_execution import ModelCallBudget
from tests.editorial_fixtures import install_editorial_service
from tests.unit.test_editorial_contracts import _evidence, _source

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize(
    ("excerpts", "title", "summary", "valid"),
    [
        (["第一轮10分"], "获得1分", "比赛结束", False),
        (["第一轮1分", "第二轮2分"], "第一轮1", "2分是第二轮", True),
        (["总计1,000分"], "获得1分", "比赛结束", False),
        (["总计1,000分"], "获得1000分", "比赛结束", True),
        (["增长10%"], "增长10", "结果公布", False),
        (["得分１２分"], "获得12分", "比赛结束", True),
        (["比分在10-20分之间"], "最高20分", "比赛结束", True),
        (["净变化-20"], "净变化20", "比赛结束", False),
        (["净变化−10%"], "净变化10%", "比赛结束", False),
        (["净变化−10%"], "净变化-10%", "比赛结束", True),
    ],
)
def test_explanation_numbers_are_complete_tokens_in_separate_fields(
    excerpts: list[str], title: str, summary: str, valid: bool
) -> None:
    source = _source(evidence=[_evidence(excerpts=excerpts)])
    payload = {
        "title": title,
        "summary": summary,
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["asr:1"],
    }
    if valid:
        result = validate_explanation(payload, source, max_title_chars=64, max_summary_chars=1000, max_entities=50)
        assert result.title == title and result.summary == summary
    else:
        with pytest.raises(ValueError, match="数字缺少原文依据"):
            validate_explanation(payload, source, max_title_chars=64, max_summary_chars=1000, max_entities=50)


@pytest.mark.parametrize(
    ("excerpts", "entity", "valid"),
    [
        (["Foo", "Bar"], "Foo Bar", False),
        (["Foo\n Bar"], "Foo Bar", True),
        (["ＡＢＣ宣布规则"], "ABC", True),
        (["announcement"], "Ann", False),
    ],
)
def test_explanation_entities_remain_within_one_literal_excerpt(excerpts: list[str], entity: str, valid: bool) -> None:
    source = _source(evidence=[_evidence(excerpts=excerpts)])
    payload = {
        "title": "规则说明",
        "summary": "直播介绍规则。",
        "category": "discussion",
        "entities": [entity],
        "semantic_confidence": 0.8,
        "evidence_ids": ["asr:1"],
    }
    if valid:
        result = validate_explanation(payload, source, max_title_chars=64, max_summary_chars=1000, max_entities=50)
        assert result.entities == [entity]
    else:
        with pytest.raises(ValueError, match="实体缺少原文依据"):
            validate_explanation(payload, source, max_title_chars=64, max_summary_chars=1000, max_entities=50)


def test_reference_mapping_does_not_mutate_source_or_accept_original_long_id() -> None:
    source = _source(evidence=[_evidence(evidence_id="source:long"), _evidence(evidence_id="E1")])
    references = EvidenceReferences.from_source(source)
    assert [item.evidence_id for item in source.evidence] == ["source:long", "E1"]
    assert [item.evidence_id for item in references.source.evidence] == ["E1", "E2"]
    assert references.expand(["E2", "E1"]) == ["E1", "source:long"]
    with pytest.raises(ValueError, match="未知的短标识"):
        references.expand(["source:long"])


def test_sdk_short_references_preserve_long_excerpt_and_persist_original_id(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    original_id = "original:" + "x" * 200
    excerpt = "主播详细介绍挑战规则。" * 250
    source = _source(evidence=[_evidence(evidence_id=original_id, excerpts=[excerpt])])

    def respond(prompt: str, **_kwargs: object) -> str:
        data = json.loads(prompt.splitlines()[-1])
        assert data["evidence"][0]["evidence_id"] == "E1"
        assert data["evidence"][0]["excerpts"] == [excerpt]
        return json.dumps(
            {
                "title": "挑战规则说明",
                "summary": "主播详细介绍挑战规则。",
                "category": "discussion",
                "entities": ["挑战规则"],
                "semantic_confidence": 0.8,
                "evidence_ids": ["E1"],
            }
        )

    requests = install_editorial_service(monkeypatch, respond, grounded=True)
    result = explain(
        source,
        ModelCallBudget(max_attempts=4, max_elapsed_s=30),
        max_tokens=65536,
        max_title_chars=64,
        max_summary_chars=1000,
        max_entities=50,
    )
    assert result.error is None and result.output.evidence_ids == [original_id]
    assert len(requests) == 2
    grounding = json.loads(requests[1]["messages"][1]["content"])
    assert grounding["candidate"]["evidence_ids"] == ["E1"]
    assert grounding["evidence"][0]["quotes"] == [{"quote_id": "Q1", "text": excerpt}]
    assert original_id not in json.dumps(requests)


def test_quote_id_cannot_be_used_with_another_evidence_id() -> None:
    output = ExplanationOutput(
        title="规则",
        summary="规则说明",
        category="discussion",
        entities=[],
        semantic_confidence=0.8,
        evidence_ids=["E1", "E2"],
    )
    payload = {
        "judgements": [
            {
                "field": field,
                "supported": True,
                "reason": "受控错误引用",
                "quotes": [{"evidence_id": "E1", "quote_id": "Q2"}],
            }
            for field in ("title", "summary")
        ]
    }
    with pytest.raises(ValueError, match="引用不在原文中"):
        _grounding(payload, output, {"Q1": ("E1", "规则"), "Q2": ("E2", "其他事实")})
