"""Клиент стандартного интерфейса OData 1С:Предприятие 8.3 (stdlib).

- HTTP Basic, JSON ($format=json).
- Постраничная выборка $top/$skip (у 1С нет server-driven paging).
- Ретраи с экспоненциальным backoff на 5xx/сетевых ошибках.
- Валидация маппинга против $metadata при подключении.
- Запись: только POST-создание; Post/Unpost (проведение) НЕ реализованы
  намеренно — агент создаёт исключительно черновики (Posted=false).

Хост базы обязан быть в config/perimeter.yaml:allowed_hosts (валидируется
конфигом; netguard дублирует защиту в рантайме).
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .backend import (KIND_BOOL, KIND_DATETIME, KIND_GUID, KIND_NUMBER, OP_CONTAINS,
                      OP_EQ, OP_GE, OP_LE, Cond, Query)
from .mapping import ConfigurationMapping, EntityMapping


class ODataError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ODataClient:
    def __init__(self, base_url: str, username: str, password: str, *,
                 timeout_s: float = 30.0, retries: int = 3, page_size: int = 200,
                 mapping: ConfigurationMapping | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self.page_size = page_size
        self.mapping = mapping
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._auth = f"Basic {token}"

    # --- транспорт -------------------------------------------------------

    def _url(self, path: str, params: dict[str, str] | None = None) -> str:
        # Имена сущностей 1С — кириллица; urllib требует ASCII → кодируем путь.
        quoted = urllib.parse.quote(path, safe="()'$/-")
        url = f"{self.base_url}/odata/standard.odata/{quoted}"
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return url

    def _request(self, method: str, path: str, params: dict[str, str] | None = None,
                 body: dict[str, Any] | None = None, accept_json: bool = True) -> Any:
        url = self._url(path, params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": self._auth,
                "Accept": "application/json" if accept_json else "application/xml",
                **({"Content-Type": "application/json"} if data else {}),
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if accept_json else raw.decode("utf-8")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:500]
                if e.code >= 500 and attempt < self.retries:
                    last_error = e
                else:
                    raise ODataError(f"1С OData HTTP {e.code}: {detail}", status=e.code) from e
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt >= self.retries:
                    raise ODataError(f"1С недоступна: {e}") from e
                last_error = e
            time.sleep(min(2.0 ** attempt, 10.0))
        raise ODataError(f"1С: ретраи исчерпаны: {last_error}")

    # --- чтение ----------------------------------------------------------

    def query(self, entity_set: str, *, filter_: str | None = None,
              select: list[str] | None = None, order_by: str | None = None,
              top: int | None = None) -> Iterator[dict[str, Any]]:
        """Выборка с автоматической пагинацией ($top/$skip)."""
        remaining = top
        skip = 0
        while True:
            page = self.page_size if remaining is None else min(self.page_size, remaining)
            if page <= 0:
                return
            params: dict[str, str] = {"$format": "json", "$top": str(page), "$skip": str(skip)}
            if filter_:
                params["$filter"] = filter_
            if select:
                params["$select"] = ",".join(select)
            if order_by:
                params["$orderby"] = order_by
            data = self._request("GET", entity_set, params)
            rows = data.get("value", [])
            yield from rows
            if len(rows) < page:
                return
            skip += len(rows)
            if remaining is not None:
                remaining -= len(rows)

    def run(self, query: Query) -> Iterator[dict[str, Any]]:
        """Реализация Backend: структурный запрос → $filter."""
        return self.query(
            query.entity_set,
            filter_=render_filter(query.conditions) or None,
            select=query.select,
            order_by=query.order_by,
            top=query.top,
        )

    def get(self, entity_set: str, ref_key: str, select: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, str] = {"$format": "json"}
        if select:
            params["$select"] = ",".join(select)
        return self._request("GET", f"{entity_set}(guid'{ref_key}')", params)

    # --- запись (только черновики!) --------------------------------------

    def create_draft(self, entity_set: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Создание документа-черновика. Posted принудительно False;
        метода «провести» у клиента нет by design (guardrail продукта)."""
        body = dict(payload)
        body["Posted"] = False
        return self._request("POST", entity_set, {"$format": "json"}, body=body)

    # --- $metadata -------------------------------------------------------

    def fetch_metadata_xml(self) -> str:
        return self._request("GET", "$metadata", accept_json=False)

    def validate_mapping(self) -> list[str]:
        """Сверка маппинга с $metadata базы. Возвращает список проблем
        (пустой = всё сходится). Не бросает: решение — за вызывающим."""
        if self.mapping is None:
            return ["маппинг не задан"]
        xml = self.fetch_metadata_xml()
        problems = []
        for ent in self.mapping.entities.values():
            if f'"{ent.entity_set}"' not in xml and f"'{ent.entity_set}'" not in xml \
                    and ent.entity_set not in xml:
                problems.append(f"сущность {ent.entity_set} отсутствует в $metadata")
                continue
            for logical, name_1c in ent.fields.items():
                if name_1c not in xml:
                    problems.append(
                        f"{ent.entity_set}: поле {name_1c} (={logical}) не найдено в $metadata")
        return problems


# --- помощники построения $filter (синтаксис OData 3.0 1С) ---------------

def f_eq_guid(field: str, guid: str) -> str:
    return f"{field} eq guid'{guid}'"


def f_eq_str(field: str, value: str) -> str:
    escaped = value.replace("'", "''")
    return f"{field} eq '{escaped}'"


def f_eq_bool(field: str, value: bool) -> str:
    return f"{field} eq {'true' if value else 'false'}"


def f_date_range(field: str, date_from: str | None, date_to: str | None) -> list[str]:
    """Даты в формате ISO: 2026-07-01T00:00:00."""
    parts = []
    if date_from:
        parts.append(f"{field} ge datetime'{date_from}'")
    if date_to:
        parts.append(f"{field} le datetime'{date_to}'")
    return parts


def f_and(parts: list[str]) -> str:
    return " and ".join(p for p in parts if p)


def render_cond(c: Cond) -> str:
    """Одно структурное условие → фрагмент $filter."""
    if c.op == OP_CONTAINS:
        return f"substringof('{str(c.value).replace(chr(39), chr(39) * 2)}', {c.field})"
    if c.op not in (OP_EQ, OP_GE, OP_LE):
        raise ODataError(f"неподдерживаемая операция отбора: {c.op}")
    if c.kind == KIND_GUID:
        literal = f"guid'{c.value}'"
    elif c.kind == KIND_DATETIME:
        literal = f"datetime'{c.value}'"
    elif c.kind == KIND_BOOL:
        literal = "true" if c.value else "false"
    elif c.kind == KIND_NUMBER:
        literal = str(c.value)
    else:
        literal = "'" + str(c.value).replace("'", "''") + "'"
    return f"{c.field} {c.op} {literal}"


def render_filter(conditions: list[Cond]) -> str:
    return f_and([render_cond(c) for c in conditions])
