import os
import firebase_admin
from firebase_admin import credentials
from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException
from fastapi.responses import JSONResponse
from typing import List
import tempfile
import fitz  # PyMuPDF
from docx import Document as DocxDocument
import markdown
from fpdf import FPDF, HTMLMixin
import logging

# --- INICIO DE LA MODIFICACIÓN ---
from fastapi.middleware.cors import CORSMiddleware
# --- FIN DE LA MODIFICACIÓN ---

from src.core.security import get_current_user
from src.core.prompts import get_analysis_prompt
from vertexai.generative_models import GenerativeModel, Part

# Configuración del logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicialización de Firebase
try:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
    logger.info("Firebase Admin SDK inicializado correctamente.")
except ValueError:
    logger.warning("Firebase Admin SDK ya ha sido inicializado previamente.")
except Exception as e:
    logger.error(f"Error inesperado al inicializar Firebase Admin SDK: {e}")

# Inicialización del modelo generativo de Vertex AI
model = GenerativeModel("gemini-1.5-pro-001")

app = FastAPI(
    title="PIDA - API del Analizador de Documentos",
    description="Procesa documentos, genera análisis y propuestas con IA.",
    version="1.0.0"
)

# --- INICIO DE LA MODIFICACIÓN ---
# Configuración de CORS
origins = [
    "https://pida-ai.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# --- FIN DE LA MODIFICACIÓN ---

class MyFPDF(FPDF, HTMLMixin):
    pass

def extract_text_from_pdf(file_path: str) -> str:
    try:
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        logger.error(f"Error extrayendo texto de PDF {file_path}: {e}")
        raise

def extract_text_from_docx(file_path: str) -> str:
    try:
        doc = DocxDocument(file_path)
        return "\n".join([para.text for para in doc.paragraphs])
    except Exception as e:
        logger.error(f"Error extrayendo texto de DOCX {file_path}: {e}")
        raise

async def process_documents_and_generate_analysis(files: List[UploadFile], user_instructions: str, user_id: str) -> str:
    combined_text = ""
    temp_files = []

    try:
        for file in files:
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as temp_file:
                content = await file.read()
                temp_file.write(content)
                temp_file_path = temp_file.name
                temp_files.append(temp_file_path)

            logger.info(f"Procesando archivo: {file.filename} ({file.content_type})")
            
            text = ""
            if file.content_type == "application/pdf":
                text = extract_text_from_pdf(temp_file_path)
            elif file.content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]:
                text = extract_text_from_docx(temp_file_path)
            
            combined_text += f"\n--- INICIO DEL DOCUMENTO: {file.filename} ---\n"
            combined_text += text
            combined_text += f"\n--- FIN DEL DOCUMENTO: {file.filename} ---\n"

        if not combined_text.strip():
            logger.warning("No se pudo extraer texto de los documentos proporcionados.")
            return "No se pudo extraer texto de los documentos proporcionados. Por favor, verifica que no estén vacíos o protegidos."

        prompt_text = get_analysis_prompt(user_instructions, combined_text)
        
        logger.info(f"Generando análisis para el usuario {user_id}...")
        response = await model.generate_content_async(prompt_text)
        
        analysis_result = response.text
        logger.info(f"Análisis generado exitosamente para el usuario {user_id}.")
        
        return analysis_result

    except Exception as e:
        logger.error(f"Error en process_documents_and_generate_analysis: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for temp_file_path in temp_files:
            try:
                os.remove(temp_file_path)
            except OSError as e:
                logger.error(f"Error eliminando archivo temporal {temp_file_path}: {e}")

def generate_pdf_from_html(html_content: str, output_path: str):
    pdf = MyFPDF()
    pdf.add_page()
    
    # Añadir fuentes que soporten caracteres latinos extendidos si es necesario
    try:
        # Asumiendo que las fuentes están en un directorio 'fonts' en la raíz del proyecto.
        # Es importante que el Dockerfile copie este directorio.
        pdf.add_font('NotoSans', '', 'fonts/NotoSans-Regular.ttf', uni=True)
        pdf.add_font('NotoSans', 'B', 'fonts/NotoSans-Bold.ttf', uni=True)
        pdf.add_font('NotoSans', 'I', 'fonts/NotoSans-Italic.ttf', uni=True)
        pdf.set_font("NotoSans", size=12)
    except RuntimeError:
        logger.warning("No se encontraron las fuentes NotoSans. Usando fuente por defecto (puede haber problemas con caracteres especiales).")
        pdf.set_font("Arial", size=12)

    try:
        pdf.write_html(html_content)
        pdf.output(output_path)
        logger.info(f"PDF generado correctamente en {output_path}")
    except Exception as e:
        logger.error(f"Error al escribir HTML en PDF: {e}")
        # Intento de fallback a texto plano si write_html falla
        pdf.set_font("Arial", size=12)
        pdf.multi_cell(0, 5, html_content)
        pdf.output(output_path)
        logger.info(f"PDF generado en modo fallback (texto plano) en {output_path}")


@app.post("/analyze/")
async def analyze_documents(
    files: List[UploadFile] = File(...),
    user_instructions: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if not files:
        raise HTTPException(status_code=400, detail="No se proporcionaron archivos para analizar.")
    if not user_instructions:
        raise HTTPException(status_code=400, detail="No se proporcionaron instrucciones para el análisis.")

    allowed_content_types = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ]

    for file in files:
        if file.content_type not in allowed_content_types:
            raise HTTPException(
                status_code=400,
                detail=f"Tipo de archivo no permitido: {file.filename}. Solo se aceptan .pdf, .doc, .docx."
            )

    try:
        analysis_result = await process_documents_and_generate_analysis(files, user_instructions, user['uid'])
        return JSONResponse(content={"analysis": analysis_result})
    except Exception as e:
        logger.error(f"Error en el endpoint /analyze/: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno del servidor al procesar los documentos: {e}")

@app.post("/download/pdf/")
async def download_analysis_as_pdf(
    analysis_content: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if not analysis_content:
        raise HTTPException(status_code=400, detail="No se proporcionó contenido para generar el PDF.")

    try:
        # Convertir el contenido de Markdown (que viene del análisis) a HTML
        html_content = markdown.markdown(analysis_content)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            output_path = temp_file.name
        
        generate_pdf_from_html(html_content, output_path)
        
        # Esta parte requeriría una forma de devolver el archivo,
        # lo cual es más complejo en un setup serverless.
        # Para simplificar, asumiremos que el frontend maneja la generación del PDF.
        # Esta función se deja como ejemplo o para futura implementación.
        # De momento, el frontend ya tiene la lógica para crear el PDF.
        
        # Devolver una confirmación en lugar del archivo.
        return JSONResponse(content={"message": "La generación de PDF en el backend está en desarrollo.", "path_temporal": output_path})

    except Exception as e:
        logger.error(f"Error en /download/pdf/: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al generar el PDF: {e}")


@app.get("/")
def read_root():
    return {"status": "Servicio de análisis de PIDA funcionando correctamente"}
