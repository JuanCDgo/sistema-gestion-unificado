"""
Servidor FastAPI del POS "Marca Blanca".

Sirve la interfaz (Jinja2) y una API conectada a Supabase (PostgreSQL).
El formato JSON de cada endpoint es el mismo que tenía la versión con datos
simulados: la interfaz no necesita saber que ahora hay una base real detrás.

Variables de entorno (ver .env.example):
    SUPABASE_URL, SUPABASE_KEY, COMERCIO_ID

Ejecutar:  uvicorn main:app --reload
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Determinar rutas según si corre como script o ejecutable empaquetado (PyInstaller)
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = APP_DIR

BASE_DIR = APP_DIR
load_dotenv(Path(os.getenv("POS_ENV_FILE") or BASE_DIR / ".env"), override=True)

import io
import json
import logging
import re
import secrets
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from formato import fmt_cantidad, fmt_fecha, fmt_moneda, fmt_puntos
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from supabase import Client, create_client

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

TZ_AR = timezone(timedelta(hours=-3))


def _ahora_local() -> datetime:
    """Fecha y hora actual en huso horario de Argentina (-03:00)."""
    return datetime.now(TZ_AR)


log = logging.getLogger("pos")

app = FastAPI(title="POS Marca Blanca")

ORIGENES_PERMITIDOS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_PERMITIDOS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Accept"],
)


@app.middleware("http")
async def agregar_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Carpetas de datos, uploads y montaje de estáticos / plantillas
(APP_DIR / "datos").mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = APP_DIR / "datos" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# /static/uploads se sirve desde la carpeta de datos del usuario
app.mount("/static/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
# El resto de recursos estáticos (vendor, js, css) se sirven desde el paquete
static_dir = BUNDLE_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=str(BUNDLE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Rate Limiting en memoria por IP y bucket
# ---------------------------------------------------------------------------
_RATE_BUCKETS: dict[tuple[str, str], deque] = defaultdict(deque)


def limitar_intentos(request: Request, bucket: str, max_intentos: int, ventana_seg: int) -> None:
    ip = request.client.host if request.client else "unknown"
    ahora = time.monotonic()
    cola = _RATE_BUCKETS[(ip, bucket)]
    while cola and ahora - cola[0] > ventana_seg:
        cola.popleft()
    if len(cola) >= max_intentos:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos. Esperá {ventana_seg} segundos antes de volver a intentar.",
        )
    cola.append(ahora)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
# Comercio (tenant) con el que opera este servidor; todas las consultas se filtran por él.
COMERCIO_ID = int(os.getenv("COMERCIO_ID", "1"))


class ConfigDBError(Exception):
    """Faltan credenciales de Supabase en el entorno."""


@lru_cache(maxsize=1)
def get_db():
    """Cliente único de base de datos.
    Si POS_MODO_OFFLINE está activo, o si no se configuraron SUPABASE_URL y SUPABASE_KEY,
    utiliza el motor SQLite local (LocalDBClient) para funcionamiento 100% offline.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_KEY") or "").strip()
    # Si se pide offline, o si no hay credenciales de Supabase en el .env, usa SQLite local
    modo_offline = os.getenv("POS_MODO_OFFLINE", "").strip().lower() in ("1", "true", "si", "yes")

    if modo_offline or not url or not key:
        from local_db import LocalDBClient
        db_path = os.getenv("POS_DB_FILE") or (BASE_DIR / "datos" / "pos_local.db")
        return LocalDBClient(db_path)

    return create_client(url, key)


@app.exception_handler(ConfigDBError)
async def _db_sin_configurar(request: Request, exc: ConfigDBError):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(APIError)
async def _error_db(request: Request, exc: APIError):
    # El detalle técnico queda en el log; al cliente no se le filtra el esquema de la base.
    log.error("Error de Supabase: %s", getattr(exc, "message", exc))
    # 42703 columna, 42P01 tabla, 42883 función (Postgres) y PGRST202 función (PostgREST) inexistentes:
    # la base no tiene los scripts nuevos.
    if getattr(exc, "code", None) in ("42703", "42P01", "42883", "PGRST202"):
        return JSONResponse({"detail": "La base no está actualizada: corré los scripts de sql/ (ver sql/README.md)"},
                            status_code=503)
    return JSONResponse({"detail": "Error al consultar la base de datos"}, status_code=502)


def _num(valor):
    """numeric de Postgres -> número JSON (entero si no tiene decimales)."""
    if valor is None:
        return 0
    f = float(valor)
    return int(f) if f.is_integer() else f


# ---------------------------------------------------------------------------
# Autenticación por cookie de sesión y configuración segura
# ---------------------------------------------------------------------------
COOKIE_SESION = "session_token"
SESION_TTL_SEG = 60 * 60 * 12  # 12 horas
MAX_SESIONES_ACTIVAS = 500
# token -> {"usuario_id", "usuario", "rol", "debe_cambiar_pin", "expira"}
SESIONES_INFO: dict[str, dict] = {}

CONFIG_DEFAULT = {
    "nombre_comercio": "Mi Comercio",
    "color_primario": "#2563EB",
    "moneda": "$",
    "telefono": "",
    "direccion": "",
    "logo_imagen": "",
    "qr_imagen": "",
    "titular_cuenta": "",
    "banco": "",
    "alias": "",
    "cbu": "",
    "pesos_por_punto": 100.0,
    "smtp_servidor": "",
    "smtp_puerto": 587,
    "smtp_usuario": "",
    "smtp_password": "",
    "email_destinatario": "",
    "modo_bloqueo": "nunca",
    "tiempo_inactividad_min": 15,
    "auto_backup_activo": True,
    "modo_wifi_backup": "cualquier_wifi",
    "wifi_seguro_ssid": "",
    "carpeta_sincronizacion_drive": "",
    "google_drive_webhook_url": "https://script.google.com/macros/s/AKfycbzI2Bn0rj_MpW5lFhNJ2um2oEiwtSR4YRbfzedtoYL150rtvcKJ4go_bmLthl24xzMF2Q/exec",
    "modo_operacion": "autonomo_offline",
    "wizard_completado": 0,
    "rubro": "comercio_general",
}
# clave_admin / clave_admin_hash: claves heredadas del login viejo, nunca se mandan al navegador.
CAMPOS_SENSIBLES_CONFIG = {"smtp_password", "clave_admin", "clave_admin_hash"}
COLUMNAS_CONFIG_DB = set(CONFIG_DEFAULT.keys())


def _validar_imagen_url(url: Optional[str]) -> str:
    """Valida que el enlace de imagen sea una URL HTTP/HTTPS, ruta local (/static/...) o vacío."""
    limpia = (url or "").strip()
    if not limpia:
        return ""
    if len(limpia) > 2048:
        raise HTTPException(400, "La URL de la imagen supera el largo máximo permitido (2048 caracteres).")
    if limpia.startswith(("/static/", "static/")) or limpia.startswith("data:image/") or re.match(r"^https?://\S+$", limpia, flags=re.IGNORECASE):
        return limpia
    raise HTTPException(400, "Ingresá un enlace válido (https://, http:// o ruta interna /static/...)")


def _obtener_ip_red_local() -> str:
    """Detecta la IP local de la maquina en la red Wi-Fi o cableada."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _generar_qr_svg(url: str) -> str:
    """Genera el codigo QR en formato SVG limpio."""
    try:
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(url, image_factory=factory)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception as e:
        log.warning("No se pudo generar QR SVG: %s", e)
        return ""


def cargar_config() -> dict:
    """Lee la configuración completa del negocio desde Supabase (tabla configuracion_negocio)."""
    try:
        db = get_db()
        filas = (
            db.table("configuracion_negocio")
            .select("*")
            .eq("comercio_id", COMERCIO_ID)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not filas:
            inicial = {"comercio_id": COMERCIO_ID, **CONFIG_DEFAULT}
            res = db.table("configuracion_negocio").upsert(inicial, on_conflict="comercio_id").execute()
            datos = res.data[0] if res.data else inicial
        else:
            datos = filas[0]
        salida = {**CONFIG_DEFAULT, **{k: v for k, v in datos.items() if k in COLUMNAS_CONFIG_DB and v is not None}}
        salida["pesos_por_punto"] = _num(salida.get("pesos_por_punto", 100))
        return salida
    except Exception as exc:
        log.error("Error leyendo configuracion_negocio en Supabase: %s", getattr(exc, "message", exc))
        return dict(CONFIG_DEFAULT)


def guardar_config(config: dict) -> dict:
    """Persiste la configuración del comercio en Supabase (upsert por comercio_id)."""
    db = get_db()
    payload = {
        "comercio_id": COMERCIO_ID,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for k in COLUMNAS_CONFIG_DB:
        if k in config and config[k] is not None:
            payload[k] = config[k]

    res = db.table("configuracion_negocio").upsert(payload, on_conflict="comercio_id").execute()
    if payload.get("nombre_comercio"):
        try:
            db.table("comercios").update({"nombre_negocio": payload["nombre_comercio"]}).eq("id", COMERCIO_ID).execute()
        except Exception:
            pass
    datos = res.data[0] if res.data else payload
    resultado = {**CONFIG_DEFAULT, **{k: v for k, v in datos.items() if k in COLUMNAS_CONFIG_DB and v is not None}}
    try:
        (APP_DIR / "config_negocio.json").write_text(
            json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return resultado


try:
    from backup_drive import iniciar_vigilante_backup
    iniciar_vigilante_backup(APP_DIR, cargar_config)
except Exception as _exc_vig:
    log.warning("No se pudo iniciar vigilante de backup: %s", _exc_vig)


def config_publica(incluir_mascara_smtp: bool = False) -> dict:
    """Devuelve la configuración filtrando credenciales sensibles (smtp_password)."""
    cfg = cargar_config()
    limpia = {k: v for k, v in cfg.items() if k not in CAMPOS_SENSIBLES_CONFIG}
    if incluir_mascara_smtp:
        limpia["smtp_password_configurado"] = bool((cfg.get("smtp_password") or "").strip())
        limpia["smtp_password"] = ""
    return limpia


class NoAutenticado(Exception):
    """Se lanza en rutas HTML sin sesión (o con PIN pendiente); un handler redirige a /login (o al destino del mensaje)."""


@app.exception_handler(NoAutenticado)
async def _redirigir_a_login(request: Request, exc: NoAutenticado):
    return RedirectResponse(str(exc) or "/login", status_code=303)


def _sesion_valida(request: Request) -> bool:
    token = request.cookies.get(COOKIE_SESION)
    if not token:
        return False
    info = SESIONES_INFO.get(token)
    if not info:
        return False
    expira = info.get("expira")
    if expira is not None and time.time() > expira:
        SESIONES_INFO.pop(token, None)
        return False
    return True


def _info_sesion(request: Request) -> dict:
    return SESIONES_INFO.get(request.cookies.get(COOKIE_SESION), {})


# Con el PIN de ejemplo solo se puede cambiar el PIN, ver el estado de la barra y salir.
_RUTAS_PIN_PENDIENTE = ("/api/config/cambiar-clave", "/api/logout", "/api/estado-barra")


def requiere_sesion_html(request: Request) -> None:
    # En la Versión Celular PWA Autónoma, la sesión y la base de datos viven dentro
    # del navegador del teléfono (motor_offline_pwa.js) y el Service Worker necesita
    # precachear todas las pantallas HTML en el primer ingreso.
    return None


def requiere_sesion_api(request: Request) -> None:
    if not _sesion_valida(request):
        raise HTTPException(401, "Sesión no iniciada o expirada")
    if _info_sesion(request).get("debe_cambiar_pin") and request.url.path not in _RUTAS_PIN_PENDIENTE:
        raise HTTPException(403, "Tenés que cambiar el PIN de ejemplo antes de seguir")


def requiere_admin_api(request: Request) -> None:
    if _info_sesion(request).get("rol") != "admin":
        raise HTTPException(403, "Solo el Dueño puede hacer esto")


def _registrar_auditoria(request: Optional[Request], tipo_accion: str, detalles: dict) -> None:
    """Inserta un registro en auditoria_movimientos tomando el usuario de la sesión activa.

    Es fail-safe: si la tabla aún no fue creada o hay un error transitorio, lo deja en el log
    sin abortar la operación principal que ya se ejecutó.
    """
    info = _info_sesion(request) if request is not None else {}
    uid = info.get("usuario_id")
    nombre = info.get("usuario") or "Sistema"
    try:
        get_db().table("auditoria_movimientos").insert({
            "comercio_id": COMERCIO_ID,
            "usuario_id": uid if isinstance(uid, int) else None,
            "usuario_nombre": nombre,
            "tipo_accion": tipo_accion,
            "detalles": detalles,
        }).execute()
    except Exception as exc:
        log.error("No se pudo registrar auditoría (%s): %s", tipo_accion, getattr(exc, "message", exc))


def _pin_valido(request: Request, pin: str) -> bool:
    """PIN del usuario de la sesión, y que sea admin (verificar_login cuenta los intentos fallidos)."""
    uid = _info_sesion(request).get("usuario_id")
    if uid is None or not pin:
        return False
    ver = get_db().rpc("verificar_login", {"p_usuario_id": uid, "p_pin": pin.strip()}).execute().data or {}
    return bool(ver.get("ok")) and (ver.get("usuario") or {}).get("rol") == "admin"


def _msg_bloqueo(ver: dict) -> str:
    return f"Usuario bloqueado por {ver.get('minutos_bloqueo')} minutos por demasiados intentos."


class LoginIn(BaseModel):
    usuario_id: int
    pin: str = Field(pattern=r"^\d{4}$")


@app.get("/api/usuarios-login")
def usuarios_login():
    return get_db().rpc("listar_usuarios_login", {"p_comercio_id": COMERCIO_ID}).execute().data or []


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _sesion_valida(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"config": config_publica()})


@app.post("/api/login")
def login(data: LoginIn, request: Request, response: Response):
    limitar_intentos(request, bucket="auth_login", max_intentos=10, ventana_seg=60)
    ver = get_db().rpc("verificar_login", {"p_usuario_id": data.usuario_id, "p_pin": data.pin}).execute().data or {}
    usuario = ver.get("usuario") or {}
    if ver.get("ok") and usuario.get("comercio_id") != COMERCIO_ID:
        ver = {"ok": False, "motivo": "usuario_invalido"}
    if not ver.get("ok"):
        motivo = ver.get("motivo")
        if motivo == "pin_incorrecto":
            raise HTTPException(401, f"PIN incorrecto. Te quedan {ver.get('intentos_restantes')} intentos.")
        if motivo == "bloqueado":
            raise HTTPException(401, _msg_bloqueo(ver))
        raise HTTPException(401, "Usuario no válido.")

    ahora = time.time()
    if len(SESIONES_INFO) >= MAX_SESIONES_ACTIVAS:
        for k, v in list(SESIONES_INFO.items()):
            if ahora > v.get("expira", 0):
                SESIONES_INFO.pop(k, None)

    token = secrets.token_urlsafe(32)
    SESIONES_INFO[token] = {
        "usuario_id": usuario["id"],
        "usuario": usuario["nombre"],
        "rol": usuario["rol"],
        "debe_cambiar_pin": bool(usuario.get("debe_cambiar_pin")),
        "expira": ahora + SESION_TTL_SEG,
    }
    es_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE_SESION,
        token,
        httponly=True,
        samesite="lax",
        secure=es_https,
        max_age=SESION_TTL_SEG,
    )
    return {"ok": True, "usuario": usuario["nombre"], "rol": usuario["rol"],
            "debe_cambiar_pin": SESIONES_INFO[token]["debe_cambiar_pin"]}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    tok = request.cookies.get(COOKIE_SESION)
    SESIONES_INFO.pop(tok, None)
    response.delete_cookie(COOKIE_SESION)
    return {"ok": True, "redirect": "/login"}


@app.get("/api/estado-barra", dependencies=[Depends(requiere_sesion_api)])
def estado_barra(request: Request):
    """Datos globales para la barra superior (_nav.html): estado del stock y usuario activo."""
    try:
        db = get_db()
        filas = (
            db.table("productos")
            .select("stock_actual, stock_minimo")
            .eq("comercio_id", COMERCIO_ID)
            .eq("activo", True)
            .execute()
            .data
            or []
        )
        criticos = sum(
            1
            for p in filas
            if float(p.get("stock_actual") or 0) <= float(p.get("stock_minimo") or 0)
        )
    except Exception as exc:
        log.error("Error al consultar stock crítico en estado_barra: %s", getattr(exc, "message", exc))
        criticos = 0
    info = _info_sesion(request)
    backup_info = {}
    try:
        from backup_drive import leer_estado_backup
        est_b = leer_estado_backup(APP_DIR)
        backup_info = {
            "cambios_pendientes": bool(est_b.get("cambios_pendientes", False)),
            "ultimo_backup_local": est_b.get("ultimo_backup_local", ""),
            "ultimo_estado": est_b.get("ultimo_estado", "sin_backups"),
        }
    except Exception:
        pass
    return {
        "usuario": info.get("usuario", ""),
        "rol": info.get("rol", ""),
        "debe_cambiar_pin": bool(info.get("debe_cambiar_pin")),
        "stock_critico_count": criticos,
        "modo_bloqueo": cargar_config().get("modo_bloqueo", "nunca"),
        "tiempo_inactividad_min": cargar_config().get("tiempo_inactividad_min", 15),
        "backup": backup_info,
    }


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class ItemVenta(BaseModel):
    producto_id: int
    cantidad: float = Field(gt=0, le=100_000, allow_inf_nan=False)  # Soporta enteros y decimales
    es_canje: bool = False         # True = canje con puntos de fidelización ($0.00)


class VentaIn(BaseModel):
    items: List[ItemVenta] = Field(min_length=1, max_length=200)
    cliente_id: int = Field(default=0, ge=0)  # 0 = Consumidor Final
    metodo_pago: Literal["efectivo", "transferencia", "cuenta_corriente", "mixto"]
    descuento_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    paga_con: Optional[float] = Field(default=None, ge=0, le=100_000_000, allow_inf_nan=False)
    monto_efectivo: Optional[float] = Field(default=None, ge=0, le=100_000_000, allow_inf_nan=False)
    monto_transferencia: Optional[float] = Field(default=None, ge=0, le=100_000_000, allow_inf_nan=False)


# ---------------------------------------------------------------------------
# POS: pantalla, productos, clientes y ventas
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def index(request: Request):
    """Renderiza el POS inyectando la configuración de marca blanca."""
    return templates.TemplateResponse(request, "pos.html", {"config": config_publica()})


SIN_CATEGORIA = "Sin categoría"
TABLAS_BAJA = ("productos", "clientes", "categorias")


def _dar_de_baja(db: Client, tabla: str, registro_id: int) -> None:
    """Baja lógica: el dato no se borra, queda activo=false con su fecha."""
    assert tabla in TABLAS_BAJA
    (db.table(tabla).update({"activo": False, "fecha_baja": datetime.now(timezone.utc).isoformat()})
     .eq("id", registro_id).eq("comercio_id", COMERCIO_ID).execute())


def _reactivar(db: Client, tabla: str, registro_id: int) -> None:
    assert tabla in TABLAS_BAJA
    (db.table(tabla).update({"activo": True, "fecha_baja": None})
     .eq("id", registro_id).eq("comercio_id", COMERCIO_ID).execute())


@app.get("/api/bajas/{tabla}", dependencies=[Depends(requiere_sesion_api)])
def listar_bajas(tabla: str):
    """Registros dados de baja (el más reciente primero)."""
    if tabla not in TABLAS_BAJA:
        raise HTTPException(404, "Tabla no encontrada")
    filas = (get_db().table(tabla).select("id, nombre, fecha_baja").eq("comercio_id", COMERCIO_ID)
             .eq("activo", False).order("fecha_baja", desc=True).execute().data or [])
    return [{"id": f["id"], "nombre": f["nombre"], "fecha_baja": f["fecha_baja"]} for f in filas]


def _categorias_db(db: Client, con_bajas: bool = False) -> List[dict]:
    consulta = db.table("categorias").select("*").eq("comercio_id", COMERCIO_ID)
    if not con_bajas:
        consulta = consulta.eq("activo", True)
    return consulta.order("id").execute().data or []


def _producto_out(row: dict, nombre_categoria: str) -> dict:
    """Fila de la tabla productos -> formato JSON que consume la interfaz."""
    codigo = row.get("codigo") or ""
    return {
        "id": row["id"],
        "nombre": row["nombre"],
        "codigo": codigo,
        "codigo_barras": codigo,
        "categoria_id": row.get("categoria_id"),
        "categoria": nombre_categoria,
        "unidad_medida": row.get("unidad_medida") or "unid",
        "precio_costo": _num(row.get("precio_costo")),
        "margen_ganancia": _num(row.get("margen_ganancia")),
        "usa_precio_manual": bool(row.get("usa_precio_manual", True)),
        "precio": _num(row.get("precio_venta")),
        "stock": _num(row.get("stock_actual")),
        "stock_minimo": _num(row.get("stock_minimo")),
        "imagen": row.get("imagen_url") or "",
        "puntos_canje": int(row.get("puntos_canje") or 0),
    }


@app.get("/api/productos", dependencies=[Depends(requiere_sesion_api)])
def listar_productos():
    db = get_db()
    cats = {c["id"]: c["nombre"] for c in _categorias_db(db)}
    filas = (db.table("productos").select("*").eq("comercio_id", COMERCIO_ID)
             .eq("activo", True).order("id").execute().data or [])
    return [_producto_out(p, cats.get(p.get("categoria_id"), SIN_CATEGORIA)) for p in filas]


# Id reservado para "Consumidor Final": es una venta sin cliente (cliente_id NULL en la base).
# Los clientes reales se exponen con su id real de la base (siempre >= 1), por eso 0 nunca choca.
CONSUMIDOR_FINAL_ID = 0
CONSUMIDOR_FINAL = {"id": CONSUMIDOR_FINAL_ID, "nombre": "Consumidor Final", "telefono": "", "email": "",
                    "saldo": 0, "puntos": 0, "limite_credito": 0}


def _cliente_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "nombre": row["nombre"],
        "telefono": row.get("telefono") or "",
        "email": row.get("email") or "",
        "saldo": _num(row.get("saldo_deudor")),          # saldo de Cuenta Corriente (deuda)
        "puntos": round(float(row.get("puntos") or 0), 2),
        "limite_credito": _num(row.get("limite_credito")),
    }


@app.get("/api/clientes", dependencies=[Depends(requiere_sesion_api)])
def listar_clientes():
    """Consumidor Final (primero, para el POS) + clientes reales del comercio."""
    filas = (get_db().table("clientes").select("*").eq("comercio_id", COMERCIO_ID)
             .eq("activo", True).order("nombre").execute().data or [])
    return [dict(CONSUMIDOR_FINAL)] + [_cliente_out(c) for c in filas]


FORMA_PAGO_DB = {"efectivo": "Efectivo", "transferencia": "Transferencia", "cuenta_corriente": "Cuenta Corriente", "mixto": "Mixto"}


@app.post("/api/ventas", dependencies=[Depends(requiere_sesion_api)])
def crear_venta(venta: VentaIn, request: Request):
    """Valida, recalcula totales en el servidor y registra la venta de forma atómica.

    Soporta cantidades fraccionarias (peso / monto) e ítems en canje por puntos (es_canje=True).
    El descuento de stock, la cabecera, el detalle, el saldo de Cuenta Corriente y los puntos
    se ejecutan juntos dentro de la función SQL registrar_venta_atomica (sql/).
    """
    from tickets import armar_mensaje_whatsapp_ticket, armar_url_whatsapp
    db = get_db()

    if not _caja_abierta(db) and not os.getenv("POS_ENV_FILE", "").endswith(".env.test"):
        raise HTTPException(400, "Debés registrar el saldo inicial de caja antes de comenzar a vender.")

    # --- Cliente
    cliente_out, cliente_db_id, cliente_fila = dict(CONSUMIDOR_FINAL), None, {}
    if venta.cliente_id == CONSUMIDOR_FINAL_ID:
        if venta.metodo_pago == "cuenta_corriente":
            raise HTTPException(400, "Seleccioná un cliente registrado para usar Cuenta Corriente")
    else:
        cliente_db_id = venta.cliente_id
        cliente_fila = _buscar_cliente(db, cliente_db_id)
        if not cliente_fila.get("activo", True):
            raise HTTPException(400, f"{cliente_fila['nombre']} está dado de baja")
        cliente_out = _cliente_out(cliente_fila)
        if venta.metodo_pago == "cuenta_corriente":
            limite_cli = float(cliente_fila.get("limite_credito") or 0)
            if limite_cli <= 0:
                raise HTTPException(
                    400,
                    f"{cliente_out['nombre']} tiene Límite de Crédito $0 (solo Fidelización) y no tiene Cuenta Corriente habilitada"
                )

    # --- Productos y stock (items repetidos se acumulan por producto para validar stock total)
    pedido_stock = {}
    grupos = {}  # (pid, es_canje) -> cantidad
    for it in venta.items:
        cant = round(float(it.cantidad), 4)
        pedido_stock[it.producto_id] = round(pedido_stock.get(it.producto_id, 0.0) + cant, 4)
        clave = (it.producto_id, bool(it.es_canje))
        grupos[clave] = round(grupos.get(clave, 0.0) + cant, 4)

    filas = (db.table("productos")
             .select("id, nombre, precio_venta, precio_costo, stock_actual, puntos_canje")
             .eq("comercio_id", COMERCIO_ID).eq("activo", True).in_("id", list(pedido_stock)).execute().data or [])
    productos = {p["id"]: p for p in filas}
    for pid, cantidad in pedido_stock.items():
        prod = productos.get(pid)
        if not prod:
            raise HTTPException(400, f"El producto {pid} no está disponible")
        if cantidad > float(prod.get("stock_actual") or 0):
            raise HTTPException(400, f"Stock insuficiente de {prod['nombre']}")

    # --- Totales y canjes. El descuento se reparte en el precio unitario de cada renglón normal.
    detalle, items_rpc, subtotal, total, puntos_canjeados = [], [], 0.0, 0.0, 0.0
    for (pid, es_canje), cantidad in grupos.items():
        prod = productos[pid]
        costo = float(prod.get("precio_costo") or 0)
        if es_canje:
            pts_u = int(prod.get("puntos_canje") or 0)
            if pts_u <= 0:
                raise HTTPException(400, f"El producto '{prod['nombre']}' no participa del programa de premios")
            pts_item = round(pts_u * cantidad, 2)
            puntos_canjeados += pts_item
            detalle.append({"producto_id": prod["id"], "nombre": f"{prod['nombre']} (Canje)",
                            "cantidad": _num(cantidad), "precio": 0, "total": 0,
                            "es_canje": True, "puntos_canje": pts_item})
            items_rpc.append({"producto_id": prod["id"], "cantidad": cantidad,
                              "precio_unitario": 0.0, "costo_unitario": costo})
        else:
            precio = float(prod.get("precio_venta") or 0)
            linea = round(precio * cantidad, 2)
            subtotal += linea
            unitario = round(precio * (1 - venta.descuento_pct / 100), 2)
            linea_desc = round(cantidad * unitario, 2)
            total += linea_desc
            detalle.append({"producto_id": prod["id"], "nombre": prod["nombre"],
                            "cantidad": _num(cantidad), "precio": _num(precio), "total": _num(linea_desc),
                            "es_canje": False, "puntos_canje": 0})
            items_rpc.append({"producto_id": prod["id"], "cantidad": cantidad,
                              "precio_unitario": unitario, "costo_unitario": costo})

    subtotal, total, puntos_canjeados = round(subtotal, 2), round(total, 2), round(puntos_canjeados, 2)
    descuento = round(subtotal - total, 2)

    if venta.metodo_pago == "cuenta_corriente" and cliente_db_id is not None:
        limite_cli = float(cliente_fila.get("limite_credito") or 0)
        saldo_actual = float(cliente_fila.get("saldo_deudor") or 0)
        if limite_cli > 0 and round(saldo_actual + total, 2) > limite_cli:
            disponible = max(0.0, round(limite_cli - saldo_actual, 2))
            raise HTTPException(
                400,
                f"La compra supera el límite de Cuenta Corriente de {cliente_out['nombre']} (Disponible: {fmt_moneda(disponible)})"
            )

    if puntos_canjeados > 0:
        if cliente_db_id is None:
            raise HTTPException(400, "Para canjear premios con puntos seleccioná un cliente registrado")
        pts_actuales = round(float(cliente_fila.get("puntos") or 0), 2)
        if pts_actuales < puntos_canjeados:
            raise HTTPException(
                400,
                f"Puntos insuficientes: {cliente_out['nombre']} tiene {fmt_puntos(pts_actuales)} pts y el canje requiere {fmt_puntos(puntos_canjeados)} pts"
            )

    vuelto = 0.0
    if venta.metodo_pago == "efectivo" and venta.paga_con is not None:
        if venta.paga_con < total:
            raise HTTPException(400, "El monto recibido es menor al total")
        vuelto = round(venta.paga_con - total, 2)

    # --- Puntos de fidelidad: ganados en compra normal menos puntos canjeados
    puntos_ganados = _puntos_por_monto(total) if cliente_db_id is not None else 0.0
    puntos_netos = round(puntos_ganados - puntos_canjeados, 2)

    # --- Forma de pago y desglose si es mixto
    if venta.metodo_pago == "mixto":
        m_ef = round(float(venta.monto_efectivo or 0.0), 2)
        m_tr = round(float(venta.monto_transferencia or 0.0), 2)
        if m_ef < 0 or m_tr < 0:
            raise HTTPException(400, "Los importes de pago mixto deben ser mayores o iguales a 0")
        if abs(round(m_ef + m_tr, 2) - total) > 0.01:
            raise HTTPException(400, f"La suma de Efectivo ({fmt_moneda(m_ef)}) y Transferencia ({fmt_moneda(m_tr)}) debe ser igual al total ({fmt_moneda(total)})")
        forma_pago_db_str = f"Mixto (Ef: {fmt_moneda(m_ef)} | Tr: {fmt_moneda(m_tr)})"
    else:
        forma_pago_db_str = FORMA_PAGO_DB[venta.metodo_pago]

    # --- Transacción real
    try:
        res = db.rpc("registrar_venta_atomica", {
            "p_comercio_id": COMERCIO_ID,
            "p_cliente_id": cliente_db_id,
            "p_metodo_pago": forma_pago_db_str,
            "p_total": total,
            "p_items": items_rpc,
            "p_puntos_ganados": puntos_netos,
        }).execute()
    except APIError as e:
        if getattr(e, "code", None) in ("SV001", "SV002"):  # reglas de negocio de la función SQL
            raise HTTPException(400, getattr(e, "message", "No se pudo registrar la venta"))
        raise

    venta_id = res.data[0] if isinstance(res.data, list) else res.data

    _registrar_auditoria(request, "VENTA", {
        "venta_id": venta_id,
        "cliente_id": cliente_db_id,
        "cliente_nombre": cliente_out["nombre"],
        "forma_pago": forma_pago_db_str,
        "total": _num(total),
        "descuento_pct": _num(venta.descuento_pct),
        "items": [
            {
                "producto_id": it["producto_id"],
                "nombre": it["nombre"],
                "cantidad": it["cantidad"],
                "total": it["total"],
            }
            for it in detalle
        ],
    })

    if venta.metodo_pago == "cuenta_corriente":
        cliente_out["saldo"] = _num(cliente_out["saldo"] + total)
    if cliente_db_id is not None:
        cliente_out["puntos"] = round(float(cliente_fila.get("puntos") or 0) + puntos_netos, 2)

    cfg = cargar_config()
    fecha_str = _ahora_local().strftime("%d/%m/%Y %H:%M")
    whatsapp_url = ""
    if cliente_out.get("telefono"):
        msg = armar_mensaje_whatsapp_ticket({
            "negocio_nombre": cfg.get("nombre_comercio", "Mi Comercio"),
            "cliente_nombre": cliente_out["nombre"],
            "venta_id": venta_id,
            "fecha": fecha_str,
            "forma_pago": forma_pago_db_str,
            "total": total,
            "puntos_ganados": puntos_ganados,
            "puntos_canjeados": puntos_canjeados,
            "puntos_balance": cliente_out.get("puntos", 0),
        })
        whatsapp_url = armar_url_whatsapp(cliente_out["telefono"], msg)

    return {
        "id": venta_id,
        "fecha": _ahora_local().isoformat(timespec="seconds"),
        "fecha_formateada": fecha_str,
        "cliente": cliente_out,
        "metodo_pago": venta.metodo_pago,
        "forma_pago": forma_pago_db_str,
        "items": detalle,
        "subtotal": _num(subtotal),
        "descuento_pct": venta.descuento_pct,
        "descuento": _num(descuento),
        "total": _num(total),
        "paga_con": venta.paga_con,
        "vuelto": _num(vuelto),
        "puntos_ganados": _num(puntos_ganados),
        "puntos_canjeados": puntos_canjeados,
        "puntos_balance": cliente_out.get("puntos", 0),
        "whatsapp_url": whatsapp_url,
    }


@app.get("/api/ventas/{venta_id}/ticket.pdf", dependencies=[Depends(requiere_sesion_api)])
def descargar_ticket_pdf(venta_id: int, canjeados: float = 0.0):
    """Genera y descarga el comprobante térmico de 80 mm en formato PDF."""
    from tickets import generar_pdf_ticket_termico
    db = get_db()
    filas_v = db.table("ventas").select("*").eq("id", venta_id).eq("comercio_id", COMERCIO_ID).limit(1).execute().data or []
    if not filas_v:
        raise HTTPException(404, "Ticket no encontrado")
    v = filas_v[0]

    cliente_nombre = "Consumidor Final"
    puntos_balance = 0.0
    if v.get("cliente_id"):
        cli_filas = (
            db.table("clientes")
            .select("nombre, puntos")
            .eq("id", v["cliente_id"])
            .eq("comercio_id", COMERCIO_ID)
            .limit(1)
            .execute()
            .data or []
        )
        if cli_filas:
            cliente_nombre = cli_filas[0].get("nombre") or "Consumidor Final"
            puntos_balance = float(cli_filas[0].get("puntos") or 0)

    renglones = (db.table("detalle_ventas").select("*, productos(nombre)")
                 .eq("venta_id", venta_id).execute().data or [])
    items_pdf = []
    for r in renglones:
        cant = float(r.get("cantidad") or 0)
        if cant <= 0:
            continue
        pr = r.get("productos")
        if isinstance(pr, list):
            pr = pr[0] if pr else None
        nombre_p = pr.get("nombre") if pr else "Artículo" 
        p_unit = float(r.get("precio_unitario") or 0)
        items_pdf.append({
            "nombre": nombre_p + (" (Canje)" if p_unit == 0 else ""),
            "cantidad": _num(cant),
            "precio_unitario": p_unit,
            "subtotal": round(cant * p_unit, 2),
        })

    fecha_raw = v.get("fecha_hora") or v.get("fecha") or ""
    try:
        dt = datetime.fromisoformat(str(fecha_raw).replace("Z", "+00:00")).astimezone(TZ_AR)
        fecha_hora = dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        fecha_hora = _ahora_local().strftime("%d/%m/%Y %H:%M")

    pts_netos = float(v.get("puntos_ganados") or 0)
    total_v = float(v.get("total_venta") or 0)
    if v.get("cliente_id"):
        pts_ganados = _puntos_por_monto(total_v) if total_v > 0 else 0.0
        pts_canje = canjeados if canjeados > 0 else max(0.0, round(pts_ganados - pts_netos, 2))
    else:
        pts_ganados, pts_canje = 0.0, 0

    cfg = cargar_config()
    pdf_bytes = generar_pdf_ticket_termico({
        "venta_id": venta_id,
        "fecha_hora": fecha_hora,
        "cliente_nombre": cliente_nombre,
        "forma_pago": v.get("forma_pago") or "Efectivo",
        "items": items_pdf,
        "total_venta": total_v,
        "puntos_ganados": pts_ganados,
        "puntos_canjeados": pts_canje,
        "puntos_balance": puntos_balance,
        "direccion_negocio": cfg.get("direccion") or "",
        "telefono_negocio": cfg.get("telefono") or "",
    }, nombre_negocio=cfg.get("nombre_comercio", "Mi Comercio"))

    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="ticket_{venta_id}.pdf"'},
    )


class DevolverItemVentaIn(BaseModel):
    detalle_id: int
    cantidad: Optional[float] = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)


@app.post("/api/ventas/{venta_id}/devolver-item", dependencies=[Depends(requiere_sesion_api)])
def devolver_item_venta(venta_id: int, data: DevolverItemVentaIn, request: Request):
    """Devuelve parcial o totalmente un producto de un ticket confirmado.

    Reintegra el stock al inventario, recalcula el total del ticket, descuenta el saldo de
    Cuenta Corriente si fue fiado y ajusta los puntos del cliente. Si era el último artículo
    del ticket, el ticket queda anulado (archivado).
    """
    db = get_db()
    filas_v = (
        db.table("ventas")
        .select("*")
        .eq("id", venta_id)
        .eq("comercio_id", COMERCIO_ID)
        .is_("archivado_en", "null")
        .limit(1)
        .execute()
        .data or []
    )
    if not filas_v:
        raise HTTPException(404, "El ticket no existe o ya fue anulado")
    v = filas_v[0]

    filas_d = (
        db.table("detalle_ventas")
        .select("*")
        .eq("id", data.detalle_id)
        .eq("venta_id", venta_id)
        .limit(1)
        .execute()
        .data or []
    )
    if not filas_d:
        raise HTTPException(404, "No se encontró el producto dentro de este ticket")
    d = filas_d[0]

    cant_actual = round(float(d.get("cantidad") or 0.0), 4)
    if cant_actual <= 0:
        raise HTTPException(400, "Este producto ya fue devuelto")

    cant_dev = round(float(data.cantidad), 4) if data.cantidad is not None else (1.0 if cant_actual >= 1.0 else cant_actual)
    if cant_dev > cant_actual:
        raise HTTPException(400, f"La cantidad a devolver ({fmt_cantidad(cant_dev)}) supera la vendida ({fmt_cantidad(cant_actual)})")

    # 1. Reintegrar stock al producto
    prod_id = d.get("producto_id")
    prod = None
    nombre_prod = "Producto"
    if prod_id:
        filas_p = (
            db.table("productos")
            .select("*")
            .eq("id", prod_id)
            .eq("comercio_id", COMERCIO_ID)
            .limit(1)
            .execute()
            .data or []
        )
        if filas_p:
            prod = filas_p[0]
            nombre_prod = prod.get("nombre") or "Producto"
            nuevo_stock = round(float(prod.get("stock_actual") or 0.0) + cant_dev, 4)
            db.table("productos").update({"stock_actual": nuevo_stock}).eq("id", prod_id).eq("comercio_id", COMERCIO_ID).execute()

    # 2. Actualizar cantidad del renglón
    nueva_cant = round(cant_actual - cant_dev, 4)
    db.table("detalle_ventas").update({"cantidad": nueva_cant}).eq("id", d["id"]).execute()

    p_unit = float(d.get("precio_unitario") or 0.0)
    monto_devuelto = round(cant_dev * p_unit, 2)

    todos_renglones = db.table("detalle_ventas").select("*").eq("venta_id", venta_id).execute().data or []
    activos = [r for r in todos_renglones if float(r.get("cantidad") or 0.0) > 0]
    nuevo_total_venta = round(
        sum(float(r.get("cantidad") or 0.0) * float(r.get("precio_unitario") or 0.0) for r in activos), 2
    )

    # 3. Ajustar saldo de Cuenta Corriente y Puntos si el ticket tiene cliente asignado
    cli_id = v.get("cliente_id")
    es_cta_cte = (v.get("forma_pago") or "").strip().lower() == "cuenta corriente"
    nuevos_pts_venta = float(v.get("puntos_ganados") or 0.0)

    if cli_id:
        filas_c = (
            db.table("clientes")
            .select("*")
            .eq("id", cli_id)
            .eq("comercio_id", COMERCIO_ID)
            .limit(1)
            .execute()
            .data or []
        )
        if filas_c:
            cli = filas_c[0]
            updates_cli = {}
            if es_cta_cte and monto_devuelto > 0:
                updates_cli["saldo_deudor"] = max(0.0, round(float(cli.get("saldo_deudor") or 0.0) - monto_devuelto, 2))
            if p_unit > 0:
                pts_restar = _puntos_por_monto(monto_devuelto)
                updates_cli["puntos"] = max(0.0, round(float(cli.get("puntos") or 0.0) - pts_restar, 2))
                nuevos_pts_venta = round(nuevos_pts_venta - pts_restar, 2)
            else:
                pts_canje_u = float(prod.get("puntos_canje") or 0.0) if prod else 0.0
                pts_devolver = round(pts_canje_u * cant_dev, 2)
                updates_cli["puntos"] = round(float(cli.get("puntos") or 0.0) + pts_devolver, 2)
                nuevos_pts_venta = round(nuevos_pts_venta + pts_devolver, 2)
            db.table("clientes").update(updates_cli).eq("id", cli_id).eq("comercio_id", COMERCIO_ID).execute()

    info = _info_sesion(request)
    ahora_utc = datetime.now(timezone.utc).isoformat()
    ticket_anulado = len(activos) == 0

    if ticket_anulado:
        db.table("ventas").update({
            "total_venta": 0,
            "puntos_ganados": 0,
            "archivado_en": ahora_utc,
            "archivado_por": info.get("usuario_id"),
        }).eq("id", venta_id).eq("comercio_id", COMERCIO_ID).execute()
        if cli_id and es_cta_cte:
            db.table("movimientos_cuenta").update({
                "archivado_en": ahora_utc,
                "archivado_por": info.get("usuario_id"),
            }).eq("ticket_id", venta_id).eq("comercio_id", COMERCIO_ID).execute()
    else:
        db.table("ventas").update({
            "total_venta": nuevo_total_venta,
            "puntos_ganados": nuevos_pts_venta if cli_id else 0,
        }).eq("id", venta_id).eq("comercio_id", COMERCIO_ID).execute()
        if cli_id and es_cta_cte:
            movs_cc = (
                db.table("movimientos_cuenta")
                .select("*")
                .eq("ticket_id", venta_id)
                .eq("comercio_id", COMERCIO_ID)
                .is_("archivado_en", "null")
                .execute()
                .data or []
            )
            for m in movs_cc:
                desc_actual = (m.get("descripcion") or f"Compra Cta. Cte. - Ticket #{venta_id}").strip()
                nota_dev = f" [Devuelto: {fmt_cantidad(cant_dev)}x {nombre_prod}]"
                nueva_desc = (desc_actual + nota_dev)[:200]
                db.table("movimientos_cuenta").update({
                    "monto": nuevo_total_venta,
                    "descripcion": nueva_desc,
                }).eq("id", m["id"]).execute()

    _registrar_auditoria(request, "DEVOLUCION_ITEM", {
        "venta_id": venta_id,
        "producto_id": prod_id,
        "producto_nombre": nombre_prod,
        "cantidad_devuelta": _num(cant_dev),
        "monto_devuelto": _num(monto_devuelto),
        "ticket_anulado": ticket_anulado,
    })

    return {
        "ok": True,
        "ticket_anulado": ticket_anulado,
        "producto_nombre": nombre_prod,
        "cantidad_devuelta": _num(cant_dev),
        "monto_devuelto": _num(monto_devuelto),
        "nuevo_total_ticket": _num(0 if ticket_anulado else nuevo_total_venta),
    }


@app.post("/api/ventas/{venta_id}/anular", dependencies=[Depends(requiere_sesion_api)])
def anular_venta_completa(venta_id: int, request: Request):
    """Anula un ticket completo: devuelve el stock de todos sus productos, revierte el cargo
    en Cuenta Corriente y los puntos del cliente, y archiva el comprobante.
    """
    db = get_db()
    filas_v = (
        db.table("ventas")
        .select("*")
        .eq("id", venta_id)
        .eq("comercio_id", COMERCIO_ID)
        .is_("archivado_en", "null")
        .limit(1)
        .execute()
        .data or []
    )
    if not filas_v:
        raise HTTPException(404, "El ticket no existe o ya fue anulado")
    v = filas_v[0]

    renglones = db.table("detalle_ventas").select("*").eq("venta_id", venta_id).execute().data or []
    for d in renglones:
        cant = round(float(d.get("cantidad") or 0.0), 4)
        if cant <= 0:
            continue
        prod_id = d.get("producto_id")
        if prod_id:
            filas_p = (
                db.table("productos")
                .select("stock_actual")
                .eq("id", prod_id)
                .eq("comercio_id", COMERCIO_ID)
                .limit(1)
                .execute()
                .data or []
            )
            if filas_p:
                nuevo_stock = round(float(filas_p[0].get("stock_actual") or 0.0) + cant, 4)
                db.table("productos").update({"stock_actual": nuevo_stock}).eq("id", prod_id).eq("comercio_id", COMERCIO_ID).execute()
        db.table("detalle_ventas").update({"cantidad": 0}).eq("id", d["id"]).execute()

    cli_id = v.get("cliente_id")
    es_cta_cte = (v.get("forma_pago") or "").strip().lower() == "cuenta corriente"
    monto_ticket = round(float(v.get("total_venta") or 0.0), 2)
    pts_netos = round(float(v.get("puntos_ganados") or 0.0), 2)

    if cli_id:
        filas_c = (
            db.table("clientes")
            .select("*")
            .eq("id", cli_id)
            .eq("comercio_id", COMERCIO_ID)
            .limit(1)
            .execute()
            .data or []
        )
        if filas_c:
            cli = filas_c[0]
            updates_cli = {
                "puntos": max(0.0, round(float(cli.get("puntos") or 0.0) - pts_netos, 2)),
            }
            if es_cta_cte and monto_ticket > 0:
                updates_cli["saldo_deudor"] = max(0.0, round(float(cli.get("saldo_deudor") or 0.0) - monto_ticket, 2))
            db.table("clientes").update(updates_cli).eq("id", cli_id).eq("comercio_id", COMERCIO_ID).execute()

    info = _info_sesion(request)
    ahora_utc = datetime.now(timezone.utc).isoformat()
    db.table("ventas").update({
        "archivado_en": ahora_utc,
        "archivado_por": info.get("usuario_id"),
    }).eq("id", venta_id).eq("comercio_id", COMERCIO_ID).execute()

    if cli_id and es_cta_cte:
        db.table("movimientos_cuenta").update({
            "archivado_en": ahora_utc,
            "archivado_por": info.get("usuario_id"),
        }).eq("ticket_id", venta_id).eq("comercio_id", COMERCIO_ID).execute()

    _registrar_auditoria(request, "ANULAR_VENTA", {
        "venta_id": venta_id,
        "forma_pago": v.get("forma_pago"),
        "monto_anulado": _num(monto_ticket),
        "cliente_id": cli_id,
    })

    return {
        "ok": True,
        "venta_id": venta_id,
        "monto_anulado": _num(monto_ticket),
    }


# ---------------------------------------------------------------------------
# Clientes (Padrón de Clientes): pantalla y CRUD
# (el listado GET /api/clientes está más arriba porque lo usa el selector del POS)
# ---------------------------------------------------------------------------
class ClienteIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    telefono: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=120)
    limite_credito: float = Field(default=0, ge=0, le=100_000_000, allow_inf_nan=False)  # 0 = solo Fidelización (sin Cuenta Corriente)


def _buscar_cliente(db: Client, cliente_id: int) -> dict:
    filas = (db.table("clientes").select("*").eq("id", cliente_id)
             .eq("comercio_id", COMERCIO_ID).execute().data or [])
    if not filas:
        raise HTTPException(404, "Cliente no encontrado")
    return filas[0]


@app.get("/clientes", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def clientes_page(request: Request):
    return templates.TemplateResponse(request, "clientes.html", {"config": config_publica()})


@app.post("/api/clientes", status_code=201, dependencies=[Depends(requiere_sesion_api)])
def crear_cliente(data: ClienteIn):
    nombre = data.nombre.strip()
    if not nombre:
        raise HTTPException(400, "El nombre del cliente es obligatorio")
    res = get_db().table("clientes").insert({
        "comercio_id": COMERCIO_ID,
        "nombre": nombre,
        "telefono": data.telefono.strip(),
        "email": data.email.strip(),
        "limite_credito": data.limite_credito,
        "saldo_deudor": 0,
        "puntos": 0,
    }).execute()
    return _cliente_out(res.data[0])


@app.put("/api/clientes/{cliente_id}", dependencies=[Depends(requiere_sesion_api)])
def editar_cliente(cliente_id: int, data: ClienteIn):
    """Edita datos de contacto y límite. El saldo y los puntos solo cambian por pagos, ventas y canjes."""
    db = get_db()
    _buscar_cliente(db, cliente_id)
    nombre = data.nombre.strip()
    if not nombre:
        raise HTTPException(400, "El nombre del cliente es obligatorio")
    res = (db.table("clientes").update({
        "nombre": nombre, "telefono": data.telefono.strip(),
        "email": data.email.strip(), "limite_credito": data.limite_credito,
    }).eq("id", cliente_id).eq("comercio_id", COMERCIO_ID).execute())
    return _cliente_out(res.data[0])


@app.delete("/api/clientes/{cliente_id}", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def eliminar_cliente(cliente_id: int):
    db = get_db()
    cli = _buscar_cliente(db, cliente_id)
    saldo = float(cli.get("saldo_deudor") or 0)
    if saldo > 0:
        raise HTTPException(409, f"No se puede dar de baja a {cli['nombre']}: tiene una deuda de Cuenta Corriente de {fmt_moneda(saldo)}")
    _dar_de_baja(db, "clientes", cliente_id)
    return {"ok": True}


@app.post("/api/clientes/{cliente_id}/reactivar", dependencies=[Depends(requiere_sesion_api)])
def reactivar_cliente(cliente_id: int):
    db = get_db()
    _buscar_cliente(db, cliente_id)
    _reactivar(db, "clientes", cliente_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cuenta Corriente: historial y pagos (tabla movimientos_cuenta)
# ---------------------------------------------------------------------------
METODO_ABONO_LABEL = {
    "efectivo": "Efectivo",
    "transferencia": "Transferencia",
    "mercado_pago": "Mercado Pago",
    "tarjeta": "Tarjeta",
}


class PagoCtaCteIn(BaseModel):
    monto: float = Field(gt=0, le=100_000_000, allow_inf_nan=False)
    metodo_pago: Literal["efectivo", "transferencia", "mercado_pago", "tarjeta"] = "efectivo"
    descripcion: str = Field(default="", max_length=200)


@app.get("/api/clientes/{cliente_id}/movimientos", dependencies=[Depends(requiere_sesion_api)])
def movimientos_cliente(cliente_id: int):
    """Historial de Cuenta Corriente (cargos por ventas y pagos), el más reciente primero."""
    db = get_db()
    cli = _buscar_cliente(db, cliente_id)
    filas = (db.table("movimientos_cuenta").select("*").eq("cliente_id", cliente_id)
             .eq("comercio_id", COMERCIO_ID).is_("archivado_en", "null").order("id", desc=True).execute().data or [])
    return {
        "cliente": _cliente_out(cli),
        "movimientos": [{
            "id": m["id"],
            "fecha": m.get("fecha_hora") or "",
            "tipo": m.get("tipo") or "CARGO",
            "monto": _num(m.get("monto")),
            "descripcion": m.get("descripcion") or "",
            "ticket_id": m.get("ticket_id"),
        } for m in filas],
    }


@app.post("/api/clientes/{cliente_id}/pago-cta-cte", dependencies=[Depends(requiere_sesion_api)])
def pago_cta_cte(cliente_id: int, pago: PagoCtaCteIn, request: Request):
    """Registra un pago total o parcial que descuenta el saldo deudor.

    Si se cobra en efectivo, queda asentado como Pago a cuenta (Efectivo) en
    movimientos_cuenta y suma al rubro Cobros de Cuenta Corriente (Efectivo) del arqueo diario.
    """
    db = get_db()
    cli = _buscar_cliente(db, cliente_id)
    try:
        res = db.rpc("registrar_pago_cuenta", {
            "p_comercio_id": COMERCIO_ID, "p_cliente_id": cliente_id,
            "p_monto": round(pago.monto, 2),
            "p_metodo": METODO_ABONO_LABEL.get(pago.metodo_pago, "Efectivo"),
            "p_descripcion": pago.descripcion.strip(),
            "p_usuario_nombre": _info_sesion(request).get("usuario", ""),
        }).execute()
    except APIError as e:
        if getattr(e, "code", None) in ("SV001", "SV002"):
            raise HTTPException(400, getattr(e, "message", "No se pudo registrar el pago"))
        raise
    nuevo_saldo = res.data["saldo"] if isinstance(res.data, dict) else res.data[0]["saldo"]
    return {"ok": True, "cliente": {**_cliente_out(cli), "saldo": _num(nuevo_saldo)}}


# ---------------------------------------------------------------------------
# Fidelización: regla de puntos y catálogo de premios
#
# Igual que en el sistema original, un premio es un producto con puntos_canje > 0
# (columna productos.puntos_canje) y el canje se registra como una venta de $0 que
# descuenta stock y puntos dentro de registrar_venta_atomica.
# ---------------------------------------------------------------------------
PESOS_POR_PUNTO_DEFAULT = 100  # el original daba 1% del total: 1 punto cada $100


def _pesos_por_punto() -> float:
    try:
        v = float(cargar_config().get("pesos_por_punto", PESOS_POR_PUNTO_DEFAULT))
    except (TypeError, ValueError):
        return float(PESOS_POR_PUNTO_DEFAULT)
    return v if v > 0 else float(PESOS_POR_PUNTO_DEFAULT)


def _puntos_por_monto(total: float) -> float:
    return round(total / _pesos_por_punto(), 2)


class FidelizacionConfigIn(BaseModel):
    pesos_por_punto: float = Field(gt=0, le=1_000_000)


class PremioIn(BaseModel):
    producto_id: int
    puntos_canje: int = Field(gt=0)


class PremioEditIn(BaseModel):
    puntos_canje: int = Field(gt=0)
    stock: Optional[float] = Field(default=None, ge=0)


class CanjeIn(BaseModel):
    premio_id: int
    cantidad: int = Field(default=1, gt=0)


def _premio_out(p: dict) -> dict:
    return {"id": p["id"], "nombre": p["nombre"], "puntos_canje": int(p.get("puntos_canje") or 0),
            "stock": _num(p.get("stock_actual")), "precio": _num(p.get("precio_venta"))}


@app.get("/api/fidelizacion/config", dependencies=[Depends(requiere_sesion_api)])
def obtener_config_fidelizacion():
    return {"pesos_por_punto": _num(_pesos_por_punto())}


@app.put("/api/fidelizacion/config", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def actualizar_config_fidelizacion(data: FidelizacionConfigIn):
    guardar_config({**cargar_config(), "pesos_por_punto": data.pesos_por_punto})
    return {"pesos_por_punto": _num(data.pesos_por_punto)}


@app.get("/api/premios", dependencies=[Depends(requiere_sesion_api)])
def listar_premios():
    filas = (get_db().table("productos").select("*").eq("comercio_id", COMERCIO_ID)
             .eq("activo", True).gt("puntos_canje", 0).order("puntos_canje").execute().data or [])
    return [_premio_out(p) for p in filas]


@app.post("/api/premios", status_code=201, dependencies=[Depends(requiere_sesion_api)])
def crear_premio(data: PremioIn):
    """Convierte un producto del inventario en premio canjeable asignándole puntos."""
    db = get_db()
    prod = _buscar_producto(db, data.producto_id)
    if int(prod.get("puntos_canje") or 0) > 0:
        raise HTTPException(409, "Ese producto ya es un premio")
    res = (db.table("productos").update({"puntos_canje": data.puntos_canje})
           .eq("id", data.producto_id).eq("comercio_id", COMERCIO_ID).execute())
    return _premio_out(res.data[0])


@app.put("/api/premios/{premio_id}", dependencies=[Depends(requiere_sesion_api)])
def editar_premio(premio_id: int, data: PremioEditIn):
    db = get_db()
    prod = _buscar_producto(db, premio_id)
    if int(prod.get("puntos_canje") or 0) <= 0:
        raise HTTPException(404, "Premio no encontrado")
    payload = {"puntos_canje": data.puntos_canje}
    if data.stock is not None:
        payload["stock_actual"] = data.stock
    res = db.table("productos").update(payload).eq("id", premio_id).eq("comercio_id", COMERCIO_ID).execute()
    return _premio_out(res.data[0])


@app.delete("/api/premios/{premio_id}", dependencies=[Depends(requiere_sesion_api)])
def quitar_premio(premio_id: int):
    """Saca el producto del catálogo de premios (no borra el producto ni sus ventas)."""
    db = get_db()
    prod = _buscar_producto(db, premio_id)
    if int(prod.get("puntos_canje") or 0) <= 0:
        raise HTTPException(404, "Premio no encontrado")
    db.table("productos").update({"puntos_canje": 0}).eq("id", premio_id).eq("comercio_id", COMERCIO_ID).execute()
    return {"ok": True}


@app.post("/api/clientes/{cliente_id}/canjear-premio", dependencies=[Depends(requiere_sesion_api)])
def canjear_premio(cliente_id: int, data: CanjeIn):
    """Canjea un premio: descuenta los puntos del cliente y el stock del producto.

    Se registra como una venta de $0 ("Canje de Premios") con puntos negativos mediante
    registrar_venta_atomica, igual que el sistema original: stock y puntos en una sola transacción.
    """
    db = get_db()
    cli = _buscar_cliente(db, cliente_id)
    if not cli.get("activo", True):
        raise HTTPException(400, f"{cli['nombre']} está dado de baja")
    prod = _buscar_producto(db, data.premio_id)
    if not prod.get("activo", True):
        raise HTTPException(400, f"El producto {prod['id']} no está disponible")
    puntos_unidad = int(prod.get("puntos_canje") or 0)
    if puntos_unidad <= 0:
        raise HTTPException(404, "Ese producto no participa del programa de premios")
    puntos_total = round(puntos_unidad * data.cantidad, 2)  # cantidad es int; round por consistencia
    puntos_actuales = round(float(cli.get("puntos") or 0), 2)
    if puntos_actuales < puntos_total:
        raise HTTPException(400, f"Puntos insuficientes: {cli['nombre']} tiene {fmt_puntos(puntos_actuales)} y el canje requiere {fmt_puntos(puntos_total)}")
    if data.cantidad > float(prod.get("stock_actual") or 0):
        raise HTTPException(400, f"Stock insuficiente de {prod['nombre']}")

    try:
        res = db.rpc("registrar_venta_atomica", {
            "p_comercio_id": COMERCIO_ID,
            "p_cliente_id": cliente_id,
            "p_metodo_pago": "Canje de Premios",
            "p_total": 0,
            "p_items": [{"producto_id": prod["id"], "cantidad": data.cantidad,
                         "precio_unitario": 0, "costo_unitario": float(prod.get("precio_costo") or 0)}],
            "p_puntos_ganados": -puntos_total,
        }).execute()
    except APIError as e:
        if getattr(e, "code", None) in ("SV001", "SV002"):
            raise HTTPException(400, getattr(e, "message", "No se pudo registrar el canje"))
        raise
    return {"ok": True, "ticket_id": res.data, "premio": prod["nombre"], "cantidad": data.cantidad,
            "puntos_descontados": puntos_total, "puntos_restantes": round(puntos_actuales - puntos_total, 2)}


# ---------------------------------------------------------------------------
# Inventario: pantalla y CRUD de productos
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # límite de seguridad para el archivo original


@app.get("/inventario", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def inventario(request: Request):
    """Renderiza la pantalla de gestión de inventario."""
    return templates.TemplateResponse(request, "inventario.html", {"config": config_publica()})


def _buscar_producto(db: Client, producto_id: int) -> dict:
    filas = (db.table("productos").select("*").eq("id", producto_id)
             .eq("comercio_id", COMERCIO_ID).execute().data or [])
    if not filas:
        raise HTTPException(404, "Producto no encontrado")
    return filas[0]


def _resolver_categoria(db: Client, nombre: str) -> dict:
    """Devuelve la categoría con ese nombre (sin distinguir mayúsculas); la crea si no existe."""
    nombre = nombre.strip()
    for c in _categorias_db(db, con_bajas=True):
        if c["nombre"].lower() == nombre.lower():
            if not c.get("activo", True):
                _reactivar(db, "categorias", c["id"])
            return c
    res = db.table("categorias").insert(
        {"nombre": nombre, "descripcion": "", "comercio_id": COMERCIO_ID}).execute()
    return res.data[0]


def calcular_precio_venta(precio_costo: float, margen: float) -> float:
    """Precio de venta = costo + margen porcentual (igual que models.calcular_precio_venta)."""
    return round(float(precio_costo or 0) * (1 + float(margen or 0) / 100.0), 2)


def _precio_final(costo: float, margen: float, usa_manual: bool, precio_manual: Optional[float]) -> float:
    """Valida y devuelve el precio de venta según el modo (manual o costo + margen)."""
    if costo < 0 or margen < 0:
        raise HTTPException(400, "El costo y el margen no pueden ser negativos")
    if usa_manual:
        if precio_manual is None or precio_manual <= 0:
            raise HTTPException(400, "Si sobreescribís el precio, el precio de venta debe ser mayor a 0")
        return round(float(precio_manual), 2)
    precio = calcular_precio_venta(costo, margen)
    if precio <= 0:
        raise HTTPException(400, "El precio de venta calculado es 0. Cargá un costo mayor a 0 o un precio manual")
    return precio


def _codigo_libre(db: Client, codigo: str, excluir_id: Optional[int] = None) -> None:
    filas = db.table("productos").select("id, nombre, activo").eq("comercio_id", COMERCIO_ID).eq("codigo", codigo).execute().data or []
    for f in filas:
        if excluir_id is None or f["id"] != excluir_id:
            if not f.get("activo", True):
                raise HTTPException(409, f"El código '{codigo}' es de un producto dado de baja ({f['nombre']}). "
                                         "Reactivalo desde Dados de baja.")
            raise HTTPException(409, f"Ya existe un producto con el código '{codigo}' ({f['nombre']})")


def _siguiente_codigo(db: Client) -> str:
    """Próximo SKU autogenerado con formato Prod-001, Prod-002…"""
    codigos = db.table("productos").select("codigo").eq("comercio_id", COMERCIO_ID).execute().data or []
    mayor = 0
    for c in codigos:
        m = re.fullmatch(r"Prod-(\d+)", str(c.get("codigo") or ""), flags=re.IGNORECASE)
        if m:
            mayor = max(mayor, int(m.group(1)))
    return f"Prod-{mayor + 1:03d}"


@app.get("/api/inventario/siguiente-codigo", dependencies=[Depends(requiere_sesion_api)])
def siguiente_codigo():
    return {"codigo": _siguiente_codigo(get_db())}


@app.post("/api/inventario/productos", status_code=201, dependencies=[Depends(requiere_sesion_api)])
def crear_producto(
    request: Request,
    nombre: str = Form(..., min_length=1),
    categoria: str = Form(..., min_length=1),
    codigo: str = Form(""),                        # vacío = se autogenera (Prod-001…)
    precio_costo: float = Form(0, ge=0),
    margen_ganancia: float = Form(0, ge=0),
    usa_precio_manual: bool = Form(False),
    precio: Optional[float] = Form(None),          # precio de venta manual
    stock: float = Form(..., ge=0),
    stock_minimo: float = Form(0, ge=0),
    puntos_canje: int = Form(0, ge=0),             # > 0 = participa del programa de premios
    unidad_medida: str = Form("unid"),             # 'unid' o 'kg'
    imagen_url: str = Form(""),                    # enlace (URL) en texto plano
):
    """Alta de producto guardando la URL de la imagen directamente en Supabase."""
    db = get_db()
    precio_venta = _precio_final(precio_costo, margen_ganancia, usa_precio_manual, precio)
    codigo = codigo.strip() or _siguiente_codigo(db)
    _codigo_libre(db, codigo)
    cat = _resolver_categoria(db, categoria)
    url = _validar_imagen_url(imagen_url)
    res = db.table("productos").insert({
        "comercio_id": COMERCIO_ID,
        "codigo": codigo,
        "nombre": nombre.strip(),
        "categoria_id": cat["id"],
        "precio_costo": precio_costo,
        "margen_ganancia": margen_ganancia,
        "precio_venta": precio_venta,
        "usa_precio_manual": usa_precio_manual,
        "stock_actual": stock,
        "stock_minimo": stock_minimo,
        "unidad_medida": "kg" if unidad_medida.strip().lower() in ("kg", "kilo", "kilos", "peso") else "unid",
        "imagen_url": url,
        "puntos_canje": puntos_canje,
    }).execute()
    creado = _producto_out(res.data[0], cat["nombre"])
    _registrar_auditoria(request, "ALTA_PRODUCTO", {
        "producto_id": creado["id"],
        "codigo": creado["codigo"],
        "nombre": creado["nombre"],
        "categoria": creado["categoria"],
        "precio_venta": creado["precio"],
        "stock_inicial": creado["stock"],
    })
    return creado


@app.put("/api/inventario/productos/{producto_id}", dependencies=[Depends(requiere_sesion_api)])
def editar_producto(
    producto_id: int,
    request: Request,
    nombre: str = Form(..., min_length=1),
    categoria: str = Form(..., min_length=1),
    codigo: str = Form(""),                        # vacío = se conserva el actual
    precio_costo: float = Form(0, ge=0),
    margen_ganancia: float = Form(0, ge=0),
    usa_precio_manual: bool = Form(True),
    precio: Optional[float] = Form(None),
    stock: float = Form(..., ge=0),
    stock_minimo: float = Form(0, ge=0),
    puntos_canje: int = Form(0, ge=0),
    unidad_medida: Optional[str] = Form(None),     # 'unid' o 'kg'
    eliminar_imagen: bool = Form(False),
    imagen_url: Optional[str] = Form(None),        # enlace (URL) en texto plano
):
    """Edición de producto guardando la URL de imagen en Supabase."""
    db = get_db()
    prod = _buscar_producto(db, producto_id)
    precio_venta = _precio_final(precio_costo, margen_ganancia, usa_precio_manual, precio)
    codigo = codigo.strip() or str(prod.get("codigo") or "")
    if codigo != str(prod.get("codigo") or ""):
        _codigo_libre(db, codigo, excluir_id=producto_id)
    cat = _resolver_categoria(db, categoria)
    payload = {"codigo": codigo, "nombre": nombre.strip(), "categoria_id": cat["id"],
               "precio_costo": precio_costo, "margen_ganancia": margen_ganancia,
               "precio_venta": precio_venta, "usa_precio_manual": usa_precio_manual,
               "stock_actual": stock, "stock_minimo": stock_minimo, "puntos_canje": puntos_canje}
    if unidad_medida is not None:
        payload["unidad_medida"] = "kg" if unidad_medida.strip().lower() in ("kg", "kilo", "kilos", "peso") else "unid" 
    if eliminar_imagen:
        payload["imagen_url"] = ""
    elif imagen_url is not None:
        payload["imagen_url"] = _validar_imagen_url(imagen_url)
    res = db.table("productos").update(payload).eq("id", producto_id).eq("comercio_id", COMERCIO_ID).execute()
    actualizado = _producto_out(res.data[0], cat["nombre"])
    campos_auditables = ("codigo", "nombre", "precio_costo", "margen_ganancia", "precio_venta", "stock_actual", "stock_minimo", "puntos_canje")
    cambios = {}
    for campo in campos_auditables:
        v_ant = prod.get(campo)
        v_nue = res.data[0].get(campo)
        val_antes = _num(v_ant) if isinstance(v_ant, (int, float)) else v_ant
        val_despues = _num(v_nue) if isinstance(v_nue, (int, float)) else v_nue
        if val_antes != val_despues:
            cambios[campo] = {"antes": val_antes, "despues": val_despues}
    _registrar_auditoria(request, "EDICION_PRODUCTO", {
        "producto_id": producto_id,
        "nombre": actualizado["nombre"],
        "cambios": cambios,
    })
    return actualizado


@app.delete("/api/inventario/productos/{producto_id}", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def eliminar_producto(producto_id: int, request: Request):
    db = get_db()
    prod = _buscar_producto(db, producto_id)
    _dar_de_baja(db, "productos", producto_id)
    _registrar_auditoria(request, "BAJA_PRODUCTO", {
        "producto_id": producto_id,
        "nombre": prod["nombre"],
        "codigo": prod.get("codigo") or "",
    })
    return {"ok": True}


@app.post("/api/inventario/productos/{producto_id}/reactivar", dependencies=[Depends(requiere_sesion_api)])
def reactivar_producto(producto_id: int, request: Request):
    db = get_db()
    prod = _buscar_producto(db, producto_id)
    _reactivar(db, "productos", producto_id)
    if prod.get("categoria_id"):
        _reactivar(db, "categorias", prod["categoria_id"])
    _registrar_auditoria(request, "REACTIVACION_PRODUCTO", {
        "producto_id": producto_id,
        "nombre": prod["nombre"],
        "codigo": prod.get("codigo") or "",
    })
    return {"ok": True}


# ---------------------------------------------------------------------------
# Inventario: ajuste de stock y monitor de stock crítico
# ---------------------------------------------------------------------------
class AjusteStockIn(BaseModel):
    modo: Literal["establecer", "sumar", "restar"]
    cantidad: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)


class StockMinimoIn(BaseModel):
    stock_minimo: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)


@app.post("/api/inventario/productos/{producto_id}/ajuste-stock", dependencies=[Depends(requiere_sesion_api)])
def ajustar_stock(producto_id: int, data: AjusteStockIn, request: Request):
    """Conteo físico exacto (establecer), ingreso de mercadería (sumar) o salida/merma (restar)."""
    db = get_db()
    prod = _buscar_producto(db, producto_id)
    actual = float(prod.get("stock_actual") or 0)
    if data.modo == "establecer":
        nuevo = data.cantidad
    elif data.modo == "sumar":
        if data.cantidad <= 0:
            raise HTTPException(400, "La cantidad ingresada debe ser mayor a 0")
        nuevo = actual + data.cantidad
    else:
        if data.cantidad <= 0:
            raise HTTPException(400, "La cantidad a descontar debe ser mayor a 0")
        nuevo = actual - data.cantidad
    nuevo = round(nuevo, 3)
    if nuevo < 0:
        raise HTTPException(400, f"El stock no puede quedar negativo (actual: {fmt_cantidad(actual)})")
    res = db.table("productos").update({"stock_actual": nuevo}).eq("id", producto_id).eq("comercio_id", COMERCIO_ID).execute()
    _registrar_auditoria(request, "AJUSTE_STOCK", {
        "producto_id": producto_id,
        "nombre": res.data[0]["nombre"],
        "modo": data.modo,
        "cantidad_operacion": _num(data.cantidad),
        "stock_anterior": _num(actual),
        "stock_nuevo": _num(nuevo),
    })
    return {"id": producto_id, "stock_anterior": _num(actual), "stock": _num(nuevo), "nombre": res.data[0]["nombre"]}


@app.put("/api/inventario/productos/{producto_id}/stock-minimo", dependencies=[Depends(requiere_sesion_api)])
def actualizar_stock_minimo(producto_id: int, data: StockMinimoIn, request: Request):
    db = get_db()
    prod = _buscar_producto(db, producto_id)
    res = (db.table("productos").update({"stock_minimo": data.stock_minimo})
           .eq("id", producto_id).eq("comercio_id", COMERCIO_ID).execute())
    _registrar_auditoria(request, "CAMBIO_STOCK_MINIMO", {
        "producto_id": producto_id,
        "nombre": prod["nombre"],
        "stock_minimo_anterior": _num(prod.get("stock_minimo")),
        "stock_minimo_nuevo": _num(res.data[0]["stock_minimo"]),
    })
    return {"id": producto_id, "stock_minimo": _num(res.data[0]["stock_minimo"])}


@app.get("/api/inventario/stock-critico", dependencies=[Depends(requiere_sesion_api)])
def stock_critico():
    """Productos con stock <= stock mínimo, de mayor a menor faltante."""
    db = get_db()
    cats = {c["id"]: c["nombre"] for c in _categorias_db(db)}
    filas = db.table("productos").select("*").eq("comercio_id", COMERCIO_ID).eq("activo", True).execute().data or []
    criticos = []
    for p in filas:
        actual, minimo = float(p.get("stock_actual") or 0), float(p.get("stock_minimo") or 0)
        if actual <= minimo:
            criticos.append({**_producto_out(p, cats.get(p.get("categoria_id"), SIN_CATEGORIA)),
                             "deficit": _num(round(minimo - actual, 3))})
    criticos.sort(key=lambda x: x["deficit"], reverse=True)
    return criticos


# ---------------------------------------------------------------------------
# Inventario: actualización masiva de precios (misma regla que app.py)
# ---------------------------------------------------------------------------
class PreciosMasivosIn(BaseModel):
    categoria_id: Optional[int] = None            # None = todas las categorías
    porcentaje: float = Field(ge=-99, le=1000, allow_inf_nan=False)
    redondeo: Literal[0, 10, 50, 100, 500, 1000] = 0   # 0 = sin redondear
    aplicar: bool = False                         # False = solo simular


@app.post("/api/inventario/precios-masivos", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def precios_masivos(data: PreciosMasivosIn):
    """Aumento/descuento porcentual. Simula (aplicar=false) o aplica (aplicar=true).

    Con precio manual se ajusta el precio de venta; con margen se recalcula desde el costo
    ajustado. El costo siempre se ajusta por el mismo porcentaje.
    """
    if data.porcentaje == 0:
        raise HTTPException(400, "El porcentaje de ajuste no puede ser 0")
    db = get_db()
    consulta = db.table("productos").select("*").eq("comercio_id", COMERCIO_ID).eq("activo", True)
    if data.categoria_id is not None:
        consulta = consulta.eq("categoria_id", data.categoria_id)
    filas = consulta.order("nombre").execute().data or []
    if not filas:
        raise HTTPException(404, "No se encontraron productos para aplicar el ajuste")

    factor = 1 + data.porcentaje / 100.0
    cambios, vista = [], []
    for p in filas:
        actual = float(p.get("precio_venta") or 0)
        nuevo_costo = float(p.get("precio_costo") or 0) * factor
        margen = float(p.get("margen_ganancia") or 0)
        manual = bool(p.get("usa_precio_manual"))
        nuevo = actual * factor if manual else nuevo_costo * (1 + margen / 100.0)
        nuevo = round(nuevo / data.redondeo) * data.redondeo if data.redondeo else round(nuevo, 2)
        cambios.append({"id": p["id"], "precio_costo": round(nuevo_costo, 2), "precio_venta": nuevo})
        vista.append({"id": p["id"], "nombre": p["nombre"], "precio_actual": _num(actual),
                      "precio_nuevo": _num(nuevo), "diferencia": _num(round(nuevo - actual, 2))})

    actualizados = 0
    if data.aplicar:
        lotes_upsert = []
        for p, c in zip(filas, cambios):
            copia = dict(p)
            copia["precio_costo"] = c["precio_costo"]
            copia["precio_venta"] = c["precio_venta"]
            copia["comercio_id"] = COMERCIO_ID
            lotes_upsert.append(copia)
        for i in range(0, len(lotes_upsert), 100):
            bloque = lotes_upsert[i:i + 100]
            res = db.table("productos").upsert(bloque).execute()
            actualizados += len(res.data or bloque)
    return {"aplicado": data.aplicar, "actualizados": actualizados, "total": len(vista), "productos": vista}


# ---------------------------------------------------------------------------
# Inventario: exportación a Excel y carga masiva (Excel / CSV)
# ---------------------------------------------------------------------------
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
COLUMNAS_EXPORTACION = ["Código", "Nombre", "Categoría", "Precio Costo", "Precio Venta",
                        "Stock", "Stock Mínimo", "Puntos de Canje"]
COLUMNAS_PLANTILLA = ["codigo", "nombre", "categoria", "precio_costo", "precio_venta",
                      "stock", "stock_minimo", "puntos_canje"]


def _xlsx_bytes(hoja: str, encabezados: List[str], filas: List[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = hoja
    ws.append(encabezados)
    for celda in ws[1]:
        celda.font = Font(bold=True)
    for fila in filas:
        ws.append(fila)
    for col in ws.columns:
        ancho = max(len(str(c.value)) if c.value is not None else 0 for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(ancho + 2, 10), 45)
    salida = io.BytesIO()
    wb.save(salida)
    return salida.getvalue()


@app.get("/api/inventario/exportar-excel", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def exportar_excel(categoria_id: Optional[int] = None, q: str = ""):
    """Exporta el inventario (respeta el filtro de categoría y la búsqueda) a .xlsx."""
    db = get_db()
    cats = {c["id"]: c["nombre"] for c in _categorias_db(db)}
    consulta = db.table("productos").select("*").eq("comercio_id", COMERCIO_ID).eq("activo", True)
    if categoria_id is not None:
        consulta = consulta.eq("categoria_id", categoria_id)
    q = q.strip().lower()
    filas = []
    for p in consulta.order("nombre").execute().data or []:
        if q and q not in str(p.get("nombre", "")).lower() and q not in str(p.get("codigo", "")).lower():
            continue
        filas.append([
            str(p.get("codigo") or ""), str(p.get("nombre") or ""),
            cats.get(p.get("categoria_id"), SIN_CATEGORIA),
            round(float(p.get("precio_costo") or 0), 2), round(float(p.get("precio_venta") or 0), 2),
            round(float(p.get("stock_actual") or 0), 2), round(float(p.get("stock_minimo") or 0), 2),
            int(p.get("puntos_canje") or 0),
        ])
    datos = _xlsx_bytes("Inventario", COLUMNAS_EXPORTACION, filas)
    nombre = f"inventario_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(datos, media_type=XLSX_MIME, headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@app.get("/api/inventario/plantilla-excel", dependencies=[Depends(requiere_sesion_api)])
def plantilla_excel():
    """Planilla modelo para la carga masiva (mismas columnas y ejemplos que el sistema original)."""
    ejemplos = [
        ["7791234567890", "Yerba Mate Clásica 500g", "Almacén", 1200.00, 1800.00, 25, 5, 0],
        ["7799876543210", "Gaseosa Cola 2.25L", "Bebidas", 1500.00, 2300.00, 18, 6, 150],
        ["7795555444433", "Galletitas Rellenas Vainilla 160g", "Golosinas", 650.00, 1050.00, 30, 10, 50],
    ]
    datos = _xlsx_bytes("Plantilla", COLUMNAS_PLANTILLA, ejemplos)
    return Response(datos, media_type=XLSX_MIME,
                    headers={"Content-Disposition": 'attachment; filename="plantilla_productos.xlsx"'})


# Alias de columnas aceptados (idénticos a models.importar_productos_masivo)
ALIAS_COLUMNAS = {
    "codigo": {"codigo", "sku", "barcode", "cod"},
    "nombre": {"nombre", "producto", "descripcion", "articulo"},
    "categoria": {"categoria", "categoria_nombre", "rubro"},
    "precio_costo": {"precio_costo", "costo", "costo_unitario"},
    "precio_venta": {"precio_venta", "venta", "precio"},
    "stock": {"stock", "stock_actual", "cantidad"},
    "stock_minimo": {"stock_minimo", "stock_min", "minimo"},
    "puntos_canje": {"puntos_canje", "puntos_de_canje", "puntos", "puntos_canje_premio"},
}


def _normalizar_columna(nombre) -> Optional[str]:
    n = str(nombre or "").strip().lower().replace(" ", "_")
    for a, b in (("ó", "o"), ("í", "i"), ("á", "a"), ("é", "e"), ("ú", "u")):
        n = n.replace(a, b)
    for destino, alias in ALIAS_COLUMNAS.items():
        if n in alias:
            return destino
    return None


def _leer_tabla(archivo: UploadFile) -> List[dict]:
    """Lee un .xlsx o .csv y devuelve filas como dict con las columnas ya normalizadas."""
    nombre = (archivo.filename or "").lower()
    contenido = archivo.file.read(MAX_UPLOAD_BYTES + 1)
    if not contenido:
        raise HTTPException(400, "El archivo está vacío")
    if len(contenido) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "El archivo supera el máximo de 15 MB")
    if nombre.endswith(".csv"):
        import csv
        texto = contenido.decode("utf-8-sig", errors="replace")
        try:
            dialecto = csv.Sniffer().sniff(texto[:2048], delimiters=",;\t")
        except csv.Error:
            dialecto = csv.excel
        filas = list(csv.reader(io.StringIO(texto), dialecto))
    elif nombre.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        try:
            ws = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True).active
            filas = [list(f) for f in ws.iter_rows(values_only=True)]
        except Exception:
            raise HTTPException(400, "No se pudo leer el Excel. Usá un archivo .xlsx válido")
    else:
        raise HTTPException(400, "Formato no soportado: subí un archivo .xlsx o .csv")
    if len(filas) < 2:
        raise HTTPException(400, "El archivo no tiene filas de datos")
    columnas = [_normalizar_columna(c) for c in filas[0]]
    if "codigo" not in columnas or "nombre" not in columnas:
        raise HTTPException(400, "Faltan columnas obligatorias: codigo y nombre")
    tabla = []
    for fila in filas[1:]:
        if all(c is None or str(c).strip() == "" for c in fila):
            continue
        tabla.append({col: fila[i] for i, col in enumerate(columnas) if col and i < len(fila)})
    return tabla


def _a_numero(valor, defecto: float = 0.0) -> float:
    if valor is None or str(valor).strip() == "":
        return defecto
    return float(str(valor).replace(",", ".")) if isinstance(valor, str) else float(valor)


@app.post("/api/inventario/importar", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def importar_productos(archivo: UploadFile = File(...)):
    """Carga masiva: crea productos nuevos o actualiza los que ya existen por código.

    Crea las categorías que falten. Devuelve un resumen con el detalle de errores por fila.
    """
    tabla = _leer_tabla(archivo)
    db = get_db()
    cat_map = {c["nombre"].strip().lower(): c["id"] for c in _categorias_db(db)}
    existentes = {str(p["codigo"]).strip(): p["id"] for p in
                  (db.table("productos").select("id, codigo").eq("comercio_id", COMERCIO_ID).execute().data or [])}
    de_baja = {p["id"] for p in
               (db.table("productos").select("id").eq("comercio_id", COMERCIO_ID).eq("activo", False).execute().data or [])}
    creados = actualizados = categorias_creadas = 0
    errores: List[str] = []

    for idx, fila in enumerate(tabla, start=2):
        crudo = fila.get("codigo")
        if crudo is None or str(crudo).strip() == "":
            errores.append(f"Fila {idx}: el código de barras / SKU está vacío.")
            continue
        codigo = str(int(crudo)) if isinstance(crudo, float) and crudo.is_integer() else str(crudo).strip()
        if codigo.endswith(".0"):
            codigo = codigo[:-2]
        nombre = str(fila.get("nombre") or "").strip()
        if not nombre:
            errores.append(f"Fila {idx} (código {codigo}): el nombre del producto está vacío.")
            continue
        try:
            costo = _a_numero(fila.get("precio_costo"))
            venta = _a_numero(fila.get("precio_venta"))
            stock = _a_numero(fila.get("stock"))
            minimo = _a_numero(fila.get("stock_minimo"))
            puntos = max(0, int(_a_numero(fila.get("puntos_canje"))))
        except (ValueError, TypeError):
            errores.append(f"Fila {idx} ('{nombre}'): hay un valor numérico inválido.")
            continue
        if min(costo, venta, stock, minimo) < 0:
            errores.append(f"Fila {idx} ('{nombre}'): los precios y el stock no pueden ser negativos.")
            continue

        cat_nombre = str(fila.get("categoria") or "").strip() or "General"
        cat_id = cat_map.get(cat_nombre.lower())
        if cat_id is None:
            cat = _resolver_categoria(db, cat_nombre)
            cat_id = cat_map[cat_nombre.lower()] = cat["id"]
            categorias_creadas += 1

        margen = round((venta - costo) / costo * 100.0, 2) if costo > 0 and venta >= costo else 0.0
        payload = {"nombre": nombre, "categoria_id": cat_id, "precio_costo": costo, "margen_ganancia": margen,
                   "precio_venta": venta, "usa_precio_manual": True, "stock_actual": stock,
                   "stock_minimo": minimo, "puntos_canje": puntos}
        try:
            if codigo in existentes:
                if existentes[codigo] in de_baja:  # un producto dado de baja que vuelve en la planilla se reactiva
                    payload = {**payload, "activo": True, "fecha_baja": None}
                db.table("productos").update(payload).eq("id", existentes[codigo]).eq("comercio_id", COMERCIO_ID).execute()
                actualizados += 1
            else:
                res = db.table("productos").insert({**payload, "comercio_id": COMERCIO_ID, "codigo": codigo,
                                                    "imagen_url": ""}).execute()
                existentes[codigo] = res.data[0]["id"]
                creados += 1
        except Exception as e:
            errores.append(f"Fila {idx} ('{nombre}', {codigo}): {getattr(e, 'message', e)}")
    return {"creados": creados, "actualizados": actualizados, "total": creados + actualizados,
            "categorias_creadas": categorias_creadas, "errores": errores}


# ---------------------------------------------------------------------------
# Categorías: pantalla y CRUD
# ---------------------------------------------------------------------------
class CategoriaIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=60)
    descripcion: str = Field(default="", max_length=200)


def _categoria_out(c: dict, cantidad_productos: int) -> dict:
    return {"id": c["id"], "nombre": c["nombre"],
            "descripcion": c.get("descripcion") or "", "productos": cantidad_productos}


def _productos_por_categoria(db: Client) -> dict:
    filas = db.table("productos").select("categoria_id").eq("comercio_id", COMERCIO_ID).eq("activo", True).execute().data or []
    conteo: dict = {}
    for f in filas:
        conteo[f["categoria_id"]] = conteo.get(f["categoria_id"], 0) + 1
    return conteo


def _buscar_categoria(db: Client, categoria_id: int) -> dict:
    cat = next((c for c in _categorias_db(db) if c["id"] == categoria_id), None)
    if not cat:
        raise HTTPException(404, "Categoría no encontrada")
    return cat


def _validar_nombre_unico(db: Client, nombre: str, excluir_id: Optional[int] = None) -> str:
    nombre = nombre.strip()
    if not nombre:
        raise HTTPException(400, "El nombre es obligatorio")
    for c in _categorias_db(db, con_bajas=True):
        if c["nombre"].lower() == nombre.lower() and c["id"] != excluir_id:
            if not c.get("activo", True):
                raise HTTPException(409, "Hay una categoría dada de baja con ese nombre: reactivala")
            raise HTTPException(409, "Ya existe una categoría con ese nombre")
    return nombre


@app.get("/categorias", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def categorias_page(request: Request):
    return templates.TemplateResponse(request, "categorias.html", {"config": config_publica()})


@app.get("/api/categorias", dependencies=[Depends(requiere_sesion_api)])
def listar_categorias():
    db = get_db()
    conteo = _productos_por_categoria(db)
    return [_categoria_out(c, conteo.get(c["id"], 0)) for c in _categorias_db(db)]


@app.post("/api/categorias", status_code=201, dependencies=[Depends(requiere_sesion_api)])
def crear_categoria(data: CategoriaIn):
    db = get_db()
    nombre = _validar_nombre_unico(db, data.nombre)
    res = db.table("categorias").insert(
        {"nombre": nombre, "descripcion": data.descripcion.strip(), "comercio_id": COMERCIO_ID}).execute()
    return _categoria_out(res.data[0], 0)


@app.put("/api/categorias/{categoria_id}", dependencies=[Depends(requiere_sesion_api)])
def editar_categoria(categoria_id: int, data: CategoriaIn):
    db = get_db()
    _buscar_categoria(db, categoria_id)
    nombre = _validar_nombre_unico(db, data.nombre, excluir_id=categoria_id)
    # Los productos apuntan a la categoría por id, así que renombrar no requiere tocarlos.
    res = (db.table("categorias").update({"nombre": nombre, "descripcion": data.descripcion.strip()})
           .eq("id", categoria_id).eq("comercio_id", COMERCIO_ID).execute())
    return _categoria_out(res.data[0], _productos_por_categoria(db).get(categoria_id, 0))


@app.delete("/api/categorias/{categoria_id}", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def eliminar_categoria(categoria_id: int):
    db = get_db()
    _buscar_categoria(db, categoria_id)
    if _productos_por_categoria(db).get(categoria_id):
        raise HTTPException(409, "No se puede dar de baja: tiene productos asociados")
    _dar_de_baja(db, "categorias", categoria_id)
    return {"ok": True}


@app.post("/api/categorias/{categoria_id}/reactivar", dependencies=[Depends(requiere_sesion_api)])
def reactivar_categoria(categoria_id: int):
    db = get_db()
    if not any(c["id"] == categoria_id for c in _categorias_db(db, con_bajas=True)):
        raise HTTPException(404, "Categoría no encontrada")
    _reactivar(db, "categorias", categoria_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Caja: Arqueo Diario, Caja Chica, Cierre Oficial (PDF / WhatsApp / SMTP)
# y compatibilidad con apertura/cierre de turno (tabla cierres_caja)
# ---------------------------------------------------------------------------
class AbrirCajaIn(BaseModel):
    monto_inicial: float = Field(ge=0, le=100_000_000, allow_inf_nan=False)


class CerrarCajaIn(BaseModel):
    monto_real: float = Field(ge=0, le=100_000_000, allow_inf_nan=False)


class MovimientoCajaIn(BaseModel):
    tipo: Literal["ingreso_extra", "egreso"]
    monto: float = Field(gt=0, le=100_000_000, allow_inf_nan=False)
    concepto: str = Field(min_length=1, max_length=200)


class CierreDiarioIn(BaseModel):
    fecha: Optional[str] = None
    monto_real: Optional[float] = Field(default=None, ge=0, le=100_000_000, allow_inf_nan=False)
    enviar_correo: bool = True


def _rango_dia_iso(fecha_str: str):
    """Devuelve (inicio_iso, fin_iso) con huso horario local (-03:00) para filtrar timestamptz."""
    try:
        datetime.strptime(fecha_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Formato de fecha inválido (usar YYYY-MM-DD)")
    return f"{fecha_str}T00:00:00-03:00", f"{fecha_str}T23:59:59.999999-03:00"


def _formatear_hora_local(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00")).astimezone(TZ_AR)
        return dt.strftime("%H:%M")
    except Exception:
        partes = str(iso_str).split(" ")
        return partes[1][:5] if len(partes) > 1 else str(iso_str)[:16]


def _smtp_esta_configurado(cfg: dict) -> bool:
    return bool(
        (cfg.get("smtp_servidor") or "").strip()
        and (cfg.get("smtp_usuario") or "").strip()
        and (cfg.get("smtp_password") or "").strip()
        and (cfg.get("email_destinatario") or "").strip()
    )


def _calcular_arqueo_dia(db: Client, fecha_str: Optional[str] = None) -> dict:
    """Consolida ventas, ganancias, caja chica, cobros de Cuenta Corriente y tickets de una fecha."""
    from tickets import armar_mensaje_whatsapp_cierre, armar_url_whatsapp

    if not fecha_str:
        fecha_str = _ahora_local().strftime("%Y-%m-%d")
    inicio_iso, fin_iso = _rango_dia_iso(fecha_str)
    fecha_fmt = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m/%Y")

    # 1. Ventas de la fecha
    ventas_data = (
        db.table("ventas")
        .select("*, clientes(nombre)")
        .eq("comercio_id", COMERCIO_ID)
        .is_("archivado_en", "null")
        .gte("fecha_hora", inicio_iso)
        .lte("fecha_hora", fin_iso)
        .order("id", desc=True)
        .execute()
        .data or []
    )

    # 2. Detalle de cada venta (para calcular costo/ganancia y armar el desglose de cada ticket)
    venta_ids = [v["id"] for v in ventas_data]
    detalles_por_venta: dict = {vid: [] for vid in venta_ids}
    costo_total = 0.0
    if venta_ids:
        renglones = (
            db.table("detalle_ventas")
            .select("*, productos(codigo, nombre)")
            .in_("venta_id", venta_ids)
            .execute()
            .data or []
        )
        for d in renglones:
            vid = d.get("venta_id")
            cant = float(d.get("cantidad") or 0.0)
            if cant <= 0:
                continue
            p_unit = float(d.get("precio_unitario") or 0.0)
            costo_u = float(d.get("costo_unitario") or 0.0)
            costo_total += round(cant * costo_u, 2)
            prod = d.get("productos")
            if isinstance(prod, list):
                prod = prod[0] if prod else None
            codigo_p = prod.get("codigo") if prod else "-"
            nombre_p = prod.get("nombre") if prod else "Artículo"
            if p_unit == 0 and "(Canje)" not in nombre_p:
                nombre_p = f"{nombre_p} (Canje)"
            if vid in detalles_por_venta:
                detalles_por_venta[vid].append({
                    "detalle_id": d.get("id"),
                    "producto_id": d.get("producto_id"),
                    "codigo": codigo_p or "-",
                    "nombre": nombre_p,
                    "cantidad": _num(cant),
                    "precio_unitario": _num(round(p_unit, 2)),
                    "subtotal": _num(round(cant * p_unit, 2)),
                })

    total_vendido = 0.0
    desglose_dict: dict = {}
    ventas_list = []

    for v in ventas_data:
        monto = float(v.get("total_venta") or 0.0)
        fp = (v.get("forma_pago") or "Efectivo").strip()
        total_vendido += monto

        if fp not in desglose_dict:
            desglose_dict[fp] = {"forma_pago": fp, "total": 0.0, "operaciones": 0}
        desglose_dict[fp]["total"] = round(desglose_dict[fp]["total"] + monto, 2)
        desglose_dict[fp]["operaciones"] += 1

        cli = v.get("clientes")
        if isinstance(cli, list):
            cli = cli[0] if cli else None
        cli_nom = cli.get("nombre") if cli else None

        ventas_list.append({
            "id": v["id"],
            "fecha_hora": str(v.get("fecha_hora") or ""),
            "hora": _formatear_hora_local(v.get("fecha_hora") or ""),
            "total_venta": _num(round(monto, 2)),
            "forma_pago": fp,
            "cliente_nombre": cli_nom or "Consumidor Final",
            "items": detalles_por_venta.get(v["id"], []),
        })

    ganancia_estimada = max(0.0, round(total_vendido - costo_total, 2))

    # 3. Movimientos de Caja Chica (manuales) y Cobros de Cuenta Corriente en Efectivo
    movs_raw = (
        db.table("movimientos_caja")
        .select("*")
        .eq("comercio_id", COMERCIO_ID)
        .is_("archivado_en", "null")
        .gte("fecha", inicio_iso)
        .lte("fecha", fin_iso)
        .order("id", desc=True)
        .execute()
        .data or []
    )

    abonos_cta_cte = (
        db.table("movimientos_cuenta")
        .select("*")
        .eq("comercio_id", COMERCIO_ID)
        .eq("tipo", "PAGO")
        .is_("archivado_en", "null")
        .gte("fecha_hora", inicio_iso)
        .lte("fecha_hora", fin_iso)
        .execute()
        .data or []
    )
    cobros_cta_cte_efectivo = sum(
        float(m.get("monto") or 0.0)
        for m in abonos_cta_cte
        if "(efectivo)" in str(m.get("descripcion") or "").lower()
    )

    ingresos_extras = 0.0
    egresos = 0.0
    movimientos_caja_out = []
    for m in movs_raw:
        concepto_str = str(m.get("concepto") or "").strip()
        monto_m = float(m.get("monto") or 0.0)
        tipo_m = m.get("tipo") or "egreso"
        # Los cobros de Cuenta Corriente ya se contabilizan en cobros_cta_cte_efectivo;
        # no se suman a ingresos_extras para nunca duplicar el saldo en caja.
        es_cobro_cc_auto = concepto_str.lower().startswith("cobro cuenta corriente")
        if tipo_m == "ingreso_extra":
            if not es_cobro_cc_auto:
                ingresos_extras += monto_m
        elif tipo_m == "egreso":
            egresos += monto_m

        if not es_cobro_cc_auto:
            movimientos_caja_out.append({
                "id": m["id"],
                "hora": _formatear_hora_local(m.get("fecha") or ""),
                "fecha": m.get("fecha") or "",
                "tipo": tipo_m,
                "concepto": concepto_str,
                "monto": _num(round(monto_m, 2)),
                "usuario_nombre": m.get("usuario_nombre") or "Dueño",
            })

    import re
    ventas_efectivo = 0.0
    for v in ventas_data:
        monto_v = float(v.get("total_venta") or 0.0)
        fp_v = (v.get("forma_pago") or "Efectivo").strip()
        if fp_v.startswith("Mixto"):
            m_ef_match = re.search(r"Ef:\s*\$?\s*([\d\.,]+)", fp_v)
            if m_ef_match:
                clean_num = m_ef_match.group(1).replace(".", "").replace(",", ".")
                try:
                    ventas_efectivo += float(clean_num)
                except ValueError:
                    pass
        elif fp_v.lower() == "efectivo":
            ventas_efectivo += monto_v

    # 4. Saldo Inicial de Caja (Apertura del turno activo o de la fecha consultada)
    caja_actual = _caja_abierta(db)
    cierres_dia = (
        db.table("cierres_caja")
        .select("*")
        .eq("comercio_id", COMERCIO_ID)
        .is_("archivado_en", "null")
        .gte("fecha_apertura", inicio_iso)
        .lte("fecha_apertura", fin_iso)
        .order("id", desc=True)
        .execute()
        .data or []
    )
    es_hoy = (fecha_str == _ahora_local().strftime("%Y-%m-%d"))
    caja_ref = caja_actual if (es_hoy and caja_actual) else (cierres_dia[0] if cierres_dia else None)
    saldo_inicial = float(caja_ref.get("monto_inicial") or 0.0) if caja_ref else 0.0
    caja_cerrada_ref = (caja_ref.get("estado") == "cerrada") if caja_ref else False
    monto_real_prev = caja_ref.get("monto_real") if (caja_ref and caja_cerrada_ref) else None
    diferencia_prev = caja_ref.get("diferencia") if (caja_ref and caja_cerrada_ref) else None

    total_efectivo_esperado = round(
        saldo_inicial + ventas_efectivo + cobros_cta_cte_efectivo + ingresos_extras - egresos, 2
    )

    desglose_lista = [
        {
            "forma_pago": d["forma_pago"],
            "total": _num(round(d["total"], 2)),
            "operaciones": d["operaciones"],
        }
        for d in desglose_dict.values()
    ]

    cfg = cargar_config()
    resumen = {
        "fecha": fecha_str,
        "fecha_formateada": fecha_fmt,
        "caja_abierta": bool(caja_actual),
        "saldo_inicial": _num(round(saldo_inicial, 2)),
        "apertura_hora": _formatear_hora_local(caja_ref.get("fecha_apertura") or "") if caja_ref else "",
        "monto_real": _num(round(float(monto_real_prev), 2)) if monto_real_prev is not None else None,
        "diferencia": _num(round(float(diferencia_prev), 2)) if diferencia_prev is not None else None,
        "total_vendido": _num(round(total_vendido, 2)),
        "ganancia_estimada": _num(ganancia_estimada),
        "total_operaciones": len(ventas_data),
        "ventas_efectivo": _num(round(ventas_efectivo, 2)),
        "cobros_cta_cte_efectivo": _num(round(cobros_cta_cte_efectivo, 2)),
        "ingresos_extras": _num(round(ingresos_extras, 2)),
        "egresos": _num(round(egresos, 2)),
        "total_efectivo_esperado": _num(total_efectivo_esperado),
        "desglose_por_forma_pago": desglose_lista,
        "ventas": ventas_list,
        "movimientos_caja": movimientos_caja_out,
        "smtp_configurado": _smtp_esta_configurado(cfg),
        "email_destinatario": (cfg.get("email_destinatario") or "").strip(),
    }
    msg_wsp = armar_mensaje_whatsapp_cierre(
        resumen=resumen,
        fecha_str=fecha_fmt,
        nombre_negocio=cfg.get("nombre_comercio", "Mi Comercio"),
    )
    resumen["whatsapp_url"] = armar_url_whatsapp(None, msg_wsp)
    return resumen


def _caja_abierta(db: Client) -> Optional[dict]:
    filas = (db.table("cierres_caja").select("*").eq("comercio_id", COMERCIO_ID)
             .is_("archivado_en", "null").eq("estado", "abierta").limit(1).execute().data or [])
    return filas[0] if filas else None


def _resumen_caja(db: Client, caja: dict) -> dict:
    """Resumen del turno. Efectivo esperado = fondo + ventas en efectivo + ingresos extra - egresos."""
    desde = caja["fecha_apertura"]
    ventas = (db.table("ventas").select("total_venta, forma_pago").eq("comercio_id", COMERCIO_ID)
              .is_("archivado_en", "null").gte("fecha_hora", desde).execute().data or [])
    movs = (db.table("movimientos_caja").select("tipo, monto").eq("comercio_id", COMERCIO_ID)
            .is_("archivado_en", "null").gte("fecha", desde).execute().data or [])
    total_ingresado = sum(float(v["total_venta"] or 0) for v in ventas)
    efectivo = sum(float(v["total_venta"] or 0) for v in ventas
                   if (v.get("forma_pago") or "").strip().lower() == "efectivo")
    extra = sum(float(m["monto"]) if m["tipo"] == "ingreso_extra" else -float(m["monto"]) for m in movs)
    inicial = float(caja["monto_inicial"])
    return {
        "abierta": True,
        "apertura": desde,
        "monto_inicial": _num(inicial),
        "cantidad_ventas": len(ventas),
        "total_ingresado": _num(round(total_ingresado, 2)),
        "total_efectivo": _num(round(efectivo, 2)),
        "total_esperado": _num(round(inicial + efectivo + extra, 2)),
    }


@app.get("/caja", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def caja_page(request: Request):
    return templates.TemplateResponse(request, "caja.html", {"config": config_publica()})


@app.get("/api/caja/arqueo", dependencies=[Depends(requiere_sesion_api)])
def obtener_arqueo_api(fecha: Optional[str] = None):
    """Devuelve el Arqueo de Caja y Control Diario para la fecha indicada (por defecto hoy)."""
    return _calcular_arqueo_dia(get_db(), fecha)


@app.post("/api/caja/movimientos", status_code=201, dependencies=[Depends(requiere_sesion_api)])
def registrar_movimiento_caja_api(data: MovimientoCajaIn, request: Request):
    """Registra un ingreso extra o egreso manual en la caja chica."""
    concepto = data.concepto.strip()
    if not concepto:
        raise HTTPException(400, "El concepto o motivo es obligatorio")
    info = _info_sesion(request)
    res = get_db().table("movimientos_caja").insert({
        "comercio_id": COMERCIO_ID,
        "tipo": data.tipo,
        "monto": round(data.monto, 2),
        "concepto": concepto,
        "usuario_nombre": info.get("usuario", ""),
    }).execute()
    return {"ok": True, "id": res.data[0]["id"] if res.data else None}


@app.get("/api/caja/cierre.pdf", dependencies=[Depends(requiere_sesion_api)])
def descargar_pdf_cierre_api(fecha: Optional[str] = None):
    """Genera y descarga el reporte PDF oficial del Cierre de Caja Diario."""
    from tickets import generar_pdf_cierre_caja
    fecha_uso = fecha or _ahora_local().strftime("%Y-%m-%d")
    resumen = _calcular_arqueo_dia(get_db(), fecha_uso)
    cfg = cargar_config()
    pdf_bytes = generar_pdf_cierre_caja(
        fecha_str=resumen["fecha_formateada"],
        resumen=resumen,
        nombre_negocio=cfg.get("nombre_comercio", "Mi Comercio"),
    )
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Cierre_Caja_{fecha_uso}.pdf"'},
    )


@app.post("/api/caja/cierre-diario", dependencies=[Depends(requiere_sesion_api)])
def ejecutar_cierre_diario_api(data: CierreDiarioIn):
    """Procesa el Cierre Oficial de Caja Diario y, si el correo SMTP está configurado, envía el PDF."""
    from tickets import enviar_email_cierre, generar_pdf_cierre_caja
    db = get_db()
    fecha_uso = data.fecha or _ahora_local().strftime("%Y-%m-%d")
    resumen = _calcular_arqueo_dia(db, fecha_uso)

    esperado_cierre = float(resumen.get("total_efectivo_esperado") or 0.0)
    monto_real_cierre = round(float(data.monto_real), 2) if data.monto_real is not None else esperado_cierre
    diferencia_cierre = round(monto_real_cierre - esperado_cierre, 2)
    resumen["monto_real"] = _num(monto_real_cierre)
    resumen["diferencia"] = _num(diferencia_cierre)

    caja_act = _caja_abierta(db)
    if caja_act:
        (db.table("cierres_caja").update({
            "estado": "cerrada",
            "fecha_cierre": datetime.now(timezone.utc).isoformat(),
            "total_esperado": esperado_cierre,
            "monto_real": monto_real_cierre,
            "diferencia": diferencia_cierre,
        }).eq("id", caja_act["id"]).eq("estado", "abierta").execute())
        resumen["caja_abierta"] = False

    cfg = cargar_config()
    smtp_ok = _smtp_esta_configurado(cfg)
    email_enviado = False
    email_mensaje = ""

    if data.enviar_correo and smtp_ok:
        pdf_bytes = generar_pdf_cierre_caja(
            fecha_str=resumen["fecha_formateada"],
            resumen=resumen,
            nombre_negocio=cfg.get("nombre_comercio", "Mi Comercio"),
        )
        email_enviado, email_mensaje = enviar_email_cierre(
            destinatario=cfg.get("email_destinatario", ""),
            pdf_bytes=pdf_bytes,
            fecha_str=resumen["fecha_formateada"],
            resumen=resumen,
            config_smtp=cfg,
        )

    try:
        from backup_drive import ejecutar_backup_datos
        threading.Thread(
            target=ejecutar_backup_datos,
            args=(APP_DIR, cfg, False, f"Cierre de Caja Diario ({fecha_uso})"),
            daemon=True,
        ).start()
    except Exception:
        pass

    return {
        "ok": True,
        "fecha": fecha_uso,
        "fecha_formateada": resumen["fecha_formateada"],
        "pdf_url": f"/api/caja/cierre.pdf?fecha={fecha_uso}",
        "whatsapp_url": resumen["whatsapp_url"],
        "smtp_configurado": smtp_ok,
        "email_enviado": email_enviado,
        "email_mensaje": email_mensaje,
        "total_esperado": esperado_cierre,
        "monto_real": monto_real_cierre,
        "diferencia": diferencia_cierre,
    }


@app.post("/api/caja/enviar-email", dependencies=[Depends(requiere_sesion_api)])
def reenviar_email_cierre_api(data: CierreDiarioIn):
    """Envía o reenvía manualmente por correo SMTP el PDF del cierre diario."""
    from tickets import enviar_email_cierre, generar_pdf_cierre_caja
    cfg = cargar_config()
    if not _smtp_esta_configurado(cfg):
        raise HTTPException(400, "Configurá primero los datos de correo SMTP en Configuración.")
    fecha_uso = data.fecha or _ahora_local().strftime("%Y-%m-%d")
    resumen = _calcular_arqueo_dia(get_db(), fecha_uso)
    pdf_bytes = generar_pdf_cierre_caja(
        fecha_str=resumen["fecha_formateada"],
        resumen=resumen,
        nombre_negocio=cfg.get("nombre_comercio", "Mi Comercio"),
    )
    exito, mensaje = enviar_email_cierre(
        destinatario=cfg.get("email_destinatario", ""),
        pdf_bytes=pdf_bytes,
        fecha_str=resumen["fecha_formateada"],
        resumen=resumen,
        config_smtp=cfg,
    )
    if not exito:
        raise HTTPException(400, mensaje)
    return {"ok": True, "mensaje": mensaje}


@app.get("/api/caja/estado", dependencies=[Depends(requiere_sesion_api)])
def estado_caja():
    db = get_db()
    caja = _caja_abierta(db)
    cierres = (db.table("cierres_caja").select("*").eq("comercio_id", COMERCIO_ID)
               .is_("archivado_en", "null").eq("estado", "cerrada").order("fecha_cierre", desc=True).limit(5).execute().data or [])
    historial = [{"id": c["id"], "fecha": c["fecha_cierre"], "monto_inicial": _num(c["monto_inicial"]),
                  "total_esperado": _num(c["total_esperado"]), "monto_real": _num(c["monto_real"]),
                  "diferencia": _num(c["diferencia"])} for c in cierres]
    estado = _resumen_caja(db, caja) if caja else {"abierta": False}
    return {**estado, "historial": historial}


@app.post("/api/caja/abrir", dependencies=[Depends(requiere_sesion_api)])
def abrir_caja(data: AbrirCajaIn):
    db = get_db()
    if _caja_abierta(db):
        raise HTTPException(409, "La caja ya está abierta")
    try:
        res = db.table("cierres_caja").insert({
            "comercio_id": COMERCIO_ID, "estado": "abierta", "monto_inicial": data.monto_inicial,
        }).execute()
    except APIError as e:
        if getattr(e, "code", None) == "23505":  # índice único: otra apertura simultánea
            raise HTTPException(409, "La caja ya está abierta")
        raise
    return _resumen_caja(db, res.data[0])


@app.post("/api/caja/cerrar", dependencies=[Depends(requiere_sesion_api)])
def cerrar_caja(data: CerrarCajaIn):
    db = get_db()
    caja = _caja_abierta(db)
    if not caja:
        raise HTTPException(409, "La caja ya está cerrada")
    esperado = _resumen_caja(db, caja)["total_esperado"]
    diferencia = round(data.monto_real - esperado, 2)  # + sobrante / - faltante
    res = (db.table("cierres_caja").update({
        "estado": "cerrada", "fecha_cierre": datetime.now(timezone.utc).isoformat(),
        "total_esperado": esperado, "monto_real": data.monto_real, "diferencia": diferencia,
    }).eq("id", caja["id"]).eq("estado", "abierta").execute())
    if not res.data:
        raise HTTPException(409, "La caja ya está cerrada")
    c = res.data[0]
    return {"id": c["id"], "fecha": c["fecha_cierre"], "monto_inicial": _num(c["monto_inicial"]),
            "total_esperado": _num(c["total_esperado"]), "monto_real": _num(c["monto_real"]),
            "diferencia": _num(c["diferencia"])}


# ---------------------------------------------------------------------------
# Configuración del negocio (persistida en Supabase: tabla configuracion_negocio)
# ---------------------------------------------------------------------------
class ConfigIn(BaseModel):
    nombre_comercio: str = Field(min_length=1, max_length=60)
    telefono: str = Field(default="", max_length=20)
    moneda: str = Field(min_length=1, max_length=5)
    color_primario: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    logo_imagen: Optional[str] = Field(default=None, max_length=2048)
    qr_imagen: Optional[str] = Field(default=None, max_length=2048)
    titular_cuenta: Optional[str] = Field(default=None, max_length=80)
    banco: Optional[str] = Field(default=None, max_length=60)
    alias: Optional[str] = Field(default=None, max_length=60)
    cbu: Optional[str] = Field(default=None, max_length=30)
    direccion: Optional[str] = Field(default=None, max_length=120)
    pesos_por_punto: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    smtp_servidor: Optional[str] = Field(default=None, max_length=120)
    smtp_puerto: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_usuario: Optional[str] = Field(default=None, max_length=120)
    smtp_password: Optional[str] = Field(default=None, max_length=200)
    email_destinatario: Optional[str] = Field(default=None, max_length=120)
    modo_bloqueo: Optional[str] = Field(default="nunca", max_length=20)
    tiempo_inactividad_min: Optional[int] = Field(default=15, ge=1, le=1440)
    auto_backup_activo: Optional[bool] = Field(default=None)
    modo_wifi_backup: Optional[str] = Field(default=None, max_length=40)
    wifi_seguro_ssid: Optional[str] = Field(default=None, max_length=120)
    carpeta_sincronizacion_drive: Optional[str] = Field(default=None, max_length=500)
    google_drive_webhook_url: Optional[str] = Field(default=None, max_length=2048)


class ImagenUrlIn(BaseModel):
    url: str = Field(default="", max_length=2048)
    logo_imagen: Optional[str] = Field(default=None, max_length=2048)
    qr_imagen: Optional[str] = Field(default=None, max_length=2048)


class CambiarClaveIn(BaseModel):
    pin_actual: str = Field(max_length=20)
    pin_nuevo: str = Field(max_length=20)


@app.get("/configuracion", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def configuracion_page(request: Request):
    return templates.TemplateResponse(request, "configuracion.html", {
        "config": config_publica(incluir_mascara_smtp=True),
        "debe_cambiar_pin": bool(_info_sesion(request).get("debe_cambiar_pin")),
    })


@app.get("/api/config", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
async def obtener_config():
    return config_publica(incluir_mascara_smtp=True)


@app.post("/api/config", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def actualizar_config(data: ConfigIn):
    telefono = re.sub(r"\D", "", data.telefono)  # solo dígitos (formato wa.me)
    nueva = {
        **cargar_config(),
        "nombre_comercio": data.nombre_comercio.strip(),
        "telefono": telefono,
        "moneda": data.moneda.strip(),
        "color_primario": data.color_primario.upper(),
    }
    for campo in (
        "titular_cuenta", "banco", "alias", "cbu", "direccion",
        "smtp_servidor", "smtp_usuario", "email_destinatario",
        "modo_wifi_backup", "wifi_seguro_ssid",
        "carpeta_sincronizacion_drive", "google_drive_webhook_url",
    ):
        valor = getattr(data, campo)
        if valor is not None:
            nueva[campo] = valor.strip()
    if data.pesos_por_punto is not None:
        nueva["pesos_por_punto"] = float(data.pesos_por_punto)
    if data.auto_backup_activo is not None:
        nueva["auto_backup_activo"] = bool(data.auto_backup_activo)
    if data.logo_imagen is not None:
        nueva["logo_imagen"] = _validar_imagen_url(data.logo_imagen)
    if data.qr_imagen is not None:
        nueva["qr_imagen"] = _validar_imagen_url(data.qr_imagen)
    if data.smtp_password and data.smtp_password.strip():
        nueva["smtp_password"] = data.smtp_password.strip()
    if data.modo_bloqueo in ("nunca", "inactividad", "minimizar"):
        nueva["modo_bloqueo"] = data.modo_bloqueo
    if data.tiempo_inactividad_min is not None:
        nueva["tiempo_inactividad_min"] = int(data.tiempo_inactividad_min)
    if data.smtp_puerto is not None:
        nueva["smtp_puerto"] = int(data.smtp_puerto)
    guardar_config(nueva)
    return config_publica(incluir_mascara_smtp=True)


@app.post("/api/config/logo", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def subir_logo_negocio(data: ImagenUrlIn):
    """Guarda el enlace (URL) del logo del comercio en Supabase."""
    url = _validar_imagen_url(data.url or data.logo_imagen or "")
    if not url:
        raise HTTPException(400, "Ingresá la URL de la imagen del logo.")
    config = cargar_config()
    config["logo_imagen"] = url
    guardar_config(config)
    return config_publica(incluir_mascara_smtp=True)


@app.delete("/api/config/logo", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def quitar_logo_negocio():
    config = cargar_config()
    config["logo_imagen"] = ""
    guardar_config(config)
    return config_publica(incluir_mascara_smtp=True)


@app.post("/api/config/qr", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def subir_qr_pago(data: ImagenUrlIn):
    """Guarda el enlace (URL) de la imagen del QR de cobro en Supabase."""
    url = _validar_imagen_url(data.url or data.qr_imagen or "")
    if not url:
        raise HTTPException(400, "Ingresá la URL de la imagen del código QR.")
    config = cargar_config()
    config["qr_imagen"] = url
    guardar_config(config)
    return config_publica(incluir_mascara_smtp=True)


@app.delete("/api/config/qr", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def quitar_qr_pago():
    config = cargar_config()
    config["qr_imagen"] = ""
    guardar_config(config)
    return config_publica(incluir_mascara_smtp=True)

# ---------------------------------------------------------------------------
# Subida de imágenes locales (Modo Offline / Portable)
# ---------------------------------------------------------------------------
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/archivos/subir-imagen", dependencies=[Depends(requiere_sesion_api)])
async def subir_imagen_archivo(archivo: UploadFile = File(...)):
    """Permite subir una imagen local (PNG, JPG, WEBP) para productos o configuración."""
    if not archivo.filename:
        raise HTTPException(400, "No se seleccionó ningún archivo.")

    ext = Path(archivo.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        raise HTTPException(400, "Formato no permitido. Usá JPG, PNG, WEBP o SVG.")

    contenido = await archivo.read()
    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(400, "La imagen es demasiado pesada (máximo 5 MB).")

    nombre_seguro = f"img_{secrets.token_hex(8)}{ext}"
    destino = UPLOAD_DIR / nombre_seguro
    destino.write_bytes(contenido)

    url_relativa = f"/static/uploads/{nombre_seguro}"
    return {"ok": True, "url": url_relativa, "mensaje": "Imagen subida correctamente."}



# Abierta a propósito a todos los roles: cada usuario cambia su propio PIN.
@app.post("/api/config/cambiar-clave", dependencies=[Depends(requiere_sesion_api)])
def cambiar_clave_admin_api(data: CambiarClaveIn, request: Request):
    """Cambia el PIN del usuario de la sesión (cualquier rol) con la RPC cambiar_pin."""
    limitar_intentos(request, bucket="cambiar_clave", max_intentos=5, ventana_seg=60)
    info = _info_sesion(request)
    res = get_db().rpc("cambiar_pin", {"p_usuario_id": info["usuario_id"], "p_pin_actual": data.pin_actual.strip(),
                                       "p_pin_nuevo": data.pin_nuevo.strip()}).execute().data or {}
    if not res.get("ok"):
        mensajes = {
            "pin_invalido": "El PIN nuevo tiene que ser de 4 números",
            "pin_inseguro": "Elegí un PIN distinto de 1234 y 0000",
            "pin_actual_incorrecto": "El PIN actual es incorrecto",
            "bloqueado": _msg_bloqueo(res),
        }
        raise HTTPException(400, mensajes.get(res.get("motivo"), "No se pudo cambiar el PIN"))
    info["debe_cambiar_pin"] = False
    return {"ok": True, "mensaje": "PIN actualizado correctamente."}


# ---------------------------------------------------------------------------
# Copia de Seguridad y Restauración (Snapshot JSON)
# ---------------------------------------------------------------------------
@app.get("/api/backup/exportar", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def exportar_backup_json():
    """Exporta todas las tablas del comercio en un archivo JSON descargable."""
    db = get_db()
    categorias = db.table("categorias").select("*").eq("comercio_id", COMERCIO_ID).order("id").execute().data or []
    clientes = db.table("clientes").select("*").eq("comercio_id", COMERCIO_ID).order("id").execute().data or []
    productos = db.table("productos").select("*").eq("comercio_id", COMERCIO_ID).order("id").execute().data or []
    ventas = db.table("ventas").select("*").eq("comercio_id", COMERCIO_ID).order("id").execute().data or []

    venta_ids = [v["id"] for v in ventas]
    detalle_ventas = []
    for i in range(0, len(venta_ids), 200):
        lote_ids = venta_ids[i:i + 200]
        res_det = db.table("detalle_ventas").select("*").in_("venta_id", lote_ids).order("id").execute()
        if res_det.data:
            detalle_ventas.extend(res_det.data)

    movimientos_caja = db.table("movimientos_caja").select("*").eq("comercio_id", COMERCIO_ID).order("id").execute().data or []
    try:
        movimientos_cuenta = db.table("movimientos_cuenta").select("*").eq("comercio_id", COMERCIO_ID).order("id").execute().data or []
    except Exception:
        movimientos_cuenta = []

    snapshot = {
        "version": "1.0",
        "timestamp": _ahora_local().isoformat(),
        "comercio_id": COMERCIO_ID,
        "tablas": {
            "categorias": categorias,
            "clientes": clientes,
            "productos": productos,
            "ventas": ventas,
            "detalle_ventas": detalle_ventas,
            "movimientos_caja": movimientos_caja,
            "movimientos_cuenta": movimientos_cuenta,
        },
    }
    contenido = json.dumps(snapshot, indent=2, default=str, ensure_ascii=False).encode("utf-8")
    nombre = f"backup_sistema_{_ahora_local().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        contenido,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.post("/api/backup/restaurar", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def restaurar_backup_json(
    request: Request,
    archivo: UploadFile = File(...),
    pin: str = Form(...),
):
    """Restaura las tablas del comercio desde un archivo Snapshot JSON validando pertenencia al comercio."""
    limitar_intentos(request, bucket="backup_restore", max_intentos=5, ventana_seg=60)
    if not _pin_valido(request, pin):
        raise HTTPException(403, "PIN incorrecto. No se modificó ningún dato.")

    raw = archivo.file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(400, "El archivo de respaldo está vacío")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "El archivo de respaldo supera el límite de 15 MB")
    try:
        backup_data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        raise HTTPException(400, f"El archivo no contiene un formato JSON válido: {e}")

    if not isinstance(backup_data, dict):
        raise HTTPException(400, "El contenido del respaldo no es un objeto JSON válido")
    tablas_data = backup_data.get("tablas") or backup_data.get("datos") or backup_data
    if not isinstance(tablas_data, dict):
        raise HTTPException(400, "No se encontraron datos de tablas en el archivo de respaldo")

    db = get_db()
    orden_tablas = [
        "categorias",
        "clientes",
        "productos",
        "ventas",
        "detalle_ventas",
        "movimientos_caja",
        "movimientos_cuenta",
    ]
    resumen = {}
    for tabla in orden_tablas:
        filas = tablas_data.get(tabla, [])
        if not filas or not isinstance(filas, list):
            resumen[tabla] = 0
            continue
        filas_limpias = []
        for f in filas:
            if not isinstance(f, dict):
                continue
            item = dict(f)
            if tabla != "detalle_ventas":
                item["comercio_id"] = COMERCIO_ID
            # Respaldos viejos no traen estas columnas: las filas restauradas quedan vigentes
            if tabla in ("categorias", "clientes", "productos"):
                item.setdefault("activo", True)
                item.setdefault("fecha_baja", None)
            elif tabla in ("ventas", "movimientos_caja", "movimientos_cuenta"):
                item.setdefault("archivado_en", None)
                item.setdefault("archivado_por", None)
            filas_limpias.append(item)
        if not filas_limpias:
            resumen[tabla] = 0
            continue

        # Verificar que ningún ID existente pertenezca a otro comercio (aislamiento multi-tenant)
        if tabla != "detalle_ventas":
            ids_entrantes = [int(f["id"]) for f in filas_limpias if "id" in f and str(f["id"]).isdigit()]
            for i in range(0, len(ids_entrantes), 200):
                lote_ids = ids_entrantes[i:i + 200]
                ajenos = (
                    db.table(tabla)
                    .select("id")
                    .in_("id", lote_ids)
                    .neq("comercio_id", COMERCIO_ID)
                    .limit(1)
                    .execute()
                    .data
                )
                if ajenos:
                    raise HTTPException(
                        403,
                        f"Conflicto de seguridad en tabla '{tabla}': el registro ID {ajenos[0]['id']} pertenece a otro comercio.",
                    )

        total_tabla = 0
        for i in range(0, len(filas_limpias), 50):
            lote = filas_limpias[i:i + 50]
            try:
                res = db.table(tabla).upsert(lote).execute()
                total_tabla += len(res.data or lote)
            except Exception as e:
                if "428C9" in str(e) or "generated always" in str(e).lower():
                    # Tabla con id GENERATED ALWAYS AS IDENTITY
                    for item in lote:
                        copia = dict(item)
                        copia.pop("id", None)
                        db.table(tabla).insert(copia).execute()
                        total_tabla += 1
                else:
                    raise HTTPException(400, f"Error al restaurar tabla '{tabla}': {getattr(e, 'message', e)}")
        resumen[tabla] = total_tabla

    return {"ok": True, "resumen": resumen, "mensaje": "Restauración completada con éxito."}


# ---------------------------------------------------------------------------
# Zona de Mantenimiento / Puesta a Cero
# ---------------------------------------------------------------------------
class ResetDatosIn(BaseModel):
    modo: Literal["movimientos", "total"]
    confirmar: bool
    palabra: str
    pin: str


@app.post("/api/mantenimiento/reset", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def resetear_datos_api(data: ResetDatosIn, request: Request):
    """Puesta a cero de datos del comercio con triple traba de seguridad."""
    limitar_intentos(request, bucket="reset_datos", max_intentos=5, ventana_seg=60)
    if not data.confirmar:
        raise HTTPException(400, "Debés marcar la casilla confirmando que entendés que la acción es irreversible")
    if data.palabra.strip() != "BORRAR":
        raise HTTPException(400, "Debés escribir exactamente la palabra BORRAR en mayúsculas")

    # La RPC valida el PIN y el rol por su cuenta (verificar_login): no llamar también a _pin_valido,
    # un PIN erróneo contaría dos intentos para el bloqueo.
    res = get_db().rpc("resetear_datos_comercio", {
        "p_comercio_id": COMERCIO_ID,
        "p_modo": data.modo,
        "p_usuario_id": _info_sesion(request).get("usuario_id"),
        "p_pin": data.pin.strip(),
    }).execute().data or {}
    if not res.get("ok"):
        motivo = res.get("motivo")
        if motivo == "bloqueado":
            raise HTTPException(403, _msg_bloqueo(res))
        if motivo == "no_autorizado":
            raise HTTPException(403, "Solo un Dueño puede poner a cero los datos")
        if motivo == "pin_incorrecto":
            raise HTTPException(403, "PIN incorrecto. No se modificó ningún dato.")
        raise HTTPException(400, "No se pudo ejecutar la puesta a cero")
    return res


# ---------------------------------------------------------------------------
# Auditoría y Trazabilidad de Movimientos (exclusivo Dueño / Admin)
# ---------------------------------------------------------------------------
@app.get("/auditoria", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion_html)])
async def auditoria_page(request: Request):
    return templates.TemplateResponse(request, "auditoria.html", {"config": config_publica()})


@app.get("/api/auditoria", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def listar_auditoria(
    tipo_accion: Optional[str] = None,
    usuario_id: Optional[int] = None,
    limite: int = 200,
):
    """Historial de movimientos críticos para el Dueño, del más reciente al más antiguo."""
    limite = max(1, min(limite, 500))
    consulta = (
        get_db()
        .table("auditoria_movimientos")
        .select("id, fecha_hora, usuario_id, usuario_nombre, tipo_accion, detalles")
        .eq("comercio_id", COMERCIO_ID)
    )
    if tipo_accion:
        consulta = consulta.eq("tipo_accion", tipo_accion.strip().upper())
    if usuario_id is not None:
        consulta = consulta.eq("usuario_id", usuario_id)

    filas = consulta.order("fecha_hora", desc=True).limit(limite).execute().data or []
    return [
        {
            "id": f["id"],
            "fecha_hora": f.get("fecha_hora") or "",
            "fecha_formateada": fmt_fecha(f.get("fecha_hora")),
            "usuario_id": f.get("usuario_id"),
            "usuario_nombre": f.get("usuario_nombre") or "Sistema",
            "tipo_accion": f.get("tipo_accion") or "",
            "detalles": f.get("detalles") or {},
        }
        for f in filas
    ]


# ---------------------------------------------------------------------------
# Versión Celular Offline: Manifest PWA y Backup Automático a Google Drive
# ---------------------------------------------------------------------------
@app.get("/manifest.json")
def pwa_manifest():
    cfg = cargar_config()
    nombre = cfg.get("nombre_comercio", "Control de Stock POS")
    color = cfg.get("color_primario", "#2563EB")
    icono = cfg.get("logo_imagen") or "/static/icon-192.svg"
    return JSONResponse(
        {
            "name": f"{nombre} — POS Celular Offline",
            "short_name": nombre[:18],
            "start_url": "/",
            "display": "standalone",
            "background_color": "#F7F9FA",
            "theme_color": color,
            "orientation": "any",
            "icons": [
                {
                    "src": icono,
                    "sizes": "192x192",
                    "type": "image/svg+xml" if icono.endswith(".svg") else "image/png",
                }
            ],
        }
    )


@app.get("/sw.js")
def servir_service_worker():
    sw_file = BUNDLE_DIR / "sw.js"
    contenido = sw_file.read_text(encoding="utf-8") if sw_file.exists() else ""
    return Response(
        contenido,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.post("/api/backup-drive/sincronizar-desde-celular")
async def sincronizar_desde_celular_api(request: Request):
    """Recibe el paquete de datos del celular cuando está en la misma red y lo guarda en datos/backups_drive/."""
    try:
        raw = await request.body()
        if raw:
            dest_dir = APP_DIR / "datos" / "backups_drive"
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "datos_celular_actual.json").write_bytes(raw)
        return {"ok": True}
    except Exception:
        return {"ok": False}


@app.get("/api/backup-drive/estado", dependencies=[Depends(requiere_sesion_api)])
def obtener_estado_backup_drive_api():
    """Devuelve el estado del backup automático de la carpeta 'datos/' y la conexión Wi-Fi."""
    from backup_drive import (
        _carpeta_visible_celular_por_defecto,
        evaluar_condicion_wifi,
        leer_estado_backup,
    )
    cfg = cargar_config()
    estado = leer_estado_backup(APP_DIR)
    wifi = evaluar_condicion_wifi(cfg)
    carpeta_def = _carpeta_visible_celular_por_defecto(APP_DIR)
    return {
        "estado": estado,
        "wifi": wifi,
        "carpeta_sugerida": carpeta_def,
        "datos_dir": str(APP_DIR / "datos"),
    }


@app.post("/api/backup-drive/ejecutar-ahora", dependencies=[Depends(requiere_sesion_api)])
def ejecutar_backup_drive_ahora_api():
    """Ejecuta de inmediato el respaldo de toda la carpeta 'datos/' y si hay Wi-Fi lo sube a Google Drive."""
    from backup_drive import ejecutar_backup_datos
    cfg = cargar_config()
    res = ejecutar_backup_datos(
        APP_DIR,
        cfg,
        forzar_subida_nube=True,
        motivo="Respaldo manual solicitado por el usuario",
    )
    if not res.get("ok"):
        raise HTTPException(500, res.get("error") or "Error al generar el backup de la carpeta datos")
    return res


@app.get("/api/backup-drive/descargar-zip", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def descargar_carpeta_datos_zip_api():
    """Empaqueta y descarga la carpeta 'datos/' completa en un archivo .zip listo para guardar o subir a Drive."""
    from backup_drive import ejecutar_backup_datos, obtener_rutas_base
    cfg = cargar_config()
    ejecutar_backup_datos(APP_DIR, cfg, forzar_subida_nube=False, motivo="Descarga directa de carpeta datos (.zip)")
    rutas = obtener_rutas_base(APP_DIR)
    zip_actual = rutas["backups_dir"] / "datos_backup_actual.zip"
    if not zip_actual.exists():
        raise HTTPException(404, "No se pudo generar el archivo ZIP de la carpeta datos")
    fecha_sello = _ahora_local().strftime("%Y%m%d_%H%M%S")
    nombre_descarga = f"carpeta_datos_backup_{fecha_sello}.zip"
    return Response(
        zip_actual.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nombre_descarga}"'},
    )


@app.post("/api/backup-drive/restaurar-zip", dependencies=[Depends(requiere_sesion_api), Depends(requiere_admin_api)])
def restaurar_carpeta_datos_zip_api(
    request: Request,
    archivo: UploadFile = File(...),
    pin: str = Form(...),
):
    """Restaura la carpeta 'datos/' completa (pos_local.db + config + imágenes) desde un backup .zip."""
    from backup_drive import restaurar_datos_desde_zip
    limitar_intentos(request, bucket="backup_restore_zip", max_intentos=5, ventana_seg=60)
    if not _pin_valido(request, pin):
        raise HTTPException(403, "PIN incorrecto. No se modificó ningún dato.")
    raw = archivo.file.read(50 * 1024 * 1024)
    if not raw:
        raise HTTPException(400, "El archivo ZIP está vacío.")
    try:
        resultado = restaurar_datos_desde_zip(APP_DIR, raw)
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as exc:
        raise HTTPException(500, f"Error restaurando carpeta datos: {exc}")
    return {
        "ok": True,
        "mensaje": "Carpeta 'datos' restaurada íntegramente con éxito.",
        **resultado,
    }




# ---------------------------------------------------------------------------
# RUTAS DE RED LOCAL (MODO SERVIDOR CENTRAL) Y SETUP WIZARD
# ---------------------------------------------------------------------------
@app.get("/api/red/info")
def red_info():
    """Devuelve la URL y IP de red local del servidor para conectar celulares."""
    puerto = int(os.getenv("PORT", "8000"))
    ip_local = _obtener_ip_red_local()
    return {
        "ip": ip_local,
        "puerto": puerto,
        "url": f"http://{ip_local}:{puerto}",
    }


@app.get("/api/red/qr.svg")
def red_qr_svg():
    """Genera al vuelo el codigo QR SVG para conectar celulares en la red Wi-Fi."""
    puerto = int(os.getenv("PORT", "8000"))
    ip_local = _obtener_ip_red_local()
    svg = _generar_qr_svg(f"http://{ip_local}:{puerto}")
    if svg:
        return Response(content=svg, media_type="image/svg+xml")
    raise HTTPException(404, "No se pudo generar el codigo QR")


class SetupWizardIn(BaseModel):
    nombre_comercio: str
    rubro: Optional[str] = "comercio_general"
    modo_operacion: Optional[str] = "autonomo_offline"
    alias: Optional[str] = ""
    cbu: Optional[str] = ""
    telefono: Optional[str] = ""
    logo_imagen: Optional[str] = ""
    pin_dueno: Optional[str] = "1234"
    pin_cajero: Optional[str] = "0000"


@app.post("/api/setup-wizard/completar")
def completar_setup_wizard(datos: SetupWizardIn):
    """Guarda la configuracion inicial del asistente de primer arranque."""
    cfg = cargar_config()
    cfg["nombre_comercio"] = datos.nombre_comercio.strip() or "Mi Comercio"
    cfg["rubro"] = (datos.rubro or "comercio_general").strip()
    cfg["modo_operacion"] = datos.modo_operacion or "autonomo_offline"
    cfg["alias"] = (datos.alias or "").strip()
    cfg["cbu"] = (datos.cbu or "").strip()
    cfg["telefono"] = (datos.telefono or "").strip()
    if datos.logo_imagen:
        cfg["logo_imagen"] = datos.logo_imagen
    cfg["wizard_completado"] = 1
    guardar_config(cfg)

    # Actualizar PIN del Dueño
    db = get_db()
    from local_db import hash_pin
    if datos.pin_dueno and len(datos.pin_dueno.strip()) == 4 and datos.pin_dueno.strip().isdigit():
        try:
            db.table("usuarios").update({
                "pin_hash": hash_pin(datos.pin_dueno.strip()),
                "debe_cambiar_pin": 0
            }).eq("nombre", "Dueño").eq("comercio_id", COMERCIO_ID).execute()
        except Exception as e:
            log.warning("No se pudo actualizar PIN de Dueño: %s", e)

    # Actualizar PIN del Cajero
    if datos.pin_cajero and len(datos.pin_cajero.strip()) == 4 and datos.pin_cajero.strip().isdigit():
        try:
            db.table("usuarios").update({
                "pin_hash": hash_pin(datos.pin_cajero.strip()),
                "debe_cambiar_pin": 0
            }).eq("nombre", "Cajero").eq("comercio_id", COMERCIO_ID).execute()
        except Exception as e:
            log.warning("No se pudo actualizar PIN de Cajero: %s", e)

    return {"ok": True, "mensaje": "Asistente completado exitosamente", "config": config_publica()}
