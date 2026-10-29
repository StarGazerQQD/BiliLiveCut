# CPU 运行镜像；构建工具和 uv 只保留在构建阶段。
FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv
FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS python-base
# 同时修复基础 Python 自带的安装工具；uv 与锁文件只在本条构建指令挂载。
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=packaging/docker/requirements-system.lock,target=/tmp/requirements-system.lock \
    /bin/uv --no-cache pip install --python /usr/local/bin/python --require-hashes --no-deps \
    -r /tmp/requirements-system.lock

# FFmpeg 使用官方签名发行源码；命令行与 PyAV 共用同一套已修复共享库。
# 字幕渲染使用 Debian 官方 libass-dev/libass9，不维护外加补丁。
FROM python-base AS ffmpeg-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config nasm xz-utils curl \
       libass-dev libfontconfig-dev libfreetype-dev libharfbuzz-dev libfribidi-dev \
       libx264-dev libx265-dev libvpx-dev libopus-dev libmp3lame-dev libvorbis-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
# 官方源传输失败后使用同摘要的 MacPorts 源码镜像；摘要不一致直接终止。
COPY scripts/download_docker_ffmpeg.py /tmp/download_docker_ffmpeg.py
RUN python /tmp/download_docker_ffmpeg.py --output /tmp/ffmpeg.tar.xz
WORKDIR /tmp/ffmpeg
RUN tar -xf /tmp/ffmpeg.tar.xz --strip-components=1 \
    && ./configure --prefix=/opt/ffmpeg --enable-shared --disable-static \
       --enable-gpl --enable-version3 --enable-openssl --enable-libass \
       --enable-libfontconfig --enable-libfreetype --enable-libharfbuzz --enable-libfribidi \
       --enable-libx264 --enable-libx265 --enable-libvpx --enable-libopus \
       --enable-libmp3lame --enable-libvorbis --disable-doc --disable-ffplay \
    && make -j 4 \
    && make install \
    && mkdir -p /opt/ffmpeg/share/source \
    && cp /tmp/ffmpeg.tar.xz /opt/ffmpeg/share/source/ffmpeg-9.0.2.tar.xz \
    && cp COPYING.GPLv3 LICENSE.md /opt/ffmpeg/share/source/

FROM node:24-bookworm-slim AS node-tools

FROM python-base AS builder
COPY --from=uv /uv /bin/uv
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config cargo rustc \
       libass9 libfontconfig1 libfreetype6 libharfbuzz0b libfribidi0 \
       libx264-164 libx265-215 libvpx9 libopus0 libmp3lame0 libvorbis0a libvorbisenc2 libssl3t64 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-builder /opt/ffmpeg /opt/ffmpeg
ENV PKG_CONFIG_PATH=/opt/ffmpeg/lib/pkgconfig LD_LIBRARY_PATH=/opt/ffmpeg/lib
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_BINARY_PACKAGE=av
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra asr-all --extra llm --extra web --no-install-project

# PyTorch 不在项目 extras 中，单独安装配套的官方 CPU wheel 和锁定依赖。
COPY packaging/docker/requirements-cpu.lock ./requirements-cpu.lock
RUN uv pip install --python /app/.venv/bin/python --require-hashes --no-deps \
    --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match -r requirements-cpu.lock

# 包括 setuptools data-files 与 C/Cython 构建源，不能只复制 app/config。
COPY README.md LICENSE MANIFEST.in setup.py setup_c.py .env.example ./
COPY app ./app
COPY frontend ./frontend
COPY config ./config
COPY docs/development/live-source-plugins.md docs/development/douyin-plugin-handoff.md ./docs/development/
COPY plugin ./plugin
COPY tools/native/c ./tools/native/c
COPY tools/native/cython ./tools/native/cython
COPY tools/native/repeat ./tools/native/repeat
COPY tools/native/rust ./tools/native/rust
COPY tools/native/build_rust.py ./tools/native/build_rust.py
RUN /app/.venv/bin/python tools/native/build_rust.py
RUN uv sync --locked --no-dev --extra asr-all --extra llm --extra web --no-editable \
    --inexact \
    && uv pip check --python /app/.venv/bin/python \
    && /app/.venv/bin/python -I -c "import app.accelerators, pathlib, shutil; shutil.copy2('/app/app/accelerators/_rust_speedups.so', pathlib.Path(app.accelerators.__file__).parent / '_rust_speedups.so')" \
    && PATH="/opt/ffmpeg/bin:$PATH" /app/.venv/bin/python -I -c "from app.accelerators import _c_speedups, _cython_speedups, _repeat_speedups, _rust_speedups; import torch, torchaudio; from funasr import AutoModel; import av; assert torch.version.cuda is None; assert av.ffmpeg_version_info == '9.0.2'"

FROM python-base AS runtime
# 运行容器通过重建镜像更新依赖，不保留系统安装工具及其可重新安装的旧引导 wheel。
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    /bin/uv --no-cache pip uninstall --python /usr/local/bin/python pip \
    && python -I -c "import ensurepip; from pathlib import Path; [p.unlink() for p in (Path(ensurepip.__file__).parent / '_bundled').glob('*.whl')]"
# 保留 libass 的官方变更日志，使 dpkg --verify 能核验完整包，其他 slim 裁剪规则不变。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       -o 'Dpkg::Options::=--path-include=/usr/share/doc/libass9/changelog*' \
       ca-certificates libbrotli1 libexpat1 fonts-noto-cjk fontconfig libgomp1 liblzma5 \
       libass9 libfontconfig1 libfreetype6 libharfbuzz0b libfribidi0 \
       libx264-164 libx265-215 libvpx9 libopus0 libmp3lame0 libvorbis0a libvorbisenc2 libssl3t64 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge '20250419+deb13u1' \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libbrotli1)" ge '1.1.0-2+deb13u1' \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libexpat1)" ge '2.9.0-1~deb13u1' \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home appuser \
    && mkdir -p /data /app \
    && chown appuser:appuser /data
# 账号和目录建好后移除仅用于安装、创建账号及挂载的工具；保留 dpkg 数据库供漏洞审计。
# 不自动清理依赖、不强制删除 Essential/Protected 包；依赖关系变化时构建必须失败。
RUN dpkg --purge apt libapt-pkg7.0 sqv mount passwd adduser \
       libsemanage2 libsemanage-common libsepol2 \
    && test -z "$(dpkg --audit)"

COPY --from=ffmpeg-builder /opt/ffmpeg /opt/ffmpeg
ENV LD_LIBRARY_PATH=/opt/ffmpeg/lib \
    PATH="/app/.venv/bin:/opt/ffmpeg/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_ENV=prod \
    DOCKER_AUTH_ENABLED=true \
    STORAGE_ROOT=/data \
    DATABASE_URL=sqlite:////data/blc.db \
    PLUGIN_DIR=/data/plugins \
    BLC_APP_ROOT=/data \
    HF_HOME=/data/cache/huggingface \
    MODELSCOPE_CACHE=/data/cache/modelscope \
    PLAYWRIGHT_BROWSERS_PATH=/data/cache/playwright

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY packaging/docker/entrypoint.py packaging/docker/healthcheck.py ./docker/
COPY tools/native/rust/THIRD_PARTY_NOTICES.md /usr/share/doc/bili-live-cut-rust/THIRD_PARTY_NOTICES.md
COPY tools/native/repeat/vendor/libsais/LICENSE /usr/share/doc/bili-live-cut-repeat/LICENSE-libsais
USER appuser
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["blc", "backend", "--host", "0.0.0.0", "--port", "8000"]
