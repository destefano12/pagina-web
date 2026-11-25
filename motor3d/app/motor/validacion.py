"""Control de calidad de las fotos ANTES de reconstruir (etapas 2, 3 y 4 del motor).

Por cada foto mide: nitidez, exposición, tamaño y si es casi igual a otra.
Descarta las inútiles y, si no quedan suficientes, frena con un motivo claro
en lugar de gastar media hora de procesamiento en algo que va a salir mal.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 60_000_000  # protección contra imágenes "bomba"

MIN_LADO = 1600          # px del lado mayor
MIN_FOTOS = 20           # fotos útiles mínimas para un objeto
LADO_MEDIDA = 1024       # se mide sobre una copia de este tamaño


@dataclass
class Foto:
    nombre: str
    ancho: int = 0
    alto: int = 0
    nitidez: float = 0.0
    luz: float = 0.0
    quemado: float = 0.0
    oscuro: float = 0.0
    detalle: float = 0.0
    huella: int = 0
    ok: bool = True
    motivo: str = ''


@dataclass
class Informe:
    fotos: list = field(default_factory=list)
    usadas: list = field(default_factory=list)
    descartadas: dict = field(default_factory=dict)
    avisos: list = field(default_factory=list)

    def resumen(self):
        return {'total': len(self.fotos), 'usadas': len(self.usadas),
                'descartadas': {k: len(v) for k, v in self.descartadas.items()}, 'avisos': self.avisos,
                'nitidez_mediana': float(np.median([f.nitidez for f in self.fotos])) if self.fotos else 0.0}


def _gris(img: Image.Image, caja=None) -> np.ndarray:
    img = ImageOps.exif_transpose(img).convert('L')
    if caja:
        w, h = img.size
        x0, y0, x1, y1 = caja
        m = 0.06
        img = img.crop((max(0, int((x0 - m) * w)), max(0, int((y0 - m) * h)), min(w, int((x1 + m) * w)), min(h, int((y1 + m) * h))))
    img.thumbnail((LADO_MEDIDA, LADO_MEDIDA))
    return np.asarray(img, dtype=np.float32)


def _laplaciano(g: np.ndarray) -> np.ndarray:
    return g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]


def _huella(g: np.ndarray) -> int:
    """dHash de 16x16: dos fotos casi iguales tienen huellas casi iguales."""
    im = Image.fromarray(g.astype(np.uint8)).resize((17, 16), Image.BILINEAR)
    a = np.asarray(im, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int(''.join('1' if b else '0' for b in bits), 2)


def _distancia(a: int, b: int) -> int:
    return bin(a ^ b).count('1')


def medir(ruta: str, caja=None) -> Foto:
    f = Foto(nombre=os.path.basename(ruta))
    try:
        with Image.open(ruta) as im:
            im.verify()  # archivo dañado o que no es imagen
        with Image.open(ruta) as im:
            im = ImageOps.exif_transpose(im)
            f.ancho, f.alto = im.size
            g = _gris(im)
            gc = _gris(im, caja) if caja else g[g.shape[0] // 5: -g.shape[0] // 5, g.shape[1] // 5: -g.shape[1] // 5]
    except Exception as e:
        f.ok, f.motivo = False, f'no se pudo leer ({e.__class__.__name__})'
        return f
    lap = _laplaciano(g)
    f.nitidez = float(lap.var())
    f.detalle = float(_laplaciano(gc).var()) if gc.size > 100 else f.nitidez
    f.luz = float(g.mean())
    f.quemado = float((g > 250).mean())
    f.oscuro = float((g < 12).mean())
    f.huella = _huella(g)
    return f


def validar(rutas: list[str], cajas: dict | None = None) -> Informe:
    """Mide todas las fotos y decide cuáles se usan. No lanza error: devuelve el informe."""
    inf = Informe()
    cajas = cajas or {}
    for r in rutas:
        inf.fotos.append(medir(r, cajas.get(os.path.basename(r))))

    def descartar(f: Foto, motivo: str):
        f.ok, f.motivo = False, motivo
        inf.descartadas.setdefault(motivo, []).append(f.nombre)

    for f in inf.fotos:
        if not f.ok:
            inf.descartadas.setdefault('dañada', []).append(f.nombre)
    vivas = [f for f in inf.fotos if f.ok]
    for f in vivas:
        if max(f.ancho, f.alto) < MIN_LADO:
            descartar(f, 'baja resolución')
        elif f.luz < 35 or f.oscuro > 0.5:
            descartar(f, 'muy oscura')
        elif f.luz > 235 or f.quemado > 0.35:
            descartar(f, 'sobreexpuesta')
    vivas = [f for f in inf.fotos if f.ok]
    # nitidez relativa: comparada con las demás fotos del mismo escaneo (la escena es la misma)
    if vivas:
        med = float(np.median([f.nitidez for f in vivas]))
        for f in vivas:
            if f.nitidez < med * 0.35 or f.nitidez < 8:
                descartar(f, 'movida o desenfocada')
    # repetidas: casi idénticas a una foto anterior (se queda la más nítida)
    vivas = [f for f in inf.fotos if f.ok]
    for i, f in enumerate(vivas):
        for g in vivas[:i]:
            if g.ok and _distancia(f.huella, g.huella) <= 6:
                peor = f if f.nitidez <= g.nitidez else g
                descartar(peor, 'repetida')
                break
    inf.usadas = [f.nombre for f in inf.fotos if f.ok]
    usadas = [f for f in inf.fotos if f.ok]
    if usadas:
        det = float(np.median([f.detalle for f in usadas]))
        if det < 40:
            inf.avisos.append('El objeto tiene muy poca textura visible: puede salir con huecos o deformado.')
        if float(np.median([f.quemado for f in usadas])) > 0.08:
            inf.avisos.append('Hay reflejos fuertes: las partes brillantes pueden salir mal.')
    return inf


def suficiente(inf: Informe) -> tuple[bool, str]:
    n = len(inf.usadas)
    if n >= MIN_FOTOS:
        return True, ''
    partes = [f'{len(v)} {k}' for k, v in inf.descartadas.items()]
    detalle = f" (descartadas: {', '.join(partes)})" if partes else ''
    return False, (f'Solo {n} de {len(inf.fotos)} fotos sirven y hacen falta al menos {MIN_FOTOS}{detalle}. '
                   'Volvé a sacarlas con buena luz, el teléfono quieto en cada posición y dando la vuelta completa.')


def a_dict(inf: Informe) -> dict:
    d = inf.resumen()
    d['fotos'] = [{k: v for k, v in asdict(f).items() if k != 'huella'} for f in inf.fotos]
    return d
