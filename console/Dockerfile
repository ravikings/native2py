# Console app image. Runs the FastAPI app that lets users upload native code,
# review the inferred contract, build it, and deploy it as a service — all by
# shelling out to `ngate` and `docker`, both of which this image needs.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install nativegate itself (editable) plus the console's own runtime deps.
COPY tools/nativegate /app/tools/nativegate
RUN pip install --no-cache-dir -e /app/tools/nativegate \
    && pip install --no-cache-dir fastapi uvicorn jinja2 python-multipart httpx

COPY console /app/console

EXPOSE 8000

CMD ["uvicorn", "console.app:app", "--host", "0.0.0.0", "--port", "8000"]
