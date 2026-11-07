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
2. **`usuarios`**: Roles (`admin` para Dueño, `cajero` para Empleados), hash SHA-256 de PIN de 4 dígitos.
3. **`categorias`**: Rubros de productos con orden y color identificador.
4. **`productos`**: Catálogo con código de barras, precios (costo y venta en ARS $), stock actual y mínimo, e imagen.
5. **`clientes`**: Padrón con límite de crédito, saldo en cuenta corriente y puntos de fidelización.
6. **`ventas`** y **`ventas_items`**: Historial transaccional con desglose por medio de pago (efectivo, digital, cta cte, mixto).
7. **`movimientos_caja`** y **`cierres_caja`**: Libro de caja chica y cierres diarios con diferencias y auditoría.
8. **`auditoria_movimientos`**: Registro inmutable de operaciones sensibles (anulaciones, cambios de precio).

---

## 3. Motor PWA y Almacenamiento en Celular (Modo A)

En clientes sin computadora, la app corre de forma estática servida desde GitHub Pages o Netlify:

1. **`sw.js` (Service Worker en Raíz):**
   - Resuelve su scope dinámicamente (`self.location.pathname`).
   - Almacena en caché los archivos HTML, vendors JS e íconos SVG para arranque instantáneo sin conexión.
   - No intercepta llamadas `/api/` (las deriva a `motor_offline_pwa.js`).

2. **Compresión Quirúrgica de Imágenes en Canvas:**
   - Para evitar saturar los 5 MB de `localStorage` o ahogar la memoria del navegador con fotos de 8 MB sacadas con la cámara:
   - Toda imagen subida pasa por la función `comprimirImagenEnCanvas(file, maxLado = 400)`:
     - Escala proporcional a un máximo de **400x400 px**.
     - Renderiza sobre `<canvas>` con compresión JPEG al 75%.
     - Resultado: imágenes de **15 KB a 35 KB** (reducción > 99%).
   - Se almacenan de forma segura en `IndexedDB` en el almacén `carpeta_datos`.

3. **Sincronización con Google Drive:**
   - `google_drive_script_puente.gs` corre en la cuenta del usuario como Webhook `POST`.
   - Recibe el JSON comprimido con la base de datos de la app y genera el archivo `datos_backup_actual.zip` en la carpeta `Backups_POS_Celular` de su Drive.

---

## 4. Protocolo WhatsApp (Cero Rombos - Cero Emojis)

Siguiendo la regla de oro del archivo maestro **`GEMINI.md`**, para evitar que los celulares decodifiquen rombos negros corruptos con signos de pregunta ():

* **Estrictamente prohibido:** Emojis Unicode de 4 bytes (`👤`, `👉`, `📱`, `✅`, `💵`, etc.) en mensajes generados.
* **Formato permitido:**
  - Viñetas estándar: `• `
  - Negrita nativa: `*texto*`
  - Protocolo de destino: `https://api.whatsapp.com/send?phone=...&text=...` (o sin `phone=` para selector).
  - Codificación obligatoria con `encodeURIComponent()` en JavaScript y `urllib.parse.quote()` en Python.
  - Normalizador de número argentino: `formatArgentineWhatsApp()` (prefijo `549` + característica sin `0` y número sin `15`).

---

## 5. Pipeline de Compilación y Despliegue en GitHub Pages

El script `generar_pwa_estatica.py` genera la carpeta **`dist/`**:

```powershell
# Compilar frontend estático hacia dist/
python generar_pwa_estatica.py

# Compilar y desplegar automáticamente a la rama gh-pages con GitHub CLI
python generar_pwa_estatica.py --deploy
```

* El archivo `.nojekyll` asegura que GitHub Pages no ignore assets estáticos.
* Los archivos `_redirects` y `_headers` aseguran compatibilidad con Netlify y Cloudflare Pages.
