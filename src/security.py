# src/core/security.py

import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

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
    Devuelve el diccionario con los datos decodificados del usuario.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # Verificar el token usando el SDK de Firebase Admin
        decoded_token = auth.verify_id_token(token)
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
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error inesperado durante la verificación del token: {e}",
        )
