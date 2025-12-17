FROM node:20-bullseye-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*


RUN corepack enable
WORKDIR /app
ENV VIRTUAL_ENV=/app/venv

RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt
COPY builder.py .
CMD ["python", "builder.py"]