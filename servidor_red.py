"""
Lanzador del Servidor en Red Local / Cloud para Sistema de Gestión Unificado.
Detecta automáticamente la dirección IP local de la computadora en la red Wi-Fi,
abre el navegador automáticamente y muestra el enlace y código QR para conectar celulares.
"""
import io
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Asegurar que la base de datos local apunte a la carpeta datos/ de esta versión
os.environ.setdefault("POS_DB_FILE", str(BASE_DIR / "datos" / "pos_local.db"))


def obtener_ip_red_local() -> str:
    """Detecta la IP local de la máquina en la red Wi-Fi o cableada."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No envía datos reales, solo determina la interfaz de salida principal
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def imprimir_qr_consola(url: str):
    """Si está disponible la librería qrcode, dibuja el código en consola."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print("\n  Escanea este codigo QR con la camara del celular:")
        qr.print_ascii(invert=True)
    except Exception:
        pass


def generar_qr_svg(url: str) -> str:
    """Genera el código QR en formato SVG limpio para el navegador."""
    try:
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(url, image_factory=factory)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ""


def _abrir_navegador(url: str, puerto: int = 8000):
    """Espera a que el servidor Uvicorn esté escuchando en el puerto y abre el navegador."""
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", puerto), timeout=0.5):
                break
        except OSError:
            time.sleep(0.3)

    url_destino = f"{url}/login"
    try:
        webbrowser.open(url_destino)
    except Exception:
        pass


def main():
    puerto = int(os.getenv("PORT", "8000"))
    ip_local = obtener_ip_red_local()
    url_local = f"http://localhost:{puerto}"
    url_red = f"http://{ip_local}:{puerto}"

    ancho = 72
    print("\n" + "=" * ancho)
    print("  SISTEMA DE GESTION UNIFICADO (NEXT-GEN)")
    print("  PC de Mostrador + Celulares Sincronizados por Wi-Fi / PWA")
    print("=" * ancho)
    print(f"\n  [1] En esta computadora (Mostrador / Caja):")
    print(f"      -> {url_local}")
    print(f"\n  [2] Desde celulares o tablets (conectados al mismo Wi-Fi):")
    print(f"      -> {url_red}")
    print("\n" + "-" * ancho)
    print("  USUARIOS INICIALES (PIN DE FABRICA):")
    print("  - Dueno:    Usuario 'Dueno'   · PIN: 1234")
    print("  - Empleado: Usuario 'Cajero'  · PIN: 0000")
    print("  El sistema solicitara crear tu PIN privado en el primer ingreso.")
    print("-" * ancho)

    imprimir_qr_consola(url_red)

    # Iniciar vigilante de backup automático si está disponible
    try:
        from backup_drive import iniciar_vigilante_backup
        iniciar_vigilante_backup(BASE_DIR)
    except Exception as e:
        print(f"  [Aviso] Backup vigilante: {e}")

    # Abrir navegador automáticamente cuando el puerto esté listo
    threading.Thread(target=_abrir_navegador, args=(url_local, puerto), daemon=True).start()

    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=puerto, reload=False, log_level="info")


if __name__ == "__main__":
    main()
