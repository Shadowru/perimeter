---
name: find-document
description: Поиск документов 1С по типу, контрагенту, периоду, статусу проведения
---

Порядок:
1. Если указан контрагент — сначала get_counterparty, возьми key.
2. find_document с точными фильтрами: doc_type (sale | purchase | incoming_payment | customer_invoice), период ISO-датами, posted при необходимости.
3. Ответ — список «№ … от …, сумма, статус». Если пусто — скажи прямо и предложи расширить период.
