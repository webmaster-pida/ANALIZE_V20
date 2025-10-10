# Usar una imagen base de Python 3.12 oficial
FROM python:3.12-slim

# Establecer el directorio de trabajo
WORKDIR /app

# Crear un usuario y grupo no-root por seguridad
RUN addgroup --system app && adduser --system --group app

# Actualizar pip
RUN pip install --upgrade pip

# Copiar solo los archivos de dependencias primero para aprovechar el caché de Docker
COPY requirements.txt setup.py ./
RUN pip install --no-cache-dir -r requirements.txt

# --- CAMBIO CLAVE: Instalar tu aplicación como un paquete editable ---
# El punto '.' se refiere al directorio actual (/app)
RUN pip install -e .

# Ahora, copiar el resto del código fuente
COPY ./src ./src
COPY ./fonts ./fonts

# Cambiar la propiedad de los archivos al usuario de la aplicación
RUN chown -R app:app /app

# Cambiar al usuario no-root
USER app

# Exponer el puerto
EXPOSE 8080

# --- CAMBIO CLAVE: Volver al comando original y robusto ---
# Como 'src' está ahora instalado como paquete, Python siempre lo encontrará.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
