"""Cliente liviano de la cola (la API encola por nombre de tarea, sin cargar el motor)."""
from celery import Celery

from . import config

cola = Celery('motor3d', broker=config.REDIS_URL, backend=config.REDIS_URL)


def encolar_reconstruccion(tid: str) -> str:
    return cola.send_task('worker.tareas.reconstruir', args=[tid], queue='reconstruccion').id


def revocar(id_tarea: str):
    cola.control.revoke(id_tarea)
