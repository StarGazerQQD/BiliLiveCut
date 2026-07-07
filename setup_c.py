"""BiliLiveCut C 加速模块编译脚本 (V0.1.13.2).

用法:
    python setup_c.py build_ext --inplace
    # 或通过 pip install -e . 自动触发 pyproject.toml 中配置的编译
"""

import os
import sys

from setuptools import Extension, setup

_skip = os.environ.get("BLC_SKIP_C_EXTENSIONS", "").strip().lower() in ("1", "true", "yes")
_extensions = []

if not _skip:
    _c_speedups = Extension(
        "app.analysis._c_speedups",
        sources=["app/accelerators/c/_c_speedups.c"],
        extra_compile_args=(["/O2", "/arch:AVX2", "/fp:fast"] if sys.platform == "win32" else ["-O3", "-ffast-math"]),
    )
    _extensions.append(_c_speedups)

setup(
    name="bili_live_cut_c",
    version="0.1.14.2",
    ext_modules=_extensions,
)
