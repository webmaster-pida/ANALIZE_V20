# src/main.py

import os
import json
import io
import re
import asyncio
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse 
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from docx import Document
from fpdf import FPDF
from datetime import datetime, timedelta, timezone
from google.cloud.firestore import AsyncClient, SERVER_TIMESTAMP, Query
from google.cloud import firestore # Para tipos como Increment
import google.auth
import vertexai
from vertexai.generative_models import (
    GenerativeModel, 
    Part, 
    SafetySetting, 
    HarmCategory, 
    HarmBlockThreshold
)
from src.core.security import get_current_user
from src.core.prompts import ANALYZER_SYSTEM_PROMPT

# Cargar variables
load_dotenv()

# --- CONFIGURACIÓN VERTEX AI ---
try:
    _, project_id_default = google.auth.default()
    PROJECT_ID = os.getenv("PROJECT_ID", project_id_default)
except:
    PROJECT_ID = os.getenv("PROJECT_ID")

LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

if PROJECT_ID:
    try:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        print(f"Vertex AI inicializado: {PROJECT_ID}")
    except Exception as e:
        print(f"Error Vertex AI: {e}")

# Inicializar Firestore
db = AsyncClient(project=PROJECT_ID)

# --- LÍMITES CONFIGURABLES (IGUAL QUE EN CHATv20) ---
DAILY_LIMIT_BASICO = int(os.getenv("DAILY_LIMIT_BASICO", "3"))
DOCS_LIMIT_BASICO = int(os.getenv("DOCS_LIMIT_BASICO", "1"))

DAILY_LIMIT_AVANZADO = int(os.getenv("DAILY_LIMIT_AVANZADO", "15"))
DOCS_LIMIT_AVANZADO = int(os.getenv("DOCS_LIMIT_AVANZADO", "3"))

DAILY_LIMIT_PREMIUM = int(os.getenv("DAILY_LIMIT_PREMIUM", "25"))
DOCS_LIMIT_PREMIUM = int(os.getenv("DOCS_LIMIT_PREMIUM", "5"))

app = FastAPI(title="PIDA Document Analyzer (Streaming)")

# --- VARIABLES DE LÍMITES (Desde Cloud Run) ---
# Valores por defecto de seguridad
LIMIT_BASICO_ANALYSIS_DAILY = int(os.getenv("LIMIT_BASICO_ANALYSIS_DAILY", 3))
LIMIT_AVANZADO_ANALYSIS_DAILY = int(os.getenv("LIMIT_AVANZADO_ANALYSIS_DAILY", 15))
LIMIT_PREMIUM_ANALYSIS_DAILY = int(os.getenv("LIMIT_PREMIUM_ANALYSIS_DAILY", 25))

LIMIT_BASICO_DOCS = int(os.getenv("LIMIT_BASICO_DOCS", 1))
LIMIT_AVANZADO_DOCS = int(os.getenv("LIMIT_AVANZADO_DOCS", 3))
LIMIT_PREMIUM_DOCS = int(os.getenv("LIMIT_PREMIUM_DOCS", 5))

# --- CONFIGURACIÓN DE SEGURIDAD DE ARCHIVOS ---
try:
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
except ValueError:
    MAX_FILE_SIZE_MB = 10

# --- CORS ---
raw_origins = os.getenv("ALLOWED_ORIGINS", '["https://pida-ai.com"]')
try:
    origins = json.loads(raw_origins)
except:
    origins = ["https://pida-ai.com"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://pida-ai-v20--.*\.web\.app$|https://.*\.app\.github\.dev$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# --- MAPAS DE LÍMITES ---
ANALYSIS_LIMITS = {
    "basico": LIMIT_BASICO_ANALYSIS_DAILY,
    "avanzado": LIMIT_AVANZADO_ANALYSIS_DAILY,
    "premium": LIMIT_PREMIUM_ANALYSIS_DAILY,
    "vip": -1  # Ilimitado
}

DOCS_LIMITS = {
    "basico": LIMIT_BASICO_DOCS,
    "avanzado": LIMIT_AVANZADO_DOCS,
    "premium": LIMIT_PREMIUM_DOCS,
    "vip": 100 
}

# --- FUNCIONES DE UTILIDAD Y CONTROL ---

def get_date_utc_minus_6() -> str:
    """Devuelve la fecha actual ajustada a UTC-6"""
    utc_now = datetime.now(timezone.utc)
    cst_now = utc_now - timedelta(hours=6)
    return cst_now.strftime('%Y-%m-%d')

async def get_user_plan_unified(current_user: Dict[str, Any]) -> str:
    """
    Determina el plan del usuario unificando lógica VIP y DB.
    Retorna: 'vip', 'basico', 'avanzado', 'premium' o 'none'.
    """
    user_id = current_user.get('uid')
    user_email = current_user.get('email', '').strip().lower()
    email_verified = current_user.get('email_verified', False)
    
    # 1. VERIFICACIÓN VIP (Variables de Entorno)
    try:
        raw_domains = os.getenv("ADMIN_DOMAINS", '[]')
        raw_emails = os.getenv("ADMIN_EMAILS", '[]')
        admin_domains = [str(d).strip().lower() for d in json.loads(raw_domains)]
        admin_emails = [str(e).strip().lower() for e in json.loads(raw_emails)]
    except:
        admin_domains, admin_emails = [], []

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""
    
    # 🛡️ PROTECCIÓN: Exigir email_verified
    if email_verified and ((email_domain in admin_domains) or (user_email in admin_emails)):
        return 'vip'

    # 2. VERIFICACIÓN FIRESTORE (Documento de Cliente)
    try:
        cust_doc = await db.collection('customers').document(user_id).get()
        if cust_doc.exists:
            data = cust_doc.to_dict()
            status = data.get('status')
            if status in ['active', 'trialing']:
                plan = data.get('plan', 'basico')
                return plan.lower() if plan else 'basico'
    except Exception as e:
        print(f"Error consultando plan en DB: {e}")
        
    return 'none' # Sin acceso

# 🛡️ NUEVAS FUNCIONES DE CONSUMO ATÓMICO Y REEMBOLSO (Mitigación DoS y ByPass)
async def consume_analysis_credit(user_id: str, plan_key: str):
    limit_daily = ANALYSIS_LIMITS.get(plan_key, 0)
    if limit_daily == -1: return # VIP Ilimitado

    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    @firestore.async_transactional
    async def check_and_increment(transaction, ref):
        snapshot = await ref.get(transaction=transaction)
        current_count = snapshot.get('analysis_count') if snapshot.exists else 0
        
        if current_count >= limit_daily:
            raise HTTPException(status_code=429, detail=f"Límite diario alcanzado para el plan {plan_key}")
        
        transaction.set(ref, {
            'analysis_count': current_count + 1,
            'last_updated': firestore.SERVER_TIMESTAMP
        }, merge=True)

    transaction = db.transaction()
    await check_and_increment(transaction, stats_ref)

async def refund_analysis_credit(user_id: str):
    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    @firestore.async_transactional
    async def check_and_decrement(transaction, ref):
        snapshot = await ref.get(transaction=transaction)
        if snapshot.exists:
            current_count = snapshot.get('analysis_count', 0)
            if current_count > 0:
                transaction.update(ref, {
                    'analysis_count': current_count - 1,
                    'last_updated': firestore.SERVER_TIMESTAMP
                })
    try:
        transaction = db.transaction()
        await check_and_decrement(transaction, stats_ref)
    except Exception as e:
        print(f"Error en reembolso de análisis: {e}")

# --- UTILIDADES DE NOMBRE DE ARCHIVO ---
def generate_filename(instructions: str, extension: str) -> str:
    safe_title = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ]', '', instructions[:40])
    safe_title = safe_title.strip().replace(' ', '_')
    if not safe_title:
        safe_title = "Analisis_PIDA"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return f"{safe_title}_{timestamp}.{extension}"

# --- UTILIDADES DE LIMPIEZA TEXTO ---
def sanitize_text_for_pdf(text: str) -> str:
    if not text: return ""
    replacements = {
        "•": "-", "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
        "\u2013": "-", "\u2014": "-", "\u2022": "-", "\uF0B7": "-"
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text.encode('latin1', 'replace').decode('latin-1')

# --- PARSER DE MARKDOWN PARA PDF ---
def write_markdown_to_pdf(pdf, text):
    pdf.set_font("Arial", "", 11)
    
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            pdf.ln(5)
            continue
            
        if line.startswith('## '):
            pdf.ln(3)
            pdf.set_font("Arial", "B", 13)
            pdf.set_text_color(29, 53, 87)
            pdf.multi_cell(0, 8, line.replace('## ', ''))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Arial", "", 11)
        elif line.startswith('# '):
            pdf.ln(5)
            pdf.set_font("Arial", "B", 15)
            pdf.set_text_color(185, 47, 50)
            pdf.multi_cell(0, 10, line.replace('# ', ''))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Arial", "", 11)
            
        elif line.startswith('* ') or line.startswith('- '):
            pdf.set_x(15)
            clean_line = line[2:]
            pdf.write(6, "- ")
            parts = re.split(r'(\*\*.*?\*\*)', clean_line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    pdf.set_font("Arial", "B", 11)
                    pdf.write(6, part.strip('*'))
                    pdf.set_font("Arial", "", 11)
                else:
                    pdf.write(6, part)
            pdf.ln(6)

        else:
            parts = re.split(r'(\*\*.*?\*\*)', line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    pdf.set_font("Arial", "B", 11)
                    pdf.write(6, part.strip('*'))
                    pdf.set_font("Arial", "", 11)
                else:
                    pdf.write(6, part)
            pdf.ln(6)

def parse_and_add_markdown_to_docx(document, markdown_text):
    for line in markdown_text.strip().split('\n'):
        if line.startswith('## '):
            document.add_heading(line.lstrip('## '), level=2)
        elif line.startswith('# '):
            document.add_heading(line.lstrip('# '), level=1)
        elif not line.strip():
            document.add_paragraph('')
        else:
            p = document.add_paragraph()
            parts = re.split(r'(\*\*.*?\*\*)', line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    p.add_run(part.strip('*')).bold = True
                else:
                    p.add_run(part)

# --- CLASE PDF ---
class PDF(FPDF):
    def header(self):
        self.set_font("Arial", "B", 14)
        self.set_text_color(29, 53, 87)
        self.cell(0, 10, "PIDA-AI: Resumen de Consulta", 0, 1, "L")
        self.set_font("Arial", "", 9)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}", 0, 1, "L")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", 0, 0, "C")

# --- FUNCIONES ASÍNCRONAS (SYNC WRAPPERS) ---
def read_docx_sync(content: bytes) -> str:
    try:
        doc = Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs])
    except: return ""

def create_docx_sync(analysis_text: str, instructions: str) -> tuple[bytes, str, str]:
    stream = io.BytesIO()
    doc = Document()
    doc.add_heading("PIDA-AI: Resumen", 0)
    doc.add_paragraph(f"Fecha: {datetime.now().strftime('%d/%m/%Y')}")
    doc.add_heading("Instrucciones", 2)
    doc.add_paragraph(instructions)
    doc.add_heading("Analisis", 2)
    parse_and_add_markdown_to_docx(doc, analysis_text)
    doc.save(stream)
    stream.seek(0)
    fname = generate_filename(instructions, "docx")
    return stream.read(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", fname

def create_pdf_sync(analysis_text: str, instructions: str) -> tuple[bytes, str, str]:
    safe_inst = sanitize_text_for_pdf(instructions)
    safe_ana = sanitize_text_for_pdf(analysis_text)
    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Instrucciones", 0, 1)
    pdf.set_font("Arial", "", 11)
    pdf.multi_cell(0, 6, safe_inst)
    pdf.ln(5)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Analisis", 0, 1)
    if not safe_ana.strip():
        pdf.set_font("Arial", "I", 11)
        pdf.multi_cell(0, 6, "[Sin contenido]")
    else:
        write_markdown_to_pdf(pdf, safe_ana)
    try:
        pdf_string = pdf.output(dest='S')
        pdf_bytes = pdf_string.encode('latin-1', 'replace') if isinstance(pdf_string, str) else pdf_string
        stream = io.BytesIO(pdf_bytes)
        fname = generate_filename(instructions, "pdf")
        return stream.read(), "application/pdf", fname
    except Exception as e:
        print(f"Error PDF: {e}")
        err = FPDF()
        err.add_page()
        err.multi_cell(0, 10, f"Error: {str(e)}")
        return err.output(dest='S').encode('latin-1'), "application/pdf", "Error.pdf"


async def stream_analysis_generator(model, model_parts, gen_config, safety_settings, current_user, instructions, original_filenames, analysis_id, history_json):
    full_text = ""
    try:
        responses = await model.generate_content_async(
            model_parts, generation_config=gen_config, safety_settings=safety_settings, stream=True
        )
        async for chunk in responses:
            if chunk.text:
                full_text += chunk.text
                yield f"data: {json.dumps({'text': chunk.text})}\n\n"
        
        user_id = current_user.get("uid")
        
        # PROCESO DE HILO CONTINUO (THREADING)
        final_history = []
        if history_json:
            try:
                final_history = json.loads(history_json)
            except:
                pass
        
        final_history.append({"role": "model", "content": full_text})
        final_id = analysis_id
        
        # 1. Si existe un ID previo, actualizamos el documento concatenando el historial
        if final_id:
            doc_ref = db.collection("analysis_history").document(final_id)
            doc = await doc_ref.get()
            if doc.exists:
                await doc_ref.update({
                    "analysis": json.dumps(final_history),
                    "timestamp": SERVER_TIMESTAMP
                })
            else:
                final_id = "" # Si el doc fue borrado, creamos uno nuevo
                
        # 2. Si es un análisis nuevo, lo creamos
        if not final_id:
            title_source = instructions
            if final_history and len(final_history) > 0 and final_history[0].get("role") == "user":
                title_source = final_history[0].get("content", instructions)
            
            title = (title_source[:40] + '...') if len(title_source) > 40 else title_source
            doc_ref = db.collection("analysis_history").document()
            await doc_ref.set({
                "userId": user_id, 
                "title": title, 
                "instructions": title_source,
                "analysis": json.dumps(final_history) if final_history else full_text, 
                "timestamp": SERVER_TIMESTAMP, 
                "original_filenames": original_filenames
            })
            final_id = doc_ref.id
        
        yield f"data: {json.dumps({'done': True, 'analysis_id': final_id})}\n\n"
        
    except Exception as e:
        print(f"Error stream: {e}")
        yield f"data: {json.dumps({'error': str(e)})}\n\n"

# --- ENDPOINTS ---
@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    instructions: str = Form(...),
    analysis_id: str = Form(""),
    history_json: str = Form(""),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)
    
    if plan == 'none':
        raise HTTPException(status_code=403, detail="No tienes un plan activo para realizar análisis.")

    # Verificación de cantidad de archivos
    num_files = len(files)
    max_docs = DOCS_LIMITS.get(plan, 0)
    if max_docs != -1 and num_files > max_docs:
        raise HTTPException(status_code=403, detail=f"Tu plan {plan.capitalize()} solo permite analizar {max_docs} documento(s) a la vez.")

    # 🛡️ CONSUMO ATÓMICO PREVIO (Previene bypass de concurrencia)
    await consume_analysis_credit(user_id, plan)

    # 3. Procesar Archivos
    model_parts = []
    original_filenames = []

    for file in files:
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)
        
        if file_size > (MAX_FILE_SIZE_MB * 1024 * 1024):
            asyncio.create_task(refund_analysis_credit(user_id)) # Reembolsar si el archivo es grande
            raise HTTPException(400, f"El archivo {file.filename} excede el límite de {MAX_FILE_SIZE_MB}MB.")
        
        content = await file.read()
        is_pdf = content.startswith(b'%PDF')
        is_docx = content.startswith(b'PK\x03\x04')
        
        if not (is_pdf or is_docx):
             asyncio.create_task(refund_analysis_credit(user_id)) # Reembolsar si archivo inválido
             raise HTTPException(400, f"El archivo {file.filename} no es un PDF o DOCX válido.")
             
        original_filenames.append(file.filename)
        
        if is_pdf:
            model_parts.append(Part.from_data(data=content, mime_type="application/pdf"))
        else:
            text = await asyncio.to_thread(read_docx_sync, content)
            model_parts.append(f"--- DOC: {file.filename} ---\n{text}\n------\n")

    model_parts.append(f"\nINSTRUCCIONES E HISTORIAL:\n{instructions}")
    model = GenerativeModel(model_name=GEMINI_MODEL_NAME, system_instruction=ANALYZER_SYSTEM_PROMPT)
    
    safety_settings = [
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_NONE),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_NONE),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_NONE),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_NONE),
    ]

    gen_config = {
        "temperature": float(os.getenv("GEMINI_TEMP", "0.4")),
        "top_p": float(os.getenv("GEMINI_TOP_P", "0.95")),
        "max_output_tokens": 32696
    }

    # 🛡️ GENERADOR CONSTRUIDO CON PROTECCIÓN DE REEMBOLSO
    async def counted_stream_generator():
        has_error = False
        tokens_sent = False
        try:
            async for chunk in stream_analysis_generator(
                model, model_parts, gen_config, safety_settings, current_user, instructions, original_filenames, analysis_id, history_json
            ):
                if '"error":' in chunk:
                    has_error = True
                if '"text":' in chunk and not has_error:
                    tokens_sent = True
                yield chunk
        finally:
            if has_error or not tokens_sent:
                asyncio.create_task(refund_analysis_credit(user_id))

    return StreamingResponse(counted_stream_generator(), media_type="text/event-stream")

@app.post("/download-analysis")
async def download_analysis(
    analysis_text: str = Form(...),
    instructions: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    plan = await get_user_plan_unified(current_user)
    if plan == 'none': raise HTTPException(403, "Sin acceso")

    # 🛡️ PROTECCIÓN CONTRA INYECCIÓN DE TEXTO / DoS
    if len(analysis_text) > 50000:
        analysis_text = analysis_text[:50000] + "\n\n[Texto truncado por límite de seguridad]"
    if len(instructions) > 5000:
        instructions = instructions[:5000] + "..."

    try:
        if file_format.lower() == "docx":
            content, mime, fname = await asyncio.to_thread(create_docx_sync, analysis_text, instructions)
        else:
            content, mime, fname = await asyncio.to_thread(create_pdf_sync, analysis_text, instructions)
        return Response(content=content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        raise HTTPException(500, f"Error descarga: {e}")

@app.get("/analysis-history/")
async def get_analysis_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user['uid']
    
    plan = await get_user_plan_unified(current_user)
    if plan == 'none': raise HTTPException(403, "Requiere plan activo para ver historial")

    ref = db.collection("analysis_history").where("userId", "==", user_id).order_by("timestamp", direction=Query.DESCENDING)
    history = []
    async for d in ref.stream():
        history.append({"id": d.id, "title": d.get("title"), "timestamp": d.get("timestamp"), "userId": user_id})
    return history

@app.get("/analysis-history/{analysis_id}")
async def get_analysis_detail(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user['uid']
    
    plan = await get_user_plan_unified(current_user)
    if plan == 'none': raise HTTPException(403, "Requiere plan activo")

    doc = await db.collection("analysis_history").document(analysis_id).get()
    if not doc.exists: raise HTTPException(404)
    data = doc.to_dict()
    if data.get("userId") != user_id: raise HTTPException(403)
    return data

@app.delete("/analysis-history/{analysis_id}")
async def delete_analysis(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user['uid']
    doc_ref = db.collection("analysis_history").document(analysis_id)
    doc = await doc_ref.get()
    
    if not doc.exists: raise HTTPException(404)
    if doc.to_dict().get("userId") != user_id: raise HTTPException(403)
    
    await doc_ref.delete()
    return {"status": "ok"}

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "API Analizador v2.1 (Unified Plan Logic & Security Patched)"}
