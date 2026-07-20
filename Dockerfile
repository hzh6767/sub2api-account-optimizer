FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 optimizer \
    && useradd --uid 10001 --gid 10001 --create-home optimizer

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY optimizer ./optimizer
COPY README.md DESIGN.md ./

RUN mkdir -p /app/logs /app/state /app/backups \
    && chown -R optimizer:optimizer /app

USER optimizer
ENTRYPOINT ["python", "-m", "optimizer.cli"]
CMD ["--daemon"]
