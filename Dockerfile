FROM zauberzeug/nicegui:latest

# Instala dependencias del sistema necesarias para compilar asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

EXPOSE 8080
# Usa el Python del entorno virtual donde se instalaron las dependencias
CMD ["/opt/venv/bin/python", "app/main.py"]
