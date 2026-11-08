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
def importar_snapshot_json_en_sqlite(
    app_dir: Path,
    backup_data: Dict[str, Any],
    forzar_modo_b: bool = False,
) -> Dict[str, Any]:
    """
    Importa un archivo de respaldo JSON (generado en Modo A Celular o desde Exportar JSON)
    directamente adentro de la base de datos SQLite de la PC (`datos/pos_local.db`),
    copiando configuración, logo, usuarios/cajeros con sus claves, categorías, productos,
    promos, clientes, cuentas corrientes, caja e historial de ventas.
    """
    from local_db import LocalDBClient, hash_pin

    if not isinstance(backup_data, dict):
        raise ValueError("El archivo de respaldo no contiene un objeto JSON válido.")

    tablas = backup_data.get("tablas") or backup_data.get("datos") or backup_data
    if not isinstance(tablas, dict):
        raise ValueError("No se encontraron tablas de datos en el archivo seleccionado.")

    with _lock_backup:
        rutas = obtener_rutas_base(app_dir)
        _ = LocalDBClient(rutas["db_file"])

        if rutas["db_file"].exists():
            previo = rutas["backups_dir"] / f"resguardo_antes_de_importar_{_ahora().strftime('%Y%m%d_%H%M%S')}.db"
            try:
                _copiar_sqlite_consistente(rutas["db_file"], previo)
            except Exception:
                pass

        conn = sqlite3.connect(str(rutas["db_file"]), timeout=20.0)
        conn.row_factory = sqlite3.Row
        resumen: Dict[str, int] = {}
        try:
            cur = conn.cursor()
            comercio_id = 1

            # 1. Configuración del negocio
            cfg_in = backup_data.get("config") or tablas.get("config") or {}
            if isinstance(cfg_in, dict) and cfg_in:
                nombre_com = str(cfg_in.get("nombre_comercio") or "Mi Comercio").strip() or "Mi Comercio"
                cur.execute("UPDATE comercios SET nombre_negocio = ? WHERE id = ?", (nombre_com, comercio_id))
                modo_op = "servidor_central" if forzar_modo_b else str(cfg_in.get("modo_operacion") or "servidor_central")
                cur.execute(
                    """
                    UPDATE configuracion_negocio SET
                        nombre_comercio = ?,
                        telefono = ?,
                        direccion = ?,
                        moneda = ?,
                        color_primario = ?,
                        logo_imagen = ?,
                        qr_imagen = ?,
                        titular_cuenta = ?,
                        banco = ?,
                        alias = ?,
                        cbu = ?,
                        pesos_por_punto = ?,
                        modo_bloqueo = ?,
                        tiempo_inactividad_min = ?,
                        modo_operacion = ?,
                        wizard_completado = 1,
                        rubro = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE comercio_id = ?
                    """,
                    (
                        nombre_com,
                        str(cfg_in.get("telefono") or ""),
                        str(cfg_in.get("direccion") or ""),
                        str(cfg_in.get("moneda") or "$"),
                        str(cfg_in.get("color_primario") or "#2563EB"),
                        str(cfg_in.get("logo_imagen") or ""),
                        str(cfg_in.get("qr_imagen") or ""),
                        str(cfg_in.get("titular_cuenta") or ""),
                        str(cfg_in.get("banco") or ""),
                        str(cfg_in.get("alias") or ""),
                        str(cfg_in.get("cbu") or ""),
                        float(cfg_in.get("pesos_por_punto") or 100.0),
                        str(cfg_in.get("modo_bloqueo") or "nunca"),
                        int(cfg_in.get("tiempo_inactividad_min") or 15),
                        modo_op,
                        str(cfg_in.get("rubro") or "comercio_general"),
                        comercio_id,
                    ),
                )
                cfg_guardar = dict(cfg_in)
                cfg_guardar["modo_operacion"] = modo_op
                cfg_guardar["wizard_completado"] = 1
                try:
                    rutas["config_file"].write_text(
                        json.dumps(cfg_guardar, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass
            elif forzar_modo_b:
                cur.execute(
                    "UPDATE configuracion_negocio SET modo_operacion = 'servidor_central', wizard_completado = 1 WHERE comercio_id = ?",
                    (comercio_id,),
                )

            # 2. Usuarios y Cajeros (con sus PINs convertidos a hash seguro PBKDF2)
            usuarios_in = backup_data.get("usuarios") or tablas.get("usuarios") or []
            if isinstance(usuarios_in, list) and len(usuarios_in) > 0:
                cur.execute("DELETE FROM usuarios WHERE comercio_id = ?", (comercio_id,))
                usr_count = 0
                tiene_admin = False
                for u in usuarios_in:
                    if not isinstance(u, dict):
                        continue
                    nom_u = str(u.get("nombre") or "").strip()
                    if not nom_u:
                        continue
                    rol_u = "admin" if str(u.get("rol") or "").lower() == "admin" else "cajero"
                    if rol_u == "admin":
                        tiene_admin = True
                    pin_plano = str(u.get("pin") or "").strip()
                    pin_h = str(u.get("pin_hash") or "").strip()
                    if pin_plano and len(pin_plano) == 4 and pin_plano.isdigit():
                        hash_final = hash_pin(pin_plano)
                    elif pin_h.startswith("pbkdf2:"):
                        hash_final = pin_h
                    else:
                        hash_final = hash_pin("1234" if rol_u == "admin" else "0000")
                    act_u = 1 if u.get("activo", True) else 0
                    uid = int(u["id"]) if "id" in u and str(u["id"]).isdigit() else None
                    if uid is not None:
                        cur.execute(
                            """
                            INSERT OR REPLACE INTO usuarios (id, comercio_id, nombre, pin_hash, rol, activo, intentos_fallidos)
                            VALUES (?, ?, ?, ?, ?, ?, 0)
                            """,
                            (uid, comercio_id, nom_u, hash_final, rol_u, act_u),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT OR REPLACE INTO usuarios (comercio_id, nombre, pin_hash, rol, activo, intentos_fallidos)
                            VALUES (?, ?, ?, ?, ?, 0)
                            """,
                            (comercio_id, nom_u, hash_final, rol_u, act_u),
                        )
                    usr_count += 1
                if not tiene_admin:
                    cur.execute(
                        "INSERT INTO usuarios (comercio_id, nombre, pin_hash, rol, activo) VALUES (?, 'Dueño', ?, 'admin', 1)",
                        (comercio_id, hash_pin("1234")),
                    )
                    usr_count += 1
                resumen["usuarios"] = usr_count

            # 3. Categorías
            cats_in = tablas.get("categorias")
            if isinstance(cats_in, list) and len(cats_in) > 0:
                cur.execute("DELETE FROM categorias WHERE comercio_id = ?", (comercio_id,))
                c_count = 0
                for c in cats_in:
                    if not isinstance(c, dict) or not c.get("nombre"):
                        continue
                    cid = int(c["id"]) if "id" in c and str(c["id"]).isdigit() else None
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO categorias (id, comercio_id, nombre, activo, fecha_baja, descripcion, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            comercio_id,
                            str(c["nombre"]).strip(),
                            1 if c.get("activo", True) else 0,
                            c.get("fecha_baja"),
                            str(c.get("descripcion") or ""),
                            str(c.get("created_at") or _ahora().isoformat()),
                        ),
                    )
                    c_count += 1
                resumen["categorias"] = c_count

            # 4. Productos
            prods_in = tablas.get("productos")
            if isinstance(prods_in, list) and len(prods_in) > 0:
                cur.execute("DELETE FROM productos WHERE comercio_id = ?", (comercio_id,))
                p_count = 0
                for p in prods_in:
                    if not isinstance(p, dict) or not p.get("nombre"):
                        continue
                    pid = int(p["id"]) if "id" in p and str(p["id"]).isdigit() else None
                    codigo = str(p.get("codigo") or f"Prod-{pid or (p_count + 1):03d}").strip()
                    p_venta = float(p.get("precio_venta") if p.get("precio_venta") is not None else (p.get("precio") or 0))
                    stk_act = float(p.get("stock_actual") if p.get("stock_actual") is not None else (p.get("stock") or 0))
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO productos (
                            id, comercio_id, codigo, nombre, categoria_id,
                            precio_costo, margen_ganancia, precio_venta, usa_precio_manual,
                            stock_actual, stock_minimo, imagen_url, activo, fecha_baja,
                            puntos_canje, unidad_medida, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pid,
                            comercio_id,
                            codigo,
                            str(p["nombre"]).strip(),
                            p.get("categoria_id"),
                            float(p.get("precio_costo") or 0),
                            float(p.get("margen_ganancia") or 0),
                            p_venta,
                            1 if p.get("usa_precio_manual", True) else 0,
                            stk_act,
                            float(p.get("stock_minimo") or 0),
                            str(p.get("imagen_url") or p.get("imagen") or ""),
                            1 if p.get("activo", True) else 0,
                            p.get("fecha_baja"),
                            float(p.get("puntos_canje") or 0),
                            str(p.get("unidad_medida") or "unid"),
                            str(p.get("created_at") or _ahora().isoformat()),
                        ),
                    )
                    p_count += 1
                resumen["productos"] = p_count

            # 5. Promos y Combos
            promos_in = tablas.get("promos") if "promos" in tablas else backup_data.get("promos")
            if isinstance(promos_in, list) and len(promos_in) > 0:
                cur.execute("DELETE FROM promos WHERE comercio_id = ?", (comercio_id,))
                pr_count = 0
                for pr in promos_in:
                    if not isinstance(pr, dict) or not pr.get("nombre"):
                        continue
                    pr_id = int(pr["id"]) if "id" in pr and str(pr["id"]).isdigit() else None
                    p_venta = float(pr.get("precio_venta") if pr.get("precio_venta") is not None else (pr.get("precio") or 0))
                    if isinstance(pr.get("fijos_json"), str):
                        fijos_j = pr["fijos_json"]
                    else:
                        fijos_j = json.dumps(pr.get("fijos") or [], ensure_ascii=False)
                    if isinstance(pr.get("elecciones_json"), str):
                        elec_j = pr["elecciones_json"]
                    else:
                        elec_j = json.dumps(pr.get("elecciones") or [], ensure_ascii=False)
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO promos (
                            id, comercio_id, nombre, categoria, descripcion,
                            precio_venta, fijos_json, elecciones_json, activo, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pr_id,
                            comercio_id,
                            str(pr["nombre"]).strip(),
                            str(pr.get("categoria") or "General"),
                            str(pr.get("descripcion") or ""),
                            p_venta,
                            fijos_j,
                            elec_j,
                            1 if pr.get("activo", True) else 0,
                            str(pr.get("created_at") or _ahora().isoformat()),
                        ),
                    )
                    pr_count += 1
                resumen["promos"] = pr_count

            # 6. Clientes
            clientes_in = tablas.get("clientes")
            if isinstance(clientes_in, list):
                cur.execute("DELETE FROM clientes WHERE comercio_id = ?", (comercio_id,))
                cli_count = 0
                for cl in clientes_in:
                    if not isinstance(cl, dict) or not cl.get("nombre"):
                        continue
                    cid = int(cl["id"]) if "id" in cl and str(cl["id"]).isdigit() else None
                    if cid == 0:
                        continue
                    saldo = float(cl.get("saldo_deudor") if cl.get("saldo_deudor") is not None else (cl.get("saldo") or 0))
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO clientes (
                            id, comercio_id, nombre, telefono, email,
                            limite_credito, saldo_deudor, puntos, activo, fecha_baja, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            comercio_id,
                            str(cl["nombre"]).strip(),
                            str(cl.get("telefono") or ""),
                            str(cl.get("email") or ""),
                            float(cl.get("limite_credito") or 0),
                            saldo,
                            float(cl.get("puntos") or 0),
                            1 if cl.get("activo", True) else 0,
                            cl.get("fecha_baja"),
                            str(cl.get("created_at") or _ahora().isoformat()),
                        ),
                    )
                    cli_count += 1
                resumen["clientes"] = cli_count

            # 7. Ventas y Detalle de Ventas
            ventas_in = tablas.get("ventas")
            if isinstance(ventas_in, list):
                cur.execute("DELETE FROM detalle_ventas")
                cur.execute("DELETE FROM ventas WHERE comercio_id = ?", (comercio_id,))
                v_count = 0
                for v in ventas_in:
                    if not isinstance(v, dict):
                        continue
                    vid = int(v["id"]) if "id" in v and str(v["id"]).isdigit() else None
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO ventas (
                            id, comercio_id, cliente_id, total_venta, forma_pago,
                            fecha_hora, puntos_ganados, archivado_en, archivado_por
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            vid,
                            comercio_id,
                            v.get("cliente_id"),
                            float(v.get("total_venta") or 0),
                            str(v.get("forma_pago") or "Efectivo"),
                            str(v.get("fecha_hora") or _ahora().isoformat()),
                            float(v.get("puntos_ganados") or 0),
                            v.get("archivado_en"),
                            v.get("archivado_por"),
                        ),
                    )
                    v_count += 1
                resumen["ventas"] = v_count

            det_in = tablas.get("detalle_ventas")
            if isinstance(det_in, list):
                d_count = 0
                for d in det_in:
                    if not isinstance(d, dict) or not d.get("venta_id"):
                        continue
                    did = int(d["id"]) if "id" in d and str(d["id"]).isdigit() else None
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO detalle_ventas (
                            id, venta_id, producto_id, cantidad, precio_unitario, costo_unitario
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            did,
                            int(d["venta_id"]),
                            d.get("producto_id"),
                            float(d.get("cantidad") or 0),
                            float(d.get("precio_unitario") or 0),
                            float(d.get("costo_unitario") or 0),
                        ),
                    )
                    d_count += 1
                resumen["detalle_ventas"] = d_count

            # 8. Movimientos de Caja, Cuenta Corriente y Cierres de Caja
            mov_caja_in = tablas.get("movimientos_caja")
            if isinstance(mov_caja_in, list):
                cur.execute("DELETE FROM movimientos_caja WHERE comercio_id = ?", (comercio_id,))
                mc_count = 0
                for m in mov_caja_in:
                    if not isinstance(m, dict):
                        continue
                    mid = int(m["id"]) if "id" in m and str(m["id"]).isdigit() else None
                    tipo_m = "ingreso_extra" if m.get("tipo") == "ingreso_extra" else "egreso"
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO movimientos_caja (
                            id, comercio_id, tipo, monto, concepto, usuario_nombre, fecha, archivado_en, archivado_por
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mid,
                            comercio_id,
                            tipo_m,
                            float(m.get("monto") or 0),
                            str(m.get("concepto") or ""),
                            str(m.get("usuario_nombre") or "Dueño"),
                            str(m.get("fecha") or _ahora().isoformat()),
                            m.get("archivado_en"),
                            m.get("archivado_por"),
                        ),
                    )
                    mc_count += 1
                resumen["movimientos_caja"] = mc_count

            mov_cta_in = tablas.get("movimientos_cuenta")
            if isinstance(mov_cta_in, list):
                cur.execute("DELETE FROM movimientos_cuenta WHERE comercio_id = ?", (comercio_id,))
                mct_count = 0
                for m in mov_cta_in:
                    if not isinstance(m, dict) or not m.get("cliente_id"):
                        continue
                    mid = int(m["id"]) if "id" in m and str(m["id"]).isdigit() else None
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO movimientos_cuenta (
                            id, comercio_id, cliente_id, tipo, monto, descripcion, ticket_id, fecha_hora, archivado_en, archivado_por
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mid,
                            comercio_id,
                            int(m["cliente_id"]),
                            str(m.get("tipo") or "cargo"),
                            float(m.get("monto") or 0),
                            str(m.get("descripcion") or ""),
                            m.get("ticket_id"),
                            str(m.get("fecha_hora") or _ahora().isoformat()),
                            m.get("archivado_en"),
                            m.get("archivado_por"),
                        ),
                    )
                    mct_count += 1
                resumen["movimientos_cuenta"] = mct_count

            cierres_in = tablas.get("cierres_caja")
            if isinstance(cierres_in, list):
                cur.execute("DELETE FROM cierres_caja WHERE comercio_id = ?", (comercio_id,))
                cc_count = 0
                for c in cierres_in:
                    if not isinstance(c, dict):
                        continue
                    cid = int(c["id"]) if "id" in c and str(c["id"]).isdigit() else None
                    est_c = "abierta" if c.get("estado") == "abierta" else "cerrada"
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO cierres_caja (
                            id, comercio_id, estado, monto_inicial, fecha_apertura,
                            fecha_cierre, total_esperado, monto_real, diferencia, archivado_en, archivado_por
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            comercio_id,
                            est_c,
                            float(c.get("monto_inicial") or 0),
                            str(c.get("fecha_apertura") or _ahora().isoformat()),
                            c.get("fecha_cierre"),
                            c.get("total_esperado"),
                            c.get("monto_real"),
                            c.get("diferencia"),
                            c.get("archivado_en"),
                            c.get("archivado_por"),
                        ),
                    )
                    cc_count += 1
                resumen["cierres_caja"] = cc_count

            conn.commit()
        finally:
            conn.close()

        ahora_txt = _ahora_str()
        guardar_estado_backup(
            app_dir,
            {
                "cambios_pendientes": False,
                "ultimo_estado": "migrado_desde_modo_a",
                "ultimo_mensaje": f"Datos importados íntegramente desde Modo A / JSON ({ahora_txt}).",
            },
        )
        return {
            "ok": True,
            "modo_origen": "json_modo_a",
            "resumen": resumen,
            "archivos_restaurados": ["datos/pos_local.db", "config_negocio.json"],
            "restaurado_en": ahora_txt,
        }


def restaurar_datos_desde_zip(app_dir: Path, zip_bytes: bytes, forzar_modo_b: bool = False) -> Dict[str, Any]:
    """
    Restaura `datos/pos_local.db`, `config_negocio.json` y `datos/uploads/`
    desde un archivo `.zip` generado en PC (Modo B) O desde un archivo `.json` / `.zip`
    generado en el celular (Modo A).
    """
    texto_inicio = zip_bytes[:64].lstrip()
    if texto_inicio.startswith(b"{") or texto_inicio.startswith(b"\xef\xbb\xbf{"):
        try:
            json_data = json.loads(zip_bytes.decode("utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"El archivo JSON de respaldo no es válido: {exc}")
        return importar_snapshot_json_en_sqlite(app_dir, json_data, forzar_modo_b=forzar_modo_b)

    json_embebido = None
    with _lock_backup:
        rutas = obtener_rutas_base(app_dir)
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
        except Exception as exc:
            raise ValueError(f"El archivo seleccionado no es un ZIP ni un JSON válido: {exc}")

        nombres = zf.namelist()
        tiene_db = any(n.endswith("pos_local.db") for n in nombres)
        if not tiene_db:
            archivos_json = [
                n for n in nombres
                if n.endswith(".json") and not n.endswith("info_backup.json") and not n.endswith("config_negocio.json")
            ]
            if archivos_json:
                raw_json = zf.read(archivos_json[0])
                json_embebido = json.loads(raw_json.decode("utf-8-sig"))
                zf.close()
            else:
                zf.close()
                raise ValueError(
                    "El archivo no contiene 'pos_local.db' ni un respaldo JSON de celular válido."
                )
        else:
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
            zf.close()

            if forzar_modo_b and rutas["db_file"].exists():
                try:
                    conn = sqlite3.connect(str(rutas["db_file"]), timeout=15.0)
                    with conn:
                        conn.execute(
                            "UPDATE configuracion_negocio SET modo_operacion = 'servidor_central', wizard_completado = 1 WHERE comercio_id = 1"
                        )
                    conn.close()
                except Exception:
                    pass

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

    if json_embebido is not None:
        return importar_snapshot_json_en_sqlite(app_dir, json_embebido, forzar_modo_b=forzar_modo_b)
    raise ValueError("No se pudo procesar el archivo de respaldo.")


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
