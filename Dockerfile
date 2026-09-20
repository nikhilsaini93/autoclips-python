FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    unzip \
    fonts-dejavu-core \
    fonts-noto-core \
    fontconfig \
    && (command -v node >/dev/null 2>&1 || ln -sf /usr/bin/nodejs /usr/bin/node) \
    # Deno is yt-dlp's recommended JS runtime (enabled by default, no flag
    # needed). Debian's apt nodejs is v18-v20 but yt-dlp 2026 builds need
    # node>=22 — deno avoids that trap entirely.
    && curl -fsSL https://deno.land/install.sh | sh \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \
    && rm -rf /root/.deno \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
