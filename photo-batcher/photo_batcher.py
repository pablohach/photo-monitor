#!/usr/bin/env python3
"""
photo_batcher.py

Monitorea SRC_DIR, que contiene subdirectorios (uno por camara/tema, etc).
Cada foto se procesa y se sube a destino DE FORMA INMEDIATA en cuanto se
detecta que esta estable (termino de subir por SFTP) y valida (sin corrupcion,
duplicado descartado y correspondiente al dia de hoy).

Estructura generada en DST_DIR:
  DST_DIR/yyyy-mm-dd/<camara>/HHmm Hs/
      0001.jpg      0001_#T.jpg (thumb THUMB_SIZE)   0001_#V.jpg (visual VISUAL_SIZE)
      0002.jpg      0002_#T.jpg                       0002_#V.jpg
      ...

Asignacion de carpetas horarias (HHmm Hs):
  a) Si la ultima carpeta tiene menos de MAX_COUNT fotos y no supero el
     tiempo de inactividad (FOLDER_TIMEOUT_MIN), se guarda ahi con el
     siguiente numero correlativo.
  b) Si la ultima carpeta alcanzo MAX_COUNT fotos o pasaron mas de
     FOLDER_TIMEOUT_MIN minutos de inactividad (si FOLDER_TIMEOUT_MIN > 0),
     se crea una nueva carpeta con la hora actual (ej: "1258 Hs") y se
     continua la numeracion correlativa del dia.
  c) Si todavia no existe ninguna carpeta hoy, se crea con la hora actual y
     se comienza desde la foto 1 (ej: 0001.jpg).

Formato de fotos:
  - Cantidad de digitos configurable mediante FILENAME_DIGITS (ej: 4 -> 0001.jpg).
  - Los jpg con tag EXIF de orientacion se rotan fisicamente al generarse.
  - Formatos RAW (cr2, nef, etc) no se procesan con Pillow: se mueven
    renombrados (0001.cr2) sin generar thumb/visual.

DETECCION DE DUPLICADOS POR TRANSFERENCIA CORTADA:
  Cuando la camara reintenta subir un archivo cuyo nombre ya existe en el
  servidor (por un corte de WiFi a mitad de subida), sube una copia con sufijo
  "-N" (ej: DSC_0818.JPG y DSC_0818-1.JPG). Se detectan estos grupos, se
  conserva la version de mayor tamano (la que llego completa) y se envian
  las demas a QUARANTINE_DIR.

DETECCION DE ARCHIVOS CORRUPTOS/TRUNCADOS:
  Se valida que cada imagen se pueda decodificar por completo; si esta
  incompleta o corrupta, se borra directamente.

LIMPIEZA POR FECHA:
  Si un archivo estable no corresponde al dia de HOY, se borra en vez de
  mezclarlo con las fotos del dia actual.

ESTABILIDAD SIN BLOQUEO:
  is_stable() no usa time.sleep(). Trackea el tamano de cada archivo en cada
  vuelta del loop principal y calcula el tiempo transcurrido sin cambios.
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

MAX_COUNT = (
    100  # cantidad maxima de fotos por carpeta horaria (al llenarse se crea una nueva)
)
FOLDER_TIMEOUT_MIN = 30  # minutos de inactividad para crear nueva carpeta aunque no este llena (0 = deshabilitado)
FILENAME_DIGITS = (
    4  # cantidad de digitos para el numero de foto (ej: 4 -> 0001.jpg, 3 -> 001.jpg)
)

POLL_INTERVAL_SEC = 5  # cada cuanto se revisa el directorio de origen
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
# una transferencia SFTP cortada y reintentada por la camara.
QUARANTINE_DIR = "/mnt/nas/fotos/sospechosos"

LOG_FILE = "/opt/photo_batcher/logs/photo_batcher.log"
# ---------------------------------------------------------------------------

try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
except Exception:
    pass

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
    llama en cada poll para que la estabilidad se vaya resolviendo en el
    tiempo sin sleep."""
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
# archivo cuyo nombre ya existe en el servidor. Nos quedamos con la version
# mas grande (la que llego completa) y mandamos la otra a cuarentena.
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
    QUARANTINE_DIR/subdir_name/, en vez de borrarlo."""
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
      - si alguno todavia se esta subiendo, no decide nada en esta vuelta y
        tampoco cuenta ninguno de ese grupo todavia.
    Devuelve la lista de archivos que siguen en juego."""
    groups = {}
    for f in files:
        groups.setdefault(_duplicate_key(f), []).append(f)

    resolved = []
    for key, group in groups.items():
        if len(group) == 1:
            resolved.append(group[0])
            continue

        if not all(is_stable(f) for f in group):
            # todavia hay alguna version subiendo
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
    True si esta truncada/corrupta. Solo aplica a formatos PIL_EXTENSIONS."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in PIL_EXTENSIONS:
        return False
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img2:
            img2.load()
        return False
    except Exception:
        return True


def is_from_today(path):
    """True si la fecha de modificacion del archivo es la de HOY. Si no se
    puede determinar, se asume True para no borrar por error."""
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return True
    return datetime.fromtimestamp(mtime).date() == datetime.now().date()


def cleanup_invalid_files(subdir_name, files):
    """Para archivos YA ESTABLES: borra los corruptos y los que no sean de HOY.
    Los archivos que todavia se estan subiendo se dejan pasar sin tocar."""
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
# Gestion de carpetas horarias y numeracion correlativa
# ---------------------------------------------------------------------------

_HOUR_FOLDER_RE = re.compile(r"^\d{4}\s+[Hh][Ss]$")


def is_main_photo(filename):
    """True si el archivo es una foto principal numerada (ej: 001.jpg, 0001.jpg, 0087.cr2)
    y no una miniatura o version visual (_#T, _#V)."""
    name, ext = os.path.splitext(filename)
    if ext.lower() not in ALL_EXTENSIONS:
        return False
    return name.isdigit()


def inspect_folder(folder_path):
    """Devuelve (count, max_seq, last_mtime) para las fotos principales
    en una carpeta horaria dada."""
    count = 0
    max_seq = 0
    last_mtime = 0.0
    try:
        for fname in os.listdir(folder_path):
            if is_main_photo(fname):
                full_path = os.path.join(folder_path, fname)
                if os.path.isfile(full_path):
                    count += 1
                    name, _ = os.path.splitext(fname)
                    val = int(name)
                    if val > max_seq:
                        max_seq = val
                    mtime = os.path.getmtime(full_path)
                    if mtime > last_mtime:
                        last_mtime = mtime
    except OSError:
        pass
    return count, max_seq, last_mtime


def get_hour_folders(day_dir):
    """Lista y ordena alfabeticamente/cronologicamente las carpetas horarias (HHmm Hs)."""
    try:
        return sorted(
            d
            for d in os.listdir(day_dir)
            if os.path.isdir(os.path.join(day_dir, d)) and _HOUR_FOLDER_RE.match(d)
        )
    except FileNotFoundError:
        return []


def get_next_seq(day_dir, hour_folders):
    """Devuelve el siguiente numero de secuencia correlativo para el dia de hoy,
    revisando las carpetas horarias de la mas reciente a la mas antigua."""
    for folder_name in reversed(hour_folders):
        folder_path = os.path.join(day_dir, folder_name)
        _, max_seq, _ = inspect_folder(folder_path)
        if max_seq > 0:
            return max_seq + 1
    return 1


def resolve_dest_folder(dst_base, subdir_name, max_count, timeout_min=0):
    """Determina la carpeta destino activa para la siguiente foto segun:
    a) Si la ultima carpeta tiene < MAX_COUNT y no expiro por inactividad, se usa esa.
    b) Si tiene >= MAX_COUNT o expiro por inactividad, se crea una nueva con la hora actual.
    c) Si todavia no existe ninguna carpeta hoy, se crea con la hora actual.

    Retorna (target_dir, day_dir, hour_folders)."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    day_dir = os.path.join(dst_base, date_str, subdir_name)
    os.makedirs(day_dir, exist_ok=True)

    hour_folders = get_hour_folders(day_dir)

    # Caso c: primera carpeta del dia
    if not hour_folders:
        time_str = datetime.now().strftime("%H%M") + " Hs"
        target_dir = os.path.join(day_dir, time_str)
        os.makedirs(target_dir, exist_ok=True)
        return target_dir, day_dir, [time_str]

    last_folder = hour_folders[-1]
    last_folder_path = os.path.join(day_dir, last_folder)
    count, _, last_mtime = inspect_folder(last_folder_path)

    # Chequeo de inactividad
    is_timed_out = False
    if timeout_min > 0 and count > 0 and last_mtime > 0:
        elapsed_min = (time.time() - last_mtime) / 60.0
        if elapsed_min >= timeout_min:
            is_timed_out = True
            logging.info(
                f"[{subdir_name}] Inactividad de {elapsed_min:.1f} min >= {timeout_min} min "
                f"en carpeta {last_folder}. Se creara una nueva carpeta horaria."
            )

    # Caso a: todavia entra y no expiro por inactividad
    if count < max_count and not is_timed_out:
        return last_folder_path, day_dir, hour_folders

    # Caso b: carpeta llena o supero tiempo -> crear nueva carpeta con hora actual
    dt = datetime.now()
    while True:
        candidate_name = dt.strftime("%H%M") + " Hs"
        candidate_path = os.path.join(day_dir, candidate_name)
        if not os.path.exists(candidate_path):
            os.makedirs(candidate_path, exist_ok=True)
            if candidate_name not in hour_folders:
                hour_folders.append(candidate_name)
                hour_folders.sort()
            return candidate_path, day_dir, hour_folders

        # Si ya existe, verificar si tiene espacio y no esta expirada
        cand_count, _, cand_mtime = inspect_folder(candidate_path)
        cand_timed_out = False
        if timeout_min > 0 and cand_count > 0 and cand_mtime > 0:
            if (time.time() - cand_mtime) / 60.0 >= timeout_min:
                cand_timed_out = True

        if cand_count < max_count and not cand_timed_out:
            return candidate_path, day_dir, hour_folders

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


def process_single_photo(subdir_name, src_path):
    """Procesa una unica foto estable, resolviendo la carpeta destino y
    su numero correlativo."""
    try:
        dest_dir, day_dir, hour_folders = resolve_dest_folder(
            DST_DIR, subdir_name, MAX_COUNT, timeout_min=FOLDER_TIMEOUT_MIN
        )
        seq = get_next_seq(day_dir, hour_folders)
        seq_str = f"{seq:0{FILENAME_DIGITS}d}"

        ext = os.path.splitext(src_path)[1].lower()

        if ext in PIL_EXTENSIONS:
            process_photo_pil(src_path, dest_dir, seq_str)
            os.remove(src_path)
        else:
            process_photo_raw(src_path, dest_dir, seq_str)

        logging.info(
            f"[{subdir_name}] {os.path.basename(src_path)} -> {seq_str} "
            f"en {os.path.basename(dest_dir)}"
        )
        return True
    except Exception as e:
        logging.error(f"[{subdir_name}] Error procesando {src_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------


def main():
    logging.info("=== photo_batcher iniciado ===")
    logging.info(f"SRC_DIR={SRC_DIR}  DST_DIR={DST_DIR}")
    logging.info(
        f"MAX_COUNT={MAX_COUNT}  FOLDER_TIMEOUT_MIN={FOLDER_TIMEOUT_MIN}  "
        f"FILENAME_DIGITS={FILENAME_DIGITS}"
    )
    logging.info(f"THUMB_SIZE={THUMB_SIZE}  VISUAL_SIZE={VISUAL_SIZE}")
    logging.info(f"STABLE_WAIT_SEC={STABLE_WAIT_SEC} (sin bloqueo)")

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

                # Las fotos estables se procesan de inmediato
                stable_files = [f for f in files if is_stable(f)]
                for f in stable_files:
                    process_single_photo(subdir_name, f)

        except Exception as e:
            logging.error(f"Error en loop principal: {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
