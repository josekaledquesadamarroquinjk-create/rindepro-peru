import streamlit as st
import pandas as pd
import sqlite3
import datetime
import json
import os
from PIL import Image
import plotly.express as px

# Opcional: Integración con Gemini para OCR Inteligente (Scanit Clone)
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# ==========================================
# CONFIGURACIÓN DE PÁGINA Y ESTILOS
# ==========================================
st.set_page_config(
    page_title="RindePro Perú | Gestión de Gastos y Viáticos",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main-header {font-size: 26px; font-weight: 700; color: #1E3A8A; margin-bottom: 5px;}
    .sub-header {font-size: 14px; color: #64748B; margin-bottom: 20px;}
    .metric-card {background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px; padding: 15px;}
    .badge-aprobado {background-color: #DCFCE7; color: #166534; padding: 4px 8px; border-radius: 6px; font-weight: 600; font-size: 12px;}
    .badge-pendiente {background-color: #FEF9C3; color: #854D0E; padding: 4px 8px; border-radius: 6px; font-weight: 600; font-size: 12px;}
    .badge-rechazado {background-color: #FEE2E2; color: #991B1B; padding: 4px 8px; border-radius: 6px; font-weight: 600; font-size: 12px;}
</style>
""", unsafe_allow_html=True)

# ==========================================
# BASE DE DATOS (SQLITE)
# ==========================================
DB_FILE = "rindepro_peru.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Tabla de Informes / Fondos por Rendir (Ej: Viaje Iquitos, Caja Chica)
    c.execute('''
        CREATE TABLE IF NOT EXISTS informes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            colaborador TEXT NOT NULL,
            centro_costo TEXT NOT NULL,
            presupuesto_asignado REAL DEFAULT 0.0,
            fecha_creacion TEXT,
            estado TEXT DEFAULT 'Abierto'
        )
    ''')
    
    # Tabla de Gastos Individuales (Comprobantes)
    c.execute('''
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            informe_id INTEGER,
            colaborador TEXT,
            fecha_emision TEXT,
            tipo_doc TEXT,
            ruc_proveedor TEXT,
            razon_social TEXT,
            serie_numero TEXT,
            categoria TEXT,
            moneda TEXT,
            subtotal REAL,
            igv REAL,
            total REAL,
            tasa_igv INTEGER,
            alerta_sunat TEXT,
            estado_aprobacion TEXT DEFAULT 'Pendiente',
            comentarios TEXT,
            fecha_registro TEXT,
            FOREIGN KEY(informe_id) REFERENCES informes(id)
        )
    ''')
    
    # Insertar un informe demo si está vacío
    c.execute("SELECT COUNT(*) FROM informes")
    if c.fetchone()[0] == 0:
        c.execute('''
            INSERT INTO informes (titulo, colaborador, centro_costo, presupuesto_asignado, fecha_creacion, estado)
            VALUES ('Viáticos Visita Comercial - Región Norte', 'Ejecutivo Comercial 01', 'Comercial B2B', 1800.00, ?, 'En Aprobación')
        ''', (datetime.date.today().strftime("%Y-%m-%d"),))
        conn.commit()
        
    conn.close()

init_db()

def run_query(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        cols = [description[0] for description in c.description]
        data = c.fetchall()
        conn.close()
        return pd.DataFrame(data, columns=cols)
    conn.commit()
    conn.close()

# ==========================================
# REGLAS DE NEGOCIO Y VALIDACIONES SUNAT
# ==========================================
TOPE_MOVILIDAD_DIARIO = 41.0  # Tope referencial SUNAT (4% RMV)
MONTO_DETRACCION = 700.0      # Servicios afectos a detracción > S/ 700

def validar_ruc_peru(ruc: str) -> tuple[bool, str]:
    """Valida estructura matemática y prefijo de RUC peruano (11 dígitos)."""
    ruc = ruc.strip()
    if not ruc.isdigit() or len(ruc) != 11:
        return False, "El RUC debe contener exactamente 11 dígitos numéricos."
    if not ruc.startswith(("10", "15", "17", "20")):
        return False, "Prefijo de RUC inválido (debe iniciar con 10 o 20)."
    return True, "RUC Válido (Estructura SUNAT OK)"

def verificar_duplicado(ruc: str, serie_numero: str) -> bool:
    """Algoritmo anti-fraude: detecta si el comprobante ya fue rendido antes."""
    if not ruc or not serie_numero or serie_numero == "S/N":
        return False
    df = run_query(
        "SELECT id FROM gastos WHERE ruc_proveedor = ? AND UPPER(serie_numero) = ?",
        (ruc.strip(), serie_numero.strip().upper()),
        fetch=True
    )
    return not df.empty

def evaluar_alertas_sunat(tipo_doc, ruc, serie_numero, categoria, total, moneda, fecha_emision, colaborador):
    alertas = []
    
    # 1. Duplicidad
    if verificar_duplicado(ruc, serie_numero):
        alertas.append("🚨 DUPLICADO: Este comprobante ya fue registrado anteriormente.")
        
    # 2. Validación de RUC en Facturas
    if tipo_doc in ["Factura Electrónica", "Recibo por Honorarios"]:
        valido, msg = validar_ruc_peru(ruc)
        if not valido:
            alertas.append(f"⚠️ RUC Inválido: {msg}")
            
    # 3. Alerta de Detracción (> S/ 700 en servicios/otros)
    monto_soles = total * 3.75 if moneda == "USD ($)" else total
    if tipo_doc == "Factura Electrónica" and monto_soles > MONTO_DETRACCION and categoria in ["Alojamiento / Hotel", "Alimentación / Representación", "Otros Servicios"]:
        alertas.append("📌 SUJETO A DETRACCIÓN: Operación supera S/ 700.00.")
        
    # 4. Control de Planilla de Movilidad Diaria
    if tipo_doc == "Planilla de Movilidad":
        df_mov = run_query(
            "SELECT SUM(total) as acumulado FROM gastos WHERE colaborador = ? AND fecha_emision = ? AND tipo_doc = 'Planilla de Movilidad'",
            (colaborador, str(fecha_emision)),
            fetch=True
        )
        acumulado_previo = df_mov['acumulado'].iloc[0] if not df_mov.empty and df_mov['acumulado'].iloc[0] else 0.0
        if (acumulado_previo + monto_soles) > TOPE_MOVILIDAD_DIARIO:
            alertas.append(f"⚠️ TOPE MOVILIDAD EXCEDIDO: Acumulado del día S/ {acumulado_previo + monto_soles:.2f} supera el límite deducible SUNAT (S/ {TOPE_MOVILIDAD_DIARIO:.2f}).")

    return " | ".join(alertas) if alertas else "✅ Cumple Política y Normativa SUNAT"

# ==========================================
# FUNCIÓN OCR IA (SCANIT 2.0 EQUIVALENTE)
# ==========================================
def extraer_datos_comprobante_ia(image_file, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    img = Image.open(image_file)
    
    prompt = """
    Eres un auditor contable experto en comprobantes electrónicos de SUNAT (Perú).
    Extrae los datos de esta imagen y devuelve ÚNICAMENTE un objeto JSON válido con esta estructura exacta:
    {
      "ruc_proveedor": "11 dígitos o vacío",
      "razon_social": "nombre de la empresa o comercio",
      "tipo_doc": "Factura Electrónica" o "Boleta de Venta" o "Ticket / Otros",
      "serie_numero": "Ejemplo F001-0001234",
      "fecha_emision": "YYYY-MM-DD",
      "moneda": "PEN (S/)" o "USD ($)",
      "total": numero_float,
      "tasa_igv": 18 o 10 o 0,
      "categoria": "Alimentación / Representación" o "Alojamiento / Hotel" o "Transporte / Taxi / Peaje" o "Combustible" o "Otros Servicios"
    }
    """
    response = model.generate_content([prompt, img])
    texto = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(texto)

# ==========================================
# BARRA LATERAL (CONFIGURACIÓN Y ROLES)
# ==========================================
with st.sidebar:
    st.markdown("## 🧾 RindePro Perú")
    st.caption("Clon Corporativo de Rindegastos + SUNAT")
    st.divider()
    
    rol_activo = st.selectbox(
        "👤 Seleccionar Vista / Rol:",
        ["1. Rendidor (Colaborador)", "2. Aprobador / Jefatura", "3. Finanzas / Contabilidad (BI)"]
    )
    
    usuario_actual = st.text_input("Nombre del Colaborador:", value="Ejecutivo Comercial 01")
    
    st.divider()
    st.markdown("### 🤖 Motor Scanit IA (OCR)")
    gemini_key = st.text_input("Gemini API Key (Opcional para auto-lectura de fotos):", type="password")
    
    st.divider()
    st.info("💡 **Reglas Activas SUNAT:**\n- Validación RUC 11 dígitos\n- Alerta Detracción (> S/ 700)\n- Tope Planilla Movilidad (S/ 41/día)\n- Detector de Facturas Duplicadas")

# ==========================================
# VISTA 1: RENDIDOR (INGRESO DE GASTOS E INFORMES)
# ==========================================
if rol_activo.startswith("1."):
    st.markdown('<div class="main-header">Portal del Rendidor: Mis Gastos y Viáticos</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Digitaliza tus comprobantes, asigna a tu bolsa de viaje y verifica validez SUNAT en tiempo real.</div>', unsafe_allow_html=True)

    tab_nuevo_gasto, tab_informes, tab_movilidad = st.tabs([
        "📸 Subir Comprobante (Scanit IA)", 
        "📁 Mis Informes / Fondos por Rendir",
        "🚕 Planilla de Movilidad"
    ])

    # --- TAB 1: SUBIR COMPROBANTE ---
    with tab_nuevo_gasto:
        df_informes = run_query("SELECT id, titulo, centro_costo, presupuesto_asignado FROM informes WHERE estado != 'Cerrado'", fetch=True)
        
        col_scan, col_form = st.columns([1, 1.4])
        
        # Variables de sesión para autocompletado OCR
        if "ocr_data" not in st.session_state:
            st.session_state.ocr_data = {}

        with col_scan:
            st.subheader("1. Captura de Comprobante")
            uploaded_file = st.file_uploader("Sube foto o imagen de tu Factura / Boleta (JPG, PNG)", type=["jpg", "jpeg", "png"])
            
            if uploaded_file:
                st.image(uploaded_file, caption="Comprobante cargado", use_container_width=True)
                if st.button("✨ Extraer Datos con Scanit IA", type="primary", use_container_width=True):
                    if gemini_key and GEMINI_AVAILABLE:
                        with st.spinner("Analizando comprobante electrónico SUNAT..."):
                            try:
                                st.session_state.ocr_data = extraer_datos_comprobante_ia(uploaded_file, gemini_key)
                                st.success("¡Datos extraídos con éxito! Verifica el formulario a la derecha.")
                            except Exception as e:
                                st.error(f"Error al leer imagen: {e}")
                    else:
                        # Simulación realista si no ingresó API Key
                        st.session_state.ocr_data = {
                            "ruc_proveedor": "20512345678",
                            "razon_social": "RESTAURANTE CORPORATIVO S.A.C.",
                            "tipo_doc": "Factura Electrónica",
                            "serie_numero": "F001-0045892",
                            "fecha_emision": str(datetime.date.today()),
                            "moneda": "PEN (S/)",
                            "total": 145.50,
                            "tasa_igv": 10,
                            "categoria": "Alimentación / Representación"
                        }
                        st.info("Modo Demo IA activado (Ingresa tu Gemini API Key en el menú lateral para lectura real de tus fotos).")

        with col_form:
            st.subheader("2. Datos Tributarios y Contables")
            ocr = st.session_state.ocr_data
            
            with st.form("form_registro_gasto"):
                opciones_informe = {f"#{r['id']} - {r['titulo']} ({r['centro_costo']})": r['id'] for _, r in df_informes.iterrows()}
                informe_sel = st.selectbox("Asignar a Informe / Fondo:", options=list(opciones_informe.keys()))
                
                c1, c2 = st.columns(2)
                with c1:
                    tipo_doc = st.selectbox("Tipo de Comprobante:", ["Factura Electrónica", "Boleta de Venta", "Recibo por Honorarios", "Ticket / Otros"])
                    ruc_prov = st.text_input("RUC Proveedor (11 dígitos):", value=ocr.get("ruc_proveedor", ""))
                    serie_num = st.text_input("Serie y Correlativo (Ej. F001-00123):", value=ocr.get("serie_numero", ""))
                with c2:
                    fecha_emi = st.date_input("Fecha de Emisión:", value=datetime.date.today())
                    razon_soc = st.text_input("Razón Social / Comercio:", value=ocr.get("razon_social", ""))
                    categoria = st.selectbox("Categoría de Gasto:", [
                        "Alimentación / Representación",
                        "Alojamiento / Hotel",
                        "Transporte / Taxi / Peaje",
                        "Pasajes Aéreos / Terrestres",
                        "Combustible / Mantenimiento",
                        "Otros Servicios"
                    ])
                
                c3, c4, c5 = st.columns(3)
                with c3:
                    moneda = st.selectbox("Moneda:", ["PEN (S/)", "USD ($)"])
                with c4:
                    tasa_igv = st.selectbox("Tasa IGV (Perú):", [18, 10, 0], help="18% General | 10% Ley MYPES Restaurantes/Hoteles | 0% Exonerado/Inafecto")
                with c5:
                    total_gasto = st.number_input("Monto Total:", min_value=0.0, value=float(ocr.get("total", 0.0)), step=1.0)
                
                comentarios = st.text_input("Glosa / Motivo del gasto (Ej. Almuerzo con cliente Clínica X):")
                
                submitted = st.form_submit_button("💾 Validar con SUNAT y Guardar Gasto", use_container_width=True)
                
                if submitted:
                    if total_gasto <= 0:
                        st.error("El monto total debe ser mayor a 0.")
                    else:
                        subtotal = round(total_gasto / (1 + (tasa_igv / 100)), 2) if tasa_igv > 0 else total_gasto
                        igv_calc = round(total_gasto - subtotal, 2)
                        
                        alerta = evaluar_alertas_sunat(
                            tipo_doc, ruc_prov, serie_num, categoria, total_gasto, moneda, fecha_emi, usuario_actual
                        )
                        
                        run_query('''
                            INSERT INTO gastos (
                                informe_id, colaborador, fecha_emision, tipo_doc, ruc_proveedor, razon_social,
                                serie_numero, categoria, moneda, subtotal, igv, total, tasa_igv,
                                alerta_sunat, estado_aprobacion, comentarios, fecha_registro
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pendiente', ?, ?)
                        ''', (
                            opciones_informe[informe_sel], usuario_actual, str(fecha_emi), tipo_doc,
                            ruc_prov.strip(), razon_soc.strip(), serie_num.strip().upper(), categoria,
                            moneda, subtotal, igv_calc, total_gasto, tasa_igv, alerta, comentarios,
                            datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                        ))
                        
                        st.session_state.ocr_data = {}
                        if "🚨" in alerta or "⚠️" in alerta:
                            st.warning(f"Gasto registrado con observaciones: {alerta}")
                        else:
                            st.success(f"¡Gasto registrado exitosamente! {alerta}")

    # --- TAB 2: MIS INFORMES / FONDOS POR RENDIR ---
    with tab_informes:
        col_inf1, col_inf2 = st.columns([1, 2])
        with col_inf1:
            st.subheader("Crear Nuevo Informe / Solicitud de Viáticos")
            with st.form("nuevo_informe"):
                titulo_inf = st.text_input("Título del Informe (Ej. Viaje Comercial Cajamarca):")
                cc_inf = st.selectbox("Centro de Costo:", ["Comercial B2B", "Operaciones y Soporte", "Marketing / Eventos", "Gerencia General"])
                presupuesto_inf = st.number_input("Fondo / Viático Asignado (S/):", min_value=0.0, value=1000.0, step=100.0)
                if st.form_submit_button("➕ Crear Informe de Rendición"):
                    if titulo_inf:
                        run_query(
                            "INSERT INTO informes (titulo, colaborador, centro_costo, presupuesto_asignado, fecha_creacion, estado) VALUES (?, ?, ?, ?, ?, 'Abierto')",
                            (titulo_inf, usuario_actual, cc_inf, presupuesto_inf, str(datetime.date.today()))
                        )
                        st.success("Informe creado correctamente.")
                        st.rerun()
        with col_inf2:
            st.subheader("Estado de mis Bolsas de Viaje y Cajas Chicas")
            df_mis_inf = run_query('''
                SELECT i.id, i.titulo, i.centro_costo, i.presupuesto_asignado,
                       COALESCE(SUM(g.total), 0) as total_rendido,
                       (i.presupuesto_asignado - COALESCE(SUM(g.total), 0)) as saldo_por_devolver,
                       COUNT(g.id) as cant_comprobantes
                FROM informes i
                LEFT JOIN gastos g ON i.id = g.informe_id
                GROUP BY i.id
            ''', fetch=True)
            st.dataframe(df_mis_inf, use_container_width=True, hide_index=True)

    # --- TAB 3: PLANILLA DE MOVILIDAD ---
    with tab_movilidad:
        st.subheader("🚕 Registro Rápido de Planilla de Movilidad (Art. 37° Inciso a1 LIR - SUNAT)")
        st.caption(f"Sustenta desplazamientos urbanos (Uber, Cabify, Taxis sin factura, Buses). Tope máximo deducible por día por trabajador: **S/ {TOPE_MOVILIDAD_DIARIO:.2f}**.")
        
        with st.form("form_movilidad"):
            cm1, cm2, cm3, cm4 = st.columns(4)
            with cm1:
                inf_mov = st.selectbox("Informe Asociado:", options=list(opciones_informe.keys()), key="mov_inf")
            with cm2:
                fecha_mov = st.date_input("Fecha del Desplazamiento:", value=datetime.date.today())
            with cm3:
                ruta_mov = st.text_input("Origen -> Destino (Ej. Miraflores -> Clínica San Borja):")
            with cm4:
                monto_mov = st.number_input("Importe (S/):", min_value=1.0, value=18.0, step=1.0)
            
            motivo_mov = st.text_input("Motivo de la comisión (Ej. Demostración de producto / Visita técnica):")
            
            if st.form_submit_button("🚕 Agregar Desplazamiento a Planilla de Movilidad"):
                alerta_mov = evaluar_alertas_sunat(
                    "Planilla de Movilidad", "S/RUC", "MOV-DIA", "Transporte / Taxi / Peaje",
                    monto_mov, "PEN (S/)", fecha_mov, usuario_actual
                )
                run_query('''
                    INSERT INTO gastos (
                        informe_id, colaborador, fecha_emision, tipo_doc, ruc_proveedor, razon_social,
                        serie_numero, categoria, moneda, subtotal, igv, total, tasa_igv,
                        alerta_sunat, estado_aprobacion, comentarios, fecha_registro
                    ) VALUES (?, ?, ?, 'Planilla de Movilidad', 'S/RUC', ?, 'PLANILLA-MOV', 'Transporte / Taxi / Peaje', 'PEN (S/)', ?, 0.0, ?, 0, ?, 'Pendiente', ?, ?)
                ''', (
                    opciones_informe[inf_mov], usuario_actual, str(fecha_mov),
                    f"Ruta: {ruta_mov}", monto_mov, monto_mov, alerta_mov, motivo_mov,
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                ))
                st.success(f"Desplazamiento registrado. {alerta_mov}")

# ==========================================
# VISTA 2: APROBADOR / JEFATURA
# ==========================================
elif rol_activo.startswith("2."):
    st.markdown('<div class="main-header">Bandeja de Aprobación y Auditoría IA</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Revisa gastos en un clic. El motor de reglas resalta duplicados, excesos de movilidad y alertas SUNAT.</div>', unsafe_allow_html=True)
    
    df_pendientes = run_query("SELECT * FROM gastos ORDER BY id DESC", fetch=True)
    
    if df_pendientes.empty:
        st.info("No hay gastos registrados para auditar todavía.")
    else:
        filtro_estado = st.radio("Filtrar por estado:", ["Todos", "Pendiente", "Aprobado", "Rechazado"], horizontal=True)
        if filtro_estado != "Todos":
            df_pendientes = df_pendientes[df_pendientes["estado_aprobacion"] == filtro_estado]
            
        st.dataframe(
            df_pendientes[[
                "id", "fecha_emision", "colaborador", "tipo_doc", "ruc_proveedor",
                "razon_social", "serie_numero", "categoria", "total", "alerta_sunat", "estado_aprobacion"
            ]],
            use_container_width=True,
            hide_index=True
        )
        
        st.divider()
        st.subheader("⚡ Acción Rápida de Aprobación / Rechazo")
        col_ap1, col_ap2, col_ap3 = st.columns([1, 1, 2])
        with col_ap1:
            id_seleccionado = st.number_input("ID del Gasto a gestionar:", min_value=1, step=1)
        with col_ap2:
            nuevo_estado = st.selectbox("Decisión:", ["Aprobado", "Rechazado", "Pendiente"])
        with col_ap3:
            st.write("")
            st.write("")
            if st.button("Aplicar Decisión al Gasto", type="primary"):
                run_query("UPDATE gastos SET estado_aprobacion = ? WHERE id = ?", (nuevo_estado, id_seleccionado))
                st.success(f"Gasto #{id_seleccionado} actualizado a **{nuevo_estado}**.")
                st.rerun()

# ==========================================
# VISTA 3: FINANZAS / CONTABILIDAD (BI & ERP)
# ==========================================
elif rol_activo.startswith("3."):
    st.markdown('<div class="main-header">Dashboard CFO & Contabilidad (SUNAT / ERP)</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Control presupuestario por Centro de Costo, recuperación de crédito fiscal (IGV) y exportación contable.</div>', unsafe_allow_html=True)
    
    df_all = run_query('''
        SELECT g.*, i.titulo as nombre_informe, i.centro_costo
        FROM gastos g
        LEFT JOIN informes i ON g.informe_id = i.id
    ''', fetch=True)
    
    if df_all.empty:
        st.warning("Aún no existen transacciones registradas en la base de datos.")
    else:
        # KPIs Superiores
        total_gastado = df_all["total"].sum()
        igv_recuperable = df_all[df_all["tipo_doc"] == "Factura Electrónica"]["igv"].sum()
        alertas_count = df_all[df_all["alerta_sunat"].str.contains("🚨|⚠️", na=False)].shape[0]
        pendientes_monto = df_all[df_all["estado_aprobacion"] == "Pendiente"]["total"].sum()
        
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("💰 Total Rendido (S/)", f"S/ {total_gastado:,.2f}")
        k2.metric("🏛️ Crédito Fiscal IGV (Facturas)", f"S/ {igv_recuperable:,.2f}")
        k3.metric("⏳ Pendiente de Aprobación", f"S/ {pendientes_monto:,.2f}")
        k4.metric("🚨 Gastos con Alerta SUNAT", f"{alertas_count} docs")
        
        st.divider()
        
        # Gráficos Analíticos
        g1, g2 = st.columns(2)
        with g1:
            fig_cat = px.pie(
                df_all, values="total", names="categoria",
                title="Distribución de Gastos por Categoría",
                hole=0.4, color_discrete_sequence=px.colors.qualitative.Safe
            )
            st.plotly_chart(fig_cat, use_container_width=True)
            
        with g2:
            fig_cc = px.bar(
                df_all.groupby(["centro_costo", "estado_aprobacion"], as_index=False)["total"].sum(),
                x="centro_costo", y="total", color="estado_aprobacion",
                title="Ejecución por Centro de Costo y Estado",
                barmode="group"
            )
            st.plotly_chart(fig_cc, use_container_width=True)
            
        # Exportación Contable (CONCAR / SISCONT / SAP / Excel)
        st.subheader("📥 Exportación de Asientos Contables y Registro de Compras SUNAT")
        st.dataframe(df_all, use_container_width=True, hide_index=True)
        
        csv_data = df_all.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="📊 Descargar Reporte Maestro para Contabilidad (.CSV / Excel)",
            data=csv_data,
            file_name=f"Rendiciones_SUNAT_{datetime.date.today()}.csv",
            mime="text/csv",
            type="primary"
        )

