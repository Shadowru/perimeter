#!/usr/bin/env bash
# Правило №0, динамическая проверка: весь тестовый прогон выполняется в
# сетевом неймспейсе без интернета (только loopback). Любая попытка
# продукта выйти наружу заканчивается ошибкой сети — тесты обязаны пройти.
set -euo pipefail

cd "$(dirname "$0")/../.."

if ! unshare -r -n true 2>/dev/null; then
    echo "netns_test: unshare недоступен (нужны user namespaces); пропуск." >&2
    exit 0
fi

exec unshare -r -n bash -c '
    ip link set lo up 2>/dev/null || python3 - <<PY
import fcntl, socket, struct
# fallback: поднять lo через ioctl, если нет iproute2
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ifreq = struct.pack("16sH", b"lo", 0x1 | 0x40)  # IFF_UP | IFF_RUNNING
fcntl.ioctl(s, 0x8914, ifreq)  # SIOCSIFFLAGS
PY
    echo "netns_test: сеть изолирована (только loopback), запускаю тесты"
    python3 -m pytest tests
'
