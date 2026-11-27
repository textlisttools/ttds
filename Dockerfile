FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Keep the SQLite database on a persistent volume mounted at /data
ENV DATABASE_URL=sqlite:////data/ttds.db
RUN mkdir -p /data
EXPOSE 8000
# One worker only: the background scheduler must not run twice.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
