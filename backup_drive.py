"""
Motor de Backup Automático de la carpeta 'datos/' hacia Google Drive en Wi-Fi Seguro.

Diseñado para la Versión Celular Offline:
1. Mientras el usuario trabaja sin internet, marca automáticamente que hay cambios nuevos.
2. Mantiene una copia consistente (hot-backup de SQLite) de toda la carpeta `datos/`
   (base de datos `pos_local.db`, `config_negocio.json` e imágenes `uploads/`).
3. En segundo plano, revisa si el celular se conectó a internet y si está en el
   "Wi-Fi Seguro" elegido por el usuario (o cualquier Wi-Fi).
4. Cuando detecta el Wi-Fi seguro:
   - Actualiza la carpeta espejo lista para sincronizar con Google Drive (Autosync / FolderSync / Drive).
   - Si el usuario configuró el Puente Directo de Google Drive (Apps Script URL),
     sube automáticamente el archivo `datos_backup_actual.zip` a su Google Drive.
5. Permite restaurar la carpeta `datos/` completa desde un archivo `.zip` descargado de Google Drive.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from formato import fmt_fecha

log = logging.getLogger("pos.backup_drive")
TZ_AR = timezone(timedelta(hours=-3))

_lock_backup = threading.Lock()
_vigilante_iniciado = False


def _ahora() -> datetime:
    return datetime.now(TZ_AR)


def _ahora_str() -> str:
    return fmt_fecha(_ahora())


def obtener_rutas_base(app_dir: Path) -> Dict[str, Path]:
    datos_dir = app_dir / "datos"
    backups_dir = datos_dir / "backups_drive"
    espejo_dir = backups_dir / "datos_espejo"
    estado_file = datos_dir / "estado_backup.json"
    db_file = Path(os.getenv("POS_DB_FILE") or (datos_dir / "pos_local.db"))
    config_file = app_dir / "config_negocio.json"
    uploads_static = app_dir / "static" / "uploads"
    uploads_datos = datos_dir / "uploads"

    datos_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    espejo_dir.mkdir(parents=True, exist_ok=True)

    return {
        "app_dir": app_dir,
        "datos_dir": datos_dir,
        "backups_dir": backups_dir,
        "espejo_dir": espejo_dir,
        "estado_file": estado_file,
        "db_file": db_file,
        "config_file": config_file,
        "uploads_static": uploads_static,
        "uploads_datos": uploads_datos,
    }


def _carpeta_visible_celular_por_defecto(app_dir: Path) -> str:
    """
    Si corre en Android (Termux), devuelve /sdcard/POS_Backup_Drive para que sea
    fácil de encontrar en 'Mis Archivos' y en Autosync for Google Drive.
    En PC o si no existe /sdcard, devuelve datos/backups_drive.
    """
    sdcard = Path("/sdcard")
    storage_emulated = Path("/storage/emulated/0")
    if sdcard.exists() and os.access(str(sdcard), os.W_OK):
        return str(sdcard / "POS_Backup_Drive")
    if storage_emulated.exists() and os.access(str(storage_emulated), os.W_OK):
        return str(storage_emulated / "POS_Backup_Drive")
    return str(app_dir / "datos" / "backups_drive")


# ---------------------------------------------------------------------------
# Estado persistente del Backup
# ---------------------------------------------------------------------------
def leer_estado_backup(app_dir: Path) -> Dict[str, Any]:
    rutas = obtener_rutas_base(app_dir)
    estado_defecto: Dict[str, Any] = {
        "cambios_pendientes": False,
        "ultimo_cambio_local": "",
        "ultimo_backup_local": "",
        "ultimo_backup_nube": "",
        "ultimo_estado": "sin_backups",
        "ultimo_mensaje": "Todavía no se ejecutó ningún backup automático.",
        "ultimo_archivo_zip": "",
        "ultimo_tamano_kb": 0,
        "cantidad_backups_guardados": 0,
    }
    if rutas["estado_file"].exists():
        try:
            leido = json.loads(rutas["estado_file"].read_text(encoding="utf-8"))
            if isinstance(leido, dict):
                estado_defecto.update(leido)
        except Exception:
            pass

    # Contar archivos zip históricos
    try:
        zips = list(rutas["backups_dir"].glob("datos_backup_*.zip"))
        estado_defecto["cantidad_backups_guardados"] = len(zips)
    except Exception:
        pass

    return estado_defecto


def guardar_estado_backup(app_dir: Path, nuevos_datos: Dict[str, Any]) -> Dict[str, Any]:
    rutas = obtener_rutas_base(app_dir)
    actual = leer_estado_backup(app_dir)
    actual.update(nuevos_datos)
    try:
        rutas["estado_file"].write_text(
            json.dumps(actual, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("No se pudo guardar estado_backup.json: %s", exc)
    return actual


def marcar_cambios_pendientes(app_dir: Path, motivo: str = "Movimiento registrado") -> None:
    """
    Se llama automáticamente cuando se registra una venta, cobro, movimiento de caja,
    cambio de producto/cliente o configuración.
    """
    ahora_txt = _ahora_str()
    estado = leer_estado_backup(app_dir)
    guardar_estado_backup(
        app_dir,
        {
            "cambios_pendientes": True,
            "ultimo_cambio_local": ahora_txt,
            "ultimo_estado": "pendiente_wifi" if estado.get("ultimo_estado") != "error" else "pendiente_wifi",
            "ultimo_mensaje": f"{motivo} ({ahora_txt}). Guardado en el celular; pendiente de subir en Wi-Fi seguro.",
        },
    )


# ---------------------------------------------------------------------------
# Detección de Internet y Nombre de Red Wi-Fi (SSID)
# ---------------------------------------------------------------------------
def hay_conexion_internet(timeout: float = 2.0) -> bool:
    """Verifica rápido si hay salida a internet sin bloquear el uso offline."""
    for host, port in [("8.8.8.8", 53), ("1.1.1.1", 53), ("drive.google.com", 443)]:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def detectar_wifi_ssid() -> str:
    """
    Intenta obtener el nombre de la red Wi-Fi actual (SSID) en Android (Termux),
    Windows o Linux. Si no se puede leer (por permisos de Android o por cable),
    devuelve cadena vacía ''.
    """
    sistema = platform.system().lower()

    # 1. Windows: netsh wlan show interfaces
    if "windows" in sistema:
        try:
            salida = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                stderr=subprocess.DEVNULL,
                timeout=2.5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).decode("utf-8", errors="ignore")
            for linea in salida.splitlines():
                linea_str = linea.strip()
                if linea_str.startswith("SSID") and not linea_str.startswith("BSSID"):
                    partes = linea_str.split(":", 1)
                    if len(partes) == 2:
                        ssid = partes[1].strip()
                        if ssid:
                            return ssid
        except Exception:
            pass

    # 2. Android (Termux): termux-wifi-connectioninfo
    for cmd in [
        ["termux-wifi-connectioninfo"],
        ["iwgetid", "-r"],
    ]:
        try:
            salida = subprocess.check_output(
                cmd, stderr=subprocess.DEVNULL, timeout=2.5
            ).decode("utf-8", errors="ignore").strip()
            if cmd[0] == "termux-wifi-connectioninfo" and salida.startswith("{"):
                datos = json.loads(salida)
                ssid = str(datos.get("ssid") or "").strip().strip('"')
                if ssid and ssid.lower() != "<unknown ssid>":
                    return ssid
            elif salida:
                return salida.strip().strip('"')
        except Exception:
            continue

    return ""


def evaluar_condicion_wifi(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evalúa si están dadas las condiciones para hacer el backup automático en la nube:
    - Conectado a internet.
    - Si el usuario pidió 'wifi_especifico', compara el SSID actual con `wifi_seguro_ssid`.
    """
    online = hay_conexion_internet()
    ssid_actual = detectar_wifi_ssid()
    modo = str(config.get("modo_wifi_backup") or "cualquier_wifi").strip()
    wifi_esperado_raw = str(config.get("wifi_seguro_ssid") or "").strip()

    if not online:
        return {
            "online": False,
            "ssid_actual": ssid_actual,
            "es_wifi_seguro": False,
            "motivo": "Sin conexión a internet (Modo Offline activo).",
        }

    if modo == "wifi_especifico" and wifi_esperado_raw:
        Permitidos = [x.strip().lower() for x in wifi_esperado_raw.split(",") if x.strip()]
        if not ssid_actual:
            # En Android sin permiso de ubicación a veces no informa el SSID aunque esté en Wi-Fi
            return {
                "online": True,
                "ssid_actual": "",
                "es_wifi_seguro": False,
                "motivo": (
                    f"Hay internet, pero el dispositivo no informó el nombre del Wi-Fi para comparar con "
                    f"'{wifi_esperado_raw}'. Podés elegir 'Cualquier Wi-Fi con internet' si preferís no pedir permiso de ubicación."
                ),
            }
        coincide = ssid_actual.strip().lower() in Permitidos
        return {
            "online": True,
            "ssid_actual": ssid_actual,
            "es_wifi_seguro": coincide,
            "motivo": (
                f"Conectado al Wi-Fi seguro '{ssid_actual}'."
                if coincide
                else f"Conectado a '{ssid_actual}', esperando el Wi-Fi seguro '{wifi_esperado_raw}'."
            ),
        }

    return {
        "online": True,
        "ssid_actual": ssid_actual,
        "es_wifi_seguro": True,
        "motivo": (
            f"Conectado a internet ({'Wi-Fi ' + ssid_actual if ssid_actual else 'Red activa'})."
        ),
    }


# ---------------------------------------------------------------------------
# Empaquetado seguro de la carpeta `datos/` (SQLite Hot Backup + ZIP + Espejo)
# ---------------------------------------------------------------------------
def _copiar_sqlite_consistente(origen_db: Path, destino_db: Path) -> None:
    """Usa la API nativa de backup de SQLite para copiar sin corromper transacciones abiertas."""
    destino_db.parent.mkdir(parents=True, exist_ok=True)
    if not origen_db.exists():
        return
    src_conn = sqlite3.connect(str(origen_db), timeout=15.0)
    dst_conn = sqlite3.connect(str(destino_db), timeout=15.0)
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def ejecutar_backup_datos(
    app_dir: Path,
    config: Dict[str, Any],
    forzar_subida_nube: bool = False,
    motivo: str = "Backup automático",
) -> Dict[str, Any]:
    """
    Empaqueta toda la carpeta `datos/` (`pos_local.db`, `config_negocio.json`, `uploads/`):
    1. Crea el espejo actualizado en `datos/backups_drive/datos_espejo/` y en la carpeta
       de sincronización elegida (por ej. `/sdcard/POS_Backup_Drive` en Android o carpeta Drive).
    2. Genera `datos_backup_actual.zip` y una copia histórica con fecha y hora (conserva las últimas 10).
    3. Si hay Wi-Fi seguro (o `forzar_subida_nube=True`) y está configurado el Webhook/Script de
       Google Drive, sube el `.zip` directamente a Google Drive por HTTPS.
    """
    with _lock_backup:
        rutas = obtener_rutas_base(app_dir)
        ahora = _ahora()
        sello = ahora.strftime("%Y%m%d_%H%M%S")
        ahora_legible = fmt_fecha(ahora)

        try:
            # 1. Copia espejo consistente de pos_local.db
            espejo_dir = rutas["espejo_dir"]
            espejo_db = espejo_dir / "pos_local.db"
            _copiar_sqlite_consistente(rutas["db_file"], espejo_db)

            # Copiar config_negocio.json al espejo (sin contraseñas sensibles si se prefiere, o completo para restaurar)
            if rutas["config_file"].exists():
                shutil.copy2(rutas["config_file"], espejo_dir / "config_negocio.json")

            # Copiar imágenes subidas (uploads)
            espejo_uploads = espejo_dir / "uploads"
            espejo_uploads.mkdir(parents=True, exist_ok=True)
            for carpeta_up in (rutas["uploads_static"], rutas["uploads_datos"]):
                if carpeta_up.exists():
                    for archivo in carpeta_up.iterdir():
                        if archivo.is_file():
                            shutil.copy2(archivo, espejo_uploads / archivo.name)

            # 2. Crear archivo ZIP completo de la carpeta datos
            zip_actual = rutas["backups_dir"] / "datos_backup_actual.zip"
            zip_historico = rutas["backups_dir"] / f"datos_backup_{sello}.zip"

            info_meta = {
                "generado_en": ahora.isoformat(),
                "generado_en_ar": ahora_legible,
                "nombre_comercio": config.get("nombre_comercio", "Mi Comercio"),
                "motivo": motivo,
                "contiene": ["datos/pos_local.db", "config_negocio.json", "uploads/"],
            }

            with zipfile.ZipFile(zip_actual, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                if espejo_db.exists():
                    zf.write(espejo_db, arcname="datos/pos_local.db")
                if (espejo_dir / "config_negocio.json").exists():
                    zf.write(espejo_dir / "config_negocio.json", arcname="config_negocio.json")
                if espejo_uploads.exists():
                    for img in espejo_uploads.iterdir():
                        if img.is_file():
                            zf.write(img, arcname=f"datos/uploads/{img.name}")
                zf.writestr("info_backup.json", json.dumps(info_meta, ensure_ascii=False, indent=2))

            shutil.copy2(zip_actual, zip_historico)

            # Rotación: mantener los últimos 10 backups con fecha para cuidar el espacio del celular
            historicos = sorted(
                rutas["backups_dir"].glob("datos_backup_*.zip"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for viejo in historicos[10:]:
                try:
                    viejo.unlink()
                except Exception:
                    pass

            tamano_kb = round(zip_actual.stat().st_size / 1024.0, 1) if zip_actual.exists() else 0

            # 3. Si hay una carpeta externa configurada (ej. /sdcard/POS_Backup_Drive o Google Drive local)
            carpeta_ext_str = str(
                config.get("carpeta_sincronizacion_drive")
                or _carpeta_visible_celular_por_defecto(app_dir)
            ).strip()
            copiado_externo = False
            if carpeta_ext_str:
                try:
                    carpeta_ext = Path(carpeta_ext_str)
                    if carpeta_ext.resolve() != rutas["backups_dir"].resolve():
                        carpeta_ext.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(zip_actual, carpeta_ext / "datos_backup_actual.zip")
                        shutil.copy2(zip_actual, carpeta_ext / f"datos_backup_{sello}.zip")
                        # También dejar la subcarpeta datos/ directa para sincronizadores por carpeta
                        ext_datos = carpeta_ext / "datos"
                        ext_datos.mkdir(parents=True, exist_ok=True)
                        if espejo_db.exists():
                            shutil.copy2(espejo_db, ext_datos / "pos_local.db")
                        if (espejo_dir / "config_negocio.json").exists():
                            shutil.copy2(espejo_dir / "config_negocio.json", ext_datos / "config_negocio.json")
                        # Rotar históricos en la carpeta externa también
                        ext_hist = sorted(
                            carpeta_ext.glob("datos_backup_2*.zip"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        for v in ext_hist[10:]:
                            try:
                                v.unlink()
                            except Exception:
                                pass
                        copiado_externo = True
                except Exception as exc_ext:
                    log.warning("No se pudo copiar a la carpeta externa '%s': %s", carpeta_ext_str, exc_ext)

            # 4. Evaluar subida directa a Google Drive (si hay Wi-Fi seguro y URL de Google Apps Script)
            cond_wifi = evaluar_condicion_wifi(config)
            webhook_url = str(config.get("google_drive_webhook_url") or "").strip()
            subido_nube = False
            detalle_nube = ""

            if webhook_url and (cond_wifi["es_wifi_seguro"] or (forzar_subida_nube and cond_wifi["online"])):
                ok_subida, msg_subida = _subir_zip_a_google_drive_webhook(
                    zip_actual, webhook_url, config.get("nombre_comercio", "Mi_Comercio"), sello
                )
                subido_nube = ok_subida
                detalle_nube = msg_subida

            # 5. Actualizar estado
            if subido_nube:
                estado_final = "ok_nube"
                msg_final = f"Backup completo subido a Google Drive ({ahora_legible} · {tamano_kb} KB)."
                cambios_pend = False
                ult_nube = ahora_legible
            elif cond_wifi["es_wifi_seguro"]:
                estado_final = "ok_local_wifi"
                msg_final = (
                    f"Carpeta 'datos' respaldada en Wi-Fi ({ahora_legible} · {tamano_kb} KB) "
                    f"lista en '{carpeta_ext_str}'."
                )
                if webhook_url and detalle_nube:
                    msg_final += f" Aviso nube: {detalle_nube}"
                cambios_pend = False
                ult_nube = ahora_legible
            else:
                estado_final = "guardado_local_esperando_wifi"
                msg_final = (
                    f"Copia local de 'datos' asegurada ({ahora_legible} · {tamano_kb} KB). "
                    f"{cond_wifi['motivo']}"
                )
                cambios_pend = True
                ult_nube = leer_estado_backup(app_dir).get("ultimo_backup_nube", "")

            nuevo_estado = guardar_estado_backup(
                app_dir,
                {
                    "cambios_pendientes": cambios_pend,
                    "ultimo_backup_local": ahora_legible,
                    "ultimo_backup_nube": ult_nube,
                    "ultimo_estado": estado_final,
                    "ultimo_mensaje": msg_final,
                    "ultimo_archivo_zip": str(zip_actual),
                    "ultimo_tamano_kb": tamano_kb,
                    "carpeta_destino_activa": carpeta_ext_str if copiado_externo else str(rutas["backups_dir"]),
                },
            )
            return {
                "ok": True,
                "estado": nuevo_estado,
                "wifi": cond_wifi,
                "subido_nube": subido_nube,
            }

        except Exception as exc:
            log.exception("Error ejecutando backup de carpeta datos:")
            err_msg = f"Error al respaldar carpeta datos ({ahora_legible}): {exc}"
            nuevo_estado = guardar_estado_backup(
                app_dir,
                {
                    "ultimo_estado": "error",
                    "ultimo_mensaje": err_msg,
                },
            )
            return {
                "ok": False,
                "error": str(exc),
                "estado": nuevo_estado,
            }


def _subir_zip_a_google_drive_webhook(
    zip_path: Path, webhook_url: str, nombre_comercio: str, sello: str
) -> Tuple[bool, str]:
    """
    Envía el archivo `datos_backup_actual.zip` codificado en base64 a un Web App
    de Google Apps Script del propio usuario, que lo guarda en su Google Drive
    dentro de la carpeta `Backups_POS_Celular`.
    """
    try:
        contenido_b64 = base64.b64encode(zip_path.read_bytes()).decode("ascii")
        nombre_limpio = "".join(c if c.isalnum() else "_" for c in (nombre_comercio or "Comercio")).strip("_")
        payload = json.dumps(
            {
                "filename": f"datos_backup_{nombre_limpio}_{sello}.zip",
                "latest_filename": f"datos_backup_{nombre_limpio}_ACTUAL.zip",
                "mimeType": "application/zip",
                "fileBase64": contenido_b64,
                "comercio": nombre_comercio,
                "timestamp": sello,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "POS-Celular-Backup/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp_txt = resp.read().decode("utf-8", errors="ignore")
            try:
                resp_json = json.loads(resp_txt)
                if resp_json.get("ok"):
                    return True, resp_json.get("message", "Subido a Google Drive")
                return False, resp_json.get("error", "Respuesta sin confirmación de Google Drive")
            except Exception:
                if 200 <= resp.status < 300:
                    return True, "Subido correctamente a Google Drive"
                return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"No se pudo conectar con el puente de Google Drive: {exc}"


# ---------------------------------------------------------------------------
# Restaurar la carpeta `datos/` desde un archivo `.zip`
# ---------------------------------------------------------------------------
def restaurar_datos_desde_zip(app_dir: Path, zip_bytes: bytes) -> Dict[str, Any]:
    """
    Restaura `datos/pos_local.db`, `config_negocio.json` y `datos/uploads/`
    desde un archivo `.zip` generado por el backup automático.
    """
    with _lock_backup:
        rutas = obtener_rutas_base(app_dir)
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
        except Exception as exc:
            raise ValueError(f"El archivo seleccionado no es un ZIP válido: {exc}")

        nombres = zf.namelist()
        tiene_db = any(n.endswith("pos_local.db") for n in nombres)
        if not tiene_db:
            raise ValueError(
                "El archivo ZIP no contiene 'pos_local.db'. Asegurate de subir un backup generado por este sistema."
            )

        # Hacer un resguardo previo antes de pisar
        if rutas["db_file"].exists():
            previo = rutas["backups_dir"] / f"resguardo_antes_de_restaurar_{_ahora().strftime('%Y%m%d_%H%M%S')}.db"
            try:
                _copiar_sqlite_consistente(rutas["db_file"], previo)
            except Exception:
                pass

        archivos_restaurados: List[str] = []
        for item in zf.infolist():
            if item.is_dir():
                continue
            nombre = item.filename.replace("\\", "/")
            datos_item = zf.read(item.filename)

            if nombre.endswith("pos_local.db"):
                # Restaurar base SQLite de forma segura
                tmp_db = rutas["datos_dir"] / "pos_local_restore_tmp.db"
                tmp_db.write_bytes(datos_item)
                _copiar_sqlite_consistente(tmp_db, rutas["db_file"])
                try:
                    tmp_db.unlink()
                except Exception:
                    pass
                archivos_restaurados.append("datos/pos_local.db")
            elif nombre.endswith("config_negocio.json"):
                rutas["config_file"].write_bytes(datos_item)
                archivos_restaurados.append("config_negocio.json")
            elif "/uploads/" in nombre or nombre.startswith("uploads/"):
                nombre_img = Path(nombre).name
                if nombre_img:
                    rutas["uploads_static"].mkdir(parents=True, exist_ok=True)
                    rutas["uploads_datos"].mkdir(parents=True, exist_ok=True)
                    (rutas["uploads_static"] / nombre_img).write_bytes(datos_item)
                    (rutas["uploads_datos"] / nombre_img).write_bytes(datos_item)
                    archivos_restaurados.append(f"uploads/{nombre_img}")

        ahora_txt = _ahora_str()
        guardar_estado_backup(
            app_dir,
            {
                "cambios_pendientes": False,
                "ultimo_estado": "restaurado",
                "ultimo_mensaje": f"Carpeta 'datos' restaurada correctamente desde ZIP ({ahora_txt}).",
            },
        )
        return {
            "ok": True,
            "archivos_restaurados": archivos_restaurados,
            "restaurado_en": ahora_txt,
        }


# ---------------------------------------------------------------------------
# Vigilante en segundo plano (Auto-Backup al detectar Wi-Fi seguro)
# ---------------------------------------------------------------------------
def iniciar_vigilante_backup(app_dir: Path, cargar_config_fn) -> None:
    """
    Inicia un hilo daemon que revisa periódicamente si hay cambios pendientes
    y si el celular está conectado al Wi-Fi seguro para respaldar sola la carpeta `datos/`.
    """
    global _vigilante_iniciado
    if _vigilante_iniciado:
        return
    _vigilante_iniciado = True

    def _ciclo():
        # Esperar unos segundos al arranque
        time.sleep(8)
        while True:
            try:
                cfg = cargar_config_fn()
                activo = cfg.get("auto_backup_activo", True)
                if activo:
                    estado = leer_estado_backup(app_dir)
                    hay_pendientes = bool(estado.get("cambios_pendientes", False))
                    sin_backups_aun = estado.get("ultimo_estado") == "sin_backups"

                    if hay_pendientes or sin_backups_aun:
                        cond = evaluar_condicion_wifi(cfg)
                        if cond["es_wifi_seguro"]:
                            log.info("Wi-Fi seguro detectado (%s). Ejecutando auto-backup de 'datos/'...", cond["motivo"])
                            ejecutar_backup_datos(
                                app_dir,
                                cfg,
                                forzar_subida_nube=False,
                                motivo="Auto-backup en Wi-Fi seguro",
                            )
                        elif sin_backups_aun:
                            # Hacer al menos el primer resguardo local aunque esté offline
                            ejecutar_backup_datos(
                                app_dir,
                                cfg,
                                forzar_subida_nube=False,
                                motivo="Primer resguardo automático local",
                            )
            except Exception as exc:
                log.debug("Ciclo vigilante backup: %s", exc)
            time.sleep(35)

    hilo = threading.Thread(target=_ciclo, name="vigilante-backup-drive", daemon=True)
    hilo.start()
