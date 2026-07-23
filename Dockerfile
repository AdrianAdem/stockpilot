FROM python:3.11-slim

WORKDIR /app

# gcc is needed only to build wheels that lack prebuilt binaries
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# US market hours only; the container should stay up 24/7 (see compose restart policy)
CMD ["python", "-m", "main"]
