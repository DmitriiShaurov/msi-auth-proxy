# ── Stage 1: dependency installation ─────────────────────────────────────────
# Use a full Python image to compile any C-extension wheels, then copy only
# the installed packages into the slim runtime image.
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed for any compiled wheels.
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install into an isolated prefix so copying to the runtime stage is easy.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

# Create a dedicated non-root user.
RUN addgroup --system --gid 1001 appgroup && \
    adduser  --system --uid 1001 --gid 1001 --no-create-home appuser

WORKDIR /app

# Copy installed packages from the builder stage.
COPY --from=builder /install /usr/local

# Copy application source.
COPY main.py .

# Drop to non-root.
USER appuser

EXPOSE 8080

# Use uvicorn directly.
# --workers 1 is intentional: the proxy is fully async so a single process
# handles hundreds of concurrent connections. Increase only if you need true
# CPU parallelism (unlikely for a thin proxy).
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--log-level", "info", \
     "--no-access-log"]
