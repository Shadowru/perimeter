#!/bin/bash
# Замер одной модели: поднять локальный сервер, прогнать набор вопросов,
# погасить сервер. Пиковая память сервера пишется в отчёт — она определяет
# требования к железу (см. docs/hardware.md).
#
#   tools/bench_model.sh models/gigachat3.1-10b-a1.8b-q4.gguf gigachat
#   tools/bench_model.sh models/qwen3-14b-q4.gguf qwen14b --reasoning off
#
# Третий и далее аргументы уходят в llama-server как есть. Для гибридных
# моделей (Qwen3 и производные) обязательно `--reasoning off`: на 5-15 ток/с
# размышления перед вызовом инструмента стоят минут.
#
# Порт 8091 выбран, чтобы не конфликтовать с рабочим сервером на 8090.
set -u

if [ $# -lt 2 ]; then
    echo "Использование: $0 <путь к .gguf> <имя> [доп. аргументы llama-server]" >&2
    exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_PATH="$1"; NAME="$2"; shift 2
EXTRA="$*"
OUT="${BENCH_OUT:-$ROOT/bench_${NAME}.txt}"
MEM="$(mktemp)"
SRV="$ROOT/vendor/llama.cpp/build/bin/llama-server"
PORT="${BENCH_PORT:-8091}"

[ -x "$SRV" ] || { echo "Нет собранного llama-server: $SRV" >&2; exit 1; }
[ -f "$MODEL_PATH" ] || { echo "Нет файла модели: $MODEL_PATH" >&2; exit 1; }

"$SRV" --model "$MODEL_PATH" --host 127.0.0.1 --port "$PORT" --alias bench \
       --ctx-size 8192 --threads "$(nproc)" --jinja $EXTRA \
       > "$ROOT/srv_${NAME}.log" 2>&1 &
SRV_PID=$!
trap 'kill $SRV_PID 2>/dev/null; rm -f "$MEM"' EXIT

for _ in $(seq 1 120); do
    curl -sf -m 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    kill -0 $SRV_PID 2>/dev/null || { echo "Сервер упал при загрузке:" | tee "$OUT"
                                      tail -5 "$ROOT/srv_${NAME}.log" | tee -a "$OUT"
                                      exit 1; }
    sleep 5
done
curl -sf -m 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || {
    echo "Сервер не поднялся за 10 минут" | tee "$OUT"; exit 1; }

# Пик памяти читаем из /proc: getrusage на живом сервере всегда даёт 0.
( while kill -0 $SRV_PID 2>/dev/null; do
      grep VmHWM "/proc/$SRV_PID/status" 2>/dev/null >> "$MEM"
      sleep 10
  done ) &

"$ROOT/.venv/bin/python" "$ROOT/tests/bench/tool_choice.py" \
    "http://127.0.0.1:$PORT" bench "${BENCH_REPEAT:-1}" > "$OUT" 2>&1
{
    echo
    echo "пиковая память сервера: $(sort -k2 -n "$MEM" 2>/dev/null | tail -1)"
} >> "$OUT"
cat "$OUT"
