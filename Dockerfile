FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# For real traffic, put this behind a reverse proxy (Caddy, nginx, or your
# host's built-in one) that terminates HTTPS and forwards X-Forwarded-Proto —
# then set FORCE_HTTPS=1 below.
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
