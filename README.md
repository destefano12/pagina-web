# Nube de escaneo 3D · Menú 3D

Convierte las ~40 fotos que saca cada restaurante en un modelo 3D real de su plato (fotogrametría con COLMAP + OpenMVS) y lo publica en su menú.

Corre gratis en GitHub Actions: cada 5 minutos se fija si hay platos en la cola. Si hay, arma los modelos y los sube a la plataforma. No necesita claves guardadas: se identifica con el token que GitHub firma para esta tarea.

- `nube3d/fotogrametria.py`: fotos → modelo .glb recortado de la mesa, derecho y a tamaño real.
- `nube3d/nube_github.py`: toma los trabajos de la cola y publica el resultado.
- `herramientas/openmvs/`: OpenMVS 2.3 compilado para Ubuntu 24.04.

Para correrla a mano: pestaña **Actions** → "Nube de escaneo 3D" → **Run workflow**.
