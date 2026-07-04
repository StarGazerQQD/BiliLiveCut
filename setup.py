"""BiliLiveCut 构建配置 — 含 C 加速模块编译 (V0.1.9)."""

import sys

from setuptools import Extension, find_packages, setup

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
    name="bili-live-cut",
    version="0.1.9.1-alpha",
    description="AI 直播实时切片系统",
    packages=find_packages(include=["app", "app.*"]),
    ext_modules=[_c_speedups],
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
