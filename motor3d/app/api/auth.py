"""Autenticación: el panel manda el token de sesión del dueño del restaurante (Supabase Auth).
Se verifica la firma del token y se confirma con la plataforma que ese usuario es dueño del restaurante."""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Header, HTTPException

from comun import config

_jwks = None
_cache_duenio: dict = {}


@dataclass
class Usuario:
    id: str
    token: str


def _verificar(token: str) -> dict:
    global _jwks
    try:
        cab = jwt.get_unverified_header(token)
        if config.MODO_PRUEBA and config.PRUEBA_SECRETO:
            return jwt.decode(token, config.PRUEBA_SECRETO, algorithms=['HS256'], audience='authenticated')
        if cab.get('alg') == 'HS256':
            if not config.SUPABASE_JWT_SECRET:
                raise HTTPException(401, 'Token HS256 sin secreto configurado')
            return jwt.decode(token, config.SUPABASE_JWT_SECRET, algorithms=['HS256'], audience='authenticated')
        if _jwks is None:
            _jwks = jwt.PyJWKClient(f'{config.SUPABASE_URL}/auth/v1/.well-known/jwks.json', cache_keys=True, lifespan=3600)
        clave = _jwks.get_signing_key_from_jwt(token)
        return jwt.decode(token, clave.key, algorithms=['ES256', 'RS256'], audience='authenticated')
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, 'La sesión venció: volvé a entrar al panel')
    except Exception:
        raise HTTPException(401, 'Sesión inválida')


def usuario_actual(authorization: str = Header(default='')) -> Usuario:
    token = authorization.removeprefix('Bearer ').strip()
    if not token:
        raise HTTPException(401, 'Falta iniciar sesión')
    datos = _verificar(token)
    if not datos.get('sub'):
        raise HTTPException(401, 'Sesión inválida')
    return Usuario(id=datos['sub'], token=token)


def _rest(u: Usuario):
    return httpx.Client(base_url=f'{config.SUPABASE_URL}/rest/v1', timeout=15,
                        headers={'apikey': config.SUPABASE_ANON_KEY, 'Authorization': f'Bearer {u.token}', 'Content-Type': 'application/json'})


def exigir_duenio(u: Usuario, restaurante_id: str):
    """La plataforma (con sus propias reglas de seguridad) dice si el usuario es dueño del restaurante."""
    if config.MODO_PRUEBA:
        permitidos = {x.strip() for x in __import__('os').environ.get('PRUEBA_RESTAURANTES', '').split(',')}
        if restaurante_id not in permitidos:
            raise HTTPException(403, 'No sos dueño de ese restaurante')
        return
    k = (u.id, restaurante_id)
    if _cache_duenio.get(k, 0) > time.time():
        return
    with _rest(u) as c:
        r = c.post('/rpc/es_duenio', json={'rid': restaurante_id})
    if r.status_code != 200 or r.json() is not True:
        raise HTTPException(403, 'No sos dueño de ese restaurante')
    _cache_duenio[k] = time.time() + 300


def exigir_plato(u: Usuario, restaurante_id: str, plato_id: str) -> dict:
    if config.MODO_PRUEBA:
        return {'id': plato_id, 'nombre': 'Plato de prueba'}
    with _rest(u) as c:
        r = c.get('/platos', params={'id': f'eq.{plato_id}', 'restaurante_id': f'eq.{restaurante_id}', 'select': 'id,nombre,ancho_cm,prof_cm,alto_cm,modelo_url'})
    if r.status_code != 200 or not r.json():
        raise HTTPException(404, 'Ese plato no existe en tu restaurante')
    return r.json()[0]


def publicar_en_menu(u: Usuario, restaurante_id: str, plato_id: str, url: str | None, medidas: dict | None):
    """Pone (o quita, con url=None) el modelo en el plato del menú, con las medidas confirmadas."""
    if config.MODO_PRUEBA:
        return
    cuerpo = {'modelo_url': url}
    if medidas:
        cuerpo.update({'ancho_cm': medidas['ancho'], 'prof_cm': medidas['prof'], 'alto_cm': medidas['alto']})
    with _rest(u) as c:
        r = c.patch('/platos', params={'id': f'eq.{plato_id}', 'restaurante_id': f'eq.{restaurante_id}'}, json=cuerpo,
                    headers={'Prefer': 'return=representation'})
    if r.status_code not in (200, 204) or (r.status_code == 200 and not r.json()):
        raise HTTPException(502, 'No se pudo actualizar el plato en el menú')
