"""BiliLiveCut 构建配置 — 含 C 加速模块编译 (V0.1.13.2)."""

import os
import sys

from setuptools import Extension, find_packages, setup

_skip_extensions = os.environ.get("BLC_SKIP_C_EXTENSIONS", "").strip().lower() in ("1", "true", "yes")
_extensions = []

if not _skip_extensions:
    # 第一轮: C 直接编译 (Aho-Corasick + 余弦相似度 + bigram)。
    _c_speedups = Extension(
        "app.analysis._c_speedups",
        sources=["tools/native/c/_c_speedups.c"],
        extra_compile_args=(["/O2", "/fp:fast", "/utf-8"] if sys.platform == "win32" else ["-O3", "-ffast-math"]),
    )
    _extensions.append(_c_speedups)

    # 第二轮: Cython 编译 (聚类矩阵 + 弹幕基线 + SRT)。
    try:
        from Cython.Build import cythonize  # noqa: F401

        _r2 = Extension(
            "app.analysis._speedups_round2",
            sources=["tools/native/cython/_speedups_round2.pyx"],
            extra_compile_args=(["/O2", "/utf-8"] if sys.platform == "win32" else ["-O3", "-ffast-math"]),
        )
        _extensions.append(_r2)
    except ImportError:
        # Cython 未安装:跳过第二轮编译,加速分派层自动回退 Python
        pass

setup(
    packages=find_packages(include=["app", "app.*", "config"]),
    ext_modules=_extensions,
    include_package_data=True,
)
