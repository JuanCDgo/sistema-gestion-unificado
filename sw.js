/**
 * Service Worker Offline-First para el Sistema de Gestión Unificado (Next-Gen).
 * Compatible con localhost, LAN Wi-Fi y subcarpetas de GitHub Pages.
 */
const CACHE_NAME = 'gestion-unificado-v4';

// Resuelve la ruta base para soportar despliegues en subcarpetas de GitHub Pages
const BASE_PATH = self.location.pathname.replace(/\/sw\.js$/, '') || '';

const RECURSOS_BASE = [
  BASE_PATH + '/',
  BASE_PATH + '/index.html',
  BASE_PATH + '/login.html',
  BASE_PATH + '/inventario.html',
  BASE_PATH + '/categorias.html',
  BASE_PATH + '/clientes.html',
  BASE_PATH + '/caja.html',
  BASE_PATH + '/configuracion.html',
  BASE_PATH + '/auditoria.html',
  BASE_PATH + '/manifest.json',
  BASE_PATH + '/static/icon-192.svg',
  BASE_PATH + '/static/icon-512.svg',
  BASE_PATH + '/static/js/motor_offline_pwa.js',
  BASE_PATH + '/static/vendor/tailwind.js',
  BASE_PATH + '/static/vendor/alpine.min.js',
  BASE_PATH + '/static/vendor/lucide.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      for (const url of RECURSOS_BASE) {
        try {
          const resp = await fetch(url, { cache: 'reload' });
          if (resp && resp.ok) {
            await cache.put(url, resp);
          }
        } catch (_) {}
      }
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((k) => {
          if (k !== CACHE_NAME) return caches.delete(k);
        })
      )
    )
  );
  self.clients.claim();
});

function fetchConTimeout(req, ms = 2500) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    fetch(req, { cache: 'no-cache' })
      .then((res) => {
        clearTimeout(timer);
        resolve(res);
      })
      .catch((err) => {
        clearTimeout(timer);
        reject(err);
      });
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  let url;
  try {
    url = new URL(req.url);
  } catch (_) {
    return;
  }
  if (url.origin !== self.location.origin) return;
  // No interceptar llamadas /api/: de eso se encarga motor_offline_pwa.js o el backend FastAPI
  if (url.pathname.includes('/api/')) return;

  const esVendor = url.pathname.includes('/static/vendor/');

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const enCache = await cache.match(req);

      if (esVendor && enCache) {
        return enCache;
      }

      try {
        const redResp = await fetchConTimeout(req, 2000);
        if (redResp && redResp.ok) {
          if (!redResp.redirected) {
            cache.put(req, redResp.clone());
          }
          return redResp;
        }
      } catch (_) {}

      if (enCache) return enCache;

      // Fallback a index.html si navega dentro de la app
      if (req.mode === 'navigate') {
        const fallback = await cache.match(BASE_PATH + '/index.html') || await cache.match('/');
        if (fallback) return fallback;
      }

      return new Response('Sin conexión', { status: 503, statusText: 'Offline' });
    })()
  );
});
