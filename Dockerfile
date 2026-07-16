# BiliLiveCut 运行镜像
# 使用 Python 3.12(对 faster-whisper/ctranslate2 等 AI 依赖的 wheel 支持最稳)。
FROM python:3.12-slim

# 安装 FFmpeg(录制/切片/音频特征所必需)。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户, 限制容器权限 (在 COPY 前建用户, 在 COPY 后转交所有权)。
RUN useradd -m appuser && mkdir -p /data /app && chown -R appuser:appuser /data /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_ROOT=/data \
    DATABASE_URL=sqlite:////data/blc.db

WORKDIR /app

# 先拷贝依赖清单以利用层缓存。
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config

# 为 appuser 赋予文件所有权 (此时仍为 root, 可执行 chown)。
RUN chown -R appuser:appuser /app

# 切换到非 root 用户执行后续操作。
USER appuser

# 安装 Python 依赖 (不启用哈希锁定, 请确保依赖来源可信)。
RUN pip install --upgrade pip \
    && pip install ".[asr,web]"

# 运行产物挂载到 /data(见 docker-compose)。
VOLUME ["/data"]
EXPOSE 8000

# 默认启动 Web 控制台;监听 0.0.0.0 以便容器外访问。
CMD ["python", "-m", "app.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
