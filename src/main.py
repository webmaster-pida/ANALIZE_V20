import os
import base64
import requests
import json
import io
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from dotenv import load_dotenv
from docx import Document
from docx.shared import Pt # Importación necesaria para definir el tamaño de la fuente en DOCX
from fpdf import FPDF
from datetime import datetime

from src.core.prompts import ANALYZER_SYSTEM_PROMPT

# Cargar variables de entorno
load_dotenv()

# Configurar la API Key de Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("No se encontró la variable de entorno GEMINI_API_KEY")

GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"

app = FastAPI(title="PIDA Document Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# --- CLASE PDF MODIFICADA ---
# Se cambiaron las fuentes de "WorkSans" a "NotoSans"
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

@app.post("/analyze-documents")
async def analyze_documents(files: List[UploadFile] = File(...), instructions: str = Form(...)):
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Se permite un máximo de 5 archivos.")
    file_parts = []
    for file in files:
        if file.content_type not in ["application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
            raise HTTPException(status_code=400, detail=f"Tipo de archivo no soportado: {file.filename}")
        contents = await file.read()
        encoded_contents = base64.b64encode(contents).decode("utf-8")
        file_parts.append({"inline_data": {"mime_type": file.content_type, "data": encoded_contents}})
    
    prompt_parts = [*file_parts, {"text": f"\n--- \nInstrucciones del Usuario: {instructions}"}]

    generation_config = {}
    
    temp_env = os.getenv("GEMINI_TEMP")
    if temp_env:
        try:
            generation_config["temperature"] = float(temp_env)
        except ValueError:
            print(f"Advertencia: El valor de GEMINI_TEMP ('{temp_env}') no es un número válido. Se ignorará.")

    top_p_env = os.getenv("GEMINI_TOP_P")
    if top_p_env:
        try:
            generation_config["topP"] = float(top_p_env)
        except ValueError:
            print(f"Advertencia: El valor de GEMINI_TOP_P ('{top_p_env}') no es un número válido. Se ignorará.")
            
    request_payload = {
        "contents": [{"parts": prompt_parts}],
        "systemInstruction": {"parts": [{"text": ANALYZER_SYSTEM_PROMPT}]}
    }

    if generation_config:
        request_payload["generationConfig"] = generation_config
        
    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(GEMINI_API_URL, headers=headers, data=json.dumps(request_payload))
        response.raise_for_status()
        response_json = response.json()
        if "candidates" in response_json and response_json["candidates"]:
            first_candidate = response_json["candidates"][0]
            if "content" in first_candidate and "parts" in first_candidate["content"]:
                analysis_text = "".join(part.get("text", "") for part in first_candidate["content"]["parts"])
                return {"analysis": analysis_text}
        raise HTTPException(status_code=500, detail=f"Respuesta inesperada de la API de Gemini: {response.text}")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error al contactar la API de Gemini: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")


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
            
            # --- SECCIÓN DOCX MODIFICADA ---
            # Establecer "Noto Sans" como la fuente por defecto para los estilos
            style = document.styles['Normal']
            font = style.font
            font.name = 'Noto Sans'
            font.size = Pt(11)

            style_h1 = document.styles['Heading 1']
            style_h1.font.name = 'Noto Sans'
            
            style_h2 = document.styles['Heading 2']
            style_h2.font.name = 'Noto Sans'
            # --- FIN DE LA MODIFICACIÓN ---

            document.add_heading("PIDA-AI: Resumen de Consulta", level=1)
            document.add_paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}")
            document.add_heading("Tu Pregunta", level=2)
            document.add_paragraph(instructions)
            document.add_heading("Respuesta de PIDA-AI", level=2)
            document.add_paragraph(analysis_text)
            document.save(file_stream)
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            filename = f"PIDA-AI-Analisis - {timestamp}.docx"
        else: # pdf
            pdf = PDF()
            pdf.alias_nb_pages()
            pdf.add_page()
            
            # --- SECCIÓN PDF MODIFICADA ---
            # Se cambiaron las fuentes de "WorkSans" a "NotoSans"
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
            pdf.set_font("NotoSans", "", 11)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 8, analysis_text)
            
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
