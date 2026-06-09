"""
Punto Rojo — API de procesamiento de facturas XML DIAN
Servidor FastAPI que recibe ZIP/XML y devuelve los datos procesados.
"""
import os, io, re, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Punto Rojo API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
IVA_DEFAULT  = 19.0

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Helpers XML ────────────────────────────────────────────────────────────

def local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag

def all_descendants(parent, name: str):
    for el in parent.iter():
        if local_name(el.tag) == name:
            yield el

def first_text(parent, path_names: list) -> str:
    if not path_names:
        return parent.text.strip() if parent.text else ""
    for child in parent:
        if local_name(child.tag) == path_names[0]:
            return first_text(child, path_names[1:])
    return ""

def parse_decimal(text, default=0.0) -> float:
    try:
        return float(str(text or "").replace(",", "").strip())
    except Exception:
        return float(default)

def money(value) -> float:
    try:
        d = Decimal(str(float(value or 0)))
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0

def extract_invoice_xml(raw_xml: bytes) -> ET.Element:
    text = raw_xml.decode("utf-8", errors="replace")
    cdata = re.search(r'<!\[CDATA\[(.*?)\]\]>', text, re.DOTALL)
    inner = cdata.group(1) if cdata else text
    try:
        return ET.fromstring(inner.encode("utf-8"))
    except ET.ParseError:
        return ET.fromstring(re.sub(r'<\?xml[^>]*\?>', '', inner).strip().encode("utf-8"))

# ─── Detección de presentación ───────────────────────────────────────────────

def detect_packaging(desc: str):
    """Detecta la estructura de empaque de la descripción del producto.
    Retorna (unidades_por_paquete, unidades_por_caja, paquetes_por_caja)

    Reglas:
    - Si hay "X N (CJxM)" → up=N, pc=M, uc=N*M
    - Si solo hay "(CJxN)" → caja directa up=1, pc=N, uc=N
    - Si hay "PCN" o "PQN" → paquete de N unidades
    - Si hay "CN" al final → caja de N paquetes (COPA FAYCO C10)
    """
    if not desc:
        return 1, 1, 1
    d = desc.upper()

    m = re.search(r'CJ\s*X?\s*(\d+)', d)
    if m:
        cj_num = int(m.group(1))
        pm = re.search(r'(?:PC|PQ|PK)\s*X?\s*(\d+)', d)
        if pm:
            up = int(pm.group(1))
            pc = max(1, cj_num // up)
            return up, cj_num, pc
        pos_cj = m.start()
        texto_antes = d[:pos_cj]
        matches_x = re.findall(r'(?<!\d)X\s*(\d+)', texto_antes)
        if matches_x:
            up = int(matches_x[-1])
            pc = cj_num
            uc = up * pc
            return up, uc, pc
        return 1, cj_num, cj_num

    m = re.search(r'(?:PC|PQ|PK)\s*X?\s*(\d+)', d)
    if m:
        up = int(m.group(1))
        return up, up, 1

    m = re.search(r'\bC(\d+)\b', d)
    if m:
        packs = int(m.group(1))
        pm = re.search(r'(?<!\d)X\s*(\d+)', d)
        up = int(pm.group(1)) if pm else 1
        return up, up * packs, packs

    return 1, 1, 1

def default_sale_flags(pres: str, up: int, packs: int, box: int):
    """Determina qué presentaciones se venden por defecto."""
    if pres == "Caja/Paca":
        vu = up > 1
        vp = packs > 1
        vc = True
    elif pres == "Paquete":
        vu = up > 1
        vp = True
        vc = False
    else:
        vu = True
        vp = False
        vc = False
    return vu, vp, vc

def get_pres_sugerida(up: int, box: int, packs: int) -> str:
    if box > 1 and packs > 1:
        return "Caja/Paca"
    if up > 1:
        return "Paquete"
    return "Unidad"

# ─── Parser principal ────────────────────────────────────────────────────────

def parse_invoice(root: ET.Element) -> dict:
    supplier = (
        first_text(root, ["AccountingSupplierParty","Party","PartyName","Name"]) or
        first_text(root, ["AccountingSupplierParty","Party","PartyLegalEntity","RegistrationName"]) or
        "PROVEEDOR SIN NOMBRE"
    )
    proveedor_nit = (
        first_text(root, ["AccountingSupplierParty","Party","PartyTaxScheme","CompanyID"]) or
        first_text(root, ["AccountingSupplierParty","Party","PartyLegalEntity","CompanyID"])
    )
    number   = first_text(root, ["ID"]) or f"SIN_NUMERO_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    fecha    = first_text(root, ["IssueDate"]) or str(date.today())
    cufe     = ""
    for u in all_descendants(root, "UUID"):
        if u.text and u.text.strip():
            cufe = u.text.strip(); break

    subtotal = parse_decimal(first_text(root, ["LegalMonetaryTotal","LineExtensionAmount"]))
    total    = parse_decimal(first_text(root, ["LegalMonetaryTotal","PayableAmount"]))
    iva      = money(total - subtotal) if subtotal and total and total >= subtotal else 0.0

    lines = []
    for i, line in enumerate(all_descendants(root, "InvoiceLine"), start=1):
        qty      = parse_decimal(first_text(line, ["InvoicedQuantity"]), 1) or 1
        desc     = first_text(line, ["Item","Description"]) or first_text(line, ["Item","Name"]) or ""
        sku      = (
            first_text(line, ["Item","StandardItemIdentification","ID"]) or
            first_text(line, ["Item","SellersItemIdentification","ID"]) or
            f"L{i:03d}"
        )
        price    = parse_decimal(first_text(line, ["Price","PriceAmount"]))
        line_ext = parse_decimal(first_text(line, ["LineExtensionAmount"]))
        line_iva = 0.0
        iva_pct  = IVA_DEFAULT
        for ta in all_descendants(line, "TaxTotal"):
            line_iva += parse_decimal(first_text(ta, ["TaxAmount"]))
            pct = parse_decimal(first_text(ta, ["TaxSubtotal","TaxCategory","Percent"]), 0)
            if pct: iva_pct = pct

        # Descuento en línea
        desc_amt = 0.0
        desc_pct = 0.0
        for ac in all_descendants(line, "AllowanceCharge"):
            charge_ind = first_text(ac, ["ChargeIndicator"])
            if charge_ind.lower() == "false":  # es descuento
                desc_amt = parse_decimal(first_text(ac, ["Amount"]))
                desc_pct = parse_decimal(first_text(ac, ["MultiplierFactorNumeric"]))

        up, box, packs = detect_packaging(desc)
        pres = get_pres_sugerida(up, box, packs)
        vu, vp, vc = default_sale_flags(pres, up, packs, box)

        lines.append({
            "linea": i,
            "sku_proveedor": str(sku or ""),
            "nombre_factura": desc,
            "cantidad_facturada": qty,
            "precio_unitario_factura": price,
            "subtotal_linea": line_ext,
            "iva_linea": line_iva,
            "iva_porcentaje": iva_pct,
            "nombre_punto_rojo": desc,
            "categoria": "",
            "presentacion_facturada": pres,
            "unidades_por_paquete": up,
            "paquetes_por_caja": packs,
            "unidades_por_caja": box,
            "venta_unidad": vu,
            "venta_paquete": vp,
            "venta_caja": vc,
            "markup_unidad_pct": 40.0,
            "markup_paquete_pct": 35.0,
            "markup_caja_pct": 30.0,
            "descuento_factura_pct": desc_pct,
            "descuento_factura_amt": desc_amt,
            "nota_descuento": f"Descuento en factura {desc_pct:.0f}% (${desc_amt:,.0f})" if desc_amt > 0 else "",
        })

    if not subtotal: subtotal = sum(x["subtotal_linea"] for x in lines)
    if not iva:      iva = sum(x["iva_linea"] for x in lines)
    if not total:    total = subtotal + iva

    # Detectar INPUSU
    inpusu = 0.0
    for note_el in all_descendants(root, "Note"):
        note_text = (note_el.text or "").upper()
        if "INPUSU" in note_text or "PLASTICO" in note_text:
            m = re.search(r'\$\s*([\d,\.]+)', note_text)
            if m:
                try:
                    inpusu = float(m.group(1).replace(",", "").replace(".", ""))
                except Exception:
                    pass

    # IVA mode
    total_ajustado = total - inpusu
    tolerancia = max(500, total * 0.02)
    iva_detectado = (
        "NO_INCLUIDO"
        if abs((subtotal + iva) - total_ajustado) < tolerancia
        else "INCLUIDO"
    )

    return {
        "proveedor": supplier,
        "proveedor_nit": proveedor_nit,
        "cufe": cufe,
        "numero_factura": number,
        "fecha": fecha,
        "subtotal_factura": money(subtotal),
        "iva_factura": money(iva),
        "total_factura": money(total),
        "inpusu": money(inpusu),
        "iva_detectado": iva_detectado,
        "lineas": lines,
    }

# ─── Cálculo de costos y precios ─────────────────────────────────────────────

def cost_without_tax(precio: float, iva_mode: str, iva_pct: float = IVA_DEFAULT) -> float:
    if iva_mode == "INCLUIDO":
        return money(precio / (1 + iva_pct / 100))
    return money(precio)

def calc_costs(costo_fact: float, pres: str, up: int, pc: int):
    """
    costo_fact = precio de la unidad facturada.
    En facturas DIAN el precio siempre corresponde al PAQUETE
    (la unidad de compra es el paquete, qty = número de paquetes comprados).
    
    Siempre calculamos:
      cp = costo_fact (costo del paquete)
      cu = cp / up    (costo de la unidad individual)
      cc = cp * pc    (costo de la caja completa)
    
    Para Unidad (up=1, pc=1): precio_fact es el costo unitario directamente.
    """
    up = max(up, 1); pc = max(pc, 1)
    
    if pres == "Unidad" or (up == 1 and pc == 1):
        cu = costo_fact
        cp = costo_fact
        cc = costo_fact
    else:
        # precio_fact = costo del paquete
        cp = costo_fact
        cu = money(cp / up) if up > 1 else cp
        cc = money(cp * pc)
    
    return cu, cp, cc

def sale_price(costo: float, markup: float, transporte: float = 0.0) -> float:
    costo_total = (costo + transporte) * (1 + IVA_DEFAULT / 100)
    m = max(0.0, min(float(markup or 0) / 100.0, 0.95))
    return money(costo_total / (1 - m))

def add_calcs(lines: list, iva_mode: str) -> list:
    result = []
    for l in lines:
        up   = max(int(l.get("unidades_por_paquete") or 1), 1)
        pc   = max(int(l.get("paquetes_por_caja") or 1), 1)
        uc   = max(up * pc, 1)
        pres = l.get("presentacion_facturada", "Unidad")
        mu   = float(l.get("markup_unidad_pct") or 40)
        mp   = float(l.get("markup_paquete_pct") or 35)
        mc   = float(l.get("markup_caja_pct") or 30)

        precio_fact = l.get("precio_unitario_factura") or (l.get("subtotal_linea", 0) / max(l.get("cantidad_facturada", 1), 1))
        
        # Si el proveedor tiene descuento_afecta_costo=True y hay descuento en la línea,
        # aplicar el descuento al precio antes de calcular el costo
        desc_pct = float(l.get("descuento_factura_pct") or 0)
        if l.get("descuento_afecta_costo") and desc_pct > 0:
            precio_fact = money(precio_fact * (1 - desc_pct / 100))

        costo_base  = cost_without_tax(precio_fact, iva_mode, l.get("iva_porcentaje", IVA_DEFAULT))
        transporte  = float(l.get("transporte_adicional", 0) or 0)
        costo_fact_final = costo_base + transporte

        cu, cp, cc = calc_costs(costo_fact_final, pres, up, pc)

        row = {**l,
            "unidades_por_caja": uc,
            "costo_unidad_sin_iva": cu,
            "costo_paquete_sin_iva": cp,
            "costo_caja_sin_iva": cc,
            "precio_unidad_con_iva": sale_price(cu, mu),
            "precio_paquete_con_iva": sale_price(cp, mp),
            "precio_caja_con_iva": sale_price(cc, mc),
        }
        result.append(row)
    return result

# ─── Búsqueda de productos similares en Supabase ─────────────────────────────

def find_similar_product(supabase: Client, proveedor_nit: str, sku: str, nombre: str):
    """Busca si el producto ya existe en la BD para mostrar match.
    Si existe, devuelve todos los campos para pre-llenar el formulario.
    """
    try:
        # Buscar por SKU proveedor exacto
        r = supabase.table("productos").select(
            "id,sku_interno,sku_proveedor,nombre_punto_rojo,categoria,"
            "presentacion_facturada,unidades_por_paquete,paquetes_por_caja,unidades_por_caja,"
            "costo_unidad_sin_iva,markup_unidad_pct,markup_paquete_pct,markup_caja_pct,"
            "venta_unidad,venta_paquete,venta_caja,costo_transporte"
        ).eq("sku_proveedor", sku).eq("activo", True).limit(1).execute()
        if r.data:
            return {"match": "Exacto", "producto": r.data[0]}
    except Exception:
        pass
    return {"match": "Nuevo", "producto": None}

def check_duplicate(supabase: Client, cufe: str, numero_factura: str) -> bool:
    try:
        r = supabase.table("facturas").select("id").eq("cufe", cufe).execute()
        if r.data:
            return True
        r2 = supabase.table("facturas").select("id").eq("numero_factura", numero_factura).execute()
        return bool(r2.data)
    except Exception:
        return False

def get_proveedor_info(supabase: Client, nit: str, nombre: str) -> dict:
    """Busca el proveedor en proveedores_contables para obtener configuración."""
    try:
        r = supabase.table("proveedores_contables").select(
            "proveedor_nombre,forma_pago,descuento_pct,aplica_retefuente,tipo,regimen,descuento_afecta_costo"
        ).eq("nit", nit).limit(1).execute()
        if r.data:
            return r.data[0]
        # Buscar por nombre similar usando la función SQL
        r2 = supabase.rpc("match_proveedor", {"nombre_buscar": nombre}).execute()
        if r2.data:
            return r2.data[0]
    except Exception:
        pass
    return {}

# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "Punto Rojo API", "version": "1.0.0"}

@app.post("/parse-invoice")
async def parse_invoice_endpoint(
    file: UploadFile = File(...),
    iva_mode_override: Optional[str] = None,
):
    """Recibe un ZIP o XML de factura DIAN y devuelve los datos procesados."""
    raw = await file.read()
    filename = file.filename or ""

    try:
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml_names = [n for n in z.namelist() if n.lower().endswith(".xml") and not n.lower().startswith("__")]
                if not xml_names:
                    raise HTTPException(status_code=400, detail="El ZIP no contiene archivos XML.")
                raw_xml = z.read(xml_names[0])
        elif filename.lower().endswith(".xml"):
            raw_xml = raw
        else:
            raise HTTPException(status_code=400, detail="Solo se aceptan archivos XML o ZIP.")

        root_el = extract_invoice_xml(raw_xml)
        invoice  = parse_invoice(root_el)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error al leer el XML: {str(e)}")

    # Usar iva_mode detectado o el override
    iva_mode = iva_mode_override or invoice["iva_detectado"]

    # Conectar a Supabase para enriquecer datos
    try:
        sb = get_supabase()

        # Verificar duplicado
        invoice["es_duplicado"] = check_duplicate(sb, invoice["cufe"], invoice["numero_factura"])

        # Info del proveedor contable — necesaria ANTES de calcular costos
        prov_info = get_proveedor_info(sb, invoice["proveedor_nit"] or "", invoice["proveedor"])
        invoice["proveedor_info"] = prov_info

        # Marcar descuento_afecta_costo en cada línea según el proveedor
        descuento_afecta = bool(prov_info.get("descuento_afecta_costo", False))
        for line in invoice["lineas"]:
            line["descuento_afecta_costo"] = descuento_afecta

    except Exception as e:
        invoice["supabase_error"] = str(e)
        invoice["es_duplicado"] = False
        invoice["proveedor_info"] = {}

    # Calcular costos y precios (después de marcar descuento_afecta_costo)
    invoice["lineas"] = add_calcs(invoice["lineas"], iva_mode)
    invoice["iva_mode_usado"] = iva_mode

    # Conectar a Supabase para match de productos
    try:
        sb = get_supabase()

        # Match de productos — si existe, pre-llenar con datos guardados
        for line in invoice["lineas"]:
            match_info = find_similar_product(sb, invoice["proveedor_nit"] or "", line["sku_proveedor"], line["nombre_factura"])
            line["match_tipo"]  = match_info["match"]
            line["producto_bd"] = match_info["producto"]

            if match_info["match"] == "Exacto" and match_info["producto"]:
                p = match_info["producto"]
                # Pre-llenar con datos guardados (nombre, márgenes, presentación, cómo se vende)
                # El costo se recalcula siempre desde la factura nueva
                line["producto_id"]          = p.get("id")
                line["nombre_punto_rojo"]    = p.get("nombre_punto_rojo") or line["nombre_factura"]
                line["categoria"]            = p.get("categoria") or ""
                line["presentacion_facturada"] = p.get("presentacion_facturada") or line["presentacion_facturada"]
                line["unidades_por_paquete"] = p.get("unidades_por_paquete") or line["unidades_por_paquete"]
                line["paquetes_por_caja"]    = p.get("paquetes_por_caja") or line["paquetes_por_caja"]
                line["unidades_por_caja"]    = p.get("unidades_por_caja") or line["unidades_por_caja"]
                line["markup_unidad_pct"]    = p.get("markup_unidad_pct") or line["markup_unidad_pct"]
                line["markup_paquete_pct"]   = p.get("markup_paquete_pct") or line["markup_paquete_pct"]
                line["markup_caja_pct"]      = p.get("markup_caja_pct") or line["markup_caja_pct"]
                line["venta_unidad"]         = p.get("venta_unidad") if p.get("venta_unidad") is not None else line["venta_unidad"]
                line["venta_paquete"]        = p.get("venta_paquete") if p.get("venta_paquete") is not None else line["venta_paquete"]
                line["venta_caja"]           = p.get("venta_caja") if p.get("venta_caja") is not None else line["venta_caja"]
                line["costo_anterior"]       = float(p.get("costo_unidad_sin_iva") or 0)

    except Exception as e:
        invoice["supabase_match_error"] = str(e)

    return invoice


@app.post("/save-invoice")
async def save_invoice_endpoint(data: dict):
    """Guarda la factura y sus productos en Supabase."""
    try:
        sb = get_supabase()
        invoice = data.get("invoice", {})
        lineas  = data.get("lineas", [])
        iva_mode = data.get("iva_mode", "NO_INCLUIDO")

        # 1. Buscar o crear proveedor
        nit    = invoice.get("proveedor_nit", "")
        nombre = invoice.get("proveedor", "")
        r = sb.table("proveedores").select("id").eq("nit", nit).limit(1).execute() if nit else None
        if r and r.data:
            proveedor_id = r.data[0]["id"]
        else:
            r2 = sb.table("proveedores").insert({"nombre": nombre, "nit": nit}).execute()
            proveedor_id = r2.data[0]["id"]

        # 2. Insertar factura
        fac = sb.table("facturas").insert({
            "proveedor_id":    proveedor_id,
            "proveedor_nit":   nit,
            "numero_factura":  invoice.get("numero_factura"),
            "fecha":           invoice.get("fecha"),
            "subtotal":        invoice.get("subtotal_factura"),
            "iva":             invoice.get("iva_factura"),
            "total":           invoice.get("total_factura"),
            "iva_modo":        iva_mode,
            "cufe":            invoice.get("cufe"),
            "archivo":         invoice.get("archivo_nombre", ""),
        }).execute()
        factura_id = fac.data[0]["id"]

        # 3. Insertar/actualizar productos e historial
        for line in lineas:
            sku_int  = line.get("sku_interno") or line.get("sku_proveedor", "")
            prod_id  = line.get("producto_id")
            cu       = float(line.get("costo_unidad_sin_iva") or 0)
            cp       = float(line.get("costo_paquete_sin_iva") or 0)
            cc       = float(line.get("costo_caja_sin_iva") or 0)
            up       = int(line.get("unidades_por_paquete") or 1)
            pc       = int(line.get("paquetes_por_caja") or 1)
            uc       = int(line.get("unidades_por_caja") or (up * pc))

            if prod_id:
                # Producto existente — obtener costo anterior
                old = sb.table("productos").select("costo_unidad_sin_iva").eq("id", prod_id).single().execute()
                costo_ant = float(old.data.get("costo_unidad_sin_iva") or 0) if old.data else 0
                variacion = round(((cu - costo_ant) / costo_ant * 100), 2) if costo_ant > 0 else 0

                sb.table("productos").update({
                    "costo_unidad_sin_iva":  cu,
                    "costo_paquete_sin_iva": cp,
                    "costo_caja_sin_iva":    cc,
                    "unidades_por_paquete": up,
                    "paquetes_por_caja":    pc,
                    "unidades_por_caja":    uc,
                    "ultima_factura":       invoice.get("numero_factura"),
                    "ultima_fecha":         invoice.get("fecha"),
                    "markup_unidad_pct":    float(line.get("markup_unidad_pct") or 40),
                    "markup_paquete_pct":   float(line.get("markup_paquete_pct") or 35),
                    "markup_caja_pct":      float(line.get("markup_caja_pct") or 30),
                    "venta_unidad":         bool(line.get("venta_unidad")),
                    "venta_paquete":        bool(line.get("venta_paquete")),
                    "venta_caja":           bool(line.get("venta_caja")),
                    "nota_descuento":       line.get("nota_descuento") or "",
                }).eq("id", prod_id).execute()

                estado = "NUEVO" if costo_ant == 0 else ("SUBIO" if cu > costo_ant else "BAJO" if cu < costo_ant else "SIN_CAMBIO")
            else:
                costo_ant = 0
                variacion = 0
                estado    = "NUEVO"
                res = sb.table("productos").insert({
                    "proveedor_id":         proveedor_id,
                    "sku_proveedor":        line.get("sku_proveedor", ""),
                    "sku_interno":          sku_int,
                    "nombre_factura":       line.get("nombre_factura", ""),
                    "nombre_punto_rojo":    line.get("nombre_punto_rojo") or line.get("nombre_factura", ""),
                    "categoria":            line.get("categoria", ""),
                    "presentacion_facturada": line.get("presentacion_facturada", "Unidad"),
                    "unidades_por_paquete": up,
                    "paquetes_por_caja":    pc,
                    "unidades_por_caja":    uc,
                    "costo_unidad_sin_iva":  cu,
                    "costo_paquete_sin_iva": cp,
                    "costo_caja_sin_iva":    cc,
                    "markup_unidad_pct":    float(line.get("markup_unidad_pct") or 40),
                    "markup_paquete_pct":   float(line.get("markup_paquete_pct") or 35),
                    "markup_caja_pct":      float(line.get("markup_caja_pct") or 30),
                    "venta_unidad":         bool(line.get("venta_unidad")),
                    "venta_paquete":        bool(line.get("venta_paquete")),
                    "venta_caja":           bool(line.get("venta_caja")),
                    "activo":               True,
                    "ultima_factura":       invoice.get("numero_factura"),
                    "ultima_fecha":         invoice.get("fecha"),
                    "nota_descuento":       line.get("nota_descuento") or "",
                }).execute()
                prod_id = res.data[0]["id"]

            # Historial de costos
            sb.table("historial_costos").insert({
                "factura_id":                   factura_id,
                "producto_id":                  prod_id,
                "estado":                       estado,
                "presentacion_facturada":       line.get("presentacion_facturada"),
                "costo_presentacion_facturada": float(line.get("costo_caja_sin_iva") or cu),
                "costo_unidad_anterior":        costo_ant,
                "costo_unidad_nuevo":           cu,
                "variacion_porcentaje":         variacion,
                "venta_unidad":                 bool(line.get("venta_unidad")),
                "venta_paquete":                bool(line.get("venta_paquete")),
                "venta_caja":                   bool(line.get("venta_caja")),
            }).execute()

        # 4. Factura contable
        prov_info = data.get("proveedor_info", {})
        forma_pago = prov_info.get("forma_pago", "CREDITO")
        desc_pct   = float(prov_info.get("descuento_pct") or 0)
        subtotal   = float(invoice.get("subtotal_factura") or 0)
        iva_val    = float(invoice.get("iva_factura") or 0)
        valor_desc = round(subtotal * desc_pct / 100, 2)
        valor_pagar = subtotal + iva_val - valor_desc

        sb.table("facturas_contables").insert({
            "factura_id":      factura_id,
            "proveedor":       nombre,
            "numero_factura":  invoice.get("numero_factura"),
            "fecha_factura":   invoice.get("fecha"),
            "fecha_revision":  str(date.today()),
            "forma_pago":      forma_pago,
            "subtotal":        subtotal,
            "descuento_pct":   desc_pct,
            "valor_descuento": valor_desc,
            "aplica_retefuente": prov_info.get("aplica_retefuente", "NO"),
            "retefuente":      0,
            "iva":             iva_val,
            "valor_a_pagar":   valor_pagar,
            "precios_revisados": "NO",
            "cufe":            invoice.get("cufe", ""),
        }).execute()

        return {"ok": True, "factura_id": factura_id, "mensaje": f"Factura {invoice.get('numero_factura')} guardada correctamente."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
