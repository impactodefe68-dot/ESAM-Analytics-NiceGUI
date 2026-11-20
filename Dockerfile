FROM zauberzeug/nicegui:latest
WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY . .
ENV PYTHONPATH=/app:/app/app
EXPOSE 8080
CMD ["python", "app/main.py"]
