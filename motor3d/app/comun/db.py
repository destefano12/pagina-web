"""Acceso a PostgreSQL (psycopg 3, conexiones en pool)."""
import json
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config

_pool = None


def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=10, kwargs={'row_factory': dict_row}, open=True)
    return _pool


@contextmanager
def cur():
    with pool().connection() as c:
        with c.cursor() as k:
            yield k


def uno(sql, *args):
    with cur() as k:
        k.execute(sql, args)
        return k.fetchone() if k.description else None


def todos(sql, *args):
    with cur() as k:
        k.execute(sql, args)
        return k.fetchall()


def j(v):
    return Jsonb(v)


def evento(trabajo_id, mensaje, nivel='info'):
    uno('insert into eventos (trabajo_id, nivel, mensaje) values (%s, %s, %s)', trabajo_id, nivel, str(mensaje)[:2000])


def migrar(ruta_sql):
    with cur() as k:  # con candado: varias copias de la API pueden arrancar a la vez
        k.execute('select pg_advisory_xact_lock(7303)')
        k.execute(open(ruta_sql, encoding='utf-8').read())
