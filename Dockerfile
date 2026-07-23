FROM python:3.11-slim

WORKDIR /app

# Install standard stockfish and system compilation tools
RUN apt-get update && apt-get install -y \
    stockfish \
    wget \
    git \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone, compile, and install Fairy-Stockfish from source securely
ENV REPO_OWNER="fairy-stockfish"
ENV REPO_NAME="Fairy-Stockfish"
RUN git clone https://github.com/${REPO_OWNER}/${REPO_NAME}.git /tmp/fairy-stockfish \
    && cd /tmp/fairy-stockfish/src \
    && make -j$(nproc) build ARCH=x86-64 \
    && cp stockfish /usr/local/bin/fairy-stockfish \
    && rm -rf /tmp/fairy-stockfish

COPY requirements.txt .

# Upgrade building utilities first to solve the pip install exit code 1 error
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Installs python-chess and requests cleanly
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

EXPOSE 8080

CMD ["python", "bot.py"]
