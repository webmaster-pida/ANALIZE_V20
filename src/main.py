import os
import requests
import json
import io
import re
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from dotenv import load_dotenv
from docx import Document
from docx.shared import Pt
from fpdf import FPDF
from markdown_it import MarkdownIt
from datetime import datetime
import google.generativeai as genai

from src.core.prompts import ANALYZER_SYSTEM_PROMPT

# Cargar variables de entorno
load_dotenv()

# --- SECCIÓN DE CONFIGURACIÓN MODIFICADA PARA EL SDK ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("No se encontró la variable de entorno GEMINI_API_KEY")

# Configura el SDK de Google
genai.configure(api_key=GEMINI_API_KEY)
# --- FIN DE LA MODIFICACIÓN ---

app = FastAPI(title="PIDA Document Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

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
        self.add_font("NotoSans", "", "fonts/NotoSans-Regular.ttf", uni=True)
        self.add_font("NotoSans", "B", "fonts/NotoSans-Bold.ttf", uni=True)
        self.set_font("NotoSans", "B", 15)
        self.set_text_color(29, 53, 87)
        self.cell(0, 10, "PIDA-AI: Resumen de Consulta", 0, 1, "L")
        self.set_font("NotoSans", "", 10)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}", 0, 1, "L")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("NotoSans", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Página {self.page_no()}/{{nb}}", 0, 0, "C")

# --- FUNCIÓN DE ANÁLISIS REFACTORIZADA CON EL SDK ---
@app.post("/analyze-documents")
async def analyze_documents(files: List[UploadFile] = File(...), instructions: str = Form(...)):
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Se permite un máximo de 5 archivos.")

    # 1. Configuración del modelo y parámetros
    gemini_model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    temperature = float(os.getenv("GEMINI_TEMP", 0.3))
    top_p = float(os.getenv("GEMINI_TOP_P", 0.95))

    generation_config = genai.types.GenerationConfig(
        temperature=temperature,
        top_p=top_p
    )

    # 2. Prepara los archivos para el SDK
    prompt_parts = []
    for file in files:
        if file.content_type not in ["application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
            raise HTTPException(status_code=400, detail=f"Tipo de archivo no soportado: {file.filename}")
        
        contents = await file.read()
        prompt_parts.append({"mime_type": file.content_type, "data": contents})

    # Añade las instrucciones del usuario al final
    prompt_parts.append(instructions)
    
    try:
        # 3. Inicializa el modelo con el SDK
        model = genai.GenerativeModel(
            model_name=gemini_model_name,
            system_instruction=ANALYZER_SYSTEM_PROMPT,
            generation_config=generation_config
        )

        # 4. Llama a la API a través del SDK
        response = model.generate_content(prompt_parts)

        return {"analysis": response.text}

    except Exception as e:
        # El SDK tiene sus propios errores, los capturamos y devolvemos un error genérico
        raise HTTPException(status_code=500, detail=f"Error al procesar con la API de Gemini: {str(e)}")


@app.post("/download-analysis")
async def download_analysis(
    analysis_text: str = Form(...),
    instructions: str = Form(...),
    file_format: str = Form("docx")
):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        file_stream = io.BytesIO()

        if file_format.lower() == "docx":
            document = Document()
            
            style = document.styles['Normal']
            font = style.font
            font.name = 'Noto Sans'
            font.size = Pt(11)

            style_h1 = document.styles['Heading 1']
            style_h1.font.name = 'Noto Sans'
            
            style_h2 = document.styles['Heading 2']
            style_h2.font.name = 'Noto Sans'

            document.add_heading("PIDA-AI: Resumen de Consulta", level=1)
            document.add_paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}")
            document.add_heading("Tu Pregunta", level=2)
            document.add_paragraph(instructions)
            document.add_heading("Respuesta de PIDA-AI", level=2)
            
            parse_and_add_markdown_to_docx(document, analysis_text)

            document.save(file_stream)
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            filename = f"PIDA-AI-Analisis - {timestamp}.docx"
        else: # pdf
            pdf = PDF()
            pdf.alias_nb_pages()
            pdf.add_page()
            
            pdf.set_font("NotoSans", "B", 12)
            pdf.set_text_color(29, 53, 87)
            pdf.cell(0, 10, "Tu Pregunta", 0, 1, "L")
            pdf.set_font("NotoSans", "", 11)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 8, instructions)
            pdf.ln(10)
            
            pdf.set_font("NotoSans", "B", 12)
            pdf.set_text_color(29, 53, 87)
            pdf.cell(0, 10, "Respuesta de PIDA-AI", 0, 1, "L")
            
            md = MarkdownIt()
            html_content = md.render(analysis_text).replace('<h2>', '<h2><font color="#1D3557">').replace('</h2>', '</font></h2>')
            
            pdf.set_font("NotoSans", "", 11)
            pdf.set_text_color(0, 0, 0)
            pdf.write_html(html_content)
            
            pdf_output = pdf.output()
            file_stream.write(pdf_output)
            media_type = "application/pdf"
            filename = f"PIDA-AI-Analisis - {timestamp}.pdf"

        file_stream.seek(0)
        return Response(
            content=file_stream.read(),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        error_message = f"Error interno al generar el archivo: {type(e).__name__} -> {str(e)}"
        print(error_message)
        raise HTTPException(status_code=500, detail=error_message)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "PIDA Document Analyzer está funcionando."}
