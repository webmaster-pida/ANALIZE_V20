# Usar una imagen base de Python 3.12 oficial
FROM python:3.12-slim

# Establecer el directorio de trabajo
WORKDIR /app

# Crear un usuario y grupo no-root por seguridad
RUN addgroup --system app && adduser --system --group app

# Actualizar pip
RUN pip install --upgrade pip

# Copiar y instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo tu código fuente
COPY . .

# --- CAMBIO CLAVE Y ÚNICO ---
# Añade el directorio de trabajo actual al PYTHONPATH.
# Esto hace que 'src' sea importable desde cualquier lugar.
ENV PYTHONPATH="${PYTHONPATH}:/app"

# Cambiar la propiedad de los archivos al usuario de la aplicación
RUN chown -R app:app /app

# Cambiar al usuario no-root
USER app

# Exponer el puerto
EXPOSE 8080

# Usar el comando de inicio estándar y robusto
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
