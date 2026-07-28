"""高光模型独立版本回归测试。"""

from app.analysis.highlight_ml.version import HIGHLIGHT_MODEL_VERSION, HIGHLIGHT_MODEL_VERSION_LABEL


def test_highlight_model_release_version_is_independent_from_app_version() -> None:
    """高光模型使用独立 HL-Alpha 展示版本，不污染主程序版本。"""
    from app import __version__

    assert HIGHLIGHT_MODEL_VERSION == "0.1.15.2"
    assert HIGHLIGHT_MODEL_VERSION_LABEL == "V0.1.15.2 HL-Alpha"
    assert HIGHLIGHT_MODEL_VERSION != __version__
