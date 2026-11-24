"""Nube de escaneo 3D corriendo en GitHub Actions.

Habla con la plataforma a través de la función "nube" de Supabase. Se identifica con el token
que GitHub firma para esta tarea (OIDC), así que no hace falta guardar ninguna clave secreta.
Procesa todos los escaneos que haya en la cola y termina.
"""
import json
import os
import shutil
import sys
import time
import traceback
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fotogrametria import ErrorFoto, procesar  # noqa: E402

NUBE = os.environ.get('NUBE_URL', 'https://xeajsygiqueyzxemcbxl.supabase.co/functions/v1/nube')
TRABAJOS = os.environ.get('TRABAJOS_DIR', '/tmp/trabajos')
NOMBRE = os.environ.get('WORKER_NOMBRE', 'github')


def token_github():
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=menu3d-nube'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['value']


def nube(accion, **datos):
    cuerpo = json.dumps({'accion': accion, **datos}).encode()
    ultimo = None
    for intento in range(5):
        try:
            req = urllib.request.Request(NUBE, data=cuerpo, method='POST',
                                         headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token_github()})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            ultimo = RuntimeError(f'{accion}: {e.code} {e.read().decode("utf-8", "replace")[:300]}')
            if e.code in (400, 401, 403):
                raise ultimo
        except Exception as e:  # red
            ultimo = e
        time.sleep(3 * (intento + 1))
    raise ultimo


def bajar(url, destino):
    for intento in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(destino, 'wb') as f:
                shutil.copyfileobj(r, f)
            return
        except Exception:
            if intento == 3:
                raise
            time.sleep(3)


def subir(url, ruta):
    datos = open(ruta, 'rb').read()
    req = urllib.request.Request(url, data=datos, method='PUT', headers={'Content-Type': 'model/gltf-binary', 'x-upsert': 'true'})
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()


def hacer(r):
    e = r['trabajo']; eid = e['id']
    carpeta = os.path.join(TRABAJOS, eid); fotos = os.path.join(carpeta, 'fotos'); os.makedirs(fotos, exist_ok=True)
    print(f'\n== Escaneo {eid}: {len(r["fotos"])} fotos, ancho {r["ancho_cm"]} cm', flush=True)
    ultimo = [0.0]

    def avisar(pct, etapa):
        print(f'  [{pct:3d}%] {etapa}', flush=True)
        if time.time() - ultimo[0] > 5 or pct >= 90:
            ultimo[0] = time.time()
            try:
                nube('actualizar', id=eid, campos={'progreso': int(pct), 'etapa': etapa})
            except Exception as ex:
                print('  (no pude avisar el progreso:', ex, ')', flush=True)

    try:
        avisar(3, 'Bajando las fotos')
        for f in r['fotos']:
            bajar(f['url'], os.path.join(fotos, f['nombre']))
        glb = procesar(fotos, os.path.join(carpeta, 'trabajo'), float(r['ancho_cm']), avisar=avisar, hilos=0, gpu=False)
        avisar(97, 'Publicando el modelo')
        lugar = nube('lugar_modelo', id=eid)
        subir(lugar['subir'], glb)
        fin = nube('terminar', id=eid, camino=lugar['camino'])
        print('  LISTO ->', fin.get('url'), flush=True)
    except ErrorFoto as ex:
        print('  FALLÓ (fotos):', ex, flush=True)
        nube('actualizar', id=eid, campos={'estado': 'fallo', 'etapa': 'No se pudo armar', 'error': str(ex)})
    except Exception as ex:
        traceback.print_exc()
        reg = os.path.join(carpeta, 'trabajo', 'registro.log')
        if os.path.exists(reg):
            print(open(reg, encoding='utf-8', errors='replace').read()[-4000:])
        nube('actualizar', id=eid, campos={'estado': 'fallo', 'etapa': 'Error de la nube', 'error': f'Error al procesar: {ex}'[:300]})
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'hay':
        n = nube('hay')['pendientes']
        print(f'Escaneos en la cola: {n}')
        with open(os.environ.get('GITHUB_OUTPUT', '/dev/null'), 'a') as f:
            f.write(f'hay={"si" if n else "no"}\n')
        return
    while True:
        r = nube('tomar', worker=NOMBRE)
        if not r.get('trabajo'):
            print('No hay más escaneos en la cola.')
            return
        hacer(r)


if __name__ == '__main__':
    main()
