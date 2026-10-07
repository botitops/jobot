# jobot

Pull diario (sin LLM) de avisos de empleo remoto para Jobot.

```
GitHub Actions 22:00 Madrid  ->  pull.py  ->  data/new/AAAA-MM-DD.{tsv,jsonl} + data/report.md
Tarea programada de Claude 00:00 -> clona el repo -> etapa 1 (titulos, barato) -> etapa 2 (detalle, Sonnet) -> Drive
```

## Estado
Repo y workflow ya subidos. Falta solo: secreto `FIRECRAWL_API_KEY` (Settings > Secrets and variables > Actions) y lanzar el workflow `pull` a mano; revisar `data/report.md`.

## Agregar un portal
- Con API/RSS: una función como `remotive()` en `pull.py` y agregarla a `SOURCES`.
- Solo HTML: `html_source(nombre, [urls de listado], regex_de_link_de_aviso)`.
