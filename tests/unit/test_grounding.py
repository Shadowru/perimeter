"""Проверка антигаллюцинационного контура.

Поводом послужил живой диалог: модель выдала пять контрагентов, которых
в базе нет, и исказила название шестого. Тесты фиксируют оба класса ошибок.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from perimeter_core.grounding import check_grounding


TOOL_OUT = [
    "Ромашка, ООО | ИНН 7701234567 | key=aaa",
    "РТ-0001 от 2026-07-01 | 90 000.00 | проведён",
]


def test_answer_from_data_passes():
    ok = check_grounding(
        "По «Ромашка, ООО» есть документ № РТ-0001 на 90 000.00 руб.", TOOL_OUT)
    assert ok.ok


def test_invented_counterparty_is_caught():
    res = check_grounding("Крупнейшие клиенты: «ООО Вектор», «ИП Иванов».", TOOL_OUT)
    assert res.unverified_names == ["ООО Вектор", "ИП Иванов"]
    assert not res.ok


def test_distorted_name_is_caught():
    """«Технервис» вместо «ТехноСервис» — данные есть, но переписаны неверно."""
    res = check_grounding("Отгрузка в адрес «Технервис».",
                          ["АО «ТехноСервис» | key=bbb"])
    assert res.unverified_names == ["Технервис"]


def test_invented_amount_is_caught():
    res = check_grounding("Итого продаж 1 234 500.00 руб.", TOOL_OUT)
    assert res.unverified_amounts and not res.ok


def test_amount_matches_regardless_of_separators():
    """«90000», «90 000.00» и «90 000,00» — одна и та же сумма."""
    assert check_grounding("Сумма 90 000,00", ["РТ-0001 | 90000.00"]).ok


def test_invented_document_number_is_caught():
    res = check_grounding("См. документ № РТ-0099.", TOOL_OUT)
    assert res.unverified_docs == ["РТ-0099"]


def test_small_numbers_are_not_treated_as_amounts():
    """«3 контрагента», «за 7 дней» — не суммы, придирок быть не должно."""
    assert check_grounding("Найдено 3 контрагента за 7 дней.", TOOL_OUT).ok


def test_no_tools_no_verdict():
    """Ответ без обращения к данным («кто ты?») проверять не с чем."""
    assert check_grounding("Я — «Периметр», локальный помощник.", []).ok


def test_describe_lists_all_problems():
    res = check_grounding("«ООО Вектор» должен 555 000.00 по № РТ-0099.", TOOL_OUT)
    text = res.describe()
    assert "РТ-0099" in text and "555 000.00" in text and "ООО Вектор" in text


def test_legal_form_may_be_written_differently():
    """В базе «ООО "Ромашка"», в ответе «Ромашка, ООО» — это тот же контрагент."""
    assert check_grounding('Контрагент «Ромашка, ООО».',
                           ['ООО "Ромашка" | ИНН 7701234567']).ok


def test_legal_form_alone_is_not_a_name():
    assert check_grounding('Форма собственности — «ООО».', TOOL_OUT).ok


# --- защита от ложных срабатываний ----------------------------------------
# Сверка не должна отправлять на переписывание добросовестный ответ: каждый
# лишний ход — это ещё 20-30 секунд ожидания на стенде 16 ГБ.

def _reports():
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer
    from perimeter_bridge1c.analytics import AnalyticsTools
    from perimeter_bridge1c.mapping import load_mapping
    from perimeter_bridge1c.odata import ODataClient
    with Fake1CServer() as srv:
        m = load_mapping("bp30")
        a = AnalyticsTools(ODataClient(srv.base_url, "robot", "test", mapping=m), m)
        yield "abc", a.abc_analysis("counterparty"), (
            "ABC-анализ по выручке за всё время: группа A — «ООО «Ромашка»» "
            "252 000.00 руб. (77.1%) и «ООО «Василёк»» 75 000.00 руб. (22.9%). "
            "Всего 327 000.00 руб.")
        yield "receivables", a.receivables_aging(as_of="2026-07-31T00:00:00"), (
            "Нам должны 192 000.00 руб. «ООО «Ромашка»» — 132 000.00 руб. "
            "(№РТ-0001 от 03.07.2026 на 120 000.00 и №РТ-0005 от 25.06.2026 "
            "на 12 000.00), «ООО «Василёк»» — 60 000.00 руб.")
        yield "payables", a.payables_aging(as_of="2026-07-31T00:00:00"), (
            "Мы должны поставщикам 140 000.00 руб.: «АО «ТехноСервис»» "
            "100 000.00 по № ПТ-0001, «ООО «Василёк»» 40 000.00 по № ПТ-0002.")
        yield "cash_flow", a.cash_flow(), (
            "За всё время поступило 135 000.00 руб., списано 200 000.00 руб., "
            "чистый поток минус 65 000.00 руб.")
        yield "pnl", a.pnl_report(), (
            "Выручка 234 000.00 руб., себестоимость 159 500.00 руб., "
            "валовая прибыль 74 500.00 руб. Это валовая прибыль: расходы по "
            "счетам 26 и 44 в данных отсутствуют.")
        yield "act", a.reconciliation_act(GUID_ROMASHKA), (
            "Акт сверки с «ООО «Ромашка»»: отгружено 252 000.00 руб., "
            "оплачено 120 000.00 руб., сальдо на конец 132 000.00 руб. "
            "в нашу пользу (№ РТ-0001, № РТ-0005, № РТ-0007, № ПС-0001).")
        yield "dynamics", a.sales_dynamics(), (
            "Продажи растут: май 93 000.00, июнь 99 000.00, июль 135 000.00 руб. "
            "Средний чек в июле 67 500.00 руб. при 2 отгрузках.")


def test_faithful_retelling_of_every_report_passes():
    for name, report, answer in _reports():
        res = check_grounding(answer, [report])
        assert res.ok, f"{name}: ложное срабатывание — {res.describe()}"


def test_single_wrong_digit_in_a_report_is_caught():
    """Подмена одной цифры в пересказе отчёта должна не пройти."""
    for name, report, answer in _reports():
        broken = answer.replace("000.00", "111.00", 1)
        assert not check_grounding(broken, [report]).ok, f"{name}: подмена не поймана"


def test_dates_are_not_mistaken_for_amounts():
    """«от 03.07.2026» — это дата, а не сумма: год не должен ловиться."""
    assert check_grounding("Отгрузка от 03.07.2026 и от 2026-07-03.",
                           ["РТ-0001 | 2026-07-03 | 90 000.00"]).ok


def test_name_from_the_question_is_not_invented():
    """«Сделай акт сверки с Ромашкой» -> «по Ромашке данных нет» — это правда."""
    res = check_grounding("По контрагенту «Ромашка» данных нет.",
                          ["По этому контрагенту нет проведённых отгрузок и оплат."],
                          question="Сделай акт сверки с Ромашкой")
    assert res.ok


def test_question_does_not_excuse_invented_amounts():
    res = check_grounding("Долг «Ромашка» — 700 000.00 руб.",
                          ["По этому контрагенту нет проведённых отгрузок."],
                          question="Сколько должна Ромашка?")
    assert res.unverified_amounts == ["700 000.00"]


def test_case_endings_do_not_break_name_check():
    """«Ромашке», «Ромашкой», «Ромашки» — тот же контрагент, что «Ромашка»."""
    src = ['ООО "Ромашка" | ИНН 7701234567']
    for form in ("Ромашке", "Ромашкой", "Ромашки", "Ромашка"):
        assert check_grounding(f"Долг «{form}» — 100.00 руб.", src).unverified_names == []


def test_stem_match_still_catches_distortion():
    """Послабление на падежи не должно пропускать искажённое название."""
    res = check_grounding("Отгрузка «ТехнSERVIC».", ['АО "ТехноСервис" | key=bbb'])
    assert res.unverified_names == ["ТехнSERVIC"]


def test_distorted_name_gets_the_correct_spelling():
    """Модели мало сказать «неверно» — она повторяет ошибку. Даём написание."""
    res = check_grounding("Долг «Технервис» — 100.00 руб.",
                          ['АО "ТехноСервис" | ИНН 7703334455 | key=bbb'])
    assert res.name_corrections["Технервис"] == 'АО "ТехноСервис"'
    assert 'в данных «АО "ТехноСервис"»' in res.describe()


def test_no_suggestion_when_nothing_resembles_it():
    """Полностью выдуманный контрагент — не опечатка, подсказывать нечего."""
    res = check_grounding("Клиент «ООО Вектор».", ['ООО "Ромашка" | key=aaa'])
    assert res.unverified_names == ["ООО Вектор"] and not res.name_corrections


def test_latin_garbled_name_is_matched_by_prefix():
    """«ТехнSERVICОВЕР» — то, что живая модель выдала вместо «ТехноСервис»."""
    res = check_grounding("Долг «ТехнSERVICОВЕР» — 100.00 руб.",
                          ['АО "ТехноСервис" | ИНН 5047112233 | key=bbb'])
    assert res.name_corrections["ТехнSERVICОВЕР"] == 'АО "ТехноСервис"'


def test_ambiguous_prefix_is_not_guessed():
    """Два кандидата с тем же началом — угадывать нельзя."""
    res = check_grounding("Долг «ТехнXXX» — 100.00 руб.",
                          ['АО "ТехноСервис" | key=b', 'ООО "Технополис" | key=c'])
    assert res.unverified_names == ["ТехнXXX"] and not res.name_corrections


def test_nested_quotes_do_not_hide_a_distortion():
    """«АО "Технʼyервис"»: раньше проверялось только «АО », искажение проходило."""
    res = check_grounding('Контрагент «АО "Технʼyервис"» должен 100.00 руб.',
                          ['АО "ТехноСервис" | ИНН 5047112233 | key=b'])
    assert res.unverified_names == ['АО "Технʼyервис"']
    assert res.name_corrections['АО "Технʼyервис"'] == 'АО "ТехноСервис"'


def test_substitution_reads_naturally():
    """Подстановка не должна оставлять «АО «АО "X"»» или «"X"»."""
    from perimeter_core.grounding import apply_name_corrections
    src = ['АО "ТехноСервис" | ИНН 5047112233 | key=b']
    cases = {
        '100 000.00 руб. (АО «ТехнSERVICER»)': '100 000.00 руб. (АО "ТехноСервис")',
        'Контрагент «ТехнSERVICER» должен 100.00 руб.':
            'Контрагент АО "ТехноСервис" должен 100.00 руб.',
        'Долг «АО "ТехнSERVICER"» — 100.00 руб.': 'Долг АО "ТехноСервис" — 100.00 руб.',
    }
    for before, after in cases.items():
        text, fixes = apply_name_corrections(before, check_grounding(before, src))
        assert text == after, text
        assert fixes
