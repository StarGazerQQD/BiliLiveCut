"""自学习引擎测试 (v0.1.8.2-HL-alpha)。"""
from __future__ import annotations

import pytest


class TestSelfLearnEngine:
    def test_init(self) -> None:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        engine = SelfLearnEngine(min_positive=100, model_type="xgboost")
        assert engine.min_positive == 100
        assert engine.model_type == "xgboost"
        assert engine.auto_incremental
        assert isinstance(engine.status, dict)
        assert not engine.is_model_available

    def test_status_keys(self) -> None:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        engine = SelfLearnEngine()
        status = engine.status
        for k in ("model_available", "iteration", "n_total_samples", "model_path"):
            assert k in status

    def test_run_insufficient_data(self) -> None:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        engine = SelfLearnEngine(min_positive=1000)
        result = engine.run(room_id=-999)
        assert not result.success
        assert "不足" in (result.error or "")
