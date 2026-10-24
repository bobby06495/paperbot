# paper-bot container image. Build from the project root:
#   docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PAPERBOT_HOME=/app \
    TZ=America/New_York

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# config/, logs/, data/ and dashboard/ are mounted from the host (docker-compose.yml)
# so settings, state and history survive container rebuilds.
COPY config ./config

# No terminal in a container: the strategy comes from PAPERBOT_STRATEGY (first
# start) or the saved choice; a STOP file makes it exit without a restart.
CMD ["paper-bot", "start", "--yes"]
