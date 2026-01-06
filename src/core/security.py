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

# --- EL PORTERO (SOLO AUTENTICACIÓN) ---
async def get_current_user(request: Request):
    """
    Verifica que el token de Firebase ID sea válido.
    Ya no bloquea por dominio o email aquí para permitir el acceso a 
    usuarios que no son VIP pero tienen suscripción de Stripe.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar firma y validez del token
        decoded_token = auth.verify_id_token(token)
        
        # 2. Retornamos el token decodificado
        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(status_code=401, detail="El token ha expirado.")
    except auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Token inválido.")
    except Exception as e:
        log.error(f"Error de autenticación: {e}")
        raise HTTPException(status_code=500, detail="Error de seguridad interno.")
