FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir runpod requests

# Build with the repository root as context:
# docker build -f worker-cpu/Dockerfile -t runonrunpod-cpu-stager .
COPY cpu_staging_contract.py /app/cpu_staging_contract.py
COPY cpu_artifact_contract.py /app/cpu_artifact_contract.py
COPY resource_plan.py /app/resource_plan.py
COPY file_lock.py /app/file_lock.py
COPY worker-cpu/handler.py /app/handler.py

WORKDIR /app
CMD ["python", "handler.py"]
