# Instalación

## 1. Montar los directorios del NAS (persistente en /etc/fstab)

Editar `/etc/fstab` y agregar las entradas correspondientes al protocolo de tu NAS.

### Si el NAS expone NFS
```
192.168.1.50:/volumen1/origen   /mnt/nas/origen   nfs   defaults,_netdev,noatime  0  0
192.168.1.50:/volumen1/destino  /mnt/nas/destino  nfs   defaults,_netdev,noatime  0  0
```

### Si el NAS expone CIFS/SMB
```
//192.168.1.50/origen   /mnt/nas/origen   cifs  credentials=/etc/nas-credentials,uid=tu_usuario,gid=tu_usuario,_netdev  0  0
//192.168.1.50/destino  /mnt/nas/destino  cifs  credentials=/etc/nas-credentials,uid=tu_usuario,gid=tu_usuario,_netdev  0  0
```

Crear el archivo de credenciales (si usás CIFS) y protegerlo:
```bash
sudo nano /etc/nas-credentials
# contenido:
# username=usuario_nas
# password=clave_nas

sudo chmod 600 /etc/nas-credentials
```

Crear los puntos de montaje y montar:
```bash
sudo mkdir -p /mnt/nas/origen /mnt/nas/destino
sudo mount -a
```

`_netdev` le indica al sistema que espere a tener red antes de intentar montar (importante para que funcione bien en el boot).

## 2. Instalar el script y sus dependencias

```bash
sudo mkdir -p /opt/photo_batcher
sudo cp photo_batcher.py /opt/photo_batcher/
sudo chown tu_usuario:tu_usuario /opt/photo_batcher/photo_batcher.py

# Pillow es necesario para generar thumbnail/visual y rotar segun EXIF
pip install Pillow --break-system-packages
```

Editar las variables de configuración al inicio de `photo_batcher.py`:
- `SRC_DIR` (debe contener subdirectorios, uno por cada origen: `SRC_DIR/camara1`, `SRC_DIR/camara2`, etc.)
- `DST_DIR`
- `TIME_LIMIT_MIN`, `MAX_COUNT` (se evalúan de forma independiente por cada subdirectorio)
- `THUMB_SIZE` / `VISUAL_SIZE`
- `PIL_EXTENSIONS` / `OTHER_EXTENSIONS` según los formatos de tu cámara

### Estructura de origen esperada
```
SRC_DIR/
  camara1/
    IMG_001.jpg
    IMG_002.jpg
  camara2/
    foto1.jpg
```

### Estructura de destino generada
Cada subdirectorio se procesa de forma independiente. Al dispararse el lote de
`camara1`, se crea:
```
DST_DIR/
  2026-07-25/
    camara1/
      1258 Hs/
        001.jpg      001_#T.jpg (thumb)   001_#V.jpg (visual)
        002.jpg      002_#T.jpg           002_#V.jpg
```
Si `1258 Hs` ya existe (por ejemplo porque hubo otro lote en el mismo minuto),
se prueba `1259 Hs`, `1300 Hs`, etc. hasta encontrar uno libre.

### Nota sobre formatos RAW
Los formatos en `OTHER_EXTENSIONS` (cr2, nef, raw, arw, dng) no pueden ser
abiertos por Pillow, así que **no se les genera thumbnail ni visual**: solo
se renombran y mueven (ej. `001.cr2`). Si necesitás procesar RAW con
thumb/visual, avisá para agregar soporte vía `rawpy` o `exiftool`.

## 3. Instalar el servicio systemd

```bash
sudo cp photo-batcher.service /etc/systemd/system/
sudo nano /etc/systemd/system/photo-batcher.service   # ajustar User/Group

sudo systemctl daemon-reload
sudo systemctl enable photo-batcher
sudo systemctl start photo-batcher
```

## 4. Verificar

```bash
sudo systemctl status photo-batcher
tail -f /var/log/photo_batcher.log
# o
journalctl -u photo-batcher -f
```

## 5. Monitor web en vivo (opcional, recomendado)

Para que cada PC con Windows, cerca de cada cámara, pueda ver en el navegador
las fotos que van llegando por SFTP en tiempo real:

```bash
sudo cp monitor_server.py /opt/photo_batcher/
./venv/bin/pip install Flask  # (si usás el venv sugerido)

sudo cp photo-monitor.service /etc/systemd/system/
sudo nano /etc/systemd/system/photo-monitor.service   # ajustar User/Group

sudo systemctl daemon-reload
sudo systemctl enable photo-monitor
sudo systemctl start photo-monitor
```

Editar `SRC_DIR` en `monitor_server.py` para que apunte al mismo directorio
que usa `photo_batcher.py`.

### Desde cada PC Windows

Abrir un navegador (Chrome/Edge) apuntando a:
```
http://<ip-del-ubuntu>:8080/camara/camara1
```
(reemplazando `camara1` por el nombre del subdirectorio de esa cámara).

La página se auto-refresca sola cada pocos segundos, muestra miniaturas de
las fotos pendientes (aún no movidas por `photo_batcher.py`), y un indicador
de color:
- 🟢 **verde**: llegó una foto hace poco
- 🟠 **naranja**: hace más de `STALE_WARN_MIN` minutos sin novedades
- 🔴 **rojo**: hace más de `STALE_ALERT_MIN` minutos sin novedades (revisar la cámara)

Para pantalla completa tipo kiosco en Windows con Edge:
```
msedge.exe --kiosk "http://<ip-del-ubuntu>:8080/camara/camara1" --edge-kiosk-type=fullscreen
```

**Nota de seguridad**: este servidor no tiene autenticación ni HTTPS — pensado
para uso dentro de la LAN local únicamente. Si la red no es de confianza,
convendría ponerlo detrás de un proxy con autenticación o restringir el
puerto 8080 por firewall a las IPs de esas PCs.

## Notas
- El script chequea que cada archivo tenga tamaño estable antes de moverlo, para no
  mover una foto que todavía se está copiando al origen.
- Si el mount del NAS se cae, el script logea el error y sigue reintentando en el
  siguiente ciclo, sin crashear.
- Si dos fotos llegan con el mismo nombre en distintos lotes, el destino les agrega
  un sufijo (`_1`, `_2`, ...) en vez de sobrescribir.
- El monitor web y `photo_batcher.py` leen del mismo `SRC_DIR` pero no compiten entre
  sí: el monitor solo lee/genera thumbnails al vuelo, nunca mueve ni borra archivos.
