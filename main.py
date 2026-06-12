"""
Punto Rojo — API de procesamiento de facturas XML DIAN
Servidor FastAPI que recibe ZIP/XML y devuelve los datos procesados.
"""
import os, io, re, zipfile, json
import xml.etree.ElementTree as ET
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from dotenv import load_dotenv
import resend

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

        desc_amt = 0.0
        desc_pct = 0.0
        for ac in all_descendants(line, "AllowanceCharge"):
            charge_ind = first_text(ac, ["ChargeIndicator"])
            if charge_ind.lower() == "false":
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
            "unidades_por_caja": up * packs,
            "venta_unidad": vu,
            "venta_paquete": vp,
            "venta_caja": vc,
            "markup_unidad_pct": 40.0,
            "markup_paquete_pct": 35.0,
            "markup_caja_pct": 30.0,
            "descuento_factura_pct": desc_pct,
            "descuento_factura_amt": desc_amt,
            "nota_descuento": f"Descuento en factura {desc_pct:.0f}% (${(desc_amt / max(qty, 1)):,.0f} por unidad compra)" if desc_amt > 0 else "",
        })

    if not subtotal: subtotal = sum(x["subtotal_linea"] for x in lines)
    if not iva:      iva = sum(x["iva_linea"] for x in lines)
    if not total:    total = subtotal + iva

    retefuente_xml = 0.0
    for ta in all_descendants(root, "TaxTotal"):
        tax_id   = ""
        tax_name = ""
        for sc in all_descendants(ta, "TaxScheme"):
            tax_id   = first_text(sc, ["ID"]) or ""
            tax_name = (first_text(sc, ["Name"]) or "").upper()
        if tax_id in ("06", "05", "07") or "RETE" in tax_name or "RTEFUENTE" in tax_name:
            retefuente_xml += parse_decimal(first_text(ta, ["TaxAmount"]))

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
        "retefuente_xml": money(retefuente_xml),
        "inpusu": money(inpusu),
        "iva_detectado": iva_detectado,
        "lineas": lines,
    }

# ─── Cálculo de costos y precios ─────────────────────────────────────────────

def cost_without_tax(precio: float, iva_mode: str, iva_pct: float = IVA_DEFAULT) -> float:
    if iva_mode == "INCLUIDO":
        return money(precio / (1 + iva_pct / 100))
    return money(precio)

def calc_costs(costo_fact: float, pres: str, up: int, pc: int, precio_es_por: str = ""):
    up = max(up, 1); pc = max(pc, 1)
    uc = up * pc

    base = precio_es_por if precio_es_por else (
        "Caja"    if pres == "Caja/Paca" else
        "Paquete" if pres == "Paquete"   else
        "Unidad"
    )

    if base == "Caja":
        cc = costo_fact
        cp = money(cc / pc) if pc > 1 else cc
        cu = money(cp / up) if up > 1 else cp
    elif base == "Paquete":
        cp = costo_fact
        cu = money(cp / up) if up > 1 else cp
        cc = money(cp * pc)
    else:
        cu = costo_fact
        cp = money(cu * up)
        cc = money(cu * uc)

    return cu, cp, cc

def inferir_precio_es_por(precio: float, up: int, pc: int, uc: int) -> str:
    """Infiere si el precio facturado es por unidad, paquete o caja
    usando lógica matemática: el costo unitario resultante debe ser razonable (>= 0.5 COP).
    
    Regla principal: si precio / up >= 0.5 y up > 1 → precio es por paquete.
    Si precio / uc >= 0.5 pero precio / up < 0.5 → precio es por caja.
    """
    MIN_CU = 0.5
    up = max(up, 1); uc = max(uc, 1)

    cu_paquete = precio / up if up > 1 else None
    cu_caja    = precio / uc if uc > up else None

    # Si precio/up da costo razonable y up > 1 → precio es por paquete
    if cu_paquete and cu_paquete >= MIN_CU:
        if cu_caja is None or cu_caja < MIN_CU:
            return "Paquete"
        if up > 1:
            return "Paquete"

    # Si precio/uc razonable y precio/up muy alto o no razonable → precio es por caja
    if cu_caja and cu_caja >= MIN_CU and (cu_paquete is None or cu_paquete > 10000):
        return "Caja"

    return "Unidad"

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
        desc_pct = float(l.get("descuento_factura_pct") or 0)
        if l.get("descuento_afecta_costo") and desc_pct > 0:
            precio_fact = money(precio_fact * (1 - desc_pct / 100))

        costo_base       = cost_without_tax(precio_fact, iva_mode, l.get("iva_porcentaje", IVA_DEFAULT))
        transporte       = float(l.get("transporte_adicional", 0) or 0)
        costo_fact_final = costo_base + transporte

        # Usar precio_es_por del usuario si viene, si no inferir automáticamente
        precio_es_por = l.get("precio_es_por") or ""
        if not precio_es_por:
            precio_es_por = inferir_precio_es_por(costo_fact_final, up, pc, uc)

        cu, cp, cc = calc_costs(costo_fact_final, pres, up, pc, precio_es_por)

        row = {**l,
            "precio_es_por": precio_es_por,
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
    try:
        r = supabase.table("productos").select(
            "id,sku_interno,sku_proveedor,nombre_punto_rojo,categoria,"
            "presentacion_facturada,precio_es_por,unidades_por_paquete,paquetes_por_caja,unidades_por_caja,"
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
        if cufe:
            r = supabase.table("facturas").select("id").eq("cufe", cufe).execute()
            if r.data:
                return True
        if numero_factura:
            r2 = supabase.table("facturas").select("id").eq("numero_factura", numero_factura).execute()
            return bool(r2.data)
        return False
    except Exception:
        return False

def get_proveedor_info(supabase: Client, nit: str, nombre: str) -> dict:
    try:
        if nit:
            r = supabase.table("proveedores_contables").select(
                "id,proveedor_nombre,forma_pago,descuento_pct,aplica_retefuente,tipo,regimen,descuento_afecta_costo"
            ).eq("nit", nit).limit(1).execute()
            if r.data:
                return r.data[0]

        r2 = supabase.rpc("match_proveedor", {"nombre_buscar": nombre}).execute()
        if r2.data:
            row = r2.data[0]
            return {
                "id":                    row.get("id"),
                "proveedor_nombre":      row.get("nombre") or row.get("proveedor_nombre"),
                "nit":                   row.get("nit"),
                "forma_pago":            row.get("forma_pago"),
                "descuento_pct":         row.get("descuento_pct"),
                "aplica_retefuente":     row.get("aplica_retefuente"),
                "tipo":                  row.get("tipo"),
                "regimen":               row.get("regimen"),
                "descuento_afecta_costo": row.get("descuento_afecta_costo", False),
                "similitud":             row.get("similitud"),
            }
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
    raw = await file.read()
    filename = file.filename or ""

    pdf_base64 = ""
    try:
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml_names = [n for n in z.namelist() if n.lower().endswith(".xml") and not n.lower().startswith("__")]
                if not xml_names:
                    raise HTTPException(status_code=400, detail="El ZIP no contiene archivos XML.")
                raw_xml = z.read(xml_names[0])
                pdf_names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
                if pdf_names:
                    import base64 as b64lib
                    pdf_base64 = b64lib.b64encode(z.read(pdf_names[0])).decode("utf-8")
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

    iva_mode = iva_mode_override or invoice["iva_detectado"]

    try:
        sb = get_supabase()
        invoice["es_duplicado"] = check_duplicate(sb, invoice["cufe"], invoice["numero_factura"])
        prov_info = get_proveedor_info(sb, invoice["proveedor_nit"] or "", invoice["proveedor"])
        invoice["proveedor_info"] = prov_info
        for line in invoice["lineas"]:
            line["descuento_afecta_costo"] = False
    except Exception as e:
        invoice["supabase_error"] = str(e)
        invoice["es_duplicado"] = False
        invoice["proveedor_info"] = {}

    invoice["iva_mode_usado"] = iva_mode

    try:
        sb = get_supabase()
        for line in invoice["lineas"]:
            match_info = find_similar_product(sb, invoice["proveedor_nit"] or "", line["sku_proveedor"], line["nombre_factura"])
            line["match_tipo"]  = match_info["match"]
            line["producto_bd"] = match_info["producto"]

            if match_info["match"] == "Exacto" and match_info["producto"]:
                p = match_info["producto"]
                line["producto_id"]            = p.get("id")
                line["nombre_punto_rojo"]      = p.get("nombre_punto_rojo") or line["nombre_factura"]
                line["categoria"]              = p.get("categoria") or ""
                line["presentacion_facturada"] = p.get("presentacion_facturada") or line["presentacion_facturada"]
                line["precio_es_por"]          = p.get("precio_es_por") or ""
                line["unidades_por_paquete"]   = p.get("unidades_por_paquete") or line["unidades_por_paquete"]
                line["paquetes_por_caja"]      = p.get("paquetes_por_caja") or line["paquetes_por_caja"]
                up_match = p.get("unidades_por_paquete") or line["unidades_por_paquete"]
                pc_match = p.get("paquetes_por_caja") or line["paquetes_por_caja"]
                line["unidades_por_caja"]      = up_match * pc_match
                line["markup_unidad_pct"]      = p.get("markup_unidad_pct") or line["markup_unidad_pct"]
                line["markup_paquete_pct"]     = p.get("markup_paquete_pct") or line["markup_paquete_pct"]
                line["markup_caja_pct"]        = p.get("markup_caja_pct") or line["markup_caja_pct"]
                line["venta_unidad"]           = p.get("venta_unidad") if p.get("venta_unidad") is not None else line["venta_unidad"]
                line["venta_paquete"]          = p.get("venta_paquete") if p.get("venta_paquete") is not None else line["venta_paquete"]
                line["venta_caja"]             = p.get("venta_caja") if p.get("venta_caja") is not None else line["venta_caja"]
                line["costo_anterior"]         = float(p.get("costo_unidad_sin_iva") or 0)

    except Exception as e:
        invoice["supabase_match_error"] = str(e)

    invoice["lineas"] = add_calcs(invoice["lineas"], iva_mode)
    invoice["pdf_base64"] = pdf_base64

    return invoice


def recalcular_retefuente_grupo(supabase: Client, proveedor: str, fecha_factura: str, aplica_rete: str) -> None:
    if aplica_rete != "SI":
        return
    try:
        params = supabase.table("parametros_retefuente").select(
            "porcentaje,base_minima"
        ).eq("aplica_a", "COMPRAS").eq("activo", True).lte("vigente_desde", fecha_factura).gte("vigente_hasta", fecha_factura).limit(1).execute()
        if not params.data:
            return
        pct_rete = float(params.data[0].get("porcentaje") or 2.5)
        base_min = float(params.data[0].get("base_minima") or 1148000)
        facturas = supabase.table("facturas_contables").select(
            "id,subtotal,retefuente"
        ).eq("proveedor", proveedor).eq("fecha_factura", fecha_factura).execute()
        if not facturas.data:
            return
        total_subtotal = sum(float(f.get("subtotal") or 0) for f in facturas.data)
        if total_subtotal >= base_min:
            for f in facturas.data:
                subtotal_f = float(f.get("subtotal") or 0)
                rete_f = round(subtotal_f * pct_rete / 100, 2)
                valor_pagar_query = supabase.table("facturas_contables").select(
                    "subtotal,iva,valor_descuento"
                ).eq("id", f["id"]).single().execute()
                if valor_pagar_query.data:
                    vd = valor_pagar_query.data
                    nuevo_valor = float(vd.get("subtotal") or 0) + float(vd.get("iva") or 0) - float(vd.get("valor_descuento") or 0) - rete_f
                    supabase.table("facturas_contables").update({
                        "retefuente": rete_f,
                        "valor_a_pagar": nuevo_valor
                    }).eq("id", f["id"]).execute()
        else:
            for f in facturas.data:
                if float(f.get("retefuente") or 0) > 0:
                    valor_pagar_query = supabase.table("facturas_contables").select(
                        "subtotal,iva,valor_descuento"
                    ).eq("id", f["id"]).single().execute()
                    if valor_pagar_query.data:
                        vd = valor_pagar_query.data
                        nuevo_valor = float(vd.get("subtotal") or 0) + float(vd.get("iva") or 0) - float(vd.get("valor_descuento") or 0)
                        supabase.table("facturas_contables").update({
                            "retefuente": 0,
                            "valor_a_pagar": nuevo_valor
                        }).eq("id", f["id"]).execute()
    except Exception:
        pass


def generate_sku(supabase: Client, categoria: str) -> str:
    import unicodedata
    prefix = categoria.strip().upper() if categoria else "PROD"
    prefix = ''.join(
        c for c in unicodedata.normalize('NFD', prefix)
        if unicodedata.category(c) != 'Mn'
    )
    prefix = ''.join(c for c in prefix if c.isalnum())[:8]
    if not prefix:
        prefix = "PROD"
    try:
        r = supabase.table("productos").select("sku_interno").like("sku_interno", f"{prefix}-%").execute()
        count = len(r.data) if r.data else 0
    except Exception:
        count = 0
    return f"{prefix}-{str(count + 1).zfill(4)}"


@app.post("/save-invoice")
async def save_invoice_endpoint(data: dict):
    try:
        sb = get_supabase()
        invoice  = data.get("invoice", {})
        lineas   = data.get("lineas", [])
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

        # 1b. Actualizar NIT en proveedores_contables si no lo tiene
        if nit:
            try:
                rc = sb.rpc("match_proveedor", {"nombre_buscar": nombre}).execute()
                if rc.data:
                    pc_id = rc.data[0].get("id")
                    pc_nit = rc.data[0].get("nit")
                    if pc_id and not pc_nit:
                        sb.table("proveedores_contables").update({"nit": nit}).eq("id", pc_id).execute()
            except Exception:
                pass

        # 2. Insertar factura
        fac = sb.table("facturas").insert({
            "proveedor_id":   proveedor_id,
            "proveedor_nit":  nit,
            "numero_factura": invoice.get("numero_factura"),
            "fecha":          invoice.get("fecha"),
            "subtotal":       invoice.get("subtotal_factura"),
            "iva":            invoice.get("iva_factura"),
            "total":          invoice.get("total_factura"),
            "iva_modo":       iva_mode,
            "cufe":           invoice.get("cufe"),
            "archivo":        invoice.get("archivo_nombre", ""),
        }).execute()
        factura_id = fac.data[0]["id"]

        # 3. Insertar/actualizar productos e historial
        for line in lineas:
            prod_id = line.get("producto_id")
            up      = int(line.get("unidades_por_paquete") or 1)
            pc      = int(line.get("paquetes_por_caja") or 1)
            uc      = up * pc

            precio_fact      = float(line.get("precio_unitario_factura") or 0)
            desc_pct_l       = float(line.get("descuento_factura_pct") or 0)
            precio_fact_base = precio_fact  # guardar original antes del descuento
            if line.get("descuento_afecta_costo") and desc_pct_l > 0:
                precio_fact = money(precio_fact * (1 - desc_pct_l / 100))

            iva_mode_s  = data.get("iva_mode", "NO_INCLUIDO")
            iva_pct_l   = float(line.get("iva_porcentaje") or IVA_DEFAULT)
            costo_base  = cost_without_tax(precio_fact, iva_mode_s, iva_pct_l)
            transporte  = float(line.get("transporte_adicional") or 0)
            costo_final = costo_base + transporte
            pres_s      = line.get("presentacion_facturada", "Unidad")

            # Usar precio_es_por del usuario si viene, si no inferir
            precio_es_por_s = line.get("precio_es_por") or ""
            if not precio_es_por_s:
                precio_es_por_s = inferir_precio_es_por(costo_final, up, pc, uc)

            cu, cp, cc = calc_costs(costo_final, pres_s, up, pc, precio_es_por_s)

            if prod_id:
                old = sb.table("productos").select("costo_unidad_sin_iva").eq("id", prod_id).single().execute()
                costo_ant = float(old.data.get("costo_unidad_sin_iva") or 0) if old.data else 0
                variacion = round(((cu - costo_ant) / costo_ant * 100), 2) if costo_ant > 0 else 0

                sb.table("productos").update({
                    "costo_unidad_sin_iva":   cu,
                    "costo_paquete_sin_iva":  cp,
                    "costo_caja_sin_iva":     cc,
                    "unidades_por_paquete":   up,
                    "paquetes_por_caja":      pc,
                    "unidades_por_caja":      uc,
                    "ultima_factura":         invoice.get("numero_factura"),
                    "ultima_fecha":           invoice.get("fecha"),
                    "markup_unidad_pct":      float(line.get("markup_unidad_pct") or 40),
                    "markup_paquete_pct":     float(line.get("markup_paquete_pct") or 35),
                    "markup_caja_pct":        float(line.get("markup_caja_pct") or 30),
                    "venta_unidad":           bool(line.get("venta_unidad")),
                    "venta_paquete":          bool(line.get("venta_paquete")),
                    "venta_caja":             bool(line.get("venta_caja")),
                    "nota_descuento":         line.get("nota_descuento") or "",
                    "precio_factura_base":    precio_fact_base,
                    "precio_es_por":          precio_es_por_s,
                    "iva_porcentaje":         iva_pct_l,
                }).eq("id", prod_id).execute()

                estado = "NUEVO" if costo_ant == 0 else ("SUBIO" if cu > costo_ant else "BAJO" if cu < costo_ant else "SIN_CAMBIO")
            else:
                costo_ant = 0
                variacion = 0
                estado    = "NUEVO"
                categoria_line = line.get("categoria", "") or ""
                sku_int = generate_sku(sb, categoria_line)
                res = sb.table("productos").upsert({
                    "proveedor_id":           proveedor_id,
                    "sku_proveedor":          line.get("sku_proveedor", ""),
                    "sku_interno":            sku_int,
                    "nombre_factura":         line.get("nombre_factura", ""),
                    "nombre_punto_rojo":      line.get("nombre_punto_rojo") or line.get("nombre_factura", ""),
                    "categoria":              line.get("categoria", ""),
                    "presentacion_facturada": line.get("presentacion_facturada", "Unidad"),
                    "unidades_por_paquete":   up,
                    "paquetes_por_caja":      pc,
                    "unidades_por_caja":      uc,
                    "costo_unidad_sin_iva":   cu,
                    "costo_paquete_sin_iva":  cp,
                    "costo_caja_sin_iva":     cc,
                    "markup_unidad_pct":      float(line.get("markup_unidad_pct") or 40),
                    "markup_paquete_pct":     float(line.get("markup_paquete_pct") or 35),
                    "markup_caja_pct":        float(line.get("markup_caja_pct") or 30),
                    "venta_unidad":           bool(line.get("venta_unidad")),
                    "venta_paquete":          bool(line.get("venta_paquete")),
                    "venta_caja":             bool(line.get("venta_caja")),
                    "activo":                 True,
                    "ultima_factura":         invoice.get("numero_factura"),
                    "ultima_fecha":           invoice.get("fecha"),
                    "nota_descuento":         line.get("nota_descuento") or "",
                    "precio_factura_base":    precio_fact_base,
                    "precio_es_por":          precio_es_por_s,
                    "iva_porcentaje":         iva_pct_l,
                }, on_conflict="proveedor_id,sku_proveedor,nombre_factura").execute()
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
                "precio_factura_original":      precio_fact_base,
                "precio_es_por":                precio_es_por_s,
            }).execute()

            # Historial de descuentos
            desc_pct_hist = float(line.get("descuento_factura_pct") or 0)
            if desc_pct_hist > 0:
                desc_aplicado = bool(line.get("descuento_afecta_costo", False))
                nota_hist = line.get("nota_descuento") or f"Descuento {desc_pct_hist:.0f}% en factura"
                if desc_aplicado:
                    nota_hist = f"{nota_hist} — aplicado al costo"
                else:
                    nota_hist = f"{nota_hist} — NO aplicado al costo"
                sb.table("historial_descuentos").insert({
                    "producto_id":   prod_id,
                    "descuento_pct": desc_pct_hist,
                    "nota":          nota_hist,
                    "factura_id":    factura_id,
                }).execute()
                sb.table("productos").update({
                    "descuento_pct_factura": desc_pct_hist,
                    "descuento_aplicado":    desc_aplicado,
                }).eq("id", prod_id).execute()

        # 4. Factura contable
        prov_info  = data.get("proveedor_info", {})
        forma_pago = prov_info.get("forma_pago", "CREDITO")
        desc_pct   = float(prov_info.get("descuento_pct") or 0)
        subtotal   = float(invoice.get("subtotal_factura") or 0)
        iva_val    = float(invoice.get("iva_factura") or 0)
        valor_desc = round(subtotal * desc_pct / 100, 2)

        retefuente     = 0.0
        aplica_rete    = prov_info.get("aplica_retefuente", "NO")
        retefuente_xml = float(invoice.get("retefuente_xml") or 0)

        if retefuente_xml > 0:
            retefuente = retefuente_xml
        elif aplica_rete == "SI":
            try:
                fecha_factura = invoice.get("fecha") or str(date.today())
                params = sb.table("parametros_retefuente").select(
                    "porcentaje,base_minima"
                ).eq("aplica_a", "COMPRAS").eq("activo", True).lte("vigente_desde", fecha_factura).gte("vigente_hasta", fecha_factura).limit(1).execute()
                if params.data:
                    pct_rete = float(params.data[0].get("porcentaje") or 2.5)
                    base_min = float(params.data[0].get("base_minima") or 1148000)
                    if subtotal >= base_min:
                        retefuente = round(subtotal * pct_rete / 100, 2)
            except Exception:
                retefuente = 0.0

        valor_pagar = subtotal + iva_val - valor_desc - retefuente

        sb.table("facturas_contables").insert({
            "factura_id":        factura_id,
            "proveedor":         nombre,
            "numero_factura":    invoice.get("numero_factura"),
            "fecha_factura":     invoice.get("fecha"),
            "fecha_revision":    str(date.today()),
            "forma_pago":        forma_pago,
            "subtotal":          subtotal,
            "descuento_pct":     desc_pct,
            "valor_descuento":   valor_desc,
            "aplica_retefuente": aplica_rete,
            "retefuente":        retefuente,
            "iva":               iva_val,
            "valor_a_pagar":     valor_pagar,
            "precios_revisados": "NO",
            "cufe":              invoice.get("cufe", ""),
        }).execute()

        if not retefuente_xml:
            recalcular_retefuente_grupo(sb, nombre, invoice.get("fecha", ""), aplica_rete)

        return {"ok": True, "factura_id": factura_id, "mensaje": f"Factura {invoice.get('numero_factura')} guardada correctamente."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/parse-credit-note")
async def parse_credit_note_endpoint(file: UploadFile = File(...)):
    raw = await file.read()
    filename = file.filename or ""

    try:
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml_names = [n for n in z.namelist() if n.lower().endswith(".xml") and not n.lower().startswith("__")]
                if not xml_names:
                    raise HTTPException(status_code=400, detail="El ZIP no contiene XML.")
                raw_xml = z.read(xml_names[0])
        elif filename.lower().endswith(".xml"):
            raw_xml = raw
        else:
            raise HTTPException(status_code=400, detail="Solo se aceptan XML o ZIP.")

        root_el = extract_invoice_xml(raw_xml)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error al leer XML: {str(e)}")

    try:
        supplier = (
            first_text(root_el, ["AccountingSupplierParty","Party","PartyLegalEntity","RegistrationName"]) or
            first_text(root_el, ["AccountingSupplierParty","Party","PartyName","Name"]) or ""
        )
        nit = (
            first_text(root_el, ["AccountingSupplierParty","Party","PartyTaxScheme","CompanyID"]) or
            first_text(root_el, ["AccountingSupplierParty","Party","PartyLegalEntity","CompanyID"]) or ""
        )
        numero_nota = first_text(root_el, ["ID"]) or ""
        fecha_nota  = first_text(root_el, ["IssueDate"]) or str(date.today())

        factura_original = ""
        for ref in all_descendants(root_el, "BillingReference"):
            factura_original = first_text(ref, ["InvoiceDocumentReference","ID"]) or ""
            if factura_original:
                break
        if not factura_original:
            for dr in all_descendants(root_el, "DiscrepancyResponse"):
                factura_original = first_text(dr, ["ReferenceID"]) or ""
                if factura_original:
                    break

        subtotal = parse_decimal(first_text(root_el, ["LegalMonetaryTotal","LineExtensionAmount"]))
        total    = parse_decimal(first_text(root_el, ["LegalMonetaryTotal","PayableAmount"]))
        iva      = money(total - subtotal) if subtotal and total and total >= subtotal else 0.0

        retefuente = 0.0
        for ta in all_descendants(root_el, "TaxTotal"):
            tax_id   = ""
            tax_name = ""
            for sc in all_descendants(ta, "TaxScheme"):
                tax_id   = first_text(sc, ["ID"]) or ""
                tax_name = (first_text(sc, ["Name"]) or "").upper()
            if tax_id in ("06","05","07") or "RETE" in tax_name:
                retefuente += parse_decimal(first_text(ta, ["TaxAmount"]))

        motivo = ""
        for dr in all_descendants(root_el, "DiscrepancyResponse"):
            motivo = first_text(dr, ["Description"]) or ""
            if motivo:
                break
        if not motivo:
            for note in all_descendants(root_el, "Note"):
                if note.text and note.text.strip():
                    motivo = note.text.strip()
                    break

        factura_contable_id = None
        try:
            sb = get_supabase()
            if factura_original:
                r = sb.table("facturas_contables").select("id,subtotal,iva,retefuente,valor_a_pagar,proveedor").eq("numero_factura", factura_original).limit(1).execute()
                if r.data:
                    factura_contable_id = r.data[0]["id"]
        except Exception:
            pass

        return {
            "proveedor":           supplier,
            "proveedor_nit":       nit,
            "numero_nota":         numero_nota,
            "fecha_nota":          fecha_nota,
            "factura_original":    factura_original,
            "factura_contable_id": factura_contable_id,
            "subtotal":            money(subtotal),
            "iva":                 money(iva),
            "retefuente":          money(retefuente),
            "total":               money(total),
            "motivo":              motivo,
        }

    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error al parsear nota crédito: {str(e)}")


@app.post("/toggle-descuento")
async def toggle_descuento(data: dict):
    try:
        sb       = get_supabase()
        prod_id  = data.get("producto_id")
        aplicado = bool(data.get("descuento_aplicado", False))
        nota     = data.get("nota", "")

        prod = sb.table("productos").select(
            "precio_factura_base,precio_es_por,descuento_pct_factura,"
            "unidades_por_paquete,paquetes_por_caja,presentacion_facturada,iva_porcentaje"
        ).eq("id", prod_id).single().execute()

        if not prod.data:
            raise HTTPException(status_code=404, detail="Producto no encontrado")

        p             = prod.data
        precio_base   = float(p.get("precio_factura_base") or 0)
        desc_pct      = float(p.get("descuento_pct_factura") or 0)
        precio_es_por = p.get("precio_es_por") or ""
        pres          = p.get("presentacion_facturada") or "Unidad"
        up            = int(p.get("unidades_por_paquete") or 1)
        pc            = int(p.get("paquetes_por_caja") or 1)
        iva_pct       = float(p.get("iva_porcentaje") or IVA_DEFAULT)

        if precio_base <= 0:
            raise HTTPException(status_code=400, detail="Este producto no tiene precio base guardado. Sube la factura nuevamente para activar esta función.")

        precio_fact = precio_base
        if aplicado and desc_pct > 0:
            precio_fact = money(precio_base * (1 - desc_pct / 100))

        costo_base = cost_without_tax(precio_fact, "NO_INCLUIDO", iva_pct)
        cu, cp, cc = calc_costs(costo_base, pres, up, pc, precio_es_por)

        sb.table("productos").update({
            "costo_unidad_sin_iva":  cu,
            "costo_paquete_sin_iva": cp,
            "costo_caja_sin_iva":    cc,
            "descuento_aplicado":    aplicado,
            "nota_descuento":        nota,
        }).eq("id", prod_id).execute()

        sb.table("historial_descuentos").insert({
            "producto_id":   prod_id,
            "descuento_pct": desc_pct if aplicado else 0,
            "nota":          nota or ("Descuento activado desde panel" if aplicado else "Descuento desactivado desde panel"),
        }).execute()

        return {
            "ok": True,
            "costo_unidad_sin_iva":  cu,
            "costo_paquete_sin_iva": cp,
            "costo_caja_sin_iva":    cc,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/recalc-precio-es-por")
async def recalc_precio_es_por(data: dict):
    """Recalcula los costos desde el precio original de factura usando el nuevo precio_es_por."""
    try:
        sb            = get_supabase()
        prod_id       = data.get("producto_id")
        precio_es_por = data.get("precio_es_por", "")
        pres          = data.get("presentacion_facturada", "")
        up            = int(data.get("unidades_por_paquete") or 1)
        pc            = int(data.get("paquetes_por_caja") or 1)

        prod = sb.table("productos").select(
            "precio_factura_base,descuento_pct_factura,descuento_aplicado,"
            "presentacion_facturada,unidades_por_paquete,paquetes_por_caja,iva_porcentaje"
        ).eq("id", prod_id).single().execute()

        if not prod.data:
            raise HTTPException(status_code=404, detail="Producto no encontrado")

        p            = prod.data
        precio_base  = float(p.get("precio_factura_base") or 0)

        if precio_base <= 0:
            raise HTTPException(status_code=400, detail="Este producto no tiene precio base guardado. Sube la factura nuevamente.")

        desc_pct      = float(p.get("descuento_pct_factura") or 0)
        desc_aplicado = bool(p.get("descuento_aplicado") or False)
        iva_pct       = float(p.get("iva_porcentaje") or IVA_DEFAULT)

        pres_final = pres or p.get("presentacion_facturada") or "Unidad"
        up_final   = up or int(p.get("unidades_por_paquete") or 1)
        pc_final   = pc or int(p.get("paquetes_por_caja") or 1)
        uc_final   = up_final * pc_final

        precio_fact = precio_base
        if desc_aplicado and desc_pct > 0:
            precio_fact = money(precio_base * (1 - desc_pct / 100))

        costo_base_val = cost_without_tax(precio_fact, "NO_INCLUIDO", iva_pct)

        # Si no viene precio_es_por, inferir automáticamente
        if not precio_es_por:
            precio_es_por = inferir_precio_es_por(costo_base_val, up_final, pc_final, uc_final)

        cu, cp, cc = calc_costs(costo_base_val, pres_final, up_final, pc_final, precio_es_por)

        sb.table("productos").update({
            "precio_es_por":          precio_es_por,
            "presentacion_facturada": pres_final,
            "unidades_por_paquete":   up_final,
            "paquetes_por_caja":      pc_final,
            "unidades_por_caja":      uc_final,
            "costo_unidad_sin_iva":   cu,
            "costo_paquete_sin_iva":  cp,
            "costo_caja_sin_iva":     cc,
        }).eq("id", prod_id).execute()

        return {
            "ok": True,
            "costo_unidad_sin_iva":  cu,
            "costo_paquete_sin_iva": cp,
            "costo_caja_sin_iva":    cc,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/extract-text")
async def extract_text(file: UploadFile = File(...)):
    try:
        data     = await file.read()
        filename = file.filename or ""
        ext      = filename.rsplit(".", 1)[-1].lower()
        texto    = ""

        if ext in ("xlsx", "xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
                filas = []
                for sheet in wb.worksheets:
                    for row in sheet.iter_rows(values_only=True):
                        fila = [str(c) if c is not None else "" for c in row]
                        if any(f.strip() for f in fila):
                            filas.append(" | ".join(fila))
                texto = "\n".join(filas)
            except Exception as e:
                texto = f"Error leyendo Excel: {e}"

        elif ext in ("docx", "doc"):
            try:
                import docx
                doc = docx.Document(io.BytesIO(data))
                texto = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                for table in doc.tables:
                    for row in table.rows:
                        texto += "\n" + " | ".join(c.text for c in row.cells)
            except Exception as e:
                texto = f"Error leyendo Word: {e}"

        else:
            texto = data.decode("utf-8", errors="ignore")

        return {"texto": texto[:15000]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/extract-lista")
async def extract_lista(file: UploadFile = File(...)):
    import base64, google.generativeai as genai

    try:
        GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
        if not GOOGLE_API_KEY:
            raise HTTPException(status_code=500, detail="GOOGLE_API_KEY no configurada")

        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        data     = await file.read()
        filename = file.filename or ""
        ext      = filename.rsplit(".", 1)[-1].lower()

        prompt = (
            "Eres un asistente que extrae listas de precios de proveedores. "
            "Analiza este documento y extrae TODOS los productos con sus precios. "
            "Los precios pueden usar punto como separador de miles (ej: 23.243 = 23243 pesos colombianos). "
            "Devuelve los precios como numeros enteros sin puntos ni comas. "
            "Responde SOLO con JSON valido, sin texto adicional, sin backticks: "
            '{"productos":[{"nombre":"nombre del producto","sku":"codigo si existe sino vacio","precio":12500,"unidad":"unidad de medida"}]} '
            "Solo incluye productos reales con precios numericos. Ignora encabezados, totales y notas."
        )

        if ext in ("jpg", "jpeg", "png", "webp"):
            import PIL.Image
            img = PIL.Image.open(io.BytesIO(data))
            response = model.generate_content([prompt, img])
        elif ext == "pdf":
            pdf_part = {"mime_type": "application/pdf", "data": base64.b64encode(data).decode()}
            response = model.generate_content([prompt, pdf_part])
        else:
            texto = ""
            if ext in ("xlsx", "xls"):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
                    filas = []
                    for sheet in wb.worksheets:
                        for row in sheet.iter_rows(values_only=True):
                            fila = [str(c) if c is not None else "" for c in row]
                            if any(f.strip() for f in fila):
                                filas.append(" | ".join(fila))
                    texto = "\n".join(filas)
                except Exception as e:
                    texto = f"Error: {e}"
            elif ext in ("docx", "doc"):
                try:
                    import docxlib
                    doc = docxlib.Document(io.BytesIO(data))
                    lineas = [p.text for p in doc.paragraphs if p.text.strip()]
                    texto = "\n".join(lineas)
                except Exception as e:
                    texto = f"Error: {e}"
            else:
                texto = data.decode("utf-8", errors="ignore")
            response = model.generate_content(prompt + "\n\nContenido:\n" + texto[:10000])

        texto_resp = response.text or ""
        clean      = texto_resp.replace("```json", "").replace("```", "").strip()
        parsed     = json.loads(clean)
        productos  = parsed.get("productos", [])

        return {"productos": productos, "total": len(productos)}

    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="No se pudo parsear la respuesta de Gemini")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/config-admin/{clave}")
async def get_config(clave: str):
    try:
        sb  = get_supabase()
        res = sb.table("config_admin").select("valor").eq("clave", clave).single().execute()
        return {"valor": res.data.get("valor", "") if res.data else ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/config-admin")
async def set_config(data: dict):
    try:
        sb = get_supabase()
        sb.table("config_admin").upsert({"clave": data["clave"], "valor": data["valor"]}).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/enviar-reporte-mensual")
async def enviar_reporte_mensual(data: dict):
    try:
        sb  = get_supabase()
        mes = data.get("mes", "")

        cfg    = sb.table("config_admin").select("valor").eq("clave", "correo_reporte").single().execute()
        correo = cfg.data.get("valor", "") if cfg.data else ""
        if not correo:
            raise HTTPException(status_code=400, detail="No hay correo configurado en Admin")

        total_compras       = float(data.get("total_compras", 0))
        total_iva_compras   = float(data.get("total_iva_compras", 0))
        total_rete          = float(data.get("total_rete", 0))
        total_arriendos     = float(data.get("total_arriendos", 0))
        ventas              = float(data.get("ventas", 0))
        iva_ventas          = float(data.get("iva_ventas", 0))
        iva_arriendos       = float(data.get("iva_arriendos", 0))
        iva_neto            = float(data.get("iva_neto", 0))
        facturas_pagadas    = int(data.get("facturas_pagadas", 0))
        facturas_pendientes = int(data.get("facturas_pendientes", 0))
        valor_pendiente     = float(data.get("valor_pendiente", 0))

        def fmt(n: float) -> str:
            return f"${int(round(n)):,}".replace(",", ".")

        color_iva = "#f59e0b" if iva_neto > 0 else "#22c55e"

        html = f"""
        <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#f4f4f5;padding:32px;border-radius:12px;">
          <div style="background:#C41E2C;padding:20px 24px;border-radius:8px;margin-bottom:24px;">
            <h1 style="color:white;margin:0;font-size:20px;">Resumen Contable {mes}</h1>
            <p style="color:rgba(255,255,255,0.7);margin:6px 0 0;font-size:13px;">Punto Rojo</p>
          </div>

          <div style="background:white;border-radius:8px;padding:20px;margin-bottom:16px;">
            <p style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin:0 0 14px;font-weight:600;">Ventas del mes</p>
            <table width="100%" cellpadding="6" style="font-size:14px;border-collapse:collapse;">
              <tr><td style="color:#444;">Total ventas</td><td align="right" style="font-weight:bold;">{fmt(ventas)}</td></tr>
              <tr><td style="color:#444;">IVA generado (19%)</td><td align="right" style="color:#22c55e;font-weight:bold;">{fmt(iva_ventas)}</td></tr>
            </table>
          </div>

          <div style="background:white;border-radius:8px;padding:20px;margin-bottom:16px;">
            <p style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin:0 0 14px;font-weight:600;">Compras del mes</p>
            <table width="100%" cellpadding="6" style="font-size:14px;border-collapse:collapse;">
              <tr><td style="color:#444;">Total compras (subtotal)</td><td align="right" style="font-weight:bold;">{fmt(total_compras)}</td></tr>
              <tr><td style="color:#444;">IVA descontable</td><td align="right" style="color:#ef4444;">{fmt(total_iva_compras)}</td></tr>
              <tr><td style="color:#444;">Retefuente</td><td align="right" style="color:#ef4444;">{fmt(total_rete)}</td></tr>
            </table>
          </div>

          <div style="background:white;border-radius:8px;padding:20px;margin-bottom:16px;">
            <p style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin:0 0 14px;font-weight:600;">Arriendos y otros gastos</p>
            <table width="100%" cellpadding="6" style="font-size:14px;border-collapse:collapse;">
              <tr><td style="color:#444;">Total gastos</td><td align="right" style="font-weight:bold;">{fmt(total_arriendos)}</td></tr>
              <tr><td style="color:#444;">IVA gastos</td><td align="right" style="color:#ef4444;">{fmt(iva_arriendos)}</td></tr>
            </table>
          </div>

          <div style="background:white;border-radius:8px;padding:20px;margin-bottom:16px;border-left:4px solid {color_iva};">
            <p style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin:0 0 14px;font-weight:600;">Resumen IVA</p>
            <table width="100%" cellpadding="6" style="font-size:14px;border-collapse:collapse;">
              <tr><td style="color:#444;">IVA ventas</td><td align="right">{fmt(iva_ventas)}</td></tr>
              <tr><td style="color:#444;">IVA compras</td><td align="right">- {fmt(total_iva_compras)}</td></tr>
              <tr><td style="color:#444;">IVA gastos</td><td align="right">- {fmt(iva_arriendos)}</td></tr>
              <tr style="border-top:1px solid #eee;">
                <td style="color:#111;font-weight:bold;padding-top:10px;">IVA neto a pagar</td>
                <td align="right" style="font-weight:bold;font-size:16px;color:{color_iva};padding-top:10px;">{fmt(iva_neto)}</td>
              </tr>
            </table>
          </div>

          <div style="background:white;border-radius:8px;padding:20px;margin-bottom:24px;">
            <p style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin:0 0 14px;font-weight:600;">Estado de pagos</p>
            <table width="100%" cellpadding="6" style="font-size:14px;border-collapse:collapse;">
              <tr><td style="color:#444;">Facturas pagadas</td><td align="right" style="color:#22c55e;font-weight:bold;">{facturas_pagadas}</td></tr>
              <tr><td style="color:#444;">Facturas pendientes</td><td align="right" style="color:#f59e0b;font-weight:bold;">{facturas_pendientes}</td></tr>
              <tr><td style="color:#444;">Valor pendiente</td><td align="right" style="color:#ef4444;font-weight:bold;">{fmt(valor_pendiente)}</td></tr>
            </table>
          </div>

          <p style="text-align:center;color:#aaa;font-size:11px;margin:0;">Generado desde Costos Punto Rojo</p>
        </div>
        """

        resend.api_key = os.getenv("RESEND_API_KEY", "")
        resend.Emails.send({
            "from":    "Punto Rojo <onboarding@resend.dev>",
            "to":      [correo],
            "subject": f"Resumen Contable {mes} - Punto Rojo",
            "html":    html,
        })

        return {"ok": True, "correo": correo}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Limpiar tablas ──────────────────────────────────────────────────────────

TABLAS_PERMITIDAS = [
    "historial_descuentos",
    "historial_costos",
    "facturas_revisadas",
    "reportes_revisados",
    "listas_precios_items",
    "listas_precios",
    "productos",
    "facturas_contables",
    "facturas",
]

class LimpiarRequest(BaseModel):
    tablas: list[str]

@app.delete("/limpiar-tablas")
async def limpiar_tablas(req: LimpiarRequest):
    for tabla in req.tablas:
        if tabla not in TABLAS_PERMITIDAS:
            raise HTTPException(status_code=400, detail=f"Tabla no permitida: {tabla}")
    try:
        sb = get_supabase()
        for tabla in req.tablas:
            sb.table(tabla).delete().neq("id", 0).execute()
        return {"ok": True, "tablas_borradas": req.tablas}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Reporte de costos por email ─────────────────────────────────────────────

@app.post("/enviar-reporte-costos")
async def enviar_reporte_costos(data: dict):
    try:
        sb  = get_supabase()
        fecha = data.get("fecha", "")
        if not fecha:
            raise HTTPException(status_code=400, detail="Se requiere una fecha")

        # Obtener correo configurado
        cfg    = sb.table("config_admin").select("valor").eq("clave", "correo_reporte").single().execute()
        correo = cfg.data.get("valor", "") if cfg.data else ""
        if not correo:
            raise HTTPException(status_code=400, detail="No hay correo configurado en Admin")

        # Obtener historial del día (solo NUEVO, SUBIO, BAJO)
        desde = f"{fecha}T00:00:00"
        hasta = f"{fecha}T23:59:59"
        hist_res = sb.table("historial_costos").select(
            "producto_id,estado,costo_unidad_anterior,costo_unidad_nuevo,variacion_porcentaje,factura_id"
        ).gte("creada_en", desde).lte("creada_en", hasta).in_("estado", ["NUEVO", "SUBIO", "BAJO"]).order("estado").execute()

        hist = hist_res.data or []
        if not hist:
            raise HTTPException(status_code=400, detail="No hay productos con cambios en esa fecha")

        # Obtener productos y facturas relacionados
        prod_ids = list({h["producto_id"] for h in hist})
        fact_ids = list({h["factura_id"] for h in hist if h.get("factura_id")})

        prods_res = sb.table("productos_con_proveedor").select(
            "id,nombre_punto_rojo,sku_interno,categoria,unidades_por_paquete,paquetes_por_caja,unidades_por_caja,"
            "markup_unidad_pct,markup_paquete_pct,markup_caja_pct,"
            "costo_paquete_sin_iva,costo_caja_sin_iva,venta_unidad,venta_paquete,venta_caja,proveedor_nombre"
        ).in_("id", prod_ids).execute()

        facts_res = sb.table("facturas").select("id,numero_factura,proveedor").in_("id", fact_ids).execute() if fact_ids else type("R", (), {"data": []})()

        prods_map = {p["id"]: p for p in (prods_res.data or [])}
        facts_map = {f["id"]: f for f in (facts_res.data or [])}

        # Combinar datos
        items = []
        for h in hist:
            p = prods_map.get(h["producto_id"], {})
            f = facts_map.get(h.get("factura_id"), {})
            cu = h.get("costo_unidad_nuevo") or 0
            cp = p.get("costo_paquete_sin_iva") or cu * (p.get("unidades_por_paquete") or 1)
            cc = p.get("costo_caja_sin_iva") or cu * (p.get("unidades_por_caja") or 1)
            mu = p.get("markup_unidad_pct") or 40
            mp = p.get("markup_paquete_pct") or 35
            mc = p.get("markup_caja_pct") or 30
            items.append({
                "estado":      h.get("estado"),
                "nombre":      p.get("nombre_punto_rojo") or "—",
                "sku":         p.get("sku_interno") or "—",
                "categoria":   p.get("categoria") or "—",
                "proveedor":   p.get("proveedor_nombre") or f.get("proveedor") or "—",
                "factura":     f.get("numero_factura") or "—",
                "costo_ant":   h.get("costo_unidad_anterior") or 0,
                "costo_nuevo": cu,
                "variacion":   h.get("variacion_porcentaje") or 0,
                "venta_unidad":  p.get("venta_unidad", True),
                "venta_paquete": p.get("venta_paquete", False),
                "venta_caja":    p.get("venta_caja", False),
                "up": p.get("unidades_por_paquete") or 1,
                "uc": p.get("unidades_por_caja") or 1,
                "precio_unidad":  round((cu * 1.19) / (1 - mu / 100)) if cu and mu < 100 else 0,
                "precio_paquete": round((cp * 1.19) / (1 - mp / 100)) if cp and mp < 100 else 0,
                "precio_caja":    round((cc * 1.19) / (1 - mc / 100)) if cc and mc < 100 else 0,
                "mu": mu, "mp": mp, "mc": mc,
            })

        def fmt(n): return f"${int(round(n)):,}".replace(",", ".")
        def fecha_bonita(f): 
            try:
                from datetime import datetime
                return datetime.strptime(f, "%Y-%m-%d").strftime("%d de %B de %Y").replace(
                    "January","enero").replace("February","febrero").replace("March","marzo").replace(
                    "April","abril").replace("May","mayo").replace("June","junio").replace(
                    "July","julio").replace("August","agosto").replace("September","septiembre").replace(
                    "October","octubre").replace("November","noviembre").replace("December","diciembre")
            except: return f

        def estado_color(estado):
            return {"NUEVO": "#C41E2C", "SUBIO": "#ef4444", "BAJO": "#22c55e"}.get(estado, "#888")

        def estado_label(estado):
            return {"NUEVO": "NUEVO", "SUBIO": "↑ SUBIÓ", "BAJO": "↓ BAJÓ"}.get(estado, estado)

        def render_precios(item):
            precios = []
            if item["venta_unidad"]:
                precios.append(f'<td style="padding:4px 8px;"><span style="font-size:10px;color:#888;">Unidad</span><br><strong style="font-size:13px;">{fmt(item["precio_unidad"])}</strong></td>')
            if item["venta_paquete"]:
                precios.append(f'<td style="padding:4px 8px;"><span style="font-size:10px;color:#888;">Paq x{item["up"]}</span><br><strong style="font-size:13px;">{fmt(item["precio_paquete"])}</strong></td>')
            if item["venta_caja"]:
                precios.append(f'<td style="padding:4px 8px;"><span style="font-size:10px;color:#888;">Caja x{item["uc"]}</span><br><strong style="font-size:13px;">{fmt(item["precio_caja"])}</strong></td>')
            return "".join(precios)

        # Agrupar por estado
        nuevos   = [i for i in items if i["estado"] == "NUEVO"]
        subieron = [i for i in items if i["estado"] == "SUBIO"]
        bajaron  = [i for i in items if i["estado"] == "BAJO"]

        def render_grupo(titulo, color, grupo):
            if not grupo: return ""
            filas = ""
            for item in grupo:
                variacion_html = ""
                if item["variacion"] != 0:
                    arrow = "↑" if item["variacion"] > 0 else "↓"
                    col   = "#ef4444" if item["variacion"] > 0 else "#22c55e"
                    variacion_html = f'<span style="color:{col};font-weight:bold;margin-left:6px;">{arrow}{abs(item["variacion"])}%</span>'

                costo_ant_html = ""
                if item["costo_ant"] > 0:
                    costo_ant_html = f'<span style="color:#aaa;font-size:11px;text-decoration:line-through;margin-right:6px;">{fmt(item["costo_ant"])}</span>'

                filas += f"""
                <tr style="border-bottom:1px solid #f0f0f0;">
                  <td style="padding:10px 12px;vertical-align:top;">
                    <strong style="font-size:13px;color:#111;">{item["nombre"]}</strong><br>
                    <span style="font-size:11px;color:#888;">{item["categoria"]} · {item["proveedor"]}</span><br>
                    <span style="font-size:10px;color:#bbb;">Factura: {item["factura"]}</span>
                  </td>
                  <td style="padding:10px 12px;vertical-align:top;white-space:nowrap;">
                    {costo_ant_html}
                    <strong style="font-size:13px;color:#111;">{fmt(item["costo_nuevo"])}</strong>
                    {variacion_html}
                  </td>
                  <td style="padding:10px 12px;vertical-align:top;">
                    <table cellpadding="0" cellspacing="0"><tr>{render_precios(item)}</tr></table>
                  </td>
                </tr>"""

            return f"""
            <div style="margin-bottom:20px;">
              <div style="background:{color};padding:10px 16px;border-radius:8px 8px 0 0;">
                <strong style="color:white;font-size:13px;">{titulo} ({len(grupo)})</strong>
              </div>
              <table width="100%" cellpadding="0" cellspacing="0" style="background:white;border-radius:0 0 8px 8px;overflow:hidden;border:1px solid #eee;border-top:none;">
                <thead>
                  <tr style="background:#f9f9f9;border-bottom:1px solid #eee;">
                    <th style="padding:8px 12px;text-align:left;font-size:11px;color:#888;font-weight:600;text-transform:uppercase;">Producto</th>
                    <th style="padding:8px 12px;text-align:left;font-size:11px;color:#888;font-weight:600;text-transform:uppercase;">Costo</th>
                    <th style="padding:8px 12px;text-align:left;font-size:11px;color:#888;font-weight:600;text-transform:uppercase;">Precios de venta</th>
                  </tr>
                </thead>
                <tbody>{filas}</tbody>
              </table>
            </div>"""

        resumen_html = f"""
        <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap;">
          {"".join(f'<div style="flex:1;min-width:80px;background:white;border-radius:8px;padding:14px;text-align:center;border-top:3px solid {estado_color(e)};"><strong style="font-size:22px;color:{estado_color(e)};">{c}</strong><br><span style="font-size:11px;color:#888;">{l}</span></div>'
            for e, c, l in [("NUEVO", len(nuevos), "Nuevos"), ("SUBIO", len(subieron), "Subieron"), ("BAJO", len(bajaron), "Bajaron")])}
        </div>"""

        html = f"""
        <div style="font-family:sans-serif;max-width:680px;margin:0 auto;background:#f4f4f5;padding:32px;border-radius:12px;">
          <div style="background:#C41E2C;padding:20px 24px;border-radius:8px;margin-bottom:24px;">
            <h1 style="color:white;margin:0;font-size:20px;">Reporte de Costos</h1>
            <p style="color:rgba(255,255,255,0.75);margin:6px 0 0;font-size:13px;">{fecha_bonita(fecha)} · Punto Rojo</p>
          </div>
          {resumen_html}
          {render_grupo("🆕 Productos nuevos",   "#C41E2C", nuevos)}
          {render_grupo("📈 Subieron de precio", "#ef4444", subieron)}
          {render_grupo("📉 Bajaron de precio",  "#22c55e", bajaron)}
          <p style="text-align:center;color:#aaa;font-size:11px;margin:0;">Generado desde Costos Punto Rojo</p>
        </div>"""

        resend.api_key = os.getenv("RESEND_API_KEY", "")
        resend.Emails.send({
            "from":    "Punto Rojo <onboarding@resend.dev>",
            "to":      [correo],
            "subject": f"Reporte de Costos {fecha_bonita(fecha)} - Punto Rojo",
            "html":    html,
        })

        return {"ok": True, "correo": correo, "total": len(items)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
