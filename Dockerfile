FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-server.txt .
RUN pip install -r requirements-server.txt

COPY app.py sources.py engine.py indicators.py market.py recommender.py ai.py pushers.py ./
COPY static ./static

EXPOSE 8777

# Render/Railway 等平台会注入 $PORT
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8777}"]
