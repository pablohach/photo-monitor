#!/usr/bin/env python3
"""
monitor_server.py

Servidor web liviano para monitorear EN VIVO las fotos que van llegando
por SFTP a cada subdirectorio de camara, antes de que photo_batcher.py
las procese y mueva al NAS.

Pensado para abrir en un navegador desde una PC con Windows cerca de
cada camara:

    http://<ip-del-ubuntu>:8080/                -> lista de camaras
    http://<ip-del-ubuntu>:8080/camara/camara1   -> grilla en vivo de camara1

La pagina se auto-refresca sola. Si no llega una foto nueva en mas de
STALE_MIN minutos, el indicador de estado pasa a naranja/rojo para que
el operador note que algo dejo de subir.
"""

import os
import io
import time
import threading
from datetime import datetime

from flask import Flask, render_template_string, send_file, abort, Response
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# CONFIGURACION - debe apuntar al mismo SRC_DIR que usa photo_batcher.py
# Todo es configurable por variable de entorno (util para Docker); si no se
# define la variable, se usa el valor por defecto de mas abajo.
# ---------------------------------------------------------------------------
SRC_DIR = os.environ.get("SRC_DIR", "/mnt/fotos/origen/nas")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

REFRESH_SEC = int(os.environ.get("REFRESH_SEC", "4"))            # cada cuanto se auto-refresca la pagina
MONITOR_THUMB_SIZE = int(os.environ.get("MONITOR_THUMB_SIZE", "400"))  # lado maximo de las miniaturas

STALE_WARN_MIN = float(os.environ.get("STALE_WARN_MIN", "2"))    # sin fotos nuevas hace mas de esto -> aviso naranja
STALE_ALERT_MIN = float(os.environ.get("STALE_ALERT_MIN", "5"))  # sin fotos nuevas hace mas de esto -> aviso rojo

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
RAW_EXTENSIONS = {".cr2", ".nef", ".raw", ".arw", ".dng"}
ALL_EXTENSIONS = IMAGE_EXTENSIONS | RAW_EXTENSIONS
# ---------------------------------------------------------------------------

app = Flask(__name__)


def list_cameras():
    try:
        return sorted(
            d for d in os.listdir(SRC_DIR)
            if os.path.isdir(os.path.join(SRC_DIR, d))
        )
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Tracking de "primera vez visto" por archivo, en memoria.
#
# OJO: no usamos el mtime del archivo para calcular la frescura, porque SFTP
# (y muchos clientes/camaras) preservan la fecha original del archivo en vez
# de poner "ahora" al subirlo. Si una camara tenia fotos viejas en el buffer,
# o si el archivo se copio con una herramienta que preserva timestamps, el
# mtime puede ser de anos atras. Lo que realmente importa para detectar
# "la camara dejo de subir" es cuando ESTE PROCESO vio aparecer el archivo
# por primera vez, asi que lo trackeamos nosotros mismos.
# ---------------------------------------------------------------------------
_first_seen = {}
_first_seen_lock = threading.Lock()


def list_photos(camera):
    """Devuelve [(nombre, path, first_seen), ...] ordenado por mas reciente
    primero. first_seen es el momento en que ESTE proceso detecto el
    archivo por primera vez (no el mtime del archivo)."""
    path = os.path.join(SRC_DIR, camera)
    try:
        entries = os.listdir(path)
    except FileNotFoundError:
        return []

    now = time.time()
    photos = []
    current_paths = set()

    with _first_seen_lock:
        for name in entries:
            full = os.path.join(path, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in ALL_EXTENSIONS:
                current_paths.add(full)
                if full not in _first_seen:
                    _first_seen[full] = now
                photos.append((name, full, _first_seen[full]))

        # Poda: se olvida de archivos de esta camara que ya no estan
        # (fueron movidos por photo_batcher.py), para no crecer sin limite.
        camera_prefix = path + os.sep
        for tracked_path in list(_first_seen.keys()):
            if tracked_path.startswith(camera_prefix) and tracked_path not in current_paths:
                _first_seen.pop(tracked_path, None)

    photos.sort(key=lambda p: p[2], reverse=True)
    return photos


def freshness_status(last_seen):
    """Devuelve (clase_css, texto) segun hace cuanto ESTE PROCESO detecto
    la ultima foto (no el mtime del archivo)."""
    if last_seen is None:
        return "status-none", "Sin fotos pendientes"

    elapsed_min = (time.time() - last_seen) / 60.0
    if elapsed_min < STALE_WARN_MIN:
        return "status-ok", f"Ultima foto hace {elapsed_min:.1f} min"
    elif elapsed_min < STALE_ALERT_MIN:
        return "status-warn", f"Sin novedades hace {elapsed_min:.1f} min"
    else:
        return "status-alert", f"⚠ Sin novedades hace {elapsed_min:.1f} min"


# ---------------------------------------------------------------------------
# Templates (embebidos para no depender de una carpeta templates/)
# ---------------------------------------------------------------------------

BASE_STYLE = """
<style>
  body { background:#111; color:#eee; font-family: Arial, sans-serif; margin:0; padding:20px; }
  h1 { font-size: 28px; }
  a { color: #6cf; text-decoration:none; }
  .cameras { display:flex; gap:16px; flex-wrap:wrap; margin-top:20px; }
  .camera-link { background:#222; padding:20px 30px; border-radius:10px; font-size:22px; }
  .status-bar { padding:14px 20px; border-radius:8px; font-size:20px; margin-bottom:20px; font-weight:bold; }
  .status-ok    { background:#1b4d1b; color:#8f8; }
  .status-warn  { background:#5a4b12; color:#fd6; }
  .status-alert { background:#5a1414; color:#f77; }
  .status-none  { background:#333; color:#aaa; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap:14px; }
  .card { background:#1c1c1c; border-radius:8px; overflow:hidden; }
  .card img { width:100%; display:block; }
  .card .meta { padding:6px 10px; font-size:13px; color:#aaa; }
  .clock { float:right; font-size:16px; color:#888; }
</style>
"""

INDEX_HTML = BASE_STYLE + """
<h1>Monitor de camaras <span class="clock">{{ now }}</span></h1>
<div class="cameras">
  {% for cam in cameras %}
    <a class="camera-link" href="/camara/{{ cam }}">📷 {{ cam }}</a>
  {% else %}
    <p>No hay subdirectorios de camaras en {{ src_dir }} todavia.</p>
  {% endfor %}
</div>
"""

CAMERA_HTML = BASE_STYLE + """
<meta http-equiv="refresh" content="{{ refresh }}">
<h1>📷 {{ camera }} <span class="clock">{{ now }}</span></h1>
<div class="status-bar {{ status_class }}">{{ status_text }} &nbsp;|&nbsp; {{ count }} foto(s) pendiente(s)</div>
<p><a href="/">&larr; volver</a></p>
<div class="grid">
  {% for name, mtime_str, is_raw in photos %}
    <div class="card">
      {% if is_raw %}
        <div class="meta" style="padding-top:60px;text-align:center;">📄 RAW<br>(sin preview)</div>
      {% else %}
        <img src="/thumb/{{ camera }}/{{ name }}?v={{ mtime_str }}" loading="lazy">
      {% endif %}
      <div class="meta">{{ name }}<br>{{ mtime_str }}</div>
    </div>
  {% endfor %}
</div>
"""


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        cameras=list_cameras(),
        src_dir=SRC_DIR,
        now=datetime.now().strftime("%H:%M:%S"),
    )


@app.route("/camara/<camera>")
def camera_view(camera):
    if camera not in list_cameras():
        abort(404, f"No existe el subdirectorio de camara '{camera}'")

    photos = list_photos(camera)
    last_seen = photos[0][2] if photos else None
    status_class, status_text = freshness_status(last_seen)

    photos_view = [
        (name, datetime.fromtimestamp(first_seen).strftime("%H:%M:%S"), ext_is_raw(name))
        for name, _, first_seen in photos
    ]

    return render_template_string(
        CAMERA_HTML,
        camera=camera,
        photos=photos_view,
        count=len(photos),
        status_class=status_class,
        status_text=status_text,
        refresh=REFRESH_SEC,
        now=datetime.now().strftime("%H:%M:%S"),
    )


def ext_is_raw(filename):
    return os.path.splitext(filename)[1].lower() in RAW_EXTENSIONS


@app.route("/thumb/<camera>/<filename>")
def thumb(camera, filename):
    if camera not in list_cameras():
        abort(404)

    path = os.path.join(SRC_DIR, camera, filename)
    if not os.path.isfile(path):
        abort(404)

    ext = os.path.splitext(filename)[1].lower()
    if ext in RAW_EXTENSIONS:
        abort(415, "No se puede generar preview de formatos RAW")

    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((MONITOR_THUMB_SIZE, MONITOR_THUMB_SIZE), Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            buf.seek(0)
            resp = Response(buf.getvalue(), mimetype="image/jpeg")
            # Cachea por archivo: el query param ?v=mtime cambia solo si el
            # archivo cambio, asi el navegador reusa la miniatura sin pedirla
            # de nuevo en cada auto-refresh.
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        abort(500, str(e))


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
