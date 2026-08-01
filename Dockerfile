FROM python:3.11-slim

WORKDIR /app

# Install Stockfish and system tools
RUN apt-get update && apt-get install -y \
    stockfish \
    wget \
    git \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone, compile, and install Fairy-Stockfish
ENV REPO_OWNER="fairy-stockfish"
ENV REPO_NAME="Fairy-Stockfish"
RUN git clone https://github.com/${REPO_OWNER}/${REPO_NAME}.git /tmp/fairy-stockfish \
    && cd /tmp/fairy-stockfish/src \
    && make -j$(nproc) build ARCH=x86-64 \
    && cp stockfish /usr/local/bin/fairy-stockfish \
    && rm -rf /tmp/fairy-stockfish

# Copy requirements
COPY requirements.txt .

# Upgrade pip tools first
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Expose health check port
EXPOSE 8080

# Run bot
CMD ["python", "bot.py"]
