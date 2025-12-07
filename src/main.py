# /src/main.py

import os
import json
import io
import re
import asyncio
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse 
from typing import List, Dict, Any
from dotenv import load_dotenv
from docx import Document
from fpdf import FPDF
from datetime import datetime
# SOLUCIÓN I/O BLOCKING: Usamos AsyncClient y tipos específicos de Firestore
from google.cloud.firestore import AsyncClient, SERVER_TIMESTAMP, Query
import google.auth
import vertexai
from vertexai.generative_models import GenerativeModel, Part, SafetySetting
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

# SOLUCIÓN I/O BLOCKING: Inicializar Firestore con AsyncClient
db = AsyncClient(project=PROJECT_ID)

app = FastAPI(title="PIDA Document Analyzer (Streaming)")

# --- CORS ---
raw_origins = os.getenv("ALLOWED_ORIGINS", '["https://pida-ai.com"]')
try:
    origins = json.loads(raw_origins)
except:
    origins = ["https://pida-ai.com"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# --- UTILIDADES DE LIMPIEZA (SOLUCIÓN PDF EN BLANCO) ---
def sanitize_text_for_pdf(text: str) -> str:
    """
    Limpia el texto para asegurar compatibilidad total con FPDF (Latin-1).
    Reemplaza caracteres problemáticos y elimina emojis que rompen el PDF.
    """
    if not text:
        return ""
    
    # Reemplazos manuales de caracteres comunes que rompen FPDF
    replacements = {
        "•": "-", "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
        "\u2013": "-", "\u2014": "-", "\u2022": "-", "\uF0B7": "-"
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    
    # Forzar codificación Latin-1 reemplazando errores con '?'
    return text.encode('latin1', 'replace').decode('latin-1')

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
            # Eliminar negritas de markdown simple para evitar errores de parseo complejos
            clean_line = line.replace('**', '')
            p.add_run(clean_line)

# --- CLASE PDF ROBUSTA ---
class PDF(FPDF):
    def header(self):
        # Usamos Arial nativa para evitar errores de rutas de fuentes
        self.set_font("Arial", "B", 15)
        self.set_text_color(29, 53, 87)
        self.cell(0, 10, "PIDA-AI: Resumen de Consulta", 0, 1, "L")
        self.set_font("Arial", "", 10)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}", 0, 1, "L")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", 0, 0, "C")

# --- GENERADORES SÍNCRONOS (SOLUCIÓN CPU BOUND) ---
# Estas funciones se ejecutan en un hilo separado para no bloquear el servidor

def read_docx_sync(content: bytes) -> str:
    """Lee DOCX sin bloquear el event loop principal."""
    try:
        doc = Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs])
    except:
        return ""

def create_docx_sync(analysis_text: str, instructions: str, timestamp: str) -> tuple[bytes, str, str]:
    """Crea DOCX en hilo separado."""
    stream = io.BytesIO()
    doc = Document()
    doc.add_heading("PIDA-AI: Resumen", 0)
    doc.add_paragraph(f"Fecha: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}")
    doc.add_heading("Instrucciones", 2)
    doc.add_paragraph(instructions)
    doc.add_heading("Analisis", 2)
    parse_and_add_markdown_to_docx(doc, analysis_text)
    doc.save(stream)
    stream.seek(0)
    return stream.read(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f"PIDA_{timestamp}.docx"

def create_pdf_sync(analysis_text: str, instructions: str, timestamp: str) -> tuple[bytes, str, str]:
    """Crea PDF en hilo separado con sanitización."""
    # 1. Sanitizar textos ANTES de crear el PDF (Crucial para evitar blanco)
    safe_inst = sanitize_text_for_pdf(instructions)
    safe_ana = sanitize_text_for_pdf(analysis_text)

    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    
    # Instrucciones
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Instrucciones", 0, 1)
    pdf.set_font("Arial", "", 11)
    pdf.multi_cell(0, 6, safe_inst)
    pdf.ln(5)
    
    # Análisis
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Analisis", 0, 1)
    pdf.set_font("Arial", "", 11)
    
    # Escribir contenido
    if not safe_ana.strip():
        pdf.multi_cell(0, 6, "[Sin contenido]")
    else:
        pdf.multi_cell(0, 6, safe_ana)
    
    # Generar salida
    try:
        # output(dest='S') devuelve string latin-1 en fpdf v1
        pdf_string = pdf.output(dest='S')
        # Codificar a bytes asegurando compatibilidad
        if isinstance(pdf_string, str):
            pdf_bytes = pdf_string.encode('latin-1', 'replace')
        else:
            pdf_bytes = pdf_string

        stream = io.BytesIO(pdf_bytes)
        return stream.read(), "application/pdf", f"PIDA_{timestamp}.pdf"
    except Exception as e:
        print(f"Error PDF Interno: {e}")
        # PDF de error de emergencia
        err = FPDF()
        err.add_page()
        err.set_font("Arial", "", 12)
        err.multi_cell(0, 10, f"Error generando PDF: {str(e)}")
        return err.output(dest='S').encode('latin-1', 'replace'), "application/pdf", "Error.pdf"

# --- ENDPOINTS ---
@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    instructions: str = Form(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    if len(files) > 3:
        raise HTTPException(status_code=400, detail="Máximo 3 archivos.")

    model_parts = []
    original_filenames = []

    for file in files:
        original_filenames.append(file.filename)
        content = await file.read()

        if file.content_type == "application/pdf":
            model_parts.append(Part.from_data(data=content, mime_type="application/pdf"))
        else:
            # SOLUCIÓN CPU BLOCKING: Usar asyncio.to_thread
            text = await asyncio.to_thread(read_docx_sync, content)
            model_parts.append(f"--- DOC: {file.filename} ---\n{text}\n------\n")

    model_parts.append(f"\nINSTRUCCIONES: {instructions}")

    model = GenerativeModel(model_name=GEMINI_MODEL_NAME, system_instruction=ANALYZER_SYSTEM_PROMPT)
    
    async def generate_stream():
        full_text = ""
        try:
            responses = await model.generate_content_async(
                model_parts,
                generation_config={"temperature": 0.4, "max_output_tokens": 16348},
                stream=True
            )
            async for chunk in responses:
                if chunk.text:
                    full_text += chunk.text
                    yield f"data: {json.dumps({'text': chunk.text})}\n\n"
            
            user_id = current_user.get("uid")
            title = (instructions[:40] + '...') if len(instructions) > 40 else instructions
            
            # SOLUCIÓN I/O BLOCKING: Usar await con AsyncClient
            doc_ref = db.collection("analysis_history").document()
            await doc_ref.set({
                "userId": user_id,
                "title": title,
                "instructions": instructions,
                "analysis": full_text,
                "timestamp": SERVER_TIMESTAMP,
                "original_filenames": original_filenames
            })
            yield f"data: {json.dumps({'done': True, 'analysis_id': doc_ref.id})}\n\n"
        except Exception as e:
            print(f"Error stream: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate_stream(), media_type="text/event-stream")

@app.post("/download-analysis")
async def download_analysis(
    analysis_text: str = Form(...),
    instructions: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        if file_format.lower() == "docx":
            # SOLUCIÓN CPU BLOCKING: Mover a hilo secundario
            content, mime, fname = await asyncio.to_thread(create_docx_sync, analysis_text, instructions, timestamp)
        else:
            # SOLUCIÓN CPU BLOCKING Y PDF BLANCO: Mover a hilo secundario
            content, mime, fname = await asyncio.to_thread(create_pdf_sync, analysis_text, instructions, timestamp)

        return Response(content=content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        raise HTTPException(500, f"Error descarga: {e}")

@app.get("/analysis-history/")
async def get_analysis_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user.get("uid")
    # SOLUCIÓN I/O BLOCKING: Consultas asíncronas
    ref = db.collection("analysis_history").where("userId", "==", user_id).order_by("timestamp", direction=Query.DESCENDING)
    history = []
    async for d in ref.stream():
        history.append({"id": d.id, "title": d.get("title"), "timestamp": d.get("timestamp")})
    return history

@app.get("/analysis-history/{analysis_id}")
async def get_analysis_detail(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    # SOLUCIÓN I/O BLOCKING: await get()
    doc = await db.collection("analysis_history").document(analysis_id).get()
    if not doc.exists: raise HTTPException(404)
    data = doc.to_dict()
    if data.get("userId") != current_user.get("uid"): raise HTTPException(403)
    return data

@app.delete("/analysis-history/{analysis_id}")
async def delete_analysis(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    ref = db.collection("analysis_history").document(analysis_id)
    # SOLUCIÓN I/O BLOCKING: await get() y await delete()
    doc = await ref.get()
    if not doc.exists: raise HTTPException(404)
    if doc.to_dict().get("userId") != current_user.get("uid"): raise HTTPException(403)
    await ref.delete()
    return {"status": "ok"}

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "API Analizador Activa v2.1 (FIXED)"}
