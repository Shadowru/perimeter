#!/usr/bin/env bash
# Сборка внешней обработки perimeter_robot.epf из исходников — пакетно,
# без графического Конфигуратора (платформа 8.3.13+).
#
#   ./build_epf.sh <путь-к-1cv8> <путь-к-любой-базе> [выходной-файл]
#
# База нужна только как контекст запуска Конфигуратора; её данные не меняются.
# Пример:
#   ./build_epf.sh /opt/1cv8/x86_64/8.3.27.1508/1cv8 /srv/1c/test
#
# После сборки полезно прогнать синтаксический контроль:
#   1cv8 DESIGNER /F <база> /CheckModules -ExternalDataProcessorOrReport <epf> \
#        -ThinClient -Server /Out check.log
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
V8="${1:?укажите путь к исполняемому файлу 1cv8}"
IB="${2:?укажите путь к информационной базе}"
OUT="${3:-$HERE/perimeter_robot.epf}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/Ext"
cp "$HERE/src/ПериметрРобот.xml" "$WORK/"
cp "$HERE/robot_module.bsl" "$WORK/Ext/ObjectModule.bsl"

# На свежих дистрибутивах Linux платформа падает из-за собственной libgcc:
# подставляем системную (проверено на Ubuntu 26.04).
if [ -f /lib/x86_64-linux-gnu/libgcc_s.so.1 ]; then
    export LD_PRELOAD=/lib/x86_64-linux-gnu/libgcc_s.so.1
fi

RUN="$V8"
command -v xvfb-run >/dev/null 2>&1 && RUN="xvfb-run -a $V8"

$RUN DESIGNER /F"$IB" \
    /LoadExternalDataProcessorOrReportFromFiles "$WORK/ПериметрРобот.xml" "$OUT" \
    /DisableStartupMessages /Out "$WORK/build.log"

cat "$WORK/build.log"
echo "Готово: $OUT"
