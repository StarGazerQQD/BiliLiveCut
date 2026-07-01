# BiliLiveCut 运行镜像
# 使用 Python 3.12(对 faster-whisper/ctranslate2 等 AI 依赖的 wheel 支持最稳)。
FROM python:3.12-slim

# 安装 FFmpeg(录制/切片/音频特征所必需)。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_ROOT=/data \
    DATABASE_URL=sqlite:////data/blc.db

WORKDIR /app

# 先拷贝依赖清单以利用层缓存。
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config

# 安装核心 + asr + web(如需 llm 可自行加 .[llm])。
RUN pip install --upgrade pip \
    && pip install -e ".[asr,web]"

# 运行产物挂载到 /data(见 docker-compose)。
VOLUME ["/data"]
EXPOSE 8000

# 默认启动 Web 控制台;监听 0.0.0.0 以便容器外访问。
CMD ["python", "-m", "app.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
