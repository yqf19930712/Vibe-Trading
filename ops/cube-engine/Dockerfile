# Vibe-Trading engine image for CubeSandbox templates.
# Build context: extracted Vibe-Trading source tree + this directory's files
# (launcher.py copied in alongside). No frontend build — the router talks to
# the API only; an empty frontend/dist placeholder keeps the SPA middleware
# quiet.

FROM python:3.12-slim

WORKDIR /app

RUN sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -i https://mirrors.huaweicloud.com/repository/pypi/simple -r agent/requirements.txt

COPY pyproject.toml LICENSE README.md MANIFEST.in ./
COPY agent/ agent/
RUN mkdir -p frontend/dist && touch frontend/dist/index.html \
    && pip install --no-cache-dir -i https://mirrors.huaweicloud.com/repository/pypi/simple -e .

COPY launcher.py /usr/local/lib/vibe-launcher.py

# /app (engine code) stays root-owned and read-only for the runtime user. The
# engine's shell tools run as `vibe`; a writable /app would let tenant code
# rewrite the engine, which the next /boot then runs with the tenant-shared
# LLM credentials in its env. Nothing under /app is written at runtime: all
# tenant state goes to VIBE_DATA_DIR (the bind-mounted ~/.vibe-trading),
# caches to HOME, bytecode is compiled here while /app is still writable.
# PYTHONNOUSERSITE keeps the engine and launcher from importing a
# ~/.local site-packages (or .pth / sitecustomize) that tenant code planted
# in the writable HOME; the variable is not passed to the shell tools'
# subprocesses, so `pip install --user` there still works.
RUN python -m compileall -q /app/agent \
    && chmod -R go-w /app \
    && useradd --create-home --shell /usr/sbin/nologin vibe \
    && mkdir -p /home/vibe/.vibe-trading \
    && chown vibe:vibe /home/vibe/.vibe-trading
USER vibe
ENV HOME=/home/vibe VIBE_APP_DIR=/app VIBE_DATA_DIR=/home/vibe/.vibe-trading \
    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1

# 8898 = launcher (template probe), 8899 = engine API once booted
EXPOSE 8898 8899

CMD ["python", "/usr/local/lib/vibe-launcher.py"]
