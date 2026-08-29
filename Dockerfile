FROM python:3.11-slim

WORKDIR /app

COPY config.py imap_client.py storage.py app.py ./
COPY frontend/ ./frontend/

# 数据目录
VOLUME /app/data

# 让 storage.py 的数据库存到 /app/data
ENV MAILHUB_DATA_DIR=/app/data
# 访问密码（设了才启用登录鉴权，留空则不鉴权）
ENV MAILHUB_PASSWORD=
# favicon 代理（如 http://127.0.0.1:7890，留空则直连）
ENV MAILHUB_PROXY=

EXPOSE 20111

CMD ["python", "app.py"]
