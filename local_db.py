"""
Motor de base de datos local SQLite para Control de Stock / POS (Modo 100% Offline).

Reemplaza de forma transparente las llamadas a Supabase / PostgREST (db.table()...
y db.rpc()...) usando una base SQLite local guardada en 'datos/pos_local.db'.
Implementa:
- Conexiones concurrentes con modo WAL y timeout.
- Esquema completo idempotente con migraciones automáticas.
- API fluent idéntica a PostgREST: select, insert, upsert, update, eq, in_, gte, lte, order, limit, execute.
- Las 6 funciones de negocio (RPC): verificar_login, listar_usuarios_login, cambiar_pin,
  registrar_venta_atomica, registrar_pago_cuenta, resetear_datos_comercio.
- Formato y validaciones idénticas a las funciones PL/pgSQL de producción.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from postgrest.exceptions import APIError
except ImportError:
    class APIError(Exception):  # type: ignore
        def __init__(self, err_dict: dict):
            super().__init__(err_dict.get("message", "Error de base de datos"))
            self.code = err_dict.get("code")
            self.message = err_dict.get("message")

from formato import fmt_cantidad, fmt_moneda

log = logging.getLogger("pos.local_db")
TZ_AR = timezone(timedelta(hours=-3))

def _ahora_iso() -> str:
    return datetime.now(TZ_AR).isoformat()


# ---------------------------------------------------------------------------
# Seguridad y Hashing de PIN
# ---------------------------------------------------------------------------
def hash_pin(pin: str) -> str:
    """Genera hash seguro PBKDF2-HMAC-SHA256 para el PIN."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2:sha256:100000${salt}${h.hex()}"


def check_pin(pin: str, pin_hash: str) -> bool:
    """Verifica un PIN contra el hash PBKDF2 o bcrypt si estuviera disponible."""
    if not pin_hash:
        return False
    if pin_hash.startswith("pbkdf2:"):
        partes = pin_hash.split("$")
        if len(partes) == 3:
            salt = partes[1]
            esperado = partes[2]
            calculado = hashlib.pbkdf2_hmac(
                "sha256", pin.encode("utf-8"), salt.encode("utf-8"), 100_000
            ).hex()
            return secrets.compare_digest(calculado, esperado)
    try:
        import bcrypt  # type: ignore
        if pin_hash.startswith(("$2a$", "$2b$", "$2y$")):
            return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))
    except ImportError:
        pass
    # Fallback si en desarrollo se guardó texto plano temporal
    return pin == pin_hash


# ---------------------------------------------------------------------------
# Inicialización y Esquema SQLite
# ---------------------------------------------------------------------------
def _crear_esquema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS comercios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_negocio TEXT NOT NULL DEFAULT 'Comercio 001',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        nombre TEXT NOT NULL,
        pin_hash TEXT NOT NULL,
        rol TEXT NOT NULL CHECK (rol IN ('admin', 'cajero')),
        activo INTEGER NOT NULL DEFAULT 1,
        intentos_fallidos INTEGER NOT NULL DEFAULT 0,
        bloqueado_hasta TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (comercio_id, nombre)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS categorias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        nombre TEXT NOT NULL,
        activo INTEGER NOT NULL DEFAULT 1,
        fecha_baja TEXT,
        descripcion TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (comercio_id, nombre)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        codigo TEXT NOT NULL,
        nombre TEXT NOT NULL,
        categoria_id INTEGER REFERENCES categorias(id),
        precio_costo REAL NOT NULL DEFAULT 0,
        margen_ganancia REAL NOT NULL DEFAULT 0,
        precio_venta REAL NOT NULL DEFAULT 0,
        usa_precio_manual INTEGER NOT NULL DEFAULT 0,
        stock_actual REAL NOT NULL DEFAULT 0,
        stock_minimo REAL NOT NULL DEFAULT 0,
        imagen_url TEXT DEFAULT '',
        activo INTEGER NOT NULL DEFAULT 1,
        fecha_baja TEXT,
        puntos_canje REAL NOT NULL DEFAULT 0,
        unidad_medida TEXT NOT NULL DEFAULT 'unid',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (comercio_id, codigo)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        nombre TEXT NOT NULL,
        telefono TEXT DEFAULT '',
        email TEXT DEFAULT '',
        limite_credito REAL NOT NULL DEFAULT 0,
        saldo_deudor REAL NOT NULL DEFAULT 0,
        puntos REAL NOT NULL DEFAULT 0,
        activo INTEGER NOT NULL DEFAULT 1,
        fecha_baja TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ventas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        cliente_id INTEGER REFERENCES clientes(id),
        total_venta REAL NOT NULL DEFAULT 0,
        forma_pago TEXT NOT NULL DEFAULT 'Efectivo',
        fecha_hora TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        puntos_ganados REAL NOT NULL DEFAULT 0,
        archivado_en TEXT,
        archivado_por INTEGER
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS detalle_ventas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        venta_id INTEGER NOT NULL REFERENCES ventas(id),
        producto_id INTEGER REFERENCES productos(id),
        cantidad REAL NOT NULL,
        precio_unitario REAL NOT NULL DEFAULT 0,
        costo_unitario REAL NOT NULL DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS movimientos_cuenta (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        cliente_id INTEGER NOT NULL REFERENCES clientes(id),
        tipo TEXT NOT NULL,
        monto REAL NOT NULL,
        descripcion TEXT,
        ticket_id INTEGER REFERENCES ventas(id),
        fecha_hora TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        archivado_en TEXT,
        archivado_por INTEGER
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS movimientos_caja (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        tipo TEXT NOT NULL CHECK (tipo IN ('ingreso_extra', 'egreso')),
        monto REAL NOT NULL,
        concepto TEXT NOT NULL,
        usuario_nombre TEXT,
        fecha TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        archivado_en TEXT,
        archivado_por INTEGER
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS cierres_caja (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        estado TEXT NOT NULL CHECK (estado IN ('abierta', 'cerrada')),
        monto_inicial REAL NOT NULL DEFAULT 0,
        fecha_apertura TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        fecha_cierre TEXT,
        total_esperado REAL,
        monto_real REAL,
        diferencia REAL,
        archivado_en TEXT,
        archivado_por INTEGER
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS auditoria_movimientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        fecha_hora TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        usuario_id INTEGER,
        usuario_nombre TEXT NOT NULL DEFAULT '',
        tipo_accion TEXT NOT NULL,
        detalles TEXT NOT NULL DEFAULT '{}'
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS configuracion_negocio (
        comercio_id INTEGER PRIMARY KEY REFERENCES comercios(id),
        nombre_comercio TEXT NOT NULL DEFAULT 'Mi Comercio',
        telefono TEXT NOT NULL DEFAULT '',
        direccion TEXT NOT NULL DEFAULT '',
        moneda TEXT NOT NULL DEFAULT '$',
        color_primario TEXT NOT NULL DEFAULT '#2563EB',
        logo_imagen TEXT NOT NULL DEFAULT '',
        qr_imagen TEXT NOT NULL DEFAULT '',
        titular_cuenta TEXT NOT NULL DEFAULT '',
        banco TEXT NOT NULL DEFAULT '',
        alias TEXT NOT NULL DEFAULT '',
        cbu TEXT NOT NULL DEFAULT '',
        pesos_por_punto REAL NOT NULL DEFAULT 100.00,
        smtp_servidor TEXT NOT NULL DEFAULT '',
        smtp_puerto INTEGER NOT NULL DEFAULT 587,
        smtp_usuario TEXT NOT NULL DEFAULT '',
        smtp_password TEXT NOT NULL DEFAULT '',
        email_destinatario TEXT NOT NULL DEFAULT '',
        auto_backup_activo INTEGER NOT NULL DEFAULT 1,
        modo_wifi_backup TEXT NOT NULL DEFAULT 'cualquier_wifi',
        wifi_seguro_ssid TEXT NOT NULL DEFAULT '',
        carpeta_sincronizacion_drive TEXT NOT NULL DEFAULT '',
        google_drive_webhook_url TEXT NOT NULL DEFAULT '',
        modo_bloqueo TEXT NOT NULL DEFAULT 'nunca',
        tiempo_inactividad_min INTEGER NOT NULL DEFAULT 15,
        modo_operacion TEXT NOT NULL DEFAULT 'autonomo_offline',
        wizard_completado INTEGER NOT NULL DEFAULT 0,
        rubro TEXT NOT NULL DEFAULT 'comercio_general',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS alertas_seguridad (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        tipo TEXT NOT NULL DEFAULT 'intento_fuerza_bruta',
        ip_origen TEXT NOT NULL DEFAULT '',
        usuario_afectado TEXT NOT NULL DEFAULT '',
        detalles TEXT NOT NULL DEFAULT '',
        fecha_hora TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        leida INTEGER NOT NULL DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS promos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comercio_id INTEGER NOT NULL REFERENCES comercios(id),
        nombre TEXT NOT NULL,
        categoria TEXT NOT NULL DEFAULT 'General',
        descripcion TEXT NOT NULL DEFAULT '',
        precio_venta REAL NOT NULL DEFAULT 0,
        fijos_json TEXT NOT NULL DEFAULT '[]',
        elecciones_json TEXT NOT NULL DEFAULT '[]',
        activo INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Índices esenciales
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prod_comercio ON productos (comercio_id, nombre);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_promos_comercio ON promos (comercio_id, categoria, nombre);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alertas_seg ON alertas_seguridad (comercio_id, leida, fecha_hora DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cli_comercio ON clientes (comercio_id, nombre);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ven_comercio ON ventas (comercio_id, fecha_hora DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_det_venta ON detalle_ventas (venta_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mov_cuenta ON movimientos_cuenta (comercio_id, cliente_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mov_caja ON movimientos_caja (comercio_id, fecha DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cierres_caja ON cierres_caja (comercio_id, fecha_apertura DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_auditoria ON auditoria_movimientos (comercio_id, fecha_hora DESC);")

    cur.execute("PRAGMA table_info(productos);")
    cols_p = [r[1] for r in cur.fetchall()]
    if "unidad_medida" not in cols_p:
        cur.execute("ALTER TABLE productos ADD COLUMN unidad_medida TEXT NOT NULL DEFAULT 'unid';")

    cur.execute("PRAGMA table_info(categorias);")
    cols_c = [r[1] for r in cur.fetchall()]
    if "created_at" not in cols_c:
        cur.execute("ALTER TABLE categorias ADD COLUMN created_at TEXT NOT NULL DEFAULT '';")
        cur.execute("UPDATE categorias SET created_at = datetime('now') WHERE created_at = '';")

    cur.execute("PRAGMA table_info(configuracion_negocio);")
    cols_cfg = [r[1] for r in cur.fetchall()]
    for col_name, col_def in [
        ("auto_backup_activo", "INTEGER NOT NULL DEFAULT 1"),
        ("modo_wifi_backup", "TEXT NOT NULL DEFAULT 'cualquier_wifi'"),
        ("wifi_seguro_ssid", "TEXT NOT NULL DEFAULT ''"),
        ("carpeta_sincronizacion_drive", "TEXT NOT NULL DEFAULT ''"),
        ("google_drive_webhook_url", "TEXT NOT NULL DEFAULT ''"),
        ("modo_bloqueo", "TEXT NOT NULL DEFAULT 'nunca'"),
        ("tiempo_inactividad_min", "INTEGER NOT NULL DEFAULT 15"),
        ("modo_operacion", "TEXT NOT NULL DEFAULT 'autonomo_offline'"),
        ("wizard_completado", "INTEGER NOT NULL DEFAULT 0"),
        ("rubro", "TEXT NOT NULL DEFAULT 'comercio_general'"),
        ("clave_maestra_rescate", "TEXT NOT NULL DEFAULT 'RESCATE-BEBIDAS-7489'"),
    ]:
        if col_name not in cols_cfg:
            cur.execute(f"ALTER TABLE configuracion_negocio ADD COLUMN {col_name} {col_def};")

    # Asegurar comercio inicial 1
    cur.execute("SELECT id FROM comercios WHERE id = 1;")
    if not cur.fetchone():
        cur.execute("INSERT INTO comercios (id, nombre_negocio, created_at) VALUES (1, 'Mi Comercio', ?);", (_ahora_iso(),))

    # Asegurar usuarios iniciales: Dueño (1234) y Cajero 1, Cajero 2, Cajero 3 si aún no hay cajeros
    cur.execute("SELECT id FROM usuarios WHERE comercio_id = 1 AND rol = 'admin';")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO usuarios (comercio_id, nombre, pin_hash, rol, created_at) VALUES (1, 'Dueño', ?, 'admin', ?);",
            (hash_pin("1234"), _ahora_iso()),
        )
    cur.execute("SELECT id FROM usuarios WHERE comercio_id = 1 AND rol = 'cajero';")
    if not cur.fetchone():
        for nom_caj, pin_caj in [("Cajero 1", "1111"), ("Cajero 2", "2222"), ("Cajero 3", "3333")]:
            cur.execute(
                "INSERT INTO usuarios (comercio_id, nombre, pin_hash, rol, created_at) VALUES (1, ?, ?, 'cajero', ?);",
                (nom_caj, hash_pin(pin_caj), _ahora_iso()),
            )

    # Asegurar configuración de negocio por defecto
    cur.execute("SELECT comercio_id FROM configuracion_negocio WHERE comercio_id = 1;")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO configuracion_negocio (comercio_id, nombre_comercio, updated_at) VALUES (1, 'Mi Comercio', ?);",
            (_ahora_iso(),),
        )

    conn.commit()
    sembrar_catalogo_bebidas(conn, comercio_id=1, forzar=False)


def sembrar_catalogo_bebidas(conn: sqlite3.Connection, comercio_id: int = 1, forzar: bool = False) -> dict:
    """Siembra las categorías, productos y promos de bebidas si la tabla de productos o promos está vacía (o si forzar=True)."""
    try:
        from catalogo_bebidas import CATEGORIAS_BEBIDAS, PRODUCTOS_BEBIDAS_RAW, PROMOS_BEBIDAS_RAW
    except ImportError:
        return {"ok": False, "motivo": "modulo_catalogo_no_encontrado"}

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM productos WHERE comercio_id = ? AND activo = 1;", (comercio_id,))
    cant_prod = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM promos WHERE comercio_id = ? AND activo = 1;", (comercio_id,))
    cant_promos = int(cur.fetchone()[0])

    if not forzar and cant_prod > 0 and cant_promos > 0:
        return {"ok": True, "sembrado": False, "productos": cant_prod, "promos": cant_promos}

    ahora = _ahora_iso()
    cat_map: Dict[str, int] = {}

    for nombre_cat in CATEGORIAS_BEBIDAS:
        cur.execute(
            "SELECT id FROM categorias WHERE comercio_id = ? AND LOWER(nombre) = LOWER(?);",
            (comercio_id, nombre_cat),
        )
        row = cur.fetchone()
        if row:
            cat_id = int(row[0])
            cur.execute("UPDATE categorias SET activo = 1, fecha_baja = NULL WHERE id = ?;", (cat_id,))
        else:
            cur.execute(
                "INSERT INTO categorias (comercio_id, nombre, activo, descripcion, created_at) VALUES (?, ?, 1, '', ?);",
                (comercio_id, nombre_cat, ahora),
            )
            cat_id = int(cur.lastrowid)
        cat_map[nombre_cat] = cat_id

    prod_name_to_id: Dict[str, int] = {}
    cur.execute("SELECT id, nombre FROM productos WHERE comercio_id = ?;", (comercio_id,))
    for r in cur.fetchall():
        prod_name_to_id[str(r[1])] = int(r[0])

    insertados_prod = 0
    for idx, (prod_nom, cat_nom, precio_vta, stock_ini) in enumerate(PRODUCTOS_BEBIDAS_RAW, start=1):
        cat_id = cat_map.get(cat_nom)
        if prod_nom in prod_name_to_id:
            pid = prod_name_to_id[prod_nom]
            if forzar:
                costo = round(float(precio_vta) * 0.70, 2)
                cur.execute(
                    """UPDATE productos
                       SET categoria_id = ?, precio_costo = ?, precio_venta = ?,
                           usa_precio_manual = 1, activo = 1, fecha_baja = NULL
                       WHERE id = ?;""",
                    (cat_id, costo, float(precio_vta), pid),
                )
        else:
            codigo = f"BEB-{idx:03d}"
            # Asegurar unicidad de código
            cur.execute("SELECT id FROM productos WHERE comercio_id = ? AND codigo = ?;", (comercio_id, codigo))
            if cur.fetchone():
                codigo = f"BEB-{idx:03d}-{secrets.token_hex(2).upper()}"
            costo = round(float(precio_vta) * 0.70, 2)
            cur.execute(
                """INSERT INTO productos (
                    comercio_id, codigo, nombre, categoria_id, precio_costo,
                    margen_ganancia, precio_venta, usa_precio_manual,
                    stock_actual, stock_minimo, activo, unidad_medida, created_at
                ) VALUES (?, ?, ?, ?, ?, 42.86, ?, 1, ?, 5, 1, 'unid', ?);""",
                (comercio_id, codigo, prod_nom, cat_id, costo, float(precio_vta), float(stock_ini), ahora),
            )
            pid = int(cur.lastrowid)
            prod_name_to_id[prod_nom] = pid
            insertados_prod += 1

    # Si no hay promos o se fuerza recarga de promos
    insertados_promos = 0
    if cant_promos == 0 or forzar:
        cur.execute("SELECT nombre FROM promos WHERE comercio_id = ?;", (comercio_id,))
        promos_existentes = {str(r[0]).lower() for r in cur.fetchall()}

        for p_raw in PROMOS_BEBIDAS_RAW:
            nom_promo = p_raw["nombre"]
            if nom_promo.lower() in promos_existentes and not forzar:
                continue

            fijos_res = []
            for f in p_raw.get("fijos", []):
                p_nom = f[0] if isinstance(f, (list, tuple)) else f["producto"]
                cant_f = f[1] if isinstance(f, (list, tuple)) else f.get("cantidad", 1)
                pid = prod_name_to_id.get(p_nom)
                if pid:
                    fijos_res.append({
                        "producto_id": pid,
                        "nombre": p_nom,
                        "cantidad": float(cant_f),
                    })

            elecciones_res = []
            for grp in p_raw.get("elecciones", []):
                opciones_res = []
                for op in grp.get("opciones", []):
                    p_nom = op[0] if isinstance(op, (list, tuple)) else op["producto"]
                    extra_op = op[1] if isinstance(op, (list, tuple)) else op.get("extra", 0)
                    pid = prod_name_to_id.get(p_nom)
                    if pid:
                        opciones_res.append({
                            "producto_id": pid,
                            "nombre": p_nom,
                            "extra": float(extra_op),
                        })
                if opciones_res:
                    elecciones_res.append({
                        "titulo": grp.get("titulo", "Elegir opción"),
                        "cantidad": float(grp.get("cantidad", 1)),
                        "opciones": opciones_res,
                    })

            fijos_json = json.dumps(fijos_res, ensure_ascii=False)
            elecciones_json = json.dumps(elecciones_res, ensure_ascii=False)

            cur.execute("SELECT id FROM promos WHERE comercio_id = ? AND LOWER(nombre) = LOWER(?);", (comercio_id, nom_promo))
            ex = cur.fetchone()
            if ex:
                cur.execute(
                    """UPDATE promos
                       SET categoria = ?, descripcion = ?, precio_venta = ?,
                           fijos_json = ?, elecciones_json = ?, activo = 1
                       WHERE id = ?;""",
                    (p_raw["categoria"], p_raw.get("descripcion", ""), float(p_raw.get("precio_venta") or p_raw.get("precio") or 0), fijos_json, elecciones_json, int(ex[0])),
                )
            else:
                cur.execute(
                    """INSERT INTO promos (
                        comercio_id, nombre, categoria, descripcion, precio_venta,
                        fijos_json, elecciones_json, activo, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?);""",
                    (
                        comercio_id,
                        nom_promo,
                        p_raw["categoria"],
                        p_raw.get("descripcion", ""),
                        float(p_raw.get("precio_venta") or p_raw.get("precio") or 0),
                        fijos_json,
                        elecciones_json,
                        ahora,
                    ),
                )
                insertados_promos += 1

    conn.commit()
    return {
        "ok": True,
        "sembrado": True,
        "productos_insertados": insertados_prod,
        "promos_insertadas": insertados_promos,
    }


# ---------------------------------------------------------------------------
# Gestor de Conexiones por Hilo
# ---------------------------------------------------------------------------
class ConnectionPool:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()

    def get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                check_same_thread=False,
                isolation_level=None,  # Autocommit por defecto; transacciones explícitas con BEGIN/COMMIT
            )
            conn.row_factory = sqlite3.Row
            # WAL mode para lecturas y escrituras no bloqueantes
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


# ---------------------------------------------------------------------------
# Objeto de Respuesta compatible con PostgREST
# ---------------------------------------------------------------------------
class APIResponse:
    def __init__(self, data: Any):
        self.data = data


# ---------------------------------------------------------------------------
# Constructor de Consultas para Tablas (TableQuery)
# ---------------------------------------------------------------------------
class TableQuery:
    def __init__(self, pool: ConnectionPool, tabla: str):
        self.pool = pool
        self.tabla = tabla
        self._action = "select"
        self._select_cols = "*"
        self._insert_data: Optional[Union[dict, list[dict]]] = None
        self._update_data: Optional[dict] = None
        self._upsert_conflict: Optional[str] = None
        self._where_clauses: List[str] = []
        self._params: List[Any] = []
        self._order_by: List[str] = []
        self._limit: Optional[int] = None

    def select(self, cols: str = "*") -> TableQuery:
        self._action = "select"
        self._select_cols = cols
        return self

    def insert(self, data: Union[dict, list[dict]]) -> TableQuery:
        self._action = "insert"
        self._insert_data = data
        return self

    def upsert(self, data: Union[dict, list[dict]], on_conflict: Optional[str] = None) -> TableQuery:
        self._action = "upsert"
        self._insert_data = data
        self._upsert_conflict = on_conflict
        return self

    def update(self, data: dict) -> TableQuery:
        self._action = "update"
        self._update_data = data
        return self

    def delete(self) -> TableQuery:
        self._action = "delete"
        return self

    def eq(self, col: str, val: Any) -> TableQuery:
        if isinstance(val, bool):
            val = 1 if val else 0
        self._where_clauses.append(f'"{col}" = ?')
        self._params.append(val)
        return self

    def neq(self, col: str, val: Any) -> TableQuery:
        if isinstance(val, bool):
            val = 1 if val else 0
        self._where_clauses.append(f'"{col}" != ?')
        self._params.append(val)
        return self

    def ilike(self, col: str, val: str) -> TableQuery:
        self._where_clauses.append(f'"{col}" LIKE ?')
        self._params.append(val)
        return self

    def like(self, col: str, val: str) -> TableQuery:
        self._where_clauses.append(f'"{col}" LIKE ?')
        self._params.append(val)
        return self

    def in_(self, col: str, vals: list) -> TableQuery:
        if not vals:
            self._where_clauses.append("1 = 0")
            return self
        placeholders = ", ".join(["?"] * len(vals))
        norm_vals = [1 if v is True else 0 if v is False else v for v in vals]
        self._where_clauses.append(f'"{col}" IN ({placeholders})')
        self._params.extend(norm_vals)
        return self

    def gte(self, col: str, val: Any) -> TableQuery:
        self._where_clauses.append(f'"{col}" >= ?')
        self._params.append(val)
        return self

    def lte(self, col: str, val: Any) -> TableQuery:
        self._where_clauses.append(f'"{col}" <= ?')
        self._params.append(val)
        return self

    def gt(self, col: str, val: Any) -> TableQuery:
        self._where_clauses.append(f'"{col}" > ?')
        self._params.append(val)
        return self

    def lt(self, col: str, val: Any) -> TableQuery:
        self._where_clauses.append(f'"{col}" < ?')
        self._params.append(val)
        return self

    def is_(self, col: str, val: Any) -> TableQuery:
        if val in ("null", None):
            self._where_clauses.append(f'"{col}" IS NULL')
        else:
            self._where_clauses.append(f'"{col}" IS ?')
            self._params.append(val)
        return self

    def order(self, col: str, desc: bool = False) -> TableQuery:
        sentido = "DESC" if desc else "ASC"
        self._order_by.append(f'"{col}" {sentido}')
        return self

    def limit(self, count: int) -> TableQuery:
        self._limit = count
        return self

    def execute(self) -> APIResponse:
        conn = self.pool.get_connection()
        cur = conn.cursor()

        if self._action == "select":
            where_sql = f" WHERE {' AND '.join(self._where_clauses)}" if self._where_clauses else ""
            order_sql = f" ORDER BY {', '.join(self._order_by)}" if self._order_by else ""
            limit_sql = f" LIMIT {self._limit}" if self._limit is not None else ""

            # Detectar relaciones embebidas tipo PostgREST
            cols = self._select_cols.strip()
            table_ref = f'"{self.tabla}"'
            join_sql = ""

            if self.tabla == "ventas" and "clientes(" in cols:
                table_ref = f'"{self.tabla}" t'
                join_sql = " LEFT JOIN clientes c ON t.cliente_id = c.id"
                cols = "t.*, c.nombre as __join_clientes_nombre"
                where_sql = f" WHERE {' AND '.join([c.replace('\"', 't.\"', 1) for c in self._where_clauses])}" if self._where_clauses else ""
                order_sql = f" ORDER BY {', '.join([o.replace('\"', 't.\"', 1) for o in self._order_by])}" if self._order_by else ""

            elif self.tabla == "detalle_ventas" and "productos(" in cols:
                table_ref = f'"{self.tabla}" t'
                join_sql = " LEFT JOIN productos p ON t.producto_id = p.id"
                cols = "t.*, p.codigo as __join_productos_codigo, p.nombre as __join_productos_nombre"
                where_sql = f" WHERE {' AND '.join([c.replace('\"', 't.\"', 1) for c in self._where_clauses])}" if self._where_clauses else ""
                order_sql = f" ORDER BY {', '.join([o.replace('\"', 't.\"', 1) for o in self._order_by])}" if self._order_by else ""

            sql = f"SELECT {cols} FROM {table_ref}{join_sql}{where_sql}{order_sql}{limit_sql};"
            cur.execute(sql, self._params)
            rows = cur.fetchall()
            return APIResponse([_normalizar_fila(self.tabla, dict(r)) for r in rows])

        elif self._action == "insert":
            items = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
            insertados = []
            conn.execute("BEGIN IMMEDIATE;")
            try:
                _ts_col_por_tabla = {
                    "movimientos_caja": "fecha",
                    "cierres_caja": "fecha_apertura",
                    "auditoria_movimientos": "fecha_hora",
                    "ventas": "fecha_hora",
                    "movimientos_cuenta": "fecha_hora",
                    "productos": "created_at",
                    "clientes": "created_at",
                    "categorias": "created_at",
                    "usuarios": "created_at",
                }
                for item in items:
                    if not item:
                        continue
                    item = dict(item)
                    ts_col = _ts_col_por_tabla.get(self.tabla)
                    if ts_col and ts_col not in item:
                        item[ts_col] = _ahora_iso()
                    cols = list(item.keys())
                    vals = [_serializar_campo(self.tabla, k, item[k]) for k in cols]
                    placeholders = ", ".join(["?"] * len(cols))
                    cols_str = ", ".join([f'"{c}"' for c in cols])
                    sql = f'INSERT INTO "{self.tabla}" ({cols_str}) VALUES ({placeholders});'
                    cur.execute(sql, vals)
                    row_id = cur.lastrowid
                    cur.execute(f'SELECT * FROM "{self.tabla}" WHERE id = ?;', (row_id,))
                    r = cur.fetchone()
                    insertados.append(_normalizar_fila(self.tabla, dict(r)) if r else {**item, "id": row_id})
                conn.execute("COMMIT;")
                _notificar_cambio_db(self.pool, f"Nuevo registro en {self.tabla}")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            return APIResponse(insertados)

        elif self._action == "upsert":
            items = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
            conf = self._upsert_conflict or "id"
            resultados = []
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for item in items:
                    if not item:
                        continue
                    cols = list(item.keys())
                    vals = [_serializar_campo(self.tabla, k, item[k]) for k in cols]
                    placeholders = ", ".join(["?"] * len(cols))
                    cols_str = ", ".join([f'"{c}"' for c in cols])
                    update_parts = [f'"{c}" = excluded."{c}"' for c in cols if c != conf]
                    update_str = ", ".join(update_parts)
                    sql = f'INSERT INTO "{self.tabla}" ({cols_str}) VALUES ({placeholders}) ON CONFLICT("{conf}") DO UPDATE SET {update_str};'
                    cur.execute(sql, vals)
                    cur.execute(f'SELECT * FROM "{self.tabla}" WHERE "{conf}" = ?;', (item[conf],))
                    r = cur.fetchone()
                    resultados.append(_normalizar_fila(self.tabla, dict(r)) if r else item)
                conn.execute("COMMIT;")
                _notificar_cambio_db(self.pool, f"Actualización en {self.tabla}")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            return APIResponse(resultados)

        elif self._action == "update":
            if not self._update_data:
                return APIResponse([])
            cols = list(self._update_data.keys())
            set_parts = [f'"{c}" = ?' for c in cols]
            vals = [_serializar_campo(self.tabla, c, self._update_data[c]) for c in cols]
            where_sql = f" WHERE {' AND '.join(self._where_clauses)}" if self._where_clauses else ""
            sql = f'UPDATE "{self.tabla}" SET {", ".join(set_parts)}{where_sql};'

            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur.execute(sql, vals + self._params)
                # Seleccionar los actualizados
                sel_sql = f'SELECT * FROM "{self.tabla}"{where_sql};'
                cur.execute(sel_sql, self._params)
                rows = cur.fetchall()
                conn.execute("COMMIT;")
                _notificar_cambio_db(self.pool, f"Modificación en {self.tabla}")
                return APIResponse([_normalizar_fila(self.tabla, dict(r)) for r in rows])
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        elif self._action == "delete":
            where_sql = f" WHERE {' AND '.join(self._where_clauses)}" if self._where_clauses else ""
            conn.execute("BEGIN IMMEDIATE;")
            try:
                sel_sql = f'SELECT * FROM "{self.tabla}"{where_sql};'
                cur.execute(sel_sql, self._params)
                rows = cur.fetchall()
                cur.execute(f'DELETE FROM "{self.tabla}"{where_sql};', self._params)
                conn.execute("COMMIT;")
                return APIResponse([_normalizar_fila(self.tabla, dict(r)) for r in rows])
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return APIResponse([])


def _serializar_campo(tabla: str, campo: str, valor: Any) -> Any:
    if isinstance(valor, bool):
        return 1 if valor else 0
    if campo == "detalles" and isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return valor


def _notificar_cambio_db(pool: ConnectionPool, motivo: str) -> None:
    try:
        from backup_drive import marcar_cambios_pendientes
        app_dir = pool.db_path.parent.parent
        marcar_cambios_pendientes(app_dir, motivo)
    except Exception:
        pass


def _normalizar_fila(tabla: str, fila: dict) -> dict:
    salida = dict(fila)
    for k, v in list(salida.items()):
        if k in ("activo", "usa_precio_manual", "auto_backup_activo"):
            if v is not None:
                salida[k] = bool(v)
        elif k == "detalles" and isinstance(v, str):
            try:
                salida[k] = json.loads(v)
            except Exception:
                salida[k] = {}
        elif k == "__join_clientes_nombre":
            nom = salida.pop("__join_clientes_nombre", None)
            salida["clientes"] = {"nombre": nom} if nom else None
        elif k == "__join_productos_codigo":
            cod = salida.pop("__join_productos_codigo", "")
            nom = salida.pop("__join_productos_nombre", "")
            if cod or nom:
                salida["productos"] = {"codigo": cod, "nombre": nom}
            else:
                salida["productos"] = None
    return salida


# ---------------------------------------------------------------------------
# Ejecutor de RPCs (Funciones de negocio equivalentes a PL/pgSQL)
# ---------------------------------------------------------------------------
class RpcQuery:
    def __init__(self, pool: ConnectionPool, name: str, params: dict):
        self.pool = pool
        self.name = name
        self.params = params

    def execute(self) -> APIResponse:
        conn = self.pool.get_connection()
        if self.name == "verificar_login":
            res = _rpc_verificar_login(conn, self.params)
        elif self.name == "listar_usuarios_login":
            res = _rpc_listar_usuarios_login(conn, self.params)
        elif self.name == "cambiar_pin":
            res = _rpc_cambiar_pin(conn, self.params)
            if isinstance(res, dict) and res.get("ok"):
                _notificar_cambio_db(self.pool, "PIN actualizado")
        elif self.name == "registrar_venta_atomica":
            res = _rpc_registrar_venta_atomica(conn, self.params)
            _notificar_cambio_db(self.pool, "Venta registrada")
        elif self.name == "registrar_pago_cuenta":
            res = _rpc_registrar_pago_cuenta(conn, self.params)
            _notificar_cambio_db(self.pool, "Cobro de cuenta corriente")
        elif self.name == "resetear_datos_comercio":
            res = _rpc_resetear_datos_comercio(conn, self.params)
            if isinstance(res, dict) and res.get("ok"):
                _notificar_cambio_db(self.pool, "Puesta a cero ejecutada")
        else:
            raise APIError({"code": "PGRST202", "message": f"Función RPC '{self.name}' no implementada"})
        return APIResponse(res)


# ---------------------------------------------------------------------------
# Implementaciones de las 6 Funciones RPC
# ---------------------------------------------------------------------------
def _rpc_listar_usuarios_login(conn: sqlite3.Connection, params: dict) -> list[dict]:
    cid = params.get("p_comercio_id", 1)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nombre, rol FROM usuarios WHERE comercio_id = ? AND activo = 1 ORDER BY (rol = 'admin') DESC, nombre ASC;",
        (cid,),
    )
    return [dict(r) for r in cur.fetchall()]


def _rpc_verificar_login(conn: sqlite3.Connection, params: dict) -> dict:
    uid = params.get("p_usuario_id")
    pin = (params.get("p_pin") or "").strip()

    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM usuarios WHERE id = ?;", (uid,))
        r = cur.fetchone()
        if not r or not r["activo"]:
            conn.execute("ROLLBACK;")
            return {"ok": False, "motivo": "usuario_invalido"}

        user = dict(r)
        ahora = datetime.now(TZ_AR)

        # Verificar bloqueo temporal
        if user.get("bloqueado_hasta"):
            try:
                bloq = datetime.fromisoformat(user["bloqueado_hasta"]).astimezone(TZ_AR)
                if bloq > ahora:
                    min_restan = math.ceil((bloq - ahora).total_seconds() / 60)
                    conn.execute("ROLLBACK;")
                    return {"ok": False, "motivo": "bloqueado", "minutos_bloqueo": max(1, min_restan)}
            except Exception:
                pass

        # PIN correcto
        if check_pin(pin, user.get("pin_hash", "")):
            cur.execute(
                "UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id = ?;",
                (uid,),
            )
            conn.execute("COMMIT;")
            return {
                "ok": True,
                "usuario": {
                    "id": user["id"],
                    "nombre": user["nombre"],
                    "rol": user["rol"],
                    "comercio_id": user["comercio_id"],
                    "debe_cambiar_pin": pin in ("1234", "0000"),
                },
            }

        # PIN incorrecto
        intentos = user.get("intentos_fallidos", 0) + 1
        if intentos >= 5:
            bloq_hasta = (ahora + timedelta(minutes=5)).isoformat()
            cur.execute(
                "UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = ? WHERE id = ?;",
                (bloq_hasta, uid),
            )
            conn.execute("COMMIT;")
            return {"ok": False, "motivo": "bloqueado", "minutos_bloqueo": 5}
        else:
            cur.execute(
                "UPDATE usuarios SET intentos_fallidos = ?, bloqueado_hasta = NULL WHERE id = ?;",
                (intentos, uid),
            )
            conn.execute("COMMIT;")
            return {"ok": False, "motivo": "pin_incorrecto", "intentos_restantes": 5 - intentos}
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def _rpc_cambiar_pin(conn: sqlite3.Connection, params: dict) -> dict:
    uid = params.get("p_usuario_id")
    actual = (params.get("p_pin_actual") or "").strip()
    nuevo = (params.get("p_pin_nuevo") or "").strip()

    if not re.match(r"^\d{4}$", nuevo):
        return {"ok": False, "motivo": "pin_invalido"}
    if nuevo in ("1234", "0000"):
        return {"ok": False, "motivo": "pin_inseguro"}

    ver = _rpc_verificar_login(conn, {"p_usuario_id": uid, "p_pin": actual})
    if not ver.get("ok"):
        motivo = "pin_actual_incorrecto" if ver.get("motivo") == "pin_incorrecto" else ver.get("motivo")
        return {"ok": False, "motivo": motivo, **{k: v for k, v in ver.items() if k not in ("ok", "motivo")}}

    nuevo_hash = hash_pin(nuevo)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET pin_hash = ? WHERE id = ?;", (nuevo_hash, uid))
        conn.execute("COMMIT;")
        return {"ok": True}
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def _rpc_registrar_venta_atomica(conn: sqlite3.Connection, params: dict) -> int:
    cid = params.get("p_comercio_id")
    cli_id = params.get("p_cliente_id")
    metodo = params.get("p_metodo_pago") or "Efectivo"
    total = float(params.get("p_total") or 0.0)
    items = params.get("p_items") or []
    puntos_ganados = float(params.get("p_puntos_ganados") or 0.0)

    if not items or not isinstance(items, list):
        raise APIError({"code": "SV002", "message": "El carrito está vacío."})

    total_calc = 0.0
    for it in items:
        pid = it.get("producto_id")
        cant = float(it.get("cantidad") or 0.0)
        pu = float(it.get("precio_unitario") or 0.0)
        cu = float(it.get("costo_unitario") or 0.0)
        if not pid or cant <= 0 or pu < 0 or cu < 0:
            raise APIError({"code": "SV002", "message": "Hay un ítem inválido en el carrito (producto, cantidad o precio incorrectos)."})
        total_calc += round(cant * pu, 2)

    if abs(total - total_calc) > 0.01:
        raise APIError({
            "code": "SV002",
            "message": f"El total informado ({fmt_moneda(total)}) no coincide con la suma de los renglones ({fmt_moneda(total_calc)})."
        })

    es_fiado = "cuenta corriente" in metodo.lower()
    if es_fiado and not cli_id:
        raise APIError({"code": "SV002", "message": "Para vender a Cuenta Corriente tenés que elegir un cliente."})

    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.cursor()

        # Agrupar por producto y validar stock
        cantidades_por_prod: Dict[int, float] = {}
        for it in items:
            p_id = int(it["producto_id"])
            cantidades_por_prod[p_id] = cantidades_por_prod.get(p_id, 0.0) + float(it["cantidad"])

        for p_id in sorted(cantidades_por_prod.keys()):
            cur.execute("SELECT id, nombre, stock_actual, activo FROM productos WHERE id = ? AND comercio_id = ?;", (p_id, cid))
            p_row = cur.fetchone()
            if not p_row or not p_row["activo"]:
                raise APIError({"code": "SV002", "message": f"El producto #{p_id} ya no existe en el catálogo."})

            stock_act = float(p_row["stock_actual"] or 0.0)
            pedido = cantidades_por_prod[p_id]
            if stock_act < pedido:
                raise APIError({
                    "code": "SV001",
                    "message": f"'{p_row['nombre']}': pediste {fmt_cantidad(pedido)} y hay {fmt_cantidad(stock_act)} en stock."
                })

            cur.execute("UPDATE productos SET stock_actual = stock_actual - ? WHERE id = ?;", (pedido, p_id))

        # Validar límite de crédito del cliente si es fiado
        if cli_id:
            cur.execute("SELECT id, nombre, saldo_deudor, limite_credito, activo, puntos FROM clientes WHERE id = ? AND comercio_id = ?;", (cli_id, cid))
            c_row = cur.fetchone()
            if not c_row or not c_row["activo"]:
                raise APIError({"code": "SV002", "message": "El cliente seleccionado no existe o está dado de baja."})

            if es_fiado:
                limite = float(c_row["limite_credito"] or 0.0)
                saldo = float(c_row["saldo_deudor"] or 0.0)
                if limite > 0 and (saldo + total) > limite:
                    raise APIError({
                        "code": "SV002",
                        "message": f"{c_row['nombre']} superaría su límite de crédito de {fmt_moneda(limite)}."
                    })

        # Insertar cabecera de venta
        ahora_iso = _ahora_iso()
        cur.execute(
            """INSERT INTO ventas (comercio_id, cliente_id, total_venta, forma_pago, fecha_hora, puntos_ganados)
               VALUES (?, ?, ?, ?, ?, ?);""",
            (cid, cli_id, total, metodo, ahora_iso, puntos_ganados),
        )
        venta_id = cur.lastrowid

        # Insertar detalle de venta
        for it in items:
            cur.execute(
                """INSERT INTO detalle_ventas (venta_id, producto_id, cantidad, precio_unitario, costo_unitario)
                   VALUES (?, ?, ?, ?, ?);""",
                (venta_id, int(it["producto_id"]), float(it["cantidad"]), float(it["precio_unitario"]), float(it["costo_unitario"])),
            )

        # Actualizar saldo y puntos de cliente
        if cli_id:
            if es_fiado:
                cur.execute(
                    "UPDATE clientes SET saldo_deudor = round(saldo_deudor + ?, 2), puntos = round(puntos + ?, 2) WHERE id = ?;",
                    (total, puntos_ganados, cli_id),
                )
                cur.execute(
                    """INSERT INTO movimientos_cuenta (comercio_id, cliente_id, tipo, monto, descripcion, ticket_id, fecha_hora)
                       VALUES (?, ?, 'CARGO', ?, ?, ?, ?);""",
                    (cid, cli_id, total, f"Compra según Ticket #{venta_id}", venta_id, ahora_iso),
                )
            else:
                cur.execute(
                    "UPDATE clientes SET puntos = round(puntos + ?, 2) WHERE id = ?;",
                    (puntos_ganados, cli_id),
                )

        conn.execute("COMMIT;")
        return venta_id
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def _rpc_registrar_pago_cuenta(conn: sqlite3.Connection, params: dict) -> dict:
    cid = params.get("p_comercio_id")
    cli_id = params.get("p_cliente_id")
    monto = float(params.get("p_monto") or 0.0)
    metodo = params.get("p_metodo") or "Efectivo"
    descripcion = (params.get("p_descripcion") or "").strip()
    usuario_nombre = params.get("p_usuario_nombre") or ""

    if monto <= 0:
        raise APIError({"code": "SV002", "message": "El monto del pago debe ser mayor a cero."})

    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nombre, saldo_deudor, activo FROM clientes WHERE id = ? AND comercio_id = ?;", (cli_id, cid))
        cli = cur.fetchone()
        if not cli or not cli["activo"]:
            raise APIError({"code": "SV002", "message": "El cliente seleccionado no existe."})

        saldo = round(float(cli["saldo_deudor"] or 0.0), 2)
        if saldo <= 0:
            raise APIError({"code": "SV002", "message": f"{cli['nombre']} no tiene deuda en Cuenta Corriente."})
        if round(monto, 2) > saldo:
            raise APIError({
                "code": "SV002",
                "message": f"El pago ({fmt_moneda(monto)}) supera la deuda actual ({fmt_moneda(saldo)})."
            })

        nuevo_saldo = round(saldo - round(monto, 2), 2)
        cur.execute("UPDATE clientes SET saldo_deudor = ? WHERE id = ?;", (nuevo_saldo, cli_id))

        ahora_iso = _ahora_iso()
        desc_mov = f"Pago a cuenta ({metodo})" + (f": {descripcion}" if descripcion else "")
        cur.execute(
            """INSERT INTO movimientos_cuenta (comercio_id, cliente_id, tipo, monto, descripcion, fecha_hora)
               VALUES (?, ?, 'PAGO', ?, ?, ?);""",
            (cid, cli_id, round(monto, 2), desc_mov, ahora_iso),
        )
        mov_id = cur.lastrowid

        if metodo == "Efectivo":
            cur.execute(
                """INSERT INTO movimientos_caja (comercio_id, tipo, monto, concepto, usuario_nombre, fecha)
                   VALUES (?, 'ingreso_extra', ?, ?, ?, ?);""",
                (cid, round(monto, 2), f"Cobro Cuenta Corriente - {cli['nombre']}", usuario_nombre, ahora_iso),
            )

        conn.execute("COMMIT;")
        return {"saldo": nuevo_saldo, "movimiento_id": mov_id}
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def _rpc_resetear_datos_comercio(conn: sqlite3.Connection, params: dict) -> dict:
    cid = params.get("p_comercio_id")
    modo = params.get("p_modo")
    uid = params.get("p_usuario_id")
    pin = (params.get("p_pin") or "").strip()

    if modo not in ("movimientos", "total"):
        return {"ok": False, "motivo": "modo_invalido"}

    login_res = _rpc_verificar_login(conn, {"p_usuario_id": uid, "p_pin": pin})
    if not login_res.get("ok"):
        return {"ok": False, "motivo": login_res.get("motivo", "pin_incorrecto")}

    user_info = login_res.get("usuario") or {}
    if user_info.get("rol") != "admin" or user_info.get("comercio_id") != cid:
        return {"ok": False, "motivo": "no_autorizado"}

    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.cursor()
        ahora_iso = _ahora_iso()
        res = {"ok": True, "modo": modo}

        # Archivar ventas
        cur.execute(
            "UPDATE ventas SET archivado_en = ?, archivado_por = ? WHERE comercio_id = ? AND archivado_en IS NULL;",
            (ahora_iso, uid, cid),
        )
        res["ventas"] = cur.rowcount

        # Archivar movimientos_cuenta
        cur.execute(
            "UPDATE movimientos_cuenta SET archivado_en = ?, archivado_por = ? WHERE comercio_id = ? AND archivado_en IS NULL;",
            (ahora_iso, uid, cid),
        )
        res["movimientos_cuenta"] = cur.rowcount

        # Archivar movimientos_caja
        cur.execute(
            "UPDATE movimientos_caja SET archivado_en = ?, archivado_por = ? WHERE comercio_id = ? AND archivado_en IS NULL;",
            (ahora_iso, uid, cid),
        )
        res["movimientos_caja"] = cur.rowcount

        # Archivar cierres_caja
        cur.execute(
            "UPDATE cierres_caja SET archivado_en = ?, archivado_por = ? WHERE comercio_id = ? AND archivado_en IS NULL;",
            (ahora_iso, uid, cid),
        )
        res["cierres_caja"] = cur.rowcount

        # Resetear saldo y puntos
        cur.execute("UPDATE clientes SET saldo_deudor = 0, puntos = 0 WHERE comercio_id = ?;", (cid,))
        res["saldos_reseteados"] = cur.rowcount

        # Modo total: dar de baja catálogo
        if modo == "total":
            cur.execute(
                "UPDATE productos SET activo = 0, fecha_baja = ? WHERE comercio_id = ? AND activo = 1;",
                (ahora_iso, cid),
            )
            res["productos"] = cur.rowcount

            cur.execute(
                "UPDATE categorias SET activo = 0, fecha_baja = ? WHERE comercio_id = ? AND activo = 1;",
                (ahora_iso, cid),
            )
            res["categorias"] = cur.rowcount

            cur.execute(
                "UPDATE clientes SET activo = 0, fecha_baja = ? WHERE comercio_id = ? AND activo = 1;",
                (ahora_iso, cid),
            )
            res["clientes"] = cur.rowcount

        conn.execute("COMMIT;")
        return res
    except Exception:
        conn.execute("ROLLBACK;")
        raise


# ---------------------------------------------------------------------------
# Seguridad y Alertas
# ---------------------------------------------------------------------------
def registrar_alerta_seguridad(comercio_id: int, tipo: str, ip_origen: str, usuario_afectado: str, detalles: str, pool: Optional[ConnectionPool] = None) -> int:
    """Registra un evento de seguridad (fuerza bruta, bloqueo, etc.)."""
    conn = pool.get_connection() if pool else ConnectionPool._instance.get_connection() if hasattr(ConnectionPool, "_instance") else None
    if conn is None:
        return 0
    try:
        cur = conn.cursor()
        ahora = _ahora_iso()
        cur.execute(
            """INSERT INTO alertas_seguridad (comercio_id, tipo, ip_origen, usuario_afectado, detalles, fecha_hora, leida)
               VALUES (?, ?, ?, ?, ?, ?, 0);""",
            (comercio_id, tipo, ip_origen, usuario_afectado, detalles, ahora),
        )
        conn.commit()
        return cur.lastrowid or 0
    except Exception as e:
        log.warning("No se pudo registrar alerta de seguridad: %s", e)
        return 0


def listar_alertas_seguridad(comercio_id: int = 1, solo_no_leidas: bool = True, limite: int = 20, pool: Optional[ConnectionPool] = None) -> List[dict]:
    """Lista eventos de seguridad."""
    conn = pool.get_connection() if pool else ConnectionPool._instance.get_connection() if hasattr(ConnectionPool, "_instance") else None
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        if solo_no_leidas:
            cur.execute(
                """SELECT * FROM alertas_seguridad WHERE comercio_id = ? AND leida = 0 ORDER BY id DESC LIMIT ?;""",
                (comercio_id, limite),
            )
        else:
            cur.execute(
                """SELECT * FROM alertas_seguridad WHERE comercio_id = ? ORDER BY id DESC LIMIT ?;""",
                (comercio_id, limite),
            )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def descartar_alerta_seguridad(comercio_id: int, alerta_id: int, pool: Optional[ConnectionPool] = None) -> bool:
    """Marca una alerta de seguridad como leída."""
    conn = pool.get_connection() if pool else ConnectionPool._instance.get_connection() if hasattr(ConnectionPool, "_instance") else None
    if conn is None:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE alertas_seguridad SET leida = 1 WHERE comercio_id = ? AND id = ?;""",
            (comercio_id, alerta_id),
        )
        conn.commit()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cliente Principal LocalDBClient
# ---------------------------------------------------------------------------
class LocalDBClient:
    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path is None:
            if getattr(sys, "frozen", False):
                base_dir = Path(sys.executable).resolve().parent
            else:
                base_dir = Path(__file__).resolve().parent
            datos_dir = base_dir / "datos"
            datos_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = datos_dir / "pos_local.db"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.pool = ConnectionPool(self.db_path)
        # Inicializar esquema
        conn = self.pool.get_connection()
        _crear_esquema(conn)

    def table(self, nombre: str) -> TableQuery:
        return TableQuery(self.pool, nombre)

    def rpc(self, name: str, params: Optional[dict] = None) -> RpcQuery:
        return RpcQuery(self.pool, name, params or {})

    def close(self) -> None:
        self.pool.close()
