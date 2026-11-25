"""Motor de reconstrucción 3D: fotos de un plato -> modelo GLB real, medido y con puntaje de calidad.

Etapas (cada una informa su avance REAL, leído de la salida de cada programa):
  1 Recepción            2 Validación de fotos     3 Puntos característicos   4 Unión de fotos
  5 Posición de cámaras  6 Cobertura de ángulos    7 Preparación de fotos     8 Nube densa
  9 Malla                10 Separación del objeto  11 Textura                 12 Limpieza y escala
  13 Exportación y validación del GLB

Herramientas: COLMAP (BSD) para ubicar las cámaras y OpenMVS (AGPL-3.0, sin modificar) para la nube
densa, la malla y la textura. Ver DOCUMENTACION_MOTOR_3D.md.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

import numpy as np
import trimesh

from . import calidad, exportar, validacion
from .geometria import (ErrorFoto, _arriba_camaras, _arriba_por_vuelta, _bin, _memoria_libre_mb, _recortar_objeto, limpiar)

# Etapas: (nombre visible, peso en el total). Los pesos salen de tiempos medidos en CPU de 4 núcleos.
ETAPAS = [
    ('Recibiendo las fotos', 1),
    ('Revisando la calidad de las fotos', 2),
    ('Buscando puntos en las fotos', 7),
    ('Uniendo las fotos entre sí', 15),
    ('Calculando dónde estaba la cámara', 4),
    ('Revisando que estén todos los ángulos', 1),
    ('Preparando las fotos', 2),
    ('Armando la nube de puntos densa', 38),
    ('Creando la superficie', 9),
    ('Separando el plato de la mesa y del fondo', 2),
    ('Pintando la textura', 10),
    ('Limpiando y poniendo el tamaño real', 5),
    ('Generando las versiones para celular y validando', 4),
]
_PESO_TOTAL = sum(p for _, p in ETAPAS)


class Cancelado(Exception):
    """El restaurante canceló el trabajo."""


@dataclass
class Config:
    colmap: str = 'colmap'
    mvs_dir: str | None = None
    gpu: bool = False
    hilos: int = 0                 # 0 = todos
    tam_foto: int = 4096           # tamaño de las fotos para textura
    tam_puntos: int = 2400         # tamaño para buscar puntos (más grande no mejora y llena la memoria)
    densa_max: int = 1600          # resolución máxima para la nube densa
    densa_nivel: int = 1
    textura_max: int = 4096
    limite_paso_s: int = 3 * 3600  # ningún paso puede colgarse para siempre

    @classmethod
    def desde_entorno(cls):
        e = os.environ.get
        return cls(colmap=e('COLMAP_BIN', 'colmap'), mvs_dir=e('MVS_DIR') or None, gpu=e('USAR_GPU', '0') == '1',
                   hilos=int(e('HILOS', '0')), densa_max=int(e('DENSA_MAX', '1600')), textura_max=int(e('TEXTURA_MAX', '4096')))


class Avance:
    """Convierte (etapa, fracción real dentro de la etapa) en un porcentaje total que nunca retrocede."""

    def __init__(self, avisar, cancelado):
        self._avisar, self._cancelado = avisar, cancelado
        self.etapa, self.pct, self.tiempos, self._t0 = 0, 0, [], time.time()

    def entrar(self, n: int):
        if self.etapa:
            self.tiempos.append({'etapa': ETAPAS[self.etapa - 1][0], 'segundos': round(time.time() - self._t0, 1)})
        self.etapa, self._t0 = n, time.time()
        self.fraccion(0.0)

    def fraccion(self, f: float):
        if self._cancelado():
            raise Cancelado()
        hechas = sum(p for _, p in ETAPAS[: self.etapa - 1])
        pct = int(100 * (hechas + ETAPAS[self.etapa - 1][1] * max(0.0, min(1.0, f))) / _PESO_TOTAL)
        pct = min(max(pct, self.pct), 99)
        self.pct = pct
        self._avisar(pct, self.etapa, len(ETAPAS), ETAPAS[self.etapa - 1][0])

    def cerrar(self):
        self.entrar(len(ETAPAS))  # registra el tiempo de la última
        return self.tiempos


# patrones de avance real en la salida de cada programa
_PATRONES = [
    re.compile(r'Processed file \[(\d+)/(\d+)\]'),                 # COLMAP feature_extractor
    re.compile(r'Matching block \[(\d+)/(\d+), (\d+)/(\d+)\]'),    # COLMAP exhaustive_matcher
    re.compile(r'Matching image \[(\d+)/(\d+)\]'),                 # COLMAP sequential_matcher
    re.compile(r'\((\d+(?:\.\d+)?)%'),                             # OpenMVS: "... (45.2%, 1m3s)"
]


def _fraccion_de(linea: str, total_imgs: int | None):
    m = _PATRONES[0].search(linea)
    if m: return int(m.group(1)) / max(int(m.group(2)), 1)
    m = _PATRONES[1].search(linea)
    if m:
        a, b, c, d = map(int, m.groups())
        return ((a - 1) * d + c) / max(b * d, 1)
    m = _PATRONES[2].search(linea)
    if m: return int(m.group(1)) / max(int(m.group(2)), 1)
    m = re.search(r'Registering image #\d+ \((\d+)\)', linea)
    if m and total_imgs: return int(m.group(1)) / total_imgs
    m = _PATRONES[3].search(linea)
    if m: return float(m.group(1)) / 100
    return None


class LectorDensa:
    """Avance real de DensifyPointCloud: cuenta los mapas de profundidad terminados sobre el total de imágenes."""

    def __init__(self, total):
        self.total, self.hechos = total, 0

    def __call__(self, linea):
        m = re.search(r'Selecting images for dense reconstruction completed: (\d+) images', linea)
        if m:
            self.total = int(m.group(1)) or self.total
        if re.search(r'Depth-map for image\s+\d+ estimated', linea):
            self.hechos += 1
            return 0.85 * min(1.0, self.hechos / max(self.total or 1, 1))
        if 'Point-cloud composed of' in linea:
            return 1.0
        return None


def correr(cmd, cwd, log, av: Avance | None = None, total_imgs=None, limite=None, lector=None):
    """Corre un programa, guarda su salida en el registro y lee de ella el avance real.
    Los programas de OpenMVS guardan la salida en bloques cuando no hay terminal, así que de ellos se lee
    su propio archivo de registro, que escriben línea por línea (lector = intérprete de esas líneas).
    Lo corta si se pasa de tiempo, si la máquina se queda sin memoria o si cancelan el trabajo."""
    log.write('\n$ ' + ' '.join(map(str, cmd)) + '\n'); log.flush()
    p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    t0, buf, ultimo_chequeo = time.time(), b'', 0.0
    os.set_blocking(p.stdout.fileno(), False)
    nombre = os.path.basename(str(cmd[0]))
    seguido, pos, resto = None, 0, ''
    try:
        while True:
            trozo = None
            try:
                trozo = p.stdout.read(65536)
            except BlockingIOError:
                pass
            if trozo:
                log.write(trozo.decode('utf-8', 'replace')); buf += trozo
                *lineas, buf = re.split(rb'[\r\n]', buf)
                if lector is None:
                    for l in lineas:
                        f = _fraccion_de(l.decode('utf-8', 'replace'), total_imgs)
                        if f is not None and av:
                            av.fraccion(f)
            if lector is not None:
                if seguido is None:
                    cands = [c for c in glob.glob(os.path.join(cwd, nombre + '-*.log')) if os.path.getmtime(c) >= t0 - 2]
                    seguido = max(cands, key=os.path.getmtime) if cands else None
                if seguido:
                    with open(seguido, 'r', encoding='utf-8', errors='replace') as fl:
                        fl.seek(pos); nuevo = fl.read(); pos = fl.tell()
                    *lineas_l, resto = (resto + nuevo).split('\n')
                    for l in lineas_l:
                        f = lector(l)
                        if f is not None and av:
                            av.fraccion(f)
            if not trozo:
                if p.poll() is not None:
                    break
                time.sleep(0.3)
            if time.time() - ultimo_chequeo > 2:
                ultimo_chequeo = time.time()
                if av: av.fraccion(-1)  # solo para revisar si cancelaron (no cambia el %)
                libre = _memoria_libre_mb()
                if libre is not None and libre < 700:
                    raise RuntimeError(f'{os.path.basename(str(cmd[0]))} {cmd[1]}: la máquina se quedó sin memoria')
                if limite and time.time() - t0 > limite:
                    raise RuntimeError(f'{os.path.basename(str(cmd[0]))} {cmd[1]} tardó más de {int(limite // 60)} min')
    finally:
        if p.poll() is None:
            p.kill(); p.wait()
        log.flush()
    if p.returncode != 0:
        raise RuntimeError(f'Falló {os.path.basename(str(cmd[0]))} {cmd[1] if len(cmd) > 1 else ""} (código {p.returncode})')


def _analizar_modelo(colmap, modelo, log) -> dict:
    out = subprocess.run([colmap, 'model_analyzer', '--path', modelo], capture_output=True, text=True, timeout=300)
    txt = out.stdout + out.stderr
    log.write(txt[-3000:]); log.flush()
    def num(pat, tipo=float):
        m = re.search(pat, txt)
        return tipo(m.group(1)) if m else None
    return {'fotos_ubicadas': num(r'Registered images:\s*(\d+)', int) or 0, 'puntos': num(r'Points:\s*(\d+)', int) or 0,
            'error_reproyeccion': num(r'Mean reprojection error:\s*([\d.]+)')}


def reconstruir(fotos_dir: str, trabajo: str, dims_cm: dict, avisar, cancelado=lambda: False,
                cajas: dict | None = None, cfg: Config | None = None) -> dict:
    """Corre todo el motor. dims_cm = {'ancho','prof','alto'} cargadas por el restaurante (no se inventan).
    avisar(pct, etapa_num, etapas_total, texto). cancelado() -> True corta el trabajo.
    Devuelve un dict con archivos generados, medidas, métricas, puntaje y registro de etapas.
    Lanza ErrorFoto (problema de las fotos: se le explica al restaurante) o RuntimeError (problema técnico)."""
    cfg = cfg or Config.desde_entorno()
    os.makedirs(trabajo, exist_ok=True)
    av = Avance(avisar, cancelado)
    log = open(os.path.join(trabajo, 'registro.log'), 'a', encoding='utf-8')
    colmap = _bin(None, cfg.colmap) if not os.path.isabs(cfg.colmap) else cfg.colmap
    mvs = lambda n: _bin(cfg.mvs_dir, n)
    g = '1' if cfg.gpu else '0'
    hilos = str(cfg.hilos or os.cpu_count() or 4)
    th = ['--max-threads', str(cfg.hilos)] if cfg.hilos else []
    metricas: dict = {'avisos': []}

    # 1) recepción
    av.entrar(1)
    rutas = sorted(os.path.join(fotos_dir, f) for f in os.listdir(fotos_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png')))
    if len(rutas) < validacion.MIN_FOTOS:
        raise ErrorFoto(f'Llegaron {len(rutas)} fotos y hacen falta al menos {validacion.MIN_FOTOS}.')

    # 2) validación: nitidez, luz, repetidas
    av.entrar(2)
    inf = validacion.validar(rutas, cajas)
    metricas['fotos'] = validacion.a_dict(inf)
    metricas['avisos'] += inf.avisos
    ok, motivo = validacion.suficiente(inf)
    if not ok:
        raise ErrorFoto(motivo)
    usadas = os.path.join(trabajo, 'usadas'); os.makedirs(usadas, exist_ok=True)
    for n in inf.usadas:
        dst = os.path.join(usadas, n)
        if not os.path.exists(dst):
            os.link(os.path.join(fotos_dir, n), dst) if hasattr(os, 'link') else shutil.copy(os.path.join(fotos_dir, n), dst)
    n_fotos = len(inf.usadas)
    metricas['fotos_usadas'] = n_fotos

    # 3) puntos característicos
    av.entrar(3)
    db = os.path.join(trabajo, 'db.db'); sparse = os.path.join(trabajo, 'sparse'); os.makedirs(sparse, exist_ok=True)
    correr([colmap, 'feature_extractor', '--database_path', db, '--image_path', usadas,
            '--ImageReader.single_camera', '1', '--ImageReader.camera_model', 'SIMPLE_RADIAL',
            '--SiftExtraction.use_gpu', g, '--SiftExtraction.max_image_size', str(cfg.tam_puntos),
            '--SiftExtraction.max_num_features', '8000', '--SiftExtraction.num_threads', hilos], trabajo, log, av, limite=cfg.limite_paso_s)

    # 4) unión de fotos (todas contra todas; si no alcanza el tiempo, en orden de captura)
    av.entrar(4)
    base = ['--database_path', db, '--SiftMatching.use_gpu', g, '--SiftMatching.max_num_matches', '8000', '--SiftMatching.num_threads', hilos]
    try:
        correr([colmap, 'exhaustive_matcher', *base, '--ExhaustiveMatching.block_size', '10'], trabajo, log, av, limite=40 * 60)
    except RuntimeError as ex:
        metricas['avisos'].append(f'Unión completa cortada ({ex}); se unió en orden de captura.')
        correr([colmap, 'sequential_matcher', *base, '--SequentialMatching.overlap', '15', '--SequentialMatching.quadratic_overlap', '1'],
               trabajo, log, av, limite=cfg.limite_paso_s)

    # 5) posición de cámaras
    av.entrar(5)
    correr([colmap, 'mapper', '--database_path', db, '--image_path', usadas, '--output_path', sparse], trabajo, log, av, total_imgs=n_fotos, limite=cfg.limite_paso_s)
    modelos = [d for d in (os.path.join(sparse, x) for x in os.listdir(sparse)) if os.path.isdir(d)]
    if not modelos:
        raise ErrorFoto('No se pudieron unir las fotos entre sí. Sacalas con más luz, sin moverte rápido y dando la vuelta completa.')
    mejor = max(modelos, key=lambda d: os.path.getsize(os.path.join(d, 'images.bin')) if os.path.exists(os.path.join(d, 'images.bin')) else 0)
    metricas.update(_analizar_modelo(colmap, mejor, log))
    if metricas['fotos_ubicadas'] < max(15, n_fotos * 0.5) or metricas['puntos'] < 1000:
        raise ErrorFoto(f"Solo se pudieron ubicar {metricas['fotos_ubicadas']} de {n_fotos} fotos. Suele pasar con platos muy lisos o "
                        'brillantes, o si se movió el plato. Probá con luz pareja, sin reflejos, y una mesa con textura (madera, mantel).')

    # 6) cobertura de ángulos (con las cámaras ya ubicadas)
    av.entrar(6)
    dense = os.path.join(trabajo, 'dense')
    correr([colmap, 'image_undistorter', '--image_path', usadas, '--input_path', mejor, '--output_path', dense,
            '--output_type', 'COLMAP', '--max_image_size', str(cfg.tam_foto)], trabajo, log, None, limite=cfg.limite_paso_s)
    sp = os.path.join(dense, 'sparse')
    correr([colmap, 'model_converter', '--input_path', sp, '--output_path', sp, '--output_type', 'TXT'], trabajo, log)
    arriba = _arriba_por_vuelta(sp, _arriba_camaras(os.path.join(sp, 'images.txt')))
    cob = calidad.cobertura(sp, arriba)
    metricas['cobertura'] = cob
    ok, motivo = calidad.cobertura_suficiente(cob)
    if not ok:
        raise ErrorFoto(motivo)

    # 7) preparación para OpenMVS
    av.entrar(7)
    correr([mvs('InterfaceCOLMAP'), '-i', dense, '-o', 'escena.mvs', '--image-folder', os.path.join(dense, 'images'), '-v', '2'], trabajo, log, av, lector=lambda l: None)

    # 8) nube densa
    av.entrar(8)
    correr([mvs('DensifyPointCloud'), 'escena.mvs', '--resolution-level', str(cfg.densa_nivel), '--min-resolution', '800',
            '--max-resolution', str(cfg.densa_max), '-v', '2', *th], trabajo, log, av, limite=cfg.limite_paso_s,
           lector=LectorDensa(n_fotos))

    # 9) malla (cierra agujeros chicos)
    av.entrar(9)
    correr([mvs('ReconstructMesh'), 'escena_dense.mvs', '-p', 'escena_dense.ply', '--close-holes', '300', '-v', '2', *th], trabajo, log, av, limite=cfg.limite_paso_s,
           lector=lambda l: None)  # sin avance parcial confiable: la barra espera el final de la etapa

    # 10) separar el objeto (mesa, fondo, cosas sueltas) y refinar la malla
    av.entrar(10)
    malla, plano = 'escena_dense_mesh.ply', None
    plano = _recortar_objeto(os.path.join(trabajo, 'escena_dense_mesh.ply'), os.path.join(trabajo, 'objeto.ply'), arriba, cajas, sp)
    if plano is None:
        raise ErrorFoto('No se pudo separar el plato de la mesa. Usá una mesa con textura (madera, mantel) distinta del plato, '
                        'y marcá el plato con el recuadro al empezar.')
    malla = 'objeto.ply'

    # 11) textura en alta resolución (con las fotos completas)
    av.entrar(11)
    correr([mvs('TextureMesh'), 'escena_dense.mvs', '-m', malla, '--decimate', '1', '--max-texture-size', str(cfg.textura_max),
            '--export-type', 'glb', '-o', 'escena_textura.glb', '-v', '2', *th], trabajo, log, av, limite=cfg.limite_paso_s,
           lector=lambda l: None)
    crudo = os.path.join(trabajo, 'escena_textura.glb')
    if not os.path.exists(crudo):
        raise RuntimeError('OpenMVS no generó el modelo con textura')

    # 12) limpieza, enderezado y escala real (con la medida que cargó el restaurante)
    av.entrar(12)
    limpio = os.path.join(trabajo, 'limpio.glb')
    ancho = float(max(dims_cm.get('ancho') or 0, dims_cm.get('prof') or 0)) or None
    medidas = limpiar(crudo, limpio, ancho, max_caras=10**9,  # la reducción la hace gltfpack (respeta la textura)
                      arriba=arriba, sparse_dir=sp, plano=plano)
    m = trimesh.load(limpio, force='mesh')
    metricas['textura_vacia'] = calidad.textura_vacia(m)
    metricas['geometria'] = calidad.geometria(m)
    metricas['escala'] = _control_escala(medidas, dims_cm)

    # 13) versiones: maestra (máxima calidad) y celular (liviana), miniatura, y validación de cada GLB
    av.entrar(13)
    salidas = exportar.generar(limpio, trabajo, usadas, cajas, inf)
    tiempos = av.cerrar()
    pts, aprobable, motivos = calidad.puntaje(metricas)
    resultado = {'archivos': salidas, 'medidas_cm': {k: round(v * 100, 1) for k, v in medidas.items() if k in ('ancho', 'prof', 'alto')},
                 'metricas': metricas, 'puntaje': pts, 'aprobable': aprobable, 'motivos': motivos, 'etapas': tiempos}
    with open(os.path.join(trabajo, 'resultado.json'), 'w') as f:
        json.dump(resultado, f, ensure_ascii=False, indent=1, default=float)
    log.close()
    return resultado


def _control_escala(medidas: dict, dims: dict) -> dict:
    """Compara las PROPORCIONES del modelo con las medidas cargadas (la escala absoluta sale de la medida cargada)."""
    try:
        a, p, h = (float(dims.get(k) or 0) for k in ('ancho', 'prof', 'alto'))
        ma, mp, mh = medidas['ancho'], medidas['prof'], medidas['alto']
        if not (a and p and h and ma):
            return {'relacion_ok': None}
        car = sorted([a, p], reverse=True); mod = sorted([ma, mp], reverse=True)
        r_c = (car[1] / car[0], h / car[0]); r_m = (mod[1] / mod[0], mh / mod[0])
        ok = all(abs(x - y) / max(y, 0.05) < 0.35 for x, y in zip(r_m, r_c))
        k = car[0] / 100 / mod[0]
        return {'relacion_ok': bool(ok), 'cargado': f'{car[0]:g} × {car[1]:g} × {h:g} cm',
                'modelo': f'{car[0]:g} × {mod[1] * k * 100:.1f} × {mh * k * 100:.1f} cm'}
    except Exception:
        return {'relacion_ok': None}
