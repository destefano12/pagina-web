"""API del motor 3D (FastAPI). La usa el panel del restaurante.

Flujo: crear trabajo -> subir fotos (+ recuadros) -> enviar -> [cola] -> procesando -> revisión
       -> el restaurante revisa, ajusta medidas y PUBLICA (nada se publica solo).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import time
import uuid

import redis
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field

from comun import almacen, cola, config, db
from .auth import Usuario, exigir_duenio, exigir_plato, publicar_en_menu, usuario_actual

Image.MAX_IMAGE_PIXELS = 60_000_000

app = FastAPI(title='Motor 3D', version='1.0.0', docs_url='/docs', redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=config.ORIGENES_PERMITIDOS, allow_methods=['GET', 'POST', 'DELETE'],
                   allow_headers=['Authorization', 'Content-Type'], max_age=3600)
_r = redis.Redis.from_url(config.REDIS_URL)
SECRETO = (os.environ.get('SECRETO_FIRMAS') or '').encode()
ACTIVOS = ('subiendo', 'en_cola', 'procesando')


@app.on_event('startup')
def _inicio():
    if not SECRETO or len(SECRETO) < 32:
        raise RuntimeError('Falta SECRETO_FIRMAS (al menos 32 caracteres) en el entorno')
    db.migrar(os.path.join(os.path.dirname(__file__), '..', '..', 'sql', '001_inicial.sql'))
    almacen.preparar_buckets()


# ---------- utilidades ----------
def _limite(u: Usuario):
    k = f'rl:{u.id}:{int(time.time() // 60)}'
    n = _r.incr(k); _r.expire(k, 120)
    if n > config.MAX_PEDIDOS_MINUTO:
        raise HTTPException(429, 'Demasiados pedidos seguidos. Esperá un minuto.')


def _usuario(u: Usuario = Depends(usuario_actual)) -> Usuario:
    _limite(u)
    return u


def _trabajo(tid: str, u: Usuario) -> dict:
    try:
        uuid.UUID(tid)
    except ValueError:
        raise HTTPException(404, 'No existe ese trabajo')
    t = db.uno('select * from trabajos where id = %s', tid)
    if not t:
        raise HTTPException(404, 'No existe ese trabajo')
    exigir_duenio(u, str(t['restaurante_id']))
    return t


def _firmar(tid: str, tipo: str, seg: int = 3600) -> str:
    exp = int(time.time()) + seg
    f = hmac.new(SECRETO, f'{tid}|{tipo}|{exp}'.encode(), hashlib.sha256).digest()
    return f'{exp}.{base64.urlsafe_b64encode(f[:18]).decode()}'


def _ver_firma(tid: str, tipo: str, t: str) -> bool:
    try:
        exp, f = t.split('.', 1)
        if int(exp) < time.time():
            return False
        ok = hmac.new(SECRETO, f'{tid}|{tipo}|{exp}'.encode(), hashlib.sha256).digest()
        return hmac.compare_digest(base64.urlsafe_b64encode(ok[:18]).decode(), f)
    except Exception:
        return False


def _publico(t: dict) -> dict:
    """Lo que ve el panel de un trabajo (con links firmados para la vista previa)."""
    d = {k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if hasattr(v, 'isoformat') else v)
         for k, v in t.items() if k not in ('tarea_celery', 'usuario_id', 'error_tecnico')}
    tid = str(t['id'])
    if t['estado'] in ('revision', 'publicado'):
        base = f'/v1/archivos/{tid}'
        d['vista_previa'] = {tp: f'{base}/{tp}?t={_firmar(tid, tp)}' for tp in ('movil', 'maestro', 'miniatura')}
    return d


def _encolar(tid: str):
    antes = db.uno('select estado, etapa from trabajos where id=%s', tid)
    db.uno("update trabajos set estado='en_cola', etapa='En la cola', progreso=0, etapa_num=0, cancelar=false where id=%s", tid)
    try:
        id_tarea = cola.encolar_reconstruccion(tid)
    except Exception as e:
        db.uno('update trabajos set estado=%s, etapa=%s where id=%s', antes['estado'], antes['etapa'], tid)
        db.evento(tid, f'No se pudo poner en la cola: {e.__class__.__name__}', 'error')
        raise HTTPException(503, 'La cola de reconstrucción no responde. Probá de nuevo en un minuto.')
    db.uno('update trabajos set tarea_celery=%s where id=%s', id_tarea, tid)
    db.evento(tid, 'En la cola de reconstrucción')


class Medidas(BaseModel):
    ancho: float = Field(gt=1, le=150, description='cm')
    prof: float = Field(gt=1, le=150, description='cm')
    alto: float = Field(gt=0.5, le=100, description='cm')


class NuevoTrabajo(BaseModel):
    restaurante_id: uuid.UUID
    plato_id: uuid.UUID
    medidas: Medidas


# ---------- salud ----------
@app.get('/salud')
def salud():
    estado = {}
    try:
        db.uno('select 1'); estado['postgres'] = 'ok'
    except Exception as e:
        estado['postgres'] = f'error: {e.__class__.__name__}'
    try:
        _r.ping(); estado['redis'] = 'ok'
    except Exception as e:
        estado['redis'] = f'error: {e.__class__.__name__}'
    try:
        almacen.s3().head_bucket(Bucket=config.S3_BUCKET_PRIVADO); estado['almacenamiento'] = 'ok'
    except Exception as e:
        estado['almacenamiento'] = f'error: {e.__class__.__name__}'
    try:
        estado['cola'] = int(_r.llen('reconstruccion'))
    except Exception:
        pass
    ok = all(v == 'ok' for k, v in estado.items() if k != 'cola')
    return JSONResponse(estado, status_code=200 if ok else 503)


# ---------- trabajos ----------
@app.post('/v1/trabajos', status_code=201)
def crear(n: NuevoTrabajo, u: Usuario = Depends(_usuario)):
    rid, pid = str(n.restaurante_id), str(n.plato_id)
    exigir_duenio(u, rid)
    exigir_plato(u, rid, pid)
    activos = db.uno('select count(*) c from trabajos where restaurante_id=%s and estado = any(%s)', rid, list(ACTIVOS))['c']
    if activos >= config.MAX_TRABAJOS_ACTIVOS:
        raise HTTPException(409, f'Ya tenés {activos} modelos en proceso. Esperá a que termine alguno.')
    t = db.uno('insert into trabajos (restaurante_id, plato_id, usuario_id, medidas_cargadas) values (%s,%s,%s,%s) returning *',
               rid, pid, u.id, db.j(n.medidas.model_dump()))
    db.evento(t['id'], 'Trabajo creado')
    return _publico(t)


@app.post('/v1/trabajos/{tid}/fotos')
async def subir_fotos(tid: str, fotos: list[UploadFile] = File(...), u: Usuario = Depends(_usuario)):
    t = _trabajo(tid, u)
    if t['estado'] != 'subiendo':
        raise HTTPException(409, 'Este trabajo ya no recibe fotos')
    if len(fotos) > 20:
        raise HTTPException(413, 'Mandá las fotos de a 20 como máximo por pedido')
    # primero se valida todo el lote; recién después se reservan los números de foto de forma atómica
    lote = []
    for f in fotos:
        datos = await f.read(config.MAX_MB_FOTO * 1024 * 1024 + 1)
        if len(datos) > config.MAX_MB_FOTO * 1024 * 1024:
            raise HTTPException(413, f'Cada foto puede pesar hasta {config.MAX_MB_FOTO} MB')
        # que sea de verdad una imagen JPG/PNG (no alcanza con el nombre o el tipo que dice el navegador)
        try:
            with Image.open(io.BytesIO(datos)) as im:
                fmt = im.format; im.verify()
            with Image.open(io.BytesIO(datos)) as im:
                w, h = im.size
        except Exception:
            raise HTTPException(415, f'"{f.filename}" no es una imagen válida')
        if fmt not in ('JPEG', 'PNG'):
            raise HTTPException(415, 'Solo se aceptan fotos JPG o PNG')
        if max(w, h) < 1600:  # igual que el control de calidad del motor (validacion.MIN_LADO)
            raise HTTPException(422, f'"{f.filename}" es muy chica ({w}×{h}); hacen falta al menos 1600 px de lado')
        lote.append((datos, fmt))
    fila = db.uno("update trabajos set fotos_subidas = fotos_subidas + %s where id=%s and estado='subiendo' "
                  "and fotos_subidas + %s <= %s returning fotos_subidas", len(lote), tid, len(lote), config.MAX_FOTOS)
    if not fila:
        raise HTTPException(413, f'Como máximo {config.MAX_FOTOS} fotos por modelo')
    n = fila['fotos_subidas'] - len(lote)
    guardadas = []
    for datos, fmt in lote:
        n += 1
        nombre = f'{n:03d}.{"jpg" if fmt == "JPEG" else "png"}'
        clave = f'trabajos/{tid}/fotos/{nombre}'
        tam, sha = almacen.subir_bytes(config.S3_BUCKET_PRIVADO, clave, datos, 'image/jpeg' if fmt == 'JPEG' else 'image/png')
        db.uno('insert into archivos (trabajo_id, tipo, clave, bytes, sha256) values (%s,%s,%s,%s,%s)', tid, 'foto', clave, tam, sha)
        guardadas.append(nombre)
    return {'fotos_subidas': fila['fotos_subidas'], 'guardadas': guardadas}


@app.post('/v1/trabajos/{tid}/recuadros')
async def recuadros(tid: str, req: Request, u: Usuario = Depends(_usuario)):
    """Recuadro del plato en cada foto {"001.jpg": [x0,y0,x1,y1]} en proporciones 0..1."""
    t = _trabajo(tid, u)
    if t['estado'] != 'subiendo':
        raise HTTPException(409, 'Este trabajo ya no recibe datos')
    cuerpo = await req.body()
    if len(cuerpo) > 200_000:
        raise HTTPException(413, 'Demasiados datos')
    try:
        cj = json.loads(cuerpo)
        limpio = {}
        for k, v in cj.items():
            if not (isinstance(k, str) and len(k) <= 12 and k.replace('.', '').isalnum()):
                continue
            x0, y0, x1, y1 = (float(a) for a in v)
            if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1:
                limpio[k] = [x0, y0, x1, y1]
    except Exception:
        raise HTTPException(422, 'Recuadros inválidos')
    clave = f'trabajos/{tid}/cajas.json'
    tam, sha = almacen.subir_bytes(config.S3_BUCKET_PRIVADO, clave, json.dumps(limpio).encode(), 'application/json')
    db.uno('insert into archivos (trabajo_id, tipo, clave, bytes, sha256) values (%s,%s,%s,%s,%s) on conflict (clave) do update set bytes=excluded.bytes, sha256=excluded.sha256',
           tid, 'cajas', clave, tam, sha)
    return {'recuadros': len(limpio)}


@app.post('/v1/trabajos/{tid}/enviar')
def enviar(tid: str, u: Usuario = Depends(_usuario)):
    t = _trabajo(tid, u)
    if t['estado'] != 'subiendo':
        raise HTTPException(409, 'Este trabajo ya fue enviado')
    hay = db.uno("select count(*) c from archivos where trabajo_id=%s and tipo='foto' and borrado is null", tid)['c']
    if hay < config.MIN_FOTOS:
        raise HTTPException(422, f'Hacen falta al menos {config.MIN_FOTOS} fotos (llegaron {hay})')
    db.uno('update trabajos set fotos_subidas=%s where id=%s', hay, tid)
    _encolar(tid)
    return _publico(db.uno('select * from trabajos where id=%s', tid))


@app.get('/v1/trabajos')
def listar(restaurante_id: uuid.UUID, plato_id: uuid.UUID | None = None, u: Usuario = Depends(_usuario)):
    exigir_duenio(u, str(restaurante_id))
    if plato_id:
        filas = db.todos("select * from trabajos where restaurante_id=%s and plato_id=%s and estado <> 'descartado' order by creado desc limit 50", str(restaurante_id), str(plato_id))
    else:
        filas = db.todos("select * from trabajos where restaurante_id=%s and estado <> 'descartado' order by creado desc limit 200", str(restaurante_id))
    return [_publico(t) for t in filas]


@app.get('/v1/trabajos/{tid}')
def detalle(tid: str, u: Usuario = Depends(_usuario)):
    t = _trabajo(tid, u)
    d = _publico(t)
    d['eventos'] = [{'momento': e['momento'].isoformat(), 'nivel': e['nivel'], 'mensaje': e['mensaje']}
                    for e in db.todos('select momento, nivel, mensaje from eventos where trabajo_id=%s order by id desc limit 60', tid)]
    d['archivos'] = db.todos("select tipo, count(*) n, sum(bytes) bytes from archivos where trabajo_id=%s and borrado is null group by tipo", tid)
    return d


@app.post('/v1/trabajos/{tid}/cancelar')
def cancelar(tid: str, u: Usuario = Depends(_usuario)):
    t = _trabajo(tid, u)
    if t['estado'] not in ACTIVOS:
        raise HTTPException(409, 'Ese trabajo no está en proceso')
    if t['estado'] == 'procesando':
        db.uno('update trabajos set cancelar=true where id=%s', tid)  # el motor lo corta en segundos
        db.evento(tid, 'Cancelación pedida por el restaurante')
    else:
        if t['tarea_celery']:
            cola.revocar(t['tarea_celery'])
        db.uno("update trabajos set estado='cancelado', etapa='Cancelado', terminado=now() where id=%s", tid)
        db.evento(tid, 'Cancelado por el restaurante')
    return _publico(db.uno('select * from trabajos where id=%s', tid))


@app.post('/v1/trabajos/{tid}/reprocesar')
def reprocesar(tid: str, u: Usuario = Depends(_usuario)):
    t = _trabajo(tid, u)
    if t['estado'] not in ('fallo', 'cancelado', 'revision'):
        raise HTTPException(409, 'Ese trabajo no se puede volver a procesar ahora')
    fotos = db.uno("select count(*) c from archivos where trabajo_id=%s and tipo='foto' and borrado is null", tid)['c']
    if fotos < config.MIN_FOTOS:
        raise HTTPException(410, 'Las fotos de ese trabajo ya se borraron: hay que volver a escanear')
    db.uno('update trabajos set metricas=null, puntaje=null, aprobable=null, motivos=null, error=null, error_tecnico=null, medidas_modelo=null where id=%s', tid)
    _encolar(tid)
    return _publico(db.uno('select * from trabajos where id=%s', tid))


@app.post('/v1/trabajos/{tid}/publicar')
def publicar(tid: str, m: Medidas, u: Usuario = Depends(_usuario)):
    """El restaurante revisó el modelo, confirmó las medidas reales y lo publica en el menú."""
    t = _trabajo(tid, u)
    if t['estado'] != 'revision':
        raise HTTPException(409, 'Ese modelo no está listo para revisar')
    if not t['aprobable']:
        raise HTTPException(422, 'Este modelo no pasó el control de calidad y no se puede publicar. Volvé a escanear el plato.')
    tid_s, rid, pid = str(t['id']), str(t['restaurante_id']), str(t['plato_id'])
    # se copia la versión para celular y la miniatura al almacenamiento público
    clave_pub = f'{rid}/{pid}/{tid_s}.glb'
    datos = almacen.leer(config.S3_BUCKET_PRIVADO, f'trabajos/{tid_s}/salida/movil.glb')
    almacen.subir_bytes(config.S3_BUCKET_PUBLICO, clave_pub, datos, 'model/gltf-binary', cache='public, max-age=31536000, immutable')
    try:
        mini = almacen.leer(config.S3_BUCKET_PRIVADO, f'trabajos/{tid_s}/salida/miniatura.jpg')
        almacen.subir_bytes(config.S3_BUCKET_PUBLICO, f'{rid}/{pid}/{tid_s}.jpg', mini, 'image/jpeg', cache='public, max-age=31536000, immutable')
    except Exception:
        pass
    url = f'{config.URL_PUBLICA_MODELOS.rstrip("/")}/{clave_pub}'
    publicar_en_menu(u, rid, pid, url, m.model_dump())
    anteriores = db.todos("select id from trabajos where plato_id=%s and estado='publicado' and id<>%s", pid, tid_s)
    for a in anteriores:
        db.uno("update trabajos set estado='descartado', etapa='Reemplazado' where id=%s", a['id'])
        db.evento(a['id'], f'Reemplazado por el modelo {tid_s}')
    db.uno("update trabajos set estado='publicado', etapa='Publicado', publicado_en=now(), medidas_publicadas=%s where id=%s", db.j(m.model_dump()), tid_s)
    db.evento(tid_s, f'Publicado en el menú con medidas {m.ancho} × {m.prof} × {m.alto} cm')
    return _publico(db.uno('select * from trabajos where id=%s', tid_s)) | {'url_publica': url}


@app.delete('/v1/trabajos/{tid}')
def eliminar(tid: str, u: Usuario = Depends(_usuario)):
    """Descarta un modelo en revisión o fallido, o quita del menú el publicado."""
    t = _trabajo(tid, u)
    if t['estado'] in ACTIVOS:
        raise HTTPException(409, 'Primero cancelá el trabajo')
    if t['estado'] == 'publicado':
        publicar_en_menu(u, str(t['restaurante_id']), str(t['plato_id']), None, None)
    db.uno("update trabajos set estado='descartado', etapa='Borrado', terminado=coalesce(terminado, now()) where id=%s", tid)
    db.evento(tid, 'Borrado por el restaurante (los archivos se eliminan en la limpieza diaria)')
    return {'ok': True}


@app.get('/v1/archivos/{tid}/{tipo}')
def archivo(tid: str, tipo: str, t: str):
    """Archivos privados para la vista previa en el panel, con link firmado y vencimiento."""
    if tipo not in ('movil', 'maestro', 'miniatura') or not _ver_firma(tid, tipo, t):
        raise HTTPException(403, 'Link vencido o inválido')
    nombre = {'movil': 'movil.glb', 'maestro': 'maestro.glb', 'miniatura': 'miniatura.jpg'}[tipo]
    try:
        obj = almacen.s3().get_object(Bucket=config.S3_BUCKET_PRIVADO, Key=f'trabajos/{tid}/salida/{nombre}')
    except Exception:
        raise HTTPException(404, 'No existe ese archivo')
    tipo_mime = 'image/jpeg' if tipo == 'miniatura' else 'model/gltf-binary'
    return StreamingResponse(obj['Body'].iter_chunks(1 << 16), media_type=tipo_mime,
                             headers={'Cache-Control': 'private, max-age=600', 'Content-Length': str(obj['ContentLength'])})
