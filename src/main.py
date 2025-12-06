# /src/main.py

import os
import base64
import json
import io
import re
import asyncio
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
# Importamos StreamingResponse para el efecto "poco a poco"
from fastapi.responses import StreamingResponse 
from typing import List, Dict, Any
from dotenv import load_dotenv
from docx import Document
from docx.shared import Pt
from fpdf import FPDF
from markdown_it import MarkdownIt
from datetime import datetime
from google.cloud.firestore import AsyncClient, SERVER_TIMESTAMP, Query
import google.auth

# --- VERTEX AI SDK ---
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

# --- UTILIDADES ---
def verify_active_subscription(current_user: Dict[str, Any]):
    pass 

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
        # Usando Arial (fuente estándar FPDF)
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
        self.cell(0, 10, f"Página {self.page_no()}/{{nb}}", 0, 0, "C")


# --- UTILIDADES ASÍNCRONAS PARA PROCESAMIENTO/DESCARGA (to_thread helpers) ---

def read_docx_sync(content: bytes) -> str:
    """Función síncrona para leer el contenido de un DOCX (CPU-Bound)."""
    doc = Document(io.BytesIO(content))
    return "\n".join([p.text for p in doc.paragraphs])

def create_docx_sync(analysis_text: str, instructions: str, timestamp: str) -> tuple[bytes, str, str]:
    """Función síncrona para generar el archivo DOCX (CPU-Bound)."""
    stream = io.BytesIO()
    doc = Document()
    try:
        doc.styles['Normal'].font.name = 'Arial'
    except: pass
    doc.add_heading("PIDA-AI: Resumen", 0)
    doc.add_paragraph(f"Fecha: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}")
    doc.add_heading("Instrucciones", 2)
    doc.add_paragraph(instructions)
    doc.add_heading("Análisis", 2)
    parse_and_add_markdown_to_docx(doc, analysis_text)
    doc.save(stream)
    stream.seek(0)
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    fname = f"PIDA_{timestamp}.docx"
    return stream.read(), mime, fname

def create_pdf_sync(analysis_text: str, instructions: str, timestamp: str) -> tuple[bytes, str, str]:
    """Función síncrona para generar el archivo PDF (CPU-Bound)."""
    pdf = PDF()
    
    pdf.alias_nb_pages()
    pdf.add_page()
    
    # Usar Arial directamente
    pdf.set_font("Arial", "B", 12)

    pdf.cell(0, 10, "Instrucciones", 0, 1)
    
    # Usar Arial directamente
    pdf.set_font("Arial", "", 11)
        
    pdf.multi_cell(0, 6, instructions)
    pdf.ln(5)
    
    # Usar Arial directamente
    pdf.set_font("Arial", "B", 12)
        
    pdf.cell(0, 10, "Análisis", 0, 1)
    
    # Usar Arial directamente
    pdf.set_font("Arial", "", 11)
    
    stream = io.BytesIO()
    
    # CORREGIDO: Se eliminó el uso de markdown_it y pdf.write_html para evitar que el PDF salga en blanco.
    # Se usa multi_cell directamente con el texto de análisis (que ya es texto plano/markdown).
    # Esta es la forma más estable de volcar contenido de texto en fpdf.
    pdf.multi_cell(0, 6, analysis_text)
    
    # pdf.output(dest='S') es la operación CPU-Bound que devuelve el contenido
    stream.write(pdf.output(dest='S').encode('latin1', 'ignore'))
    stream.seek(0)
    
    mime = "application/pdf"
    fname = f"PIDA_{timestamp}.pdf"
    return stream.read(), mime, fname


# --- ENDPOINT STREAMING ---
@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    instructions: str = Form(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    print(f"Análisis (Streaming) solicitado por: {current_user.get('email')}")
    
    if len(files) > 3:
        raise HTTPException(status_code=400, detail="Máximo 3 archivos.")

    supported_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    model_parts = []
    original_filenames = []

    # 1. Procesar archivos (Leemos en memoria antes de empezar el stream)
    for file in files:
        if file.content_type not in supported_types:
            raise HTTPException(status_code=400, detail=f"Formato no soportado: {file.filename}")
        
        original_filenames.append(file.filename)
        content = await file.read()

        if file.content_type == "application/pdf":
            part = Part.from_data(data=content, mime_type="application/pdf")
            model_parts.append(part)
        else:
            try:
                # Usar asyncio.to_thread para no bloquear el Event Loop con docx
                text = await asyncio.to_thread(read_docx_sync, content)
                model_parts.append(f"--- DOC: {file.filename} ---\n{text}\n------\n")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Error leyendo DOCX {file.filename}: {e}")

    model_parts.append(f"\nINSTRUCCIONES: {instructions}")

    # 2. Configurar Modelo
    model = GenerativeModel(
        model_name=GEMINI_MODEL_NAME,
        system_instruction=ANALYZER_SYSTEM_PROMPT
    )
    
    safety = [
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_ONLY_HIGH
        ),
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_ONLY_HIGH
        ),
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_ONLY_HIGH
        ),
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_ONLY_HIGH
        ),
    ]

    # 3. Generador Asíncrono para Streaming
    async def generate_stream():
        full_text = ""
        try:
            # Usamos generate_content_async con stream=True
            responses = await model.generate_content_async(
                model_parts,
                generation_config={"temperature": 0.4, "max_output_tokens": 16348},
                safety_settings=safety,
                stream=True
            )

            async for chunk in responses:
                if chunk.text:
                    full_text += chunk.text
                    # Enviamos formato SSE (Server-Sent Events)
                    yield f"data: {json.dumps({'text': chunk.text})}\n\n"
            
            # Al terminar, guardamos en Firestore
            user_id = current_user.get("uid")
            title = (instructions[:40] + '...') if len(instructions) > 40 else instructions
            
            doc_ref = db.collection("analysis_history").document()
            # Usar await con doc_ref.set()
            await doc_ref.set({
                "userId": user_id,
                "title": title,
                "instructions": instructions,
                "analysis": full_text,
                "timestamp": SERVER_TIMESTAMP, # Usar SERVER_TIMESTAMP importado
                "original_filenames": original_filenames
            })
            
            # Enviamos señal de fin con el ID del análisis
            yield f"data: {json.dumps({'done': True, 'analysis_id': doc_ref.id})}\n\n"

        except Exception as e:
            print(f"Error en stream: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    # Retornamos la respuesta como un stream de eventos
    return StreamingResponse(generate_stream(), media_type="text/event-stream")

# ... (Resto de endpoints GET, DELETE, DOWNLOAD) ...
@app.get("/analysis-history/")
async def get_analysis_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user.get("uid")
    # Usar Query.DESCENDING importado
    ref = db.collection("analysis_history").where("userId", "==", user_id).order_by("timestamp", direction=Query.DESCENDING)
    
    history = []
    # Usar async for en el stream del AsyncClient
    async for d in ref.stream():
        history.append({"id": d.id, "title": d.get("title"), "timestamp": d.get("timestamp")})
        
    return history

@app.get("/analysis-history/{analysis_id}")
async def get_analysis_detail(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    # Usar await con doc.get()
    doc = await db.collection("analysis_history").document(analysis_id).get()
    if not doc.exists: raise HTTPException(404, "No encontrado")
    data = doc.to_dict()
    if data.get("userId") != current_user.get("uid"): raise HTTPException(403, "Sin permiso")
    return data

@app.delete("/analysis-history/{analysis_id}")
async def delete_analysis(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    ref = db.collection("analysis_history").document(analysis_id)
    # Usar await con doc.get()
    doc = await ref.get()
    if not doc.exists: raise HTTPException(404, "No encontrado")
    if doc.to_dict().get("userId") != current_user.get("uid"): raise HTTPException(403, "Sin permiso")
    # Usar await con ref.delete()
    await ref.delete()
    return {"status": "ok"}

@app.post("/download-analysis")
async def download_analysis(
    analysis_text: str = Form(...),
    instructions: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    try:
        # El formato incluye Año, Mes, Día, Hora, Minutos y Segundos (YYYY-MM-DD_HHMMSS)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S") 

        if file_format.lower() == "docx":
            # Mover la generación DOCX a un hilo secundario
            content, mime, fname = await asyncio.to_thread(create_docx_sync, analysis_text, instructions, timestamp)
        else:
            # Mover la generación PDF a un hilo secundario
            content, mime, fname = await asyncio.to_thread(create_pdf_sync, analysis_text, instructions, timestamp)

        # Retornar la respuesta con el contenido generado en el hilo
        return Response(content=content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        raise HTTPException(500, f"Error generando archivo: {e}")

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "Analizador Vertex Activo (Streaming)"}
