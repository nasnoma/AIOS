FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc g++ libpq-dev git \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY polymarket_bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Run bot
CMD ["python", "-m", "polymarket_bot.main"]
