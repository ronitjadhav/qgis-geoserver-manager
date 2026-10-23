#!/bin/bash
# The live suite against GeoServer Cloud, started from its own compose files.
#
#   .github/cloud/run_live.sh VERSION CATALOG     # e.g. 3.0.2 datadir
#
# CATALOG is datadir (the demo data) or pgconfig (an empty catalog in
# PostgreSQL, the demo's sf files copied in). The suite runs in a QGIS
# container on the stack's network, so it reaches GeoServer as the gateway.
# Optional: GATEWAY_PORT (9090), QGIS_IMAGE (qgis/qgis:stable), KEEP=1 to
# leave the stack running.
set -euo pipefail

version=$1 catalog=$2
plugin=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
export COMPOSE_PROJECT_NAME="gsm-live-cloud"
export GS_USER="$(id -u):$(id -g)"
export GATEWAY_PORT=${GATEWAY_PORT:-9090}
export CATALOG_DATADIR_PATH="$work/catalog"
network="${COMPOSE_PROJECT_NAME}_default"

files=(-f compose.yml -f "catalog-$catalog.yml" -f standalone.yml -f gsm.yml)

stop() {
  [[ ${KEEP:-} == 1 ]] && return
  docker rm -f gsm-live-postgis >/dev/null 2>&1 || true
  docker compose "${files[@]}" down -v >/dev/null 2>&1 || true
  docker run --rm -v "$work:/w" alpine:3.24.0 rm -rf /w/catalog >/dev/null 2>&1 || true
  rm -rf "$work"
}
# Before the clone: a bad version must not leave the work directory behind.
trap stop EXIT

git clone -q --depth 1 --branch "v$version" --filter=blob:none --sparse \
  https://github.com/geoserver/geoserver-cloud.git "$work/cloud"
git -C "$work/cloud" sparse-checkout set compose
cd "$work/cloud/compose"
mkdir -p "$CATALOG_DATADIR_PATH"

# What the suite does not use stays off.
cat > gsm.yml <<'EOF'
services:
  webui: {deploy: {replicas: 0}}
  wps: {deploy: {replicas: 0}}
  wcs: {deploy: {replicas: 0}}
EOF
if [[ $version == 2.* ]]; then
  # Cloud 2.28.6's services cannot declare their event queues on the
  # RabbitMQ 4 its compose ships, and never learn a catalog change.
  printf '  rabbitmq: {image: "rabbitmq:3.13-alpine"}\n' >> gsm.yml
fi
docker compose "${files[@]}" up -d --quiet-pull
for _ in $(seq 1 150); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -u admin:geoserver \
    "http://localhost:$GATEWAY_PORT/geoserver/cloud/rest/about/version.json" || true)
  [[ $code == 200 ]] && break
  sleep 2
done
[[ $code == 200 ]] || { docker compose "${files[@]}" ps; exit 1; }

# The datastore steps connect GeoServer to it as host "postgis".
docker run -d --name gsm-live-postgis --network "$network" --network-alias postgis \
  -e POSTGRES_DB=geoserver -e POSTGRES_USER=geoserver -e POSTGRES_PASSWORD=geoserver \
  postgis/postgis:16-3.4 >/dev/null
until docker exec gsm-live-postgis pg_isready -U geoserver -d geoserver >/dev/null 2>&1; do
  sleep 1
done

shapes=file:data/sf dem=file:data/sf/sfdem.tif
if [[ $catalog == pgconfig ]]; then
  # The services share /opt/geoserver_data; the demo's sf files go there.
  docker run --rm -v "${COMPOSE_PROJECT_NAME}_geoserver_data:/d" -v "$PWD:/c:ro" \
    alpine:3.24.0 sh -c "cd /tmp && tar xzf /c/catalog-datadir.tgz data/sf 2>/dev/null; \
    cp -r data/sf /d/sf && chown -R $GS_USER /d/sf"
  shapes=file:///opt/geoserver_data/sf dem=file:///opt/geoserver_data/sf/sfdem.tif
fi

# As root on the checkout: no __pycache__ left behind, owned by root.
# Offscreen, so no X server: xvfb-run waited for one forever in this image.
docker run --rm --network "$network" -v "$plugin:/plugin" -w /plugin \
  -e PYTHONDONTWRITEBYTECODE=1 -e QT_QPA_PLATFORM=offscreen \
  -e PYTHONPATH="/usr/share/qgis/python/plugins:/usr/share/qgis/python:." \
  -e GSM_LIVE_URL=http://gateway:8080/geoserver/cloud \
  -e GSM_LIVE_SHAPES="$shapes" -e GSM_LIVE_DEM="$dem" \
  "${QGIS_IMAGE:-qgis/qgis:stable}" bash -c '
    set -e
    apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
    python3 -m venv --system-site-packages /tmp/venv
    /tmp/venv/bin/pip install -q -r requirements/testing.txt
    /tmp/venv/bin/python -m pytest -v -p no:cacheprovider \
      tests/qgis/test_live_geoserver.py'
