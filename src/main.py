# src/main.py

import os
import json
import io
import re
import asyncio
import fitz  # PyMuPDF para comprimir PDFs
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response, Depends, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse 
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from docx import Document
from fpdf import FPDF
from datetime import datetime, timedelta, timezone
from google.cloud.firestore import AsyncClient, SERVER_TIMESTAMP, Query
from google.cloud import firestore # Para tipos como Increment
from google.cloud import storage # Para generar URLs firmadas y leer PDFs
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

# --- CONFIGURACIÓN VERTEX AI Y STORAGE ---
try:
    raw_credentials, project_id_default = google.auth.default()
    PROJECT_ID = os.getenv("PROJECT_ID", project_id_default)
    
    from google.auth.compute_engine.credentials import Credentials as ComputeEngineCredentials
    from google.auth import impersonated_credentials
    
    if isinstance(raw_credentials, ComputeEngineCredentials):
        print("Entorno Cloud Run detectado. Impersonando cuenta para firmar URLs...")
        credentials = impersonated_credentials.Credentials(
            source_credentials=raw_credentials,
            target_principal="analize-v20@pida-ai-v20.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=3600
        )
    else:
        credentials = raw_credentials

except Exception as e:
    print(f"Error configurando credenciales: {e}")
    PROJECT_ID = os.getenv("PROJECT_ID")
    credentials = None

LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-pro").strip()
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "pida-ai-temp-docs")

if PROJECT_ID:
    try:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        storage_client = storage.Client(project=PROJECT_ID, credentials=credentials) 
        print(f"Vertex AI y Storage inicializados: {PROJECT_ID}")
    except Exception as e:
        print(f"Error inicializando GCP: {e}")

# Inicializar Firestore
db = AsyncClient(project=PROJECT_ID)

# --- LÍMITES CONFIGURABLES ---
DAILY_LIMIT_BASICO = int(os.getenv("DAILY_LIMIT_BASICO", "3"))
DOCS_LIMIT_BASICO = int(os.getenv("DOCS_LIMIT_BASICO", "1"))

DAILY_LIMIT_AVANZADO = int(os.getenv("DAILY_LIMIT_AVANZADO", "15"))
DOCS_LIMIT_AVANZADO = int(os.getenv("DOCS_LIMIT_AVANZADO", "3"))

DAILY_LIMIT_PREMIUM = int(os.getenv("DAILY_LIMIT_PREMIUM", "25"))
DOCS_LIMIT_PREMIUM = int(os.getenv("DOCS_LIMIT_PREMIUM", "5"))

app = FastAPI(title="PIDA Document Analyzer (Streaming & GCS)")

LIMIT_BASICO_ANALYSIS_DAILY = int(os.getenv("LIMIT_BASICO_ANALYSIS_DAILY", 3))
LIMIT_AVANZADO_ANALYSIS_DAILY = int(os.getenv("LIMIT_AVANZADO_ANALYSIS_DAILY", 15))
LIMIT_PREMIUM_ANALYSIS_DAILY = int(os.getenv("LIMIT_PREMIUM_ANALYSIS_DAILY", 25))

LIMIT_BASICO_DOCS = int(os.getenv("LIMIT_BASICO_DOCS", 1))
LIMIT_AVANZADO_DOCS = int(os.getenv("LIMIT_AVANZADO_DOCS", 3))
LIMIT_PREMIUM_DOCS = int(os.getenv("LIMIT_PREMIUM_DOCS", 5))

# --- CONFIGURACIÓN DE SEGURIDAD DE ARCHIVOS ---
try:
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
except ValueError:
    MAX_FILE_SIZE_MB = 50

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

ANALYSIS_LIMITS = {
    "basico": LIMIT_BASICO_ANALYSIS_DAILY,
    "avanzado": LIMIT_AVANZADO_ANALYSIS_DAILY,
    "premium": LIMIT_PREMIUM_ANALYSIS_DAILY,
    "vip": -1 
}

DOCS_LIMITS = {
    "basico": LIMIT_BASICO_DOCS,
    "avanzado": LIMIT_AVANZADO_DOCS,
    "premium": LIMIT_PREMIUM_DOCS,
    "vip": 100 
}

# --- FUNCIONES DE UTILIDAD Y CONTROL ---

def get_date_utc_minus_6() -> str:
    utc_now = datetime.now(timezone.utc)
    cst_now = utc_now - timedelta(hours=6)
    return cst_now.strftime('%Y-%m-%d')

async def get_user_plan_unified(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get('uid')
    user_email = current_user.get('email', '').strip().lower()
    email_verified = current_user.get('email_verified', False)
    
    try:
        raw_domains = os.getenv("ADMIN_DOMAINS", '[]')
        raw_emails = os.getenv("ADMIN_EMAILS", '[]')
        admin_domains = [str(d).strip().lower() for d in json.loads(raw_domains)]
        admin_emails = [str(e).strip().lower() for e in json.loads(raw_emails)]
    except:
        admin_domains, admin_emails = [], []

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""
    
    if email_verified and ((email_domain in admin_domains) or (user_email in admin_emails)):
        return 'vip'

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
        
    return 'none'

async def consume_analysis_credit(user_id: str, plan_key: str):
    limit_daily = ANALYSIS_LIMITS.get(plan_key, 0)
    if limit_daily == -1: return 

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

def generate_filename(instructions: str, extension: str) -> str:
    safe_title = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ]', '', instructions[:40])
    safe_title = safe_title.strip().replace(' ', '_')
    if not safe_title:
        safe_title = "Analisis_PIDA"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return f"{safe_title}_{timestamp}.{extension}"

def sanitize_text_for_pdf(text: str) -> str:
    if not text: return ""
    replacements = {
        "•": "-", "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
        "\u2013": "-", "\u2014": "-", "\u2022": "-", "\uF0B7": "-"
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text.encode('latin1', 'replace').decode('latin-1')

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

def read_docx_sync(content: bytes) -> str:
    try:
        doc = Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs])
    except: return ""

def download_and_parse_docx(gs_uri: str) -> str:
    try:
        blob_path = "/".join(gs_uri.replace("gs://", "").split("/")[1:])
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        content = bucket.blob(blob_path).download_as_bytes()
        return read_docx_sync(content)
    except Exception as e:
        print(f"Error procesando DOCX desde GCS: {e}")
        return ""

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
        
        final_history = []
        if history_json:
            try:
                final_history = json.loads(history_json)
            except:
                pass
        
        final_history.append({"role": "model", "content": full_text})
        final_id = analysis_id
        
        if final_id:
            doc_ref = db.collection("analysis_history").document(final_id)
            doc = await doc_ref.get()
            if doc.exists:
                await doc_ref.update({
                    "analysis": json.dumps(final_history),
                    "timestamp": SERVER_TIMESTAMP
                })
            else:
                final_id = "" 
                
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


# =========================================================================
# ENDPOINT DE COMPRESIÓN DE PDF (PyMuPDF)
# CUIDADO: Fíjate que el decorador NO tiene barra al final
# =========================================================================
@app.post("/compress-and-upload")
async def compress_and_upload(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)
    if plan == 'none': 
        raise HTTPException(status_code=403, detail="Plan inactivo.")

    file_bytes = await file.read()
    original_size = len(file_bytes) / (1024 * 1024)

    try:
        def compress_pdf(data: bytes) -> bytes:
            if file.content_type != "application/pdf":
                return data
            
            doc = fitz.open(stream=data, filetype="pdf")
            return doc.tobytes(garbage=4, deflate=True)

        compressed_bytes = await asyncio.to_thread(compress_pdf, file_bytes)
        new_size = len(compressed_bytes) / (1024 * 1024)

        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '', file.filename.replace(' ', '_'))
        blob_path = f"uploads/{user_id}/{int(datetime.now().timestamp())}_opt_{safe_name}"
        blob = bucket.blob(blob_path)
        
        await asyncio.to_thread(blob.upload_from_string, compressed_bytes, content_type=file.content_type)

        return {
            "filename": file.filename,
            "gs_uri": f"gs://{GCS_BUCKET_NAME}/{blob_path}",
            "mime_type": file.content_type,
            "original_size_mb": original_size,
            "new_size_mb": new_size
        }
        
    except Exception as e:
        print(f"Error comprimiendo el archivo {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Error optimizando el documento: {str(e)}")


@app.post("/generate-upload-urls")
async def generate_upload_urls(
    payload: Dict[str, Any] = Body(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)
    if plan == 'none': 
        raise HTTPException(status_code=403, detail="Plan inactivo.")

    files_req = payload.get("files", [])
    max_docs = DOCS_LIMITS.get(plan, 0)
    if max_docs != -1 and len(files_req) > max_docs:
         raise HTTPException(status_code=403, detail=f"Tu plan permite subir {max_docs} documento(s) a la vez.")

    urls_response = []
    bucket = storage_client.bucket(GCS_BUCKET_NAME)

    for f in files_req:
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '', f.get("name", "doc").replace(' ', '_'))
        blob_path = f"uploads/{user_id}/{int(datetime.now().timestamp())}_{safe_name}"
        blob = bucket.blob(blob_path)
        
        try:
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=15),
                method="PUT",
                content_type=f.get("type", "application/pdf")
            )
            urls_response.append({
                "filename": f.get("name"),
                "upload_url": signed_url,
                "gs_uri": f"gs://{GCS_BUCKET_NAME}/{blob_path}",
                "mime_type": f.get("type", "application/pdf")
            })
        except Exception as e:
            raise HTTPException(500, f"Error generando URL segura. Verifica IAM: {e}")
            
    return {"urls": urls_response}


@app.post("/analyze")
async def analyze_documents(
    files_data: str = Form(...), 
    instructions: str = Form(...),
    analysis_id: str = Form(""),
    history_json: str = Form(""),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)
    
    if plan == 'none':
        raise HTTPException(status_code=403, detail="No tienes un plan activo para realizar análisis.")

    try:
        files_info = json.loads(files_data)
    except:
        raise HTTPException(400, "Formato de metadatos de archivos inválido.")

    num_files = len(files_info)
    max_docs = DOCS_LIMITS.get(plan, 0)
    if max_docs != -1 and num_files > max_docs:
        raise HTTPException(status_code=403, detail=f"Tu plan {plan.capitalize()} solo permite analizar {max_docs} documento(s) a la vez.")

    await consume_analysis_credit(user_id, plan)

    model_parts = []
    original_filenames = []
    bucket = storage_client.bucket(GCS_BUCKET_NAME)

    for f_info in files_info:
        gs_uri = f_info.get("gs_uri")
        mime_type = f_info.get("mime_type", "application/pdf")
        original_filename = f_info.get("filename", "documento")
        
        try:
            blob_path = "/".join(gs_uri.replace("gs://", "").split("/")[1:])
            blob = bucket.get_blob(blob_path)
            
            if blob:
                file_size_mb = blob.size / (1024 * 1024)
                if file_size_mb > MAX_FILE_SIZE_MB:
                    blob.delete()
                    raise HTTPException(
                        status_code=400, 
                        detail=f"El archivo '{original_filename}' pesa {file_size_mb:.2f} MB, superando el límite de {MAX_FILE_SIZE_MB} MB."
                    )
        except HTTPException as he:
            raise he
        except Exception as e:
            print(f"Error verificando tamaño en GCS: {e}")

        original_filenames.append(original_filename)
        
        if mime_type == "application/pdf":
            model_parts.append(Part.from_uri(uri=gs_uri, mime_type="application/pdf"))
        else:
            text = await asyncio.to_thread(download_and_parse_docx, gs_uri)
            model_parts.append(f"--- DOC: {original_filename} ---\n{text}\n------\n")

    model_parts.append(f"\nINSTRUCCIONES E HISTORIAL:\n{instructions}")
    model = GenerativeModel(model_name=GEMINI_MODEL_NAME, system_instruction=ANALYZER_SYSTEM_PROMPT)
    
    safety_settings = [
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    ]

    gen_config = {
        "temperature": float(os.getenv("GEMINI_TEMP", "0.4")),
        "top_p": float(os.getenv("GEMINI_TOP_P", "0.95")),
        "max_output_tokens": 65535
    }

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

    if len(analysis_text) > 500000:
        analysis_text = analysis_text[:500000] + "\n\n[Texto truncado por límite de seguridad]"
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
    return {"status": "ok", "msg": "API Analizador v2.1 (GCS Storage Integrado + Compresión PyMuPDF)"}
