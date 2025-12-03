# src/core/security.py

import json
import os
import logging
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# Configuración básica de logs
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pida-security")

# --- Inicialización de Firebase Admin ---
try:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
except ValueError:
    pass

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- EL PORTERO ---
async def get_current_user(request: Request):
    """
    Verifica el token y aplica reglas de seguridad (Dominios y Emails)
    leyendo directamente de las variables de entorno.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar firma del token
        decoded_token = auth.verify_id_token(token)
        
        # 2. Obtener datos del usuario
        email = decoded_token.get("email", "").lower()
        domain = email.split("@")[1] if "@" in email else ""
        
        # 3. Leer reglas directamente del Entorno (Sin src.config)
        raw_domains = os.getenv("ADMIN_DOMAINS", '[]')
        raw_emails = os.getenv("ADMIN_EMAILS", '[]')

        try:
            allowed_domains = json.loads(raw_domains)
            allowed_emails = [e.lower() for e in json.loads(raw_emails)]
        except Exception as e:
            log.error(f"Error procesando reglas de seguridad: {e}")
            allowed_domains = []
            allowed_emails = []

        # 4. Aplicar filtro si existen reglas
        has_restrictions = bool(allowed_domains or allowed_emails)
        
        if has_restrictions:
            is_domain_authorized = domain in allowed_domains
            is_email_authorized = email in allowed_emails
            
            if not (is_domain_authorized or is_email_authorized):
                log.warning(f"ACCESO DENEGADO: {email}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes autorización para usar este servicio."
                )

        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(status_code=401, detail="El token ha expirado.")
    except auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Token inválido.")
    except HTTPException as he:
        raise he
    except Exception as e:
        log.error(f"Error de autenticación: {e}")
        raise HTTPException(status_code=500, detail="Error de seguridad interno.")
