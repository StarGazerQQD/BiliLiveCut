#!/bin/bash
# BiliLiveCut Docker 停止脚本 (Linux/macOS)
echo "[BiliLiveCut Docker] 停止服务..."
docker compose -f packaging/docker/compose.yaml down
echo "[BiliLiveCut Docker] 已停止"
