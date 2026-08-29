FROM python:3.11-slim

WORKDIR /app

COPY config.py imap_client.py storage.py app.py ./
COPY frontend/ ./frontend/

# 数据目录
VOLUME /app/data

# 让 storage.py 的数据库存到 /app/data
ENV MAILHUB_DATA_DIR=/app/data

EXPOSE 20111

CMD ["python", "app.py"]
