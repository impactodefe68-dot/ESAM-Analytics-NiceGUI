FROM zauberzeug/nicegui:latest

WORKDIR /app

# Elimina el main.py de ejemplo que trae la imagen base de NiceGUI
RUN rm -f /app/main.py

# Copia e instala dependencias
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Copia todo el proyecto (incluye tu main.py real)
COPY . .

EXPOSE 8080
CMD ["python", "main.py"]
