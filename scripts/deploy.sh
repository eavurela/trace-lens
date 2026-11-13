#!/usr/bin/env bash
# Construye y (re)crea el contenedor trace-lens en monobuntu. Correr ahí, desde la raíz del repo.
# La base queda en el volumen trace-lens-data: recrear el contenedor no pierde los hallazgos.
#
# Credenciales opcionales (las carga el usuario, nunca se commitean): ~/monitoring_agent/poc/trace-lens.env
#   LAZY_URL=...  LAZY_TOKEN=...   (deploys de Lazy para cruzar con el inicio de cada problema)
set -euo pipefail

PROMETHEUS_URL="${PROMETHEUS_URL:-http://10.0.12.207:9090}"
TEMPO_URL="${TEMPO_URL:-http://ms-tempo.prod.internal-utn.org}"
SECRETS="${SECRETS:-$HOME/monitoring_agent/poc/trace-lens.env}"

# Prometheus y Tempo se alcanzan por la VPN (tun0, MTU 1300): red vpn-mtu como salida por defecto
# (con MTU 1500 las respuestas grandes se cuelgan). monitoring-agent es para stack-lens.
docker network inspect vpn-mtu >/dev/null 2>&1 \
  || docker network create --opt com.docker.network.driver.mtu=1300 vpn-mtu >/dev/null

env_file=()
[ -f "$SECRETS" ] && env_file=(--env-file "$SECRETS")

docker build -t monitoring-agent/trace-lens:local .
docker rm -f trace-lens 2>/dev/null || true
docker run -d --name trace-lens \
  --network name=vpn-mtu,gw-priority=1 \
  --restart unless-stopped \
  -p 127.0.0.1:7500:7500 \
  -v trace-lens-data:/data \
  -e PROMETHEUS_URL="$PROMETHEUS_URL" \
  -e TEMPO_URL="$TEMPO_URL" \
  -e STACK_LENS_URL=http://stack-lens:7200 \
  "${env_file[@]}" \
  monitoring-agent/trace-lens:local
docker network connect monitoring-agent trace-lens
docker ps --filter name=trace-lens --format '{{.Names}} {{.Status}} {{.Ports}}'
