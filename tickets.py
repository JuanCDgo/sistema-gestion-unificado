"""
Comprobantes de venta y reportes de cierre diario:
- PDF térmico de 80 mm para tickets de venta
- PDF formal en tamaño Carta (Letter) para el Cierre Diario de Caja y Arqueo
- Armado de mensajes y enlaces de WhatsApp (wa.me)
- Envío de correo electrónico por SMTP con el PDF del cierre adjunto
"""
import io
import re
import smtplib
import urllib.parse
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from formato import fmt_cantidad, fmt_moneda, fmt_puntos


def _esc(s: Any) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generar_pdf_ticket_termico(datos_ticket: Dict[str, Any], nombre_negocio: str = "Mi Comercio") -> bytes:
    """Genera en memoria un comprobante en formato térmico estándar de 80 mm (bytes del PDF)."""
    items = datos_ticket.get("items", [])

    # Altura dinámica en mm según la cantidad de renglones
    alto_mm = max(115.0, 75.0 + (len(items) * 8.5))
    ancho_pt = 80 * mm
    alto_pt = alto_mm * mm
    margen_pt = 11.3  # aprox 4 mm

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=(ancho_pt, alto_pt), leftMargin=margen_pt,
                            rightMargin=margen_pt, topMargin=margen_pt, bottomMargin=margen_pt)
    styles = getSampleStyleSheet()

    negocio_style = ParagraphStyle("TicketNegocio", parent=styles["Normal"], fontName="Helvetica-Bold",
                                   fontSize=12, leading=14, alignment=1, textColor=colors.black)
    centrado_chico = ParagraphStyle("TicketCentradoChico", parent=styles["Normal"], fontName="Helvetica",
                                    fontSize=7.5, leading=9.5, alignment=1, textColor=colors.HexColor("#222222"))
    normal_ticket = ParagraphStyle("TicketNormal", parent=styles["Normal"], fontName="Helvetica",
                                   fontSize=7.5, leading=9.5, textColor=colors.black)
    bold_ticket = ParagraphStyle("TicketBold", parent=styles["Normal"], fontName="Helvetica-Bold",
                                 fontSize=7.5, leading=9.5, textColor=colors.black)
    total_style = ParagraphStyle("TicketTotal", parent=styles["Normal"], fontName="Helvetica-Bold",
                                 fontSize=11, leading=13, alignment=2, textColor=colors.black)

    def linea():
        return HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#333333"), spaceBefore=3, spaceAfter=4)

    elements = []

    # 1. ENCABEZADO DEL COMERCIO
    elements.append(Paragraph(_esc(nombre_negocio.upper()), negocio_style))
    sub_lineas = []
    if datos_ticket.get("direccion_negocio"):
        sub_lineas.append(_esc(datos_ticket["direccion_negocio"]))
    if datos_ticket.get("telefono_negocio"):
        sub_lineas.append(f"Tel: {_esc(datos_ticket['telefono_negocio'])}")
    sub_lineas.append("TICKET DE VENTA")
    elements.append(Paragraph("<br/>".join(sub_lineas), centrado_chico))
    elements.append(linea())

    # 2. DATOS DE LA VENTA
    venta_id = datos_ticket.get("venta_id", "-")
    fecha_hora = datos_ticket.get("fecha_hora", datetime.now().strftime("%d/%m/%Y %H:%M"))
    cliente_nombre = _esc(datos_ticket.get("cliente_nombre", "Consumidor Final"))
    forma_pago = _esc(datos_ticket.get("forma_pago", "Efectivo"))
    info_venta = [
        [Paragraph(f"<b>Ticket:</b> #{venta_id}", normal_ticket), Paragraph(f"<b>Fecha:</b> {_esc(fecha_hora)}", normal_ticket)],
        [Paragraph(f"<b>Cliente:</b> {cliente_nombre}", normal_ticket), Paragraph(f"<b>Pago:</b> {forma_pago}", normal_ticket)],
    ]
    t_info = Table(info_venta, colWidths=[95, 109])
    t_info.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    elements.append(t_info)
    elements.append(linea())

    # 3. TABLA DE ARTÍCULOS. Ancho disponible ~204 pt: Cant (26), Detalle (96), P.Unit (40), Subtot (42)
    filas = [[Paragraph("<b>Cant</b>", bold_ticket), Paragraph("<b>Detalle</b>", bold_ticket),
              Paragraph("<b>P.Unit</b>", bold_ticket), Paragraph("<b>Subtot</b>", bold_ticket)]]
    for it in items:
        cant = it.get("cantidad", 1)
        cant_str = fmt_cantidad(cant) if isinstance(cant, (int, float)) else str(cant)
        nombre_it = _esc(str(it.get("nombre", "Artículo"))[:30])
        filas.append([
            Paragraph(cant_str, normal_ticket),
            Paragraph(nombre_it, normal_ticket),
            Paragraph(f"{fmt_moneda(float(it.get('precio_unitario', 0.0)))}", normal_ticket),
            Paragraph(f"{fmt_moneda(float(it.get('subtotal', 0.0)))}", bold_ticket),
        ])
    t_articulos = Table(filas, colWidths=[26, 96, 40, 42])
    t_articulos.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                                     ("ALIGN", (0, 0), (0, -1), "CENTER"), ("ALIGN", (2, 0), (-1, -1), "RIGHT")]))
    elements.append(t_articulos)
    elements.append(linea())

    # 4. TOTAL Y PUNTOS
    total_venta = float(datos_ticket.get("total_venta", 0.0))
    elements.append(Paragraph(f"TOTAL: {fmt_moneda(total_venta)}", total_style))

    puntos_ganados = float(datos_ticket.get("puntos_ganados", 0) or 0)
    puntos_canjeados = float(datos_ticket.get("puntos_canjeados", 0) or 0)
    if puntos_canjeados > 0:
        elements.append(Spacer(1, 2))
        elements.append(Paragraph(f"Puntos canjeados: -{fmt_puntos(puntos_canjeados)}", normal_ticket))
    if puntos_ganados > 0:
        elements.append(Spacer(1, 2))
        elements.append(Paragraph(f"Puntos ganados: +{fmt_puntos(puntos_ganados)}", normal_ticket))
    if puntos_canjeados > 0 or puntos_ganados > 0:
        elements.append(Paragraph(f"Balance total de puntos: {fmt_puntos(float(datos_ticket.get('puntos_balance', 0) or 0))}", normal_ticket))
    elements.append(Spacer(1, 4))

    # 5. PIE DE TICKET
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#888888"), spaceBefore=2, spaceAfter=4))
    elements.append(Paragraph("¡GRACIAS POR SU COMPRA!", centrado_chico))
    elements.append(Paragraph("Comprobante de control interno", centrado_chico))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generar_pdf_cierre_caja(
    fecha_str: str,
    resumen: Dict[str, Any],
    nombre_negocio: str = "Mi Comercio"
) -> bytes:
    """Genera en memoria el documento PDF formal del Cierre Diario de Caja y Arqueo."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle(
        "TituloReporte",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#1E293B"),
        alignment=0,
    )
    subtitulo_style = ParagraphStyle(
        "SubtituloReporte",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#64748B"),
    )
    seccion_style = ParagraphStyle(
        "TituloSeccion",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#0F172A"),
    )
    cell_bold = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#0F172A"),
    )
    cell_normal = ParagraphStyle(
        "CellNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#334155"),
    )

    elements = []

    # 1. ENCABEZADO
    elements.append(Paragraph(_esc(nombre_negocio.upper()), titulo_style))
    elements.append(Paragraph("INFORME OFICIAL DE CIERRE DE CAJA &amp; ARQUEO DIARIO", subtitulo_style))
    elements.append(Paragraph(
        f"Fecha Contable: <b>{_esc(fecha_str)}</b> | Emisión: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        subtitulo_style,
    ))
    elements.append(Spacer(1, 10))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#CBD5E1"), spaceAfter=15))

    # 2. TARJETAS DE INDICADORES (4 KPIs)
    total_vendido = float(resumen.get("total_vendido", 0.0) or 0.0)
    efectivo_esperado = float(resumen.get("total_efectivo_esperado", 0.0) or 0.0)
    ganancia_estimada = float(resumen.get("ganancia_estimada", 0.0) or 0.0)
    total_operaciones = int(resumen.get("total_operaciones", 0) or 0)

    kpi_data = [[
        Paragraph(f"<font size='8' color='#64748B'>TOTAL VENTAS</font><br/><br/><b><font size='13' color='#0F172A'>{fmt_moneda(total_vendido)}</font></b>", styles["Normal"]),
        Paragraph(f"<font size='8' color='#64748B'>EFECTIVO ESPERADO</font><br/><br/><b><font size='13' color='#0F172A'>{fmt_moneda(efectivo_esperado)}</font></b>", styles["Normal"]),
        Paragraph(f"<font size='8' color='#64748B'>GANANCIA ESTIMADA</font><br/><br/><b><font size='13' color='#16A34A'>{fmt_moneda(ganancia_estimada)}</font></b>", styles["Normal"]),
        Paragraph(f"<font size='8' color='#64748B'>TICKETS</font><br/><br/><b><font size='13' color='#2563EB'>{total_operaciones}</font></b>", styles["Normal"]),
    ]]
    t_kpi = Table(kpi_data, colWidths=[135, 135, 135, 135])
    t_kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (0, 0), 1, colors.HexColor("#E2E8F0")),
        ("BOX", (1, 0), (1, 0), 1, colors.HexColor("#E2E8F0")),
        ("BOX", (2, 0), (2, 0), 1, colors.HexColor("#E2E8F0")),
        ("BOX", (3, 0), (3, 0), 1, colors.HexColor("#E2E8F0")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(t_kpi)
    elements.append(Spacer(1, 18))

    # 3. DESGLOSE DE EFECTIVO ESPERADO EN CAJA
    elements.append(Paragraph("Desglose de Efectivo Esperado en Caja", seccion_style))
    elements.append(Spacer(1, 6))
    saldo_ini = float(resumen.get("saldo_inicial", 0.0) or 0.0)
    ventas_ef = float(resumen.get("ventas_efectivo", 0.0) or 0.0)
    cobros_cc = float(resumen.get("cobros_cta_cte_efectivo", 0.0) or 0.0)
    ingresos_ex = float(resumen.get("ingresos_extras", 0.0) or 0.0)
    egresos_val = float(resumen.get("egresos", 0.0) or 0.0)

    tabla_ef_data = [
        [Paragraph("+ Saldo Inicial de Caja (Apertura)", cell_normal), Paragraph(f"{fmt_moneda(saldo_ini)}", cell_normal)],
        [Paragraph("+ Ventas en Efectivo", cell_normal), Paragraph(f"{fmt_moneda(ventas_ef)}", cell_normal)],
        [Paragraph("+ Cobros de Cuenta Corriente (Efectivo)", cell_normal), Paragraph(f"{fmt_moneda(cobros_cc)}", cell_normal)],
        [Paragraph("+ Ingresos Extras (Manual)", cell_normal), Paragraph(f"{fmt_moneda(ingresos_ex)}", cell_normal)],
        [Paragraph("- Egresos (Gastos / Retiros)", cell_normal), Paragraph(f"{fmt_moneda(-egresos_val)}", cell_normal)],
        [Paragraph("<b>= EFECTIVO ESPERADO EN CAJA</b>", cell_bold), Paragraph(f"<b>{fmt_moneda(efectivo_esperado)}</b>", cell_bold)],
    ]
    if resumen.get("monto_real") is not None:
        m_real = float(resumen["monto_real"])
        dif = float(resumen.get("diferencia", round(m_real - efectivo_esperado, 2)))
        lbl_dif = "Exacto ($ 0,00)" if dif == 0 else (f"Sobrante (+{fmt_moneda(dif)})" if dif > 0 else f"Faltante ({fmt_moneda(dif)})")
        color_dif = "#16A34A" if dif == 0 else ("#2563EB" if dif > 0 else "#DC2626")
        style_dif = ParagraphStyle("DifStyle", parent=cell_bold, textColor=colors.HexColor(color_dif))
        tabla_ef_data.append([Paragraph("<b>Efectivo Real Contado en Cajón</b>", cell_normal), Paragraph(f"<b>{fmt_moneda(m_real)}</b>", cell_normal)])
        tabla_ef_data.append([Paragraph("<b>Diferencia de Caja</b>", cell_bold), Paragraph(f"<b>{lbl_dif}</b>", style_dif)])
    t_ef = Table(tabla_ef_data, colWidths=[380, 160])
    t_ef.setStyle(TableStyle([
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    elements.append(t_ef)
    elements.append(Spacer(1, 18))

    # 4. DESGLOSE POR MEDIO DE PAGO
    elements.append(Paragraph("Desglose por Medio de Pago", seccion_style))
    elements.append(Spacer(1, 6))

    desglose = resumen.get("desglose_por_forma_pago", [])
    if desglose:
        tabla_pago_data = [[
            Paragraph("<b>Medio de Pago</b>", cell_bold),
            Paragraph("<b>Operaciones</b>", cell_bold),
            Paragraph("<b>Subtotal Recaudado</b>", cell_bold),
            Paragraph("<b>% Participación</b>", cell_bold),
        ]]
        for item in desglose:
            sub = float(item.get("total", 0.0) or 0.0)
            ops = int(item.get("operaciones", 0) or 0)
            pct = (sub / total_vendido * 100.0) if total_vendido > 0 else 0.0
            tabla_pago_data.append([
                Paragraph(_esc(item.get("forma_pago", "")), cell_normal),
                Paragraph(str(ops), cell_normal),
                Paragraph(f"{fmt_moneda(sub)}", cell_bold),
                Paragraph(f"{pct:.1f}%", cell_normal),
            ])
        t_pago = Table(tabla_pago_data, colWidths=[180, 100, 140, 120])
        t_pago.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        elements.append(t_pago)
    else:
        elements.append(Paragraph("<i>No se registraron ventas en esta jornada.</i>", cell_normal))

    elements.append(Spacer(1, 18))

    # 5. LISTA DE TICKETS DEL DÍA
    elements.append(Paragraph("Detalle Cronológico de Ventas", seccion_style))
    elements.append(Spacer(1, 6))

    ventas = resumen.get("ventas", [])
    if ventas:
        tabla_ventas_data = [[
            Paragraph("<b>Ticket #</b>", cell_bold),
            Paragraph("<b>Hora</b>", cell_bold),
            Paragraph("<b>Cliente</b>", cell_bold),
            Paragraph("<b>Medio de Pago</b>", cell_bold),
            Paragraph("<b>Total Ticket</b>", cell_bold),
        ]]
        for v in ventas:
            hora_str = v.get("hora") or v.get("fecha_hora") or ""
            tabla_ventas_data.append([
                Paragraph(f"#{v.get('id', '')}", cell_normal),
                Paragraph(_esc(hora_str), cell_normal),
                Paragraph(_esc(v.get("cliente_nombre") or "Consumidor Final"), cell_normal),
                Paragraph(_esc(v.get("forma_pago", "")), cell_normal),
                Paragraph(f"{fmt_moneda(float(v.get('total_venta', 0.0) or 0.0))}", cell_bold),
            ])
        t_ventas = Table(tabla_ventas_data, colWidths=[70, 80, 150, 120, 120])
        t_ventas.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        elements.append(t_ventas)
    else:
        elements.append(Paragraph("<i>Sin tickets emitidos en la fecha indicada.</i>", cell_normal))

    elements.append(Spacer(1, 24))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#E2E8F0"), spaceAfter=10))
    elements.append(Paragraph(
        "Documento de control interno generado automáticamente por el Sistema de Gestión Comercial.",
        subtitulo_style,
    ))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def armar_mensaje_whatsapp_ticket(datos_ticket: Dict[str, Any]) -> str:
    """Texto del comprobante para WhatsApp, con viñetas (•) y sin emojis que se corrompan en la URL."""
    negocio = datos_ticket.get("negocio_nombre", "Mi Comercio")
    cliente = datos_ticket.get("cliente_nombre", "Consumidor Final")
    lineas = [
        f"Hola {cliente}! Te compartimos el comprobante de tu compra en *{negocio}*:",
        "",
        f"• *Ticket Nro:* #{datos_ticket.get('venta_id', '-')}",
        f"• *Fecha:* {datos_ticket.get('fecha', '-')}",
        f"• *Medio de Pago:* {datos_ticket.get('forma_pago', '-')}",
    ]
    items = datos_ticket.get("items") or []
    if items:
        lineas.append("")
        lineas.append("*Detalle de compra:*")
        for it in items:
            cant = it.get("cantidad", 1)
            cant_str = fmt_cantidad(cant) if isinstance(cant, (int, float)) else str(cant)
            nom = it.get("nombre", "Artículo")
            subt = float(it.get("total") if it.get("total") is not None else (float(it.get("precio_unitario", 0)) * float(cant)))
            lineas.append(f"• {cant_str}x {nom}: {fmt_moneda(subt)}")

    lineas.append("")
    lineas.append(f"• *Total Abonado:* {fmt_moneda(float(datos_ticket.get('total', 0.0)))}")
    puntos_ganados = float(datos_ticket.get("puntos_ganados", 0) or 0)
    puntos_canjeados = float(datos_ticket.get("puntos_canjeados", 0) or 0)
    if puntos_canjeados > 0:
        lineas.append(f"• *Puntos canjeados:* -{fmt_puntos(puntos_canjeados)}")
    if puntos_ganados > 0:
        lineas.append(f"• *Puntos ganados:* +{fmt_puntos(puntos_ganados)}")
    if puntos_canjeados > 0 or puntos_ganados > 0:
        lineas.append(f"• *Balance de puntos:* {fmt_puntos(float(datos_ticket.get('puntos_balance', 0) or 0))}")
    lineas.extend(["", "Muchas gracias por tu compra!"])
    return "\n".join(lineas)


def armar_mensaje_whatsapp_cierre(
    resumen: Dict[str, Any],
    fecha_str: str,
    nombre_negocio: str = "Mi Comercio",
) -> str:
    """Arma el texto del Cierre de Caja para WhatsApp."""
    total_vendido = float(resumen.get("total_vendido", 0.0) or 0.0)
    efectivo_esperado = float(resumen.get("total_efectivo_esperado", 0.0) or 0.0)
    ganancia = float(resumen.get("ganancia_estimada", 0.0) or 0.0)
    total_ops = int(resumen.get("total_operaciones", 0) or 0)

    lineas = [
        f"*CIERRE DE CAJA - {nombre_negocio.upper()}*",
        f"• *Fecha:* {fecha_str}",
        f"• *Total Ventas:* {fmt_moneda(total_vendido)}",
        f"• *Efectivo Esperado en Caja:* {fmt_moneda(efectivo_esperado)}",
        f"• *Ganancia Estimada:* {fmt_moneda(ganancia)}",
        f"• *Total Operaciones:* {total_ops} tickets",
        "",
        "*Desglose de Efectivo Esperado:*",
        f"• + Saldo Inicial de Caja (Apertura): {fmt_moneda(float(resumen.get('saldo_inicial', 0.0) or 0.0))}",
        f"• + Ventas en Efectivo: {fmt_moneda(float(resumen.get('ventas_efectivo', 0.0) or 0.0))}",
        f"• + Cobros de Cuenta Corriente (Efectivo): {fmt_moneda(float(resumen.get('cobros_cta_cte_efectivo', 0.0) or 0.0))}",
        f"• + Ingresos Extras (Manual): {fmt_moneda(float(resumen.get('ingresos_extras', 0.0) or 0.0))}",
        f"• - Egresos (Gastos / Retiros): {fmt_moneda(float(resumen.get('egresos', 0.0) or 0.0))}",
        f"• *= EFECTIVO ESPERADO EN CAJA:* {fmt_moneda(efectivo_esperado)}",
    ]
    if resumen.get("monto_real") is not None:
        m_real_w = float(resumen["monto_real"])
        dif_w = float(resumen.get("diferencia", round(m_real_w - efectivo_esperado, 2)))
        lbl_dif_w = "Caja Exacta ($ 0,00)" if dif_w == 0 else (f"Sobrante (+{fmt_moneda(dif_w)})" if dif_w > 0 else f"Faltante ({fmt_moneda(dif_w)})")
        lineas.append(f"• *Efectivo Real Contado:* {fmt_moneda(m_real_w)}")
        lineas.append(f"• *Diferencia de Caja:* {lbl_dif_w}")
    desglose = resumen.get("desglose_por_forma_pago", [])
    if desglose:
        lineas.extend(["", "*Desglose por Medio de Pago:*"])
        for dp in desglose:
            lineas.append(f"• {dp['forma_pago']}: {fmt_moneda(float(dp['total']))} ({dp['operaciones']} ops)")
    return "\n".join(lineas)


def _limpiar_header_smtp(valor: str) -> str:
    """Elimina retornos de carro y saltos de línea para prevenir SMTP Header Injection."""
    return re.sub(r"[\r\n]+", " ", str(valor or "")).strip()


def enviar_email_cierre(
    destinatario: str,
    pdf_bytes: bytes,
    fecha_str: str,
    resumen: Dict[str, Any],
    config_smtp: Dict[str, Any],
) -> Tuple[bool, str]:
    """Envía por correo electrónico el PDF de cierre de caja adjunto usando smtplib."""
    destinatario_limpio = _limpiar_header_smtp(destinatario or config_smtp.get("email_destinatario", ""))
    if not destinatario_limpio:
        return False, "No se especificó ninguna dirección de correo destinatario."

    servidor_smtp = _limpiar_header_smtp(config_smtp.get("smtp_servidor") or "")
    try:
        puerto_smtp = int(config_smtp.get("smtp_puerto") or 587)
    except (TypeError, ValueError):
        puerto_smtp = 587
    usuario_smtp = _limpiar_header_smtp(config_smtp.get("smtp_usuario") or "")
    password_smtp = (config_smtp.get("smtp_password") or "").strip()
    nombre_negocio = _limpiar_header_smtp(
        config_smtp.get("nombre_comercio") or config_smtp.get("nombre_negocio") or "Mi Comercio"
    )
    fecha_limpia = _limpiar_header_smtp(fecha_str)

    if not servidor_smtp or not usuario_smtp or not password_smtp:
        return False, "La configuración de servidor SMTP, usuario o contraseña está incompleta."

    try:
        total_vendido = float(resumen.get("total_vendido", 0.0) or 0.0)
        efectivo_esperado = float(resumen.get("total_efectivo_esperado", 0.0) or 0.0)
        ganancia_estimada = float(resumen.get("ganancia_estimada", 0.0) or 0.0)
        total_ops = int(resumen.get("total_operaciones", 0) or 0)

        msg = MIMEMultipart()
        msg["From"] = f"{nombre_negocio} <{usuario_smtp}>"
        msg["To"] = destinatario_limpio
        msg["Subject"] = f"Cierre de Caja {fecha_limpia} - {nombre_negocio}"

        cuerpo_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0; max-width: 600px;">
                <h2 style="color: #0f172a; margin-top: 0;">Resumen Diario de Caja - {_esc(nombre_negocio)}</h2>
                <p>Adjunto encontrarás el informe oficial en formato PDF correspondiente al cierre del día <b>{_esc(fecha_limpia)}</b>.</p>
                <hr style="border: 0; border-top: 1px solid #cbd5e1; margin: 15px 0;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 8px 0; color: #64748b;">Total Ventas:</td>
                        <td style="padding: 8px 0; text-align: right; font-weight: bold; font-size: 16px; color: #0f172a;">{fmt_moneda(total_vendido)}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; color: #64748b;">Efectivo Esperado en Caja:</td>
                        <td style="padding: 8px 0; text-align: right; font-weight: bold; font-size: 16px; color: #0f172a;">{fmt_moneda(efectivo_esperado)}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; color: #64748b;">Ganancia Estimada:</td>
                        <td style="padding: 8px 0; text-align: right; font-weight: bold; font-size: 16px; color: #16a34a;">{fmt_moneda(ganancia_estimada)}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; color: #64748b;">Cantidad de Operaciones:</td>
                        <td style="padding: 8px 0; text-align: right; font-weight: bold; color: #2563eb;">{total_ops} tickets</td>
                    </tr>
                </table>
                <hr style="border: 0; border-top: 1px solid #cbd5e1; margin: 15px 0;">
                <p style="font-size: 12px; color: #94a3b8; margin-bottom: 0;">Enviado automáticamente por el Sistema de Gestión Comercial.</p>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(cuerpo_html, "html"))

        adjunto = MIMEApplication(pdf_bytes, _subtype="pdf")
        adjunto.add_header("Content-Disposition", "attachment", filename=f"Cierre_Caja_{fecha_limpia}.pdf")
        msg.attach(adjunto)

        if puerto_smtp == 465:
            with smtplib.SMTP_SSL(servidor_smtp, puerto_smtp, timeout=20) as server:
                server.ehlo()
                server.login(usuario_smtp, password_smtp)
                server.send_message(msg)
        else:
            with smtplib.SMTP(servidor_smtp, puerto_smtp, timeout=20) as server:
                server.ehlo()
                if server.has_extn("STARTTLS"):
                    server.starttls()
                    server.ehlo()
                server.login(usuario_smtp, password_smtp)
                server.send_message(msg)

        return True, f"Correo enviado exitosamente a {destinatario_limpio}."
    except Exception as e:
        return False, f"Error al enviar correo por SMTP: {str(e)}"


def normalizar_tel_ar(telefono: Optional[str]) -> Optional[str]:
    """
    Celular argentino en formato WhatsApp (549 + característica + número, 13 dígitos).
    Acepta '351 5987654', '0351 15 412-3456', '+54 9 351 ...'. Devuelve None si no se puede
    armar un número completo (ej. '15 4123456' sin característica): mejor no linkear que
    mandar a un número equivocado.
    """
    d = re.sub(r"\D", "", str(telefono or ""))
    if d.startswith("54"):
        d = d[2:]
        if len(d) == 11 and d.startswith("9"):
            d = d[1:]
    d = d.lstrip("0")
    if len(d) == 12:
        # Característica de 2 a 4 dígitos seguida del "15" de celular local
        # ponytail: heurística; no distingue una característica que termine en 15
        for largo_area in (2, 3, 4):
            if d[largo_area:largo_area + 2] == "15":
                d = d[:largo_area] + d[largo_area + 2:]
                break
    return f"549{d}" if len(d) == 10 else None


def armar_url_whatsapp(telefono: Optional[str], mensaje: str) -> str:
    """Normaliza el teléfono (Argentina) y arma el link api.whatsapp.com (cero emojis - cero rombos)."""
    tel = normalizar_tel_ar(telefono)
    texto = urllib.parse.quote(mensaje, safe="")
    if tel:
        return f"https://api.whatsapp.com/send?phone={tel}&text={texto}"
    return f"https://api.whatsapp.com/send?text={texto}"


def armar_mensaje_whatsapp_deuda(
    nombre_cliente: str,
    saldo_deudor: float,
    alias: str = "",
    cbu: str = "",
    nombre_negocio: str = "Mi Comercio"
) -> str:
    """Arma el texto para aviso y cobro de cuenta corriente pendiente (cero emojis - formato ARS)."""
    lineas = [
        f"Hola {nombre_cliente}! Te escribimos de *{nombre_negocio}*.",
        "",
        f"Te informamos el estado actual de tu cuenta corriente:",
        f"• *Saldo pendiente:* {fmt_moneda(saldo_deudor)}",
        "",
        "Podes abonar por transferencia directa a los siguientes datos:",
    ]
    if alias:
        lineas.append(f"• *Alias:* {alias}")
    if cbu:
        lineas.append(f"• *CBU:* {cbu}")
    lineas.extend([
        "",
        "Por favor envianos el comprobante una vez realizada la transferencia. Muchas gracias!"
    ])
    return "\n".join(lineas)
