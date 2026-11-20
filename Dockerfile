FROM zauberzeug/nicegui:latest
WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "main.py"]
