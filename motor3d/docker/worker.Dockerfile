# syntax=docker/dockerfile:1
# Worker de reconstrucción: COLMAP (BSD) + OpenMVS (AGPL-3.0, sin modificar) + gltfpack (MIT).
# Se compila desde el código fuente, así sirve en x86_64 y en ARM (por ejemplo Oracle Ampere).
ARG UBUNTU=24.04

FROM ubuntu:${UBUNTU} AS compilar
ARG OPENMVS_VERSION=v2.3.0
ARG MESHOPT_VERSION=v0.24
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential cmake ninja-build \
      libeigen3-dev libboost-iostreams-dev libboost-program-options-dev libboost-system-dev libboost-serialization-dev \
      libopencv-dev libcgal-dev libjpeg-dev libpng-dev libtiff-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# (opcional) certificado extra si la red pasa por un proxy que inspecciona HTTPS: docker build --secret id=ca,src=ca.crt
RUN --mount=type=secret,id=ca,required=false if [ -s /run/secrets/ca ]; then cp /run/secrets/ca /usr/local/share/ca-certificates/extra.crt && update-ca-certificates; fi
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /vcglib \
 && git clone --depth 1 --branch ${OPENMVS_VERSION} https://github.com/cdcseacave/openMVS.git /openmvs \
 && cmake -S /openmvs -B /b-mvs -G Ninja -DCMAKE_BUILD_TYPE=Release -DVCG_ROOT=/vcglib \
      -DOpenMVS_USE_CUDA=OFF -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_BUILD_VIEWER=OFF -DOpenMVS_USE_BREAKPAD=OFF \
      -DCMAKE_CXX_FLAGS="-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" -DCMAKE_C_FLAGS="-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" \
      -DCMAKE_INSTALL_PREFIX=/opt/openmvs \
 && cmake --build /b-mvs \
 && cmake --install /b-mvs
RUN git clone --depth 1 --branch ${MESHOPT_VERSION} https://github.com/zeux/meshoptimizer.git /meshopt \
 && cmake -S /meshopt -B /b-mo -G Ninja -DCMAKE_BUILD_TYPE=Release -DMESHOPT_BUILD_GLTFPACK=ON \
 && cmake --build /b-mo --target gltfpack \
 && install -m 755 /b-mo/gltfpack /usr/local/bin/gltfpack

FROM ubuntu:${UBUNTU}
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app \
    MVS_DIR=/opt/openmvs/bin/OpenMVS COLMAP_BIN=colmap CARPETA_TEMPORAL=/trabajos
RUN apt-get update && apt-get install -y --no-install-recommends \
      colmap python3 python3-pip python3-venv ca-certificates \
      libboost-iostreams1.83.0 libboost-program-options1.83.0 libboost-serialization1.83.0 libboost-system1.83.0 \
      libopencv-core406t64 libopencv-imgproc406t64 libopencv-imgcodecs406t64 libopencv-calib3d406t64 libopencv-features2d406t64 \
      libgomp1 libgmp10 libmpfr6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=compilar /opt/openmvs /opt/openmvs
COPY --from=compilar /usr/local/bin/gltfpack /usr/local/bin/gltfpack
COPY docker/requirements-worker.txt docker/bloqueo-worker.txt /tmp/
RUN --mount=type=secret,id=ca,required=false python3 -m venv /venv \
 && if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi && /venv/bin/pip install --no-cache-dir -r /tmp/requirements-worker.txt -c /tmp/bloqueo-worker.txt
ENV PATH=/venv/bin:$PATH
RUN useradd -r -u 10001 motor && mkdir -p /trabajos && chown motor /trabajos \
 && test -x "$MVS_DIR/DensifyPointCloud" && colmap -h > /dev/null && gltfpack -h > /dev/null 2>&1 || gltfpack 2>&1 | grep -q gltfpack
COPY app/motor /app/motor
COPY app/comun /app/comun
COPY app/worker /app/worker
USER motor
WORKDIR /app
CMD ["celery", "-A", "worker.tareas:celery_app", "worker", "-Q", "reconstruccion", "--concurrency", "1", "--prefetch-multiplier", "1", "-l", "info"]
