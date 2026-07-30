#!/usr/bin/env bash
# Установка «Периметра» на чистую машину.
#
#   ./tools/install.sh [--model gigachat|qwen]
#
# Что делает: ставит инструменты сборки, собирает движок llama.cpp из
# вендореных исходников, скачивает веса модели и прописывает их в
# config/perimeter.yaml.
#
# Сеть нужна ДВАЖДЫ и только при установке: пакеты сборки и веса модели.
# После установки продукт работает без интернета (правило №0). Для
# полностью закрытого контура: выполните установку на машине с доступом,
# затем перенесите каталог целиком — веса лежат в models/.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="gigachat"
[ "${1:-}" = "--model" ] && MODEL="${2:-gigachat}"

case "$MODEL" in
  gigachat)
    MODEL_URL="https://huggingface.co/ai-sage/GigaChat3.1-10B-A1.8B-GGUF/resolve/main/GigaChat3.1-10B-A1.8B-q4_K_M.gguf"
    MODEL_FILE="gigachat3.1-10b-a1.8b-q4.gguf"
    MODEL_ID="gigachat"
    NEED_RAM_GB=16 ;;
  qwen)
    MODEL_URL="https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF/resolve/main/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf"
    MODEL_FILE="qwen3-30b-a3b-instruct-q4.gguf"
    MODEL_ID="qwen3-instruct"
    NEED_RAM_GB=32 ;;
  *) echo "Неизвестная модель: $MODEL (доступны: gigachat, qwen)"; exit 1 ;;
esac

echo "=== Периметр: установка (модель: $MODEL) ==="

RAM_GB=$(free -g | awk 'NR==2{print $2}')
if [ "$RAM_GB" -lt "$((NEED_RAM_GB - 2))" ]; then
  echo "ВНИМАНИЕ: на машине ${RAM_GB} ГБ RAM, для модели $MODEL нужно ~${NEED_RAM_GB} ГБ."
  echo "Установка продолжится, но модель может не запуститься."
fi

echo "--- 1/4: инструменты сборки"
if command -v apt-get > /dev/null; then
  apt-get update -qq && apt-get install -y -qq build-essential cmake curl python3-venv > /dev/null
fi

echo "--- 2/4: сборка движка llama.cpp (из vendor/, без загрузки кода)"
LLAMA="$HERE/vendor/llama.cpp"
[ -d "$LLAMA" ] || { echo "ОШИБКА: нет vendor/llama.cpp"; exit 1; }
cmake -S "$LLAMA" -B "$LLAMA/build" \
      -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF > /dev/null
cmake --build "$LLAMA/build" --target llama-server -j"$(nproc)" > /dev/null
echo "    собран: $LLAMA/build/bin/llama-server"

echo "--- 3/4: веса модели (единственная загрузка из интернета)"
mkdir -p "$HERE/models"
MODEL_PATH="$HERE/models/$MODEL_FILE"
if [ -f "$MODEL_PATH" ]; then
  echo "    уже скачано: $MODEL_PATH"
else
  curl -L --progress-bar -o "$MODEL_PATH.part" "$MODEL_URL"
  mv "$MODEL_PATH.part" "$MODEL_PATH"
fi
echo "    модель: $(du -h "$MODEL_PATH" | cut -f1)"

echo "--- 4/4: конфигурация"
python3 - "$HERE" "$MODEL_PATH" "$MODEL_ID" <<'PY'
import re, sys
root, model_path, model_id = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = f"{root}/config/perimeter.yaml"
s = open(cfg, encoding="utf-8").read()
s = re.sub(r'(?m)^(\s*backend:).*$', r'\1 "llamacpp"', s)
s = re.sub(r'(?m)^(\s*model_path:).*$', f'\\1 "{model_path}"', s)
s = re.sub(r'(?m)^(\s*model_id:).*$', f'\\1 "{model_id}"', s)
open(cfg, "w", encoding="utf-8").write(s)
print(f"    прописано в config/perimeter.yaml: {model_id}")
PY

python3 -m venv "$HERE/.venv" 2>/dev/null || true
echo
echo "=== Готово. Запуск: python3 run_perimeter.py ==="
echo "Дальше настройте доступ к 1С в config/perimeter.yaml (см. README)."
