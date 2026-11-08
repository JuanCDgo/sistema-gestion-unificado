# Manual de Usuario Final — Sistema de Gestión Unificado (Next-Gen)

Bienvenido al **Sistema de Gestión Unificado**. Este sistema fue diseñado para resolver el control de ventas, stock, caja chica y fiados de tu comercio de forma rápida, moderna y sin costos de mantenimiento.

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

## 2. Asistente de Primer Arranque (Setup Wizard)

La primera vez que abras el sistema verás un asistente guiado de 4 pasos:

1. **Identidad del Negocio:**
   - Nombre de tu comercio, rubro y teléfono de contacto.
   - Podés subir el logo de tu negocio (el sistema lo comprime automáticamente para que no ocupe espacio).
2. **Modo de Operación:**
   - Seleccionás **Modo A (Celular)** o **Modo B (PC + Wi-Fi)**.
3. **Cobro por Transferencia y QR:**
   - Ingresás tu **Alias** de Mercado Pago o Banco (ej: `kiosco.central.mp`).
   - El sistema genera el QR automáticamente para cobrar sin comisiones en el mostrador.
4. **Seguridad y PIN:**
   - Creás tu propio **PIN privado de 4 dígitos** para ingresar como Dueño.
   - Opcionalmente podés dejar el PIN para empleados/cajeros (`0000`).

---

## 3. Uso Diario en el Mostrador

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
- **Ingresos y Egresos:** Anotá pagos a proveedores, gastos diarios o retiros del dueño.
- **Cierre Diario Oficial:** Al terminar el día, el sistema cuenta las ventas, calcula el efectivo esperado y te muestra si la caja está exacta o si hay diferencias. Podés descargar el comprobante en PDF o mandarlo por WhatsApp/Email.

---

## 4. Cómo Conectar Celulares a la PC (Modo B)

1. En la PC, hacé doble clic en **`Iniciar_Servidor.bat`**.
2. Tocá el botón **"Conectar Celular"** en la barra superior.
3. Con el celular conectado al mismo Wi-Fi del negocio, abrí la cámara y apuntá al código QR que se muestra en la pantalla de la PC.
4. En Chrome del celular, tocá los 3 puntos arriba a la derecha y elegí **"Agregar a la pantalla principal"** o **"Instalar aplicación"**.
5. ¡Listo! El celular ahora funciona como una terminal más conectada a la PC.

---

## 5. Copias de Seguridad (Google Drive)

- **En Celular (Modo A):** El sistema detecta cuando te conectás al Wi-Fi y sube una copia de seguridad a tu Google Drive sin que tengas que hacer nada.
- **En PC (Modo B):** Podés descargar un archivo `.zip` con todos tus datos desde **Configuración -> Copia de Seguridad** o configurar el enlace automático a Google Drive.
