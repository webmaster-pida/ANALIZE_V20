# Usa una imagen base de Python 3.12 oficial y ligera
FROM python:3.12-slim

# Variables de entorno para optimizar Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Establece el directorio de trabajo
WORKDIR /app

# Crea un usuario no-root por seguridad
RUN addgroup --system app && adduser --system --group app

# Actualiza pip
RUN pip install --upgrade pip

# Copia e instala las dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- EL CAMBIO FUNDAMENTAL ---
# Copia el CONTENIDO de la carpeta 'src' a /app.
# Ahora main.py y la carpeta 'core' estarán en /app.
COPY ./src/ .

# Copia las fuentes a una carpeta 'fonts' dentro de /app
COPY ./fonts/ ./fonts/

# Dale la propiedad de los archivos al usuario no-root
RUN chown -R app:app /app

# Cambia al usuario no-root
USER app

# Expone el puerto que usa Cloud Run
EXPOSE 8080

# --- EL COMANDO SIMPLIFICADO ---
# Como main.py está ahora en /app, el comando es directo.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
