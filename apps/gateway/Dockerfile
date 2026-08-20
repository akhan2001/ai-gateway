FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy requirements first so dependency layers cache across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# The ACPI dataset ships *in the image*. Compose bind-mounts a live copy over
# /data so an hourly refresh needs no restart, but a platform that only runs the
# image (Railway) has no mount: without this the catalog loads zero models, and
# a zero-model catalog does not fail loudly - every request is recorded
# `priced = false`, analytics filters those out, and the dashboard reports no
# spend while traffic is flowing.
COPY data ./data

# Default to the baked-in copy. Compose overrides this to /data, and refreshing
# the dataset on a platform without a mount means redeploying.
ENV ACPI_PRICES_PATH=/app/data/acpi_prices.json

# Run as a non-root user.
RUN useradd --create-home --uid 10001 tokenix && chown -R tokenix /app
USER tokenix

EXPOSE 8080

# Single worker: the ledger queue and ACPI cache are per-process, so scaling
# means more containers, not more workers sharing a port.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
