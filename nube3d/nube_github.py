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


def subir(url, ruta, tipo='model/gltf-binary'):
    datos = open(ruta, 'rb').read()
    req = urllib.request.Request(url, data=datos, method='PUT', headers={'Content-Type': tipo, 'x-upsert': 'true'})
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()


MOTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'motor3d', 'app')


def _sin_detalle(metricas):
    """Métricas para guardar en la plataforma: sin la lista foto por foto (pesa mucho y no hace falta para revisar)."""
    m = {k: v for k, v in metricas.items() if k != 'fotos'}
    return json.loads(json.dumps(m, default=float))


def hacer_motor(r):
    """Platos de los restaurantes: motor profesional (13 etapas, control de calidad). El modelo queda PARA REVISAR;
    lo publica el restaurante desde el panel después de verlo y confirmar las medidas."""
    if MOTOR not in sys.path:
        sys.path.insert(0, MOTOR)
    from motor.geometria import ErrorFoto
    from motor.pipeline import Config, reconstruir
    e = r['trabajo']; eid = e['id']
    carpeta = os.path.join(TRABAJOS, eid); fotos = os.path.join(carpeta, 'fotos'); os.makedirs(fotos, exist_ok=True)
    medidas = r.get('medidas') or {}
    if not medidas and r.get('ancho_cm'):
        medidas = {'ancho': float(r['ancho_cm']), 'prof': float(r['ancho_cm']), 'alto': 6.0}
    print(f'\n== Plato {eid} (motor profesional): {len(r["fotos"])} fotos, medidas cargadas {medidas}', flush=True)
    ultimo = [0.0, None]

    def avisar(pct, num, total, texto):
        cambio = ultimo[1] != num
        if cambio:
            print(f'  [{pct:3d}%] etapa {num}/{total}: {texto}', flush=True)
        if cambio or time.time() - ultimo[0] > 8:
            ultimo[0], ultimo[1] = time.time(), num
            try:
                nube('actualizar', id=eid, campos={'progreso': int(pct), 'etapa': texto, 'etapa_num': num, 'etapas_total': total})
            except Exception as ex:
                print('  (no pude avisar el progreso:', ex, ')', flush=True)

    try:
        avisar(0, 1, 13, 'Bajando las fotos')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda f: bajar(f['url'], os.path.join(fotos, f['nombre'])), r['fotos']))
        cajas = None
        if r.get('cajas'):
            try:
                with urllib.request.urlopen(r['cajas'], timeout=60) as cr:
                    cajas = json.loads(cr.read())
            except Exception as ex:
                print('  (sin recuadros del objeto:', ex, ')', flush=True)
        res = reconstruir(fotos, os.path.join(carpeta, 'trabajo'), medidas, avisar, cajas=cajas, cfg=Config.desde_entorno())
        rutas = res['archivos']['rutas']
        lugar = nube('lugar_modelo', id=eid)
        subir(lugar['subir'], rutas['movil'])  # versión para celular (la que ve el cliente en el menú y en AR)
        mini = False
        if rutas.get('miniatura') and lugar.get('mini'):
            try:
                subir(lugar['mini']['subir'], rutas['miniatura'], 'image/jpeg'); mini = True
            except Exception as ex:
                print('  (no pude subir la miniatura:', ex, ')', flush=True)
        metricas = _sin_detalle(res['metricas'] | {'archivos': {k: v for k, v in res['archivos'].items() if k != 'rutas'},
                                                   'etapas': res['etapas']})
        nube('revision', id=eid, camino=lugar['camino'], mini=mini, puntaje=res['puntaje'], aprobable=res['aprobable'],
             motivos=res['motivos'], metricas=metricas, medidas=res['medidas_cm'])
        print(f"  PARA REVISAR: puntaje {res['puntaje']}/100, aprobable={res['aprobable']}, medidas {res['medidas_cm']}", flush=True)
        for m in res['motivos']:
            print('   -', m, flush=True)
    except ErrorFoto as ex:
        print('  FALLÓ (fotos):', ex, flush=True)
        nube('actualizar', id=eid, campos={'estado': 'fallo', 'etapa': 'No se pudo armar el modelo', 'error': str(ex)[:500]})
    except Exception as ex:
        traceback.print_exc()
        reg = os.path.join(carpeta, 'trabajo', 'registro.log')
        if os.path.exists(reg):
            print(open(reg, encoding='utf-8', errors='replace').read()[-4000:])
        nube('actualizar', id=eid, campos={'estado': 'fallo', 'etapa': 'Error técnico',
                                           'error': 'Falló el proceso por un error técnico. Probá reprocesar.'})
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


def hacer(r):
    if r.get('cola', 'menu') == 'menu':
        return hacer_motor(r)
    from fotogrametria import ErrorFoto, procesar  # Metra 3D sigue con su proceso propio
    e = r['trabajo']; eid = e['id']
    cola = r.get('cola', 'menu'); modo = r.get('modo') or 'plato'
    carpeta = os.path.join(TRABAJOS, eid); fotos = os.path.join(carpeta, 'fotos'); os.makedirs(fotos, exist_ok=True)
    print(f'\n== Escaneo {eid} ({cola}/{modo}): {len(r["fotos"])} fotos, medida {r.get("ancho_cm")} cm', flush=True)
    ultimo = [0.0]

    def avisar(pct, etapa):
        print(f'  [{pct:3d}%] {etapa}', flush=True)
        if time.time() - ultimo[0] > 5 or pct >= 90:
            ultimo[0] = time.time()
            try:
                nube('actualizar', id=eid, cola=cola, campos={'progreso': int(pct), 'etapa': etapa})
            except Exception as ex:
                print('  (no pude avisar el progreso:', ex, ')', flush=True)

    try:
        avisar(1, 'Bajando las fotos')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:  # de a 8 a la vez: 45 fotos 4K en segundos
            list(ex.map(lambda f: bajar(f['url'], os.path.join(fotos, f['nombre'])), r['fotos']))
        ancho = float(r['ancho_cm']) if r.get('ancho_cm') else None
        cajas = None
        if r.get('cajas'):  # recuadro del objeto marcado en la cámara, foto por foto
            try:
                with urllib.request.urlopen(r['cajas'], timeout=60) as cr:
                    cajas = json.loads(cr.read())
                print(f'  recuadros del objeto: {len(cajas)} fotos', flush=True)
            except Exception as ex:
                print('  (no pude bajar los recuadros:', ex, ')', flush=True)
        glb = procesar(fotos, os.path.join(carpeta, 'trabajo'), ancho, avisar=avisar, mvs_dir=os.environ.get('MVS_DIR') or None, hilos=0, gpu=False, modo=modo, cajas=cajas)
        dims = {}
        if os.path.exists(glb + '.json'):
            dims = json.load(open(glb + '.json'))
        avisar(97, 'Publicando el modelo')
        lugar = nube('lugar_modelo', id=eid, cola=cola)
        subir(lugar['subir'], glb)
        fin = nube('terminar', id=eid, cola=cola, camino=lugar['camino'], dims=dims)
        print('  LISTO ->', fin.get('url') or fin.get('camino'), dims, flush=True)
    except ErrorFoto as ex:
        print('  FALLÓ (fotos):', ex, flush=True)
        nube('actualizar', id=eid, cola=cola, campos={'estado': 'fallo', 'etapa': 'No se pudo armar', 'error': str(ex)})
    except Exception as ex:
        traceback.print_exc()
        reg = os.path.join(carpeta, 'trabajo', 'registro.log')
        if os.path.exists(reg):
            print(open(reg, encoding='utf-8', errors='replace').read()[-4000:])
        nube('actualizar', id=eid, cola=cola, campos={'estado': 'fallo', 'etapa': 'Error de la nube', 'error': f'Error al procesar: {ex}'[:300]})
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


HORARIO = (9, 24)  # hora de Argentina en la que la nube queda esperando escaneos
VIGILAR_MIN = 285  # cada tarea vigila unas 4 h 45 min y deja otra en su lugar


def en_horario():
    h = (time.gmtime().tm_hour - 3) % 24
    return HORARIO[0] <= h < HORARIO[1]


def relevo():
    """Arranca la próxima tarea (la que sigue esperando) con la clave propia de esta tarea de GitHub."""
    tok, repo = os.environ.get('GH_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
    if not tok or not repo:
        return
    req = urllib.request.Request(f'https://api.github.com/repos/{repo}/actions/workflows/nube3d.yml/dispatches',
                                 data=json.dumps({'ref': 'main'}).encode(), method='POST',
                                 headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json'})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        print('Dejé la próxima tarea en marcha.', flush=True)
    except Exception as e:
        print('No pude dejar la próxima tarea:', e, flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'hay':
        n = nube('hay')['pendientes']
        print(f'Escaneos en la cola: {n}')
        with open(os.environ.get('GITHUB_OUTPUT', '/dev/null'), 'a') as f:
            f.write(f'hay={"si" if n or en_horario() else "no"}\n')
        return
    inicio, vacio = time.time(), 0
    while True:
        r = nube('tomar', worker=NOMBRE)
        if r.get('trabajo'):
            vacio = 0
            hacer(r)
            continue
        # La cola está vacía: se espera un rato por si alguien está terminando de subir otro escaneo,
        # así no queda esperando a la próxima vuelta.
        vacio += 1
        if en_horario():
            # en horario, la nube queda esperando; al final deja otra tarea en su lugar
            if time.time() - inicio > VIGILAR_MIN * 60:
                relevo()
                return
            time.sleep(15)
            continue
        if vacio > 8 or time.time() - inicio > 120 * 60:
            print('No hay más escaneos en la cola.')
            return
        time.sleep(20)


if __name__ == '__main__':
    main()
