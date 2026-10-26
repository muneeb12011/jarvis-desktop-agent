# Note on scope: this container is for running/testing the backend's
# orchestration logic (planner, chat, memory, API, WebSocket) in a
# consistent environment — e.g. CI, or a dev sandbox. Real screen
# capture/mouse-and-keyboard control (capture.py / actuator.py) needs
# direct access to your desktop session, which a container fundamentally
# doesn't have (no GUI, no display, no real input devices) — that part
# always runs on the host via `uvicorn`, per the README. Containerizing
# it here is standard practice for the parts that CAN be containerized.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
