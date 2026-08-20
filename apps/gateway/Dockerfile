FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy requirements first so dependency layers cache across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Run as a non-root user.
RUN useradd --create-home --uid 10001 tokenix && chown -R tokenix /app
USER tokenix

EXPOSE 8080

# Single worker: the ledger queue and ACPI cache are per-process, so scaling
# means more containers, not more workers sharing a port.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
