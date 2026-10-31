# ruff: noqa: D100
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.analysis.editorial_contracts import EditorialReviewInputV1


@dataclass(frozen=True, slots=True)
class EvidenceReferences:
    """请求内使用短编号，持久结果仍回指完整来源身份。"""

    source: EditorialReviewInputV1
    originals: dict[str, str]

    @classmethod
    def from_source(cls, source: EditorialReviewInputV1) -> EvidenceReferences:
        """按本次证据顺序生成可逆映射，不改变证据内容和时间。"""
        originals = {f"E{index}": item.evidence_id for index, item in enumerate(source.evidence, start=1)}
        evidence = [
            item.model_copy(update={"evidence_id": identity})
            for identity, item in zip(originals, source.evidence, strict=True)
        ]
        return cls(source.model_copy(update={"evidence": evidence}), originals)

    def expand(self, identities: Sequence[str]) -> list[str]:
        """仅接受本次输入提供的编号，拒绝旧请求或原始长 ID。"""
        if any(identity not in self.originals for identity in identities):
            raise ValueError("证据引用未知的短标识")
        return [self.originals[identity] for identity in identities]
