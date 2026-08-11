#!/usr/bin/env bash
# Hermes — tek giriş noktası.
#
#   ./start.sh                 orchestrator + web arayüzü (varsayılan)
#   ./start.sh orchestrator    aynısı, açıkça
#   ./start.sh worker          bu makinede bir worker (yerel test için)
#   ./start.sh test            testleri çalıştır
#
# Ortam değişkenleri:
#   ORCH_PORT=8140             orchestrator portu
#   SPOT_PORT=8100             worker portu
#   SPOT_API_KEY=...           worker kimlik anahtarı (worker modunda zorunlu)
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
if [ ! -d "$VENV" ]; then
  echo "› sanal ortam kuruluyor"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi
PY="$VENV/bin/python"

case "${1:-orchestrator}" in
  orchestrator)
    echo "› orchestrator  http://localhost:${ORCH_PORT:-8140}"
    # uvicorn CLI ile değil modül olarak: arayüzdeki "yeniden başlat"
    # kendini sys.argv[0] üzerinden exec ediyor, uvicorn ile o yol kaybolur.
    exec "$PY" -m orchestrator.coordinator
    ;;
  worker)
    : "${SPOT_API_KEY:?worker modunda SPOT_API_KEY gerekli}"
    echo "› worker  http://localhost:${SPOT_PORT:-8100}"
    exec "$PY" -m workers.server
    ;;
  test)
    exec "$PY" -m pytest tests/ "${@:2}"
    ;;
  *)
    echo "bilinmeyen mod: $1" >&2
    sed -n '2,14p' "$0" >&2
    exit 2
    ;;
esac
