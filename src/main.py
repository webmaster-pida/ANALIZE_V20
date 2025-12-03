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

# --- NUEVO: VERTEX AI ---
import vertexai
from vertexai.generative_models import GenerativeModel, Part, SafetySetting
import google.auth
# ------------------------

from src.core.security import get_current_user
from src.core.prompts import ANALYZER_SYSTEM_PROMPT

# Cargar variables de entorno
load_dotenv()

# --- CONFIGURACIÓN GOOGLE CLOUD / VERTEX AI ---
# Intentamos obtener el Project ID automáticamente del entorno de Cloud Run
try:
    _, project_id_default = google.auth.default()
    PROJECT_ID = os.getenv("PROJECT_ID", project_id_default)
except:
    PROJECT_ID = os.getenv("PROJECT_ID")

LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
# Nota: En Vertex AI, los nombres de modelos a veces no llevan la versión "beta" o cambian ligeramente.
# Usamos un default sólido para Vertex.
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash-001").strip()

print(f"--- Configurando Vertex AI: Proyecto={PROJECT_ID}, Región={LOCATION}, Modelo={GEMINI_MODEL_NAME} ---")

if PROJECT_ID:
    try:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
    except Exception as e:
        print(f"Error inicializando Vertex AI: {e}")
else:
    print("ADVERTENCIA: No se pudo detectar PROJECT_ID. Las llamadas a Vertex AI fallarán.")

# Inicializar Firestore
db = firestore.Client(project=PROJECT_ID)

app = FastAPI(title="PIDA Document Analyzer API (Vertex AI)")

# --- CORS ---
raw_origins = os.getenv("ALLOWED_ORIGINS", '["https://pida-ai.com"]')
try:
    origins = json.loads(raw_origins)
except json.JSONDecodeError:
    print("Error parseando ALLOWED_ORIGINS, usando default.")
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
    """
    Verifica permisos. En este caso, valida contra listas de administradores.
    """
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").lower()

    raw_domains = os.getenv("ADMIN_DOMAINS", '["iiresodh.org", "urquilla.com"]')
    raw_emails = os.getenv("ADMIN_EMAILS", '[]')

    try:
        admin_domains = json.loads(raw_domains)
        # Normalizamos emails a minúsculas
        admin_emails = [e.lower() for e in json.loads(raw_emails)]
    except json.JSONDecodeError:
        admin_domains = ["iiresodh.org", "urquilla.com"]
        admin_emails = []

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    # Acceso concedido si es Admin (Dominio o Email específico)
    if (email_domain in admin_domains) or (user_email in admin_emails):
        print(f"Acceso ADMIN concedido para: {user_email}")
        return
    
    # Lógica para clientes regulares (Suscripciones en Firestore)
    try:
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        results = list(query.get())

        if not results:
            print(f"Acceso DENEGADO para: {user_email} (Sin suscripción)")
            raise HTTPException(status_code=403, detail="No tienes una suscripción activa.")
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"Error verificando suscripción en DB: {e}")
        raise HTTPException(status_code=500, detail="Error de verificación de cuenta.")

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
        self.set_font("NotoSans" if self.font_family == "NotoSans" else "Arial", "", 10)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}", 0, 1, "L")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("NotoSans" if self.font_family == "NotoSans" else "Arial", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Página {self.page_no()}/{{nb}}", 0, 0, "C")

# --- ENDPOINT PRINCIPAL (MIGRADO A VERTEX AI) ---
@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    instructions: str = Form(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    print(f"Petición de análisis (Vertex) recibida. Usuario: {current_user.get('email')}")
    
    verify_active_subscription(current_user)
    
    if len(files) > 3:
        raise HTTPException(status_code=400, detail="Máximo 3 archivos permitidos.")

    supported_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    
    model_parts = []
    original_filenames = []

    for file in files:
        if file.content_type not in supported_types:
            raise HTTPException(status_code=400, detail=f"Tipo no soportado: {file.filename}")
        
        original_filenames.append(file.filename)
        content = await file.read()

        if file.content_type == "application/pdf":
            # Vertex AI recibe el PDF binario directamente como 'Part'
            part = Part.from_data(data=content, mime_type="application/pdf")
            model_parts.append(part)
            
        elif file.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            # Para DOCX, extraemos el texto nosotros mismos para mejor calidad
            try:
                doc_stream = io.BytesIO(content)
                document = Document(doc_stream)
                full_text = "\n".join([para.text for para in document.paragraphs])
                text_block = f"--- DOCUMENTO: {file.filename} ---\n{full_text}\n--- FIN DOCUMENTO ---\n"
                model_parts.append(text_block)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Error leyendo DOCX {file.filename}: {e}")

    # Agregar instrucciones del usuario al final
    model_parts.append(f"\nINSTRUCCIONES DEL USUARIO: {instructions}")

    try:
        # Inicializar modelo Generativo de Vertex
        model = GenerativeModel(
            model_name=GEMINI_MODEL_NAME,
            system_instruction=ANALYZER_SYSTEM_PROMPT
        )

        generation_config = {
            "max_output_tokens": 8192,
            "temperature": 0.3,
            "top_p": 0.95,
        }

        # Configuración de seguridad laxa para documentos legales/técnicos
        safety_settings = [
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
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False
        )
        
        analysis_text = responses.text

        # Guardar en Firestore
        user_id = current_user.get("uid")
        # Título corto para el historial
        title = (instructions[:37] + '...') if len(instructions) > 40 else instructions
        
        doc_ref = db.collection("analysis_history").document()
        doc_ref.set({
            "userId": user_id,
            "title": title,
            "instructions": instructions,
            "analysis": analysis_text,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "original_filenames": original_filenames
        })
        
        return {"analysis": analysis_text, "analysis_id": doc_ref.id}

    except Exception as e:
        print(f"Error CRÍTICO en Vertex AI: {e}")
        # Mapeo de errores comunes
        if "403" in str(e):
            detail_msg = "Error de permisos en Vertex AI. Verifica que la API 'Vertex AI API' esté habilitada en Google Cloud."
        elif "429" in str(e):
            detail_msg = "Se ha excedido la cuota de uso de la IA. Intenta más tarde."
        else:
            detail_msg = f"Error procesando el documento: {str(e)}"
            
        raise HTTPException(status_code=500, detail=detail_msg)

# ... (Endpoints de historial y descarga sin cambios significativos) ...

@app.get("/analysis-history/")
async def get_analysis_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    verify_active_subscription(current_user)
    user_id = current_user.get("uid")
    # Requiere el índice compuesto que creaste antes
    history_ref = db.collection("analysis_history").where("userId", "==", user_id).order_by("timestamp", direction=firestore.Query.DESCENDING)
    docs = history_ref.stream()
    history = []
    for doc in docs:
        doc_data = doc.to_dict()
        history.append({
            "id": doc.id,
            "title": doc_data.get("title"),
            "timestamp": doc_data.get("timestamp")
        })
    return history

@app.get("/analysis-history/{analysis_id}")
async def get_analysis_detail(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    verify_active_subscription(current_user)
    user_id = current_user.get("uid")
    doc_ref = db.collection("analysis_history").document(analysis_id)
    doc = doc_ref.get()
    if not doc.exists: raise HTTPException(status_code=404, detail="Análisis no encontrado.")
    doc_data = doc.to_dict()
    if doc_data.get("userId") != user_id: raise HTTPException(status_code=403, detail="Sin permiso.")
    return doc_data

@app.delete("/analysis-history/{analysis_id}")
async def delete_analysis(analysis_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    verify_active_subscription(current_user)
    user_id = current_user.get("uid")
    doc_ref = db.collection("analysis_history").document(analysis_id)
    doc = doc_ref.get()
    if not doc.exists: raise HTTPException(status_code=404, detail="Análisis no encontrado.")
    if doc.to_dict().get("userId") != user_id: raise HTTPException(status_code=403, detail="Sin permiso.")
    doc_ref.delete()
    return {"status": "ok", "message": "Eliminado."}

@app.post("/download-analysis")
async def download_analysis(
    analysis_text: str = Form(...),
    instructions: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    verify_active_subscription(current_user)
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        file_stream = io.BytesIO()

        if file_format.lower() == "docx":
            document = Document()
            try:
                style = document.styles['Normal']
                font = style.font
                font.name = 'Arial' 
                font.size = Pt(11)
            except: pass

            document.add_heading("PIDA-AI: Resumen de Consulta", level=1)
            document.add_paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}")
            document.add_heading("Tu Pregunta", level=2)
            document.add_paragraph(instructions)
            document.add_heading("Respuesta de PIDA-AI", level=2)
            parse_and_add_markdown_to_docx(document, analysis_text)
            document.save(file_stream)
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            filename = f"Analisis-{timestamp}.docx"
        else:
            pdf = PDF()
            pdf.alias_nb_pages()
            pdf.add_page()
            pdf.set_font("Arial", "B", 12)
            pdf.cell(0, 10, "Tu Pregunta", 0, 1, "L")
            pdf.set_font("Arial", "", 11)
            pdf.multi_cell(0, 8, instructions)
            pdf.ln(10)
            pdf.set_font("Arial", "B", 12)
            pdf.cell(0, 10, "Respuesta", 0, 1, "L")
            pdf.set_font("Arial", "", 11)
            md = MarkdownIt()
            html = md.render(analysis_text).replace("<strong>", "<b>").replace("</strong>", "</b>")
            try:
                pdf.write_html(html)
            except:
                pdf.multi_cell(0, 5, analysis_text)

            pdf_output = pdf.output(dest='S').encode('latin1', 'ignore') 
            file_stream.write(pdf_output)
            media_type = "application/pdf"
            filename = f"Analisis-{timestamp}.pdf"

        file_stream.seek(0)
        return Response(content=file_stream.read(), media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        print(f"Error descarga: {e}")
        raise HTTPException(status_code=500, detail="Error generando archivo.")

@app.get("/")
def read_root():
    return {"status": "ok", "message": "PIDA Document Analyzer (Vertex) funcionando."}
