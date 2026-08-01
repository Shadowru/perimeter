"""Прямой опрос живой 1С через робота — без модели.

Зачем. Когда отчёт приходит пустым или странным, надо отличить «модель не
поняла вопрос» от «в базе этого нет» и от «наш маппинг мимо». Через агента
это не различить: он всё равно что-нибудь ответит. Скрипт поднимает шлюз,
дожидается робота и задаёт базе ровно тот запрос, который построил бы
инструмент.

Именно так 2026-07-31 нашлись три дефекта, которых не видели ни тесты, ни
демо: робот не возвращал табличные части, суффикс `_Key` навешивался по
значению, а вся выручка базы лежала в части «Услуги», а не «Товары».

    # на машине с «Периметром», продукт при этом остановлен:
    systemctl stop perimeter-live
    .venv/bin/python tools/probe_live_1c.py --token <токен из perimeter.yaml>
    systemctl start perimeter-live

Робот должен быть запущен в сеансе 1С (обработка открыта) — иначе скрипт
честно скажет, что не дождался.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for pkg in ("core", "bridge-1c", "inference"):
    sys.path.insert(0, str(REPO / pkg))

from perimeter_bridge1c.backend import KIND_BOOL, OP_EQ, Cond, Query  # noqa: E402
from perimeter_bridge1c.mapping import load_mapping                  # noqa: E402
from perimeter_bridge1c.robot import RobotGateway                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="", help="токен робота из perimeter.yaml")
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--configuration", default="bp30")
    ap.add_argument("--timeout", type=float, default=240)
    args = ap.parse_args()

    mapping = load_mapping(args.configuration)
    gw = RobotGateway(host="127.0.0.1", port=args.port, token=args.token)
    gw.start()
    print(f"шлюз на {gw.base_url}, жду робота…", flush=True)

    def ask(payload: dict) -> dict:
        return gw.submit(payload, args.timeout)

    try:
        meta = ask({"op": "metadata"}).get("entities") or {}
        print(f"объектов метаданных в базе: {len(meta)}", flush=True)

        for logical in ("counterparty", "sale", "incoming_payment"):
            try:
                ent = mapping.entity(logical)
            except Exception as e:                       # noqa: BLE001
                print(f"\n=== {logical}: нет в маппинге ({e})", flush=True)
                continue

            conds = ([Cond("Posted", OP_EQ, True, KIND_BOOL)]
                     if logical != "counterparty" else [])
            q = Query(entity_set=ent.entity_set, conditions=conds,
                      with_rows=ent.row_sections or None)
            rows = ask({"op": "query", **q.as_dict()}).get("rows") or []
            print(f"\n=== {logical} ({ent.entity_set}): строк {len(rows)}", flush=True)
            if not rows:
                continue

            print("  колонки:", ", ".join(list(rows[0])[:14]), flush=True)
            # Проверяем именно то, что записано в маппинге: есть ли эти поля.
            for name in ent.fields.values():
                have = sum(1 for r in rows if name in r)
                mark = "ЕСТЬ" if have == len(rows) else ("ЧАСТИЧНО" if have else "НЕТ")
                print(f"    {name}: {mark} ({have}/{len(rows)})", flush=True)
            for section in ent.row_sections:
                filled = sum(1 for r in rows if r.get(section))
                print(f"    часть «{section}»: строки у {filled}/{len(rows)} документов",
                      flush=True)
                sample = next((r[section][0] for r in rows if r.get(section)), None)
                if sample:
                    print("      колонки строки:", ", ".join(list(sample)[:12]), flush=True)
                    for logical_row, name in ent.row_fields.items():
                        print(f"      {logical_row} -> {name}:",
                              "ЕСТЬ" if name in sample else "НЕТ", flush=True)
            print("  пример:", json.dumps({k: rows[0][k] for k in list(rows[0])[:6]},
                                          ensure_ascii=False)[:240], flush=True)
    except Exception as e:                                # noqa: BLE001
        print(f"\nне дождались робота или ошибка выполнения: {e}", flush=True)
        return 1
    finally:
        gw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
