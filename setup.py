"""BiliLiveCut 构建配置 — 含 C 加速模块编译 (V0.1.10.1)."""

import sys

from setuptools import Extension, find_packages, setup

_extensions = []

# 第一轮: C 直接编译 (Aho-Corasick + 余弦相似度 + bigram)。
_c_speedups = Extension(
    "app.analysis._c_speedups",
    sources=["app/analysis/_c_speedups.c"],
    extra_compile_args=(
        ["/O2", "/arch:AVX2", "/fp:fast"]
        if "win" in sys.platform
        else ["-O3", "-march=native", "-ffast-math"]
    ),
)
_extensions.append(_c_speedups)

# 第二轮: Cython 编译 (聚类矩阵 + 弹幕基线 + SRT)。
try:
    from Cython.Build import cythonize
    _r2 = Extension(
        "app.analysis._speedups_round2",
        sources=["app/analysis/_speedups_round2.pyx"],
        extra_compile_args=(
            ["/O2", "/arch:AVX2"]
            if "win" in sys.platform
            else ["-O3", "-march=native"]
        ),
    )
    _extensions.append(_r2)
except ImportError:
    # Cython 未安装:跳过第二轮编译,加速分派层自动回退 Python
    pass

setup(
    name="bili-live-cut",
    version="0.1.10.1-alpha",
    description="AI 直播实时切片系统",
    packages=find_packages(include=["app", "app.*"]),
    ext_modules=_extensions,
    install_requires=[
        "httpx>=0.27",
        "pydantic>=2.7",
        "pydantic-settings>=2.3",
        "sqlmodel>=0.0.22",
        "loguru>=0.7",
        "typer>=0.12",
        "rich>=13.7",
        "websockets>=12.0",
        "brotli>=1.1",
        "pyyaml>=6.0",
        "numpy>=1.26",
    ],
    extras_require={
        "asr": ["faster-whisper>=1.0"],
        "llm": ["openai>=1.40"],
        "web": ["fastapi>=0.111", "uvicorn[standard]>=0.30", "jinja2>=3.1"],
        "dev": ["pytest>=8.2", "pytest-asyncio>=0.23", "pytest-mock>=3.14", "ruff>=0.5"],
    },
    entry_points={
        "console_scripts": ["blc=app.cli:app"],
    },
)
