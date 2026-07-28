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

## Этап 3 — inference/: ✅ завершён (замеры на полной модели — заблокированы железом)

- colibri завендорен (`vendor/colibri`, commit 1b8b62e, v1.1.1; сетевые setup-скрипты исключены — см. VENDORED.txt); собран `make portable`.
- Верификация движка: C-юниты — все зелёные; teacher-forcing на крошечной GLM-фикстуре — **32/32** совпадений с эталоном transformers; 90 upstream-тестов OpenAI-сервера — OK.
- `perimeter_inference`: супервизор сервера (colibri, fallback llama.cpp — оба через один OpenAI-интерфейс) + stdlib-клиент `/v1/chat/completions` со стримингом (SSE).
- Сквозной smoke: наш супервизор → вендореный сервер → движок → стрим → клиент; оформлен интеграционным тестом (`tests/integration/test_colibri_smoke.py`), фикстура glm_tiny закоммичена — CI не требует ни весов, ни torch.
- Статический скан воздушного зазора прошёл по вендореному коду; 4 осознанных исключения задокументированы в allowlist.
- **Заблокировано**: честные замеры GLM-5.2 (tok/s, TTFT, RSS) — dev-машина (8 ГБ RAM, 45 ГБ диска) не вмещает модель (372 ГБ). Готов скрипт `tools/bench_inference.py`; upstream-цифры с пометкой происхождения — в `docs/hardware.md`. Прогнать на целевом железе перед маркетингом.

## Этап 4 — bridge-1c/: ✅ завершён

- `ODataClient` (stdlib): Basic-auth, JSON, пагинация $top/$skip, ретраи с backoff на 5xx/сетевых ошибках, percent-кодирование кириллических имён сущностей, `validate_mapping()` против `$metadata` базы.
- Имена метаданных 1С — только в `mappings/{bp30,ut11,zup31}.yaml`; неподтверждённые поля помечены `TODO(verify)` (правило «не выдумывать про 1С»), клиент сверяет их с $metadata при подключении.
- Мок-сервер 1С (`tests/fakes/fake_1c_server.py`): формат ответов OData 3.0 1С, Basic-auth, $metadata, подмножество $filter (eq/ge/le, guid/datetime/строки/bool, and, substringof), $top/$skip/$select/$orderby, POST, режим «нестабильной 1С» для тестов ретраев. Датасет — вымышленная БП 3.0 под демо-сценарии.
- Инструменты агента: `get_counterparty`, `find_document`, `ledger_report`, `create_draft_document` — компактный однострочный вывод (бюджет prefill), ссылки «№ … от …». Guardrail: у клиента нет метода Post; create_draft принудительно Posted=false и помечен requires_approval.
- Ограничение MVP: `ledger_report` считает по проведённым документам, не по регистру взаиморасчётов (TODO в коде, требуется верификация имён регистров на живой базе).
- 19 тестов зелёные.

## Этап 5 — core/: ✅ завершён

- `perimeter_core/agent.py` — компактная адаптация цикла openworker (перенос паттернов задокументирован в research.md §2): канонич. OpenAI-история, единый шов `_outbound_messages`, батч tool-вызовов, лимит итераций.
- Под локальную модель: детерминированная компактизация истории (старые tool-результаты → первая строка, бюджет символов с отбрасыванием старых ходов целиком; суммаризация моделью не используется — на 1 tok/s она дороже задачи).
- Salvage tool-вызовов, пришедших текстом (маркеры GLM) — для llama.cpp-fallback.
- Системный промпт на русском — ресурсный файл `prompts/system.ru.md` (~200 токенов: бюджет prefill), деловой тон, обязательные ссылки «№ … от …».
- Guardrails: requires_approval-инструменты исполняются только после confirm-callback (человек); отказ уходит модели как результат; у OData-клиента физически нет метода проведения.
- `perimeter_core/audit.py` — append-only JSONL-журнал (O_APPEND+fsync, без API перезаписи): user_message / tool_call / confirm_granted / confirm_denied / assistant_message.
- 9 тестов зелёные (цикл, подтверждения, компактизация, salvage, аудит).

## Этап 6: ⏳ не начат

## Замеры

См. `docs/hardware.md`: upstream-цифры colibri (1–7 tok/s в зависимости от железа, главный риск — TTFT/prefill); строка «Замеры Периметра» ждёт целевого железа.
