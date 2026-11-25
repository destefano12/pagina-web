"""Pruebas rápidas (sin COLMAP ni OpenMVS): control de fotos, puntaje de calidad, progreso y validación de GLB.
uso: python -m pytest -q tests/test_unidad.py   (desde motor3d/)"""
import os
import sys

import numpy as np
import pytest
import trimesh
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from motor import calidad, exportar, validacion  # noqa: E402
from motor.pipeline import Avance, LectorDensa, _fraccion_de  # noqa: E402


def _foto(ruta, semilla=0, borrosa=False, oscura=False, lado=2000):
    rng = np.random.default_rng(semilla)
    a = (rng.random((lado * 3 // 4 // 8, lado // 8, 3)) * 255).astype(np.uint8)
    im = Image.fromarray(a).resize((lado, lado * 3 // 4), Image.NEAREST)
    if borrosa:
        im = im.filter(ImageFilter.GaussianBlur(12))
    if oscura:
        im = Image.fromarray((np.asarray(im) * 0.05).astype(np.uint8))
    im.save(ruta, quality=92)
    return ruta


def test_descarta_borrosas_oscuras_repetidas_y_chicas(tmp_path):
    rutas = [_foto(tmp_path / f'{i:03d}.jpg', semilla=i) for i in range(24)]
    rutas.append(_foto(tmp_path / 'borrosa.jpg', semilla=100, borrosa=True))
    rutas.append(_foto(tmp_path / 'oscura.jpg', semilla=101, oscura=True))
    rutas.append(_foto(tmp_path / 'chica.jpg', semilla=102, lado=1000))
    Image.open(rutas[0]).save(tmp_path / 'repetida.jpg', quality=90); rutas.append(str(tmp_path / 'repetida.jpg'))
    inf = validacion.validar([str(r) for r in rutas])
    usadas = set(inf.usadas)
    for mala in ('borrosa.jpg', 'oscura.jpg', 'chica.jpg'):
        assert mala not in usadas, mala
    assert not ({'000.jpg', 'repetida.jpg'} <= usadas), 'una de las dos repetidas tiene que salir'
    ok, _ = validacion.suficiente(inf)
    assert ok


def test_pocas_fotos_no_alcanza(tmp_path):
    rutas = [str(_foto(tmp_path / f'{i}.jpg', semilla=i)) for i in range(8)]
    ok, motivo = validacion.suficiente(validacion.validar(rutas))
    assert not ok and 'hacen falta' in motivo


BUENO = {'fotos_ubicadas': 60, 'fotos_usadas': 62, 'error_reproyeccion': 0.8,
         'cobertura': {'sectores_cubiertos': 12, 'sectores_total': 12, 'hueco_mayor_grados': 35, 'fotos_desde_arriba': 8, 'lado_sin_fotos': None},
         'textura_vacia': 0.01, 'geometria': {'caras': 300000, 'piezas': 1, 'borde_abierto_relativo': 1.0},
         'escala': {'relacion_ok': True}}


def test_puntaje_modelo_bueno():
    pts, ok, motivos = calidad.puntaje(BUENO)
    assert ok and pts >= 90, (pts, motivos)


@pytest.mark.parametrize('cambio,texto', [
    ({'fotos_ubicadas': 20}, 'ubicar'),
    ({'textura_vacia': 0.3}, 'sin color'),
    ({'geometria': {'caras': 5000, 'piezas': 1, 'borde_abierto_relativo': 1}}, 'poco detalle'),
])
def test_puntaje_criticos_no_se_publican(cambio, texto):
    pts, ok, motivos = calidad.puntaje(BUENO | cambio)
    assert not ok and any(texto in m for m in motivos), (pts, motivos)


def test_cobertura_rechaza_un_lado_sin_fotos():
    ok, motivo = calidad.cobertura_suficiente({'hueco_mayor_grados': 200, 'lado_sin_fotos': 'de atrás'})
    assert not ok and 'de atrás' in motivo


def test_avance_nunca_retrocede():
    vistos = []
    av = Avance(lambda pct, n, tot, txt: vistos.append(pct), lambda: False)
    for etapa in range(1, 14):
        av.entrar(etapa)
        for f in (0.5, 0.2, 1.0, 0.1):
            av.fraccion(f)
    assert vistos == sorted(vistos) and vistos[-1] <= 99


def test_lectura_de_avance():
    assert _fraccion_de('Processed file [5/50]', None) == pytest.approx(0.1)
    assert _fraccion_de('Matching block [1/2, 1/2]', None) == pytest.approx(0.25)
    ld = LectorDensa(10)
    assert ld('Selecting images for dense reconstruction completed: 4 images (1ms)') is None
    assert ld('Depth-map for image   3 estimated using  5 images: 1368x770 (10s)') == pytest.approx(0.85 / 4)
    assert ld('Point-cloud composed of 12345 points with:') == 1.0


def test_validar_glb(tmp_path):
    m = trimesh.creation.box(extents=(0.2, 0.2, 0.05))
    tex = Image.fromarray((np.random.default_rng(1).random((64, 64, 3)) * 255).astype(np.uint8))
    m.visual = trimesh.visual.TextureVisuals(uv=np.random.default_rng(2).random((len(m.vertices), 2)), image=tex)
    ruta = str(tmp_path / 'caja.glb'); m.export(ruta)
    info = exportar.validar_glb(ruta)
    assert info['ok'], info
    assert info['caras'] == 12 and info['textura']
    sin = str(tmp_path / 'sin.glb'); trimesh.creation.box().export(sin)
    assert not exportar.validar_glb(sin)['ok']
