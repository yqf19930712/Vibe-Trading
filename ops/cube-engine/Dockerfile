# Vibe-Trading engine image for CubeSandbox templates.
# Build context: extracted Vibe-Trading source tree + this directory's files
# (launcher.py copied in alongside). No frontend build — the router talks to
# the API only; an empty frontend/dist placeholder keeps the SPA middleware
# quiet.

FROM python:3.12-slim

WORKDIR /app

RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple -r agent/requirements.txt

COPY pyproject.toml LICENSE README.md MANIFEST.in ./
COPY agent/ agent/
RUN mkdir -p frontend/dist && touch frontend/dist/index.html \
    && pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple -e .

COPY launcher.py /usr/local/lib/vibe-launcher.py

RUN useradd --create-home --shell /usr/sbin/nologin vibe \
    && mkdir -p agent/runs agent/sessions agent/uploads agent/.swarm/runs /home/vibe/.vibe-trading \
    && chown -R vibe:vibe /app /home/vibe/.vibe-trading
USER vibe
ENV HOME=/home/vibe VIBE_APP_DIR=/app

# 8898 = launcher (template probe), 8899 = engine API once booted
EXPOSE 8898 8899

CMD ["python", "/usr/local/lib/vibe-launcher.py"]
