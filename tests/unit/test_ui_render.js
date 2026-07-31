// Проверка рендера отчёта в таблицу — той самой функции, что работает в
// браузере. Вытаскиваем её из страницы, чтобы тест шёл по живому коду.
const fs = require("fs"), assert = require("assert");
const page = fs.readFileSync(__dirname + "/../../ui/perimeter_ui/static/index.html", "utf8");
const script = page.split("<script>")[1].split("</script>")[0];
// Граница помечена в самой странице: выше неё только чистые функции, их и
// проверяем. Раньше тест резал скрипт по первому обращению к DOM и ломался
// при любой перестановке строк.
const MARK = "// --- ГРАНИЦА:";
const cut = script.indexOf(MARK);
assert(cut > 0, "в index.html нет строки-границы для теста");
eval(script.slice(0, cut));

const report = {
  title: "Дебиторка",
  text: [
    "Дебиторская задолженность по срокам (дней с даты документа):",
    "контрагент | 0-30 | итого",
    'ООО "Ромашка" | 120 000.00 | 132 000.00',
    "    №РТ-0005 от 2026-06-25 — не оплачено 12 000.00 руб.",
    "ИТОГО | 135 000.00 | 192 000.00",
    "Расчёт на 2026-07-31, оплаты разнесены по FIFO.",
  ].join("\n"),
};
const html = renderReport(report);

assert(html.includes("<h2>Дебиторка</h2>"), "нет заголовка");
assert(html.includes("<th>контрагент</th>"), "шапка не размечена");
assert(html.includes('<tr class="total">'), "строка ИТОГО не выделена");
assert(html.includes('class="num">120 000.00'), "суммы не выровнены по правому краю");
assert(html.includes('<tr class="detail"><td colspan="3">'), "детализация не свёрнута");
assert(html.includes('<div class="note">Расчёт на 2026-07-31'), "примечание потеряно");
assert(!html.includes("Дебиторская задолженность по срокам (дней"), "шапка продублирована");

// Экранирование: имя контрагента может содержать угловые скобки и кавычки.
const evil = renderReport({title: '<img src=x onerror=alert(1)>', text: 'a | <b>&"'});
assert(!evil.includes("<img"), "заголовок не экранирован");
assert(!evil.includes("<b>"), "ячейка не экранирована");

console.log("рендер отчёта: ок");
