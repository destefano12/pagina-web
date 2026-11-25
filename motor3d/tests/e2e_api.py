"""Prueba de punta a punta del motor por la API real (con todo levantado en Docker, MODO_PRUEBA=1).

Hace lo mismo que el panel: crear trabajo -> subir fotos -> enviar -> seguir el avance real -> revisar
-> publicar con medidas -> bajar el modelo público. Además prueba la seguridad básica.

uso: python3 tests/e2e_api.py carpeta_fotos [http://localhost]
Necesita las variables de tests/prueba.env (lee el archivo solo).
"""
import io
import json
import os
import sys
import time
import uuid

import httpx
import jwt

AQUI = os.path.dirname(os.path.abspath(__file__))
ENV = dict(l.strip().split('=', 1) for l in open(os.path.join(AQUI, 'prueba.env')) if '=' in l and not l.startswith('#'))
BASE = (sys.argv[2] if len(sys.argv) > 2 else 'http://localhost').rstrip('/')
API = BASE + '/api'
RID = ENV['PRUEBA_RESTAURANTES'].split(',')[0]
PID = str(uuid.uuid4())


def token(sub='22222222-2222-2222-2222-222222222222'):
    return jwt.encode({'sub': sub, 'aud': 'authenticated', 'exp': int(time.time()) + 7200}, ENV['PRUEBA_SECRETO'], algorithm='HS256')


H = {'Authorization': 'Bearer ' + token()}
c = httpx.Client(timeout=120)
fallas = []


def chequear(nombre, cond, extra=''):
    print(('OK   ' if cond else 'FALLA'), nombre, extra)
    if not cond:
        fallas.append(nombre)


# --- seguridad ---
chequear('sin token -> 401', c.get(f'{API}/v1/trabajos', params={'restaurante_id': RID}).status_code == 401)
chequear('token inventado -> 401', c.get(f'{API}/v1/trabajos', params={'restaurante_id': RID}, headers={'Authorization': 'Bearer x.y.z'}).status_code == 401)
otro = str(uuid.uuid4())
chequear('restaurante ajeno -> 403', c.post(f'{API}/v1/trabajos', headers=H, json={'restaurante_id': otro, 'plato_id': PID, 'medidas': {'ancho': 20, 'prof': 20, 'alto': 10}}).status_code == 403)
chequear('medidas imposibles -> 422', c.post(f'{API}/v1/trabajos', headers=H, json={'restaurante_id': RID, 'plato_id': PID, 'medidas': {'ancho': 0, 'prof': 20, 'alto': 10}}).status_code == 422)

tid = os.environ.get('SEGUIR')  # para retomar el seguimiento de un trabajo ya enviado
if not tid:
    # --- crear y subir ---
    r = c.post(f'{API}/v1/trabajos', headers=H, json={'restaurante_id': RID, 'plato_id': PID, 'medidas': {'ancho': 30, 'prof': 25, 'alto': 35}})
    chequear('crear trabajo', r.status_code == 201, r.text[:200])
    t = r.json(); tid = t['id']
    falsa = c.post(f'{API}/v1/trabajos/{tid}/fotos', headers=H, files=[('fotos', ('x.jpg', b'esto no es una foto', 'image/jpeg'))])
    chequear('archivo que no es imagen -> 415', falsa.status_code == 415)
    chequear('enviar sin fotos -> 422', c.post(f'{API}/v1/trabajos/{tid}/enviar', headers=H).status_code == 422)

    carpeta = sys.argv[1]
    fotos = sorted(f for f in os.listdir(carpeta) if f.lower().endswith(('.jpg', '.jpeg', '.png')))
    for i in range(0, len(fotos), 5):
        lote = [('fotos', (f, open(os.path.join(carpeta, f), 'rb').read(), 'image/png' if f.endswith('.png') else 'image/jpeg')) for f in fotos[i:i + 5]]
        r = c.post(f'{API}/v1/trabajos/{tid}/fotos', headers=H, files=lote)
        if r.status_code != 200:
            chequear('subir fotos', False, r.text[:200]); sys.exit(1)
    print('fotos subidas:', r.json()['fotos_subidas'])
    r = c.post(f'{API}/v1/trabajos/{tid}/enviar', headers=H)
    chequear('enviar a la cola', r.status_code == 200 and r.json()['estado'] == 'en_cola', r.text[:200])

# --- avance real ---
t0, ult, vistos = time.time(), None, []
while True:
    d = c.get(f'{API}/v1/trabajos/{tid}', headers=H).json()
    linea = f"{d['estado']} {d['progreso']}% etapa {d['etapa_num']}/{d['etapas_total']} {d['etapa']}"
    if linea != ult:
        print(f'[{int(time.time() - t0):5d} s] {linea}', flush=True); ult = linea
        vistos.append(d['progreso'])
    if d['estado'] in ('revision', 'fallo', 'cancelado'):
        break
    if time.time() - t0 > 3 * 3600:
        chequear('terminó a tiempo', False); break
    time.sleep(3)
chequear('el avance nunca retrocede', all(b >= a for a, b in zip(vistos, vistos[1:])) or d['estado'] != 'revision')
chequear('terminó en revisión (no se publica solo)', d['estado'] == 'revision', d.get('error') or '')
if d['estado'] != 'revision':
    print(json.dumps(d.get('eventos', [])[:10], ensure_ascii=False, indent=1)); sys.exit(1)
print('puntaje', d['puntaje'], 'aprobable', d['aprobable'], 'motivos', d['motivos'])
print('medidas del modelo', d['medidas_modelo'])
print('métricas', json.dumps({k: d['metricas'].get(k) for k in ('fotos_usadas', 'fotos_ubicadas', 'error_reproyeccion', 'cobertura', 'textura_vacia', 'geometria', 'escala')}, ensure_ascii=False, default=str))
print('archivos', json.dumps(d['metricas'].get('archivos'), ensure_ascii=False, default=str)[:600])

# --- vista previa firmada ---
vp = d['vista_previa']
glb = c.get(API + vp['movil'])
chequear('vista previa firmada', glb.status_code == 200 and glb.content[:4] == b'glTF', f'{len(glb.content)} bytes')
chequear('link firmado alterado -> 403', c.get(API + vp['movil'][:-3] + 'AAA').status_code == 403)
with open(os.path.join(os.environ.get('SALIDA', '.'), f'{tid}_movil.glb'), 'wb') as f:
    f.write(glb.content)
mae = c.get(API + vp['maestro'])
with open(os.path.join(os.environ.get('SALIDA', '.'), f'{tid}_maestro.glb'), 'wb') as f:
    f.write(mae.content)

# --- publicar ---
if d['aprobable']:
    r = c.post(f'{API}/v1/trabajos/{tid}/publicar', headers=H, json={'ancho': 30, 'prof': 25, 'alto': 35})
    chequear('publicar', r.status_code == 200, r.text[:200])
    url = r.json()['url_publica']
    pub = c.get(url.replace(ENV['URL_PUBLICA_MODELOS'].rsplit('/modelos', 1)[0], BASE))
    chequear('modelo público accesible para el menú', pub.status_code == 200 and pub.content[:4] == b'glTF', url)
    chequear('el bucket privado NO es público', c.get(BASE + '/modelos/../motor3d-trabajos/').status_code in (403, 404))
else:
    r = c.post(f'{API}/v1/trabajos/{tid}/publicar', headers=H, json={'ancho': 30, 'prof': 25, 'alto': 35})
    chequear('no se puede publicar un modelo que no pasó la calidad', r.status_code == 422)

print('\nRESULTADO:', 'TODO OK' if not fallas else f'{len(fallas)} fallas: {fallas}')
sys.exit(1 if fallas else 0)
