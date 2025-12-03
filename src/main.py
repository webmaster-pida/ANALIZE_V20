# /src/main.py

import os
import base64
import json
import io
import re
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any
from dotenv import load_dotenv
from docx import Document
from docx.shared import Pt
from fpdf import FPDF
from markdown_it import MarkdownIt
from datetime import datetime
from google.cloud import firestore
import google.auth

# --- NUEVO: VERTEX AI SDK ---
import vertexai
from vertexai.generative_models import GenerativeModel, Part, SafetySetting

from src.core.security import get_current_user
from src.core.prompts import ANALYZER_SYSTEM_PROMPT

# Cargar variables de entorno locales
load_dotenv()

# --- CONFIGURACIÓN VERTEX AI ---
try:
    # Intentar obtener credenciales y proyecto por defecto
    _, project_id_default = google.auth.default()
    PROJECT_ID = os.getenv("PROJECT_ID", project_id_default)
except:
    PROJECT_ID = os.getenv("PROJECT_ID")

LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
# En Vertex, usamos el nombre del modelo sin 'v1beta'
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash-001").strip()

if PROJECT_ID:
    try:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        print(f"Vertex AI inicializado correctamente: {PROJECT_ID} / {LOCATION}")
    except Exception as e:
        print(f"Error inicializando Vertex AI: {e}")
else:
    print("ADVERTENCIA: No se encontró PROJECT_ID. Vertex AI fallará.")

# Inicializar Firestore
db = firestore.Client(project=PROJECT_ID)

app = FastAPI(title="PIDA Document Analyzer (Vertex)")

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

# --- LÓGICA DE SUSCRIPCIÓN (Simplificada para confiar en Security.py) ---
def verify_active_subscription(current_user: Dict[str, Any]):
    # Si el usuario pasó el filtro de seguridad (Dominio/Email Admin), no verificamos suscripción.
    # Esta función se mantiene por si en el futuro quieres cobrar a usuarios externos.
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
        try:
            self.add_font("NotoSans", "", "fonts/NotoSans-Regular.ttf", uni=True)
            self.add_font("NotoSans", "B", "fonts/NotoSans-Bold.ttf", uni=True)
            self.set_font("NotoSans", "B", 15)
        except RuntimeError:
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

# --- ENDPOINTS ---

@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    instructions: str = Form(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    print(f"Análisis (Vertex) solicitado por: {current_user.get('email')}")
    
    if len(files) > 3:
        raise HTTPException(status_code=400, detail="Máximo 3 archivos permitidos.")

    supported_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    model_parts = []
    original_filenames = []

    for file in files:
        if file.content_type not in supported_types:
            raise HTTPException(status_code=400, detail=f"Formato no soportado: {file.filename}")
        
        original_filenames.append(file.filename)
        content = await file.read()

        if file.content_type == "application/pdf":
            # Vertex recibe el PDF binario directamente
            part = Part.from_data(data=content, mime_type="application/pdf")
            model_parts.append(part)
        else:
            # DOCX a texto
            try:
                doc = Document(io.BytesIO(content))
                text = "\n".join([p.text for p in doc.paragraphs])
                model_parts.append(f"--- DOC: {file.filename} ---\n{text}\n------\n")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Error leyendo DOCX {file.filename}: {e}")

    model_parts.append(f"\nINSTRUCCIONES: {instructions}")

    try:
        model = GenerativeModel(
            model_name=GEMINI_MODEL_NAME,
            system_instruction=ANALYZER_SYSTEM_PROMPT
        )
        
        # Configuración de seguridad laxa para documentos legales/técnicos
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

        # Llamada a Vertex AI
        responses = model.generate_content(
            model_parts,
            generation_config={"temperature": 0.3, "max_output_tokens": 8192},
            safety_settings=safety,
            stream=False
        )
        
        analysis_text = responses.text

        # Guardar en Firestore
        doc_ref = db.collection("analysis_history").document()
        doc_ref.set({
            "userId": current_user.get("uid"),
            "title": (instructions[:40] + '...') if len(instructions) > 40 else instructions,
            "instructions": instructions,
            "analysis": analysis_text,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "original_filenames": original_filenames
        })

        return {"analysis": analysis_text, "analysis_id": doc_ref.id}

    except Exception as e:
        print(f"Error Vertex AI: {e}")
        msg = str(e)
        if "403" in msg or "PermissionDenied" in msg:
            raise HTTPException(status_code=500, detail="Error de permisos en Vertex AI. Verifica la API en GCP.")
        if "429" in msg or "ResourceExhausted" in msg:
            raise HTTPException(status_code=503, detail="Cuota de IA excedida, intenta más tarde.")
        raise HTTPException(status_code=500, detail=f"Error al analizar: {msg}")

@app.get("/analysis-history/")
async def get_analysis_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user.get("uid")
    # Requiere el índice compuesto que ya creaste
    ref = db.collection("analysis_history").where("userId", "==", user_id).order_by("timestamp", direction=firestore.Query.DESCENDING)
    return [{"id": d.id, "title": d.get("title"), "timestamp": d.get("timestamp")} for d in ref.stream()]

@app.get("/analysis-history/{analysis_id}")
async def get_analysis_detail(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    doc = db.collection("analysis_history").document(analysis_id).get()
    if not doc.exists: raise HTTPException(404, "No encontrado")
    data = doc.to_dict()
    if data.get("userId") != current_user.get("uid"): raise HTTPException(403, "Sin permiso")
    return data

@app.delete("/analysis-history/{analysis_id}")
async def delete_analysis(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    ref = db.collection("analysis_history").document(analysis_id)
    doc = ref.get()
    if not doc.exists: raise HTTPException(404, "No encontrado")
    if doc.to_dict().get("userId") != current_user.get("uid"): raise HTTPException(403, "Sin permiso")
    ref.delete()
    return {"status": "ok"}

@app.post("/download-analysis")
async def download_analysis(
    analysis_text: str = Form(...),
    instructions: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        stream = io.BytesIO()

        if file_format.lower() == "docx":
            doc = Document()
            try:
                doc.styles['Normal'].font.name = 'Arial'
            except: pass
            doc.add_heading("PIDA-AI: Resumen", 0)
            doc.add_paragraph(f"Fecha: {datetime.now()}")
            doc.add_heading("Instrucciones", 2)
            doc.add_paragraph(instructions)
            doc.add_heading("Análisis", 2)
            parse_and_add_markdown_to_docx(doc, analysis_text)
            doc.save(stream)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            fname = f"PIDA_{timestamp}.docx"
        else:
            pdf = PDF()
            pdf.alias_nb_pages()
            pdf.add_page()
            pdf.set_font("Arial", "B", 12)
            pdf.cell(0, 10, "Instrucciones", 0, 1)
            pdf.set_font("Arial", "", 11)
            pdf.multi_cell(0, 6, instructions)
            pdf.ln(5)
            pdf.set_font("Arial", "B", 12)
            pdf.cell(0, 10, "Análisis", 0, 1)
            pdf.set_font("Arial", "", 11)
            md = MarkdownIt()
            html = md.render(analysis_text)
            try:
                pdf.write_html(html)
            except:
                pdf.multi_cell(0, 6, analysis_text)
            stream.write(pdf.output(dest='S').encode('latin1', 'ignore'))
            mime = "application/pdf"
            fname = f"PIDA_{timestamp}.pdf"

        stream.seek(0)
        return Response(content=stream.read(), media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        raise HTTPException(500, f"Error generando archivo: {e}")

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "Analizador Vertex Activo"}
