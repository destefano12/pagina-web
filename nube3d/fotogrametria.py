"""Fotogrametría: fotos de un plato -> modelo 3D real (.glb) a tamaño real.

Usa COLMAP (estructura desde movimiento) + OpenMVS (nube densa, malla y textura),
ambos gratis y de código abierto. Después recorta la mesa, endereza el plato,
lo escala al ancho real y exporta un .glb liviano para ver en el celular.
"""
import glob
import io
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import trimesh
from PIL import Image

EXE = '.exe' if os.name == 'nt' else ''


class ErrorFoto(Exception):
    """Error que se le puede mostrar al restaurante tal cual."""


def _bin(carpeta, nombre):
    if carpeta:
        for cand in (os.path.join(carpeta, nombre + EXE), os.path.join(carpeta, 'bin', nombre + EXE)):
            if os.path.exists(cand):
                return cand
    found = shutil.which(nombre)
    if not found:
        raise RuntimeError(f'No encuentro el programa {nombre}. Revisá la instalación.')
    return found


def _run(cmd, cwd, log, avance=None):
    """avance = (avisar, desde, hasta, segundos_estimados, etapa): mientras corre el paso, el % sigue
    subiendo despacio hacia `hasta` (sin llegar), así la barra nunca se queda quieta."""
    log.write('\n$ ' + ' '.join(map(str, cmd)) + '\n'); log.flush()
    listo = threading.Event()
    if avance:
        avisar, desde, hasta, seg, etapa = avance
        def latir():
            t0 = time.time(); ultimo = desde
            while not listo.wait(8):
                pct = int(desde + (hasta - 1 - desde) * (1 - math.exp(-(time.time() - t0) / seg)))
                if pct > ultimo:
                    ultimo = pct; avisar(pct, etapa)
        threading.Thread(target=latir, daemon=True).start()
    try:
        p = subprocess.run([str(c) for c in cmd], cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    finally:
        listo.set()
    if p.returncode != 0:
        raise RuntimeError(f'Falló {os.path.basename(str(cmd[0]))} (código {p.returncode}). Ver registro.log')


# calidad: tamaño de foto que usa la nube y nivel de detalle de la nube densa (0 = máximo)
TAM = int(os.environ.get('CALIDAD_TAM', '4096'))
NIVEL = int(os.environ.get('CALIDAD_NIVEL', '1'))


def _resumen(colmap, modelo, log):
    """Cuántas fotos quedaron ubicadas y cuántos puntos tiene la reconstrucción inicial."""
    try:
        out = subprocess.run([colmap, 'model_analyzer', '--path', modelo], capture_output=True, text=True, timeout=120)
        txt = out.stdout + out.stderr
        log.write(txt[-2000:]); log.flush()
        reg = re.search(r'Registered images:\s*(\d+)', txt)
        pts = re.search(r'Points:\s*(\d+)', txt)
        return (int(reg.group(1)) if reg else 0), (int(pts.group(1)) if pts else 0)
    except Exception:
        return 0, 0


def procesar(fotos_dir, trabajo, ancho_cm, avisar=lambda pct, etapa: None, colmap_dir=None, mvs_dir=None, hilos=0, gpu=False, modo='plato', cajas=None):
    """cajas: {nombre_foto: [x0, y0, x1, y1]} en 0..1, el recuadro donde está el objeto en cada foto.
    Devuelve la ruta del .glb final. modo: 'plato' (menú), 'objeto' o 'ambiente' (Metra 3D).
    Las medidas finales quedan en <glb>.json. ancho_cm puede ser None (sin escala real)."""
    os.makedirs(trabajo, exist_ok=True)
    colmap = _bin(colmap_dir, 'colmap') if not (colmap_dir and os.path.exists(os.path.join(colmap_dir, 'COLMAP.bat'))) else os.path.join(colmap_dir, 'COLMAP.bat')
    mvs = lambda n: _bin(mvs_dir, n)
    log = open(os.path.join(trabajo, 'registro.log'), 'a', encoding='utf-8')
    fotos = sorted(f for f in glob.glob(os.path.join(fotos_dir, '*')) if f.lower().endswith(('.jpg', '.jpeg', '.png')))
    if len(fotos) < 15:
        raise ErrorFoto(f'Llegaron {len(fotos)} fotos y hacen falta al menos 15.')
    g = '1' if gpu else '0'
    k = max(0.5, len(fotos) / 45)  # los tiempos estimados son para 45 fotos
    db = os.path.join(trabajo, 'db.db')
    sparse = os.path.join(trabajo, 'sparse'); os.makedirs(sparse, exist_ok=True)

    E = 'Buscando puntos en las fotos'; avisar(8, E)
    _run([colmap, 'feature_extractor', '--database_path', db, '--image_path', fotos_dir,
          '--ImageReader.single_camera', '1', '--ImageReader.camera_model', 'SIMPLE_RADIAL',
          '--SiftExtraction.use_gpu', g, '--SiftExtraction.max_image_size', str(TAM),
          '--SiftExtraction.max_num_features', '12000'], trabajo, log, (avisar, 8, 18, 120 * k, E))
    E = 'Uniendo las fotos entre sí'; avisar(18, E)
    _run([colmap, 'exhaustive_matcher', '--database_path', db, '--SiftMatching.use_gpu', g,
          '--SiftMatching.guided_matching', '1'], trabajo, log, (avisar, 18, 30, 400 * k, E))
    E = 'Calculando dónde estaba la cámara'; avisar(30, E)
    _run([colmap, 'mapper', '--database_path', db, '--image_path', fotos_dir, '--output_path', sparse], trabajo, log, (avisar, 30, 38, 250 * k, E))
    modelos = [d for d in glob.glob(os.path.join(sparse, '*')) if os.path.isdir(d)]
    if not modelos:
        raise ErrorFoto('No se pudieron unir las fotos. Sacalas con más luz, sin moverte rápido y dando la vuelta completa.')
    mejor = max(modelos, key=lambda d: os.path.getsize(os.path.join(d, 'images.bin')) if os.path.exists(os.path.join(d, 'images.bin')) else 0)
    # control rápido: si casi no se pudieron unir las fotos, se corta acá (no tiene sentido seguir una hora)
    reg, pts = _resumen(colmap, mejor, log)
    if modo != 'ambiente' and reg and (reg < max(12, len(fotos) * 0.5) or pts < 1000):
        raise ErrorFoto(f'Solo se pudieron usar {reg} de {len(fotos)} fotos: el objeto tiene muy poco detalle o brilla mucho. '
                        'Probá con algo con más textura (comida, madera, tela), luz pareja sin reflejos y la mesa con textura.')

    E = 'Preparando las fotos'; avisar(38, E)
    dense = os.path.join(trabajo, 'dense')
    _run([colmap, 'image_undistorter', '--image_path', fotos_dir, '--input_path', mejor, '--output_path', dense,
          '--output_type', 'COLMAP', '--max_image_size', str(TAM)], trabajo, log, (avisar, 38, 44, 40 * k, E))
    # OpenMVS espera los parámetros de cámara en texto
    sp = os.path.join(dense, 'sparse')
    _run([colmap, 'model_converter', '--input_path', sp, '--output_path', sp, '--output_type', 'TXT'], trabajo, log)
    arriba = _arriba_camaras(os.path.join(sp, 'images.txt'))

    th = ['--max-threads', str(hilos)] if hilos else []
    avisar(45, 'Convirtiendo la escena')
    _run([mvs('InterfaceCOLMAP'), '-i', dense, '-o', 'escena.mvs', '--image-folder', os.path.join(dense, 'images'), '-v', '1'], trabajo, log)
    E = 'Armando la nube de puntos densa (es lo que más tarda)'; avisar(52, E)
    _run([mvs('DensifyPointCloud'), 'escena.mvs', '--resolution-level', str(NIVEL), '--min-resolution', '1024', '--max-resolution', '1800', '-v', '1', *th], trabajo, log, (avisar, 52, 75, 900 * k, E))
    E = 'Creando la superficie'; avisar(75, E)
    _run([mvs('ReconstructMesh'), 'escena_dense.mvs', '-p', 'escena_dense.ply', '--close-holes', '300' if modo != 'ambiente' else '30', '-v', '1', *th], trabajo, log, (avisar, 75, 85, 120 * k, E))
    malla, plano = 'escena_dense_mesh.ply', None
    if modo != 'ambiente':
        avisar(84, 'Separando el objeto de la mesa y del fondo')
        plano = _recortar_objeto(os.path.join(trabajo, 'escena_dense_mesh.ply'), os.path.join(trabajo, 'objeto.ply'), arriba, cajas, sp)
        if plano is not None:
            malla = 'objeto.ply'
    E = 'Pintando la textura en 4K'; avisar(85, E)
    _run([mvs('TextureMesh'), 'escena_dense.mvs', '-m', malla, '--decimate', '1',
          '--max-texture-size', '4096', '--export-type', 'glb', '-o', 'escena_textura.glb', '-v', '1', *th], trabajo, log, (avisar, 85, 94, 180 * k, E))
    crudo = os.path.join(trabajo, 'escena_textura.glb')
    if not os.path.exists(crudo):
        raise RuntimeError('OpenMVS no generó el modelo con textura')

    avisar(94, 'Recortando la mesa y ajustando el tamaño real' if modo != 'ambiente' else 'Enderezando el ambiente y midiendo')
    final = os.path.join(trabajo, 'modelo.glb' if modo != 'plato' else 'plato.glb')
    dims = limpiar(crudo, final, ancho_cm, modo=modo, arriba=arriba, cajas=None if plano is not None else cajas, sparse_dir=os.path.join(dense, 'sparse'), plano=plano)
    import json as _json
    with open(final + '.json', 'w') as f:
        _json.dump(dims, f)
    log.close()
    return final


def _arriba_camaras(images_txt):
    """Promedia el 'arriba' de cada foto (el teléfono se sostiene derecho): da la vertical real de la escena."""
    try:
        ups = []
        with open(images_txt, encoding='utf-8') as f:
            lineas = [l for l in f if l.strip() and not l.startswith('#')]
        for l in lineas[0::2]:
            p = l.split()
            qw, qx, qy, qz = map(float, p[1:5])
            R = np.array([[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
                          [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
                          [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]])
            ups.append(-R[1, :])  # eje Y de la cámara apunta hacia abajo en la imagen
        if len(ups) < 5:
            return None
        u = np.mean(ups, axis=0); n = np.linalg.norm(u)
        return u / n if n > 0.3 else None
    except Exception as e:
        print('aviso arriba:', e, file=sys.stderr)
        return None


def _plano_ransac(v, iters=600, tol=None, rng=np.random.default_rng(7)):
    ext = np.ptp(v, axis=0).max()
    tol = tol or ext * 0.004
    best, best_n = None, -1
    for _ in range(iters):
        a, b, c = v[rng.choice(len(v), 3, replace=False)]
        n = np.cross(b - a, c - a); ln = np.linalg.norm(n)
        if ln < 1e-12: continue
        n /= ln; d = -n @ a
        cnt = np.count_nonzero(np.abs(v @ n + d) < tol)
        if cnt > best_n: best, best_n = (n, d), cnt
    n, d = best
    # ajuste fino por mínimos cuadrados con los inliers
    ins = v[np.abs(v @ n + d) < tol]
    c = ins.mean(0); u, s, vt = np.linalg.svd(ins - c, full_matrices=False)
    n = vt[2]; d = -n @ c
    return n, d, tol


def _rot_a(n, destino=np.array([0, 1.0, 0])):
    n = n / np.linalg.norm(n); v = np.cross(n, destino); s = np.linalg.norm(v); c = n @ destino
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1, -1, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / s ** 2)


def _componentes(m):
    eps = max(np.ptp(m.vertices, axis=0).max() * 1e-6, 1e-9)
    _, inv = np.unique(np.round(m.vertices / eps).astype(np.int64), axis=0, return_inverse=True)
    t = trimesh.Trimesh(vertices=np.zeros((inv.max() + 1, 3)), faces=inv.reshape(-1)[m.faces], process=False)
    return trimesh.graph.connected_component_labels(t.face_adjacency, node_count=len(m.faces))


def _quedarse_con_el_plato(sub, n):
    lab = _componentes(sub)
    areas = np.bincount(lab, weights=sub.area_faces)
    principal = int(np.argmax(areas))
    # horizontal = proyección sobre el plano de la mesa
    R = _rot_a(n)
    cen = (sub.triangles_center @ R.T)[:, [0, 2]]
    def caja(mask):
        c = cen[mask]; return c.min(0), c.max(0)
    lo, hi = caja(lab == principal)
    centro, radio = (lo + hi) / 2, np.linalg.norm(hi - lo) / 2
    keep = np.zeros(len(areas), bool)
    for k in np.nonzero(areas > areas.sum() * 0.002)[0]:
        c = cen[lab == k].mean(0)
        keep[k] = np.linalg.norm(c - centro) < radio * 1.35
    keep[principal] = True
    # una segunda pasada: lo que quedó pegado a lo elegido amplía la zona (papas, salsas al costado)
    lo, hi = caja(keep[lab]); centro, radio = (lo + hi) / 2, np.linalg.norm(hi - lo) / 2
    for k in np.nonzero((areas > areas.sum() * 0.002) & ~keep)[0]:
        keep[k] = np.linalg.norm(cen[lab == k].mean(0) - centro) < radio * 1.1
    caras = np.nonzero(keep[lab])[0]
    return sub.submesh([caras], append=True)


def _dentro_de_cajas(v, cajas, sparse_dir, margen=0.08):
    """Para cada punto del modelo: ¿cae dentro del recuadro del objeto en la mayoría de las fotos que lo ven?"""
    try:
        cams = {}
        for l in open(os.path.join(sparse_dir, 'cameras.txt'), encoding='utf-8'):
            if l.startswith('#') or not l.strip():
                continue
            p = l.split(); w, h = float(p[2]), float(p[3]); q = list(map(float, p[4:]))
            fx, fy, cx, cy = (q[0], q[1], q[2], q[3]) if p[1] in ('PINHOLE', 'OPENCV', 'FULL_OPENCV') else (q[0], q[0], q[1], q[2])
            cams[p[0]] = (w, h, fx, fy, cx, cy)
        lineas = [l for l in open(os.path.join(sparse_dir, 'images.txt'), encoding='utf-8') if l.strip() and not l.startswith('#')]
        vis = np.zeros(len(v)); den = np.zeros(len(v)); usadas = 0
        for l in lineas[0::2]:
            p = l.split()
            caja = cajas.get(p[9]) or cajas.get(os.path.splitext(p[9])[0] + '.jpg')
            if not caja or p[8] not in cams:
                continue
            qw, qx, qy, qz = map(float, p[1:5]); t = np.array(list(map(float, p[5:8])))
            Rm = np.array([[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
                           [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
                           [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]])
            w, h, fx, fy, cx, cy = cams[p[8]]
            X = v @ Rm.T + t; z = X[:, 2]; ok = z > 1e-6
            u = np.where(ok, fx * X[:, 0] / np.where(ok, z, 1) + cx, -1) / w
            y = np.where(ok, fy * X[:, 1] / np.where(ok, z, 1) + cy, -1) / h
            enfoto = ok & (u >= 0) & (u < 1) & (y >= 0) & (y < 1)
            x0, y0, x1, y1 = caja; mx, my = (x1 - x0) * margen, (y1 - y0) * margen
            dentro = enfoto & (u >= x0 - mx) & (u <= x1 + mx) & (y >= y0 - my) & (y <= y1 + my)
            den += enfoto; vis += dentro; usadas += 1
        if usadas < 8:
            return None
        return (den >= 3) & (vis >= 0.6 * np.maximum(den, 1))
    except Exception as e:
        print('aviso cajas:', e, file=sys.stderr)
        return None


def _recortar_objeto(entrada, salida, arriba, cajas, sparse_dir):
    """Deja solo el objeto: saca la mesa (y todo lo que está debajo), lo que cae fuera del recuadro marcado
    y las cosas sueltas. Devuelve el plano de la mesa (n, d, tol) o None si no se pudo (se sigue con todo)."""
    try:
        m = trimesh.load(entrada, process=False)
        v = m.vertices
        n, d, tol = _plano_ransac(v[np.random.default_rng(1).choice(len(v), min(len(v), 60000), replace=False)])
        dist = v @ n + d
        if arriba is not None and abs(n @ arriba) > 0.5:
            if n @ arriba < 0:
                n, d, dist = -n, -d, -dist
        elif np.count_nonzero(dist > tol * 3) < np.count_nonzero(dist < -tol * 3):
            n, d, dist = -n, -d, -dist
        ok = dist > tol * 2.5  # todo lo que está apoyado arriba de la mesa, sin la mesa
        if cajas and sparse_dir:
            dentro = _dentro_de_cajas(v, cajas, sparse_dir)
            if dentro is not None:
                ok &= dentro
        caras = np.nonzero(ok[m.faces].all(axis=1))[0]
        print(f'  objeto separado: {len(caras)} de {len(m.faces)} caras', file=sys.stderr)
        if len(caras) < 300:
            return None
        sub = _quedarse_con_el_plato(m.submesh([caras], append=True), n)
        if len(sub.faces) < 300:
            return None
        sub.export(salida)
        return (n, d, tol)
    except Exception as e:
        print('aviso recorte:', e, file=sys.stderr)
        return None


def limpiar(entrada, salida, ancho_cm, max_caras=250000, modo='plato', arriba=None, cajas=None, sparse_dir=None, plano=None):
    scene = trimesh.load(entrada, force='scene')
    geoms = [g for g in scene.dump() if isinstance(g, trimesh.Trimesh)]
    if not geoms:
        raise ErrorFoto('El modelo salió vacío')
    m = max(geoms, key=lambda g: len(g.faces)) if len(geoms) == 1 else trimesh.util.concatenate(geoms)
    if modo == 'ambiente':
        return _limpiar_ambiente(m, salida, ancho_cm, arriba, max_caras=max(max_caras, 400000))
    # 0) si el usuario marcó el objeto en la cámara, nos quedamos solo con lo que cae dentro de su recuadro
    if cajas and sparse_dir:
        dentro = _dentro_de_cajas(m.vertices, cajas, sparse_dir)
        if dentro is not None:
            caras = np.nonzero(dentro[m.faces].all(axis=1))[0]
            print(f'  recuadro: quedan {len(caras)} de {len(m.faces)} caras', file=sys.stderr)
            if len(caras) > 500:
                m = m.submesh([caras], append=True)
    v = m.vertices
    # 1) la mesa es el plano más grande: lo usamos como piso (si ya se recortó antes, viene medido)
    if plano is not None:
        n, d, tol = plano
        dist = v @ n + d
    else:
        n, d, tol = _plano_ransac(v[np.random.default_rng(1).choice(len(v), min(len(v), 60000), replace=False)])
        dist = v @ n + d
    # el plato queda del lado del plano donde hay más volumen
    if plano is not None:
        pass
    elif arriba is not None and abs(n @ arriba) > 0.5:
        if n @ arriba < 0:
            n, d, dist = -n, -d, -dist
    elif np.count_nonzero(dist > tol * 3) < np.count_nonzero(dist < -tol * 3):
        n, d, dist = -n, -d, -dist
    # 2) nos quedamos con lo que está arriba de la mesa
    arriba = dist > tol * 1.5
    caras_ok = arriba[m.faces].all(axis=1)
    if caras_ok.sum() < 200:
        raise ErrorFoto('No se distingue el objeto de la superficie donde está apoyado. Usá una superficie opaca con algo de textura (madera, mantel de tela) y que el objeto ocupe buena parte de la foto.')
    sub = m.submesh([np.nonzero(caras_ok)[0]], append=True)
    # 3) componente principal (descarta migas y cosas sueltas en la mesa).
    #    La textura parte la malla en parches con vértices repetidos: para ver qué está conectado
    #    se unen los vértices por posición.
    sub = _quedarse_con_el_plato(sub, n)
    # 4) enderezar (mesa = piso), apoyar en y=0 y centrar
    R = _rot_a(n)
    T = np.eye(4); T[:3, :3] = R
    sub.apply_transform(T)
    piso = -(d) * 1.0  # distancia del origen al plano ya no importa: apoyamos por el mínimo
    sub.apply_translation([-sub.bounds[:, 0].mean(), -sub.bounds[0, 1], -sub.bounds[:, 2].mean()])
    # 5) escala real: el ancho horizontal mayor = ancho del plato
    xz = sub.vertices[:, [0, 2]]
    xz = xz - xz.mean(0); _, _, vt = np.linalg.svd(xz, full_matrices=False)
    ancho = np.ptp(xz @ vt.T, axis=0).max()
    # sin medida real: se deja a 1 m de lado mayor y se avisa que no tiene escala
    k = ((float(ancho_cm) / 100.0) if ancho_cm else 1.0) / max(ancho, 1e-9)
    sub.apply_scale(k)
    # 6) aliviar: menos caras y textura en JPG
    if len(sub.faces) > max_caras:
        try:
            sub = sub.simplify_quadric_decimation(face_count=max_caras)
        except Exception:
            pass
    try:
        mat = sub.visual.material
        img = getattr(mat, 'baseColorTexture', None) or getattr(mat, 'image', None)
        if img is not None:
            img = img.convert('RGB')
            if max(img.size) > 4096: img.thumbnail((4096, 4096))
            buf = io.BytesIO(); img.save(buf, 'JPEG', quality=93); buf.seek(0)
            jpg = Image.open(buf)
            if hasattr(mat, 'baseColorTexture'): mat.baseColorTexture = jpg
            else: mat.image = jpg
            if hasattr(mat, 'roughnessFactor'): mat.roughnessFactor = 0.7; mat.metallicFactor = 0.0
    except Exception as e:
        print('aviso textura:', e, file=sys.stderr)
    sub.export(salida)
    return _medidas(sub, bool(ancho_cm))


def _medidas(m, escala):
    xz = m.vertices[:, [0, 2]]; c = xz.mean(0)
    _, _, vt = np.linalg.svd(xz - c, full_matrices=False)
    ext = np.ptp((xz - c) @ vt.T, axis=0)
    d = {'ancho': float(ext.max()), 'prof': float(ext.min()), 'alto': float(np.ptp(m.vertices[:, 1])), 'escala': escala}
    try:
        d['area'] = float(m.area)
    except Exception:
        pass
    return d


def _limpiar_ambiente(m, salida, ancho_cm, arriba, max_caras=400000):
    """Ambiente: endereza (piso abajo), lo apoya en y=0, descarta basura flotante y lo pone a escala."""
    v = m.vertices
    if arriba is None:
        # sin cámaras: el plano grande con más cosas de un solo lado se toma como piso
        n, d, tol = _plano_ransac(v[np.random.default_rng(1).choice(len(v), min(len(v), 60000), replace=False)])
        arriba = n if np.count_nonzero(v @ n + d > 0) > len(v) / 2 else -n
    R = _rot_a(np.asarray(arriba, float))
    T = np.eye(4); T[:3, :3] = R
    m.apply_transform(T)
    # descartar partes sueltas chicas (reflejos, ventanas)
    try:
        lab = _componentes(m)
        areas = np.bincount(lab, weights=m.area_faces)
        keep = areas > areas.sum() * 0.01
        m = m.submesh([np.nonzero(keep[lab])[0]], append=True)
    except Exception as e:
        print('aviso componentes:', e, file=sys.stderr)
    # recorte robusto: sin el 0,5 % más extremo en cada eje
    lo = np.percentile(m.vertices, 0.5, axis=0); hi = np.percentile(m.vertices, 99.5, axis=0)
    dentro = np.all((m.vertices >= lo) & (m.vertices <= hi), axis=1)
    caras = np.nonzero(dentro[m.faces].all(axis=1))[0]
    if len(caras) > 1000:
        m = m.submesh([caras], append=True)
    # piso en y=0, centrado
    piso = np.percentile(m.vertices[:, 1], 1.0)
    m.apply_translation([-m.bounds[:, 0].mean(), -piso, -m.bounds[:, 2].mean()])
    # escala: la medida que da el usuario es el largo del ambiente (lado horizontal mayor)
    xz = m.vertices[:, [0, 2]]; c = xz.mean(0)
    _, _, vt = np.linalg.svd(xz - c, full_matrices=False)
    largo = np.ptp((xz - c) @ vt.T, axis=0).max()
    k = ((float(ancho_cm) / 100.0) if ancho_cm else 5.0) / max(largo, 1e-9)
    m.apply_scale(k)
    if len(m.faces) > max_caras:
        try:
            m = m.simplify_quadric_decimation(face_count=max_caras)
        except Exception:
            pass
    try:
        mat = m.visual.material
        img = getattr(mat, 'baseColorTexture', None) or getattr(mat, 'image', None)
        if img is not None:
            img = img.convert('RGB')
            if max(img.size) > 4096: img.thumbnail((4096, 4096))
            buf = io.BytesIO(); img.save(buf, 'JPEG', quality=93); buf.seek(0)
            jpg = Image.open(buf)
            if hasattr(mat, 'baseColorTexture'): mat.baseColorTexture = jpg
            else: mat.image = jpg
            if hasattr(mat, 'roughnessFactor'): mat.roughnessFactor = 0.85; mat.metallicFactor = 0.0
    except Exception as e:
        print('aviso textura:', e, file=sys.stderr)
    m.export(salida)
    d = _medidas(m, bool(ancho_cm))
    d['alto'] = float(np.percentile(m.vertices[:, 1], 99.0))  # piso a techo
    return d


if __name__ == '__main__':
    # uso de prueba: python fotogrametria.py carpeta_fotos carpeta_trabajo ancho_cm [plato|objeto|ambiente]
    f, t, a = sys.argv[1], sys.argv[2], (float(sys.argv[3]) or None)
    print(procesar(f, t, a, avisar=lambda p, e: print(f'[{p:3d}%] {e}', flush=True),
                   colmap_dir=os.environ.get('COLMAP_DIR'), mvs_dir=os.environ.get('MVS_DIR'),
                   modo=sys.argv[4] if len(sys.argv) > 4 else 'plato'))
