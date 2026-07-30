"""Проверка ответа на подтверждённость данными 1С.

Зачем. Модель уверенно сочиняет то, чего нет: на живом демо она выдумала
пять несуществующих контрагентов и исказила название шестого. Для отчёта
директору это хуже отказа: неверная цифра выглядит ровно как верная.

Что делаем. После того как агент сформировал ответ, вытаскиваем из него
проверяемые факты — суммы, номера документов, названия в кавычках — и
ищем их в том, что вернули инструменты на этом ходе. Не нашли — значит
модель это придумала либо исказила.

Чего НЕ проверяем: связного текста, выводов и формулировок. Задача —
поймать выдуманные данные, а не оценивать стиль.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Сумма: 99 000,00 / 99000.00 / 1 234 567 — не меньше трёх значащих цифр,
# чтобы не цепляться за «3 контрагента» и номера пунктов.
_AMOUNT_RE = re.compile(r"\b\d[\d  ]{2,}(?:[.,]\d{2})?\b")
# Номер документа: №РТ-0002, № ПС-0001
_DOCNUM_RE = re.compile(r"№\s?([A-Za-zА-Яа-я]{1,4}[-‑]?\d{2,})")
# Название в кавычках: «Ромашка», "ТехноСервис"
_QUOTED_RE = re.compile(r"[«\"]([^»\"]{2,60})[»\"]")


# Организационно-правовые формы: в базе «ООО "Ромашка"», в ответе модели
# «Ромашка, ООО» — это один и тот же контрагент, придираться не к чему.
_LEGAL_FORM_RE = re.compile(
    r"\b(ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО|АНО|ТОО|ФГУП|ГУП|МУП|ЧОУ|НОУ)\b\.?",
    re.IGNORECASE)
_PUNCT_RE = re.compile(r"[«»\"'`,.\-–—()]+")
# Даты вырезаем перед поиском сумм: «03.07.2026» иначе даёт «2026» как
# сумму и ловится на годе, которого нет в выгрузке.
_DATE_RE = re.compile(r"\b\d{2}[.\-/]\d{2}[.\-/]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b")


def _digits(text: str) -> str:
    """«99 000,00» -> «9900000»: сравниваем суммы без разделителей."""
    return re.sub(r"\D", "", text)


def _norm_name(text: str) -> str:
    """Название без правовой формы, кавычек и пунктуации, в нижнем регистре.

    Терпимо к оформлению, но не к содержанию: «Технервис» после нормализации
    по-прежнему не равно «техносервис».
    """
    text = _LEGAL_FORM_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class GroundingResult:
    unverified_amounts: list[str] = field(default_factory=list)
    unverified_docs: list[str] = field(default_factory=list)
    unverified_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.unverified_amounts or self.unverified_docs
                    or self.unverified_names)

    def describe(self) -> str:
        parts = []
        if self.unverified_docs:
            parts.append("документы: " + ", ".join(self.unverified_docs))
        if self.unverified_amounts:
            parts.append("суммы: " + ", ".join(self.unverified_amounts))
        if self.unverified_names:
            parts.append("названия: " + ", ".join(self.unverified_names))
        return "; ".join(parts)


def check_grounding(answer: str, tool_outputs: list[str]) -> GroundingResult:
    """Сверяет факты из ответа с тем, что вернули инструменты."""
    result = GroundingResult()
    if not answer.strip():
        return result

    source = "\n".join(tool_outputs)
    if not source.strip():
        # Инструменты не вызывались — проверять не с чем: это либо разговор
        # («кто ты?»), либо отказ. Данные в таком ответе появиться не должны,
        # но судить об этом мы здесь не можем.
        return result

    source_digits = _digits(source)
    source_lower = source.lower()

    for raw in _AMOUNT_RE.findall(_DATE_RE.sub(" ", answer)):
        digits = _digits(raw)
        if len(digits) >= 3 and digits not in source_digits:
            result.unverified_amounts.append(raw.strip())

    for num in _DOCNUM_RE.findall(answer):
        normalized = num.replace("‑", "-").lower()
        if normalized not in source_lower.replace("‑", "-"):
            result.unverified_docs.append(num)

    source_names = _norm_name(source)
    for name in _QUOTED_RE.findall(answer):
        normalized = _norm_name(name)
        # Слишком короткий остаток («ООО», «АО») ничего не идентифицирует.
        if len(normalized) < 3:
            continue
        if normalized not in source_names:
            result.unverified_names.append(name)

    return result


CORRECTION_PROMPT = (
    "В твоём ответе есть данные, которых нет в результатах инструментов ({details}). "
    "Перепиши ответ, используя ТОЛЬКО полученные данные: названия, номера и суммы "
    "переписывай дословно. Если чего-то в данных нет — так и скажи."
)

WARNING_SUFFIX = (
    "\n\n⚠ Часть данных в ответе не подтверждается выгрузкой из 1С "
    "({details}) — проверьте в базе перед использованием."
)
