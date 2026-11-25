# Motor 3D propio para los menús: documentación

Este documento explica el sistema que convierte fotos de un plato real en un modelo 3D. El modelo aparece en el menú y en realidad aumentada (AR) con su tamaño real.

Todo el software es gratuito y de código abierto. Lo único que puede costar plata es la máquina donde corre (ver **Costos**).

---

## 1. Qué hace, en una línea por paso

1. El restaurante escanea el plato desde el panel. Son 18 paradas: 12 alrededor y 6 desde arriba, con 2 fotos por parada.
2. Antes de enviar, revisa las miniaturas. Las fotos movidas aparecen marcadas y cualquier parada se puede rehacer.
3. Las fotos se suben a la **API** del motor, que comprueba que sean imágenes reales, su tamaño y su resolución.
4. El trabajo entra en una **cola** (Redis + Celery). Un **worker** lo toma y corre las 13 etapas de reconstrucción.
5. El panel muestra el **avance real**: la etapa N de 13 y el porcentaje leído de la salida de cada programa. No hay barras simuladas.
6. Al terminar, el modelo pasa un **control de calidad** con puntaje de 0 a 100 y queda **"Para revisar"**. Nunca se publica solo.
7. El restaurante lo mira en 3D y en AR, confirma las **medidas reales** con una regla y aprieta **Aprobar y publicar**.
8. La versión para celular se copia al almacenamiento público y el plato del menú queda con su modelo y sus medidas.

## 2. Arquitectura

```
Panel (Netlify) ──HTTPS──> Caddy ──/api/*────> API (FastAPI) ──> PostgreSQL (trabajos, archivos, eventos)
                              │                   │   └──────> Redis (cola + límites de pedidos)
Menú (Netlify) ─GET─/modelos/*┘                   └──────────> SeaweedFS S3 (fotos, resultados, modelos)
                                                                     ▲
                    Worker (Celery) ── COLMAP + OpenMVS + gltfpack ──┘   + Mantenimiento (limpieza diaria, vigilancia)
```

| Pieza | Qué hace | Tecnología | Licencia |
|---|---|---|---|
| API | Recibe fotos, crea trabajos, muestra el avance, publica | FastAPI + Uvicorn (Python 3.12) | MIT / BSD |
| Cola | Ordena los trabajos y los reparte | Celery 5 + Redis 7 | BSD / BSD-3 (Redis 7.2 o anterior) |
| Base de datos | Estados, fechas, métricas, archivos y registro de eventos | PostgreSQL 16 | PostgreSQL (libre) |
| Almacenamiento | Fotos y resultados (privado), modelos publicados (lectura pública) | SeaweedFS (S3) | Apache-2.0 |
| Reconstrucción | Ubica las cámaras y arma la nube de puntos | COLMAP 3.9 | BSD |
| Malla y textura | Nube densa, superficie y textura con las fotos reales | OpenMVS 2.3 | AGPL-3.0 (sin modificar) |
| Optimización | Reduce caras respetando la textura y comprime para celular | gltfpack (meshoptimizer) 0.24 | MIT |
| HTTPS | Certificado automático y proxy | Caddy 2 | Apache-2.0 |
| Visor y AR | 3D en la web, Scene Viewer (Android), Quick Look (iPhone) y WebXR | model-viewer | Apache-2.0 |

**Autenticación.** El panel manda el token de sesión del restaurante (Supabase Auth). La API verifica la firma con las claves públicas del proyecto (JWKS, ES256/RS256) y le pregunta a la plataforma, con el token del propio usuario, si es dueño del restaurante (`es_duenio`). Un restaurante nunca ve ni toca los trabajos de otro. La API no tiene la clave `service_role`.

## 3. Por qué estas herramientas (comparación)

| Opción | Calidad para comida | ¿Funciona sin GPU? | Licencia | Decisión |
|---|---|---|---|---|
| **COLMAP + OpenMVS** | Muy buena: malla con textura de las fotos reales | **Sí** (más lento) | BSD + AGPL | **Elegida.** Es la única combinación madura que da GLB con textura real sin GPU. |
| Meshroom / AliceVision | Muy buena | No para los mapas de profundidad (CUDA) | MPL-2.0 | Descartada: obliga a pagar GPU siempre. |
| OpenMVG + OpenMVS | Buena | Sí | MPL-2.0 + AGPL | Similar a COLMAP, con menos mantenimiento y peor ubicación de cámaras con pocas fotos. |
| Gaussian Splatting (Inria) | Excelente a la vista | No | **No comercial** | Prohibido para un negocio. |
| gsplat / OpenSplat | Excelente a la vista | gsplat no; OpenSplat sí, unas 100 veces más lento | Apache-2.0 / AGPL | No genera una malla GLB fiable para AR (Quick Look y Scene Viewer necesitan malla). |
| Apps (Polycam, 3D Snap, Luma) | Buena | Corren en el teléfono o en su nube | Privativas | No tienen API abierta ni son gratis, y dependen de otra empresa. |

## 4. Las 13 etapas (con avance real)

| # | Etapa | Qué controla |
|---|---|---|
| 1 | Recibir las fotos | Que haya al menos 20 |
| 2 | Revisar la calidad | Nitidez, luz, reflejos y fotos repetidas. Descarta las malas y explica por qué |
| 3 | Buscar puntos | COLMAP: `feature_extractor` |
| 4 | Unir las fotos | `exhaustive_matcher` (o secuencial si son muchas) |
| 5 | Ubicar las cámaras | `mapper`. Exige que se ubique al menos la mitad de las fotos |
| 6 | Revisar los ángulos | Cobertura en 12 sectores. Si falta un lado (hueco > 110°) se rechaza y dice cuál |
| 7 | Preparar las fotos | `image_undistorter` |
| 8 | Nube densa | OpenMVS `DensifyPointCloud` |
| 9 | Superficie | `ReconstructMesh` |
| 10 | Separar el plato | Recorte por la mesa y por el recuadro que marcó el restaurante |
| 11 | Textura | `TextureMesh` con las fotos completas |
| 12 | Limpieza y escala | Endereza el modelo y le pone el tamaño real según la medida cargada (nunca se inventa) |
| 13 | Versiones | Maestra (≤ 600 mil caras, textura 4096) y celular (≤ 150 mil caras, 2048, comprimida, ≤ 15 MB), miniatura y validación de cada GLB |

**Puntaje de calidad (0 a 100).** Suman puntos: el porcentaje de fotos ubicadas (20), el error de reproyección (10), la cobertura de ángulos (20), la superficie sin color (20), el detalle de forma, las piezas y los agujeros (20), y que las proporciones coincidan con las medidas cargadas (10).

Un modelo **no se puede publicar** si saca menos de 60, si se ubicó menos del 60% de las fotos, si más del 15% de la superficie quedó sin color o si tiene menos de 15 mil caras. El panel muestra los motivos.

## 5. Estructura de archivos

```
motor3d/
  app/api/main.py          API: endpoints, límites, links firmados
  app/api/auth.py          Verificación de sesión y de dueño (Supabase)
  app/comun/config.py      Todas las variables de entorno
  app/comun/db.py          PostgreSQL
  app/comun/almacen.py     S3 (SeaweedFS o cualquier S3)
  app/comun/cola.py        Encolar sin cargar el motor
  app/worker/tareas.py     Worker: reconstrucción, reintento, limpieza y vigilancia
  app/motor/pipeline.py    Las 13 etapas y el avance real
  app/motor/validacion.py  Calidad de cada foto
  app/motor/geometria.py   Orientación, recorte del plato, escala
  app/motor/calidad.py     Cobertura, textura vacía, geometría, puntaje
  app/motor/exportar.py    Versión maestra, versión celular, miniatura, validación GLB
  sql/001_inicial.sql      Tablas trabajos, archivos, eventos
  docker/                  Dockerfiles (API y worker) y arranque del almacenamiento
  caddy/Caddyfile          HTTPS y rutas /api y /modelos
  docker-compose.yml       Todo el sistema; docker-compose.gpu.yml para NVIDIA
  .env.example             Variables a completar
  tests/                   Pruebas (ver sección 10)
```

## 6. Variables de entorno (`.env`)

| Variable | Para qué |
|---|---|
| `DOMINIO` | Dominio del servidor (puede ser gratis, por ejemplo `mimotor.duckdns.org`). Caddy saca el certificado HTTPS solo |
| `URL_PUBLICA_MODELOS` | `https://DOMINIO/modelos`. Es lo que queda guardado en el plato |
| `ORIGENES_PERMITIDOS` | Dominio del panel (CORS), por ejemplo `https://tu-sitio.netlify.app` |
| `POSTGRES_PASSWORD` | Clave de la base del motor |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Claves del almacenamiento |
| `S3_BUCKET_PRIVADO`, `S3_BUCKET_PUBLICO` | Nombres de los buckets |
| `SECRETO_FIRMAS` | 32 o más caracteres al azar (`openssl rand -hex 32`) para los links de vista previa |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Los mismos de `config.js`. La anon key es pública |
| `USAR_GPU`, `HILOS`, `DENSA_MAX`, `TEXTURA_MAX`, `LIMITE_TRABAJO_S` | Ajustes del motor |
| `MAX_FOTOS`, `MIN_FOTOS`, `MAX_MB_FOTO`, `MAX_TRABAJOS_ACTIVOS`, `MAX_PEDIDOS_MINUTO` | Límites contra abuso |
| `DIAS_GUARDAR_FOTOS` | Días que se guardan las fotos después de terminar (7 por defecto) |
| `MODO_PRUEBA` | **Siempre 0 en producción.** Solo lo usan las pruebas automáticas |

## 7. Instalación (en un servidor Linux, nunca en la PC personal)

Requisitos: 4 núcleos y 12 GB de RAM como mínimo (8 GB alcanzan con `DENSA_MAX=1200`), 40 GB de disco, Docker y un dominio.

```bash
git clone https://github.com/destefano12/pagina-web.git && cd pagina-web/motor3d
cp .env.example .env && nano .env          # completar claves y dominio
docker compose up -d --build                # la primera vez compila OpenMVS (unos 20 a 40 min)
curl https://TU-DOMINIO/api/salud           # {"postgres":"ok","redis":"ok","almacenamiento":"ok",...}
```

Después, en `config.js` del sitio, poner `motorUrl: 'https://TU-DOMINIO/api'` y volver a subir el sitio a Netlify. Desde ese momento el panel usa el motor propio y aparece la sección **Modelos 3D** completa. Mientras `motorUrl` esté vacío, el panel sigue con la nube actual y lo dice en esa sección.

**Oracle Cloud gratis (Always Free).** El plan gratuito da una máquina ARM Ampere de 2 núcleos y 12 GB (desde el 15 de junio de 2026), 200 GB de disco y 10 TB de transferencia por mes. Pide tarjeta para verificar la identidad, pero no cobra si no se pasa de esos límites. La imagen del worker se compila desde el código fuente, así que funciona en ARM.

Oracle recupera las máquinas gratis que pasan 7 días casi sin uso. Con 2 núcleos un plato tarda bastante más que en una máquina de 4 núcleos. Esto es una **estimación, no está medido** en Oracle todavía.

## 8. GPU (opcional)

Con una placa NVIDIA, `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d` hace que COLMAP use la GPU para buscar y unir puntos (hace falta `nvidia-container-toolkit`).

Para que también OpenMVS use CUDA, se cambia `OpenMVS_USE_CUDA=ON` en `docker/worker.Dockerfile` y la imagen base por `nvidia/cuda:12.x-devel-ubuntu24.04`. La etapa más lenta (nube densa) baja mucho.

## 9. Almacenamiento

- **Privado** (`motor3d-trabajos`): `trabajos/{id}/fotos/NNN.jpg`, `cajas.json`, `salida/maestro.glb`, `salida/movil.glb`, `salida/miniatura.jpg`, `registro.log`. Solo se ve con links firmados que vencen en 1 hora.
- **Público** (`motor3d-modelos`): `{restaurante}/{plato}/{trabajo}.glb` y `.jpg`. Solo lectura, con caché de un año (cada versión tiene nombre nuevo).
- **Limpieza diaria**: borra las fotos de los trabajos terminados hace más de `DIAS_GUARDAR_FOTOS` días, todo lo descartado, los trabajos abandonados sin enviar y los temporales de un worker caído. Lo publicado no se toca.
- Se puede cambiar SeaweedFS por cualquier S3 (Cloudflare R2, Backblaze B2, AWS o Garage) cambiando solo `S3_ENDPOINT` y las claves.

**Por qué no MinIO.** Era la opción más común, pero su edición gratuita dejó de publicar imágenes y quedó en modo mantenimiento a fines de 2025. Usarlo hoy sería apostar a algo sin futuro.

## 10. Pruebas

| Prueba | Cómo se corre | Resultado |
|---|---|---|
| Seguridad de la API (sin token, token falso, restaurante ajeno, archivo falso, medidas imposibles, link firmado alterado, bucket privado) | `python3 tests/e2e_api.py fotos` con todo levantado en modo prueba | ver abajo |
| Punta a punta por la API, con un objeto real de 67 fotos (dataset Buddha de AliceVision, CC-BY 4.0) | lo mismo | ver abajo |
| Motor solo, sin API | `python -m tests.correr_motor fotos trabajo ancho prof alto` | ver abajo |
| Escáner del panel (18 paradas, revisión, rehacer una parada, subida) | navegador automático con cámara simulada | Pasa: 36 fotos más los recuadros, sin errores |
| Rechazo por cobertura | dataset Monstree (41 fotos de un solo lado) | Rechazado bien: *"Faltaron fotos de atrás-izquierda (hueco de 267°)"* |

**Pendiente, sin inventar resultados:** probar con comidas reales (hamburguesa, pizza, torta, ensalada, empanadas, pasta y postre). Eso necesita escaneos hechos con el teléfono en el restaurante.

## 11. Limitaciones conocidas

- **Superficies que brillan, transparentes o con vapor** (vasos, salsas brillantes, sopas calientes) salen mal con cualquier fotogrametría. El escáner avisa en el momento si hay reflejos o si el objeto es muy liso.
- **Sin GPU es lento.** El tiempo depende de la máquina y de la cantidad de fotos; el panel muestra el avance real.
- **La escala sale de la medida que carga el restaurante**, no de las fotos. Por eso se le pide confirmarla con una regla antes de publicar.
- Un worker procesa un plato a la vez. Para más volumen se levantan más workers (`docker compose up -d --scale worker=2`) si la máquina tiene memoria.

## 12. Costos

| Concepto | Costo |
|---|---|
| Todo el software (COLMAP, OpenMVS, gltfpack, FastAPI, Celery, Redis, PostgreSQL, SeaweedFS, Caddy, model-viewer) | **Gratis**, código abierto |
| Servidor Oracle Always Free (2 núcleos ARM, 12 GB) | **Gratis con límites** (pide tarjeta, no cobra dentro del límite) |
| Dominio | Gratis con DuckDNS, o unos 10 USD por año si es propio |
| Servidor con GPU por hora (opcional) | **Pago**: más o menos 0,3 a 1 USD por hora según el proveedor |
| VPS de 4 núcleos y 16 GB (si no se usa Oracle) | **Pago**: más o menos 10 a 30 USD por mes |
| Almacenamiento y transferencia | Gratis dentro del disco del servidor y de los 10 TB por mes de Oracle |
| Netlify y Supabase (lo que ya existe) | Plan gratis actual, sin cambios |

## 13. Licencias

- **OpenMVS es AGPL-3.0.** Se usa sin modificar, como programa aparte que el worker ejecuta. Si algún día se modifica su código y se ofrece como servicio, hay que publicar esos cambios. El resto del sistema no queda alcanzado por usarlo así. El autor de OpenMVS no da una interpretación legal oficial sobre el uso como servicio; ante una duda puntual, conviene consultar a un abogado.
- **COLMAP** es BSD. Algunas dependencias tienen licencia propia; se usa el paquete oficial de Ubuntu.
- **gltfpack/meshoptimizer** es MIT. **SeaweedFS**, **Caddy** y **model-viewer** son Apache-2.0. **FastAPI**, **Celery** y **PostgreSQL** tienen licencias permisivas.
- **Redis:** se usa la versión 7.2 o anterior, que es BSD. Las versiones nuevas cambiaron de licencia, pero para usarla adentro del propio servidor no hay problema.
- **No se usa** el Gaussian Splatting original de Inria, porque su licencia prohíbe el uso comercial.
- Las fotos de prueba de `tests/` (Buddha de AliceVision) son CC-BY 4.0 y no se incluyen en el repositorio.
