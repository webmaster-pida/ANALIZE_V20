# src/core/security.py

import json
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from src.config import settings, log

# --- Inicialización de Firebase Admin ---
# Esto es seguro en Cloud Run, ya que usa las credenciales del entorno.
try:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
except ValueError:
    # Esto evita que la app crashee si se inicializa múltiples veces (común en desarrollo)
    pass

# Esta instancia no se usa para validar, solo para que FastAPI muestre el candado en la documentación.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- EL PORTERO ---
async def get_current_user(request: Request):
    """
    Dependencia para verificar el token de Firebase ID enviado en la cabecera Authorization.
    Aplica reglas de seguridad basadas en Dominios y Correos permitidos configurados en Cloud Run.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar el token usando el SDK de Firebase Admin
        decoded_token = auth.verify_id_token(token)
        
        # 2. Obtener datos del usuario para validación
        email = decoded_token.get("email", "").lower()
        domain = email.split("@")[1] if "@" in email else ""
        
        # 3. Cargar reglas de acceso desde la configuración (Variables de Entorno)
        try:
            # settings.ADMIN_DOMAINS viene como string JSON -> lo convertimos a lista
            allowed_domains = json.loads(settings.ADMIN_DOMAINS)
            # settings.ADMIN_EMAILS igual -> lo convertimos a lista y normalizamos a minúsculas
            allowed_emails = [e.lower() for e in json.loads(settings.ADMIN_EMAILS)]
        except Exception as e:
            log.error(f"Error al procesar las listas de control de acceso en Analize: {e}")
            allowed_domains = []
            allowed_emails = []

        # 4. APLICAR LÓGICA DE SEGURIDAD
        # Solo aplicamos el filtro si existe AL MENOS UNA regla configurada.
        has_restrictions = bool(allowed_domains or allowed_emails)
        
        if has_restrictions:
            # El usuario pasa si su dominio está en la lista O si su email específico está en la lista
            is_domain_authorized = domain in allowed_domains
            is_email_authorized = email in allowed_emails
            
            if not (is_domain_authorized or is_email_authorized):
                log.warning(f"ACCESO DENEGADO (Analizador): El usuario {email} intentó entrar pero no está autorizado.")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes autorización para acceder a este servicio. Contacta al administrador."
                )

        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token ha expirado.",
        )
    except auth.InvalidIdTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"El token es inválido: {e}",
        )
    except HTTPException as he:
        # Re-lanzamos las excepciones HTTP propias (como el 403 Forbidden creado arriba)
        raise he
    except Exception as e:
        log.error(f"Error inesperado durante la verificación del token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error inesperado de seguridad.",
        )
