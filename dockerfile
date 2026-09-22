FROM python:3.12-slim

WORKDIR /app

COPY requirements-monitor.txt .
RUN pip install --no-cache-dir -r requirements-monitor.txt

COPY monitor_server.py .

# El directorio de origen se monta desde el host como volumen (ver docker-compose.yml)
ENV SRC_DIR=/data/origen
ENV PORT=8080

EXPOSE 8080

# gunicorn en vez del servidor de desarrollo de Flask: mas estable para dejarlo corriendo
# workers=1 es a proposito: el tracking de "primera vez visto" vive en
# memoria del proceso (ver monitor_server.py), asi que necesitamos un unico
# worker para que sea consistente entre requests. threads=4 igual permite
# atender varias conexiones/PCs en simultaneo.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "monitor_server:app"]
