#!/bin/bash
# Instalador automático del motor 3D en un servidor Ubuntu 24.04 (pensado para Oracle Cloud Always Free, sirve en cualquiera).
#
# Uso: pegar este archivo entero en "Cloud-init script" al crear la máquina (Oracle: Show advanced options > Management),
# o correrlo como root:  curl -fsSL https://raw.githubusercontent.com/destefano12/pagina-web/main/motor3d/despliegue/instalar-servidor.sh | sudo bash
#
# Qué hace, solo:
#   - instala Docker, abre los puertos 80/443 del firewall de Ubuntu y suma memoria de intercambio (8 GB)
#   - baja el código del motor, genera claves al azar (no hay ninguna clave en el repositorio) y arma el .env
#   - usa un nombre gratis tipo 1-2-3-4.sslip.io (apunta a la IP del servidor) con HTTPS automático de Let's Encrypt
#   - compila y levanta todo (la primera vez tarda entre 40 y 90 minutos en 2 núcleos ARM)
#   - deja una actualización semanal automática del motor (domingos 6:30 UTC)
# Registro de la instalación: /var/log/motor3d-instalacion.log   ·   Resultado: /opt/motor3d-estado.txt
set -euo pipefail
exec > >(tee -a /var/log/motor3d-instalacion.log) 2>&1
echo "== Motor 3D: instalación $(date -u +%FT%TZ) =="

REPO=https://github.com/destefano12/pagina-web.git
DIR=/opt/pagina-web
SUPABASE_URL=https://xeajsygiqueyzxemcbxl.supabase.co
# clave pública (anon) de la plataforma: es la misma que ya está en config.js del sitio, no es secreta
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhlYWpzeWdpcXVleXp4ZW1jYnhsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTU0Nzg3OTcsImV4cCI6MjExMTA1NDc5N30.L_6HjXPkqk0ncLwF_80qsoQYvqprNmfaS2caVZbIUcE

echo "instalando" > /opt/motor3d-estado.txt
export DEBIAN_FRONTEND=noninteractive
echo 'iptables-persistent iptables-persistent/autosave_v4 boolean true' | debconf-set-selections
echo 'iptables-persistent iptables-persistent/autosave_v6 boolean true' | debconf-set-selections
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 docker-buildx git openssl curl iptables-persistent unattended-upgrades
systemctl enable --now docker || service docker start || true

# memoria de intercambio: colchón para los picos de la nube densa
if ! swapon --show | grep -q /swapfile; then
  if fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile; then
    grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    echo "aviso: no se pudo agregar memoria de intercambio"; rm -f /swapfile
  fi
fi

# las imágenes de Ubuntu en Oracle traen el firewall cerrado salvo SSH: se abren solo 80 y 443
for p in 443 80; do
  iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport $p -j ACCEPT || echo "aviso: no se pudo abrir el puerto $p"
done
netfilter-persistent save || true

# código
if [ -d $DIR/.git ]; then git -C $DIR pull --ff-only; else git clone --depth 1 $REPO $DIR; fi
cd $DIR/motor3d

# nombre y claves
IP=${IP_FORZADA:-$(curl -fsS https://api.ipify.org || curl -fsS https://checkip.amazonaws.com || curl -fsS https://ifconfig.me || true)}
IP=$(echo "$IP" | tr -d '[:space:]')
if ! echo "$IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "No pude averiguar la IP pública del servidor" | tee /opt/motor3d-estado.txt; exit 1
fi
DOM="$(echo "$IP" | tr . -).sslip.io"
al_azar() { openssl rand -hex "$1"; }
if [ ! -f .env ]; then
  cat > .env <<EOF
DOMINIO=$DOM
URL_PUBLICA_MODELOS=https://$DOM/modelos
ORIGENES_PERMITIDOS=http://localhost:8798
POSTGRES_PASSWORD=$(al_azar 24)
S3_ENDPOINT=http://almacen:8333
S3_ACCESS_KEY=motor$(al_azar 6)
S3_SECRET_KEY=$(al_azar 24)
S3_BUCKET_PRIVADO=motor3d-trabajos
S3_BUCKET_PUBLICO=motor3d-modelos
REDIS_URL=redis://redis:6379/0
SECRETO_FIRMAS=$(al_azar 32)
SUPABASE_URL=$SUPABASE_URL
SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY
USAR_GPU=0
DENSA_MAX=1400
TEXTURA_MAX=4096
LIMITE_TRABAJO_S=21600
LIMITE_PASO_S=14400
MAX_FOTOS=150
MIN_FOTOS=20
MAX_MB_FOTO=20
MAX_TRABAJOS_ACTIVOS=3
MAX_PEDIDOS_MINUTO=240
DIAS_GUARDAR_FOTOS=7
MODO_PRUEBA=0
EOF
  chmod 600 .env
fi

if [ "${MOTOR3D_SOLO_PREPARAR:-0}" = 1 ]; then echo "preparado (sin compilar): $DOM"; exit 0; fi
echo "compilando (puede tardar más de una hora la primera vez)" > /opt/motor3d-estado.txt
docker compose build
docker compose up -d

# esperar a que responda por HTTPS (Caddy saca el certificado solo)
for i in $(seq 1 60); do
  if curl -fsS "https://$DOM/api/salud" >/dev/null 2>&1; then break; fi
  sleep 10
done

# actualización semanal del motor
cat > /etc/cron.d/motor3d <<EOF
30 6 * * 0 root cd $DIR && git pull --ff-only -q && cd motor3d && docker compose build -q && docker compose up -d >> /var/log/motor3d-actualizacion.log 2>&1
EOF

if curl -fsS "https://$DOM/api/salud"; then
  echo "LISTO: motorUrl = https://$DOM/api" | tee /opt/motor3d-estado.txt
else
  echo "Levantado, pero https://$DOM no responde desde afuera: revisá que la red de la máquina deje entrar los puertos 80 y 443 (Ingress Rules)." | tee /opt/motor3d-estado.txt
fi
