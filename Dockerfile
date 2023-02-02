FROM public.ecr.aws/docker/library/python:3.10.9-slim-bullseye AS builder

RUN apt-get update && apt-get install build-essential -y
COPY requirements.txt ./
RUN pip install --user --no-cache-dir --upgrade -r requirements.txt

FROM public.ecr.aws/docker/library/python:3.10.9-slim-bullseye

WORKDIR /app
COPY --from=builder /root/.local /root/.local

ENV HARNESS_SCRATCH_API_KEY="" \
    HARNESS_SCRATCH_RELAY_BASE_URL="https://config.ff.harness.io/api/1.0" \
    HARNESS_SCRATCH_RELAY_EVENTS_URL="https://events.ff.harness.io/api/1.0" \
    PATH="/root/.local/bin:$PATH"

COPY main.py ./
EXPOSE 8000

CMD ["uvicorn", "main:app", "--proxy-headers", "--host", "0.0.0.0", "--port", "8000"]
