"""Worker de reconstrucción (Celery + Redis).

- Una reconstrucción por worker a la vez (concurrency=1, prefetch=1): es un trabajo pesado de CPU/GPU.
- acks_late: si el worker se cae a mitad de camino, la tarea vuelve a la cola en vez de perderse.
- Reintento controlado: 1 reintento SOLO para errores técnicos. Un problema de las fotos no se reintenta
  (se le explica al restaurante qué tiene que volver a sacar).
- Tareas programadas (beat): limpieza diaria de fotos/archivos viejos y vigilancia de trabajos trabados.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from celery.schedules import crontab

from comun import almacen, config, db
from motor.geometria import ErrorFoto
from motor.pipeline import Cancelado, Config, reconstruir as correr_motor

LIMITE_S = int(os.environ.get('LIMITE_TRABAJO_S', str(3 * 3600)))
TMP = os.environ.get('CARPETA_TEMPORAL', tempfile.gettempdir())

celery_app = Celery('motor3d', broker=config.REDIS_URL, backend=config.REDIS_URL)
celery_app.conf.update(
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    task_default_queue='reconstruccion', task_track_started=True, result_expires=24 * 3600,
    broker_transport_options={'visibility_timeout': LIMITE_S + 1800},
    task_soft_time_limit=LIMITE_S, task_time_limit=LIMITE_S + 300,
    timezone='UTC',
    beat_schedule={
        'limpieza-diaria': {'task': 'worker.tareas.limpiar', 'schedule': crontab(hour=6, minute=17), 'options': {'queue': 'mantenimiento'}},
        'vigilar-trabados': {'task': 'worker.tareas.vigilar', 'schedule': 600.0, 'options': {'queue': 'mantenimiento'}},
    },
)

TIPOS = {'.glb': 'model/gltf-binary', '.jpg': 'image/jpeg', '.log': 'text/plain; charset=utf-8', '.json': 'application/json'}


def _guardar_archivo(tid, tipo, clave, ruta):
    ext = os.path.splitext(ruta)[1]
    tam, sha = almacen.subir_archivo(config.S3_BUCKET_PRIVADO, clave, ruta, TIPOS.get(ext, 'application/octet-stream'))
    db.uno('insert into archivos (trabajo_id, tipo, clave, bytes, sha256) values (%s,%s,%s,%s,%s) '
           'on conflict (clave) do update set bytes=excluded.bytes, sha256=excluded.sha256, borrado=null, creado=now()',
           tid, tipo, clave, tam, sha)


class _Aviso:
    """Guarda el avance real en la base sin martillarla: como mucho cada 2 s, o al cambiar de etapa."""

    def __init__(self, tid):
        self.tid, self.ult, self.etapa, self.t_cancel = tid, 0.0, None, 0.0
        self._cancel = False

    def __call__(self, pct, n, tot, txt):
        ahora = time.time()
        if n != self.etapa or ahora - self.ult >= 2:
            db.uno('update trabajos set progreso=%s, etapa_num=%s, etapas_total=%s, etapa=%s where id=%s',
                   int(pct), int(n), int(tot), str(txt)[:200], self.tid)
            if n != self.etapa:
                db.evento(self.tid, f'Etapa {n} de {tot}: {txt}')
            self.ult, self.etapa = ahora, n

    def cancelado(self):
        ahora = time.time()
        if ahora - self.t_cancel >= 3:
            fila = db.uno('select cancelar from trabajos where id=%s', self.tid)
            self._cancel = bool(fila and fila['cancelar'])
            self.t_cancel = ahora
        return self._cancel


@celery_app.task(bind=True, name='worker.tareas.reconstruir', max_retries=1)
def reconstruir(self, tid: str):
    t = db.uno('select * from trabajos where id=%s', tid)
    if not t or t['estado'] not in ('en_cola', 'procesando'):
        return {'omitido': True}  # cancelado o borrado mientras esperaba
    if t['cancelar']:
        db.uno("update trabajos set estado='cancelado', etapa='Cancelado', terminado=now() where id=%s", tid)
        return {'cancelado': True}
    db.uno("update trabajos set estado='procesando', etapa='Arrancando', iniciado=now(), terminado=null, "
           "intentos=intentos+1, error=null, error_tecnico=null where id=%s", tid)
    db.evento(tid, f'Empezó la reconstrucción (intento {t["intentos"] + 1})')

    carpeta = tempfile.mkdtemp(prefix=f'trabajo-{tid}-', dir=TMP)
    fotos_dir, trabajo = os.path.join(carpeta, 'fotos'), os.path.join(carpeta, 'trabajo')
    os.makedirs(fotos_dir); os.makedirs(trabajo)
    av = _Aviso(tid)
    try:
        av(0, 1, 13, 'Bajando las fotos')
        fotos = db.todos("select clave from archivos where trabajo_id=%s and tipo='foto' and borrado is null order by clave", tid)
        for f in fotos:
            almacen.bajar(config.S3_BUCKET_PRIVADO, f['clave'], os.path.join(fotos_dir, os.path.basename(f['clave'])))
        cajas = None
        c = db.uno("select clave from archivos where trabajo_id=%s and tipo='cajas' and borrado is null", tid)
        if c:
            try:
                cajas = json.loads(almacen.leer(config.S3_BUCKET_PRIVADO, c['clave']))
            except Exception:
                cajas = None

        r = correr_motor(fotos_dir, trabajo, t['medidas_cargadas'], av, av.cancelado, cajas, Config.desde_entorno())

        rutas = r['archivos']['rutas']
        _guardar_archivo(tid, 'maestro', f'trabajos/{tid}/salida/maestro.glb', rutas['maestro'])
        _guardar_archivo(tid, 'movil', f'trabajos/{tid}/salida/movil.glb', rutas['movil'])
        if rutas.get('miniatura'):
            _guardar_archivo(tid, 'miniatura', f'trabajos/{tid}/salida/miniatura.jpg', rutas['miniatura'])
        _subir_registro(tid, trabajo)
        metricas = r['metricas'] | {'archivos': {k: v for k, v in r['archivos'].items() if k != 'rutas'}}
        db.uno("update trabajos set estado='revision', etapa=%s, progreso=100, etapa_num=13, fotos_usadas=%s, "
               "medidas_modelo=%s, metricas=%s, puntaje=%s, aprobable=%s, motivos=%s, etapas=%s, terminado=now() where id=%s",
               'Listo para revisar' if r['aprobable'] else 'No pasó el control de calidad',
               metricas.get('fotos_usadas'), db.j(r['medidas_cm']), db.j(_json(metricas)), r['puntaje'], r['aprobable'],
               db.j(r['motivos']), db.j(r['etapas']), tid)
        db.evento(tid, f'Modelo generado. Puntaje {r["puntaje"]}/100' + ('' if r['aprobable'] else ' (no se puede publicar)'),
                  'info' if r['aprobable'] else 'aviso')
        return {'puntaje': r['puntaje'], 'aprobable': r['aprobable']}

    except Cancelado:
        db.uno("update trabajos set estado='cancelado', etapa='Cancelado', terminado=now() where id=%s", tid)
        db.evento(tid, 'Cancelado durante el proceso', 'aviso')
        return {'cancelado': True}
    except ErrorFoto as e:  # problema de las fotos: se explica, no se reintenta
        _subir_registro(tid, trabajo)
        db.uno("update trabajos set estado='fallo', etapa='No se pudo armar el modelo', error=%s, terminado=now() where id=%s", str(e), tid)
        db.evento(tid, str(e), 'error')
        return {'fallo': str(e)}
    except SoftTimeLimitExceeded:
        _subir_registro(tid, trabajo)
        _fallo_tecnico(tid, 'Se pasó del tiempo máximo de proceso', f'límite {LIMITE_S} s')
        return {'fallo': 'tiempo'}
    except Exception as e:
        tec = traceback.format_exc()[-4000:]
        _subir_registro(tid, trabajo)
        if self.request.retries < self.max_retries:
            db.uno("update trabajos set estado='en_cola', etapa='Reintentando después de un error técnico', error_tecnico=%s where id=%s", tec, tid)
            db.evento(tid, f'Error técnico, se reintenta una vez: {e}', 'aviso')
            raise self.retry(exc=e, countdown=60, queue='reconstruccion')
        _fallo_tecnico(tid, 'Falló el proceso por un error técnico. Ya quedó registrado; probá reprocesar más tarde.', tec)
        return {'fallo': str(e)}
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)  # nada de temporales colgados


def _fallo_tecnico(tid, mensaje, tecnico):
    db.uno("update trabajos set estado='fallo', etapa='Error técnico', error=%s, error_tecnico=%s, terminado=now() where id=%s",
           mensaje, str(tecnico)[-4000:], tid)
    db.evento(tid, mensaje, 'error')


def _subir_registro(tid, trabajo):
    ruta = os.path.join(trabajo, 'registro.log')
    try:
        if os.path.exists(ruta):
            _guardar_archivo(tid, 'registro', f'trabajos/{tid}/registro.log', ruta)
    except Exception:
        pass


def _json(v):
    return json.loads(json.dumps(v, default=float))


@celery_app.task(name='worker.tareas.limpiar')
def limpiar():
    """Borra las fotos de trabajos terminados hace más de DIAS_GUARDAR_FOTOS días, y todos los archivos de los descartados.
    Lo publicado (bucket público) y la versión maestra de lo publicado NO se tocan."""
    dias = config.DIAS_GUARDAR_FOTOS
    filas = db.todos(
        "select a.id, a.clave from archivos a join trabajos t on t.id=a.trabajo_id where a.borrado is null and ("
        " (a.tipo in ('foto','cajas') and t.estado in ('publicado','descartado','fallo','cancelado','revision') "
        "  and coalesce(t.terminado, t.actualizado) < now() - make_interval(days => %s))"
        " or (t.estado='descartado' and t.actualizado < now() - make_interval(days => %s)))", dias, dias)
    if filas:
        almacen.borrar(config.S3_BUCKET_PRIVADO, [f['clave'] for f in filas])
        db.uno('update archivos set borrado=now() where id = any(%s)', [f['id'] for f in filas])
    # trabajos que nunca se enviaron (se quedaron "subiendo" más de 2 días)
    viejos = db.todos("select id from trabajos where estado='subiendo' and creado < now() - interval '2 days'")
    for v in viejos:
        db.uno("update trabajos set estado='descartado', etapa='Abandonado sin enviar' where id=%s", v['id'])
        db.evento(v['id'], 'Descartado: se empezó a subir y nunca se envió')
    # temporales que hayan quedado de un worker caído
    n_tmp = 0
    for d in os.listdir(TMP):
        p = os.path.join(TMP, d)
        if d.startswith('trabajo-') and os.path.isdir(p) and time.time() - os.path.getmtime(p) > LIMITE_S + 3600:
            shutil.rmtree(p, ignore_errors=True); n_tmp += 1
    return {'archivos_borrados': len(filas), 'abandonados': len(viejos), 'temporales': n_tmp}


@celery_app.task(name='worker.tareas.vigilar')
def vigilar():
    """Trabajos que dicen 'procesando' pero no avanzan hace más de 30 minutos (worker muerto): se marcan como fallo."""
    trabados = db.todos("select id from trabajos where estado='procesando' and actualizado < now() - interval '30 minutes'")
    for t in trabados:
        _fallo_tecnico(t['id'], 'El proceso se detuvo sin terminar. Probá reprocesar.', 'sin avance por 30 minutos (vigilancia)')
    # en la cola pero sin tarea (la cola no respondió al enviarlo): se vuelve a encolar
    huerfanos = db.todos("select id from trabajos where estado='en_cola' and tarea_celery is null and actualizado < now() - interval '5 minutes'")
    for t in huerfanos:
        r = reconstruir.apply_async(args=[str(t['id'])], queue='reconstruccion')
        db.uno('update trabajos set tarea_celery=%s where id=%s', r.id, t['id'])
        db.evento(t['id'], 'Vuelto a poner en la cola (vigilancia)', 'aviso')
    return {'trabados': len(trabados), 'reencolados': len(huerfanos)}
