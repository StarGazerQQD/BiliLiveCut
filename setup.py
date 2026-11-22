"""BiliLiveCut 构建配置 — 含 C 加速模块编译 (V0.1.13.2)."""

import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py
from setuptools.command.egg_info import egg_info
from setuptools.command.sdist import sdist

_build = runpy.run_path(str(Path(__file__).resolve().parent / "app/_packaging_identity.py"))
identity = {key: _build[key] for key in ("RELEASE_VERSION", "VERSION_LABEL", "SOURCE_COMMIT")}


class ReleaseEggInfo(egg_info):
    """将同一份身份写入普通和 editable 安装的分发元数据。"""

    def run(self) -> None:
        """在 setuptools 生成元数据后记录构建身份。"""
        super().run()
        (Path(self.egg_info) / "blc-release.json").write_text(json.dumps(identity), encoding="utf-8")


class ReleaseSdist(sdist):
    """让从源码归档再次构建时仍能读取原提交的身份。"""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        """冻结源码包中的版本模块，供无 Git 的构建环境读取。"""
        super().make_release_tree(base_dir, files)
        module = Path(base_dir) / "app/_source_identity.py"
        # setuptools 可以硬链接工作区文件；生成归档副本前先断开链接。
        module.unlink()
        module.write_text("".join(f"{key} = {value!r}\n" for key, value in identity.items()), encoding="utf-8")


class FrontendBuildPy(build_py):
    """普通 wheel 从锁定源码构建 UI；editable 安装保留独立开发流程。"""

    def run(self) -> None:
        """只在隔离目录安装 npm 依赖，避免污染源码及复用陈旧 dist。"""
        if self.editable_mode:
            super().run()
            return
        web_output = Path(self.build_lib) / "app" / "web"
        if web_output.resolve() == Path(__file__).resolve().parent / "app" / "web":
            raise RuntimeError("wheel 构建目录不能覆盖源码目录")
        if web_output.exists():
            shutil.rmtree(web_output)
        super().run()
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("构建 wheel 需要 Node.js 24 与 npm；安装和运行已构建的 wheel 不需要 Node.js")
        frontend = Path(__file__).resolve().parent / "frontend"
        if not (frontend / "package-lock.json").is_file():
            raise RuntimeError("源码包缺少 frontend/package-lock.json，无法构建完整 wheel")
        with tempfile.TemporaryDirectory(prefix="blc-wheel-frontend-") as temporary:
            staging = Path(temporary) / "frontend"
            shutil.copytree(
                frontend,
                staging,
                ignore=shutil.ignore_patterns("node_modules", "dist", "test-results", "playwright-report"),
            )
            subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=staging, check=True, timeout=300)
            subprocess.run([npm, "run", "build"], cwd=staging, check=True, timeout=180)
            output = staging / "dist"
            if not (output / "index.html").is_file() or not any(output.rglob("*.js")):
                raise RuntimeError("前端构建未生成完整的 index.html 与 JavaScript 资源")
            destination = Path(self.build_lib) / "app" / "web" / "ui"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(output, destination)


class NativeBuildExt(build_ext):
    """按各源文件语言编译混合扩展，避免把 C++ 标准参数传给 C 编译器。"""

    def build_extension(self, ext: Extension) -> None:
        """单独生成混合扩展中的 C 对象，再交给原构建流程链接 C++ 扩展。"""
        c_sources = [source for source in ext.sources if source.endswith(".c")] if ext.language == "c++" else []
        if not c_sources:
            super().build_extension(ext)
            return
        sources, objects, depends = ext.sources, ext.extra_objects, ext.depends
        c_args = [arg for arg in ext.extra_compile_args if not arg.startswith(("-std=c++", "/std:c++"))]
        macros = ext.define_macros + [(name,) for name in ext.undef_macros]
        c_objects = self.compiler.compile(
            c_sources,
            output_dir=self.build_temp,
            macros=macros,
            include_dirs=ext.include_dirs,
            debug=self.debug,
            extra_postargs=c_args,
            depends=ext.depends,
        )
        try:
            ext.sources = [source for source in sources if source not in c_sources]
            ext.extra_objects = list(objects or []) + c_objects
            ext.depends = list(depends or []) + c_sources + c_objects
            super().build_extension(ext)
        finally:
            ext.sources, ext.extra_objects, ext.depends = sources, objects, depends


_skip_extensions = os.environ.get("BLC_SKIP_C_EXTENSIONS", "").strip().lower() in ("1", "true", "yes")
_extensions = []

if not _skip_extensions:
    # 第一轮: C 直接编译 (Aho-Corasick + 余弦相似度 + bigram)。
    _c_speedups = Extension(
        "app.accelerators._c_speedups",
        sources=["tools/native/c/_c_speedups.c"],
        extra_compile_args=(
            ["/O2", "/fp:precise", "/utf-8"] if sys.platform == "win32" else ["-O3", "-ffp-contract=off"]
        ),
        extra_link_args=(["/Brepro"] if sys.platform == "win32" else []),
    )
    _extensions.append(_c_speedups)

    # Cython: 聚类、音频数值热点、滚动基线、弹幕基线与 SRT。
    try:
        from Cython.Build import cythonize  # noqa: F401

        _r2 = Extension(
            "app.accelerators._cython_speedups",
            sources=["tools/native/cython/_cython_speedups.pyx"],
            extra_compile_args=(["/O2", "/utf-8"] if sys.platform == "win32" else ["-O3", "-ffp-contract=off"]),
            extra_link_args=(["/Brepro"] if sys.platform == "win32" else []),
        )
        _extensions.append(_r2)

        _repeat = Extension(
            "app.accelerators._repeat_speedups",
            sources=[
                "tools/native/cython/_repeat_speedups.pyx",
                "tools/native/repeat/repeat.cpp",
                "tools/native/repeat/vendor/libsais/src/libsais.c",
            ],
            include_dirs=["tools/native/repeat", "tools/native/repeat/vendor/libsais/include"],
            depends=["tools/native/repeat/repeat.hpp", "tools/native/repeat/vendor/libsais/include/libsais.h"],
            language="c++",
            extra_compile_args=(["/O2", "/utf-8", "/std:c++17"] if sys.platform == "win32" else ["-O3", "-std=c++17"]),
            extra_link_args=(["/Brepro"] if sys.platform == "win32" else []),
        )
        _extensions.append(_repeat)
    except ImportError:
        # Cython 未安装：跳过编译，由加速分派层使用 Python 参考实现。
        pass

setup(
    packages=find_packages(include=["app", "app.*", "config"]),
    ext_modules=_extensions,
    cmdclass={
        "build_ext": NativeBuildExt,
        "build_py": FrontendBuildPy,
        "egg_info": ReleaseEggInfo,
        "sdist": ReleaseSdist,
    },
    include_package_data=True,
)
