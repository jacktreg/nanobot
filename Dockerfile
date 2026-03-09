FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install ALL system dependencies in one layer (Node.js + Camoufox deps)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git \
        libgtk-3-0 libasound2 libx11-xcb1 && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached unless pyproject.toml changes)
COPY pyproject.toml README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    uv pip install --system . && \
    rm -rf nanobot bridge

# Install Node.js dependencies (cached unless package.json changes)
COPY bridge/package.json bridge/package.json
RUN --mount=type=cache,target=/root/.npm \
    cd bridge && npm install

# Install Camoufox (no source dependency)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system camoufox>=0.4.0 && \
    python -m camoufox fetch

# Copy full source code
COPY nanobot/ nanobot/
COPY bridge/src/ bridge/src/
COPY bridge/tsconfig.json bridge/tsconfig.json

# Final Python install with source
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system .

# Build the WhatsApp bridge (uses pre-installed node_modules)
RUN cd bridge && npm run build

# Create config directory
RUN mkdir -p /root/.nanobot

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["nanobot"]
CMD ["status"]
