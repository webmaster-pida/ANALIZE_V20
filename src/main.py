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

# Inicializar Firestore
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

# --- UTILIDADES DE NOMBRE DE ARCHIVO ---
def generate_filename(instructions: str, extension: str) -> str:
    """Genera un nombre de archivo basado en el título y fecha exacta."""
    # 1. Limpiar título (primeros 40 caracteres, solo alfanuméricos)
    safe_title = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ]', '', instructions[:40])
    safe_title = safe_title.strip().replace(' ', '_')
    if not safe_title:
        safe_title = "Analisis_PIDA"
    
    # 2. Fecha formato: año-mes-día-hora-segundos
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    
    return f"{safe_title}_{timestamp}.{extension}"

# --- UTILIDADES DE LIMPIEZA TEXTO ---
def sanitize_text_for_pdf(text: str) -> str:
    """Limpia caracteres incompatibles con Latin-1."""
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
    """
    Escribe texto en el PDF interpretando Markdown básico (## Títulos y **Negritas**)
    para que no salgan los asteriscos.
    """
    # Asegurar fuente base
    pdf.set_font("Arial", "", 11)
    
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            pdf.ln(5) # Espacio entre párrafos
            continue
            
        # 1. Encabezados (## o #)
        if line.startswith('## '):
            pdf.ln(3)
            pdf.set_font("Arial", "B", 13)
            pdf.set_text_color(29, 53, 87) # Navy
            pdf.multi_cell(0, 8, line.replace('## ', ''))
            pdf.set_text_color(0, 0, 0) # Reset color
            pdf.set_font("Arial", "", 11)
        elif line.startswith('# '):
            pdf.ln(5)
            pdf.set_font("Arial", "B", 15)
            pdf.set_text_color(185, 47, 50) # Pida Red
            pdf.multi_cell(0, 10, line.replace('# ', ''))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Arial", "", 11)
            
        # 2. Listas (* o -)
        elif line.startswith('* ') or line.startswith('- '):
            pdf.set_x(15) # Sangría para viñeta
            clean_line = line[2:] # Quitar el *
            pdf.write(6, "- ") # Escribir viñeta estándar
            
            # Procesar negritas dentro de la línea de lista
            parts = re.split(r'(\*\*.*?\*\*)', clean_line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    pdf.set_font("Arial", "B", 11)
                    pdf.write(6, part.strip('*'))
                    pdf.set_font("Arial", "", 11)
                else:
                    pdf.write(6, part)
            pdf.ln(6)

        # 3. Párrafo normal (con posibles negritas)
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
    # 1. Limpieza de caracteres
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
    
    # Análisis (Usando el parser manual de Markdown)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Analisis", 0, 1)
    
    if not safe_ana.strip():
        pdf.set_font("Arial", "I", 11)
        pdf.multi_cell(0, 6, "[Sin contenido]")
    else:
        # Aquí usamos la función que interpreta **negritas** y ## títulos
        write_markdown_to_pdf(pdf, safe_ana)
    
    # Generar salida bytes
    try:
        pdf_string = pdf.output(dest='S')
        if isinstance(pdf_string, str):
            pdf_bytes = pdf_string.encode('latin-1', 'replace')
        else:
            pdf_bytes = pdf_string
        
        stream = io.BytesIO(pdf_bytes)
        fname = generate_filename(instructions, "pdf")
        return stream.read(), "application/pdf", fname
    except Exception as e:
        print(f"Error PDF: {e}")
        err = FPDF()
        err.add_page()
        err.multi_cell(0, 10, f"Error: {str(e)}")
        return err.output(dest='S').encode('latin-1'), "application/pdf", "Error.pdf"

# --- ENDPOINTS ---
@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    instructions: str = Form(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    if len(files) > 3: raise HTTPException(400, "Máximo 3 archivos.")
    model_parts = []
    original_filenames = []

    for file in files:
        original_filenames.append(file.filename)
        content = await file.read()
        if file.content_type == "application/pdf":
            model_parts.append(Part.from_data(data=content, mime_type="application/pdf"))
        else:
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
            doc_ref = db.collection("analysis_history").document()
            await doc_ref.set({
                "userId": user_id, "title": title, "instructions": instructions,
                "analysis": full_text, "timestamp": SERVER_TIMESTAMP, "original_filenames": original_filenames
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
        if file_format.lower() == "docx":
            content, mime, fname = await asyncio.to_thread(create_docx_sync, analysis_text, instructions)
        else:
            content, mime, fname = await asyncio.to_thread(create_pdf_sync, analysis_text, instructions)
            
        return Response(content=content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        raise HTTPException(500, f"Error descarga: {e}")

@app.get("/analysis-history/")
async def get_analysis_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user.get("uid")
    ref = db.collection("analysis_history").where("userId", "==", user_id).order_by("timestamp", direction=Query.DESCENDING)
    history = []
    async for d in ref.stream():
        history.append({"id": d.id, "title": d.get("title"), "timestamp": d.get("timestamp")})
    return history

@app.get("/analysis-history/{analysis_id}")
async def get_analysis_detail(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    doc = await db.collection("analysis_history").document(analysis_id).get()
    if not doc.exists: raise HTTPException(404)
    data = doc.to_dict()
    if data.get("userId") != current_user.get("uid"): raise HTTPException(403)
    return data

@app.delete("/analysis-history/{analysis_id}")
async def delete_analysis(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    ref = db.collection("analysis_history").document(analysis_id)
    doc = await ref.get()
    if not doc.exists: raise HTTPException(404)
    if doc.to_dict().get("userId") != current_user.get("uid"): raise HTTPException(403)
    await ref.delete()
    return {"status": "ok"}

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "API Analizador Activa v3.0 (PDF Formatted)"}
