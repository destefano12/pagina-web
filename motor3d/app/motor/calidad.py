"""Cobertura de ángulos (después de ubicar las cámaras) y puntaje de calidad del modelo final.

Todas las métricas son medibles y quedan guardadas con el trabajo, para que el restaurante
(y nosotros) veamos POR QUÉ un modelo se puede o no se puede publicar.
"""
from __future__ import annotations

import math

import numpy as np
import trimesh

from .geometria import _hacia_donde_miran

SECTORES = 12  # de a 30° alrededor del plato
LADOS = ['de frente', 'del frente-derecha', 'del costado derecho', 'de atrás-derecha', 'de atrás', 'de atrás-izquierda',
         'del costado izquierdo', 'del frente-izquierda']


def cobertura(sparse_dir: str, arriba) -> dict | None:
    """Dónde estuvo el teléfono alrededor del objeto: sectores de 30° cubiertos, hueco mayor y alturas."""
    cm = _hacia_donde_miran(sparse_dir)
    if cm is None or arriba is None:
        return None
    centro, cams = cm
    up = np.asarray(arriba, float); up /= np.linalg.norm(up)
    rel = cams - centro
    alt = rel @ up
    horiz = rel - np.outer(alt, up)
    # base del plano horizontal: la primera cámara define "de frente"
    e1 = horiz[0] / (np.linalg.norm(horiz[0]) or 1)
    e2 = np.cross(up, e1)
    az = (np.degrees(np.arctan2(horiz @ e2, horiz @ e1)) + 360) % 360
    elev = np.degrees(np.arctan2(alt, np.linalg.norm(horiz, axis=1)))
    sectores = sorted({int(a // (360 / SECTORES)) for a in az})
    orden = np.sort(az)
    huecos = np.diff(np.concatenate([orden, [orden[0] + 360]]))
    i = int(np.argmax(huecos))
    hueco, desde = float(huecos[i]), float(orden[i])
    medio = (desde + hueco / 2) % 360
    return {
        'sectores_cubiertos': len(sectores), 'sectores_total': SECTORES,
        'hueco_mayor_grados': round(hueco, 1),
        'lado_sin_fotos': LADOS[int(((medio + 22.5) % 360) // 45)] if hueco > 60 else None,
        'elevacion_min': round(float(elev.min()), 1), 'elevacion_max': round(float(elev.max()), 1),
        'fotos_desde_arriba': int((elev > 40).sum()),
    }


def cobertura_suficiente(c: dict | None) -> tuple[bool, str]:
    if c is None:
        return True, ''
    if c['hueco_mayor_grados'] > 110:
        return False, (f"Faltaron fotos {c['lado_sin_fotos'] or 'de un lado'} del plato (un hueco de "
                       f"{int(c['hueco_mayor_grados'])}° sin fotos). Volvé a escanear dando la vuelta completa.")
    return True, ''


EMPTY = np.array([255, 127, 39])  # color con el que OpenMVS pinta lo que ninguna foto vio (0x00FF7F27)


def textura_vacia(m: trimesh.Trimesh, muestras: int = 20000) -> float | None:
    """Fracción de la superficie que quedó sin color real (pintada con el color de relleno)."""
    try:
        vis = m.visual
        img = getattr(vis.material, 'baseColorTexture', None) or getattr(vis.material, 'image', None)
        uv = getattr(vis, 'uv', None)
        if img is None or uv is None:
            return None
        a = np.asarray(img.convert('RGB'), dtype=np.int16)
        h, w = a.shape[:2]
        idx = np.random.default_rng(3).choice(len(m.faces), min(len(m.faces), muestras), replace=False)
        p = uv[m.faces[idx]].mean(axis=1)
        x = np.clip((p[:, 0] % 1) * (w - 1), 0, w - 1).astype(int)
        y = np.clip((1 - p[:, 1] % 1) * (h - 1), 0, h - 1).astype(int)
        col = a[y, x]
        vacio = np.abs(col - EMPTY).sum(axis=1) < 40
        pesos = m.area_faces[idx]
        return float((pesos * vacio).sum() / max(pesos.sum(), 1e-12))
    except Exception:
        return None


def geometria(m: trimesh.Trimesh) -> dict:
    """Agujeros (bordes abiertos), piezas sueltas y densidad de la malla."""
    from .geometria import _componentes
    try:
        lab = _componentes(m)
        areas = np.bincount(lab, weights=m.area_faces)
        piezas = int((areas > areas.sum() * 0.01).sum())
    except Exception:
        piezas = None
    # bordes abiertos: aristas que pertenecen a una sola cara (con vértices unidos por posición)
    try:
        u = m.copy(); u.merge_vertices(merge_tex=True, merge_norm=True)
        borde = trimesh.grouping.group_rows(u.edges_sorted, require_count=1)
        largo_borde = float(np.linalg.norm(u.vertices[u.edges_sorted[borde][:, 0]] - u.vertices[u.edges_sorted[borde][:, 1]], axis=1).sum())
        escala = math.sqrt(max(u.area, 1e-12))
        borde_rel = largo_borde / escala  # adimensional: cuánto borde abierto hay para el tamaño del objeto
    except Exception:
        borde_rel = None
    return {'caras': int(len(m.faces)), 'vertices': int(len(m.vertices)), 'piezas': piezas,
            'borde_abierto_relativo': None if borde_rel is None else round(borde_rel, 2)}


def puntaje(m: dict) -> tuple[int, bool, list[str]]:
    """m: métricas juntadas por el motor. Devuelve (0..100, se puede publicar, motivos)."""
    pts, motivos, critico = 0.0, [], False

    reg = m.get('fotos_ubicadas', 0) / max(m.get('fotos_usadas', 1), 1)
    pts += 20 * min(1, reg / 0.9)
    if reg < 0.6:
        critico = True; motivos.append(f'Solo se pudieron ubicar {int(reg * 100)}% de las fotos.')

    err = m.get('error_reproyeccion')
    if err is not None:
        pts += 10 * (1 if err <= 1.0 else max(0, 1 - (err - 1.0) / 1.5))
        if err > 2.0: motivos.append('Las fotos no coinciden bien entre sí (posible movimiento del plato).')
    else:
        pts += 5

    cob = m.get('cobertura') or {}
    if cob:
        frac = cob['sectores_cubiertos'] / cob['sectores_total']
        pts += 15 * frac + (5 if cob.get('fotos_desde_arriba', 0) >= 3 else 0)
        if cob['hueco_mayor_grados'] > 90: motivos.append(f"Hay un lado con pocas fotos ({cob.get('lado_sin_fotos') or 'un costado'}).")
        if cob.get('fotos_desde_arriba', 0) < 3: motivos.append('Faltan fotos desde arriba: la parte de arriba puede salir pobre.')
    else:
        pts += 10

    vacia = m.get('textura_vacia')
    if vacia is not None:
        pts += 20 * max(0, 1 - vacia / 0.15)
        if vacia > 0.15:
            critico = True; motivos.append(f'{int(vacia * 100)}% de la superficie quedó sin color real.')
        elif vacia > 0.04:
            motivos.append(f'{int(vacia * 100)}% de la superficie quedó sin color real.')
    else:
        pts += 10

    g = m.get('geometria') or {}
    caras = g.get('caras', 0)
    pts += 8 * min(1, caras / 60000)
    if caras < 15000:
        critico = True; motivos.append('El modelo tiene muy poco detalle de forma.')
    if g.get('piezas') is not None:
        pts += 6 if g['piezas'] <= 2 else 3 if g['piezas'] <= 4 else 0
        if g['piezas'] > 4: motivos.append('El modelo quedó partido en varios pedazos.')
    br = g.get('borde_abierto_relativo')
    if br is not None:
        pts += 6 * max(0, 1 - br / 6)
        if br > 6: motivos.append('El modelo tiene agujeros grandes.')

    esc = m.get('escala') or {}
    if esc.get('relacion_ok') is not None:
        pts += 10 if esc['relacion_ok'] else 3
        if not esc['relacion_ok']:
            motivos.append(f"Las proporciones medidas ({esc['modelo']}) no coinciden con las cargadas ({esc['cargado']}): revisá las medidas.")
    else:
        pts += 5

    total = int(round(min(100, pts)))
    aprobable = (not critico) and total >= 60
    if not motivos and aprobable:
        motivos.append('Sin problemas detectados.')
    return total, aprobable, motivos
