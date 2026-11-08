"""
Generador de PWA Estática Autónoma para Sistema de Gestión Unificado.
Compila las pantallas Jinja2 a HTML puro en la carpeta 'dist/',
copia el Service Worker (sw.js), manifest.json y el motor offline (motor_offline_pwa.js),
dejando la carpeta lista para GitHub Pages (rama gh-pages), Netlify o Cloudflare Pages.
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.environ["POS_MODO_OFFLINE"] = "1"
os.environ["POS_DB_FILE"] = str(BASE_DIR / "datos" / "pos_local.db")

from fastapi.testclient import TestClient
from main import app

OUT_DIR = BASE_DIR / "dist"


def inyectar_fallbacks_html(html_texto: str) -> str:
    """Inyecta scripts offline y fallbacks anti pantalla blanca."""
    # 1. Fallback para Tailwind
    tw_orig = '<script src="/static/vendor/tailwind.js"></script>'
    tw_nuevo = """<script src="/static/vendor/tailwind.js"></script>
  <script>window.tailwind || document.write('<script src="/tailwind.js"><\\/script>');</script>
  <script>window.tailwind || document.write('<script src="https://cdn.tailwindcss.com"><\\/script>');</script>"""
    if tw_orig in html_texto:
        html_texto = html_texto.replace(tw_orig, tw_nuevo)

    # 2. Fallback para motor_offline_pwa.js
    motor_orig = '<script src="/static/js/motor_offline_pwa.js"></script>'
    motor_nuevo = """<script src="/static/js/motor_offline_pwa.js"></script>
  <script>window.obtenerConfigComercio || document.write('<script src="/motor_offline_pwa.js"><\\/script>');</script>"""
    if motor_orig in html_texto:
        html_texto = html_texto.replace(motor_orig, motor_nuevo)

    # 3. Fallback para Alpine
    alp_orig = '<script defer src="/static/vendor/alpine.min.js"></script>'
    alp_nuevo = """<script defer src="/static/vendor/alpine.min.js"></script>
  <script>window.Alpine || document.write('<script defer src="/alpine.min.js"><\\/script>');</script>
  <script>window.Alpine || document.write('<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js"><\\/script>');</script>"""
    if alp_orig in html_texto:
        html_texto = html_texto.replace(alp_orig, alp_nuevo)

    # 4. Fallback para Lucide
    luc_orig = '<script src="/static/vendor/lucide.min.js"></script>'
    luc_nuevo = """<script src="/static/vendor/lucide.min.js"></script>
  <script>window.lucide || document.write('<script src="/lucide.min.js"><\\/script>');</script>
  <script>window.lucide || document.write('<script src="https://unpkg.com/lucide@latest"><\\/script>');</script>"""
    if luc_orig in html_texto:
        html_texto = html_texto.replace(luc_orig, luc_nuevo)

    # 5. Failsafe anti pantalla blanca: retira [x-cloak] si algo demora más de 1.2 segundos
    failsafe = """  <script>
    setTimeout(function() {
      document.querySelectorAll('[x-cloak]').forEach(function(el) { el.removeAttribute('x-cloak'); });
    }, 1200);
  </script>
</head>"""
    html_texto = html_texto.replace("</head>", failsafe)

    return html_texto


def _borrar_directorio_forzado(ruta: Path):
    import stat
    def _onexc(func, path, exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if ruta.exists():
        shutil.rmtree(ruta, onexc=_onexc)


def compilar_dist():
    _borrar_directorio_forzado(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    client = TestClient(app)

    rutas_html = {
        "/": "index.html",
        "/login": "login/index.html",
        "/inventario": "inventario/index.html",
        "/categorias": "categorias/index.html",
        "/clientes": "clientes/index.html",
        "/caja": "caja/index.html",
        "/configuracion": "configuracion/index.html",
        "/auditoria": "auditoria/index.html",
    }

    for ruta, archivo_rel in rutas_html.items():
        resp = client.get(ruta)
        assert resp.status_code == 200, f"Error renderizando {ruta}: {resp.status_code}"
        html_procesado = inyectar_fallbacks_html(resp.text)

        dest = OUT_DIR / archivo_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html_procesado, encoding="utf-8", newline="\n")
        if ruta != "/":
            nombre_plano = ruta.strip("/") + ".html"
            (OUT_DIR / nombre_plano).write_text(html_procesado, encoding="utf-8", newline="\n")

    # Copiar manifest.json
    if (BASE_DIR / "manifest.json").exists():
        shutil.copy2(BASE_DIR / "manifest.json", OUT_DIR / "manifest.json")

    # Service Worker con sello de versión
    sello_version = int(time.time())
    sw_texto = (BASE_DIR / "sw.js").read_text(encoding="utf-8")
    sw_texto = sw_texto.replace("const CACHE_NAME = 'gestion-unificado-v1';", f"const CACHE_NAME = 'gestion-unificado-v1-{sello_version}';")
    (OUT_DIR / "sw.js").write_text(sw_texto, encoding="utf-8", newline="\n")

    # Copiar assets estáticos
    if (BASE_DIR / "static").exists():
        shutil.copytree(BASE_DIR / "static", OUT_DIR / "static", dirs_exist_ok=True)

    # Copiar scripts raíz en dist para fallback de importación
    shutil.copy2(BASE_DIR / "static" / "js" / "motor_offline_pwa.js", OUT_DIR / "motor_offline_pwa.js")
    shutil.copy2(BASE_DIR / "static" / "vendor" / "tailwind.js", OUT_DIR / "tailwind.js")
    shutil.copy2(BASE_DIR / "static" / "vendor" / "alpine.min.js", OUT_DIR / "alpine.min.js")
    shutil.copy2(BASE_DIR / "static" / "vendor" / "lucide.min.js", OUT_DIR / "lucide.min.js")
    if (BASE_DIR / "static" / "icon-192.svg").exists():
        shutil.copy2(BASE_DIR / "static" / "icon-192.svg", OUT_DIR / "icon-192.svg")
    if (BASE_DIR / "static" / "icon-512.svg").exists():
        shutil.copy2(BASE_DIR / "static" / "icon-512.svg", OUT_DIR / "icon-512.svg")

    # Archivo .nojekyll para GitHub Pages
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    # Archivo _redirects y _headers para Netlify / Cloudflare
    redirects = """/login /login/index.html 200
/login/ /login/index.html 200
/inventario /inventario/index.html 200
/inventario/ /inventario/index.html 200
/categorias /categorias/index.html 200
/categorias/ /categorias/index.html 200
/clientes /clientes/index.html 200
/clientes/ /clientes/index.html 200
/caja /caja/index.html 200
/caja/ /caja/index.html 200
/configuracion /configuracion/index.html 200
/configuracion/ /configuracion/index.html 200
/auditoria /auditoria/index.html 200
/auditoria/ /auditoria/index.html 200
"""
    (OUT_DIR / "_redirects").write_text(redirects, encoding="utf-8", newline="\n")

    headers = """/sw.js
  Cache-Control: no-cache, no-store, must-revalidate
/static/js/motor_offline_pwa.js
  Cache-Control: no-cache, must-revalidate
/motor_offline_pwa.js
  Cache-Control: no-cache, must-revalidate
"""
    (OUT_DIR / "_headers").write_text(headers, encoding="utf-8", newline="\n")

    # Generar dist.zip
    zip_dest = BASE_DIR / "dist.zip"
    with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in OUT_DIR.rglob("*"):
            if file.is_file():
                rel_path = file.relative_to(OUT_DIR).as_posix()
                zf.write(file, arcname=rel_path)

    print(f"\n[OK] Carpeta 'dist/' compilada exitosamente en: {OUT_DIR}")
    print(f"[OK] Archivo ZIP generado en: {zip_dest}")


def desplegar_gh_pages():
    """Despliega exclusivamente el contenido de dist/ a la rama gh-pages sin ensuciar main."""
    compilar_dist()
    print("\nDesplegando 'dist/' en rama gh-pages...")
    git_dir = OUT_DIR / ".git"
    try:
        rem = subprocess.check_output("git remote get-url origin", shell=True, cwd=BASE_DIR, text=True).strip()
        if git_dir.exists():
            shutil.rmtree(git_dir)
        subprocess.run("git init", shell=True, check=True, cwd=OUT_DIR)
        subprocess.run("git checkout -b gh-pages", shell=True, check=True, cwd=OUT_DIR)
        subprocess.run("git add -A", shell=True, check=True, cwd=OUT_DIR)
        subprocess.run('git commit -m "deploy: PWA estatica compilada desde dist/"', shell=True, check=True, cwd=OUT_DIR)
        subprocess.run(f'git push -f "{rem}" gh-pages:gh-pages', shell=True, check=True, cwd=OUT_DIR)
        print("[OK] Despliegue en rama gh-pages completado.")
    except Exception as e:
        print(f"[Aviso] No se pudo hacer push automático a gh-pages: {e}")
    finally:
        _borrar_directorio_forzado(git_dir)


if __name__ == "__main__":
    if "--deploy" in sys.argv:
        desplegar_gh_pages()
    else:
        compilar_dist()
