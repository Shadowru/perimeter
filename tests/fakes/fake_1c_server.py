"""Мок-сервер стандартного OData-интерфейса 1С (структура БП 3.0).

Воспроизводит формат ответов 1С 8.3 (OData 3.0, JSON: {"odata.metadata": …,
"value": […]}), Basic-аутентификацию, $metadata, ограниченное подмножество
$filter (eq guid/строка/bool, ge/le datetime, and, substringof), $top/$skip/
$select/$orderby и POST-создание. Тесты не требуют живой 1С.

Датасет — вымышленная база «Бухгалтерия 3.0» с контрагентами и документами
июля 2026 (под демо-сценарии Этапа 6).
"""

from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

GUID_ROMASHKA = "11111111-1111-1111-1111-111111111111"
GUID_VASILEK = "22222222-2222-2222-2222-222222222222"
GUID_TEHNO = "33333333-3333-3333-3333-333333333333"

NOM_LAPTOP = "aaaaaaaa-0000-0000-0000-000000000001"
NOM_MONITOR = "aaaaaaaa-0000-0000-0000-000000000002"
NOM_CHAIR = "aaaaaaaa-0000-0000-0000-000000000003"
NOM_SERVICE = "aaaaaaaa-0000-0000-0000-000000000004"


def default_dataset() -> dict[str, list[dict[str, Any]]]:
    def doc(n: str, date: str, cp: str, total: float, posted: bool,
            vat_rate: float = 0.20) -> dict[str, Any]:
        # СуммаДокумента в БП 3.0 — сумма С НДС; НДС выделяется из неё.
        vat = round(total - total / (1 + vat_rate), 2) if vat_rate else 0.0
        return {
            "Ref_Key": str(uuid.uuid5(uuid.NAMESPACE_URL, n + date)),
            "Number": n, "Date": date, "Posted": posted, "DeletionMark": False,
            "Контрагент_Key": cp, "СуммаДокумента": total, "Комментарий": "",
            "СуммаВключаетНДС": True, "СуммаНДС": vat,
        }

    def rows(document: dict[str, Any],
             lines: list[tuple[str, float, float]]) -> dict[str, Any]:
        """Табличная часть «Товары» — в OData она приходит вложенным массивом."""
        document["Товары"] = [
            {"LineNumber": str(i + 1), "Номенклатура_Key": nom,
             "Количество": qty, "Сумма": amount, "Цена": round(amount / qty, 2),
             "СуммаНДС": round(amount - amount / 1.20, 2),
             # Себестоимость в БП 3.0 лежит в строке документа (проверено на
             # живой базе 31.07). Берём 2/3 от суммы без НДС.
             "Себестоимость": round(amount / 1.20 * 2 / 3, 2)}
            for i, (nom, qty, amount) in enumerate(lines)
        ]
        return document

    def reg(period: str, nom: str, cp: str,
            revenue: float, cost: float) -> dict[str, Any]:
        return {
            "Period": period, "Recorder": "", "LineNumber": "1",
            "Номенклатура_Key": nom, "Контрагент_Key": cp,
            "Выручка": revenue, "Себестоимость": cost,
        }

    return {
        "Catalog_Контрагенты": [
            {"Ref_Key": GUID_ROMASHKA, "Code": "К-0001", "Description": 'ООО "Ромашка"',
             "ИНН": "7701234567", "КПП": "770101001", "DeletionMark": False},
            {"Ref_Key": GUID_VASILEK, "Code": "К-0002", "Description": 'ООО "Василёк"',
             "ИНН": "7809876543", "КПП": "780901001", "DeletionMark": False},
            {"Ref_Key": GUID_TEHNO, "Code": "К-0003", "Description": 'АО "ТехноСервис"',
             "ИНН": "5047112233", "КПП": "504701001", "DeletionMark": False},
        ],
        "Catalog_Номенклатура": [
            {"Ref_Key": NOM_LAPTOP, "Code": "Н-0001", "Description": "Ноутбук ProBook 14",
             "Производитель": "Гамма", "DeletionMark": False},
            {"Ref_Key": NOM_MONITOR, "Code": "Н-0002", "Description": "Монитор 27\"",
             "Производитель": "Гамма", "DeletionMark": False},
            {"Ref_Key": NOM_CHAIR, "Code": "Н-0003", "Description": "Кресло офисное",
             "Производитель": "Дельта", "DeletionMark": False},
            {"Ref_Key": NOM_SERVICE, "Code": "Н-0004", "Description": "Услуги консультационные",
             "Производитель": "", "DeletionMark": False},
        ],
        "Document_РеализацияТоваровУслуг": [
            rows(doc("РТ-0001", "2026-07-03T10:00:00", GUID_ROMASHKA, 120000.00, True),
                 [(NOM_LAPTOP, 2, 90000.00), (NOM_MONITOR, 2, 30000.00)]),
            rows(doc("РТ-0002", "2026-07-10T15:30:00", GUID_ROMASHKA, 45000.50, False),
                 [(NOM_CHAIR, 3, 45000.50)]),
            rows(doc("РТ-0003", "2026-07-18T09:00:00", GUID_ROMASHKA, 78000.00, False),
                 [(NOM_LAPTOP, 1, 45000.00), (NOM_SERVICE, 1, 33000.00)]),
            rows(doc("РТ-0004", "2026-07-21T12:00:00", GUID_VASILEK, 15000.00, True),
                 [(NOM_MONITOR, 1, 15000.00)]),
            rows(doc("РТ-0005", "2026-06-25T11:00:00", GUID_ROMASHKA, 99000.00, True),
                 [(NOM_LAPTOP, 2, 99000.00)]),
            rows(doc("РТ-0006", "2026-05-15T10:00:00", GUID_VASILEK, 60000.00, True),
                 [(NOM_MONITOR, 4, 60000.00)]),
            rows(doc("РТ-0007", "2026-05-28T10:00:00", GUID_ROMASHKA, 33000.00, True),
                 [(NOM_SERVICE, 1, 33000.00)]),
        ],
        # Регистр выручки и себестоимости продаж: только проведённые документы.
        # TODO(verify): имя регистра и реквизитов различается по конфигурациям.
        "AccumulationRegister_ВыручкаИСебестоимостьПродаж": [
            reg("2026-07-03T10:00:00", NOM_LAPTOP, GUID_ROMASHKA, 90000.00, 61000.00),
            reg("2026-07-03T10:00:00", NOM_MONITOR, GUID_ROMASHKA, 30000.00, 21000.00),
            reg("2026-07-21T12:00:00", NOM_MONITOR, GUID_VASILEK, 15000.00, 10500.00),
            reg("2026-06-25T11:00:00", NOM_LAPTOP, GUID_ROMASHKA, 99000.00, 67000.00),
        ],
        # Возврат от покупателя: Василёк вернул монитор из июльской отгрузки.
        "Document_ВозвратТоваровОтПокупателя": [
            rows(doc("ВЗ-0001", "2026-07-25T10:00:00", GUID_VASILEK, 6000.00, True),
                 [(NOM_MONITOR, 1, 6000.00)]),
        ],
        "Document_ПоступлениеТоваровУслуг": [
            # Товарные строки нужны для расчёта себестоимости по средней
            # цене закупки, когда 1С ещё не закрыла месяц.
            rows(doc("ПТ-0001", "2026-07-05T10:00:00", GUID_TEHNO, 300000.00, True),
                 [(NOM_LAPTOP, 3, 180000.00), (NOM_MONITOR, 4, 120000.00)]),
            # Старый неоплаченный приход — чтобы в кредиторке была корзина «90+».
            rows(doc("ПТ-0002", "2026-03-10T10:00:00", GUID_VASILEK, 40000.00, True),
                 [(NOM_MONITOR, 2, 40000.00)]),
        ],
        "Document_ПоступлениеНаРасчетныйСчет": [
            doc("ПС-0001", "2026-07-07T10:00:00", GUID_ROMASHKA, 120000.00, True),
            doc("ПС-0002", "2026-07-22T10:00:00", GUID_VASILEK, 15000.00, True),
            # Предоплата от контрагента, которому мы ещё ничего не отгружали.
            doc("ПС-0003", "2026-07-28T10:00:00", GUID_TEHNO, 20000.00, True),
        ],
        "Document_СчетНаОплатуПокупателю": [
            doc("СЧ-0101", "2026-06-20T10:00:00", GUID_ROMASHKA, 99000.00, True),
        ],
        # Списания с расчётного счёта — нужны для ДДС и кредиторки
        "Document_СписаниеСРасчетногоСчета": [
            doc("СП-0001", "2026-06-30T10:00:00", GUID_TEHNO, 150000.00, True),
            doc("СП-0002", "2026-07-14T10:00:00", GUID_TEHNO, 50000.00, True),
        ],
    }


_TOKEN_RE = re.compile(
    r"substringof\('(?P<sub>[^']*)',\s*(?P<subfield>\w+)\)"
    r"|(?P<field>\w+)\s+(?P<op>eq|ge|le)\s+"
    r"(?:guid'(?P<guid>[^']*)'|datetime'(?P<dt>[^']*)'|'(?P<str>(?:[^']|'')*)'|(?P<lit>true|false|[\d.]+))"
)


def _parse_filter(expr: str) -> Callable[[dict[str, Any]], bool]:
    """Ограниченный парсер $filter: условия, соединённые and."""
    conds: list[Callable[[dict[str, Any]], bool]] = []
    for m in _TOKEN_RE.finditer(expr):
        if m.group("sub") is not None:
            sub, fld = m.group("sub").lower(), m.group("subfield")
            conds.append(lambda row, s=sub, f=fld: s in str(row.get(f, "")).lower())
            continue
        fld, op = m.group("field"), m.group("op")
        if m.group("guid") is not None:
            val: Any = m.group("guid")
        elif m.group("dt") is not None:
            val = m.group("dt")
        elif m.group("str") is not None:
            val = m.group("str").replace("''", "'")
        else:
            lit = m.group("lit")
            val = {"true": True, "false": False}.get(lit)
            if val is None:
                val = float(lit)
        def cmp(row: dict[str, Any], f=fld, o=op, v=val) -> bool:
            actual = row.get(f)
            if actual is None:
                return False
            if isinstance(v, float) and isinstance(actual, (int, float)):
                actual = float(actual)
            if o == "eq":
                return actual == v
            if o == "ge":
                return str(actual) >= str(v) if not isinstance(v, float) else actual >= v
            return str(actual) <= str(v) if not isinstance(v, float) else actual <= v
        conds.append(cmp)
    return lambda row: all(c(row) for c in conds)


def _metadata_xml(dataset: dict[str, list[dict[str, Any]]]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">',
             "<Schema>"]
    for entity_set, rows in dataset.items():
        props = sorted({k for row in rows for k in row})
        parts.append(f'<EntityType Name="{entity_set}">')
        parts.extend(f'<Property Name="{p}"/>' for p in props)
        parts.append("</EntityType>")
        parts.append(f'<EntitySet Name="{entity_set}"/>')
    parts.append("</Schema></edmx:Edmx>")
    return "\n".join(parts)


class Fake1CServer:
    def __init__(self, dataset: dict[str, list[dict[str, Any]]] | None = None,
                 username: str = "robot", password: str = "test",
                 fail_first_n: int = 0):
        self.dataset = dataset if dataset is not None else default_dataset()
        self.created: list[tuple[str, dict[str, Any]]] = []  # (entity_set, payload)
        self._fail_remaining = fail_first_n  # имитация нестабильной 1С для тестов ретраев
        self._lock = threading.Lock()
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json;charset=utf-8")

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") != expected:
                    self._send(401, b"Unauthorized", "text/plain")
                    return False
                return True

            def _flaky(self) -> bool:
                with outer._lock:
                    if outer._fail_remaining > 0:
                        outer._fail_remaining -= 1
                        self._send(503, b"Service Unavailable", "text/plain")
                        return True
                return False

            def do_GET(self) -> None:
                if not self._authorized() or self._flaky():
                    return
                split = urlsplit(self.path)
                path = unquote(split.path)
                q = {k: v[0] for k, v in parse_qs(split.query).items()}
                m = re.match(r"^/(?:[\w-]+/)?odata/standard\.odata/(.+)$", path)
                if not m:
                    self._send(404, b"Not Found", "text/plain")
                    return
                resource = m.group(1)
                if resource == "$metadata":
                    self._send(200, _metadata_xml(outer.dataset).encode("utf-8"), "application/xml")
                    return
                single = re.match(r"^(\w+)\(guid'([\w-]+)'\)$", resource)
                if single:
                    entity_set, ref = single.group(1), single.group(2)
                    rows = [r for r in outer.dataset.get(entity_set, []) if r["Ref_Key"] == ref]
                    if not rows:
                        self._json(404, {"odata.error": {"message": {"value": "Не найдено"}}})
                        return
                    self._json(200, {"odata.metadata": f"$metadata#{entity_set}/@Element", **rows[0]})
                    return
                if resource not in outer.dataset:
                    self._json(404, {"odata.error": {"message": {"value": f"Нет сущности {resource}"}}})
                    return
                rows = list(outer.dataset[resource])
                if "$filter" in q:
                    rows = [r for r in rows if _parse_filter(q["$filter"])(r)]
                if "$orderby" in q:
                    fld, *rest = q["$orderby"].split()
                    rows.sort(key=lambda r: str(r.get(fld, "")),
                              reverse=bool(rest and rest[0] == "desc"))
                skip = int(q.get("$skip", 0))
                top = int(q.get("$top", len(rows)))
                rows = rows[skip:skip + top]
                if "$select" in q:
                    keep = [s.strip() for s in q["$select"].split(",")]
                    rows = [{k: r[k] for k in keep if k in r} for r in rows]
                self._json(200, {"odata.metadata": f"$metadata#{resource}", "value": rows})

            def do_POST(self) -> None:
                if not self._authorized() or self._flaky():
                    return
                path = unquote(urlsplit(self.path).path)
                m = re.match(r"^/(?:[\w-]+/)?odata/standard\.odata/(\w+)$", path)
                if not m or m.group(1) not in outer.dataset:
                    self._send(404, b"Not Found", "text/plain")
                    return
                entity_set = m.group(1)
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with outer._lock:
                    row = dict(payload)
                    row.setdefault("Ref_Key", str(uuid.uuid4()))
                    row.setdefault("Number", f"АГ-{len(outer.created) + 1:04d}")
                    row.setdefault("Posted", False)
                    outer.dataset[entity_set].append(row)
                    outer.created.append((entity_set, row))
                self._json(201, {"odata.metadata": f"$metadata#{entity_set}/@Element", **row})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/bp30"

    def __enter__(self) -> "Fake1CServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
