FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs once and exits — the daily schedule is handled by the external
# platform (DigitalOcean App Platform Scheduled Job / cron), not by this
# container looping internally.
ENTRYPOINT ["python", "main.py"]
