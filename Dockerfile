# The dashboard app. Long-running (gunicorn), unlike the old single-shot
# Vercel serverless function — see api/index.py's module docstring.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
# --timeout 200: api_proxy_pdf's ConstructConnect live-refetch path calls
# through to the scanner and waits for it — can legitimately take minutes,
# well past gunicorn's 30s default worker timeout.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "--timeout", "200", "api.index:app"]
