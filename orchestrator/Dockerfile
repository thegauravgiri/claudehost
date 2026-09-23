FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /state is a named volume (see docker-compose.yml) mounted at this exact
# path. Docker copies an empty volume's initial content - including
# ownership - from whatever's already at the mount point in the image, so
# this chown is what makes the volume writable by the uid:gid compose runs
# this service as (1000:1000, matching claudebox's aicode user), instead of
# defaulting to root and failing every write.
RUN mkdir -p /state && chown 1000:1000 /state

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
