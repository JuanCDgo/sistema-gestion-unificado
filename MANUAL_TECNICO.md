# Manual Técnico de Arquitectura y Mantenimiento — Sistema de Gestión Unificado

Este documento detalla la arquitectura interna, base de datos, flujo de datos y protocolos del **Sistema de Gestión Unificado (Next-Gen)**.

---

## 1. Arquitectura General y Patrón Híbrido

El sistema opera bajo un patrón **Offline-First / Local-First Unificado**:

```
+-------------------------------------------------------------------------+
|                              FRONTEND                                   |
|   Tailwind CSS (Stand-alone) · Alpine.js 3.14 · Lucide Icons (Local)    |
|   Templates Jinja2 unificados (mismo DOM para Desktop y Celular)        |
+------------------------------------+------------------------------------+
                                     |
                                     v
                   ¿Modo de Operación Configurado?
                                     |
               +---------------------+---------------------+
               |                                           |
               v                                           v
    [MODO A: PWA OFFLINE]                       [MODO B: SERVIDOR LAN]
  - motor_offline_pwa.js                      - FastAPI (Python 3.10+)
  - Intercepta window.fetch('/api/...')       - SQLite 3 (Modo WAL activo)
  - Almacenamiento: IndexedDB + LocalStorage   - Red Wi-Fi: servidor_red.py
  - Compresión Canvas: Max 400px (<35 KB)     - Fotos en disco: datos/uploads/
  - Backup: Google Apps Script Webhook        - Hot-backup: sqlite3 backup API
```

---

## 2. Base de Datos SQLite (Modo Centralizado B)

El motor de persistencia en PC es SQLite 3 gestionado a través de `local_db.py`:

### Configuración de Concurrencia
* **`PRAGMA journal_mode = WAL;`**: Escrituras y lecturas concurrentes sin bloqueo de tabla.
* **`PRAGMA synchronous = NORMAL;`**: Alto rendimiento de escritura protegiendo la integridad del archivo.
* **`PRAGMA busy_timeout = 5000;`**: Espera de hasta 5 segundos para liberar cerraduras antes de rechazar transacciones.

### Esquema Principal de Tablas
1. **`configuracion_negocio`**: Identidad, colores, alias/CBU, PINs, modo de operación (`autonomo_offline` / `servidor_central`) y estado del wizard (`wizard_completado`).
2. **`usuarios`**: Roles (`admin` para Dueño, `cajero` para Empleados), múltiples cajeros (`Cajero 1`, `Cajero 2`, `Cajero 3` o nombres personalizados) con hash PBKDF2-HMAC-SHA256 de PIN de 4 dígitos (`hash_pin`).
3. **`categorias`**: Rubros de productos con orden y estado activo/baja.
4. **`productos`**: Catálogo con código de barras, precios (costo y venta en ARS $), stock actual y mínimo, e imagen.
5. **`clientes`**: Padrón con límite de crédito, saldo en cuenta corriente y puntos de fidelización.
6. **`ventas`** y **`detalle_ventas`**: Historial transaccional con desglose por medio de pago (efectivo, digital, cta cte, mixto).
7. **`promos`**: Combos y promociones compuestas (`fijos_json` para componentes fijos que se descuentan siempre y `elecciones_json` para grupos de elección Opción A con recargo opcional).
8. **`movimientos_caja`** y **`cierres_caja`**: Libro de caja chica y cierres diarios con diferencias y auditoría.
9. **`auditoria_movimientos`**: Registro inmutable de operaciones sensibles (anulaciones, cambios de precio, alta/edición de promos, alta/baja de cajeros).

### Motor de Promos y Descuento Atómico de Componentes
* **Semilla inicial (`catalogo_bebidas.py`)**: Contiene las 13 categorías, 261 productos de bebidas/kiosco y 75 promos/packs. Se siembra automáticamente en `local_db.py` (`sembrar_catalogo_bebidas`) y en `motor_offline_pwa.js` (`sembrarCatalogoBebidasEnDB`) si la base está vacía, siendo 100% editable vía CRUD (`GET/POST/PUT/PATCH/DELETE /api/promos`).
* **Descomposición exacta en `POST /api/ventas`**: Cuando se vende una Promo (fija o con elección resuelta en el modal Opción A), el POS descompone el combo en sus `producto_id` reales y distribuye el precio de la promo proporcionalmente mediante `precio_unitario_pactado` (absorbiendo cualquier centavo de redondeo en la última unidad).

### Setup Wizard en `/login`, Gestión de Múltiples Cajeros y Migrador Modo A -> Modo B
* **Setup Wizard en Primer Arranque (`templates/login.html` + `templates/_wizard.html`)**:
  - Cuando `wizard_completado == 0` (consultado vía `GET /api/setup-wizard/estado`), el asistente se despliega inmediatamente en `/login` antes de pedir credenciales.
  - En el Paso 4 permite definir el PIN del Dueño y crear múltiples empleados/cajeros (`cajeros: [{nombre, pin}, ...]`).
  - Al finalizar (`POST /api/setup-wizard/completar`) o al guardar cajeros en Configuración (`guardarYReiniciarLoginCajeros()`), se limpian todas las sesiones activas (`SESIONES_INFO.clear()` y `/api/logout`) y se redirige a `/login` para que ningún cajero opere dentro de la sesión del Dueño.
* **Migración Transparente de Modo A (Celular) a Modo B (PC SQLite) (`backup_drive.py`)**:
  - La función `importar_snapshot_json_en_sqlite(app_dir, backup_data, forzar_modo_b=True)` y `restaurar_datos_desde_zip()` aceptan tanto archivos `.json` exportados desde `motor_offline_pwa.js` (Modo A) como archivos `.zip`.
  - Migra atómicamente `configuracion_negocio` (activando `modo_operacion = 'servidor_central'` y `wizard_completado = 1`), `usuarios` (convirtiendo PINs planos de 4 dígitos a `hash_pin` PBKDF2), `categorias`, `productos`, `promos` (serializando `fijos` y `elecciones` a `fijos_json` y `elecciones_json`), `clientes`, `ventas`, `detalle_ventas`, `movimientos_caja`, `movimientos_cuenta` y `cierres_caja`.

---

## 3. Motor PWA y Almacenamiento en Celular (Modo A)

En clientes sin computadora, la app corre de forma estática servida desde GitHub Pages:

1. **`sw.js` (Service Worker en Raíz):**
   - Resuelve su scope dinámicamente (`self.location.pathname`).
   - Almacena en caché los archivos HTML, vendors JS e íconos SVG para arranque instantáneo sin conexión.
   - No intercepta llamadas `/api/` (las deriva a `motor_offline_pwa.js`).

2. **Compresión Quirúrgica de Imágenes en Canvas:**
   - Toda imagen subida pasa por `comprimirImagenEnCanvas(file, maxLado = 400)` reduciéndola a 15 KB–35 KB y almacenándola en `IndexedDB` (`carpeta_datos`).

3. **Sincronización con Google Drive:**
   - `armarSnapshotJSON(db)` incluye `config`, `usuarios` y todas las `tablas` (incluyendo `promos` y `cierres_caja`).

---

## 4. Protocolo WhatsApp (Cero Rombos - Cero Emojis)

Siguiendo la regla de oro del archivo maestro **`GEMINI.md`**, para evitar que los celulares decodifiquen rombos negros corruptos con signos de pregunta ():

* **Estrictamente prohibido:** Emojis Unicode de 4 bytes en mensajes generados para WhatsApp.
* **Formato permitido:** Viñetas estándar (`• `), negrita nativa (`*texto*`), protocolo `https://api.whatsapp.com/send`, `encodeURIComponent()` y `formatArgentineWhatsApp()`.

---

## 5. Pipeline de Compilación y Despliegue en GitHub Pages

El script `generar_pwa_estatica.py` genera la carpeta **`dist/`** y despliega en GitHub Pages:

```powershell
# Compilar frontend estático hacia dist/
python generar_pwa_estatica.py

# Compilar y desplegar automáticamente a la rama gh-pages
python generar_pwa_estatica.py --deploy
```
