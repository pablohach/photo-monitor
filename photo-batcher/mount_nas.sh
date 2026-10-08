#!/bin/bash
#
# mount_nas.sh
#
# Reintenta montar los CIFS del NAS (definidos en /etc/fstab) y, cuando
# ambos estan disponibles, se asegura de que photo-batcher este corriendo.
# Pensado para ejecutarse periodicamente desde nas-mount-retry.timer.
#
# Los "timeout" evitan que el script se cuelgue minutos si el NAS esta
# apagado o si un mount quedo "stale" (colgado).

MOUNTS=(
    /mnt/nas/fotos/origen
    /mnt/nas/fotos/destino
)
SERVICE=photo-batcher.service

all_ok=1

for m in "${MOUNTS[@]}"; do
    # Solo intenta montar si NO esta montado (evita el error "already mounted"
    # y no hace falta esconder los errores con 2>/dev/null)
    if ! timeout 10 mountpoint -q "$m"; then
        echo "$m no esta montado, intentando montar..."
        timeout 60 mount "$m" || echo "ERROR: no se pudo montar $m (NAS apagado o inaccesible?)"
    fi

    if ! timeout 10 mountpoint -q "$m"; then
        all_ok=0
    fi
done

if [ "$all_ok" -eq 1 ]; then
    # Solo arranca el servicio si esta habilitado: si lo deshabilitaste a
    # proposito (systemctl disable --now) para hacer mantenimiento, no se
    # vuelve a levantar solo.
    if systemctl is-enabled --quiet "$SERVICE" && ! systemctl is-active --quiet "$SERVICE"; then
        echo "Mounts OK, iniciando $SERVICE"
        systemctl start "$SERVICE"
    fi
fi
