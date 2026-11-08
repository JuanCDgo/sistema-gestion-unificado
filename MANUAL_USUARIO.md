# Manual de Usuario Final — Sistema de Gestión Unificado (Next-Gen)

Bienvenido al **Sistema de Gestión Unificado**. Este sistema fue diseñado para resolver el control de ventas, stock, promos, caja chica, empleados y fiados de tu comercio de forma rápida, moderna y sin costos de mantenimiento.

---

## 1. ¿Cómo querés usar el sistema? (Elegí tu modo)

El sistema cuenta con **dos modos de uso** según tu negocio:

| Característica | Modo A: Celular Autónomo (PWA) | Modo B: Servidor Central LAN |
| :--- | :--- | :--- |
| **¿Para quién es?** | Negocios unipersonales, feriantes o comercios sin computadora. | Locales con computadora principal en caja y celulares en salón/depósito. |
| **¿Dónde se instala?** | Directo en el celular desde el link web con 1 toque. | En la PC principal con doble clic en `Iniciar_Servidor.bat`. |
| **Conexión** | 100% sin internet (funciona hasta en Modo Avión). | En la red Wi-Fi de tu negocio (no consume datos móviles). |
| **Base de datos** | Guardada en la memoria interna de tu celular. | Guardada en la PC y sincronizada al segundo en todos los celulares. |
| **Respaldo** | Automático a tu Google Drive al conectarse al Wi-Fi. | Copia de seguridad en la PC y en Google Drive al cierre. |

---

## 2. Asistente de Primer Arranque (Setup Wizard) y Gestión de Cajeros

La primera vez que abras `Iniciar_Servidor.bat` (o la app nueva), antes de entrar verás un **Asistente de Bienvenida de 4 pasos**:

1. **Modo de Operación (Modo A o Modo B) o Migración desde Celular:**
   - Elegís si vas a trabajar en **Modo B (Servidor Central PC + Celulares Wi-Fi)** o **Modo A (Celular Autónomo)**.
   - Si ya venías usando el **Modo A en tu celular** y compraste una PC, desde este mismo Paso 1 podés subir el archivo de respaldo de tu celular y pasar al **Modo B** dejando todo idéntico.
2. **Identidad del Negocio:**
   - Nombre de tu comercio, rubro, teléfono de WhatsApp y carga del **Logo** de tu negocio.
3. **Cobro por Transferencia y QR:**
   - Ingresás tu **Alias** de Mercado Pago o Banco y CBU/CVU para cobrar sin comisiones en el mostrador.
4. **Clave de Dueño y Equipo de Cajeros / Empleados:**
   - Definís tu **PIN de 4 números de Dueño** (acceso total).
   - Creás todos los **Cajeros / Empleados** que trabajen en el negocio (`Cajero 1`, `Cajero 2`, `Cajero 3` o sus nombres reales como `Franco`, `Lucas`, `Micaela`) cada uno con su propia clave de 4 números.
   - **Reinicio Automático al Login por Seguridad:** Al guardar el asistente (o cuando el Dueño agrega/edita cajeros en **Configuración -> Equipo de Trabajo** y toca **"Guardar Cajeros y Reiniciar al Login"**), el sistema cierra la sesión de Dueño y vuelve automáticamente a la pantalla de Login para que el empleado o dueño de turno ingrese con su propio PIN.

---

## 3. Cómo Pasar de Modo A (Celular) a Modo B (PC Nueva) sin Perder Nada

Si arrancaste trabajando solo con tu celular en **Modo A** y más adelante sumaste una PC en el mostrador para trabajar en **Modo B**:

1. **En tu Celular (Modo A):** Entrá a **Configuración -> Copias de Seguridad** y tocá **"Descargar Copia Completa"** (o descargá la última copia automática desde la carpeta `Backups_POS_Celular` de tu Google Drive).
2. **En la PC Nueva (Modo B):** Abrí `Iniciar_Servidor.bat`.
   - En el **Paso 1 del Asistente de Bienvenida** (o dentro de **Configuración -> Restaurar Copia o Migrar desde Modo A**), seleccioná el archivo descargado del celular (`.json` o `.zip`) y tocá **"Pasar a Modo B Ahora"**.
3. **Resultado 100% Idéntico:** La PC importa automáticamente tu nombre, logo, Alias/CBU, el Dueño y todos los Cajeros con sus claves, todas las categorías, productos, promos editadas, stock actual, clientes con sus deudas de cuenta corriente y el historial de ventas y caja.
4. **Conectá los Celulares por Wi-Fi:** Al terminar de importar, la pantalla vuelve al Login y te muestra el **Código QR de Red Wi-Fi**. Escanealo con la cámara de tu celular (y los de tus empleados) para que todos trabajen conectados en tiempo real a la PC.

---

## 4. Uso Diario en el Mostrador

### A. Ventas, Promos y Punto de Venta (POS)
- **Selector Superior (Promos y Combos vs Productos Sueltos):**
  - **Promos y Combos:** Ideal para venta ágil de combos de bebidas (Fernet + Coca, Smirnoff + Speed/Monster, Gin + Tónica, Packs x6 de Cerveza, etc.).
  - **Promos Fijas en 1 toque:** Tocás **`+ Sumar`** y el combo entra directo al carrito descontando automáticamente todas las botellas individuales del stock (ej: `-1 Fernet Branca 1lt` y `-2 Coca-Cola 2,25L`).
  - **Promos con Elección (Opción A):** Si la promo permite elegir sabor o gaseosa (ej: *Sprite o Coca 2,25L*, o *Smirnoff Clásico vs Tropical +$ 500*), al tocar **`+ Sumar`** se abre una ventana rápida para tocar con el dedo qué botella lleva el cliente. Descuenta la botella exacta elegida y suma el adicional si corresponde.
  - **Botón rápido de Envío:** Contás con el botón **`+ Envío $1.500`** en 1 toque y aviso visual de compra mínima sugerida (`$ 7.000`).
- **Búsqueda rápida:** Podés escribir parte del nombre, marca o pasar el lector de código de barras.
- **Cobro en 1 toque:**
  - Efectivo (calcula el vuelto exacto).
  - Mercado Pago / Transferencia (muestra el QR con tu Alias al instante).
  - Cuenta Corriente / Fiado (asigna la deuda al cliente seleccionado).
- **Comprobante por WhatsApp:**
  - Al finalizar la venta, tocás el botón de WhatsApp y se abre el mensaje con el desglose del ticket (*formato 100% limpio sin caracteres extraños ni rombos*).

### B. Inventario y Promos 100% Editables
- **Edición total de Productos Sueltos:** Podés crear, editar precios, costos, stock, código o dar de baja cualquier bebida o artículo de kiosco.
- **Edición total de Promos y Combos:** Desde la pestaña **`Promos y Combos`** en Inventario (o tocando el ícono de lápiz en cualquier tarjeta del mostrador), podés:
  - Cambiar el precio de venta de una promo al instante.
  - Editar el nombre, categoría o descripción.
  - Agregar o quitar qué botellas fijas descuenta del stock y en qué cantidad.
  - Agregar o quitar grupos de elección (Opción A) y definir recargos opcionales (`+$ 500`, etc.).
- **Fotos de productos:** Podés sacar fotos con la cámara del celular. El sistema las comprime a menos de 35 KB para que el catálogo sea ultra veloz y no sature la memoria.
- **Semáforo de stock:** Los productos con stock bajo aparecen en rojo y las promos calculan automáticamente cuántos combos podés armar según el stock real de sus botellas.

### C. Clientes y Cuentas Corrientes (Fiados)
- **Cero etiquetas innecesarias:** Estar al día es el estado normal de un cliente.
- **Control de deuda:** Solo verás un cartel destacado en rojo cuando un cliente tenga saldo pendiente (`Debe: $ 15.400,00`).
- **Aviso de deuda en 1 toque:** Tocando el icono de WhatsApp en la ficha del cliente, le enviás el saldo exacto junto a tu Alias/CBU para que te transfiera.

### D. Caja y Cierre Diario
- **Apertura de caja:** Registrá el cambio inicial al empezar la jornada.
- **Ingresos y Egresos:** Anotá pagos a proveedores, gastos diarios o retiros del dueño (queda registrado qué usuario o cajero hizo cada movimiento).
- **Cierre Diario Oficial:** Al terminar el día, el sistema cuenta las ventas, calcula el efectivo esperado y te muestra si la caja está exacta o si hay diferencias. Podés descargar el comprobante en PDF o mandarlo por WhatsApp/Email.
