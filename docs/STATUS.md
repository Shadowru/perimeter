# STATUS

Обновлено: 2026-07-28

## Этап 1 — Ресерч и юридика: ✅ завершён

- Лицензии: openworker — MIT; colibri — Apache-2.0; веса GLM-5.2 — MIT (подтверждено по HF API для `zai-org/GLM-5.2` и `zai-org/GLM-5.2-FP8`). **Блокеров нет.**
- Отчёт: `docs/research.md` (архитектуры, точки расширения, интеграция с 1С).
- Решения: core — компактная stdlib-адаптация архитектуры openworker (не полный форк; обоснование в research.md §2); канал 1С для MVP — стандартный OData; веса — самостоятельная конвертация из официального `zai-org/GLM-5.2-FP8` (чистая цепочка происхождения).
- Ключевой риск продукта: prefill/TTFT colibri на дисковом стриминге → требование компактных промптов заложено в дизайн core.

## Этап 2 — Скелет репозитория: ✅ завершён

- Структура каталогов по спецификации; pyproject (Python 3.12, runtime-зависимости = ∅, stdlib-first — обоснование в docs/deps.md).
- Завендорен PyYAML 6.0.3 (pure-python, аудит на сетевые вызовы пройден).
- i18n с первого дня: `perimeter_core/i18n.py`, локали `ru.json` (базовая) / `en.json`; тест на синхронность ключей.
- `perimeter_core/config.py`: загрузка perimeter.yaml, валидация (inference/ui — только loopback; хост 1С обязан быть в allowed_hosts), пароль только env/keyring — пароль в конфиге отвергается с ошибкой.
- `perimeter_core/netguard.py`: рантайм-защита правила №0 — обёртка socket.connect, блокирует всё кроме loopback и allowed_hosts.
- CI воздушного зазора: `tools/ci/airgap_scan.py` (статический скан URL/внешних IP c allowlist-файлом) + `tools/ci/netns_test.sh` (прогон тестов в netns без интернета) + `.github/workflows/ci.yml`.
- Тесты: 20 unit-тестов зелёные; статический скан чист. Замечание: на dev-машине user namespaces отключены — netns-тест локально пропускается (в CI выполняется).

## Этапы 3–6: ⏳ не начаты

## Замеры

_(появятся после Этапа 3)_
