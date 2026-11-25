"""Corre el motor completo sobre una carpeta de fotos (prueba de punta a punta sin API).
uso: python -m tests.correr_motor carpeta_fotos carpeta_trabajo ancho prof alto"""
import json, sys, time
sys.path.insert(0, 'app')
from motor.pipeline import reconstruir, Config  # noqa: E402

fotos, trabajo = sys.argv[1], sys.argv[2]
dims = dict(zip(('ancho', 'prof', 'alto'), map(float, sys.argv[3:6])))
t0 = time.time()
def avisar(pct, n, tot, txt):
    print(f'[{pct:3d}%] {n}/{tot} {txt}  ({int(time.time() - t0)} s)', flush=True)
r = reconstruir(fotos, trabajo, dims, avisar, cfg=Config.desde_entorno())
print(json.dumps({k: v for k, v in r.items() if k != 'metricas'}, ensure_ascii=False, indent=1, default=float))
print('METRICAS', json.dumps(r['metricas'].get('cobertura'), default=float), r['metricas'].get('textura_vacia'), r['metricas'].get('geometria'))
