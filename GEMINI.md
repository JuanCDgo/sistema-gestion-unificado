# Instrucciones de Sistema y Flujo de Trabajo Maestro (Juan)

> **REGLA DE ORO DE ARCHIVO MAESTRO (SSOT):**  
> Este archivo (`C:\Users\juanm\.gemini\antigravity\scratch\GEMINI.md`) es el **ARCHIVO PRINCIPAL Y FUENTE ÚNICA DE LA VERDAD**.  
> Cada vez que se agregue, refine o modifique cualquier regla, comportamiento o checklist en cualquier subcarpeta o proyecto secundario, **ESTE ARCHIVO RAÍZ TIENE QUE SER MODIFICADO SÍ O SÍ EN EL MISMO ACTO**, manteniéndose siempre sincronizado y actualizado.

---

## 1. Dinámica de Comunicación
- **Pensar antes de teclear:** No tires código apenas te pida algo. Primero charlemos la idea, definamos la lógica y recién cuando estemos de acuerdo, pasamos al código.
- **Cero complacencia:** Si le estoy pifiando a algo, tomo una mala decisión técnica o me mando una macana, frename el carro en seco y corregime sin filtro. No me des la razón para quedar bien.
- **Proactividad:** Sugerí mejoras, alternativas más eficientes y anticipá posibles problemas.

---

## 2. Definición y Documentación
- **Foco de la App:** Antes de empezar cualquier aplicación, definamos primero para qué es y a quién va destinada.
- **Documentación doble:** Cada proyecto debe incluir dos manuales al finalizar: uno técnico para mí (mantenimiento, arquitectura, BD) y un manual de usuario final.

---

## 3. Código y Ejecución
- **Cambios quirúrgicos:** Cuando arregles o corrijas un script, pasame SOLAMENTE la función, clase o bloque modificado. Nunca vuelvas a imprimir el archivo entero.
- **Seguridad primero:** Antes de aplicar cambios grandes o refactorizaciones, hacé el paso (o recordame) de crear una copia de seguridad o un commit del estado actual (rama local `backup-pre-...`).
- **Formatos Argentinos:** Manejar siempre formato de hora argentina (GTM-3, reloj de 24hs) y moneda local (ARS $, usando punto para miles y coma para decimales).
- **UI Limpia y Sin Redundancias:** No mostrar badges o etiquetas obvias de "al día" en las tarjetas de clientes/pacientes. Estar al día es el estado normal; solo mostrar badge destacado cuando exista deuda pendiente (con monto ARS $ y acceso rápido a cobro QR).
- **Despliegue Autónomo en GitHub Pages:** Siempre que se cree o modifique una app, inicializar el repo y subirlo automáticamente con el CLI de GitHub (`gh` o `git push`) a la cuenta `JuanCDgo`, habilitando GitHub Pages para que quede disponible con HTTPS, lista para instalar como PWA y compartir en celulares sin requerir pasos manuales de Juan.
- **Cero Rombos en WhatsApp (PROHIBIDO EL USO DE EMOJIS EN MENSAJES):** Para evitar que en WhatsApp Web o celulares aparezcan rombos negros con signo de pregunta (), los textos generados para enviar por WhatsApp tienen **ESTRICTAMENTE PROHIBIDO** el uso de emojis (👤, 👉, 📱, 👋, etc.). Los navegadores y Windows Desktop suelen decodificar caracteres UTF-8 de 4 bytes de forma corrupta. Formato 100% libre de rombos: usar viñetas estándar (`•`), asteriscos de WhatsApp para negrita (`*Texto en negrita*`), enlaces directos con protocolo `https://`, codificación obligatoria con `encodeURIComponent()` y destino `https://api.whatsapp.com/send`.

---

## 4. Mejora Continua
- **Regla del Archivo GEMINI Principal:** `C:\Users\juanm\.gemini\antigravity\scratch\GEMINI.md` es el núcleo maestro de todas las reglas. Nunca dejar cambios aislados en un subdirectorio sin replicarlos en este archivo raíz.
- **Reglas editables:** Estas instrucciones son dinámicas y evolutivas. Si durante el desarrollo vemos que hace falta cambiar nuestra forma de trabajar, proponé actualizar este archivo para dejar la regla asentada.

---

## 5. Checklist Obligatorio para TODAS las Mini Apps
Cada aplicación de la suite debe incluir sin excepción:
1. **Identidad del Negocio (Personalizable):** Cargar Logo (subir imagen desde celular/PC comprimida a base64) y Nombre del Negocio editable, reflejado en el header, tarjeta de cliente y mensajes.
2. **Cobro QR Robusto:** Generación dinámica con Alias/CBU y fallback garantizado (para que nunca quede un cuadro blanco vacío) con llamada al abrir el modal.
3. **UI Limpia y Responsive:** Cero badges redundantes de "al día" (solo badge rojo con monto si debe). Filtros y etiquetas adaptables sin cortes de texto ("bici", etc.).
4. **Tarifas y Servicios Editables:** CRUD para cambiar precios en ARS $, agregar y borrar ítems.
5. **Avisos por WhatsApp en 1 toque (Cero Rombos - Cero Emojis en el Mensaje):** Mensajes con formato nativo de WhatsApp (*negrita*, viñetas •) **SIN NINGÚN EMOJI** para garantizar cero rombos (), desglose de precios coherente, transferencia directa al Alias de Mercado Pago / Banco (sin comisiones) y link directo al pase digital interactivo con protocolo `https://api.whatsapp.com/send` y `formatArgentineWhatsApp()`.
6. **Fidelidad y Pase Seguro:** Vista cliente pública accesible sin permisos de administración del taller.
7. **Documentación & Respaldos:** MANUAL_USUARIO.md, MANUAL_TECNICO.md, exportar/importar JSON, GitHub Pages y copia en Google Drive.
8. **Turnero en la Nube con Bloqueo Real y Control Manual del Dueño:** En toda app con reserva de turnos, los horarios tomados desde cualquier teléfono se bloquean automáticamente en la nube, aparecen en gris claro (`🔒 Ocupado` y deshabilitados) para los demás celulares con freno de colisión, y el dueño cuenta con el panel `📅 Agenda` para ocupar, liberar o cerrar días enteros manualmente con 1 toque.
9. **Estándar Visual Anti-VibeCoding (Diseño Profesional Real):** Cero clichés de IA genérica:
   - Nada de degradados agresivos ni el quemado violeta/negro en todo: paleta de color con identidad propia del rubro (ej. clínico/sobrio en salud, madera/ámbar en barbería, industrial en gomería y fábrica).
   - Sin abuso de emojis en la interfaz: solo donde aporten claridad funcional (categorías o estados clave), nunca de adorno redundante en cada renglón.
   - Sin sombras gigantes ni glassmorphism exagerado: bordes sutiles de 1px (`border-zinc-800`), acabados mate y contraste limpio.
   - Copys directos y reales: sin frases pretenciosas ("No es X, es Y"). Lenguaje operativo, claro y argentino.
10. **Despliegue y PWA:** Repositorio en GitHub sincronizado (`JuanCDgo/<app>`), GitHub Pages activo con HTTPS y manifiesto para instalación como aplicación web de pantalla completa en celular.
