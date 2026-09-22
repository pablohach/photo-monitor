#!/usr/bin/env python3
"""
photo_batcher.py

Monitorea SRC_DIR, que contiene subdirectorios (uno por camara/origen, etc).
Cada subdirectorio se trackea de forma INDEPENDIENTE: cuando en ese
subdirectorio se junta MAX_COUNT fotos o pasan TIME_LIMIT_MIN minutos desde
que aparecio la primera foto pendiente (lo que ocurra primero), se procesa
el lote de ESE subdirectorio.

Al procesar un lote de un subdirectorio "camara1":
  DST_DIR/yyyy-mm-dd/camara1/HHmm Hs/
      001.jpg      001_#T.jpg (thumb THUMB_SIZE)   001_#V.jpg (visual VISUAL_SIZE)
      002.jpg      002_#T.jpg                       002_#V.jpg
      ...

La carpeta "HHmm Hs" usa la hora de la PRIMERA foto detectada en el lote
(no la hora en que se dispara el procesamiento). Si ya existe, se le suma
1 minuto hasta encontrar una libre (si cruza medianoche, la fecha se ajusta
sola).

Los jpg con tag EXIF de orientacion se rotan fisicamente al generarse.

Formatos RAW (cr2, nef, etc) no se pueden procesar con Pillow: se mueven
renombrados (001.cr2) pero sin generar thumb/visual.

DETECCION DE DUPLICADOS POR TRANSFERENCIA CORTADA: cuando la camara reintenta
subir un archivo cuyo nombre ya existe en el servidor (por un corte de WiFi a
mitad de subida), sube una copia con sufijo "-N" (ej: DSC_0818.JPG y
DSC_0818-1.JPG). El script detecta estos grupos, se queda con la version de
mayor tamano (la que llego completa) y manda la otra a QUARANTINE_DIR para
revision manual, en vez de procesar ambas como fotos distintas.

DETECCION DE ARCHIVOS CORRUPTOS/TRUNCADOS: incluso sin un "-1" de por medio,
un archivo puede quedar truncado para siempre (la camara nunca reintento).
Se valida que cada JPEG/PNG/etc se pueda decodificar por completo; si no,
se borra directamente (no tiene ningun uso guardar una foto ilegible).

LIMPIEZA POR FECHA: si un archivo estable no corresponde al dia de HOY (por
ejemplo, quedo trabado de una sesion anterior y nunca se proceso), se borra
en vez de mezclarlo con el lote del dia actual.

ESTABILIDAD SIN BLOQUEO: is_stable() no usa time.sleep(). Trackea el tamano
de cada archivo en cada vuelta del loop principal (que ya corre cada
POLL_INTERVAL_SEC) y calcula cuanto tiempo real paso desde que dejo de
cambiar de tamano. Esto evita que procesar un lote grande (ej. 100 fotos)
tarde varios minutos solo en checks de estabilidad.

FIX carpetas vacias: el trigger por tiempo/cantidad ahora solo cuenta
archivos ya CONFIRMADOS estables (antes, un archivo solo, sin duplicado,
contaba para el lote aunque siguiera subiendose). Si al cumplirse el tiempo
limite no hay ningun archivo realmente listo, no se crea ninguna carpeta:
se loguea una advertencia y se reinicia el timer.
"""

import os
import re
import time
import shutil
import logging
from datetime import datetime, timedelta

from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# CONFIGURACION - ajustar a gusto
# ---------------------------------------------------------------------------
SRC_DIR = "/mnt/nas/fotos/origen"  # contiene subdirectorios (camara1, camara2, ...)
DST_DIR = "/mnt/nas/fotos/destino"

TIME_LIMIT_MIN = 20  # minutos maximos de espera antes de mover el lote
MAX_COUNT = 100  # cantidad maxima de fotos antes de mover el lote
POLL_INTERVAL_SEC = 5  # cada cuanto se revisa el directorio
STABLE_WAIT_SEC = 3  # segundos reales sin cambio de tamano para considerar
# un archivo "terminado de subir" (sin bloquear el loop)

THUMB_SIZE = 140  # lado maximo del thumbnail (_#T)
VISUAL_SIZE = 1200  # lado maximo de la version visual (_#V)
JPEG_QUALITY = 92

# Extensiones que Pillow puede abrir y re-procesar (thumb + visual + rotacion)
PIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
# Extensiones RAW u otras que se mueven renombradas sin procesar
OTHER_EXTENSIONS = {".cr2", ".nef", ".raw", ".arw", ".dng"}

ALL_EXTENSIONS = PIL_EXTENSIONS | OTHER_EXTENSIONS

# Directorio donde se mueven los archivos "perdedores" cuando se detectan
# duplicados con sufijo -N (ej: DSC_0818.JPG y DSC_0818-1.JPG), tipicos de
# una transferencia SFTP cortada y reintentada por la camara. Se organiza
# en subcarpetas por camara, para revision manual si hace falta.
QUARANTINE_DIR = "/mnt/nas/fotos/sospechosos"

LOG_FILE = "/opt/photo_batcher/logs/photo_batcher.log"
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger().addHandler(console)


# ---------------------------------------------------------------------------
# Estabilidad de archivos SIN bloqueo (evita el time.sleep() por archivo)
# ---------------------------------------------------------------------------

_size_history = {}  # path -> (size, timestamp desde que tiene ese tamano)


def is_stable(path):
    """True si el archivo no cambio de tamano en los ultimos STABLE_WAIT_SEC
    segundos reales. No bloquea: se apoya en el historial que se va
    actualizando en cada vuelta del loop principal (ver refresh_stability)."""
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return False

    now = time.time()
    prev = _size_history.get(path)
    if prev is None or prev[0] != size:
        _size_history[path] = (size, now)
        return False
    return (now - prev[1]) >= STABLE_WAIT_SEC


def refresh_stability(files):
    """Actualiza el historial de tamanios para una lista de archivos. Se
    llama en cada poll, independientemente de si ya toca procesar el lote,
    para que la estabilidad se vaya resolviendo en el tiempo (sin sleep)."""
    for f in files:
        is_stable(f)


def prune_size_history(subdir_path, current_files):
    """Elimina del historial los archivos de este subdirectorio que ya no
    estan presentes (fueron movidos/procesados), para no crecer sin limite."""
    prefix = subdir_path + os.sep
    current_set = set(current_files)
    for p in list(_size_history.keys()):
        if p.startswith(prefix) and p not in current_set:
            _size_history.pop(p, None)


# ---------------------------------------------------------------------------
# Utilidades de archivos
# ---------------------------------------------------------------------------


def list_subdirs(base_dir):
    """Lista los subdirectorios inmediatos de base_dir."""
    try:
        return sorted(
            d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))
        )
    except FileNotFoundError:
        logging.error(f"SRC_DIR no accesible (mount caido?): {base_dir}")
        return []


def list_ready_files(subdir_path):
    """Lista los archivos de foto dentro de un subdirectorio, ordenados
    por fecha de modificacion (mas viejo primero)."""
    files = []
    try:
        for f in os.listdir(subdir_path):
            full = os.path.join(subdir_path, f)
            if (
                os.path.isfile(full)
                and os.path.splitext(f)[1].lower() in ALL_EXTENSIONS
            ):
                files.append(full)
    except FileNotFoundError:
        return []
    return sorted(files, key=os.path.getmtime)


# ---------------------------------------------------------------------------
# Deteccion de duplicados por transferencia cortada (DSC_0818.JPG /
# DSC_0818-1.JPG). La camara agrega el sufijo "-N" cuando reintenta subir un
# archivo cuyo nombre ya existe en el servidor (version truncada por un
# corte de WiFi a mitad de subida). Nos quedamos con la version mas grande
# (la que llego completa) y mandamos la otra a cuarentena para revision.
# ---------------------------------------------------------------------------

_DUP_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")


def _duplicate_key(path):
    """Nombre base sin el sufijo -N, en minusculas, junto con la extension."""
    name = os.path.basename(path)
    base, ext = os.path.splitext(name)
    m = _DUP_SUFFIX_RE.match(base)
    base_key = m.group(1) if m else base
    return (base_key.lower(), ext.lower())


def quarantine_file(subdir_name, path):
    """Mueve un archivo sospechoso de transferencia incompleta a
    QUARANTINE_DIR/subdir_name/, en vez de borrarlo (por si hace falta
    revisarlo despues)."""
    try:
        qdir = os.path.join(QUARANTINE_DIR, subdir_name)
        os.makedirs(qdir, exist_ok=True)

        name = os.path.basename(path)
        dest = os.path.join(qdir, name)
        if os.path.exists(dest):
            base, ext = os.path.splitext(name)
            dest = os.path.join(qdir, f"{base}_{int(time.time())}{ext}")

        shutil.move(path, dest)
        logging.warning(
            f"[{subdir_name}] Posible transferencia incompleta, "
            f"movido a cuarentena: {path} -> {dest}"
        )
    except Exception as e:
        logging.error(f"[{subdir_name}] Error moviendo a cuarentena {path}: {e}")


def resolve_duplicates(subdir_name, files):
    """Agrupa archivos por nombre base (ignorando el sufijo -N). Para cada
    grupo con mas de un archivo:
      - si TODOS ya estan estables (subida terminada), se queda con el de
        mayor tamano y manda el resto a cuarentena.
      - si alguno todavia se esta subiendo, no decide nada esta vuelta (evita
        comparar una version truncada contra otra que aun no termino) y
        tampoco cuenta ninguno de ese grupo para el lote todavia.
    Devuelve la lista de archivos que siguen "en juego" para el batch."""
    groups = {}
    for f in files:
        groups.setdefault(_duplicate_key(f), []).append(f)

    resolved = []
    for key, group in groups.items():
        if len(group) == 1:
            resolved.append(group[0])
            continue

        if not all(is_stable(f) for f in group):
            # todavia hay alguna version subiendo: no se cuenta este grupo
            # para el trigger de cantidad/tiempo todavia
            continue

        try:
            group_sorted = sorted(group, key=os.path.getsize, reverse=True)
        except FileNotFoundError:
            continue

        winner = group_sorted[0]
        resolved.append(winner)
        logging.info(
            f"[{subdir_name}] Duplicado detectado, se conserva la version "
            f"mas grande: {os.path.basename(winner)}"
        )

        for loser in group_sorted[1:]:
            quarantine_file(subdir_name, loser)

    return resolved


# ---------------------------------------------------------------------------
# Validacion de archivos ya estables: corrupcion e "es de hoy"
# ---------------------------------------------------------------------------


def is_corrupt_image(path):
    """Intenta decodificar la imagen completa (no solo verify()). Devuelve
    True si esta truncada/corrupta. Solo aplica a formatos que Pillow puede
    abrir (PIL_EXTENSIONS); para RAW u otros no validamos (Pillow no los
    puede leer de entrada)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in PIL_EXTENSIONS:
        return False
    try:
        with Image.open(path) as img:
            img.verify()
        # verify() invalida el objeto Image; hay que reabrirlo para decodificar
        with Image.open(path) as img2:
            img2.load()
        return False
    except Exception:
        return True


def is_from_today(path):
    """True si la fecha de modificacion del archivo es la de HOY (fecha real
    del servidor al momento de chequear). Si no se puede determinar, se
    asume True para no borrar por error."""
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return True
    return datetime.fromtimestamp(mtime).date() == datetime.now().date()


def cleanup_invalid_files(subdir_name, files):
    """Para archivos YA ESTABLES: borra los que esten corruptos/truncados, y
    los que no sean de HOY (restos de una sesion anterior que nunca se
    procesaron). Los archivos que todavia se estan subiendo se dejan pasar
    sin tocar (no tiene sentido validar un archivo a medio escribir).
    Devuelve la lista de archivos que pasaron ambos checks (o que todavia
    estan subiendo, para que sigan su curso normal)."""
    valid = []
    for f in files:
        if not is_stable(f):
            valid.append(f)
            continue

        if is_corrupt_image(f):
            try:
                size = os.path.getsize(f)
            except FileNotFoundError:
                size = "?"
            logging.warning(
                f"[{subdir_name}] Foto corrupta/truncada, se borra: "
                f"{os.path.basename(f)} ({size} bytes)"
            )
            try:
                os.remove(f)
            except Exception as e:
                logging.error(f"[{subdir_name}] Error borrando corrupta {f}: {e}")
            continue

        if not is_from_today(f):
            try:
                file_date = datetime.fromtimestamp(os.path.getmtime(f)).strftime(
                    "%Y-%m-%d"
                )
            except FileNotFoundError:
                file_date = "?"
            logging.warning(
                f"[{subdir_name}] Foto de otro dia ({file_date}), se borra: "
                f"{os.path.basename(f)}"
            )
            try:
                os.remove(f)
            except Exception as e:
                logging.error(f"[{subdir_name}] Error borrando foto vieja {f}: {e}")
            continue

        valid.append(f)

    return valid


# ---------------------------------------------------------------------------
# Resolucion del directorio destino con la regla "HHmm Hs" + colision
# ---------------------------------------------------------------------------


def resolve_dest_dir(dst_base, subdir_name, start_dt=None):
    """Devuelve un path DST_DIR/yyyy-mm-dd/subdir_name/HHmm Hs/ que no exista
    todavia. Si ya existe, prueba sumando 1 minuto (ajustando la fecha si
    cruza medianoche) hasta encontrar uno libre."""
    dt = start_dt or datetime.now()
    while True:
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H%M") + " Hs"
        candidate = os.path.join(dst_base, date_str, subdir_name, time_str)
        if not os.path.exists(candidate):
            return candidate
        dt += timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Procesamiento de cada foto: renombrado + thumb + visual + rotacion EXIF
# ---------------------------------------------------------------------------


def process_photo_pil(src_path, dest_dir, seq_str):
    """Abre la imagen con Pillow, aplica la rotacion segun EXIF, y guarda
    3 archivos: NNN.jpg, NNN_#T.jpg (thumb) y NNN_#V.jpg (visual)."""
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)

        if img.mode != "RGB":
            img = img.convert("RGB")

        main_path = os.path.join(dest_dir, f"{seq_str}.jpg")
        img.save(main_path, "JPEG", quality=JPEG_QUALITY)

        thumb = img.copy()
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        thumb_path = os.path.join(dest_dir, f"{seq_str}_#T.jpg")
        thumb.save(thumb_path, "JPEG", quality=JPEG_QUALITY)

        visual = img.copy()
        visual.thumbnail((VISUAL_SIZE, VISUAL_SIZE), Image.LANCZOS)
        visual_path = os.path.join(dest_dir, f"{seq_str}_#V.jpg")
        visual.save(visual_path, "JPEG", quality=JPEG_QUALITY)


def process_photo_raw(src_path, dest_dir, seq_str):
    """Para formatos que Pillow no puede procesar (RAW): solo renombra y
    mueve, sin generar thumb/visual."""
    ext = os.path.splitext(src_path)[1].lower()
    dest_path = os.path.join(dest_dir, f"{seq_str}{ext}")
    shutil.move(src_path, dest_path)
    logging.warning(
        f"Formato RAW sin soporte de thumb/visual, solo renombrado: {dest_path}"
    )


def process_batch(subdir_name, files, batch_start_ts=None):
    if not files:
        return

    # Defensa extra: si por algun motivo ninguno de los archivos pasados esta
    # realmente estable en este momento, no crear ninguna carpeta.
    if not any(is_stable(f) for f in files):
        logging.warning(
            f"[{subdir_name}] process_batch llamado pero ningun archivo esta "
            f"estable; se aborta sin crear carpeta"
        )
        return

    # Usa la hora de la primera foto detectada (inicio del lote), no la hora
    # en que se dispara el procesamiento (que puede ser varios minutos despues).
    start_dt = datetime.fromtimestamp(batch_start_ts) if batch_start_ts else None

    dest_dir = resolve_dest_dir(DST_DIR, subdir_name, start_dt=start_dt)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        logging.error(f"No se pudo crear directorio destino {dest_dir}: {e}")
        return

    logging.info(f"[{subdir_name}] Procesando lote de {len(files)} fotos -> {dest_dir}")

    seq = 1
    procesadas = 0
    for f in files:
        if not is_stable(f):
            logging.info(f"[{subdir_name}] Archivo aun en escritura, se pospone: {f}")
            continue

        seq_str = f"{seq:03d}"
        ext = os.path.splitext(f)[1].lower()

        try:
            if ext in PIL_EXTENSIONS:
                process_photo_pil(f, dest_dir, seq_str)
                os.remove(f)
            else:
                process_photo_raw(f, dest_dir, seq_str)

            logging.info(f"[{subdir_name}] {os.path.basename(f)} -> {seq_str}")
            seq += 1
            procesadas += 1
        except Exception as e:
            logging.error(f"[{subdir_name}] Error procesando {f}: {e}")

    logging.info(
        f"[{subdir_name}] Lote finalizado: {procesadas}/{len(files)} fotos procesadas"
    )


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------


def main():
    logging.info("=== photo_batcher iniciado ===")
    logging.info(f"SRC_DIR={SRC_DIR}  DST_DIR={DST_DIR}")
    logging.info(f"TIME_LIMIT_MIN={TIME_LIMIT_MIN}  MAX_COUNT={MAX_COUNT}")
    logging.info(f"THUMB_SIZE={THUMB_SIZE}  VISUAL_SIZE={VISUAL_SIZE}")
    logging.info(f"STABLE_WAIT_SEC={STABLE_WAIT_SEC} (sin bloqueo)")

    batch_start = {}

    while True:
        try:
            subdirs = list_subdirs(SRC_DIR)

            for subdir_name in subdirs:
                subdir_path = os.path.join(SRC_DIR, subdir_name)
                raw_files = list_ready_files(subdir_path)

                prune_size_history(subdir_path, raw_files)
                refresh_stability(raw_files)

                files = resolve_duplicates(subdir_name, raw_files)
                files = cleanup_invalid_files(subdir_name, files)

                # Solo los archivos YA ESTABLES cuentan para el trigger y se
                # pasan a process_batch. Uno que siga subiendo no debe hacer
                # que se cree una carpeta vacia si se cumple el tiempo limite.
                stable_files = [f for f in files if is_stable(f)]

                if files:
                    if subdir_name not in batch_start:
                        batch_start[subdir_name] = time.time()
                        logging.info(
                            f"[{subdir_name}] Nuevo lote detectado, "
                            f"primera foto: {os.path.basename(files[0])}"
                        )

                    elapsed_min = (time.time() - batch_start[subdir_name]) / 60.0

                    if len(stable_files) >= MAX_COUNT:
                        logging.info(
                            f"[{subdir_name}] Trigger por cantidad: "
                            f"{len(stable_files)} >= {MAX_COUNT}"
                        )
                        process_batch(
                            subdir_name,
                            stable_files,
                            batch_start_ts=batch_start[subdir_name],
                        )
                        batch_start.pop(subdir_name, None)
                    elif elapsed_min >= TIME_LIMIT_MIN:
                        if stable_files:
                            logging.info(
                                f"[{subdir_name}] Trigger por tiempo: "
                                f"{elapsed_min:.1f} min >= {TIME_LIMIT_MIN} min"
                            )
                            process_batch(
                                subdir_name,
                                stable_files,
                                batch_start_ts=batch_start[subdir_name],
                            )
                        else:
                            logging.warning(
                                f"[{subdir_name}] Pasaron {elapsed_min:.1f} min pero "
                                f"ningun archivo termino de subir todavia (posible "
                                f"transferencia estancada); no se crea carpeta vacia, "
                                f"se reinicia la espera"
                            )
                        batch_start.pop(subdir_name, None)
                else:
                    batch_start.pop(subdir_name, None)

            for name in list(batch_start.keys()):
                if name not in subdirs:
                    batch_start.pop(name, None)

        except Exception as e:
            logging.error(f"Error en loop principal: {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
