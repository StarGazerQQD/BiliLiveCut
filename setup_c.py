"""BiliLiveCut C 加速模块编译脚本 (V0.1.9).

用法:
    python setup_c.py build_ext --inplace
    # 或通过 pip install -e . 自动触发 pyproject.toml 中配置的编译
"""

import sys

from setuptools import Extension, setup

_c_speedups = Extension(
    "app.analysis._c_speedups",
    sources=["app/analysis/_c_speedups.c"],
    extra_compile_args=(
        ["/O2", "/arch:AVX2", "/fp:fast"]
        if "win" in sys.platform
        else ["-O3", "-march=native", "-ffast-math"]
    ),
)

setup(
    name="bili_live_cut_c",
    version="0.1.9",
    ext_modules=[_c_speedups],
)
