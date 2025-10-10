# Usa una imagen base de Python 3.12 oficial y ligera
FROM python:3.12-slim

# Variables de entorno para optimizar Python en contenedores
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Establece el directorio de trabajo inicial
WORKDIR /app

# Crea un usuario no-root por seguridad
RUN addgroup --system app && adduser --system --group app

# Actualiza pip
RUN pip install --upgrade pip

# Copia e instala las dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo el código de tu proyecto al contenedor
COPY . .

# --- CAMBIO CLAVE #1: Mueve el directorio de trabajo a la carpeta del código ---
WORKDIR /app/src

# Dale la propiedad de TODOS los archivos al usuario no-root
# Nota: Usamos /app para que la propiedad se aplique a todo
RUN chown -R app:app /app

# Cambia al usuario no-root
USER app

# Expone el puerto que usa Cloud Run
EXPOSE 8080

# --- CAMBIO CLAVE #2: Comando simplificado que se ejecuta DESDE /app/src ---
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
