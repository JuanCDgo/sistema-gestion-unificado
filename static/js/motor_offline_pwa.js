/**
 * Motor Autónomo Offline (PWA) para Celular — Control de Stock y POS.
 *
 * Permite que la aplicación funcione 100% adentro del celular SIN INTERNET y SIN PC:
 * 1. Almacena toda la carpeta "datos" (productos, categorías, clientes, ventas,
 *    caja chica, cierres, usuarios, auditoría y configuración + fotos) en el
 *    almacenamiento persistente del teléfono (localStorage + espejo en IndexedDB).
 * 2. Intercepta todas las llamadas fetch('/api/...') y enlaces de descarga (/api/...)
 *    para resolverlas localmente en milisegundos aun en Modo Avión.
 * 3. Detecta automáticamente cuando el celular se conecta a Wi-Fi con internet y
 *    sube el respaldo de "datos" al Google Drive del usuario (vía Google Apps Script
 *    o menú nativo Compartir -> Guardar en Drive de Android).
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'POS_CELULAR_DATOS_V1';
  const SESSION_KEY = 'POS_CELULAR_SESION_V1';
  const IDB_NAME = 'PosCelularOfflineDB';
  const IDB_STORE = 'carpeta_datos';

  const CONFIG_DEFAULT = {
    nombre_comercio: 'Mi Comercio',
    color_primario: '#2563EB',
    moneda: '$',
    telefono: '',
    direccion: '',
    logo_imagen: '',
    qr_imagen: '',
    titular_cuenta: '',
    banco: '',
    alias: '',
    cbu: '',
    pesos_por_punto: 100,
    smtp_servidor: 'smtp.gmail.com',
    smtp_puerto: 587,
    smtp_usuario: '',
    smtp_password: '',
    email_destinatario: '',
    auto_backup_activo: true,
    modo_wifi_backup: 'cualquier_wifi',
    wifi_seguro_ssid: '',
    carpeta_sincronizacion_drive: 'Google Drive / Backups_POS_Celular',
    google_drive_webhook_url: 'https://script.google.com/macros/s/AKfycbzI2Bn0rj_MpW5lFhNJ2um2oEiwtSR4YRbfzedtoYL150rtvcKJ4go_bmLthl24xzMF2Q/exec',
    modo_bloqueo: 'nunca',
    tiempo_inactividad_min: 15,
    modo_operacion: 'autonomo_offline',
    wizard_completado: 0,
    rubro: 'comercio_general',
    clave_maestra_rescate: 'RESCATE-BEBIDAS-7489',
  };

  function ahoraISO() {
    return new Date().toISOString();
  }

  function fechaLocalYYYYMMDD(d = new Date()) {
    const off = d.getTimezoneOffset();
    const loc = new Date(d.getTime() - off * 60000);
    return loc.toISOString().slice(0, 10);
  }

  
  function formatArgentineWhatsApp(telefono) {
    let d = String(telefono || '').replace(/\D/g, '');
    if (d.startsWith('54')) {
      d = d.slice(2);
      if (d.length === 11 && d.startsWith('9')) d = d.slice(1);
    }
    d = d.replace(/^0+/, '');
    if (d.length === 12) {
      for (let area of [2, 3, 4]) {
        if (d.slice(area, area + 2) === '15') {
          d = d.slice(0, area) + d.slice(area + 2);
          break;
        }
      }
    }
    return d.length === 10 ? `549${d}` : d;
  }

  function armarUrlWhatsApp(telefono, mensaje) {
    const tel = formatArgentineWhatsApp(telefono);
    const texto = encodeURIComponent(mensaje);
    return tel
      ? `https://api.whatsapp.com/send?phone=${tel}&text=${texto}`
      : `https://api.whatsapp.com/send?text=${texto}`;
  }

  function fmtFechaAR(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtHoraAR(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtMonedaAR(n, simbolo = '$') {
    const val = Number(n || 0);
    return `${simbolo} ${val.toLocaleString('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function redondear2(n) {
    return Math.round((Number(n) || 0) * 100) / 100;
  }

  function redondear4(n) {
    return Math.round((Number(n) || 0) * 10000) / 10000;
  }

  // =========================================================================
  // 1. Base de Datos Local en el Celular (localStorage + IndexedDB Persistente)
  // =========================================================================
  function crearEstadoInicial() {
    const ahora = ahoraISO();
    return {
      version: '1.0',
      seq: {
        categorias: 1,
        productos: 1,
        promos: 1,
        clientes: 1,
        ventas: 1,
        detalle_ventas: 1,
        movimientos_caja: 1,
        movimientos_cuenta: 1,
        cierres_caja: 1,
        auditoria: 1,
        alertas_seguridad: 1,
      },
      config: { ...CONFIG_DEFAULT },
      usuarios: [
        { id: 1, comercio_id: 1, nombre: 'Dueño', pin: '1234', rol: 'admin', activo: true },
        { id: 2, comercio_id: 1, nombre: 'Cajero 1', pin: '1111', rol: 'cajero', activo: true },
        { id: 3, comercio_id: 1, nombre: 'Cajero 2', pin: '2222', rol: 'cajero', activo: true },
        { id: 4, comercio_id: 1, nombre: 'Cajero 3', pin: '3333', rol: 'cajero', activo: true },
      ],
      categorias: [],
      productos: [],
      promos: [],
      clientes: [],
      ventas: [],
      detalle_ventas: [],
      movimientos_caja: [],
      movimientos_cuenta: [],
      cierres_caja: [],
      auditoria_movimientos: [],
      estado_backup: {
        cambios_pendientes: false,
        ultimo_cambio_local: '',
        ultimo_backup_local: fmtFechaAR(ahora),
        ultimo_backup_nube: '',
        ultimo_estado: 'ok_local',
        ultimo_mensaje: 'Base de datos activa en el celular (100% Offline).',
        ultimo_tamano_kb: 2.5,
        cantidad_backups_guardados: 1,
      },
    };
  }

  const CATEGORIAS_BEBIDAS_SEED = ["Botellas y Destilados", "Gaseosas, Aguas y Jugos", "Energizantes", "Cervezas", "Whisky", "Vinos", "Espumantes y Sidra", "Licores", "Hielo y Boutique", "Kiosco, Chocolates y Postres", "Snacks", "Vapers y Cigarrillos", "Servicios y Envíos"];
  const PRODUCTOS_BEBIDAS_SEED = [["Fernet Branca 1lt", "Botellas y Destilados", 24500, 36], ["Fernet Branca 750cc", "Botellas y Destilados", 18000, 36], ["Fernet Branca 450cc", "Botellas y Destilados", 14500, 24], ["Fernet Branca Menta", "Botellas y Destilados", 13500, 24], ["Fernet Dorado Edición Limitada", "Botellas y Destilados", 20000, 0], ["Amargo Obrero", "Botellas y Destilados", 6000, 18], ["Absolut", "Botellas y Destilados", 27500, 18], ["Skyy", "Botellas y Destilados", 11300, 30], ["Skyy Jungle", "Botellas y Destilados", 12000, 24], ["Smirnoff", "Botellas y Destilados", 11000, 36], ["Smirnoff Tropical", "Botellas y Destilados", 11500, 30], ["Lata Smirnoff Frutos Rojos", "Botellas y Destilados", 4300, 24], ["Sernova", "Botellas y Destilados", 9000, 24], ["Frizzé", "Botellas y Destilados", 4600, 24], ["Dr Lemon", "Botellas y Destilados", 4600, 24], ["Campari", "Botellas y Destilados", 11500, 24], ["Aperol", "Botellas y Destilados", 11000, 24], ["Gancia", "Botellas y Destilados", 9500, 24], ["Ramazzotti", "Botellas y Destilados", 8500, 18], ["Cynar", "Botellas y Destilados", 10500, 12], ["Cinzano", "Botellas y Destilados", 11000, 12], ["Carpano", "Botellas y Destilados", 7900, 12], ["Gin Heráclito", "Botellas y Destilados", 17500, 18], ["Gin Hendricks", "Botellas y Destilados", 60000, 6], ["Gin Bombay", "Botellas y Destilados", 33500, 12], ["Gin Tanqueray Flor de Sevilla", "Botellas y Destilados", 30000, 12], ["Gin Beefeater 24", "Botellas y Destilados", 45000, 10], ["Gin Beefeater Saborizado", "Botellas y Destilados", 36000, 12], ["Gin Beefeater Original 750cc", "Botellas y Destilados", 31000, 12], ["Gin Beefeater Original 1lt", "Botellas y Destilados", 36000, 12], ["Gin Bulldog", "Botellas y Destilados", 32000, 10], ["Gin Aconcagua", "Botellas y Destilados", 19000, 18], ["Gin Aconcagua en Lata", "Botellas y Destilados", 8000, 24], ["Gin Heredero (Pink / Pomelo / Naranja)", "Botellas y Destilados", 18000, 18], ["Gin Llave Seco", "Botellas y Destilados", 14000, 18], ["Gin Gordon", "Botellas y Destilados", 16500, 18], ["Ginebra Llave", "Botellas y Destilados", 14000, 12], ["Ron Coco Bongo", "Botellas y Destilados", 10500, 18], ["Ron Bacardi Oro", "Botellas y Destilados", 26500, 12], ["Ron Bacardi Peach Red", "Botellas y Destilados", 17000, 12], ["Ron Malibú", "Botellas y Destilados", 20000, 12], ["Ron Cayman", "Botellas y Destilados", 8000, 18], ["Jägermeister 700cc", "Botellas y Destilados", 36000, 12], ["Holdmoser", "Botellas y Destilados", 25200, 12], ["Caña Legui", "Botellas y Destilados", 7200, 12], ["Tequila José Cuervo", "Botellas y Destilados", 58500, 8], ["Piña Colada", "Botellas y Destilados", 9000, 12], ["Granadina", "Botellas y Destilados", 4800, 12], ["Coca-Cola 2,25L", "Gaseosas, Aguas y Jugos", 5800, 72], ["Coca-Cola 1,5L", "Gaseosas, Aguas y Jugos", 4500, 72], ["Sprite 2,25L", "Gaseosas, Aguas y Jugos", 5800, 48], ["Sprite 1,5L", "Gaseosas, Aguas y Jugos", 4500, 48], ["Fanta 2,25L", "Gaseosas, Aguas y Jugos", 5800, 36], ["Fanta 1,5L", "Gaseosas, Aguas y Jugos", 4500, 36], ["Coca / Sprite / Fanta Retornable 2L", "Gaseosas, Aguas y Jugos", 4500, 36], ["Coca-Cola de Vidrio", "Gaseosas, Aguas y Jugos", 3500, 24], ["Lata Coca / Sprite / Fanta", "Gaseosas, Aguas y Jugos", 2100, 48], ["Coca / Sprite 500ml", "Gaseosas, Aguas y Jugos", 2600, 36], ["Paso de los Toros Pomelo 1,5L", "Gaseosas, Aguas y Jugos", 4000, 48], ["Paso de los Toros Tónica 1,5L", "Gaseosas, Aguas y Jugos", 4000, 48], ["Lata Paso de los Toros Pomelo", "Gaseosas, Aguas y Jugos", 1500, 24], ["Lata Schweppes", "Gaseosas, Aguas y Jugos", 2000, 24], ["Pritty 1,5L", "Gaseosas, Aguas y Jugos", 2600, 36], ["Pritty 2,25L", "Gaseosas, Aguas y Jugos", 3200, 48], ["Pritty 3L", "Gaseosas, Aguas y Jugos", 4100, 36], ["Secco 3L", "Gaseosas, Aguas y Jugos", 3000, 36], ["Seven Up / Pepsi 2L", "Gaseosas, Aguas y Jugos", 4100, 24], ["Pepsi 1,5L", "Gaseosas, Aguas y Jugos", 2700, 0], ["Baggio 1L", "Gaseosas, Aguas y Jugos", 2500, 48], ["Jugo Smudis Naranja 1L", "Gaseosas, Aguas y Jugos", 3500, 24], ["Cepita 1L", "Gaseosas, Aguas y Jugos", 3700, 24], ["Cepita 1,5L", "Gaseosas, Aguas y Jugos", 4200, 24], ["Gatorade 500ml", "Gaseosas, Aguas y Jugos", 2200, 0], ["Powerade Rojo / Azul 500ml", "Gaseosas, Aguas y Jugos", 2100, 24], ["Powerade Azul 1L", "Gaseosas, Aguas y Jugos", 3200, 0], ["Levité Naranja / Pomelo", "Gaseosas, Aguas y Jugos", 2700, 24], ["Aquarius Pomelo 500ml", "Gaseosas, Aguas y Jugos", 1500, 24], ["Aquarius Manzana / Pera 1,5L", "Gaseosas, Aguas y Jugos", 4100, 24], ["Agua Saborizada Fresh", "Gaseosas, Aguas y Jugos", 2100, 24], ["Agua Mineral / Soda 500ml", "Gaseosas, Aguas y Jugos", 1500, 36], ["Agua Mineral 1L", "Gaseosas, Aguas y Jugos", 1700, 36], ["Soda Grande", "Gaseosas, Aguas y Jugos", 2100, 24], ["Speed XL 500cc", "Energizantes", 3200, 72], ["Speed 250cc", "Energizantes", 2000, 72], ["Monster 500cc", "Energizantes", 3700, 60], ["Red Bull 250cc", "Energizantes", 3700, 60], ["Cerveza 361", "Cervezas", 2500, 36], ["Brahma Botella", "Cervezas", 4500, 36], ["Quilmes Botella", "Cervezas", 4300, 36], ["Andes Rubia Botella", "Cervezas", 4800, 24], ["Andes IPA Botella", "Cervezas", 4800, 24], ["Andes Roja Botella", "Cervezas", 4800, 24], ["Corona 710cc", "Cervezas", 6000, 24], ["Corona 330cc", "Cervezas", 4100, 48], ["Patagonia Botella", "Cervezas", 6000, 24], ["Stella Artois Botella", "Cervezas", 6000, 24], ["Heineken Botella", "Cervezas", 6400, 24], ["Lata Schneider", "Cervezas", 2800, 72], ["Lata Premium Grolsch", "Cervezas", 3800, 48], ["Lata Quilmes", "Cervezas", 2700, 72], ["Lata Quilmes Sin Alcohol", "Cervezas", 2700, 36], ["Latón Quilmes", "Cervezas", 3900, 48], ["Lata Brahma", "Cervezas", 2700, 72], ["Latón Brahma", "Cervezas", 3900, 48], ["Lata Andes Negra", "Cervezas", 3200, 48], ["Lata Stella Artois", "Cervezas", 3500, 48], ["Latón Stella Artois", "Cervezas", 4400, 48], ["Lata Heineken", "Cervezas", 4500, 48], ["Latón Heineken", "Cervezas", 6100, 48], ["Johnnie Walker Gold Label", "Whisky", 130000, 4], ["Johnnie Walker Double Black", "Whisky", 90000, 6], ["Johnnie Walker Black Label", "Whisky", 83000, 6], ["Johnnie Walker Red Label 750cc", "Whisky", 44000, 12], ["Jack Daniels Fire 1lt", "Whisky", 80000, 6], ["Jack Daniels Honey", "Whisky", 65000, 8], ["Jack Daniels Clásico", "Whisky", 65000, 8], ["Gentleman Jack Daniels", "Whisky", 95000, 4], ["Chivas Regal", "Whisky", 64000, 0], ["Jameson", "Whisky", 30700, 10], ["Jameson Black Barrel", "Whisky", 52000, 6], ["J&B", "Whisky", 35500, 8], ["Ballantine's", "Whisky", 35000, 8], ["Caballito Blanco", "Whisky", 20500, 12], ["Blenders", "Whisky", 12000, 18], ["Rutini Tinto", "Vinos", 20500, 12], ["Mosquita Muerta", "Vinos", 19800, 0], ["Catena Zapata", "Vinos", 18500, 12], ["Sapo de Otro Pozo", "Vinos", 16200, 12], ["Las Perdices Blanco Dulce / Tinto Malbec", "Vinos", 10000, 18], ["Pispi", "Vinos", 12300, 0], ["Santa Julia Chenin Dulce / Tinto", "Vinos", 10000, 24], ["Lata Santa Julia Chenin Dulce", "Vinos", 2800, 24], ["Santa Julia Malbec", "Vinos", 9000, 24], ["Trapiche Tinto Reserva", "Vinos", 10100, 0], ["Partridge Malbec", "Vinos", 7200, 18], ["Alma Mora Tinto", "Vinos", 7300, 18], ["Chacabuco Malbec", "Vinos", 8900, 18], ["Cordero con Piel de Lobo", "Vinos", 6500, 24], ["Prófugo Mr. Franc", "Vinos", 5500, 18], ["Cazador Cabernet Sauvignon", "Vinos", 4400, 18], ["Prófugo Especias", "Vinos", 4300, 18], ["Prófugo Frutos Rojos", "Vinos", 4300, 18], ["Otro Loco Más Tinto", "Vinos", 5700, 18], ["Lobo Negro", "Vinos", 4200, 18], ["Viñas de Balbo", "Vinos", 3800, 36], ["Quara", "Vinos", 2800, 24], ["Pecado Tinto", "Vinos", 2500, 24], ["Vino Toro Tetra", "Vinos", 2900, 48], ["Vino Blanco Toro Tetra", "Vinos", 2900, 36], ["Gata Flora Blanco Dulce", "Vinos", 7500, 18], ["Colón", "Vinos", 10000, 18], ["Colón Frutos Rojos / Durazno", "Vinos", 12500, 18], ["New Age", "Vinos", 5500, 24], ["Estancia Mendoza Chenin Dulce", "Vinos", 4000, 24], ["Marlo Blanco Dulce", "Vinos", 5300, 18], ["Norton Cosecha Tardía Blanco / Tinto Dulce", "Vinos", 4500, 24], ["Dilema Blanco Dulce", "Vinos", 5600, 24], ["Más Loco Que Una Cabra", "Vinos", 5600, 18], ["Santa Julia Espumante Classic", "Espumantes y Sidra", 8500, 18], ["Dada 7", "Espumantes y Sidra", 11000, 18], ["Rosé Espumante", "Espumantes y Sidra", 7000, 18], ["Chandon Extra Brut / Délice", "Espumantes y Sidra", 21400, 12], ["Mumm", "Espumantes y Sidra", 9800, 18], ["Renaissance", "Espumantes y Sidra", 6000, 18], ["Champagne DU", "Espumantes y Sidra", 6000, 24], ["Sidra Farruca", "Espumantes y Sidra", 2800, 24], ["Sheridans", "Licores", 50000, 6], ["Amarula Común / Vainilla", "Licores", 40000, 8], ["Baileys Original", "Licores", 40500, 8], ["Tía María Café", "Licores", 10000, 12], ["Tía María Crema", "Licores", 16500, 12], ["Tres Plumas (Huevo / Chocolate / DDL)", "Licores", 7500, 18], ["Hielo 1kg", "Hielo y Boutique", 2000, 50], ["Hielo 3kg", "Hielo y Boutique", 6000, 50], ["Hielo 12kg", "Hielo y Boutique", 12000, 30], ["Boutique Limón", "Hielo y Boutique", 3700, 20], ["Boutique Pomelo", "Hielo y Boutique", 3700, 20], ["Boutique Gin Tonic Botánicos", "Hielo y Boutique", 3700, 20], ["Frutos Congelados 100gr (Frutilla / Arándano / Mora)", "Hielo y Boutique", 2300, 20], ["Chicle Topline", "Kiosco, Chocolates y Postres", 600, 50], ["Chicle Topline 7ven", "Kiosco, Chocolates y Postres", 1200, 40], ["Chicle Topline Ultra", "Kiosco, Chocolates y Postres", 2400, 30], ["Menthoplus Negros / Miel", "Kiosco, Chocolates y Postres", 1000, 40], ["Flynn Paff Unidad", "Kiosco, Chocolates y Postres", 200, 100], ["Caja Bon o Bon Supreme Avellanas 140g", "Kiosco, Chocolates y Postres", 9500, 12], ["Chocolate Latte con Dedicatoria 60g", "Kiosco, Chocolates y Postres", 5000, 15], ["Alfajor Triple Terrabusi Blanco", "Kiosco, Chocolates y Postres", 2000, 24], ["Alfajor Milka Extra DDL Blanco / Negro", "Kiosco, Chocolates y Postres", 2000, 24], ["Alfajor Matilda", "Kiosco, Chocolates y Postres", 3000, 24], ["Alfajor Tatín Triple Blanco / Negro", "Kiosco, Chocolates y Postres", 1200, 36], ["Alfajor Fantoche Blanco / Negro", "Kiosco, Chocolates y Postres", 1300, 36], ["Alfajor Cofler Block Simple", "Kiosco, Chocolates y Postres", 1200, 30], ["Alfajor Bon o Bon Simple Blanco / Negro", "Kiosco, Chocolates y Postres", 1100, 36], ["Alfajor Bon o Bon Triple Negro", "Kiosco, Chocolates y Postres", 2000, 24], ["Alfajor Bon o Bon GOAT", "Kiosco, Chocolates y Postres", 2300, 24], ["Alfajor Cofler Block Triple", "Kiosco, Chocolates y Postres", 2000, 24], ["Alfajor Águila (Brownie / Dark / Negro / Blanco)", "Kiosco, Chocolates y Postres", 2000, 24], ["Alfajor Chocotorta Triple", "Kiosco, Chocolates y Postres", 2000, 0], ["Alfajor Tofi Negro Triple", "Kiosco, Chocolates y Postres", 2000, 24], ["Alfajor Rasta Negro / Blanco", "Kiosco, Chocolates y Postres", 2000, 36], ["Alfajor Barrigón Varios Sabores", "Kiosco, Chocolates y Postres", 2300, 36], ["Chocolate Arcor", "Kiosco, Chocolates y Postres", 1500, 24], ["Rocklets 40gr", "Kiosco, Chocolates y Postres", 2000, 24], ["Chocolate Graffiti Blanco / Negro", "Kiosco, Chocolates y Postres", 2100, 24], ["Chocolate Cofler Aireado 55gr", "Kiosco, Chocolates y Postres", 5000, 18], ["Chocolate Cofler Extra Grande", "Kiosco, Chocolates y Postres", 9500, 12], ["Cofler Extra 46gr", "Kiosco, Chocolates y Postres", 2700, 24], ["Cofler 46gr Relleno DDL", "Kiosco, Chocolates y Postres", 3000, 24], ["Cofler Tres Placeres / Rocklets 55g", "Kiosco, Chocolates y Postres", 4500, 18], ["Cofler Dubai 43g", "Kiosco, Chocolates y Postres", 4000, 18], ["Block 38gr Negro", "Kiosco, Chocolates y Postres", 2500, 24], ["Block 38gr Gold", "Kiosco, Chocolates y Postres", 2500, 24], ["Block 110gr", "Kiosco, Chocolates y Postres", 6000, 18], ["Block 170gr (Negro / Miti Miti)", "Kiosco, Chocolates y Postres", 11000, 12], ["Block 300gr Negro", "Kiosco, Chocolates y Postres", 17000, 10], ["Barra Cofler con DDL", "Kiosco, Chocolates y Postres", 2400, 24], ["Barra Block Max 60gr", "Kiosco, Chocolates y Postres", 2000, 24], ["Barra Bon o Bon XL Negro", "Kiosco, Chocolates y Postres", 2000, 24], ["Barra Bon o Bon Simple Blanco / Negro", "Kiosco, Chocolates y Postres", 1400, 24], ["Barra Cofler Chocolina XL", "Kiosco, Chocolates y Postres", 2000, 24], ["Barra Cofler Chocolina Max", "Kiosco, Chocolates y Postres", 2000, 24], ["Chocolate Bon o Bon Relleno Chocolina 48g", "Kiosco, Chocolates y Postres", 2700, 24], ["Postre Tiramisú", "Kiosco, Chocolates y Postres", 7000, 12], ["Postre Oreo", "Kiosco, Chocolates y Postres", 7000, 12], ["Postre Chocotorta", "Kiosco, Chocolates y Postres", 7000, 12], ["Postre Turrón de Quaker", "Kiosco, Chocolates y Postres", 7000, 12], ["Postre Brownie con Franui", "Kiosco, Chocolates y Postres", 7000, 12], ["Lays Clásicas", "Snacks", 4400, 24], ["Lays Jamón Serrano / Queso Crema y Ciboulette", "Snacks", 4400, 24], ["3D", "Snacks", 4400, 24], ["Doritos", "Snacks", 4400, 24], ["Papas Quento 90gr (Consultar Sabores)", "Snacks", 3100, 30], ["Vaper The Black Sheep 25k (Cola y Lima)", "Vapers y Cigarrillos", 29000, 10], ["Vaper Ebcreate 40k (Winter Mint / Pink Lemonade)", "Vapers y Cigarrillos", 36000, 10], ["Vaper Ignite 40k (Grape Ice / Strawberry)", "Vapers y Cigarrillos", 36000, 10], ["Vaper Elfbar 15k (Mango Magic)", "Vapers y Cigarrillos", 26000, 10], ["Vaper Lost Vape 10k (Raspberry / Pineapple)", "Vapers y Cigarrillos", 17000, 12], ["Vaper Funky Lands 7k (Pink Grapefruit)", "Vapers y Cigarrillos", 13000, 12], ["Vaper Ebdesign 6k (Blue Razz / Strawberry Ice)", "Vapers y Cigarrillos", 12000, 12], ["Vaper Elfbar 5k (Kiwi Passion Fruit Guava)", "Vapers y Cigarrillos", 11000, 12], ["Vaper Ignite 2500 (Berries / Mint / Watermelon)", "Vapers y Cigarrillos", 9000, 15], ["Marlboro 20 Box (Común / Fusión / Sandía)", "Vapers y Cigarrillos", 6700, 40], ["Marlboro 20 Común", "Vapers y Cigarrillos", 6300, 40], ["Marlboro 10 (Común / Fusión)", "Vapers y Cigarrillos", 4200, 40], ["Marlboro Crafted Común", "Vapers y Cigarrillos", 4100, 30], ["Marlboro Crafted Convertible", "Vapers y Cigarrillos", 4100, 30], ["Marlboro Coral / Purple", "Vapers y Cigarrillos", 4500, 30], ["Philips 20 Común", "Vapers y Cigarrillos", 5700, 40], ["Philips 20 Box (Común / Convertible)", "Vapers y Cigarrillos", 6200, 40], ["Philips 20 Red", "Vapers y Cigarrillos", 2800, 30], ["Philips 10 (Común / Convertible)", "Vapers y Cigarrillos", 3900, 30], ["Chester 20 (Común / Convertible)", "Vapers y Cigarrillos", 5200, 30], ["Chester Box (Convertible / Uva)", "Vapers y Cigarrillos", 5500, 30], ["Chester 10 (Común / Convertible)", "Vapers y Cigarrillos", 3500, 30], ["Dolchester", "Vapers y Cigarrillos", 1800, 30], ["Red Point", "Vapers y Cigarrillos", 2200, 30], ["Master", "Vapers y Cigarrillos", 2100, 30], ["Mill", "Vapers y Cigarrillos", 2100, 30], ["Encendedor", "Vapers y Cigarrillos", 600, 50], ["Preservativos", "Vapers y Cigarrillos", 5300, 24], ["Envío a Domicilio", "Servicios y Envíos", 1500, 9999]];
  const PROMOS_BEBIDAS_SEED = [{"nombre": "Promo Fernet 1lt + 2 Cocas 2,25L", "categoria": "Fernet", "descripcion": "1 Fernet Branca 1lt + 2 Coca-Cola 2,25L", "precio_venta": 35000, "fijos": [["Fernet Branca 1lt", 1], ["Coca-Cola 2,25L", 2]], "elecciones": []}, {"nombre": "Promo Fernet 750cc + Coca 2,25L", "categoria": "Fernet", "descripcion": "1 Fernet Branca 750cc + 1 Coca-Cola 2,25L", "precio_venta": 23000, "fijos": [["Fernet Branca 750cc", 1], ["Coca-Cola 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Fernet 750cc + 2 Cocas 1,5L", "categoria": "Fernet", "descripcion": "1 Fernet Branca 750cc + 2 Coca-Cola 1,5L", "precio_venta": 25000, "fijos": [["Fernet Branca 750cc", 1], ["Coca-Cola 1,5L", 2]], "elecciones": []}, {"nombre": "Promo Fernet 450cc + Coca 1,5L", "categoria": "Fernet", "descripcion": "1 Fernet Branca 450cc + 1 Coca-Cola 1,5L", "precio_venta": 17000, "fijos": [["Fernet Branca 450cc", 1], ["Coca-Cola 1,5L", 1]], "elecciones": []}, {"nombre": "Promo Fernet Menta + Sprite o Coca 2,25L", "categoria": "Fernet", "descripcion": "1 Fernet Menta + 1 Gaseosa 2,25L a elección", "precio_venta": 18000, "fijos": [["Fernet Branca Menta", 1]], "elecciones": [{"titulo": "Elegí la gaseosa 2,25L", "cantidad": 1, "opciones": [{"producto": "Sprite 2,25L", "extra": 0}, {"producto": "Coca-Cola 2,25L", "extra": 0}]}]}, {"nombre": "Promo Smirnoff + 2 Speed XL", "categoria": "Vodka", "descripcion": "Smirnoff Clásico (Tropical +$500) + 2 Speed XL 500cc", "precio_venta": 16000, "fijos": [["Speed XL 500cc", 2]], "elecciones": [{"titulo": "Elegí el sabor de Smirnoff", "cantidad": 1, "opciones": [{"producto": "Smirnoff", "extra": 0}, {"producto": "Smirnoff Tropical", "extra": 500}]}]}, {"nombre": "Promo Smirnoff + 4 Speed 250cc", "categoria": "Vodka", "descripcion": "1 Smirnoff + 4 latas Speed 250cc", "precio_venta": 17500, "fijos": [["Speed 250cc", 4]], "elecciones": [{"titulo": "Elegí el sabor de Smirnoff", "cantidad": 1, "opciones": [{"producto": "Smirnoff", "extra": 0}, {"producto": "Smirnoff Tropical", "extra": 500}]}]}, {"nombre": "Promo Smirnoff + 2 Monster", "categoria": "Vodka", "descripcion": "Smirnoff Clásico (Tropical +$500) + 2 Monster 500cc", "precio_venta": 16500, "fijos": [["Monster 500cc", 2]], "elecciones": [{"titulo": "Elegí el sabor de Smirnoff", "cantidad": 1, "opciones": [{"producto": "Smirnoff", "extra": 0}, {"producto": "Smirnoff Tropical", "extra": 500}]}]}, {"nombre": "Promo Smirnoff + Baggio 1L", "categoria": "Vodka", "descripcion": "Smirnoff Clásico (Tropical +$500) + 1 Baggio 1L", "precio_venta": 13500, "fijos": [["Baggio 1L", 1]], "elecciones": [{"titulo": "Elegí el sabor de Smirnoff", "cantidad": 1, "opciones": [{"producto": "Smirnoff", "extra": 0}, {"producto": "Smirnoff Tropical", "extra": 500}]}]}, {"nombre": "Promo Smirnoff + Paso de los Toros o Sprite 1,5L", "categoria": "Vodka", "descripcion": "Smirnoff (Tropical +$500) + 1 Gaseosa 1,5L a elección", "precio_venta": 14500, "fijos": [], "elecciones": [{"titulo": "Elegí el sabor de Smirnoff", "cantidad": 1, "opciones": [{"producto": "Smirnoff", "extra": 0}, {"producto": "Smirnoff Tropical", "extra": 500}]}, {"titulo": "Elegí la gaseosa 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Smirnoff + 4 Red Bull", "categoria": "Vodka", "descripcion": "Smirnoff Clásico (Tropical +$500) + 4 Red Bull 250cc", "precio_venta": 22500, "fijos": [["Red Bull 250cc", 4]], "elecciones": [{"titulo": "Elegí el sabor de Smirnoff", "cantidad": 1, "opciones": [{"producto": "Smirnoff", "extra": 0}, {"producto": "Smirnoff Tropical", "extra": 500}]}]}, {"nombre": "Promo Sernova + 2 Speed XL", "categoria": "Vodka", "descripcion": "1 Sernova + 2 Speed XL 500cc", "precio_venta": 13000, "fijos": [["Sernova", 1], ["Speed XL 500cc", 2]], "elecciones": []}, {"nombre": "Promo Sernova + 2 Monster", "categoria": "Vodka", "descripcion": "1 Sernova + 2 Monster 500cc", "precio_venta": 14000, "fijos": [["Sernova", 1], ["Monster 500cc", 2]], "elecciones": []}, {"nombre": "Promo Skyy + 2 Speed XL", "categoria": "Vodka", "descripcion": "1 Skyy Clásico + 2 Speed XL 500cc", "precio_venta": 16500, "fijos": [["Skyy", 1], ["Speed XL 500cc", 2]], "elecciones": []}, {"nombre": "Promo Skyy Jungle + 2 Speed XL", "categoria": "Vodka", "descripcion": "1 Skyy Jungle + 2 Speed XL 500cc", "precio_venta": 17500, "fijos": [["Skyy Jungle", 1], ["Speed XL 500cc", 2]], "elecciones": []}, {"nombre": "Promo Skyy + 2 Monster", "categoria": "Vodka", "descripcion": "1 Skyy Clásico + 2 Monster 500cc", "precio_venta": 17500, "fijos": [["Skyy", 1], ["Monster 500cc", 2]], "elecciones": []}, {"nombre": "Promo Skyy Jungle + 2 Monster", "categoria": "Vodka", "descripcion": "1 Skyy Jungle + 2 Monster 500cc", "precio_venta": 18500, "fijos": [["Skyy Jungle", 1], ["Monster 500cc", 2]], "elecciones": []}, {"nombre": "Promo Skyy + Sprite o Paso de los Toros 1,5L", "categoria": "Vodka", "descripcion": "1 Skyy + 1 Gaseosa 1,5L a elección", "precio_venta": 15500, "fijos": [["Skyy", 1]], "elecciones": [{"titulo": "Elegí la gaseosa 1,5L", "cantidad": 1, "opciones": [{"producto": "Sprite 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}]}]}, {"nombre": "Promo Skyy + Baggio 1L", "categoria": "Vodka", "descripcion": "1 Skyy + 1 Baggio 1L", "precio_venta": 14000, "fijos": [["Skyy", 1], ["Baggio 1L", 1]], "elecciones": []}, {"nombre": "Promo Skyy + 4 Red Bull", "categoria": "Vodka", "descripcion": "1 Skyy + 4 Red Bull 250cc", "precio_venta": 22500, "fijos": [["Skyy", 1], ["Red Bull 250cc", 4]], "elecciones": []}, {"nombre": "Promo Absolut + 4 Speed XL", "categoria": "Vodka", "descripcion": "1 Absolut + 4 Speed XL 500cc", "precio_venta": 37000, "fijos": [["Absolut", 1], ["Speed XL 500cc", 4]], "elecciones": []}, {"nombre": "Promo Absolut + 4 Monster", "categoria": "Vodka", "descripcion": "1 Absolut + 4 Monster 500cc", "precio_venta": 38000, "fijos": [["Absolut", 1], ["Monster 500cc", 4]], "elecciones": []}, {"nombre": "Promo Absolut + 4 Red Bull", "categoria": "Vodka", "descripcion": "1 Absolut + 4 Red Bull 250cc", "precio_venta": 38000, "fijos": [["Absolut", 1], ["Red Bull 250cc", 4]], "elecciones": []}, {"nombre": "Promo Ron Cayman + 2 Speed XL", "categoria": "Ron y Whisky", "descripcion": "1 Ron Cayman + 2 Speed XL 500cc", "precio_venta": 11500, "fijos": [["Ron Cayman", 1], ["Speed XL 500cc", 2]], "elecciones": []}, {"nombre": "Promo Ron Coco Bongo + 2 Speed XL", "categoria": "Ron y Whisky", "descripcion": "1 Ron Coco Bongo + 2 Speed XL 500cc", "precio_venta": 14500, "fijos": [["Ron Coco Bongo", 1], ["Speed XL 500cc", 2]], "elecciones": []}, {"nombre": "Promo Ron Coco Bongo + Coca 1,5L", "categoria": "Ron y Whisky", "descripcion": "1 Ron Coco Bongo + 1 Coca-Cola 1,5L", "precio_venta": 13500, "fijos": [["Ron Coco Bongo", 1], ["Coca-Cola 1,5L", 1]], "elecciones": []}, {"nombre": "Promo Ron Malibú + Baggio 1L", "categoria": "Ron y Whisky", "descripcion": "1 Ron Malibú + 1 Baggio 1L", "precio_venta": 18000, "fijos": [["Ron Malibú", 1], ["Baggio 1L", 1]], "elecciones": []}, {"nombre": "Promo Ron Malibú + Coca-Cola 1,5L", "categoria": "Ron y Whisky", "descripcion": "1 Ron Malibú + 1 Coca-Cola 1,5L", "precio_venta": 19000, "fijos": [["Ron Malibú", 1], ["Coca-Cola 1,5L", 1]], "elecciones": []}, {"nombre": "Promo Ron Bacardi Peach Red + 2 Speed XL", "categoria": "Ron y Whisky", "descripcion": "1 Bacardi Peach Red + 2 Speed XL 500cc", "precio_venta": 21000, "fijos": [["Ron Bacardi Peach Red", 1], ["Speed XL 500cc", 2]], "elecciones": []}, {"nombre": "Promo Bacardi + Coca 2,25L", "categoria": "Ron y Whisky", "descripcion": "1 Ron Bacardi Oro + 1 Coca-Cola 2,25L", "precio_venta": 28500, "fijos": [["Ron Bacardi Oro", 1], ["Coca-Cola 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Whisky Caballito Blanco + Coca o Sprite 2,25L", "categoria": "Ron y Whisky", "descripcion": "1 Caballito Blanco + 1 Gaseosa 2,25L a elección", "precio_venta": 25000, "fijos": [["Caballito Blanco", 1]], "elecciones": [{"titulo": "Elegí la gaseosa 2,25L", "cantidad": 1, "opciones": [{"producto": "Coca-Cola 2,25L", "extra": 0}, {"producto": "Sprite 2,25L", "extra": 0}]}]}, {"nombre": "Promo Whisky Blenders + Coca o Sprite 2,25L", "categoria": "Ron y Whisky", "descripcion": "1 Blenders + 1 Gaseosa 2,25L a elección", "precio_venta": 17500, "fijos": [["Blenders", 1]], "elecciones": [{"titulo": "Elegí la gaseosa 2,25L", "cantidad": 1, "opciones": [{"producto": "Coca-Cola 2,25L", "extra": 0}, {"producto": "Sprite 2,25L", "extra": 0}]}]}, {"nombre": "Promo Ginebra + Sprite 2,25L", "categoria": "Aperitivos", "descripcion": "1 Ginebra Llave + 1 Sprite 2,25L", "precio_venta": 17500, "fijos": [["Ginebra Llave", 1], ["Sprite 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Ramazzotti + Paso de los Toros 1,5L", "categoria": "Aperitivos", "descripcion": "1 Ramazzotti + 1 Paso de los Toros 1,5L", "precio_venta": 12500, "fijos": [["Ramazzotti", 1]], "elecciones": [{"titulo": "Elegí Paso de los Toros", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gancia + Sprite 2,25L", "categoria": "Aperitivos", "descripcion": "1 Gancia + 1 Sprite 2,25L", "precio_venta": 14500, "fijos": [["Gancia", 1], ["Sprite 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Gancia + Sprite 1,5L", "categoria": "Aperitivos", "descripcion": "1 Gancia + 1 Sprite 1,5L", "precio_venta": 13500, "fijos": [["Gancia", 1], ["Sprite 1,5L", 1]], "elecciones": []}, {"nombre": "Promo Aperol + Baggio 1L", "categoria": "Aperitivos", "descripcion": "1 Aperol + 1 Baggio 1L", "precio_venta": 14200, "fijos": [["Aperol", 1], ["Baggio 1L", 1]], "elecciones": []}, {"nombre": "Promo Aperol + Champagne DU", "categoria": "Aperitivos", "descripcion": "1 Aperol + 1 Champagne DU", "precio_venta": 16000, "fijos": [["Aperol", 1], ["Champagne DU", 1]], "elecciones": []}, {"nombre": "Promo Campari + Baggio 1L", "categoria": "Aperitivos", "descripcion": "1 Campari + 1 Baggio 1L", "precio_venta": 13500, "fijos": [["Campari", 1], ["Baggio 1L", 1]], "elecciones": []}, {"nombre": "Promo Jägermeister 700cc + 4 Red Bull", "categoria": "Aperitivos", "descripcion": "1 Jägermeister 700cc + 4 Red Bull 250cc", "precio_venta": 47500, "fijos": [["Jägermeister 700cc", 1], ["Red Bull 250cc", 4]], "elecciones": []}, {"nombre": "Promo Holdmoser + 4 Red Bull", "categoria": "Aperitivos", "descripcion": "1 Holdmoser + 4 Red Bull 250cc", "precio_venta": 35000, "fijos": [["Holdmoser", 1], ["Red Bull 250cc", 4]], "elecciones": []}, {"nombre": "Promo Gin Heráclito", "categoria": "Gin", "descripcion": "1 Gin Heráclito + 1 Gaseosa 1,5L a elección", "precio_venta": 20000, "fijos": [["Gin Heráclito", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Llave Seco", "categoria": "Gin", "descripcion": "1 Gin Llave Seco + 1 Gaseosa 1,5L a elección", "precio_venta": 17000, "fijos": [["Gin Llave Seco", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Beefeater 24", "categoria": "Gin", "descripcion": "1 Gin Beefeater 24 + 1 Gaseosa 1,5L a elección", "precio_venta": 48000, "fijos": [["Gin Beefeater 24", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Bombay", "categoria": "Gin", "descripcion": "1 Gin Bombay + 1 Gaseosa 1,5L a elección", "precio_venta": 35000, "fijos": [["Gin Bombay", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Tanqueray", "categoria": "Gin", "descripcion": "1 Gin Tanqueray + 1 Gaseosa 1,5L a elección", "precio_venta": 33000, "fijos": [["Gin Tanqueray Flor de Sevilla", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Bulldog", "categoria": "Gin", "descripcion": "1 Gin Bulldog + 1 Gaseosa 1,5L a elección", "precio_venta": 35000, "fijos": [["Gin Bulldog", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Beefeater Original 1Lt", "categoria": "Gin", "descripcion": "1 Gin Beefeater 1Lt + 1 Gaseosa 1,5L a elección", "precio_venta": 39000, "fijos": [["Gin Beefeater Original 1lt", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Beefeater Original 750cc", "categoria": "Gin", "descripcion": "1 Gin Beefeater 750cc + 1 Gaseosa 1,5L a elección", "precio_venta": 34000, "fijos": [["Gin Beefeater Original 750cc", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Beefeater Saborizado", "categoria": "Gin", "descripcion": "1 Gin Beefeater Saborizado + 1 Gaseosa 1,5L a elección", "precio_venta": 39000, "fijos": [["Gin Beefeater Saborizado", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Aconcagua", "categoria": "Gin", "descripcion": "1 Gin Aconcagua + 1 Gaseosa 1,5L a elección", "precio_venta": 22000, "fijos": [["Gin Aconcagua", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Heredero", "categoria": "Gin", "descripcion": "1 Gin Heredero + 1 Gaseosa 1,5L a elección", "precio_venta": 21000, "fijos": [["Gin Heredero (Pink / Pomelo / Naranja)", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Gin Gordon", "categoria": "Gin", "descripcion": "1 Gin Gordon + 1 Gaseosa 1,5L a elección", "precio_venta": 19000, "fijos": [["Gin Gordon", 1]], "elecciones": [{"titulo": "Elegí acompañante 1,5L", "cantidad": 1, "opciones": [{"producto": "Paso de los Toros Tónica 1,5L", "extra": 0}, {"producto": "Paso de los Toros Pomelo 1,5L", "extra": 0}, {"producto": "Sprite 1,5L", "extra": 0}]}]}, {"nombre": "Promo Vino Viñas de Balbo + 1 Pritty 2,25L", "categoria": "Vinos", "descripcion": "1 Viñas de Balbo + 1 Pritty 2,25L", "precio_venta": 8000, "fijos": [["Viñas de Balbo", 1], ["Pritty 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Vino Toro (2 cajas) + Secco 3L", "categoria": "Vinos", "descripcion": "2 Vino Toro Tetra + 1 Secco 3L", "precio_venta": 9500, "fijos": [["Vino Toro Tetra", 2], ["Secco 3L", 1]], "elecciones": []}, {"nombre": "Promo Vino Toro (3 cajas) + Secco 3L", "categoria": "Vinos", "descripcion": "3 Vino Toro Tetra + 1 Secco 3L", "precio_venta": 12000, "fijos": [["Vino Toro Tetra", 3], ["Secco 3L", 1]], "elecciones": []}, {"nombre": "Promo Vino Toro (2 cajas) + Pritty 2,25L", "categoria": "Vinos", "descripcion": "2 Vino Toro Tetra + 1 Pritty 2,25L", "precio_venta": 9500, "fijos": [["Vino Toro Tetra", 2], ["Pritty 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Vino Toro (3 cajas) + 1 Pritty 3L", "categoria": "Vinos", "descripcion": "3 Vino Toro Tetra + 1 Pritty 3L", "precio_venta": 12000, "fijos": [["Vino Toro Tetra", 3], ["Pritty 3L", 1]], "elecciones": []}, {"nombre": "Promo Vino Toro (2 cajas) + Coca 2,25L", "categoria": "Vinos", "descripcion": "2 Vino Toro Tetra + 1 Coca-Cola 2,25L", "precio_venta": 12000, "fijos": [["Vino Toro Tetra", 2], ["Coca-Cola 2,25L", 1]], "elecciones": []}, {"nombre": "Promo Vino Blanco Toro (2 cajas) + Sprite 2,25L", "categoria": "Vinos", "descripcion": "2 Vino Blanco Toro Tetra + 1 Sprite 2,25L", "precio_venta": 12000, "fijos": [["Vino Blanco Toro Tetra", 2], ["Sprite 2,25L", 1]], "elecciones": []}, {"nombre": "Pack x6 Corona 330cc", "categoria": "Packs Cerveza", "descripcion": "6 unidades de Corona 330cc", "precio_venta": 31000, "fijos": [["Corona 330cc", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Schneider", "categoria": "Packs Cerveza", "descripcion": "6 latas de Schneider", "precio_venta": 15600, "fijos": [["Lata Schneider", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Premium Grolsch", "categoria": "Packs Cerveza", "descripcion": "6 latas de Grolsch", "precio_venta": 21500, "fijos": [["Lata Premium Grolsch", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Quilmes", "categoria": "Packs Cerveza", "descripcion": "6 latas de Quilmes Clásica", "precio_venta": 14700, "fijos": [["Lata Quilmes", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Quilmes Sin Alcohol", "categoria": "Packs Cerveza", "descripcion": "6 latas de Quilmes 0.0%", "precio_venta": 14700, "fijos": [["Lata Quilmes Sin Alcohol", 6]], "elecciones": []}, {"nombre": "Pack x6 Latón Quilmes", "categoria": "Packs Cerveza", "descripcion": "6 latones de Quilmes", "precio_venta": 14800, "fijos": [["Latón Quilmes", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Brahma", "categoria": "Packs Cerveza", "descripcion": "6 latas de Brahma", "precio_venta": 14700, "fijos": [["Lata Brahma", 6]], "elecciones": []}, {"nombre": "Pack x6 Latón Brahma", "categoria": "Packs Cerveza", "descripcion": "6 latones de Brahma", "precio_venta": 14800, "fijos": [["Latón Brahma", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Andes Negra", "categoria": "Packs Cerveza", "descripcion": "6 latas de Andes Negra", "precio_venta": 17400, "fijos": [["Lata Andes Negra", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Stella Artois", "categoria": "Packs Cerveza", "descripcion": "6 latas de Stella Artois", "precio_venta": 19800, "fijos": [["Lata Stella Artois", 6]], "elecciones": []}, {"nombre": "Pack x6 Latón Stella Artois", "categoria": "Packs Cerveza", "descripcion": "6 latones de Stella Artois", "precio_venta": 16200, "fijos": [["Latón Stella Artois", 6]], "elecciones": []}, {"nombre": "Pack x6 Lata Heineken", "categoria": "Packs Cerveza", "descripcion": "6 latas de Heineken", "precio_venta": 25000, "fijos": [["Lata Heineken", 6]], "elecciones": []}, {"nombre": "Pack x6 Latón Heineken", "categoria": "Packs Cerveza", "descripcion": "6 latones de Heineken", "precio_venta": 34600, "fijos": [["Latón Heineken", 6]], "elecciones": []}, {"nombre": "Promo 2x Alfajor Barrigón", "categoria": "Kiosco", "descripcion": "2 Alfajores Barrigón varios sabores", "precio_venta": 4000, "fijos": [["Alfajor Barrigón Varios Sabores", 2]], "elecciones": []}, {"nombre": "Promo 2x Flynn Paff", "categoria": "Kiosco", "descripcion": "2 Caramelos Flynn Paff", "precio_venta": 300, "fijos": [["Flynn Paff Unidad", 2]], "elecciones": []}];

  function sembrarCatalogoBebidasEnDB(db, forzar = false) {
    db.categorias = Array.isArray(db.categorias) ? db.categorias : [];
    db.productos = Array.isArray(db.productos) ? db.productos : [];
    db.promos = Array.isArray(db.promos) ? db.promos : [];
    db.seq = db.seq || {};
    if (!db.seq.categorias) db.seq.categorias = db.categorias.length + 1;
    if (!db.seq.productos) db.seq.productos = db.productos.length + 1;
    if (!db.seq.promos) db.seq.promos = db.promos.length + 1;

    const prodsActivos = db.productos.filter((p) => p.activo !== false).length;
    const promosActivas = db.promos.filter((pr) => pr.activo !== false).length;
    if (!forzar && prodsActivos > 0 && promosActivas > 0) {
      return { ok: true, sembrado: false };
    }

    const ahora = ahoraISO();
    const catMap = {};
    for (const nomCat of CATEGORIAS_BEBIDAS_SEED) {
      let c = db.categorias.find((x) => String(x.nombre).toLowerCase() === nomCat.toLowerCase());
      if (!c) {
        c = {
          id: db.seq.categorias++,
          comercio_id: 1,
          nombre: nomCat,
          descripcion: '',
          activo: true,
          fecha_baja: null,
          created_at: ahora,
        };
        db.categorias.push(c);
      } else {
        c.activo = true;
        c.fecha_baja = null;
      }
      catMap[nomCat] = c.id;
    }

    const prodNameToId = {};
    for (const p of db.productos) {
      prodNameToId[p.nombre] = p.id;
    }

    let idx = 1;
    for (const item of PRODUCTOS_BEBIDAS_SEED) {
      const [prodNom, catNom, precioVta, stockIni] = item;
      const catId = catMap[catNom] || null;
      if (prodNameToId[prodNom]) {
        if (forzar) {
          const ex = db.productos.find((x) => x.id === prodNameToId[prodNom]);
          if (ex) {
            ex.categoria_id = catId;
            ex.precio_venta = Number(precioVta);
            ex.precio_costo = redondear2(Number(precioVta) * 0.7);
            ex.activo = true;
          }
        }
      } else {
        const pid = db.seq.productos++;
        const codigo = `BEB-${String(idx).padStart(3, '0')}`;
        db.productos.push({
          id: pid,
          comercio_id: 1,
          codigo,
          nombre: prodNom,
          categoria_id: catId,
          unidad_medida: 'unid',
          precio_costo: redondear2(Number(precioVta) * 0.7),
          margen_ganancia: 42.86,
          usa_precio_manual: true,
          precio_venta: Number(precioVta),
          stock_actual: Number(stockIni),
          stock_minimo: 5,
          puntos_canje: 0,
          imagen_url: '',
          activo: true,
          fecha_baja: null,
          created_at: ahora,
        });
        prodNameToId[prodNom] = pid;
      }
      idx++;
    }

    if (promosActivas === 0 || forzar) {
      for (const pRaw of PROMOS_BEBIDAS_SEED) {
        const fijosRes = (pRaw.fijos || [])
          .map((f) => {
            const pNom = Array.isArray(f) ? f[0] : f.producto;
            const cant = Array.isArray(f) ? f[1] : f.cantidad;
            return {
              producto_id: prodNameToId[pNom] || 0,
              nombre: pNom,
              cantidad: Number(cant || 1),
            };
          })
          .filter((f) => f.producto_id > 0);

        const eleccionesRes = (pRaw.elecciones || [])
          .map((g) => ({
            titulo: g.titulo || 'Elegir opción',
            cantidad: Number(g.cantidad || 1),
            opciones: (g.opciones || [])
              .map((op) => {
                const pNom = Array.isArray(op) ? op[0] : op.producto;
                const ext = Array.isArray(op) ? op[1] : op.extra;
                return {
                  producto_id: prodNameToId[pNom] || 0,
                  nombre: pNom,
                  extra: Number(ext || 0),
                };
              })
              .filter((op) => op.producto_id > 0),
          }))
          .filter((g) => g.opciones.length > 0);

        let exPr = db.promos.find((x) => String(x.nombre).toLowerCase() === String(pRaw.nombre).toLowerCase());
        if (exPr) {
          if (forzar) {
            exPr.categoria = pRaw.categoria || 'General';
            exPr.descripcion = pRaw.descripcion || '';
            exPr.precio_venta = Number(pRaw.precio_venta ?? pRaw.precio ?? 0);
            exPr.fijos = fijosRes;
            exPr.elecciones = eleccionesRes;
            exPr.activo = true;
          }
        } else {
          db.promos.push({
            id: db.seq.promos++,
            comercio_id: 1,
            nombre: pRaw.nombre,
            categoria: pRaw.categoria || 'General',
            descripcion: pRaw.descripcion || '',
            precio_venta: Number(pRaw.precio_venta ?? pRaw.precio ?? 0),
            fijos: fijosRes,
            elecciones: eleccionesRes,
            activo: true,
            created_at: ahora,
          });
        }
      }
    }
    return { ok: true, sembrado: true };
  }

  let _dbCache = null;
  let _dbCacheSerializado = '';

  function cargarDB() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw && raw === _dbCacheSerializado && _dbCache) {
        return _dbCache;
      }
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          parsed.config = { ...CONFIG_DEFAULT, ...(parsed.config || {}) };
          parsed.seq = { ...crearEstadoInicial().seq, ...(parsed.seq || {}) };
          parsed.categorias = Array.isArray(parsed.categorias) ? parsed.categorias : [];
          parsed.productos = Array.isArray(parsed.productos) ? parsed.productos : [];
          parsed.promos = Array.isArray(parsed.promos) ? parsed.promos : [];
          parsed.clientes = Array.isArray(parsed.clientes) ? parsed.clientes : [];
          parsed.ventas = Array.isArray(parsed.ventas) ? parsed.ventas : [];
          parsed.detalle_ventas = Array.isArray(parsed.detalle_ventas) ? parsed.detalle_ventas : [];
          parsed.movimientos_caja = Array.isArray(parsed.movimientos_caja) ? parsed.movimientos_caja : [];
          parsed.movimientos_cuenta = Array.isArray(parsed.movimientos_cuenta) ? parsed.movimientos_cuenta : [];
          parsed.cierres_caja = Array.isArray(parsed.cierres_caja) ? parsed.cierres_caja : [];
          parsed.auditoria_movimientos = Array.isArray(parsed.auditoria_movimientos) ? parsed.auditoria_movimientos : [];
          parsed.usuarios = Array.isArray(parsed.usuarios) && parsed.usuarios.length ? parsed.usuarios : crearEstadoInicial().usuarios;
          parsed.estado_backup = parsed.estado_backup || crearEstadoInicial().estado_backup;
          if (!parsed.productos.length || !parsed.promos.length) {
            sembrarCatalogoBebidasEnDB(parsed, false);
          }
          _dbCache = parsed;
          _dbCacheSerializado = raw;
          return _dbCache;
        }
      }
    } catch (e) {
      console.warn('Error leyendo DB local:', e);
    }
    if (!_dbCache) {
      _dbCache = crearEstadoInicial();
      sembrarCatalogoBebidasEnDB(_dbCache, false);
      _dbCacheSerializado = JSON.stringify(_dbCache);
      try {
        localStorage.setItem(STORAGE_KEY, _dbCacheSerializado);
      } catch (_) {}
    }
    return _dbCache;
  }

  function guardarDB(db, marcarCambio = true, motivoCambio = 'Datos actualizados') {
    _dbCache = db;
    if (marcarCambio) {
      const f = fmtFechaAR(ahoraISO());
      db.estado_backup = db.estado_backup || {};
      db.estado_backup.cambios_pendientes = true;
      db.estado_backup.ultimo_cambio_local = f;
      db.estado_backup.ultimo_backup_local = f;
      db.estado_backup.ultimo_mensaje = `${motivoCambio} (${f}). Guardado en el celular.`;
    }

    // 1. Guardar de forma robusta e incondicional en IndexedDB (almacenamiento masivo sin límite de 5MB)
    guardarEnIndexedDB(db);

    // 2. Guardar en localStorage como caché inmediato
    try {
      const serializado = JSON.stringify(db);
      _dbCacheSerializado = serializado;
      db.estado_backup.ultimo_tamano_kb = Math.max(1, Math.round((serializado.length / 1024) * 10) / 10);
      localStorage.setItem(STORAGE_KEY, serializado);
    } catch (e) {
      console.warn('LocalStorage superó cuota; guardando versión compacta protegida:', e);
      try {
        const copiaLigera = JSON.parse(JSON.stringify(db));
        if (copiaLigera.config) {
          if (copiaLigera.config.logo_imagen && copiaLigera.config.logo_imagen.length > 25000) {
            copiaLigera.config.logo_imagen = '';
          }
          if (copiaLigera.config.qr_imagen && copiaLigera.config.qr_imagen.length > 25000) {
            copiaLigera.config.qr_imagen = '';
          }
        }
        if (Array.isArray(copiaLigera.productos)) {
          copiaLigera.productos.forEach((p) => {
            if (p.imagen && p.imagen.length > 25000) p.imagen = '';
            if (p.imagen_url && p.imagen_url.length > 25000) p.imagen_url = '';
          });
        }
        const ligeraSerializada = JSON.stringify(copiaLigera);
        _dbCacheSerializado = ligeraSerializada;
        localStorage.setItem(STORAGE_KEY, ligeraSerializada);
      } catch (err2) {
        console.error('Error crítico guardando en localStorage:', err2);
      }
    }
  }

  function guardarEnIndexedDB(db) {
    try {
      if (!window.indexedDB) return;
      const req = indexedDB.open(IDB_NAME, 1);
      req.onupgradeneeded = (ev) => {
        const idb = ev.target.result;
        if (!idb.objectStoreNames.contains(IDB_STORE)) {
          idb.createObjectStore(IDB_STORE);
        }
      };
      req.onsuccess = (ev) => {
        try {
          const idb = ev.target.result;
          const tx = idb.transaction(IDB_STORE, 'readwrite');
          tx.objectStore(IDB_STORE).put(JSON.stringify(db), 'datos_actual');
        } catch (_) {}
      };
    } catch (_) {}
  }

  function recuperarDesdeIndexedDBSiFalta() {
    try {
      if (!window.indexedDB) return;
      const req = indexedDB.open(IDB_NAME, 1);
      req.onupgradeneeded = (ev) => {
        const idb = ev.target.result;
        if (!idb.objectStoreNames.contains(IDB_STORE)) {
          idb.createObjectStore(IDB_STORE);
        }
      };
      req.onsuccess = (ev) => {
        try {
          const idb = ev.target.result;
          const tx = idb.transaction(IDB_STORE, 'readonly');
          const getReq = tx.objectStore(IDB_STORE).get('datos_actual');
          getReq.onsuccess = () => {
            if (getReq.result) {
              try {
                const idbData = JSON.parse(getReq.result);
                const rawLocal = localStorage.getItem(STORAGE_KEY);
                const localData = rawLocal ? JSON.parse(rawLocal) : null;
                const localCats = (localData && Array.isArray(localData.categorias)) ? localData.categorias.length : 0;
                const localProds = (localData && Array.isArray(localData.productos)) ? localData.productos.length : 0;
                const idbCats = (idbData && Array.isArray(idbData.categorias)) ? idbData.categorias.length : 0;
                const idbProds = (idbData && Array.isArray(idbData.productos)) ? idbData.productos.length : 0;

                if (!localData || idbCats > localCats || idbProds > localProds || (!localData.config?.logo_imagen && idbData.config?.logo_imagen)) {
                  _dbCache = idbData;
                  guardarDB(_dbCache, false);
                  sincronizarIdentidadComercio();
                }
              } catch (_) {}
            }
          };
        } catch (_) {}
      };
    } catch (_) {}
  }

  // =========================================================================
  // 1.5 Identidad Visual Persistente (Color, Nombre de Fantasía, Logo y QR)
  // =========================================================================
  function normalizarUrlImagen(url) {
    const u = String(url || '').trim();
    if (!u || u === 'data:,') return '';
    if (u.startsWith('data:image/')) return u;
    // Convertir enlaces compartidos de Google Drive en enlace directo de imagen
    const matchDriveFile = u.match(/drive\.google\.com\/file\/d\/([a-zA-Z0-9_-]+)/);
    if (matchDriveFile && matchDriveFile[1]) {
      return `https://lh3.googleusercontent.com/d/${matchDriveFile[1]}`;
    }
    const matchDriveId = u.match(/drive\.google\.com\/(?:open|uc|thumbnail)\?[^#]*id=([a-zA-Z0-9_-]+)/);
    if (matchDriveId && matchDriveId[1]) {
      return `https://lh3.googleusercontent.com/d/${matchDriveId[1]}`;
    }
    return u;
  }

  function obtenerConfigComercio(fallback = {}) {
    const db = cargarDB();
    const Combinada = { ...CONFIG_DEFAULT, ...(fallback || {}), ...(db.config || {}) };
    Combinada.logo_imagen = normalizarUrlImagen(Combinada.logo_imagen);
    Combinada.qr_imagen = normalizarUrlImagen(Combinada.qr_imagen);
    if (!/^#[0-9a-fA-F]{6}$/.test(String(Combinada.color_primario || ''))) {
      Combinada.color_primario = '#2563EB';
    }
    if (!String(Combinada.nombre_comercio || '').trim()) {
      Combinada.nombre_comercio = 'Mi Comercio';
    }
    return Combinada;
  }

  function sincronizarIdentidadComercio(cfgOpcional) {
    const cfg = obtenerConfigComercio(cfgOpcional);
    const color = cfg.color_primario || '#2563EB';
    const nombre = cfg.nombre_comercio || 'Mi Comercio';
    const logoUrl = cfg.logo_imagen || '';

    // 1. Aplicar variables CSS globales antes del renderizado para que nunca vuelva al azul por defecto
    const root = document.documentElement;
    if (root && root.style) {
      root.style.setProperty('--color-primario', color);
      root.style.setProperty('--color-primario-suave', `color-mix(in srgb, ${color} 10%, white)`);
      root.style.setProperty('--color-primario-oscuro', `color-mix(in srgb, ${color} 85%, black)`);
    }
    const metaTheme = document.querySelector('meta[name="theme-color"]');
    if (metaTheme) metaTheme.setAttribute('content', color);

    // 2. Actualizar el título de la pestaña con el Nombre de Fantasía real
    if (document.title) {
      if (document.title.includes('·')) {
        const sufijo = document.title.split('·').slice(1).join('·').trim();
        document.title = `${nombre} · ${sufijo}`;
      } else if (document.title.includes('—')) {
        const partes = document.title.split('—');
        document.title = `${partes[0].trim()} — ${nombre}`;
      } else {
        document.title = nombre;
      }
    }

    // 3. Actualizar textos estáticos del Nombre de Fantasía (Login, Auditoría, Encabezados)
    document.querySelectorAll('[data-brand-nombre]').forEach((el) => {
      if (!el.hasAttribute('x-text')) {
        el.textContent = nombre;
      }
    });

    // 4. Actualizar el Logo del Negocio en el encabezado de todas las pantallas y en Login
    const cajasLogo = document.querySelectorAll('[data-brand-logo], header .w-9.h-9');
    cajasLogo.forEach((box) => {
      const mostrarIconoTienda = () => {
        box.innerHTML = '<i data-lucide="store" class="w-5 h-5"></i>';
        box.classList.remove('bg-white', 'border', 'border-gray-200');
        box.classList.add('bg-primario', 'text-white');
        if (window.lucide) {
          try { window.lucide.createIcons(); } catch (_) {}
        }
      };

      if (logoUrl) {
        box.innerHTML = '';
        const img = document.createElement('img');
        img.src = logoUrl;
        img.alt = nombre;
        img.className = 'w-full h-full object-contain rounded-lg p-0.5 bg-white';
        img.onerror = mostrarIconoTienda;
        box.appendChild(img);
        box.classList.remove('bg-primario', 'text-white');
        box.classList.add('bg-white', 'border', 'border-gray-200', 'overflow-hidden');
      } else {
        mostrarIconoTienda();
      }
    });
  }

  window.normalizarUrlImagen = normalizarUrlImagen;
  window.obtenerConfigComercio = obtenerConfigComercio;
  window.sincronizarIdentidadComercio = sincronizarIdentidadComercio;

  // Solicitar almacenamiento persistente en Android para que nunca borre datos
  if (navigator.storage && navigator.storage.persist) {
    navigator.storage.persist().catch(() => {});
  }
  recuperarDesdeIndexedDBSiFalta();
  // Aplicar color primario inmediatamente en <head> antes de pintar la pantalla
  sincronizarIdentidadComercio();
  document.addEventListener('DOMContentLoaded', () => sincronizarIdentidadComercio());
  window.addEventListener('pageshow', () => sincronizarIdentidadComercio());

  // =========================================================================
  // 2. Sesión y Auditoría
  // =========================================================================
  function obtenerSesion() {
    try {
      const raw = sessionStorage.getItem(SESSION_KEY);
      if (raw) {
        const s = JSON.parse(raw);
        if (s && s.usuario_id) return s;
      }
    } catch (_) {}
    return null;
  }

  function guardarSesion(s) {
    try {
      if (!s) {
        sessionStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(SESSION_KEY);
      } else {
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
        localStorage.removeItem(SESSION_KEY);
      }
    } catch (_) {}
  }

  function registrarAuditoria(db, tipoAccion, detalles) {
    const ses = obtenerSesion() || { usuario_id: 1, usuario: 'Dueño' };
    const id = db.seq.auditoria++;
    db.auditoria_movimientos.unshift({
      id,
      comercio_id: 1,
      fecha_hora: ahoraISO(),
      usuario_id: ses.usuario_id,
      usuario_nombre: ses.usuario || 'Dueño',
      tipo_accion: tipoAccion,
      detalles: detalles || {},
    });
    if (db.auditoria_movimientos.length > 500) {
      db.auditoria_movimientos.length = 500;
    }
  }

  // =========================================================================
  // 3. Auto-Backup a Google Drive cuando hay Wi-Fi / Internet
  // =========================================================================
  function evaluarConexionCelular(cfg) {
    const online = navigator.onLine;
    const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const tipo = conn && conn.type ? String(conn.type).toLowerCase() : '';
    const esDatosMoviles = tipo === 'cellular';
    const esWifi = online && !esDatosMoviles;

    if (!online) {
      return {
        online: false,
        ssid_actual: '',
        es_wifi_seguro: false,
        motivo: 'Sin internet (Modo Offline en el celular). Todos los datos se guardan en el teléfono.',
      };
    }
    if (esDatosMoviles && cfg.modo_wifi_backup === 'wifi_especifico') {
      return {
        online: true,
        ssid_actual: 'Datos móviles',
        es_wifi_seguro: false,
        motivo: 'Conectado con datos móviles. Esperando Wi-Fi para hacer el backup en Google Drive.',
      };
    }
    return {
      online: true,
      ssid_actual: esWifi ? 'Wi-Fi Activo' : 'Internet Activo',
      es_wifi_seguro: true,
      motivo: 'Conectado a internet por Wi-Fi. Listo para respaldar en Google Drive.',
    };
  }

  function armarSnapshotJSON(db) {
    return {
      version: '1.0',
      timestamp: ahoraISO(),
      comercio_id: 1,
      config: db.config,
      usuarios: db.usuarios,
      tablas: {
        categorias: db.categorias,
        clientes: db.clientes,
        productos: db.productos,
        promos: db.promos || [],
        ventas: db.ventas,
        detalle_ventas: db.detalle_ventas,
        movimientos_caja: db.movimientos_caja,
        movimientos_cuenta: db.movimientos_cuenta,
        cierres_caja: db.cierres_caja,
        auditoria_movimientos: db.auditoria_movimientos,
      },
    };
  }

  async function ejecutarBackupNube(forzar = false, motivo = 'Backup automático') {
    const db = cargarDB();
    const cfg = db.config || CONFIG_DEFAULT;
    const cond = evaluarConexionCelular(cfg);
    const ahoraTxt = fmtFechaAR(ahoraISO());
    const snap = armarSnapshotJSON(db);
    const jsonStr = JSON.stringify(snap, null, 2);
    const tamanoKb = Math.max(1, Math.round((jsonStr.length / 1024) * 10) / 10);

    db.estado_backup = db.estado_backup || {};
    db.estado_backup.ultimo_backup_local = ahoraTxt;
    db.estado_backup.ultimo_tamano_kb = tamanoKb;

    // 1. Si hay servidor local de PC conectado en la misma red, sincronizar también con él
    try {
      await _fetchOriginal('/api/backup-drive/sincronizar-desde-celular', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: jsonStr,
      });
    } catch (_) {}

    // 2. Si el usuario configuró su URL de Puente Directo a Google Drive y hay Wi-Fi
    const webhookUrl = String(cfg.google_drive_webhook_url || '').trim();
    if (webhookUrl && (cond.es_wifi_seguro || (forzar && cond.online))) {
      try {
        const b64 = btoa(unescape(encodeURIComponent(jsonStr)));
        const sello = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 15);
        const nombreLimpio = String(cfg.nombre_comercio || 'Comercio').replace(/[^a-zA-Z0-9]/g, '_');
        const payload = {
          filename: `datos_backup_${nombreLimpio}_${sello}.json`,
          latest_filename: `datos_backup_${nombreLimpio}_ACTUAL.json`,
          mimeType: 'application/json',
          fileBase64: b64,
          comercio: cfg.nombre_comercio,
          timestamp: sello,
        };
        await _fetchOriginal(webhookUrl, {
          method: 'POST',
          mode: 'no-cors',
          headers: { 'Content-Type': 'text/plain;charset=utf-8' },
          body: JSON.stringify(payload),
        });
        db.estado_backup.cambios_pendientes = false;
        db.estado_backup.ultimo_backup_nube = ahoraTxt;
        db.estado_backup.ultimo_estado = 'ok_nube';
        db.estado_backup.ultimo_mensaje = `Carpeta "datos" subida automáticamente a Google Drive (${ahoraTxt} · ${tamanoKb} KB).`;
        guardarDB(db, false);
        return { ok: true, estado: db.estado_backup, wifi: cond, subido_nube: true };
      } catch (err) {
        db.estado_backup.ultimo_mensaje = `Guardado en el celular (${ahoraTxt}). No se pudo conectar con Google Drive: ${err.message || err}`;
        guardarDB(db, false);
      }
    }

    if (cond.es_wifi_seguro) {
      db.estado_backup.cambios_pendientes = false;
      db.estado_backup.ultimo_estado = 'ok_local_wifi';
      db.estado_backup.ultimo_mensaje = webhookUrl
        ? `Respaldo listo en el celular (${ahoraTxt} · ${tamanoKb} KB).`
        : `Datos respaldados en el celular (${ahoraTxt} · ${tamanoKb} KB). Configurá el enlace de Google Drive o tocá Descargar para subir a tu Drive.`;
    } else {
      db.estado_backup.ultimo_estado = 'pendiente_wifi';
      db.estado_backup.ultimo_mensaje = `Datos guardados en el celular (${ahoraTxt} · ${tamanoKb} KB). Esperando conexión Wi-Fi para subir a Google Drive.`;
    }
    guardarDB(db, false);
    return { ok: true, estado: db.estado_backup, wifi: cond, subido_nube: false };
  }

  // Vigilar cuando el celular se conecta al Wi-Fi de la casa
  window.addEventListener('online', () => {
    const db = cargarDB();
    if (db.config && db.config.auto_backup_activo && db.estado_backup && db.estado_backup.cambios_pendientes) {
      ejecutarBackupNube(false, 'Conexión Wi-Fi detectada');
    }
  });

  setInterval(() => {
    try {
      const db = cargarDB();
      if (navigator.onLine && db.config && db.config.auto_backup_activo && db.estado_backup && db.estado_backup.cambios_pendientes) {
        ejecutarBackupNube(false, 'Auto-backup periódico en Wi-Fi');
      }
    } catch (_) {}
  }, 45000);

  // =========================================================================
  // 4. Helpers de Negocio (Productos, Categorías, Caja, Arqueo, WhatsApp)
  // =========================================================================
  function obtenerMapaCategorias(db) {
    const m = {};
    for (const c of db.categorias) {
      m[c.id] = c.nombre;
    }
    return m;
  }

  function formatearProductoOut(db, p) {
    const cats = obtenerMapaCategorias(db);
    return {
      id: p.id,
      nombre: p.nombre,
      codigo: p.codigo || '',
      codigo_barras: p.codigo || '',
      categoria_id: p.categoria_id || null,
      categoria: cats[p.categoria_id] || p.categoria || 'Sin categoría',
      unidad_medida: p.unidad_medida || 'unid',
      precio_costo: Number(p.precio_costo || 0),
      margen_ganancia: Number(p.margen_ganancia || 0),
      usa_precio_manual: p.usa_precio_manual !== false,
      precio: Number(p.precio_venta ?? p.precio ?? 0),
      stock: Number(p.stock_actual ?? p.stock ?? 0),
      stock_minimo: Number(p.stock_minimo ?? 0),
      imagen: p.imagen_url || p.imagen || '',
      puntos_canje: Number(p.puntos_canje || 0),
    };
  }

  function formatearPromoOut(db, pr) {
    const prodMap = {};
    for (const p of db.productos) {
      if (p.activo !== false) prodMap[p.id] = p;
    }
    const fijosRaw = Array.isArray(pr.fijos) ? pr.fijos : [];
    const eleccionesRaw = Array.isArray(pr.elecciones) ? pr.elecciones : [];

    const fijosOut = [];
    const limitesCombos = [];
    const faltantes = [];

    for (const f of fijosRaw) {
      const pid = Number(f.producto_id || 0);
      const cant = Number(f.cantidad || 1);
      const pInfo = prodMap[pid];
      const nom = pInfo ? pInfo.nombre : (f.nombre || `Producto #${pid}`);
      const stk = pInfo ? Number(pInfo.stock_actual ?? pInfo.stock ?? 0) : 0;
      const posibles = cant > 0 ? Math.floor(stk / cant) : 0;
      limitesCombos.push(posibles);
      if (posibles <= 0) faltantes.push(nom);
      fijosOut.push({
        producto_id: pid,
        nombre: nom,
        cantidad: cant,
        stock_actual: stk,
      });
    }

    const eleccionesOut = [];
    for (const grp of eleccionesRaw) {
      const cantGrp = Number(grp.cantidad || 1);
      const opcionesOut = [];
      let maxPosiblesGrp = 0;
      for (const op of (grp.opciones || [])) {
        const pid = Number(op.producto_id || 0);
        const pInfo = prodMap[pid];
        const nom = pInfo ? pInfo.nombre : (op.nombre || `Producto #${pid}`);
        const stk = pInfo ? Number(pInfo.stock_actual ?? pInfo.stock ?? 0) : 0;
        const posOp = cantGrp > 0 ? Math.floor(stk / cantGrp) : 0;
        if (posOp > maxPosiblesGrp) maxPosiblesGrp = posOp;
        opcionesOut.push({
          producto_id: pid,
          nombre: nom,
          extra: Number(op.extra || 0),
          stock_actual: stk,
          disponible: posOp > 0,
        });
      }
      if (opcionesOut.length > 0) {
        limitesCombos.push(maxPosiblesGrp);
        if (maxPosiblesGrp <= 0) faltantes.push(grp.titulo || 'Opción elegible');
        eleccionesOut.push({
          titulo: grp.titulo || 'Elegir opción',
          cantidad: cantGrp,
          opciones: opcionesOut,
        });
      }
    }

    const combosDisp = limitesCombos.length ? Math.min(...limitesCombos) : 999;
    return {
      id: Number(pr.id),
      nombre: pr.nombre,
      categoria: pr.categoria || 'General',
      descripcion: pr.descripcion || '',
      precio: Number(pr.precio_venta ?? pr.precio ?? 0),
      fijos: fijosOut,
      elecciones: eleccionesOut,
      tiene_elecciones: eleccionesOut.length > 0,
      combos_disponibles: combosDisp,
      disponible: combosDisp > 0,
      faltantes,
      activo: pr.activo !== false,
    };
  }

  function obtenerCajaAbierta(db) {
    return db.cierres_caja.find((c) => !c.archivado_en && c.estado === 'abierta') || null;
  }

  function calcularArqueoDia(db, fechaStr) {
    const fechaUso = fechaStr || fechaLocalYYYYMMDD();
    const partes = fechaUso.split('-');
    const fechaFmt = partes.length === 3 ? `${partes[2]}/${partes[1]}/${partes[0]}` : fechaUso;

    const cajaAct = obtenerCajaAbierta(db);
    const cierresDia = db.cierres_caja.filter(
      (c) => !c.archivado_en && String(c.fecha_apertura || '').slice(0, 10) === fechaUso
    );
    const cajaRef = cajaAct || cierresDia[cierresDia.length - 1] || null;
    const saldoInicial = cajaRef ? Number(cajaRef.monto_inicial || 0) : 0;

    const ventasDia = db.ventas.filter(
      (v) => !v.archivado_en && String(v.fecha_hora || '').slice(0, 10) === fechaUso
    );
    const movsCajaDia = db.movimientos_caja.filter(
      (m) => !m.archivado_en && String(m.fecha || '').slice(0, 10) === fechaUso
    );
    const movsCuentaDia = db.movimientos_cuenta.filter(
      (m) => !m.archivado_en && String(m.fecha_hora || '').slice(0, 10) === fechaUso
    );

    let totalVendido = 0;
    let gananciaEstimada = 0;
    let ventasEfectivo = 0;
    const desgloseMap = {};

    const ventasList = ventasDia.map((v) => {
      const tot = Number(v.total_venta || 0);
      totalVendido += tot;
      const fp = v.forma_pago || 'Efectivo';
      if (!desgloseMap[fp]) desgloseMap[fp] = { forma_pago: fp, total: 0, operaciones: 0 };
      desgloseMap[fp].total = redondear2(desgloseMap[fp].total + tot);
      desgloseMap[fp].operaciones += 1;

      const fpLower = fp.toLowerCase();
      if (fpLower === 'efectivo') {
        ventasEfectivo += tot;
      } else if (fpLower.startsWith('mixto')) {
        const mEf = Number(v.monto_efectivo || 0);
        ventasEfectivo += mEf;
      }

      const dets = db.detalle_ventas.filter((d) => d.venta_id === v.id && Number(d.cantidad) > 0);
      const itemsOut = dets.map((d) => {
        const prod = db.productos.find((p) => p.id === d.producto_id);
        const cant = Number(d.cantidad || 0);
        const pu = Number(d.precio_unitario || 0);
        const cu = Number(d.costo_unitario ?? (prod ? prod.precio_costo : 0) ?? 0);
        if (pu > 0) {
          gananciaEstimada += (pu - cu) * cant;
        }
        return {
          detalle_id: d.id,
          producto_id: d.producto_id,
          codigo: prod ? prod.codigo : '',
          nombre: prod ? prod.nombre : 'Artículo',
          cantidad: cant,
          precio_unitario: pu,
          subtotal: redondear2(cant * pu),
        };
      });

      const cli = db.clientes.find((c) => c.id === v.cliente_id);
      return {
        id: v.id,
        hora: fmtHoraAR(v.fecha_hora),
        total_venta: redondear2(tot),
        forma_pago: fp,
        cliente_nombre: cli ? cli.nombre : 'Consumidor Final',
        items: itemsOut,
      };
    });

    const cobrosCtaCteEfectivo = movsCuentaDia
      .filter((m) => m.tipo === 'pago' && String(m.descripcion || '').toLowerCase().includes('efectivo'))
      .reduce((s, m) => s + Number(m.monto || 0), 0);

    const ingresosExtras = movsCajaDia
      .filter((m) => m.tipo === 'ingreso_extra')
      .reduce((s, m) => s + Number(m.monto || 0), 0);

    const egresos = movsCajaDia
      .filter((m) => m.tipo === 'egreso')
      .reduce((s, m) => s + Number(m.monto || 0), 0);

    const totalEfectivoEsperado = redondear2(
      saldoInicial + ventasEfectivo + cobrosCtaCteEfectivo + ingresosExtras - egresos
    );

    const cfg = db.config || CONFIG_DEFAULT;
    const msgWsp = [
      `*CIERRE DE CAJA - ${cfg.nombre_comercio}*`,
      `Fecha: ${fechaFmt}`,
      `Total Vendido: *${fmtMonedaAR(totalVendido, cfg.moneda)}*`,
      `Efectivo Esperado en Caja: *${fmtMonedaAR(totalEfectivoEsperado, cfg.moneda)}*`,
      `Tickets: ${ventasDia.length}`,
    ].join('\n');

    const telNegocio = String(cfg.telefono || '').replace(/\D/g, '');
    const wspUrl = armarUrlWhatsApp(telNegocio, msgWsp);

    return {
      fecha: fechaUso,
      fecha_formateada: fechaFmt,
      caja_abierta: Boolean(cajaAct),
      saldo_inicial: redondear2(saldoInicial),
      apertura_hora: cajaRef ? fmtHoraAR(cajaRef.fecha_apertura) : '',
      monto_real: cajaRef && cajaRef.monto_real != null ? redondear2(cajaRef.monto_real) : null,
      diferencia: cajaRef && cajaRef.diferencia != null ? redondear2(cajaRef.diferencia) : null,
      total_vendido: redondear2(totalVendido),
      ganancia_estimada: redondear2(gananciaEstimada),
      total_operaciones: ventasDia.length,
      ventas_efectivo: redondear2(ventasEfectivo),
      cobros_cta_cte_efectivo: redondear2(cobrosCtaCteEfectivo),
      ingresos_extras: redondear2(ingresosExtras),
      egresos: redondear2(egresos),
      total_efectivo_esperado: totalEfectivoEsperado,
      desglose_por_forma_pago: Object.values(desgloseMap),
      ventas: ventasList.reverse(),
      movimientos_caja: movsCajaDia.map((m) => ({
        id: m.id,
        hora: fmtHoraAR(m.fecha),
        tipo: m.tipo,
        monto: redondear2(m.monto),
        concepto: m.concepto,
        usuario: m.usuario_nombre || 'Dueño',
      })).reverse(),
      smtp_configurado: false,
      email_destinatario: cfg.email_destinatario || '',
      whatsapp_url: wspUrl,
    };
  }

  // =========================================================================
  // 5. Compresión de Imágenes en el Celular (Logo, QR, Fotos de Productos)
  // =========================================================================
  function comprimirImagenEnCanvas(file, maxLado = 400) {
    return new Promise((resolve, reject) => {
      if (!file) {
        reject(new Error('No se seleccionó imagen'));
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('No se pudo leer la imagen'));
      reader.onload = (ev) => {
        const rawDataUrl = String(ev.target.result || '');
        if (!rawDataUrl) {
          reject(new Error('Archivo vacío'));
          return;
        }
        // Preservar SVG livianos de forma nativa sin pérdida ni error de dimensiones
        if (file.type === 'image/svg+xml' && rawDataUrl.length < 100000) {
          resolve(rawDataUrl);
          return;
        }
        const img = new Image();
        img.onerror = () => {
          reject(new Error('Formato de imagen no compatible'));
        };
        img.onload = () => {
          let w = img.naturalWidth || img.width || 0;
          let h = img.naturalHeight || img.height || 0;
          if (!w || !h) {
            reject(new Error('No se pudieron leer las dimensiones de la imagen'));
            return;
          }
          if (w > maxLado || h > maxLado) {
            if (w > h) {
              h = Math.max(1, Math.round((h * maxLado) / w));
              w = maxLado;
            } else {
              w = Math.max(1, Math.round((w * maxLado) / h));
              h = maxLado;
            }
          }
          const canvas = document.createElement('canvas');
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);

          // PNG nítido para Logos con transparencia y códigos QR; JPEG liviano si es foto pesada de cámara
          const pngUrl = canvas.toDataURL('image/png');
          if (pngUrl && pngUrl !== 'data:,' && pngUrl.length <= 60000) {
            resolve(pngUrl);
            return;
          }
          const canvasJpg = document.createElement('canvas');
          canvasJpg.width = w;
          canvasJpg.height = h;
          const ctxJpg = canvasJpg.getContext('2d');
          ctxJpg.fillStyle = '#FFFFFF';
          ctxJpg.fillRect(0, 0, w, h);
          ctxJpg.drawImage(img, 0, 0, w, h);
          resolve(canvasJpg.toDataURL('image/jpeg', 0.75));
        };
        img.src = rawDataUrl;
      };
      reader.readAsDataURL(file);
    });
  }

  // =========================================================================
  // 6. Impresión / Vista de Ticket Térmico 80mm y Cierre A4 en el Celular
  // =========================================================================
  function abrirVistaImpresionTicket(ventaId) {
    const db = cargarDB();
    const v = db.ventas.find((x) => x.id === Number(ventaId));
    if (!v) {
      alert('Ticket no encontrado');
      return;
    }
    const cfg = obtenerConfigComercio(db.config);
    const cli = db.clientes.find((c) => c.id === v.cliente_id);
    const dets = db.detalle_ventas.filter((d) => d.venta_id === v.id && Number(d.cantidad) > 0);
    const colorBtn = cfg.color_primario || '#2563EB';
    const logoHtml = cfg.logo_imagen
      ? `<div style="text-align:center;margin-bottom:6px;"><img src="${cfg.logo_imagen}" alt="" style="max-width:72px;max-height:72px;object-fit:contain;"></div>`
      : '';

    const filasHtml = dets.map((d) => {
      const p = db.productos.find((x) => x.id === d.producto_id);
      const nom = p ? p.nombre : 'Artículo';
      const sub = redondear2(Number(d.cantidad) * Number(d.precio_unitario));
      return `<tr>
        <td style="padding:4px 0;">${nom}<br><small>${d.cantidad} x ${fmtMonedaAR(d.precio_unitario, cfg.moneda)}</small></td>
        <td style="text-align:right;vertical-align:bottom;font-weight:bold;">${fmtMonedaAR(sub, cfg.moneda)}</td>
      </tr>`;
    }).join('');

    const html = `<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Ticket #${v.id} - ${cfg.nombre_comercio}</title>
      <style>
        body { font-family: monospace; max-width: 310px; margin: 0 auto; padding: 16px; color: #111; }
        h2 { margin: 0 0 4px 0; text-align: center; font-size: 18px; }
        p { margin: 2px 0; font-size: 12px; text-align: center; }
        hr { border: none; border-top: 1px dashed #666; margin: 10px 0; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .no-print { margin-bottom: 16px; display: flex; gap: 8px; }
        .btn { flex: 1; padding: 10px; border-radius: 8px; border: none; font-weight: bold; cursor: pointer; font-size: 13px; }
        @media print { .no-print { display: none; } }
      </style></head><body>
      <div class="no-print">
        <button class="btn" style="background:${colorBtn};color:#fff;" onclick="window.print()">Imprimir / Guardar PDF</button>
        <button class="btn" style="background:#e5e7eb;color:#111;" onclick="window.close()">Cerrar</button>
      </div>
      ${logoHtml}
      <h2>${cfg.nombre_comercio}</h2>
      ${cfg.direccion ? `<p>${cfg.direccion}</p>` : ''}
      ${cfg.telefono ? `<p>Tel: ${cfg.telefono}</p>` : ''}
      <hr>
      <p style="text-align:left;"><strong>Ticket:</strong> #${v.id}</p>
      <p style="text-align:left;"><strong>Fecha:</strong> ${fmtFechaAR(v.fecha_hora)}</p>
      <p style="text-align:left;"><strong>Cliente:</strong> ${cli ? cli.nombre : 'Consumidor Final'}</p>
      <p style="text-align:left;"><strong>Pago:</strong> ${v.forma_pago}</p>
      <hr>
      <table>${filasHtml}</table>
      <hr>
      <p style="text-align:right;font-size:16px;font-weight:bold;">TOTAL: ${fmtMonedaAR(v.total_venta, cfg.moneda)}</p>
      <hr>
      <p>¡Gracias por su compra!</p>
      </body></html>`;

    const win = window.open('', '_blank');
    if (win) {
      win.document.write(html);
      win.document.close();
    }
  }

  function abrirVistaImpresionCierre(fechaStr) {
    const db = cargarDB();
    const res = calcularArqueoDia(db, fechaStr);
    const cfg = obtenerConfigComercio(db.config);
    const colorBtn = cfg.color_primario || '#2563EB';
    const logoHtml = cfg.logo_imagen
      ? `<img src="${cfg.logo_imagen}" alt="" style="width:48px;height:48px;object-fit:contain;border-radius:8px;border:1px solid #e5e7eb;padding:2px;">`
      : '';
    const html = `<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Cierre de Caja ${res.fecha_formateada}</title>
      <style>
        body { font-family: sans-serif; max-width: 680px; margin: 0 auto; padding: 20px; color: #111; }
        .card { border: 1px solid #ddd; border-radius: 10px; padding: 14px; margin-bottom: 14px; }
        .row { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #f3f4f6; font-size: 14px; }
        .no-print { margin-bottom: 16px; display: flex; gap: 8px; }
        .btn { padding: 10px 16px; border-radius: 8px; border: none; font-weight: bold; cursor: pointer; }
        @media print { .no-print { display: none; } }
      </style></head><body>
      <div class="no-print">
        <button class="btn" style="background:${colorBtn};color:#fff;" onclick="window.print()">Imprimir / Guardar PDF</button>
        <button class="btn" style="background:#e5e7eb;color:#111;" onclick="window.close()">Cerrar</button>
      </div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
        ${logoHtml}
        <h2 style="margin:0;">${cfg.nombre_comercio} — Cierre de Caja Diario</h2>
      </div>
      <p>Fecha: <strong>${res.fecha_formateada}</strong></p>
      <div class="card">
        <div class="row"><span>Total Vendido del Día:</span><strong>${fmtMonedaAR(res.total_vendido, cfg.moneda)}</strong></div>
        <div class="row"><span>Ganancia Estimada:</span><strong>${fmtMonedaAR(res.ganancia_estimada, cfg.moneda)}</strong></div>
        <div class="row"><span>Cantidad de Tickets:</span><strong>${res.total_operaciones}</strong></div>
      </div>
      <div class="card">
        <h3>Arqueo de Efectivo en Cajón</h3>
        <div class="row"><span>+ Saldo Inicial (Apertura):</span><span>${fmtMonedaAR(res.saldo_inicial, cfg.moneda)}</span></div>
        <div class="row"><span>+ Ventas en Efectivo:</span><span>${fmtMonedaAR(res.ventas_efectivo, cfg.moneda)}</span></div>
        <div class="row"><span>+ Cobros Cta. Corriente (Efectivo):</span><span>${fmtMonedaAR(res.cobros_cta_cte_efectivo, cfg.moneda)}</span></div>
        <div class="row"><span>+ Ingresos Extras:</span><span>${fmtMonedaAR(res.ingresos_extras, cfg.moneda)}</span></div>
        <div class="row"><span>- Egresos / Gastos:</span><span>-${fmtMonedaAR(res.egresos, cfg.moneda)}</span></div>
        <div class="row" style="font-size:16px;font-weight:bold;"><span>Efectivo Esperado por Sistema:</span><span>${fmtMonedaAR(res.total_efectivo_esperado, cfg.moneda)}</span></div>
        ${res.monto_real != null ? `<div class="row"><span>Efectivo Real Contado:</span><strong>${fmtMonedaAR(res.monto_real, cfg.moneda)}</strong></div>` : ''}
        ${res.diferencia != null ? `<div class="row"><span>Diferencia (Sobrante/Faltante):</span><strong>${fmtMonedaAR(res.diferencia, cfg.moneda)}</strong></div>` : ''}
      </div>
      </body></html>`;
    const win = window.open('', '_blank');
    if (win) {
      win.document.write(html);
      win.document.close();
    }
  }

  function descargarArchivoEnCelular(nombre, contenidoStr, mime = 'application/json') {
    const blob = new Blob([contenidoStr], { type: mime });
    const file = new File([blob], nombre, { type: mime });

    // Si está en un celular Android/iOS con menú nativo de compartir archivos (permite tocar "Guardar en Drive")
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      navigator.share({
        title: nombre,
        text: 'Respaldo de carpeta datos del Sistema de Gestión',
        files: [file],
      }).catch(() => {});
    }

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = nombre;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1500);
  }

  // =========================================================================
  // 7. Router Local de la API (/api/*) dentro del Celular
  // =========================================================================
  function respuestaJSON(data, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  async function leerBodyJSON(init) {
    if (!init || !init.body) return {};
    if (typeof init.body === 'string') {
      try {
        return JSON.parse(init.body);
      } catch (_) {
        return {};
      }
    }
    return {};
  }

  const _fetchOriginal = window.fetch.bind(window);

  window.fetch = async function (input, init = {}) {
    const urlStr = typeof input === 'string' ? input : (input && input.url ? input.url : '');
    let parsedUrl;
    try {
      parsedUrl = new URL(urlStr, window.location.origin);
    } catch (_) {
      return _fetchOriginal(input, init);
    }

    if (parsedUrl.origin !== window.location.origin) {
      return _fetchOriginal(input, init);
    }

    const path = parsedUrl.pathname;
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();

    if (!path.startsWith('/api/') && path !== '/manifest.json') {
      return _fetchOriginal(input, init);
    }

    const db = cargarDB();
    const cfg = db.config || CONFIG_DEFAULT;

    // --- Manifest PWA dinámico ---
    if (path === '/manifest.json') {
      return respuestaJSON({
        name: `${cfg.nombre_comercio} — POS Celular`,
        short_name: String(cfg.nombre_comercio || 'POS').slice(0, 18),
        start_url: '/',
        display: 'standalone',
        background_color: '#F7F9FA',
        theme_color: cfg.color_primario || '#2563EB',
        icons: [
          {
            src: cfg.logo_imagen || '/static/icon-192.svg',
            sizes: '192x192',
            type: 'image/svg+xml',
          },
        ],
      });
    }

    // --- 1. Autenticación, Usuarios y Barra ---
    if (path === '/api/usuarios-login' && method === 'GET') {
      const activos = (db.usuarios || [])
        .filter((u) => u.activo !== false)
        .sort((a, b) => (a.rol === 'admin' ? -1 : 1) - (b.rol === 'admin' ? -1 : 1) || a.id - b.id);
      return respuestaJSON(
        activos.map((u) => ({ id: u.id, nombre: u.nombre, rol: u.rol }))
      );
    }

    if (path === '/api/usuarios' && method === 'GET') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede gestionar usuarios' }, 403);
      }
      const lista = (db.usuarios || [])
        .slice()
        .sort((a, b) => (a.rol === 'admin' ? -1 : 1) - (b.rol === 'admin' ? -1 : 1) || a.id - b.id);
      return respuestaJSON(
        lista.map((u) => ({
          id: u.id,
          nombre: u.nombre,
          rol: u.rol || 'cajero',
          activo: u.activo !== false,
          created_at: u.created_at || '',
        }))
      );
    }

    if (path === '/api/usuarios' && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede crear cajeros' }, 403);
      }
      const body = await leerBodyJSON(init);
      const nombre = String(body.nombre || '').trim();
      const pin = String(body.pin || '').trim();
      if (!nombre) return respuestaJSON({ detail: 'El nombre del cajero es obligatorio' }, 400);
      if (!/^\d{4}$/.test(pin)) return respuestaJSON({ detail: 'El PIN debe tener exactamente 4 números' }, 400);

      db.usuarios = db.usuarios || [];
      const ex = db.usuarios.find((u) => String(u.nombre || '').toLowerCase() === nombre.toLowerCase());
      if (ex) {
        if (ex.activo !== false) {
          return respuestaJSON({ detail: `Ya existe un usuario activo con el nombre "${nombre}"` }, 400);
        }
        ex.activo = true;
        ex.pin = pin;
        ex.intentos_fallidos = 0;
        ex.bloqueado_hasta = 0;
        registrarAuditoria(db, 'CAJERO_REACTIVADO', { id: ex.id, nombre: ex.nombre });
        guardarDB(db, true, `Cajero "${ex.nombre}" reactivado`);
        return respuestaJSON({ id: ex.id, nombre: ex.nombre, rol: ex.rol || 'cajero', activo: true });
      }

      const maxId = db.usuarios.reduce((m, u) => Math.max(m, Number(u.id || 0)), 0);
      const nuevo = {
        id: maxId + 1,
        comercio_id: 1,
        nombre,
        pin,
        rol: 'cajero',
        activo: true,
        created_at: ahoraISO(),
      };
      db.usuarios.push(nuevo);
      registrarAuditoria(db, 'CAJERO_CREADO', { id: nuevo.id, nombre: nuevo.nombre });
      guardarDB(db, true, `Cajero "${nuevo.nombre}" agregado`);
      return respuestaJSON({ id: nuevo.id, nombre: nuevo.nombre, rol: 'cajero', activo: true }, 201);
    }

    const matchUsuarioId = path.match(/^\/api\/usuarios\/(\d+)$/);
    if (matchUsuarioId) {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede modificar cajeros' }, 403);
      }
      const uid = Number(matchUsuarioId[1]);
      const u = (db.usuarios || []).find((x) => x.id === uid);
      if (!u) return respuestaJSON({ detail: 'Usuario no encontrado' }, 404);

      if (method === 'PUT') {
        const body = await leerBodyJSON(init);
        if (body.nombre != null) {
          const nom = String(body.nombre).trim();
          if (!nom) return respuestaJSON({ detail: 'El nombre no puede quedar vacío' }, 400);
          if (db.usuarios.some((otro) => otro.id !== uid && String(otro.nombre || '').toLowerCase() === nom.toLowerCase())) {
            return respuestaJSON({ detail: `Ya existe otro usuario llamado "${nom}"` }, 400);
          }
          u.nombre = nom;
        }
        if (body.pin != null && String(body.pin).trim() !== '') {
          const p = String(body.pin).trim();
          if (!/^\d{4}$/.test(p)) return respuestaJSON({ detail: 'El nuevo PIN debe tener 4 números' }, 400);
          u.pin = p;
          u.intentos_fallidos = 0;
          u.bloqueado_hasta = 0;
        }
        if (body.activo != null) {
          if (u.rol === 'admin' && !body.activo) {
            return respuestaJSON({ detail: 'No se puede desactivar al Dueño' }, 400);
          }
          u.activo = Boolean(body.activo);
        }
        registrarAuditoria(db, 'USUARIO_ACTUALIZADO', { id: u.id, nombre: u.nombre, activo: u.activo });
        guardarDB(db, true, `Usuario "${u.nombre}" actualizado`);
        return respuestaJSON({ id: u.id, nombre: u.nombre, rol: u.rol, activo: u.activo !== false });
      }

      if (method === 'DELETE') {
        if (u.rol === 'admin') {
          return respuestaJSON({ detail: 'No se puede dar de baja al Dueño' }, 400);
        }
        u.activo = false;
        registrarAuditoria(db, 'CAJERO_BAJA', { id: u.id, nombre: u.nombre });
        guardarDB(db, true, `Cajero "${u.nombre}" dado de baja`);
        return respuestaJSON({ ok: true, id: u.id });
      }
    }

    if (path === '/api/login' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const user = db.usuarios.find((u) => u.id === Number(body.usuario_id) && u.activo);
      const pin = String(body.pin || '').trim();
      if (!user) return respuestaJSON({ detail: 'Usuario no encontrado.' }, 401);
      const ahoraTs = Date.now();
      if (user.bloqueado_hasta && user.bloqueado_hasta > ahoraTs) {
        const segRestan = Math.ceil((user.bloqueado_hasta - ahoraTs) / 1000);
        return respuestaJSON({
          detail: {
            mensaje: `⛔ Acceso temporalmente bloqueado por seguridad. Podrás reintentar en ${segRestan} segundos.`,
            bloqueado: true,
            reintentar_en: segRestan,
            intentos_restantes: 0,
          }
        }, 429);
      }
      if (user.pin !== pin) {
        user.intentos_fallidos = (user.intentos_fallidos || 0) + 1;
        if (user.intentos_fallidos >= 5) {
          user.intentos_fallidos = 0;
          user.bloqueado_hasta = ahoraTs + 120 * 1000; // 2 minutos exactos
          db.alertas_seguridad = db.alertas_seguridad || [];
          db.alertas_seguridad.unshift({
            id: (db.seq.alertas_seguridad = (db.seq.alertas_seguridad || 1) + 1),
            comercio_id: 1,
            tipo: 'intento_fuerza_bruta',
            ip_origen: '127.0.0.1 (Offline)',
            usuario_afectado: user.nombre,
            detalles: `5 intentos fallidos consecutivos contra '${user.nombre}'. Dispositivo bloqueado por 2 minutos.`,
            fecha_hora: new Date().toLocaleString('es-AR'),
            leida: 0,
          });
          guardarDB(db, false);
          return respuestaJSON({
            detail: {
              mensaje: '⛔ Acceso bloqueado por seguridad tras 5 intentos fallidos. Podrás reintentar en 120 segundos.',
              bloqueado: true,
              reintentar_en: 120,
              intentos_restantes: 0,
            }
          }, 429);
        }
        guardarDB(db, false);
        const restan = 5 - user.intentos_fallidos;
        return respuestaJSON({
          detail: {
            mensaje: `PIN incorrecto. Te quedan ${restan} intento${restan > 1 ? 's' : ''} antes de bloquear este dispositivo por 2 minutos.`,
            bloqueado: false,
            intentos_restantes: restan,
            reintentar_en: 0,
          }
        }, 401);
      }
      user.intentos_fallidos = 0;
      user.bloqueado_hasta = 0;
      guardarDB(db, false);
      const debeCambiar = pin === '1234' || pin === '0000';
      const sesion = {
        usuario_id: user.id,
        usuario: user.nombre,
        rol: user.rol,
        debe_cambiar_pin: debeCambiar,
      };
      guardarSesion(sesion);
      return respuestaJSON({ ok: true, ...sesion });
    }

    if (path === '/api/auth/recuperar-pin-dueno' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const clave = String(body.clave_maestra || '').trim().toUpperCase();
      const nuevoPin = String(body.nuevo_pin || '').trim();
      const claveReal = String((db.config && db.config.clave_maestra_rescate) || 'RESCATE-BEBIDAS-7489').trim().toUpperCase();
      if (!clave || clave !== claveReal) {
        return respuestaJSON({ detail: { mensaje: 'Clave Maestra incorrecta.', bloqueado: false } }, 401);
      }
      if (!/^\d{4}$/.test(nuevoPin)) {
        return respuestaJSON({ detail: { mensaje: 'El nuevo PIN debe tener exactamente 4 números.' } }, 400);
      }
      const admin = db.usuarios.find((u) => u.rol === 'admin');
      if (admin) {
        admin.pin = nuevoPin;
        admin.intentos_fallidos = 0;
        admin.bloqueado_hasta = 0;
        guardarDB(db, false);
      }
      return respuestaJSON({ ok: true, mensaje: 'PIN del Dueño restablecido con éxito.' });
    }

    if (path === '/api/seguridad/alertas' && method === 'GET') {
      const alertas = (db.alertas_seguridad || []).filter((a) => !a.leida);
      return respuestaJSON(alertas.slice(0, 10));
    }

    if (path.startsWith('/api/seguridad/alertas/') && path.endsWith('/descartar') && method === 'POST') {
      const partes = path.split('/');
      const alId = Number(partes[4]);
      const al = (db.alertas_seguridad || []).find((a) => a.id === alId);
      if (al) {
        al.leida = 1;
        guardarDB(db, false);
      }
      return respuestaJSON({ ok: true });
    }

    if (path === '/api/logout' && method === 'POST') {
      guardarSesion(null);
      return respuestaJSON({ ok: true });
    }

    if (path === '/api/estado-barra' && method === 'GET') {
      const ses = obtenerSesion() || { usuario_id: 1, usuario: 'Dueño', rol: 'admin', debe_cambiar_pin: false };
      const cfg = db.config || CONFIG_DEFAULT;
      const criticos = db.productos.filter(
        (p) => p.activo !== false && Number(p.stock_actual ?? p.stock ?? 0) <= Number(p.stock_minimo ?? 0)
      ).length;
      return respuestaJSON({
        usuario: ses.usuario,
        rol: ses.rol,
        debe_cambiar_pin: Boolean(ses.debe_cambiar_pin),
        stock_critico_count: criticos,
        modo_bloqueo: cfg.modo_bloqueo || 'nunca',
        tiempo_inactividad_min: cfg.tiempo_inactividad_min || 15,
        wizard_completado: Number(cfg.wizard_completado || 0),
        modo_operacion: cfg.modo_operacion || 'autonomo_offline',
        backup: {
          cambios_pendientes: Boolean(db.estado_backup && db.estado_backup.cambios_pendientes),
          ultimo_backup_local: (db.estado_backup && db.estado_backup.ultimo_backup_local) || '',
          ultimo_estado: (db.estado_backup && db.estado_backup.ultimo_estado) || 'ok_local',
        },
      });
    }

    if (path === '/api/config/cambiar-clave' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const ses = obtenerSesion() || { usuario_id: 1 };
      const user = db.usuarios.find((u) => u.id === Number(ses.usuario_id)) || db.usuarios[0];
      const actual = String(body.pin_actual || '').trim();
      const nuevo = String(body.pin_nuevo || '').trim();
      if (user.pin !== actual) {
        return respuestaJSON({ detail: 'El PIN actual es incorrecto' }, 400);
      }
      if (!/^\d{4}$/.test(nuevo)) {
        return respuestaJSON({ detail: 'El PIN nuevo tiene que ser de 4 números' }, 400);
      }
      if (nuevo === '1234' || nuevo === '0000') {
        return respuestaJSON({ detail: 'Elegí un PIN distinto de 1234 y 0000' }, 400);
      }
      user.pin = nuevo;
      ses.debe_cambiar_pin = false;
      guardarSesion(ses);
      registrarAuditoria(db, 'CAMBIO_PIN', { usuario: user.nombre });
      guardarDB(db, true, 'PIN actualizado');
      return respuestaJSON({ ok: true, mensaje: 'PIN actualizado correctamente.' });
    }

    // --- 2. Categorías ---
    if (path === '/api/categorias' && method === 'GET') {
      return respuestaJSON(
        db.categorias
          .filter((c) => c.activo !== false)
          .map((c) => ({
            ...c,
            productos: db.productos.filter((p) => p.activo !== false && p.categoria_id === c.id).length,
          }))
      );
    }

    if (path === '/api/bajas/categorias' && method === 'GET') {
      return respuestaJSON(
        db.categorias
          .filter((c) => c.activo === false)
          .map((c) => ({
            ...c,
            productos: db.productos.filter((p) => p.categoria_id === c.id).length,
          }))
      );
    }

    if (path === '/api/categorias' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const nombre = String(body.nombre || '').trim();
      if (!nombre) return respuestaJSON({ detail: 'El nombre es obligatorio' }, 400);
      if (db.categorias.some((c) => c.activo !== false && c.nombre.toLowerCase() === nombre.toLowerCase())) {
        return respuestaJSON({ detail: `Ya existe la categoría "${nombre}"` }, 400);
      }
      const nueva = {
        id: db.seq.categorias++,
        comercio_id: 1,
        nombre,
        descripcion: String(body.descripcion || '').trim(),
        activo: true,
        fecha_baja: null,
        created_at: ahoraISO(),
      };
      db.categorias.push(nueva);
      registrarAuditoria(db, 'CREACION_CATEGORIA', { categoria_id: nueva.id, nombre: nueva.nombre });
      guardarDB(db, true, `Categoría "${nombre}" creada`);
      return respuestaJSON(nueva, 201);
    }

    const matchCatId = path.match(/^\/api\/categorias\/(\d+)$/);
    if (matchCatId) {
      const cid = Number(matchCatId[1]);
      const cat = db.categorias.find((c) => c.id === cid);
      if (!cat) return respuestaJSON({ detail: 'Categoría no encontrada' }, 404);
      if (method === 'PUT') {
        const body = await leerBodyJSON(init);
        cat.nombre = String(body.nombre || cat.nombre).trim();
        cat.descripcion = String(body.descripcion ?? cat.descripcion ?? '').trim();
        guardarDB(db, true, `Categoría "${cat.nombre}" editada`);
        return respuestaJSON(cat);
      }
      if (method === 'DELETE') {
        const ses = obtenerSesion();
        if (!ses || ses.rol !== 'admin') {
          return respuestaJSON({ detail: 'Solo el Dueño puede dar de baja categorías' }, 403);
        }
        const enUso = db.productos.some((p) => p.activo !== false && p.categoria_id === cid);
        if (enUso) {
          return respuestaJSON({ detail: 'No se puede dar de baja porque tiene productos activos asociados.' }, 400);
        }
        cat.activo = false;
        cat.fecha_baja = ahoraISO();
        registrarAuditoria(db, 'BAJA_CATEGORIA', { categoria_id: cid, nombre: cat.nombre });
        guardarDB(db, true, `Categoría "${cat.nombre}" dada de baja`);
        return respuestaJSON({ ok: true });
      }
    }

    const matchCatReact = path.match(/^\/api\/categorias\/(\d+)\/reactivar$/);
    if (matchCatReact && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede reactivar categorías' }, 403);
      }
      const cid = Number(matchCatReact[1]);
      const cat = db.categorias.find((c) => c.id === cid);
      if (!cat) return respuestaJSON({ detail: 'Categoría no encontrada' }, 404);
      cat.activo = true;
      cat.fecha_baja = null;
      guardarDB(db, true, `Categoría "${cat.nombre}" reactivada`);
      return respuestaJSON({ ok: true });
    }

    // --- 3. Productos, Promos e Inventario ---
    if (path === '/api/productos' && method === 'GET') {
      return respuestaJSON(
        db.productos.filter((p) => p.activo !== false).map((p) => formatearProductoOut(db, p))
      );
    }

    if (path === '/api/promos' && method === 'GET') {
      const incInactivas = urlObj.searchParams.get('incluir_inactivas') === 'true';
      const lista = (db.promos || []).filter((pr) => incInactivas || pr.activo !== false);
      return respuestaJSON(lista.map((pr) => formatearPromoOut(db, pr)));
    }

    if (path === '/api/promos/restaurar-catalogo' && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede restaurar el catálogo' }, 403);
      }
      const res = sembrarCatalogoBebidasEnDB(db, true);
      guardarDB(db, true, 'Catálogo de bebidas y promos restaurado');
      return respuestaJSON(res);
    }

    if (path === '/api/promos' && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede crear promos' }, 403);
      }
      const body = await leerBodyJSON(init);
      const nombre = String(body.nombre || '').trim();
      if (!nombre) return respuestaJSON({ detail: 'El nombre de la promo es obligatorio' }, 400);
      const nuevaPromo = {
        id: db.seq.promos++,
        comercio_id: 1,
        nombre,
        categoria: String(body.categoria || 'General').trim() || 'General',
        descripcion: String(body.descripcion || '').trim(),
        precio_venta: redondear2(body.precio || 0),
        fijos: Array.isArray(body.fijos) ? body.fijos : [],
        elecciones: Array.isArray(body.elecciones) ? body.elecciones : [],
        activo: body.activo !== false,
        created_at: ahoraISO(),
      };
      db.promos = db.promos || [];
      db.promos.push(nuevaPromo);
      registrarAuditoria(db, 'PROMO_CREADA', { id: nuevaPromo.id, nombre: nuevaPromo.nombre, precio: nuevaPromo.precio_venta });
      guardarDB(db, true, `Promo "${nuevaPromo.nombre}" creada`);
      return respuestaJSON(formatearPromoOut(db, nuevaPromo), 201);
    }

    const matchPromoPrecio = path.match(/^\/api\/promos\/(\d+)\/precio$/);
    if (matchPromoPrecio && (method === 'PATCH' || method === 'PUT')) {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede modificar precios de promos' }, 403);
      }
      const prid = Number(matchPromoPrecio[1]);
      const pr = (db.promos || []).find((x) => x.id === prid);
      if (!pr) return respuestaJSON({ detail: 'Promo no encontrada' }, 404);
      const body = await leerBodyJSON(init);
      pr.precio_venta = redondear2(body.precio ?? pr.precio_venta);
      if (body.nombre) pr.nombre = String(body.nombre).trim();
      if (body.descripcion != null) pr.descripcion = String(body.descripcion).trim();
      if (body.categoria) pr.categoria = String(body.categoria).trim();
      registrarAuditoria(db, 'PROMO_PRECIO_ACTUALIZADO', { id: pr.id, nombre: pr.nombre, precio: pr.precio_venta });
      guardarDB(db, true, `Precio de "${pr.nombre}" actualizado`);
      return respuestaJSON(formatearPromoOut(db, pr));
    }

    const matchPromoId = path.match(/^\/api\/promos\/(\d+)$/);
    if (matchPromoId) {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede modificar promos' }, 403);
      }
      const prid = Number(matchPromoId[1]);
      const pr = (db.promos || []).find((x) => x.id === prid);
      if (!pr) return respuestaJSON({ detail: 'Promo no encontrada' }, 404);
      if (method === 'PUT') {
        const body = await leerBodyJSON(init);
        pr.nombre = String(body.nombre || pr.nombre).trim();
        pr.categoria = String(body.categoria || pr.categoria || 'General').trim();
        pr.descripcion = String(body.descripcion ?? pr.descripcion ?? '').trim();
        pr.precio_venta = redondear2(body.precio ?? pr.precio_venta);
        if (Array.isArray(body.fijos)) pr.fijos = body.fijos;
        if (Array.isArray(body.elecciones)) pr.elecciones = body.elecciones;
        if (body.activo != null) pr.activo = Boolean(body.activo);
        registrarAuditoria(db, 'PROMO_EDITADA', { id: pr.id, nombre: pr.nombre, precio: pr.precio_venta });
        guardarDB(db, true, `Promo "${pr.nombre}" actualizada`);
        return respuestaJSON(formatearPromoOut(db, pr));
      }
      if (method === 'DELETE') {
        pr.activo = false;
        registrarAuditoria(db, 'PROMO_ELIMINADA', { id: pr.id, nombre: pr.nombre });
        guardarDB(db, true, `Promo "${pr.nombre}" eliminada`);
        return respuestaJSON({ ok: true, id: pr.id });
      }
    }

    if (path === '/api/bajas/productos' && method === 'GET') {
      return respuestaJSON(db.productos.filter((p) => p.activo === false));
    }

    if (path === '/api/inventario/stock-critico' && method === 'GET') {
      const criticos = db.productos
        .filter((p) => p.activo !== false && Number(p.stock_actual ?? p.stock ?? 0) <= Number(p.stock_minimo ?? 0))
        .map((p) => {
          const out = formatearProductoOut(db, p);
          out.deficit = Math.max(0, redondear4(out.stock_minimo - out.stock));
          return out;
        });
      return respuestaJSON(criticos);
    }

    if (path === '/api/inventario/siguiente-codigo' && method === 'GET') {
      const n = db.productos.length + 1;
      return respuestaJSON({ codigo: `SKU-${String(n).padStart(4, '0')}` });
    }

    if (path === '/api/inventario/productos' && method === 'POST') {
      const fd = (init && init.body) || (input && input.body);
      const getVal = (k) => (fd && typeof fd.get === 'function' ? fd.get(k) : (fd && fd[k] != null ? fd[k] : ''));
      const nombre = String(getVal('nombre') || '').trim();
      const codigo = String(getVal('codigo') || '').trim() || `SKU-${String(db.seq.productos).padStart(4, '0')}`;
      const catNombre = String(getVal('categoria') || 'General').trim() || 'General';
      if (!nombre) return respuestaJSON({ detail: 'El nombre del producto es obligatorio' }, 400);

      let cat = db.categorias.find((c) => c.nombre.toLowerCase() === catNombre.toLowerCase());
      if (!cat) {
        cat = { id: db.seq.categorias++, comercio_id: 1, nombre: catNombre, descripcion: '', activo: true, fecha_baja: null, created_at: ahoraISO() };
        db.categorias.push(cat);
      } else if (cat.activo === false) {
        cat.activo = true;
      }

      const prod = {
        id: db.seq.productos++,
        comercio_id: 1,
        codigo,
        nombre,
        categoria_id: cat.id,
        unidad_medida: String(getVal('unidad_medida') || 'unid'),
        precio_costo: Number(getVal('precio_costo') || 0),
        margen_ganancia: Number(getVal('margen_ganancia') || 0),
        usa_precio_manual: String(getVal('usa_precio_manual')) !== 'false',
        precio_venta: Number(getVal('precio') || 0),
        stock_actual: Number(getVal('stock') || 0),
        stock_minimo: Number(getVal('stock_minimo') || 0),
        puntos_canje: Number(getVal('puntos_canje') || 0),
        imagen_url: String(getVal('imagen_url') || ''),
        activo: true,
        fecha_baja: null,
        created_at: ahoraISO(),
      };
      db.productos.push(prod);
      registrarAuditoria(db, 'ALTA_PRODUCTO', {
        producto_id: prod.id,
        nombre: prod.nombre,
        codigo: prod.codigo,
        precio_venta: prod.precio_venta,
        stock_inicial: prod.stock_actual,
      });
      guardarDB(db, true, `Producto "${nombre}" agregado`);
      return respuestaJSON(formatearProductoOut(db, prod), 201);
    }

    const matchProdId = path.match(/^\/api\/inventario\/productos\/(\d+)$/);
    if (matchProdId) {
      const pid = Number(matchProdId[1]);
      const prod = db.productos.find((p) => p.id === pid);
      if (!prod) return respuestaJSON({ detail: 'Producto no encontrado' }, 404);
      if (method === 'PUT') {
        const ses = obtenerSesion();
        if (!ses || ses.rol !== 'admin') {
          return respuestaJSON({ detail: 'Solo el Dueño puede modificar productos existentes' }, 403);
        }
        const anteriorVenta = prod.precio_venta;
        const anteriorStock = prod.stock_actual;
        const fd = (init && init.body) || (input && input.body);
        const getVal = (k) => (fd && typeof fd.get === 'function' ? fd.get(k) : (fd && fd[k] != null ? fd[k] : null));
        const nombre = String(getVal('nombre') || prod.nombre).trim();
        const catNombre = String(getVal('categoria') || '').trim();
        if (catNombre) {
          let cat = db.categorias.find((c) => c.nombre.toLowerCase() === catNombre.toLowerCase());
          if (!cat) {
            cat = { id: db.seq.categorias++, comercio_id: 1, nombre: catNombre, descripcion: '', activo: true, fecha_baja: null, created_at: ahoraISO() };
            db.categorias.push(cat);
          }
          prod.categoria_id = cat.id;
        }
        prod.codigo = String(getVal('codigo') ?? prod.codigo).trim();
        prod.nombre = nombre;
        prod.unidad_medida = String(getVal('unidad_medida') || prod.unidad_medida || 'unid');
        prod.precio_costo = Number(getVal('precio_costo') ?? prod.precio_costo ?? 0);
        prod.margen_ganancia = Number(getVal('margen_ganancia') ?? prod.margen_ganancia ?? 0);
        prod.usa_precio_manual = String(getVal('usa_precio_manual')) !== 'false';
        prod.precio_venta = Number(getVal('precio') ?? prod.precio_venta ?? 0);
        prod.stock_actual = Number(getVal('stock') ?? prod.stock_actual ?? 0);
        prod.stock_minimo = Number(getVal('stock_minimo') ?? prod.stock_minimo ?? 0);
        prod.puntos_canje = Number(getVal('puntos_canje') ?? prod.puntos_canje ?? 0);
        if (String(getVal('eliminar_imagen')) === 'true') {
          prod.imagen_url = '';
        } else {
          const imgUrl = String(getVal('imagen_url') || '').trim();
          if (imgUrl) prod.imagen_url = imgUrl;
        }
        const cambios = {};
        if (anteriorVenta !== prod.precio_venta) cambios['Precio'] = { antes: anteriorVenta, despues: prod.precio_venta };
        if (anteriorStock !== prod.stock_actual) cambios['Stock'] = { antes: anteriorStock, despues: prod.stock_actual };
        registrarAuditoria(db, 'EDICION_PRODUCTO', {
          producto_id: prod.id,
          nombre: prod.nombre,
          codigo: prod.codigo,
          cambios,
        });
        guardarDB(db, true, `Producto "${prod.nombre}" actualizado`);
        return respuestaJSON(formatearProductoOut(db, prod));
      }
      if (method === 'DELETE') {
        const ses = obtenerSesion();
        if (!ses || ses.rol !== 'admin') {
          return respuestaJSON({ detail: 'Solo el Dueño puede dar de baja productos' }, 403);
        }
        prod.activo = false;
        prod.fecha_baja = ahoraISO();
        registrarAuditoria(db, 'BAJA_PRODUCTO', { producto_id: prod.id, nombre: prod.nombre, codigo: prod.codigo });
        guardarDB(db, true, `Producto "${prod.nombre}" dado de baja`);
        return respuestaJSON({ ok: true });
      }
    }

    const matchProdReact = path.match(/^\/api\/inventario\/productos\/(\d+)\/reactivar$/);
    if (matchProdReact && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede reactivar productos' }, 403);
      }
      const pid = Number(matchProdReact[1]);
      const prod = db.productos.find((p) => p.id === pid);
      if (!prod) return respuestaJSON({ detail: 'Producto no encontrado' }, 404);
      prod.activo = true;
      prod.fecha_baja = null;
      registrarAuditoria(db, 'REACTIVACION_PRODUCTO', { producto_id: prod.id, nombre: prod.nombre, codigo: prod.codigo });
      guardarDB(db, true, `Producto "${prod.nombre}" reactivado`);
      return respuestaJSON({ ok: true });
    }

    const matchAjusteStock = path.match(/^\/api\/inventario\/productos\/(\d+)\/ajuste-stock$/);
    if (matchAjusteStock && method === 'POST') {
      const pid = Number(matchAjusteStock[1]);
      const prod = db.productos.find((p) => p.id === pid);
      if (!prod) return respuestaJSON({ detail: 'Producto no encontrado' }, 404);
      const body = await leerBodyJSON(init);
      const cant = Number(body.cantidad || 0);
      const actual = Number(prod.stock_actual ?? prod.stock ?? 0);
      if (body.modo === 'establecer') prod.stock_actual = Math.max(0, cant);
      else if (body.modo === 'sumar') prod.stock_actual = redondear4(actual + cant);
      else prod.stock_actual = Math.max(0, redondear4(actual - cant));
      registrarAuditoria(db, 'AJUSTE_STOCK', {
        producto_id: prod.id,
        nombre: prod.nombre,
        modo: body.modo,
        stock_anterior: actual,
        stock_nuevo: prod.stock_actual,
      });
      guardarDB(db, true, `Stock ajustado en "${prod.nombre}"`);
      return respuestaJSON(formatearProductoOut(db, prod));
    }

    const matchStockMin = path.match(/^\/api\/inventario\/productos\/(\d+)\/stock-minimo$/);
    if (matchStockMin && method === 'PUT') {
      const pid = Number(matchStockMin[1]);
      const prod = db.productos.find((p) => p.id === pid);
      if (!prod) return respuestaJSON({ detail: 'Producto no encontrado' }, 404);
      const anteriorMinimo = prod.stock_minimo;
      const body = await leerBodyJSON(init);
      prod.stock_minimo = Math.max(0, Number(body.stock_minimo || 0));
      registrarAuditoria(db, 'CAMBIO_STOCK_MINIMO', {
        producto_id: prod.id,
        nombre: prod.nombre,
        stock_minimo_anterior: anteriorMinimo,
        stock_minimo_nuevo: prod.stock_minimo,
      });
      guardarDB(db, true, `Stock mínimo actualizado en "${prod.nombre}"`);
      return respuestaJSON(formatearProductoOut(db, prod));
    }

    if (path === '/api/inventario/precios-masivos' && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede aplicar aumentos masivos' }, 403);
      }
      const body = await leerBodyJSON(init);
      const pct = Number(body.porcentaje || 0);
      const red = Number(body.redondeo || 0);
      const catId = body.categoria_id ? Number(body.categoria_id) : null;
      const aplicar = Boolean(body.aplicar);

      const afectados = db.productos.filter(
        (p) => p.activo !== false && (!catId || p.categoria_id === catId)
      );
      const preview = afectados.map((p) => {
        const ant = Number(p.precio_venta || 0);
        let nuevo = ant * (1 + pct / 100);
        if (red > 0) nuevo = Math.round(nuevo / red) * red;
        else nuevo = redondear2(nuevo);
        if (aplicar) {
          p.precio_venta = nuevo;
          p.precio_costo = redondear2(Number(p.precio_costo || 0) * (1 + pct / 100));
        }
        const out = formatearProductoOut(db, p);
        return {
          id: p.id,
          codigo: p.codigo,
          nombre: p.nombre,
          categoria: out.categoria,
          precio_anterior: ant,
          precio_nuevo: nuevo,
        };
      });
      if (aplicar) {
        registrarAuditoria(db, 'AUMENTO_MASIVO', { porcentaje: pct, actualizados: preview.length });
        guardarDB(db, true, `Aumento masivo de precios (${pct}%)`);
      }
      return respuestaJSON({ ok: true, actualizados: preview.length, productos: preview });
    }

    // --- 4. Clientes, Cuenta Corriente y Fidelización ---
    if (path === '/api/clientes' && method === 'GET') {
      const activos = db.clientes
        .filter((c) => c.activo !== false)
        .map((c) => ({
          id: c.id,
          nombre: c.nombre,
          telefono: c.telefono || '',
          email: c.email || '',
          saldo: redondear2(c.saldo_deudor ?? c.saldo ?? 0),
          puntos: redondear2(c.puntos || 0),
          limite_credito: redondear2(c.limite_credito || 0),
        }));
      return respuestaJSON([
        { id: 0, nombre: 'Consumidor Final', telefono: '', email: '', saldo: 0, puntos: 0, limite_credito: 0 },
        ...activos,
      ]);
    }

    if (path === '/api/bajas/clientes' && method === 'GET') {
      return respuestaJSON(db.clientes.filter((c) => c.activo === false));
    }

    if (path === '/api/clientes' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const nombre = String(body.nombre || '').trim();
      if (!nombre) return respuestaJSON({ detail: 'El nombre del cliente es obligatorio' }, 400);
      const cli = {
        id: db.seq.clientes++,
        comercio_id: 1,
        nombre,
        telefono: String(body.telefono || '').trim(),
        email: String(body.email || '').trim(),
        saldo_deudor: 0,
        puntos: 0,
        limite_credito: Math.max(0, Number(body.limite_credito || 0)),
        activo: true,
        fecha_baja: null,
        created_at: ahoraISO(),
      };
      db.clientes.push(cli);
      registrarAuditoria(db, 'ALTA_CLIENTE', { cliente_id: cli.id, nombre: cli.nombre });
      guardarDB(db, true, `Cliente "${nombre}" registrado`);
      return respuestaJSON({ ...cli, saldo: 0 }, 201);
    }

    const matchCliId = path.match(/^\/api\/clientes\/(\d+)$/);
    if (matchCliId) {
      const cid = Number(matchCliId[1]);
      const cli = db.clientes.find((c) => c.id === cid);
      if (!cli) return respuestaJSON({ detail: 'Cliente no encontrado' }, 404);
      if (method === 'PUT') {
        const body = await leerBodyJSON(init);
        cli.nombre = String(body.nombre || cli.nombre).trim();
        cli.telefono = String(body.telefono ?? cli.telefono ?? '').trim();
        cli.email = String(body.email ?? cli.email ?? '').trim();
        cli.limite_credito = Math.max(0, Number(body.limite_credito ?? cli.limite_credito ?? 0));
        guardarDB(db, true, `Cliente "${cli.nombre}" actualizado`);
        return respuestaJSON({ ...cli, saldo: redondear2(cli.saldo_deudor || 0) });
      }
      if (method === 'DELETE') {
        const ses = obtenerSesion();
        if (!ses || ses.rol !== 'admin') {
          return respuestaJSON({ detail: 'Solo el Dueño puede dar de baja clientes' }, 403);
        }
        if (Number(cli.saldo_deudor || 0) > 0) {
          return respuestaJSON({ detail: 'No se puede dar de baja a un cliente con deuda pendiente.' }, 400);
        }
        cli.activo = false;
        cli.fecha_baja = ahoraISO();
        registrarAuditoria(db, 'BAJA_CLIENTE', { cliente_id: cli.id, nombre: cli.nombre });
        guardarDB(db, true, `Cliente "${cli.nombre}" dado de baja`);
        return respuestaJSON({ ok: true });
      }
    }

    const matchCliReact = path.match(/^\/api\/clientes\/(\d+)\/reactivar$/);
    if (matchCliReact && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede reactivar clientes' }, 403);
      }
      const cid = Number(matchCliReact[1]);
      const cli = db.clientes.find((c) => c.id === cid);
      if (!cli) return respuestaJSON({ detail: 'Cliente no encontrado' }, 404);
      cat = null;
      cli.activo = true;
      cli.fecha_baja = null;
      guardarDB(db, true, `Cliente "${cli.nombre}" reactivado`);
      return respuestaJSON({ ok: true });
    }

    const matchCliPago = path.match(/^\/api\/clientes\/(\d+)\/pago-cta-cte$/);
    if (matchCliPago && method === 'POST') {
      const cid = Number(matchCliPago[1]);
      const cli = db.clientes.find((c) => c.id === cid);
      if (!cli) return respuestaJSON({ detail: 'Cliente no encontrado' }, 404);
      const body = await leerBodyJSON(init);
      const monto = redondear2(body.monto);
      if (monto <= 0) return respuestaJSON({ detail: 'Ingresá un monto mayor a 0' }, 400);
      const saldoActual = redondear2(cli.saldo_deudor || 0);
      if (monto > saldoActual) {
        return respuestaJSON({ detail: `El monto supera la deuda actual del cliente (${fmtMonedaAR(saldoActual, cfg.moneda)})` }, 400);
      }
      const metodo = body.metodo_pago === 'transferencia' ? 'Transferencia' : 'Efectivo';
      cli.saldo_deudor = redondear2(saldoActual - monto);
      db.movimientos_cuenta.push({
        id: db.seq.movimientos_cuenta++,
        comercio_id: 1,
        cliente_id: cli.id,
        tipo: 'pago',
        monto,
        descripcion: `Abono (${metodo})${body.descripcion ? ' - ' + body.descripcion : ''}`,
        ticket_id: null,
        fecha_hora: ahoraISO(),
        archivado_en: null,
      });
      registrarAuditoria(db, 'PAGO_CUENTA', { cliente: cli.nombre, monto, metodo, nuevo_saldo: cli.saldo_deudor });
      guardarDB(db, true, `Cobro Cta. Cte. de ${cli.nombre}`);
      return respuestaJSON({
        ok: true,
        nuevo_saldo: cli.saldo_deudor,
        cliente: {
          id: cli.id,
          nombre: cli.nombre,
          telefono: cli.telefono || '',
          email: cli.email || '',
          saldo: cli.saldo_deudor,
          puntos: redondear2(cli.puntos || 0),
          limite_credito: redondear2(cli.limite_credito || 0),
        },
      });
    }

    const matchCliMovs = path.match(/^\/api\/clientes\/(\d+)\/movimientos$/);
    if (matchCliMovs && method === 'GET') {
      const cid = Number(matchCliMovs[1]);
      const cli = db.clientes.find((c) => c.id === cid);
      if (!cli) return respuestaJSON({ detail: 'Cliente no encontrado' }, 404);
      const movs = db.movimientos_cuenta
        .filter((m) => !m.archivado_en && m.cliente_id === cid)
        .map((m) => ({
          id: m.id,
          fecha: fmtFechaAR(m.fecha_hora),
          tipo: m.tipo,
          monto: redondear2(m.monto),
          descripcion: m.descripcion || '',
          ticket_id: m.ticket_id,
        }))
        .reverse();
      return respuestaJSON({
        cliente: {
          id: cli.id,
          nombre: cli.nombre,
          saldo: redondear2(cli.saldo_deudor || 0),
          puntos: redondear2(cli.puntos || 0),
          limite_credito: redondear2(cli.limite_credito || 0),
        },
        movimientos: movs,
      });
    }

    if (path === '/api/fidelizacion/config') {
      if (method === 'GET') {
        return respuestaJSON({ pesos_por_punto: Number(cfg.pesos_por_punto || 100) });
      }
      if (method === 'PUT') {
        const body = await leerBodyJSON(init);
        db.config.pesos_por_punto = Math.max(0, Number(body.pesos_por_punto || 100));
        guardarDB(db, true, 'Regla de puntos actualizada');
        return respuestaJSON({ ok: true, pesos_por_punto: db.config.pesos_por_punto });
      }
    }

    if (path === '/api/premios' && method === 'GET') {
      const premios = db.productos
        .filter((p) => p.activo !== false && Number(p.puntos_canje || 0) > 0)
        .map((p) => formatearProductoOut(db, p));
      return respuestaJSON(premios);
    }

    if (path === '/api/premios' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const prod = db.productos.find((p) => p.id === Number(body.producto_id));
      if (!prod) return respuestaJSON({ detail: 'Producto no encontrado' }, 404);
      prod.puntos_canje = Math.max(1, Number(body.puntos_canje || 1));
      guardarDB(db, true, `Premio "${prod.nombre}" configurado`);
      return respuestaJSON(formatearProductoOut(db, prod));
    }

    const matchPremioId = path.match(/^\/api\/premios\/(\d+)$/);
    if (matchPremioId) {
      const pid = Number(matchPremioId[1]);
      const prod = db.productos.find((p) => p.id === pid);
      if (!prod) return respuestaJSON({ detail: 'Premio no encontrado' }, 404);
      if (method === 'PUT') {
        const body = await leerBodyJSON(init);
        prod.puntos_canje = Math.max(1, Number(body.puntos_canje || prod.puntos_canje));
        if (body.stock != null) prod.stock_actual = Math.max(0, Number(body.stock));
        guardarDB(db, true, `Premio "${prod.nombre}" actualizado`);
        return respuestaJSON(formatearProductoOut(db, prod));
      }
      if (method === 'DELETE') {
        prod.puntos_canje = 0;
        guardarDB(db, true, `Premio "${prod.nombre}" quitado del catálogo`);
        return respuestaJSON({ ok: true });
      }
    }

    const matchCanjearPremio = path.match(/^\/api\/clientes\/(\d+)\/canjear-premio$/);
    if (matchCanjearPremio && method === 'POST') {
      const cid = Number(matchCanjearPremio[1]);
      const cli = db.clientes.find((c) => c.id === cid);
      const body = await leerBodyJSON(init);
      const prod = db.productos.find((p) => p.id === Number(body.premio_id));
      const cant = Math.max(1, Number(body.cantidad || 1));
      if (!cli || !prod) return respuestaJSON({ detail: 'Cliente o premio no encontrado' }, 404);
      const ptsReq = Number(prod.puntos_canje || 0) * cant;
      if (Number(cli.puntos || 0) < ptsReq) return respuestaJSON({ detail: 'Puntos insuficientes' }, 400);
      if (Number(prod.stock_actual || 0) < cant) return respuestaJSON({ detail: 'Stock insuficiente del premio' }, 400);
      cli.puntos = redondear2(Number(cli.puntos || 0) - ptsReq);
      prod.stock_actual = redondear4(Number(prod.stock_actual || 0) - cant);
      guardarDB(db, true, `Canje de premio: ${prod.nombre}`);
      return respuestaJSON({ ok: true, premio: prod.nombre, cantidad: cant, puntos_restantes: cli.puntos });
    }

    // --- 5. Ventas y Devoluciones ---
    if (path === '/api/ventas' && method === 'POST') {
      if (!obtenerCajaAbierta(db)) {
        return respuestaJSON({ detail: 'Debés registrar el saldo inicial de caja antes de comenzar a vender.' }, 400);
      }
      const body = await leerBodyJSON(init);
      const items = body.items || [];
      if (!items.length) return respuestaJSON({ detail: 'El carrito está vacío' }, 400);

      let cli = null;
      if (Number(body.cliente_id) > 0) {
        cli = db.clientes.find((c) => c.id === Number(body.cliente_id) && c.activo !== false);
        if (!cli) return respuestaJSON({ detail: 'Cliente no disponible' }, 400);
      } else if (body.metodo_pago === 'cuenta_corriente') {
        return respuestaJSON({ detail: 'Seleccioná un cliente registrado para usar Cuenta Corriente' }, 400);
      }

      const descPct = Math.max(0, Math.min(100, Number(body.descuento_pct || 0)));
      const factor = 1 - descPct / 100;

      let subtotal = 0;
      let total = 0;
      let puntosCanjeados = 0;
      const detalleOut = [];

      const acumuladoPorProd = {};
      for (const it of items) {
        const pid = Number(it.producto_id);
        acumuladoPorProd[pid] = redondear4((acumuladoPorProd[pid] || 0) + Number(it.cantidad || 0));
      }
      for (const [pidStr, cantTotal] of Object.entries(acumuladoPorProd)) {
        const prod = db.productos.find((p) => p.id === Number(pidStr) && p.activo !== false);
        if (!prod) return respuestaJSON({ detail: 'Producto no disponible' }, 400);
        if (cantTotal > Number(prod.stock_actual || 0)) {
          return respuestaJSON({ detail: `Stock insuficiente de ${prod.nombre} (disponible: ${prod.stock_actual})` }, 400);
        }
      }

      for (const it of items) {
        const prod = db.productos.find((p) => p.id === Number(it.producto_id) && p.activo !== false);
        const cant = redondear4(it.cantidad);
        const pLista = (!it.es_canje && it.precio_unitario_pactado != null)
          ? Number(it.precio_unitario_pactado)
          : Number(prod.precio_venta || 0);
        let pUnit = 0;
        if (it.es_canje) {
          puntosCanjeados += Number(prod.puntos_canje || 0) * cant;
        } else {
          subtotal += pLista * cant;
          pUnit = redondear2(pLista * factor);
          total += pUnit * cant;
        }
        const etiquetaPromo = it.nombre_promo ? ` [${it.nombre_promo}]` : '';
        detalleOut.push({
          producto: prod,
          producto_id: prod.id,
          nombre: prod.nombre + (it.es_canje ? ' (Canje)' : etiquetaPromo),
          cantidad: cant,
          precio_unitario: pUnit,
          costo_unitario: Number(prod.precio_costo || 0),
          total: redondear2(pUnit * cant),
          es_canje: Boolean(it.es_canje),
        });
      }

      subtotal = redondear2(subtotal);
      total = redondear2(total);

      if (cli && puntosCanjeados > Number(cli.puntos || 0)) {
        return respuestaJSON({ detail: 'El cliente no tiene puntos suficientes para el canje' }, 400);
      }

      if (body.metodo_pago === 'cuenta_corriente' && cli) {
        const lim = Number(cli.limite_credito || 0);
        if (lim <= 0) {
          return respuestaJSON({ detail: `${cli.nombre} no tiene Cuenta Corriente habilitada (Límite $ 0)` }, 400);
        }
        if (Number(cli.saldo_deudor || 0) + total > lim) {
          return respuestaJSON({ detail: `Supera el límite de crédito disponible de ${cli.nombre}` }, 400);
        }
      }

      const mapaFP = {
        efectivo: 'Efectivo',
        transferencia: 'Transferencia',
        cuenta_corriente: 'Cuenta Corriente',
        mixto: `Mixto (Ef: ${fmtMonedaAR(body.monto_efectivo || 0, cfg.moneda)} / Tr: ${fmtMonedaAR(body.monto_transferencia || 0, cfg.moneda)})`,
      };
      const formaPagoStr = mapaFP[body.metodo_pago] || 'Efectivo';

      const ppp = Number(cfg.pesos_por_punto || 100);
      const puntosGanados = cli && ppp > 0 && total > 0 ? redondear2(total / ppp) : 0;
      const puntosNetos = redondear2(puntosGanados - puntosCanjeados);

      const ventaId = db.seq.ventas++;
      const fechaVenta = ahoraISO();

      for (const d of detalleOut) {
        d.producto.stock_actual = redondear4(Number(d.producto.stock_actual || 0) - d.cantidad);
        db.detalle_ventas.push({
          id: db.seq.detalle_ventas++,
          venta_id: ventaId,
          producto_id: d.producto_id,
          cantidad: d.cantidad,
          precio_unitario: d.precio_unitario,
          costo_unitario: d.costo_unitario,
        });
      }

      db.ventas.push({
        id: ventaId,
        comercio_id: 1,
        cliente_id: cli ? cli.id : null,
        fecha_hora: fechaVenta,
        total_venta: total,
        forma_pago: formaPagoStr,
        monto_efectivo: body.metodo_pago === 'mixto' ? Number(body.monto_efectivo || 0) : null,
        monto_transferencia: body.metodo_pago === 'mixto' ? Number(body.monto_transferencia || 0) : null,
        puntos_ganados: puntosNetos,
        archivado_en: null,
      });

      if (cli) {
        cli.puntos = Math.max(0, redondear2(Number(cli.puntos || 0) + puntosNetos));
        if (body.metodo_pago === 'cuenta_corriente') {
          cli.saldo_deudor = redondear2(Number(cli.saldo_deudor || 0) + total);
          db.movimientos_cuenta.push({
            id: db.seq.movimientos_cuenta++,
            comercio_id: 1,
            cliente_id: cli.id,
            tipo: 'cargo',
            monto: total,
            descripcion: `Compra Ticket #${ventaId}`,
            ticket_id: ventaId,
            fecha_hora: fechaVenta,
            archivado_en: null,
          });
        }
      }

      registrarAuditoria(db, 'VENTA', {
        venta_id: ventaId,
        cliente_nombre: cli ? cli.nombre : 'Consumidor Final',
        forma_pago: formaPagoStr,
        total,
      });
      guardarDB(db, true, `Venta Ticket #${ventaId}`);

      const cliOut = cli
        ? { id: cli.id, nombre: cli.nombre, telefono: cli.telefono || '', saldo: redondear2(cli.saldo_deudor || 0), puntos: cli.puntos }
        : { id: 0, nombre: 'Consumidor Final', telefono: '', saldo: 0, puntos: 0 };

      let wspUrl = '';
      if (cliOut.telefono) {
        const txt = `*${cfg.nombre_comercio}*\n• Ticket Nro: #${ventaId}\n• Fecha: ${fmtFechaAR(fechaVenta)}\n• Total: *${fmtMonedaAR(total, cfg.moneda)}* (${formaPagoStr})\nMuchas gracias por tu compra!`;
        wspUrl = armarUrlWhatsApp(cliOut.telefono, txt);
      }

      const pagaCon = Number(body.paga_con || 0);
      const baseEfectivo = body.metodo_pago === 'mixto' ? Number(body.monto_efectivo || 0) : total;
      const vuelto = pagaCon > baseEfectivo ? redondear2(pagaCon - baseEfectivo) : 0;

      return respuestaJSON({
        id: ventaId,
        fecha: fechaVenta,
        fecha_formateada: fmtFechaAR(fechaVenta),
        cliente: cliOut,
        metodo_pago: body.metodo_pago,
        forma_pago: formaPagoStr,
        items: detalleOut.map((d) => ({
          producto_id: d.producto_id,
          nombre: d.nombre,
          cantidad: d.cantidad,
          precio_unitario: d.precio_unitario,
          total: d.total,
        })),
        subtotal,
        descuento_pct: descPct,
        descuento: redondear2(subtotal - total),
        total,
        paga_con: pagaCon || null,
        vuelto,
        puntos_ganados: puntosGanados,
        puntos_canjeados: puntosCanjeados,
        puntos_balance: cliOut.puntos,
        whatsapp_url: wspUrl,
      });
    }

    const matchDevItem = path.match(/^\/api\/ventas\/(\d+)\/devolver-item$/);
    if (matchDevItem && method === 'POST') {
      const vid = Number(matchDevItem[1]);
      const v = db.ventas.find((x) => x.id === vid && !x.archivado_en);
      if (!v) return respuestaJSON({ detail: 'Ticket no encontrado' }, 404);
      const body = await leerBodyJSON(init);
      const det = db.detalle_ventas.find((d) => d.id === Number(body.detalle_id) && d.venta_id === vid);
      if (!det) return respuestaJSON({ detail: 'Renglón no encontrado' }, 404);
      const cantDev = redondear4(body.cantidad);
      if (cantDev <= 0 || cantDev > Number(det.cantidad)) {
        return respuestaJSON({ detail: 'Cantidad a devolver inválida' }, 400);
      }
      const prod = db.productos.find((p) => p.id === det.producto_id);
      if (prod) {
        prod.stock_actual = redondear4(Number(prod.stock_actual || 0) + cantDev);
      }
      const montoDev = redondear2(cantDev * Number(det.precio_unitario || 0));
      det.cantidad = redondear4(Number(det.cantidad) - cantDev);
      v.total_venta = Math.max(0, redondear2(Number(v.total_venta || 0) - montoDev));

      if (v.cliente_id && String(v.forma_pago || '').toLowerCase().includes('cuenta corriente')) {
        const cli = db.clientes.find((c) => c.id === v.cliente_id);
        if (cli) {
          cli.saldo_deudor = Math.max(0, redondear2(Number(cli.saldo_deudor || 0) - montoDev));
        }
      }

      const quedanItems = db.detalle_ventas.some((d) => d.venta_id === vid && Number(d.cantidad) > 0);
      if (!quedanItems) {
        v.archivado_en = ahoraISO();
      }
      registrarAuditoria(db, 'DEVOLUCION_PARCIAL', {
        venta_id: vid,
        producto_nombre: prod ? prod.nombre : 'Artículo',
        cantidad_devuelta: cantDev,
        monto_devuelto: montoDev,
      });
      guardarDB(db, true, `Devolución en Ticket #${vid}`);
      return respuestaJSON({
        ok: true,
        ticket_anulado: !quedanItems,
        producto_nombre: prod ? prod.nombre : 'Artículo',
        cantidad_devuelta: cantDev,
        monto_devuelto: montoDev,
      });
    }

    const matchAnular = path.match(/^\/api\/ventas\/(\d+)\/anular$/);
    if (matchAnular && method === 'POST') {
      const vid = Number(matchAnular[1]);
      const v = db.ventas.find((x) => x.id === vid && !x.archivado_en);
      if (!v) return respuestaJSON({ detail: 'Ticket no encontrado' }, 404);
      const dets = db.detalle_ventas.filter((d) => d.venta_id === vid && Number(d.cantidad) > 0);
      for (const d of dets) {
        const prod = db.productos.find((p) => p.id === d.producto_id);
        if (prod) {
          prod.stock_actual = redondear4(Number(prod.stock_actual || 0) + Number(d.cantidad));
        }
        d.cantidad = 0;
      }
      if (v.cliente_id && String(v.forma_pago || '').toLowerCase().includes('cuenta corriente')) {
        const cli = db.clientes.find((c) => c.id === v.cliente_id);
        if (cli) {
          cli.saldo_deudor = Math.max(0, redondear2(Number(cli.saldo_deudor || 0) - Number(v.total_venta || 0)));
        }
      }
      v.archivado_en = ahoraISO();
      registrarAuditoria(db, 'ANULACION_TICKET', { venta_id: vid, monto_anulado: v.total_venta });
      guardarDB(db, true, `Ticket #${vid} anulado`);
      return respuestaJSON({ ok: true });
    }

    // --- 6. Caja y Control Diario ---
    if (path === '/api/caja/estado' && method === 'GET') {
      const caja = obtenerCajaAbierta(db);
      const historial = db.cierres_caja
        .filter((c) => !c.archivado_en && c.estado === 'cerrada')
        .slice(-5)
        .reverse();
      if (!caja) return respuestaJSON({ abierta: false, historial });
      return respuestaJSON({
        abierta: true,
        apertura: caja.fecha_apertura,
        monto_inicial: Number(caja.monto_inicial || 0),
        historial,
      });
    }

    if (path === '/api/caja/abrir' && method === 'POST') {
      if (obtenerCajaAbierta(db)) return respuestaJSON({ detail: 'La caja ya está abierta' }, 409);
      const body = await leerBodyJSON(init);
      const monto = Math.max(0, redondear2(body.monto_inicial));
      const nueva = {
        id: db.seq.cierres_caja++,
        comercio_id: 1,
        estado: 'abierta',
        monto_inicial: monto,
        fecha_apertura: ahoraISO(),
        fecha_cierre: null,
        total_esperado: null,
        monto_real: null,
        diferencia: null,
        archivado_en: null,
      };
      db.cierres_caja.push(nueva);
      registrarAuditoria(db, 'APERTURA_CAJA', { monto_inicial: monto });
      guardarDB(db, true, 'Apertura de caja');
      return respuestaJSON({ abierta: true, monto_inicial: monto, apertura: nueva.fecha_apertura });
    }

    if (path === '/api/caja/arqueo' && method === 'GET') {
      const fecha = parsedUrl.searchParams.get('fecha');
      return respuestaJSON(calcularArqueoDia(db, fecha));
    }

    if (path === '/api/caja/movimientos' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const ses = obtenerSesion() || { usuario: 'Dueño' };
      const mov = {
        id: db.seq.movimientos_caja++,
        comercio_id: 1,
        tipo: body.tipo === 'ingreso_extra' ? 'ingreso_extra' : 'egreso',
        monto: redondear2(body.monto),
        concepto: String(body.concepto || '').trim(),
        usuario_nombre: ses.usuario || 'Dueño',
        fecha: ahoraISO(),
        archivado_en: null,
      };
      db.movimientos_caja.push(mov);
      registrarAuditoria(db, 'MOVIMIENTO_CAJA', { subtipo: mov.tipo, monto: mov.monto, concepto: mov.concepto });
      guardarDB(db, true, 'Movimiento de caja chica');
      return respuestaJSON({ ok: true, id: mov.id }, 201);
    }

    if (path === '/api/caja/cierre-diario' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const fechaUso = body.fecha || fechaLocalYYYYMMDD();
      const resumen = calcularArqueoDia(db, fechaUso);
      const esperado = Number(resumen.total_efectivo_esperado || 0);
      const real = body.monto_real != null ? redondear2(body.monto_real) : esperado;
      const dif = redondear2(real - esperado);

      const cajaAct = obtenerCajaAbierta(db);
      if (cajaAct) {
        cajaAct.estado = 'cerrada';
        cajaAct.fecha_cierre = ahoraISO();
        cajaAct.total_esperado = esperado;
        cajaAct.monto_real = real;
        cajaAct.diferencia = dif;
      }
      registrarAuditoria(db, 'CIERRE_CAJA', { fecha: fechaUso, esperado, real, diferencia: dif });
      guardarDB(db, true, `Cierre de caja diario (${resumen.fecha_formateada})`);

      // Disparar auto-backup a Google Drive al cerrar la caja si hay Wi-Fi
      ejecutarBackupNube(false, 'Cierre de Caja Diario');

      return respuestaJSON({
        ok: true,
        fecha: fechaUso,
        fecha_formateada: resumen.fecha_formateada,
        pdf_url: `/api/caja/cierre.pdf?fecha=${fechaUso}`,
        whatsapp_url: resumen.whatsapp_url,
        smtp_configurado: false,
        email_enviado: false,
        email_mensaje: '',
        total_esperado: esperado,
        monto_real: real,
        diferencia: dif,
      });
    }

    // --- 7. Configuración, Imágenes, Backup a Google Drive y Puesta a Cero ---
    if (path === '/api/config' && method === 'GET') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede acceder a la configuración' }, 403);
      }
      return respuestaJSON(obtenerConfigComercio(db.config));
    }

    
    if (path === '/api/setup-wizard/estado' && method === 'GET') {
      return respuestaJSON({
        wizard_completado: Number((db.config && db.config.wizard_completado) || 0),
        modo_operacion: (db.config && db.config.modo_operacion) || 'autonomo_offline',
        config: obtenerConfigComercio(db.config),
      });
    }

    if (path === '/api/setup-wizard/completar' && method === 'POST') {
      const body = await leerBodyJSON(init);
      db.config = db.config || {};
      db.config.nombre_comercio = String(body.nombre_comercio || 'Mi Comercio').trim() || 'Mi Comercio';
      db.config.rubro = String(body.rubro || 'comercio_general').trim();
      db.config.modo_operacion = body.modo_operacion || 'autonomo_offline';
      db.config.alias = String(body.alias || '').trim();
      db.config.cbu = String(body.cbu || '').trim();
      db.config.telefono = String(body.telefono || '').trim();
      if (body.logo_imagen) db.config.logo_imagen = body.logo_imagen;
      db.config.wizard_completado = 1;

      db.usuarios = db.usuarios || [];
      if (body.pin_dueno && /^\d{4}$/.test(String(body.pin_dueno).trim())) {
        const usrDueno = db.usuarios.find((u) => u.rol === 'admin' || u.nombre === 'Dueño');
        if (usrDueno) {
          usrDueno.pin = String(body.pin_dueno).trim();
          usrDueno.intentos_fallidos = 0;
          usrDueno.bloqueado_hasta = 0;
        }
      }

      if (Array.isArray(body.cajeros) && body.cajeros.length > 0) {
        db.usuarios.forEach((u) => {
          if (u.rol !== 'admin') u.activo = false;
        });
        let maxId = db.usuarios.reduce((m, u) => Math.max(m, Number(u.id || 0)), 0);
        for (const cIn of body.cajeros) {
          const nomC = String((cIn && cIn.nombre) || '').trim();
          let pinC = String((cIn && cIn.pin) || '0000').trim();
          if (!nomC || nomC.toLowerCase() === 'dueño') continue;
          if (!/^\d{4}$/.test(pinC)) pinC = '0000';
          const ex = db.usuarios.find((u) => String(u.nombre || '').toLowerCase() === nomC.toLowerCase());
          if (ex) {
            ex.nombre = nomC;
            ex.pin = pinC;
            ex.rol = 'cajero';
            ex.activo = true;
            ex.intentos_fallidos = 0;
            ex.bloqueado_hasta = 0;
          } else {
            maxId += 1;
            db.usuarios.push({
              id: maxId,
              comercio_id: 1,
              nombre: nomC,
              pin: pinC,
              rol: 'cajero',
              activo: true,
              created_at: ahoraISO(),
            });
          }
        }
      } else if (body.pin_cajero && /^\d{4}$/.test(String(body.pin_cajero).trim())) {
        const usrCajero = db.usuarios.find((u) => u.nombre === 'Cajero');
        if (usrCajero) {
          usrCajero.pin = String(body.pin_cajero).trim();
          usrCajero.activo = true;
        }
      }

      guardarSesion(null);
      guardarDB(db, true, 'Asistente de configuración inicial completado');
      sincronizarIdentidadComercio(db.config);
      return respuestaJSON({
        ok: true,
        mensaje: 'Asistente completado exitosamente',
        redirect: '/login',
        config: obtenerConfigComercio(db.config),
      });
    }

    if (path === '/api/red/info' && method === 'GET') {
      return respuestaJSON({
        ip: window.location.hostname || '127.0.0.1',
        puerto: window.location.port || (window.location.protocol === 'https:' ? 443 : 80),
        url: window.location.origin,
      });
    }

    if (path === '/api/config' && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede modificar la configuración' }, 403);
      }
      const body = await leerBodyJSON(init);
      const previoLogo = db.config.logo_imagen || '';
      const previoQr = db.config.qr_imagen || '';
      db.config = { ...db.config, ...body };
      db.config.nombre_comercio = String(db.config.nombre_comercio || 'Mi Comercio').trim() || 'Mi Comercio';
      if (!/^#[0-9a-fA-F]{6}$/.test(String(db.config.color_primario || ''))) {
        db.config.color_primario = '#2563EB';
      }
      db.config.logo_imagen = body.logo_imagen !== undefined ? normalizarUrlImagen(body.logo_imagen) : previoLogo;
      db.config.qr_imagen = body.qr_imagen !== undefined ? normalizarUrlImagen(body.qr_imagen) : previoQr;
      if (['nunca', 'inactividad', 'minimizar'].includes(body.modo_bloqueo)) {
        db.config.modo_bloqueo = body.modo_bloqueo;
      }
      if (body.tiempo_inactividad_min !== undefined) {
        db.config.tiempo_inactividad_min = Math.max(1, Math.min(1440, Number(body.tiempo_inactividad_min) || 15));
      }
      guardarDB(db, true, 'Configuración del comercio actualizada');
      sincronizarIdentidadComercio(db.config);
      return respuestaJSON(db.config);
    }

    if (path === '/api/config/logo') {
      if (method === 'POST') {
        const body = await leerBodyJSON(init);
        db.config.logo_imagen = normalizarUrlImagen(body.url || body.logo_imagen || '');
        guardarDB(db, true, 'Logo actualizado');
      } else if (method === 'DELETE') {
        db.config.logo_imagen = '';
        guardarDB(db, true, 'Logo eliminado');
      }
      sincronizarIdentidadComercio(db.config);
      return respuestaJSON(db.config);
    }

    if (path === '/api/config/qr') {
      if (method === 'POST') {
        const body = await leerBodyJSON(init);
        db.config.qr_imagen = normalizarUrlImagen(body.url || body.qr_imagen || '');
        guardarDB(db, true, 'Código QR actualizado');
      } else if (method === 'DELETE') {
        db.config.qr_imagen = '';
        guardarDB(db, true, 'Código QR eliminado');
      }
      sincronizarIdentidadComercio(db.config);
      return respuestaJSON(db.config);
    }

    if (path === '/api/archivos/subir-imagen' && method === 'POST') {
      const fd = init.body;
      const archivo = fd && typeof fd.get === 'function' ? fd.get('archivo') : null;
      if (!archivo) return respuestaJSON({ detail: 'No se seleccionó imagen' }, 400);
      try {
        const dataUrl = await comprimirImagenEnCanvas(archivo, 480);
        return respuestaJSON({ ok: true, url: dataUrl, mensaje: 'Imagen guardada en el celular.' });
      } catch (e) {
        return respuestaJSON({ detail: 'No se pudo procesar la imagen' }, 400);
      }
    }

    if (path === '/api/inventario/importar' && method === 'POST') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede realizar importaciones masivas' }, 403);
      }
      const fd = init.body;
      const archivo = fd && typeof fd.get === 'function' ? fd.get('archivo') : null;
      if (!archivo) return respuestaJSON({ detail: 'Seleccioná una planilla CSV' }, 400);
      try {
        const txt = await archivo.text();
        const lineas = txt.replace(/^\uFEFF/, '').split(/\r?\n/).filter((l) => l.trim());
        if (lineas.length < 2) {
          return respuestaJSON({ detail: 'El archivo no tiene filas para importar. Usá formato CSV.' }, 400);
        }
        const sep = lineas[0].includes(';') ? ';' : ',';
        const cols = lineas[0].split(sep).map((c) => c.trim().replace(/^"|"$/g, '').toLowerCase());
        let creados = 0;
        let actualizados = 0;
        let catsCreadas = 0;
        const errores = [];

        for (let i = 1; i < lineas.length; i++) {
          const vals = lineas[i].split(sep).map((v) => v.trim().replace(/^"|"$/g, ''));
          const fila = {};
          cols.forEach((c, idx) => { fila[c] = vals[idx] ?? ''; });
          const nombre = String(fila.nombre || '').trim();
          if (!nombre) continue;
          const codigo = String(fila.codigo || '').trim() || `SKU-${String(db.seq.productos).padStart(4, '0')}`;
          const catNom = String(fila.categoria || 'General').trim() || 'General';
          let cat = db.categorias.find((c) => c.nombre.toLowerCase() === catNom.toLowerCase());
          if (!cat) {
            cat = { id: db.seq.categorias++, comercio_id: 1, nombre: catNom, descripcion: '', activo: true, fecha_baja: null, created_at: ahoraISO() };
            db.categorias.push(cat);
            catsCreadas++;
          }
          const existente = db.productos.find((p) => p.codigo && p.codigo.toLowerCase() === codigo.toLowerCase());
          if (existente) {
            existente.nombre = nombre;
            existente.categoria_id = cat.id;
            existente.precio_costo = Number(String(fila.precio_costo || '0').replace(',', '.')) || 0;
            existente.precio_venta = Number(String(fila.precio_venta || existente.precio_venta || '0').replace(',', '.')) || 0;
            existente.stock_actual = Number(String(fila.stock ?? existente.stock_actual ?? '0').replace(',', '.')) || 0;
            existente.stock_minimo = Number(String(fila.stock_minimo ?? existente.stock_minimo ?? '0').replace(',', '.')) || 0;
            existente.puntos_canje = Number(fila.puntos_canje || 0) || 0;
            existente.activo = true;
            actualizados++;
          } else {
            db.productos.push({
              id: db.seq.productos++,
              comercio_id: 1,
              codigo,
              nombre,
              categoria_id: cat.id,
              unidad_medida: 'unid',
              precio_costo: Number(String(fila.precio_costo || '0').replace(',', '.')) || 0,
              margen_ganancia: 0,
              usa_precio_manual: true,
              precio_venta: Number(String(fila.precio_venta || '0').replace(',', '.')) || 0,
              stock_actual: Number(String(fila.stock || '0').replace(',', '.')) || 0,
              stock_minimo: Number(String(fila.stock_minimo || '0').replace(',', '.')) || 0,
              puntos_canje: Number(fila.puntos_canje || 0) || 0,
              imagen_url: '',
              activo: true,
              fecha_baja: null,
              created_at: ahoraISO(),
            });
            creados++;
          }
        }
        guardarDB(db, true, `Importación masiva (${creados} creados, ${actualizados} actualizados)`);
        return respuestaJSON({
          total: creados + actualizados,
          creados,
          actualizados,
          categorias_creadas: catsCreadas,
          errores,
        });
      } catch (e) {
        return respuestaJSON({ detail: 'Guardá tu planilla como archivo .CSV (delimitado por comas) para importarla en modo offline.' }, 400);
      }
    }

    if (path === '/api/backup/exportar' && method === 'GET') {
      const snap = armarSnapshotJSON(db);
      return respuestaJSON(snap);
    }

    if (path === '/api/backup-drive/estado' && method === 'GET') {
      const wifi = evaluarConexionCelular(cfg);
      return respuestaJSON({
        estado: db.estado_backup || {},
        wifi,
        carpeta_sugerida: 'Google Drive / Backups_POS_Celular',
      });
    }

    if (path === '/api/backup-drive/ejecutar-ahora' && method === 'POST') {
      const res = await ejecutarBackupNube(true, 'Backup manual desde el celular');
      return respuestaJSON(res);
    }

    if ((path === '/api/backup/restaurar' || path === '/api/backup-drive/restaurar-zip' || path === '/api/setup-wizard/importar-modo-a') && method === 'POST') {
      const fd = init.body;
      const archivo = fd && typeof fd.get === 'function' ? fd.get('archivo') : null;
      if (path !== '/api/setup-wizard/importar-modo-a') {
        const pin = String((fd && typeof fd.get === 'function' ? fd.get('pin') : '') || '').trim();
        const admin = db.usuarios.find((u) => u.rol === 'admin') || db.usuarios[0];
        if (!admin || admin.pin !== pin) {
          return respuestaJSON({ detail: 'PIN incorrecto. No se modificó ningún dato.' }, 403);
        }
      }
      if (!archivo) return respuestaJSON({ detail: 'Seleccioná el archivo de respaldo' }, 400);
      try {
        const txt = await archivo.text();
        const json = JSON.parse(txt);
        const t = json.tablas || json;
        if (json.config) db.config = { ...CONFIG_DEFAULT, ...json.config, wizard_completado: 1 };
        if (Array.isArray(json.usuarios) && json.usuarios.length > 0) {
          db.usuarios = json.usuarios.map((u, idx) => ({
            id: Number(u.id || (idx + 1)),
            comercio_id: 1,
            nombre: u.nombre || `Usuario ${idx + 1}`,
            pin: u.pin || (u.rol === 'admin' ? '1234' : '0000'),
            rol: u.rol === 'admin' ? 'admin' : 'cajero',
            activo: u.activo !== false && u.activo !== 0,
            created_at: u.created_at || ahoraISO(),
          }));
        }
        if (Array.isArray(t.categorias)) db.categorias = t.categorias;
        if (Array.isArray(t.productos)) db.productos = t.productos;
        if (Array.isArray(t.promos)) db.promos = t.promos;
        if (Array.isArray(t.clientes)) db.clientes = t.clientes;
        if (Array.isArray(t.ventas)) db.ventas = t.ventas;
        if (Array.isArray(t.detalle_ventas)) db.detalle_ventas = t.detalle_ventas;
        if (Array.isArray(t.movimientos_caja)) db.movimientos_caja = t.movimientos_caja;
        if (Array.isArray(t.movimientos_cuenta)) db.movimientos_cuenta = t.movimientos_cuenta;
        if (Array.isArray(t.cierres_caja)) db.cierres_caja = t.cierres_caja;
        guardarSesion(null);
        guardarDB(db, false);
        sincronizarIdentidadComercio(db.config);
        return respuestaJSON({ ok: true, redirect: '/login', mensaje: 'Datos restaurados con éxito.' });
      } catch (e) {
        return respuestaJSON({ detail: 'El archivo seleccionado no tiene un formato de respaldo válido.' }, 400);
      }
    }

    if (path === '/api/mantenimiento/reset' && method === 'POST') {
      const body = await leerBodyJSON(init);
      const admin = db.usuarios.find((u) => u.rol === 'admin') || db.usuarios[0];
      if (!admin || admin.pin !== String(body.pin || '').trim()) {
        return respuestaJSON({ detail: 'PIN incorrecto. No se modificó ningún dato.' }, 403);
      }
      if (body.palabra !== 'BORRAR') {
        return respuestaJSON({ detail: 'Escribí la palabra BORRAR en mayúsculas' }, 400);
      }
      const ahora = ahoraISO();
      db.ventas.forEach((v) => { if (!v.archivado_en) v.archivado_en = ahora; });
      db.movimientos_caja.forEach((m) => { if (!m.archivado_en) m.archivado_en = ahora; });
      db.movimientos_cuenta.forEach((m) => { if (!m.archivado_en) m.archivado_en = ahora; });
      db.cierres_caja.forEach((c) => { if (!c.archivado_en) c.archivado_en = ahora; });
      db.clientes.forEach((c) => { c.saldo_deudor = 0; c.puntos = 0; });
      if (body.modo === 'total') {
        db.productos.forEach((p) => { p.activo = false; p.fecha_baja = ahora; });
        db.categorias.forEach((c) => { c.activo = false; c.fecha_baja = ahora; });
        db.clientes.forEach((c) => { c.activo = false; c.fecha_baja = ahora; });
      }
      guardarDB(db, true, 'Puesta a cero ejecutada');
      return respuestaJSON({ ok: true, mensaje: 'Puesta a cero completada con éxito.' });
    }

    if (path === '/api/auditoria' && method === 'GET') {
      const ses = obtenerSesion();
      if (!ses || ses.rol !== 'admin') {
        return respuestaJSON({ detail: 'Solo el Dueño puede acceder a la auditoría' }, 403);
      }
      const acc = parsedUrl.searchParams.get('tipo_accion');
      let lista = db.auditoria_movimientos || [];
      if (acc) lista = lista.filter((a) => a.tipo_accion === acc);
      return respuestaJSON(
        lista.map((a) => ({
          ...a,
          fecha_formateada: fmtFechaAR(a.fecha_hora),
        }))
      );
    }

    return _fetchOriginal(input, init);
  };

  // =========================================================================
  // 8. Interceptar Clics en enlaces de Ticket PDF, Cierre PDF y Descarga Backup
  // =========================================================================
  document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if (!a) return;
    const href = a.getAttribute('href') || '';

    const matchTicketPdf = href.match(/^\/api\/ventas\/(\d+)\/ticket\.pdf/);
    if (matchTicketPdf) {
      ev.preventDefault();
      abrirVistaImpresionTicket(Number(matchTicketPdf[1]));
      return;
    }

    if (href.startsWith('/api/caja/cierre.pdf')) {
      ev.preventDefault();
      const u = new URL(href, window.location.origin);
      abrirVistaImpresionCierre(u.searchParams.get('fecha'));
      return;
    }

    if (href === '/api/inventario/plantilla-excel') {
      ev.preventDefault();
      const csv = '\uFEFFcodigo,nombre,categoria,precio_costo,precio_venta,stock,stock_minimo,puntos_canje\nSKU-0001,Yerba Mate 500g,Almacén,1800,2500,20,5,0\n';
      descargarArchivoEnCelular('plantilla_productos.csv', csv, 'text/csv;charset=utf-8');
      return;
    }

    if (href.startsWith('/api/inventario/exportar-excel')) {
      ev.preventDefault();
      const db = cargarDB();
      const cats = obtenerMapaCategorias(db);
      const cab = '\uFEFFcodigo,nombre,categoria,precio_costo,precio_venta,stock,stock_minimo,puntos_canje\n';
      const filas = db.productos
        .filter((p) => p.activo !== false)
        .map((p) => [
          `"${String(p.codigo || '').replace(/"/g, '""')}"`,
          `"${String(p.nombre || '').replace(/"/g, '""')}"`,
          `"${String(cats[p.categoria_id] || 'General').replace(/"/g, '""')}"`,
          Number(p.precio_costo || 0),
          Number(p.precio_venta || 0),
          Number(p.stock_actual || 0),
          Number(p.stock_minimo || 0),
          Number(p.puntos_canje || 0),
        ].join(','))
        .join('\n');
      descargarArchivoEnCelular('catalogo_productos.csv', cab + filas, 'text/csv;charset=utf-8');
      return;
    }

    if (href === '/api/backup/exportar' || href === '/api/backup-drive/descargar-zip') {
      ev.preventDefault();
      const db = cargarDB();
      const snap = armarSnapshotJSON(db);
      const sello = new Date().toISOString().slice(0, 10).replace(/-/g, '');
      descargarArchivoEnCelular(`carpeta_datos_backup_${sello}.json`, JSON.stringify(snap, null, 2));
    }
  });

  // =========================================================================
  // 9. Control de Sesión Local y Bloqueo Inmediato por Salida
  // =========================================================================
  const rutaActual = (window.location.pathname.replace(/\.html$/, '').replace(/\/+$/, '')) || '/';

  // Interceptar clicks en enlaces internos para no bloquear cuando el usuario navega entre pantallas de la app
  document.addEventListener('click', (e) => {
    const a = e.target && e.target.closest('a');
    if (a && a.href && a.origin === window.location.origin) {
      if (a.target !== '_blank' && !a.href.includes('/ticket.pdf')) {
        try { sessionStorage.setItem('POS_NAVEGANDO_INTERNO', String(Date.now())); } catch (_) {}
      }
    }
  }, true);

  // Verificar si venimos de una transición interna entre pantallas
  const navInterna = Number(sessionStorage.getItem('POS_NAVEGANDO_INTERNO') || 0);
  const esNavInterna = Boolean(navInterna && (Date.now() - navInterna < 4500));
  if (esNavInterna) {
    setTimeout(() => {
      try { sessionStorage.removeItem('POS_NAVEGANDO_INTERNO'); } catch (_) {}
    }, 1000);
  }

  // Si estaba marcado para bloquear por salida previa y no es navegación interna, cerrar sesión
  const salioPrevio = sessionStorage.getItem('POS_BLOQUEAR_PIN');
  if (salioPrevio === '1' && !esNavInterna) {
    try { sessionStorage.removeItem('POS_BLOQUEAR_PIN'); } catch (_) {}
    guardarSesion(null);
  }

  const sesionActiva = obtenerSesion();
  if (!sesionActiva && rutaActual !== '/login') {
    try {
      sessionStorage.setItem('POS_REDIRECT_DESPUES_LOGIN', window.location.pathname + window.location.search);
    } catch (_) {}
    window.location.replace('/login');
  } else if (sesionActiva && (sesionActiva.rol === 'cajero' || sesionActiva.rol === 'empleado') && (rutaActual === '/configuracion' || rutaActual === '/auditoria')) {
    window.location.replace('/');
  } else if (sesionActiva && sesionActiva.debe_cambiar_pin && rutaActual !== '/configuracion' && rutaActual !== '/login') {
    if (sesionActiva.rol === 'admin') {
      window.location.replace('/configuracion#seguridad');
    }
  }

  // Bloqueo al salir de la app (solo si el usuario configuró 'minimizar')
  document.addEventListener('visibilitychange', () => {
    const db = cargarDB();
    const cfg = db.config || CONFIG_DEFAULT;
    const modo = cfg.modo_bloqueo || 'nunca';
    if (modo !== 'minimizar') return;

    if (document.visibilityState === 'hidden') {
      const navReciente = Number(sessionStorage.getItem('POS_NAVEGANDO_INTERNO') || 0);
      const enTransicionInterna = navReciente && (Date.now() - navReciente < 4500);
      if (!enTransicionInterna) {
        try { sessionStorage.setItem('POS_BLOQUEAR_PIN', '1'); } catch (_) {}
      }
    } else if (document.visibilityState === 'visible') {
      const debeBloquear = sessionStorage.getItem('POS_BLOQUEAR_PIN');
      const navReciente = Number(sessionStorage.getItem('POS_NAVEGANDO_INTERNO') || 0);
      const enTransicionInterna = navReciente && (Date.now() - navReciente < 4500);
      if (debeBloquear === '1' && !enTransicionInterna) {
        try {
          sessionStorage.removeItem('POS_BLOQUEAR_PIN');
          sessionStorage.setItem('POS_REDIRECT_DESPUES_LOGIN', window.location.pathname + window.location.search);
        } catch (_) {}
        guardarSesion(null);
        if (rutaActual !== '/login') {
          window.location.replace('/login');
        }
      }
    }
  });

  // Temporizador de inactividad configurable (solo si el usuario configuró 'inactividad')
  if (rutaActual !== '/login') {
    let timerInactividad = null;
    const resetTimerInactividad = () => {
      if (timerInactividad) clearTimeout(timerInactividad);
      const db = cargarDB();
      const cfg = db.config || CONFIG_DEFAULT;
      const modo = cfg.modo_bloqueo || 'nunca';
      if (modo !== 'inactividad') return;

      const minutos = Math.max(1, Number(cfg.tiempo_inactividad_min) || 15);
      timerInactividad = setTimeout(() => {
        try {
          sessionStorage.setItem('POS_REDIRECT_DESPUES_LOGIN', window.location.pathname + window.location.search);
        } catch (_) {}
        guardarSesion(null);
        window.location.replace('/login');
      }, minutos * 60 * 1000);
    };
    ['mousedown', 'keydown', 'touchstart', 'scroll'].forEach((ev) => {
      window.addEventListener(ev, resetTimerInactividad, { passive: true });
    });
    resetTimerInactividad();
  }

  if ('serviceWorker' in navigator) {
    let recargandoSW = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!recargandoSW) {
        recargandoSW = true;
        window.location.reload();
      }
    });
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').then((reg) => {
        if (navigator.onLine) reg.update().catch(() => {});
      }).catch(() => {});
    });
  }
})();
