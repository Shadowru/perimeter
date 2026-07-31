"""Замер: правильно ли модель выбирает инструмент и параметры.

Зачем отдельно от e2e. Живые прогоны показали, что качество продукта
распадается на две независимые части: точность расчёта (её обеспечивает
код и она не зависит от модели) и понимание вопроса — какой отчёт нужен и
за какой период. Вторая целиком на модели, и решать «взять модель поумнее»
надо по числу, а не по ощущению.

Что считаем:
- выбран ли нужный инструмент (главная метрика);
- верны ли ключевые параметры (период, размерность, контрагент);
- сколько ходов и секунд ушло на ответ.

Запуск (модель должна быть поднята):
    .venv/bin/python tests/bench/tool_choice.py http://127.0.0.1:8090 gigachat

Набор намеренно написан на языке бухгалтера, а не на языке схемы: это и
есть проверяемое умение.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for pkg in ("core", "bridge-1c", "inference", "tests"):
    sys.path.insert(0, str(ROOT / pkg))

from fakes.fake_1c_server import Fake1CServer            # noqa: E402
from perimeter_bridge1c.analytics import AnalyticsTools  # noqa: E402
from perimeter_bridge1c.mapping import load_mapping      # noqa: E402
from perimeter_bridge1c.odata import ODataClient         # noqa: E402
from perimeter_bridge1c.tools import Bridge1CTools       # noqa: E402
from perimeter_core.agent import Agent                   # noqa: E402
from perimeter_core.audit import AuditLog                # noqa: E402
from perimeter_inference.client import InferenceClient   # noqa: E402

TODAY = "2026-07-30"

# (вопрос, ожидаемый инструмент, обязательные фрагменты аргументов)
# Фрагменты проверяются подстрокой по JSON аргументов: так тест не зависит
# от того, укажет модель время суток или только дату.
CASES: list[tuple[str, str, list[str]]] = [
    # --- дебиторка и кредиторка -------------------------------------------
    ("Кто нам должен и сколько?", "receivables_aging", []),
    ("Покажи дебиторку по срокам", "receivables_aging", []),
    ("Какие клиенты тянут с оплатой дольше всех?", "receivables_aging", []),
    ("Сколько мы должны поставщикам?", "payables_aging", []),
    ("Покажи нашу кредиторскую задолженность", "payables_aging", []),
    # --- деньги и прибыль --------------------------------------------------
    ("Что с деньгами на расчётном счёте?", "cash_flow", []),
    ("Покажи движение денежных средств за июль", "cash_flow", ["2026-07"]),
    ("Сколько денег пришло и ушло в июне?", "cash_flow", ["2026-06"]),
    ("Покажи прибыли и убытки", "pnl_report", []),
    ("Какая у нас прибыль за июль?", "pnl_report", ["2026-07"]),
    ("Посчитай валовую прибыль", "pnl_report", []),
    # --- продажи -----------------------------------------------------------
    ("Как идут продажи?", "sales_dynamics", []),
    ("Какой у нас средний чек?", "sales_dynamics", []),
    ("Выручка растёт или падает?", "sales_dynamics", []),
    ("Сделай ABC-анализ по клиентам", "abc_analysis", ["counterparty"]),
    ("Кто наши крупнейшие клиенты?", "abc_analysis", ["counterparty"]),
    ("Какие товары приносят больше всего выручки?", "abc_analysis", ["nomenclature"]),
    ("ABC-анализ по номенклатуре за июль", "abc_analysis", ["nomenclature", "2026-07"]),
    ("Дай себестоимость и маржу по брендам", "profit_by_brand", []),
    ("Какая рентабельность по брендам за июль?", "profit_by_brand", ["2026-07"]),
    # --- контрагенты и документы ------------------------------------------
    ("Сделай акт сверки с Ромашкой", "reconciliation_act", ["Ромашка"]),
    ("Сверься с ООО «Василёк» за июль", "reconciliation_act", ["Василёк", "2026-07"]),
    ("Найди контрагента Ромашка", "get_counterparty", ["Ромашка"]),
    ("Какие у нас есть контрагенты?", "list_counterparties", []),
    ("Найди все непроведённые реализации за июль", "find_document",
     ["sale", "2026-07"]),
    ("Покажи реализации по Ромашке", "find_document", ["sale"]),
    ("Какие поступления товаров были в июле?", "find_document",
     ["purchase", "2026-07"]),
    ("Что мы отгрузили Ромашке и не получили оплату?", "ledger_report", ["Ромашка"]),
    # --- запись (должно требовать подтверждения) ---------------------------
    ("Подготовь черновик счёта Ромашке на 50 000", "create_draft_document",
     ["customer_invoice", "Ромашка"]),
    # --- перефразировки: одно и то же спрашивают по-разному ---------------
    ("Сколько нам должны клиенты?", "receivables_aging", []),
    ("Есть ли у нас зависшая дебиторка?", "receivables_aging", []),
    ("Кто из покупателей не расплатился?", "receivables_aging", []),
    ("Какая у нас задолженность перед поставщиками?", "payables_aging", []),
    ("Кому мы не заплатили?", "payables_aging", []),
    ("Сколько на расчётном счёте движений было?", "cash_flow", []),
    ("Приход и расход денег", "cash_flow", []),
    ("Сколько заработали?", "pnl_report", []),
    ("Покажи валовую прибыль по месяцам", "pnl_report", []),
    ("Рентабельность продаж", "pnl_report", []),
    ("Сколько мы продали в июне?", "sales_dynamics", ["2026-06"]),
    ("Динамика выручки помесячно", "sales_dynamics", []),
    ("Средний чек за июль", "sales_dynamics", ["2026-07"]),
    ("Раздели клиентов на группы A, B и C", "abc_analysis", ["counterparty"]),
    ("На ком мы больше всего зарабатываем?", "abc_analysis", ["counterparty"]),
    ("Топ товаров по продажам", "abc_analysis", ["nomenclature"]),
    ("Какие бренды прибыльнее?", "profit_by_brand", []),
    ("Себестоимость в разрезе производителей", "profit_by_brand", []),
    ("Сверка взаиморасчётов с ООО «Ромашка»", "reconciliation_act", ["Ромашка"]),
    ("Покажи обороты по Василёк за июнь", "reconciliation_act",
     ["Василёк", "2026-06"]),
    ("Найди ИНН Ромашки", "get_counterparty", ["Ромашка"]),
    ("Покажи справочник контрагентов", "list_counterparties", []),
    ("Счета покупателям за июль", "find_document",
     ["customer_invoice", "2026-07"]),
    ("Отгрузки в мае", "find_document", ["sale", "2026-05"]),
    ("Какие реализации не проведены?", "find_document", ["sale"]),
    ("Что Ромашка нам не оплатила?", "ledger_report", ["Ромашка"]),
    ("Выстави Ромашке счёт на 100 000", "create_draft_document",
     ["customer_invoice", "Ромашка"]),

    # --- вопросы с периодом: на них терялись даты -------------------------
    ("Непроведённые отгрузки за июль", "find_document", ["sale", "2026-07"]),
    ("Прибыль за июнь", "pnl_report", ["2026-06"]),
    ("ABC по клиентам за июль", "abc_analysis", ["counterparty", "2026-07"]),
    ("Движение денег за июнь", "cash_flow", ["2026-06"]),

    # --- себестоимость проданного ----------------------------------------
    ("Посчитай себестоимость проданного", "cost_report", []),
    ("Какая себестоимость по товарам за июль?", "cost_report", ["2026-07"]),
    ("Сколько мы заработали на каждом товаре?", "cost_report", []),

    # --- проверка на «не выдумывай»: данных нет -------------------------
    ("Сделай акт сверки с ООО «Ромашка-Плюс»", "reconciliation_act",
     ["Ромашка-Плюс"]),
]


def run(llm_url: str, model: str, repeat: int = 1,
        temperature: float | None = None) -> dict:
    """repeat > 1 — каждый вопрос задаётся несколько раз.

    Для финансовой отчётности воспроизводимость не придирка, а свойство:
    один и тот же вопрос обязан давать один и тот же разбор. Нестабильность
    меряем, а не предполагаем.
    """
    results = []
    with Fake1CServer() as srv:
        mapping = load_mapping("bp30")
        client = ODataClient(srv.base_url, "robot", "test", mapping=mapping)
        specs = (Bridge1CTools(client, mapping).specs()
                 + AnalyticsTools(client, mapping).specs())
        for question, want_tool, want_args in CASES:
          seen_tools = set()
          first: dict | None = None
          for attempt in range(repeat):
            kw = {} if temperature is None else {"temperature": temperature}
            agent = Agent(
                client=InferenceClient(llm_url, model=model, timeout_s=600),
                tool_specs=specs, audit=AuditLog(Path("/tmp/bench_audit.log")),
                confirm=lambda n, a: True, today=TODAY, **kw)
            t0 = time.monotonic()
            try:
                res = agent.run(question)
                error = ""
            except Exception as e:  # noqa: BLE001 — сбой модели тоже результат
                res, error = None, str(e)[:80]
            elapsed = time.monotonic() - t0

            called = [(m.get("name"), m.get("content")) for m in agent.messages
                      if m.get("role") == "tool"]
            names = [n for n, _ in called]
            args_json = "".join(
                c.get("function", {}).get("arguments", "")
                for m in agent.messages if m.get("role") == "assistant"
                for c in (m.get("tool_calls") or []))
            tool_ok = want_tool in names
            args_ok = tool_ok and all(frag.lower() in args_json.lower()
                                      for frag in want_args)
            seen_tools.add(tuple(names))
            row = {
                "question": question, "want": want_tool, "got": names,
                "args": args_json[:200], "want_args": want_args,
                "tool_ok": tool_ok, "args_ok": args_ok, "error": error,
                "seconds": round(elapsed, 1),
                "steps": res.steps if res else 0,
                "grounded": bool(res and res.grounded),
            }
            # В статистику идёт первый прогон: иначе нестабильные вопросы
            # учитывались бы дважды и портили сами проценты.
            if first is None:
                first = row
          first["unstable"] = len(seen_tools) > 1
          results.append(first)
    return summarize(results)


def summarize(results: list[dict]) -> dict:
    n = len(results)
    tool_ok = sum(r["tool_ok"] for r in results)
    args_ok = sum(r["args_ok"] for r in results)
    times = [r["seconds"] for r in results if not r["error"]]
    return {
        "cases": n,
        "tool_accuracy": round(tool_ok / n * 100, 1),
        "args_accuracy": round(args_ok / n * 100, 1),
        "errors": sum(bool(r["error"]) for r in results),
        "ungrounded": sum(not r["grounded"] for r in results),
        "median_seconds": round(sorted(times)[len(times) // 2], 1) if times else None,
        "max_seconds": round(max(times), 1) if times else None,
        "failures": [r for r in results if not r["args_ok"]],
        "unverified": [r["question"] for r in results if not r["grounded"]],
        "unstable": sorted({r["question"] for r in results if r.get("unstable")}),
    }


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
    model = sys.argv[2] if len(sys.argv) > 2 else "gigachat"
    repeat = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    temp = float(sys.argv[4]) if len(sys.argv) > 4 else None
    s = run(url, model, repeat=repeat, temperature=temp)
    print(f"\nМодель: {model}  ({s['cases']} вопросов)")
    print(f"  выбор инструмента: {s['tool_accuracy']}%")
    print(f"  инструмент + параметры: {s['args_accuracy']}%")
    print(f"  ошибок бэкенда: {s['errors']}, ответов без подтверждения: {s['ungrounded']}")
    print(f"  время: медиана {s['median_seconds']} c, максимум {s['max_seconds']} c")
    if s.get("unstable"):
        print(f"\nРазный разбор при повторе ({len(s['unstable'])}):")
        for q in s["unstable"]:
            print(f"  {q}")
    if s["unverified"]:
        print("\nОтветы без подтверждения данными:")
        for q in s["unverified"]:
            print(f"  {q}")
    if s["failures"]:
        print("\nНе прошли:")
        for f in s["failures"]:
            got = ", ".join(f["got"]) or "—"
            mark = "инструмент" if not f["tool_ok"] else "параметры"
            print(f"  [{mark}] {f['question']}")
            print(f"      ждали {f['want']} c {f['want_args'] or '—'}; вызвано: {got}")
            print(f"      аргументы: {f['args'] or '—'}"
                  + (f"  ОШИБКА: {f['error']}" if f["error"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
