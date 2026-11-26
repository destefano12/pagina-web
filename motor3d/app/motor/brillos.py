"""Brillos y reflejos de luz.

Un reflejo (la luz de una lámpara sobre la salsa, el borde del plato, un vaso) depende de dónde está la cámara:
en una foto el punto brilla y en otra, tomada desde otro ángulo, no. Por eso, para cada foto se marca dónde hay
brillos y OpenMVS pinta cada parte del modelo con las fotos en las que esa parte NO brilla
(máscara `<foto>.mask.png` al lado de cada imagen + `--ignore-mask-label 255` en TextureMesh).

Ojo, medido con OpenMVS 2.3.0: TextureMesh usa solo los píxeles cuya máscara VALE la etiqueta (255) y descarta el resto,
así que la máscara va en 255 donde la foto sirve y en 0 donde hay brillo. Si falta la máscara de alguna foto, se cae:
por eso se escribe una para cada foto.

Un brillo es una mancha casi blanca (poco color) y bastante más clara que lo que la rodea. Así un plato blanco
mate no se marca entero: solo los destellos.
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageFilter

ETIQUETA = 255           # valor de la máscara donde la foto SÍ se usa para pintar
LADO_ANALISIS = 1024     # se analiza una copia chica (rápido) y la máscara se agranda al tamaño real
MAX_FRACCION = 0.12      # si "brilla" más que esto, se queda solo con lo más fuerte (es un plato claro, no un reflejo)


def mascara(img: Image.Image) -> tuple[Image.Image, float]:
    """Devuelve (máscara en tamaño real: 255 = se usa, 0 = brillo; fracción de la foto marcada como brillo)."""
    w, h = img.size
    chica = img.convert('RGB')
    chica.thumbnail((LADO_ANALISIS, LADO_ANALISIS))
    a = np.asarray(chica, dtype=np.float32) / 255.0
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = np.where(mx > 1e-3, (mx - mn) / np.maximum(mx, 1e-3), 0.0)
    # brillo del entorno: desenfoque amplio del canal de luz
    v8 = Image.fromarray((mx * 255).astype(np.uint8))
    entorno = np.asarray(v8.filter(ImageFilter.GaussianBlur(radius=max(6, chica.size[0] // 64))), dtype=np.float32) / 255.0
    sobre = mx - entorno
    m = ((mx > 0.90) & (sat < 0.25) & (sobre > 0.08)) | ((mx > 0.985) & (sat < 0.15) & (sobre > 0.03))
    if m.mean() > MAX_FRACCION:
        m = (mx > 0.94) & (sat < 0.20) & (sobre > 0.16)
    # se agranda un poco: el borde del destello también está lavado
    mm = Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
    frac = float(np.asarray(mm).mean() / 255.0)
    usable = mm.point(lambda x: 0 if x else ETIQUETA)
    return usable.resize((w, h), Image.NEAREST), frac


def marcar(carpeta_imagenes: str) -> dict:
    """Escribe `<nombre>.mask.png` al lado de cada foto. Devuelve un resumen para las métricas."""
    fracs = []
    for n in sorted(os.listdir(carpeta_imagenes)):
        if not n.lower().endswith(('.jpg', '.jpeg', '.png')) or n.endswith('.mask.png'):
            continue
        ruta = os.path.join(carpeta_imagenes, n)
        with Image.open(ruta) as im:
            mm, f = mascara(im)
        mm.save(os.path.splitext(ruta)[0] + '.mask.png')
        fracs.append(f)
    if not fracs:
        return {'fotos': 0}
    return {'fotos': len(fracs), 'fotos_con_brillos': int(sum(f > 0.002 for f in fracs)),
            'brillo_promedio': round(float(np.mean(fracs)), 4), 'brillo_max': round(float(np.max(fracs)), 4)}


def borrar(carpeta_imagenes: str):
    for n in os.listdir(carpeta_imagenes):
        if n.endswith('.mask.png'):
            os.remove(os.path.join(carpeta_imagenes, n))
