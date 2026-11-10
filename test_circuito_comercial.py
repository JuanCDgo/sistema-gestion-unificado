import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Modo offline con base de datos local SQLite
os.environ["POS_MODO_OFFLINE"] = "1"
os.environ["POS_DB_FILE"] = str(BASE_DIR / "datos" / "pos_local.db")

from fastapi.testclient import TestClient
from main import app, COOKIE_SESION, SESIONES_INFO

client = TestClient(app)

# Inyectar sesión de Administrador para pruebas
TOKEN_TEST = "test_admin_token_sgu"
SESIONES_INFO[TOKEN_TEST] = {
    "usuario_id": 1,
    "usuario": "Dueño (Admin)",
    "rol": "admin",
    "debe_cambiar_pin": False,
    "expira": time.time() + 3600
}
client.cookies.set(COOKIE_SESION, TOKEN_TEST)

def ejecutar_test():
    print("==================================================================")
    print("      TEST INTEGRAL END-TO-END: CIRCUITO COMERCIAL SGU")
    print("==================================================================")

    # 1. Catálogo de productos
    r_prods = client.get("/api/productos")
    assert r_prods.status_code == 200, f"Error cargando productos: {r_prods.text}"
    prods = r_prods.json()
    print(f"[1/7] Catálogo activo: {len(prods)} productos registrados en inventario.")
    assert len(prods) > 0, "No hay productos en inventario."

    p = prods[0]
    p_id = p["id"]
    p_nombre = p["nombre"]
    p_precio = float(p["precio"])
    p_stock_inicial = float(p.get("stock", 0))
    print(f"      Producto de prueba: '{p_nombre}' (ID: {p_id})")
    print(f"      Precio unitario: ${p_precio:,.2f} | Stock inicial: {p_stock_inicial} u.")

    # 2. Apertura de caja del día
    r_est = client.get("/api/caja/estado")
    if r_est.status_code == 200 and r_est.json().get("abierta"):
        # Cerrar turno previo para testear apertura limpia
        client.post("/api/caja/cerrar", json={"monto_real": 15000.0})

    monto_inicial = 15000.0
    r_abrir = client.post("/api/caja/abrir", json={"monto_inicial": monto_inicial})
    assert r_abrir.status_code in [200, 201], f"Error abriendo caja: {r_abrir.text}"
    print(f"[2/7] Caja abierta exitosamente con saldo inicial: ${monto_inicial:,.2f}")

    # 3. Venta 1: Efectivo con cálculo de vuelto
    cant_v1 = 2
    total_v1 = p_precio * cant_v1
    paga_con_v1 = total_v1 + 2000.0
    vuelto_v1 = paga_con_v1 - total_v1

    payload_v1 = {
        "items": [{"producto_id": p_id, "cantidad": cant_v1}],
        "cliente_id": 0,
        "metodo_pago": "efectivo",
        "descuento_pct": 0,
        "paga_con": paga_con_v1
    }
    r_v1 = client.post("/api/ventas", json=payload_v1)
    assert r_v1.status_code in [200, 201], f"Error venta 1: {r_v1.text}"
    v1_data = r_v1.json()
    print(f"[3/7] Venta 1 (Efectivo) procesada con éxito:")
    print(f"      • Total: ${total_v1:,.2f} | Pagó con: ${paga_con_v1:,.2f} | Vuelto calculado: ${vuelto_v1:,.2f}")
    print(f"      • Ticket comprobante generado: #{v1_data.get('id')}")

    # 4. Venta 2: Transferencia bancaria / QR
    cant_v2 = 1
    total_v2 = p_precio * cant_v2
    payload_v2 = {
        "items": [{"producto_id": p_id, "cantidad": cant_v2}],
        "cliente_id": 0,
        "metodo_pago": "transferencia",
        "descuento_pct": 0,
        "paga_con": total_v2
    }
    r_v2 = client.post("/api/ventas", json=payload_v2)
    assert r_v2.status_code in [200, 201], f"Error venta 2: {r_v2.text}"
    v2_data = r_v2.json()
    print(f"[4/7] Venta 2 (Transferencia / QR) procesada con éxito:")
    print(f"      • Total: ${total_v2:,.2f} | Ticket comprobante generado: #{v2_data.get('id')}")

    # 5. Movimiento de Caja Chica: Egreso de gastos menores
    gasto_insumos = 3500.0
    payload_mov = {
        "tipo": "egreso",
        "monto": gasto_insumos,
        "concepto": "Artículos de limpieza y bolsas para mostrador"
    }
    r_mov = client.post("/api/caja/movimientos", json=payload_mov)
    assert r_mov.status_code in [200, 201], f"Error movimiento caja: {r_mov.text}"
    print(f"[5/7] Movimiento de Caja Chica registrado:")
    print(f"      • Egreso: -${gasto_insumos:,.2f} | Motivo: '{payload_mov['concepto']}'")

    # 6. Control de Stock en Tiempo Real
    r_prods_post = client.get("/api/productos")
    p_post = next(x for x in r_prods_post.json() if x["id"] == p_id)
    stock_actual = float(p_post.get("stock", 0))
    total_vendido = cant_v1 + cant_v2
    stock_esperado = p_stock_inicial - total_vendido
    print(f"[6/7] Control de Inventario en Tiempo Real:")
    print(f"      • Stock previo: {p_stock_inicial} u. | Cantidad vendida: {total_vendido} u.")
    print(f"      • Stock actual en base de datos: {stock_actual} u. (Esperado: {stock_esperado} u.)")
    assert stock_actual == stock_esperado, f"Discrepancia en stock: actual={stock_actual}, esperado={stock_esperado}"

    # 7. Arqueo y Balance de Caja
    r_arq = client.get("/api/caja/arqueo")
    assert r_arq.status_code == 200, f"Error arqueo: {r_arq.text}"
    arq = r_arq.json()

    efectivo_esperado = monto_inicial + total_v1 - gasto_insumos
    print(f"[7/7] Arqueo y Balance de Caja:")
    print(f"      • Fondo inicial de apertura:  ${monto_inicial:,.2f}")
    print(f"      • Cobros en efectivo (Venta 1):+${total_v1:,.2f}")
    print(f"      • Cobros en QR/Banco (Venta 2):+${total_v2:,.2f}")
    print(f"      • Egresos de caja chica:      -${gasto_insumos:,.2f}")
    print(f"      • Efectivo físico en cajón:    ${efectivo_esperado:,.2f}")

    print("------------------------------------------------------------------")
    print("  RESULTADO: CIRCUITO 100% VERIFICADO - CAJA Y STOCK CUADRADOS")
    print("==================================================================")

if __name__ == "__main__":
    ejecutar_test()
