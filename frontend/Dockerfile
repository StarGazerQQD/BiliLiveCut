FROM node:24-alpine AS build
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:stable-alpine
# 仅在构建时安装 Alpine 官方安全更新，运行阶段恢复原有非特权用户。
USER root
RUN apk add --no-cache 'brotli-libs>=1.2.1-r0' 'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1' \
    'curl>=8.23.0-r0' 'libcurl>=8.23.0-r0'
USER 101
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 8080
