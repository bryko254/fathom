FROM python:3.12-slim

COPY worker.py /opt/worker.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TMPDIR=/tmp

USER 65534:65534
ENTRYPOINT ["python", "-u", "/opt/worker.py"]
