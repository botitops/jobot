# jobot

Pull diario (sin LLM) de avisos publicos de empleo remoto. Solo contiene avisos publicos y codigo.

```
GitHub Actions 22:00 Madrid  ->  pull.py  ->  data/new/AAAA-MM-DD.{tsv,jsonl} + data/report.md
Agente programado 00:00 -> clona el repo -> match por titulo (modelo barato) -> revision de detalle -> lista de revision manual
```

## Estado
Repo y workflow ya subidos. Falta solo: secreto `FIRECRAWL_API_KEY` (Settings > Secrets and variables > Actions) y lanzar el workflow `pull` a mano; revisar `data/report.md`.

## Agregar un portal
- Con API/RSS: una función como `remotive()` en `pull.py` y agregarla a `SOURCES`.
- Solo HTML: `html_source(nombre, [urls de listado], regex_de_link_de_aviso)`.
