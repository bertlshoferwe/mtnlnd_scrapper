# The dashboard app. Long-running (gunicorn), unlike the old single-shot
# Vercel serverless function — see api/index.py's module docstring.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "api.index:app"]
