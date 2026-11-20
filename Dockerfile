FROM zauberzeug/nicegui:latest

# Instala el compilador C necesario para asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["python", "app/main.py"]
