"""Exportación (etapa 13): versión maestra, versión para celular, miniatura y validación de cada GLB."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess

import numpy as np
import trimesh
from PIL import Image, ImageOps

MAESTRO_CARAS = 600_000
MAESTRO_TEX = 4096
MOVIL_CARAS = 150_000
MOVIL_TEX = 2048
MOVIL_MAX_MB = 15


def _textura(m):
    mat = getattr(m.visual, 'material', None)
    if mat is None:
        return None, None
    img = getattr(mat, 'baseColorTexture', None) or getattr(mat, 'image', None)
    return mat, img


def _con_textura(m: trimesh.Trimesh, lado: int, calidad_jpg: int) -> trimesh.Trimesh:
    m = m.copy()
    mat, img = _textura(m)
    if img is not None:
        img = img.convert('RGB')
        if max(img.size) > lado:
            img = img.resize((lado, lado) if img.size[0] == img.size[1] else
                             (lado, int(img.size[1] * lado / img.size[0])) if img.size[0] > img.size[1] else
                             (int(img.size[0] * lado / img.size[1]), lado), Image.LANCZOS)
        buf = io.BytesIO(); img.save(buf, 'JPEG', quality=calidad_jpg, optimize=True); buf.seek(0)
        jpg = Image.open(buf)
        if hasattr(mat, 'baseColorTexture'):
            mat.baseColorTexture = jpg
            mat.roughnessFactor = 0.75; mat.metallicFactor = 0.0
        else:
            mat.image = jpg
    return m


def _gltfpack(ruta_in: str, ruta_out: str, caras_obj: int, caras: int, comprimir: bool) -> bool:
    """gltfpack (meshoptimizer, MIT): reduce caras respetando las costuras de la textura (UV) y,
    si se pide, comprime la geometría con meshopt. Las texturas quedan en JPG (compatibles con Quick Look)."""
    gp = shutil.which('gltfpack')
    if not gp:
        return False
    cmd = [gp, '-i', ruta_in, '-o', ruta_out, '-kn', '-km']
    if caras > caras_obj:
        cmd += ['-si', f'{caras_obj / caras:.4f}']
    if comprimir:
        cmd += ['-cc']
    else:
        cmd += ['-noq']  # sin cuantizar: máxima fidelidad
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print('aviso gltfpack:', r.stderr[-500:])
        return r.returncode == 0 and os.path.exists(ruta_out) and os.path.getsize(ruta_out) > 0
    except Exception as e:
        print('aviso gltfpack:', e)
        return False


def validar_glb(ruta: str) -> dict:
    """Validación del archivo final: que cargue, tenga caras y textura, tamaño, y (si está instalado) el validador oficial de Khronos."""
    info = {'archivo': os.path.basename(ruta), 'bytes': os.path.getsize(ruta), 'ok': False, 'errores': []}
    try:
        m = trimesh.load(ruta, force='mesh')
        info['caras'] = int(len(m.faces))
        _, img = _textura(m)
        info['textura'] = None if img is None else list(img.size)
        info['medidas_m'] = [round(float(x), 4) for x in m.extents]
        if len(m.faces) == 0: info['errores'].append('sin caras')
        if img is None: info['errores'].append('sin textura')
    except Exception as e:
        info['errores'].append(f'no se puede abrir: {e}')
    val = shutil.which('gltf_validator')
    if val:
        try:
            out = subprocess.run([val, '-o', ruta], capture_output=True, text=True, timeout=120)
            rep = json.loads(out.stdout or '{}')
            n = rep.get('issues', {}).get('numErrors', 0)
            info['khronos_errores'] = n
            if n: info['errores'].append(f'{n} errores del validador glTF')
        except Exception as e:
            info['khronos'] = f'no se pudo correr: {e}'
    info['ok'] = not info['errores']
    return info


def miniatura(usadas: str, cajas: dict | None, inf, salida: str):
    """La foto más nítida, recortada al plato, como miniatura (es una foto real del plato, no un render)."""
    fotos = [f for f in inf.fotos if f.ok]
    if not fotos:
        return None
    f = max(fotos, key=lambda x: x.detalle)
    with Image.open(os.path.join(usadas, f.nombre)) as im:
        im = ImageOps.exif_transpose(im).convert('RGB')
        w, h = im.size
        c = (cajas or {}).get(f.nombre)
        if c:
            x0, y0, x1, y1 = c; cx, cy = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h
            lado = max((x1 - x0) * w, (y1 - y0) * h) * 1.25
        else:
            cx, cy, lado = w / 2, h / 2, min(w, h) * 0.8
        lado = min(lado, w, h)
        caja = (int(max(0, min(w - lado, cx - lado / 2))), int(max(0, min(h - lado, cy - lado / 2))))
        im = im.crop((caja[0], caja[1], caja[0] + int(lado), caja[1] + int(lado))).resize((640, 640), Image.LANCZOS)
        im.save(salida, 'JPEG', quality=86, optimize=True)
    return salida


def generar(limpio: str, trabajo: str, usadas: str, cajas, inf) -> dict:
    base = trimesh.load(limpio, force='mesh')
    out = os.path.join(trabajo, 'salida'); os.makedirs(out, exist_ok=True)

    caras = len(base.faces)
    maestro, movil = os.path.join(out, 'maestro.glb'), os.path.join(out, 'movil.glb')
    tmp_m, tmp_c = os.path.join(trabajo, 'tex4k.glb'), os.path.join(trabajo, 'tex2k.glb')
    _con_textura(base, MAESTRO_TEX, 92).export(tmp_m)
    _con_textura(base, MOVIL_TEX, 85).export(tmp_c)
    if not _gltfpack(tmp_m, maestro, MAESTRO_CARAS, caras, comprimir=False):
        shutil.copy(tmp_m, maestro)
    comprimido = _gltfpack(tmp_c, movil, MOVIL_CARAS, caras, comprimir=True)
    if not comprimido:
        shutil.copy(tmp_c, movil)

    mini = miniatura(usadas, cajas, inf, os.path.join(out, 'miniatura.jpg'))
    res = {'maestro': validar_glb(maestro), 'movil': validar_glb(movil), 'miniatura': bool(mini), 'movil_comprimido': comprimido}
    res['movil']['ok'] = res['movil']['ok'] and res['movil']['bytes'] <= MOVIL_MAX_MB * 1024 * 1024
    if not res['maestro']['ok'] or not res['movil']['ok']:
        raise RuntimeError(f"El GLB generado no pasó la validación: {res['maestro']['errores'] + res['movil']['errores']}")
    res['rutas'] = {'maestro': maestro, 'movil': movil, 'miniatura': mini}
    return res
