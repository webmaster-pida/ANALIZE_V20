# Usa una imagen base de Python 3.12 oficial y ligera
FROM python:3.12-slim

# Variables de entorno para optimizar Python en contenedores
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Establece el directorio de trabajo raíz de la aplicación
WORKDIR /app

# Añade el directorio de trabajo a la ruta de búsqueda de Python.
# Esto garantiza que "src" sea siempre un paquete localizable.
ENV PYTHONPATH=/app

# Crea un usuario no-root por seguridad
RUN addgroup --system app && adduser --system --group app

# Actualiza pip
RUN pip install --upgrade pip

# Copia e instala las dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia TODO el código de tu proyecto al contenedor, manteniendo la estructura
COPY . .

# Dale la propiedad de los archivos al usuario no-root
RUN chown -R app:app /app

# Cambia al usuario no-root
USER app

# Expone el puerto que usa Cloud Run
EXPOSE 8080

# El comando de inicio estándar. Gracias al PYTHONPATH, esto ahora funcionará.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
