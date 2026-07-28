from perimeter_core import i18n


def test_ru_base():
    i18n.set_locale("ru")
    assert "Периметр" in i18n.t("agent.greeting")


def test_en_secondary():
    i18n.set_locale("en")
    assert "Perimeter" in i18n.t("agent.greeting")
    i18n.set_locale("ru")


def test_missing_key_returns_key():
    assert i18n.t("no.such.key") == "no.such.key"


def test_formatting():
    i18n.set_locale("ru")
    assert "/x/y.yaml" in i18n.t("error.config_missing", path="/x/y.yaml")


def test_locales_have_same_keys():
    import json
    from pathlib import Path
    locales = Path(i18n.__file__).parent / "locales"
    ru = json.loads((locales / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((locales / "en.json").read_text(encoding="utf-8"))
    assert ru.keys() == en.keys()
