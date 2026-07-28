from perimeter_core.skills import catalog_text, load_skills, make_load_skill_tool


def test_load_builtin_skills():
    skills = load_skills()
    assert {"sverka", "find-document", "counterparty-report"} <= set(skills)
    assert "Сверка" in skills["sverka"].description


def test_catalog_is_compact():
    skills = load_skills()
    text = catalog_text(skills)
    assert "load_skill" in text
    # каталог — по строке на навык, тело в промпт не попадает
    assert "get_counterparty" not in text
    assert len(text) < 600


def test_load_skill_tool():
    skills = load_skills()
    tool = make_load_skill_tool(skills)
    body = tool.func(name="sverka")
    assert "ledger_report" in body
    assert "Нет навыка" in tool.func(name="nope")
    assert not tool.requires_approval
