"""
Formato de números y fechas para Argentina: punto de miles y coma decimal.
Hora oficial GMT-3 (reloj de 24hs) y moneda local ARS $.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

# UTC-3 fijo: Argentina no tiene horario de verano desde 2009
ZONA_AR = timezone(timedelta(hours=-3))


def ahora_ar() -> datetime:
    """Devuelve la fecha y hora actual en zona horaria GMT-3."""
    return datetime.now(ZONA_AR)


def ahora_ar_iso() -> str:
    """Devuelve timestamp ISO en GMT-3."""
    return ahora_ar().isoformat()


def fmt_fecha(iso: Optional[str]) -> str:
    """'2026-10-19T01:30:00+00:00' -> '18/10/2026' en hora argentina."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso)).astimezone(ZONA_AR).strftime("%d/%m/%Y")
    except Exception:
        return str(iso)[:10]


def fmt_fecha_hora(iso: Optional[str]) -> str:
    """'2026-10-19T01:30:00+00:00' -> '18/10/2026 22:30' en reloj de 24hs GMT-3."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso)).astimezone(ZONA_AR).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(iso)


def _swap(texto: str) -> str:
    """Convierte '1,234.56' (en-US) en '1.234,56' (es-AR)."""
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_moneda(valor: Optional[float]) -> str:
    """$ 2.295,00 · -$ 1.500,00 · None -> $ 0,00"""
    v = round(float(valor or 0), 2) + 0.0
    signo = "-" if v < 0 else ""
    return f"{signo}$ {_swap(f'{abs(v):,.2f}')}"


def _sin_ceros(valor: Optional[float], decimales: int) -> str:
    v = round(float(valor or 0), decimales) + 0.0
    return _swap(f"{v:,.{decimales}f}".rstrip("0").rstrip("."))


def fmt_cantidad(valor: Optional[float]) -> str:
    """1 · 4,35 · 0,25 · 1.500 (hasta 3 decimales, sin ceros sobrantes)"""
    return _sin_ceros(valor, 3)


def fmt_puntos(valor: Optional[float]) -> str:
    """1.250 · 22,95"""
    return _sin_ceros(valor, 2)
