# Multi-stage Dockerfile for Customer Retention & RFM Pipeline
FROM python:3.11-slim AS base

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements-prod.txt /app/
RUN pip install --no-cache-dir -r requirements-prod.txt

# Copy source code and project metadata
COPY src/ /app/src/

# Default entrypoint runs the end-to-end pipeline
ENTRYPOINT ["python", "src/pipeline.py"]

