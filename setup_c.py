"""BiliLiveCut C 加速模块编译脚本 (V0.1.9)."""
import sys
from setuptools import Extension, setup

_c_speedups = Extension(
    "app.analysis._c_speedups",
    sources=["app/analysis/_c_speedups.c"],
    extra_compile_args=(
        ["/O2"] if "win" in sys.platform else ["-O3", "-march=native", "-ffast-math"]
    ),
)

setup(
    name="bili_live_cut_c",
    version="0.1.9",
    ext_modules=[_c_speedups],
    packages=["app", "app.analysis"],  # 指定而非自动发现，避免 flat-layout 报错
)
