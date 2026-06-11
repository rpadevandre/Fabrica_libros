# BookForge v2 — Fabrica Autonoma de Libros

Agent swarm para investigar, producir, publicar y promocionar libros de
calidad en mercados angloparlantes/europeos. Infinity Ascend Software.

## Arquitectura

5 pipelines orquestados por una state machine con dos gates humanos
obligatorios (brief y manuscrito):

```
P1 Market Intelligence -> [GATE 1] -> P2 Production Swarm -> QA >= 80
-> [GATE 2] -> P3 Visual -> P4 Publish (dry_run) -> P5 Marketing (flags)
```

## Quick start (sin infraestructura)

```bash
pip install -e ".[dev]"
cp .env.example .env       # poner BF_ANTHROPIC_API_KEY

# Tests (no requieren API keys)
pytest tests/ -q

# Investigar mercado y crear candidatos
python -m bookforge.cli research "productivity for developers" --top 2
python -m bookforge.cli books

# Servidor
uvicorn bookforge.orchestrator.main:app --reload
# POST /books/{id}/approve-brief  -> arranca produccion en background
# GET  /books/{id}/manuscript     -> revisar antes del gate 2
# POST /books/{id}/approve-manuscript
```

## Flags de marketing (ON/OFF)

Jerarquia: `master` AND grupo (`organic`/`paid`) AND canal. Todo OFF por
defecto. Fail-safe: archivo corrupto o Redis caido = todo apagado.

```bash
python -m bookforge.cli flag master on
python -m bookforge.cli flag organic on
python -m bookforge.cli flag pinterest on
python -m bookforge.cli kill          # apaga TODO
```

Circuit breakers del pipeline pago (`PaidAdsManager.tick`):
presupuesto diario, ACOS maximo, 14 dias sin ventas.

## Publicacion KDP

`BF_KDP_DRY_RUN=true` por defecto: Playwright llena el flujo, captura
screenshots y se detiene antes de Publish. La divulgacion de contenido
IA esta forzada en codigo (`build_kdp_plan` aborta si se desactiva).
Los selectores del DOM de KDP cambian: completar el flujo iterando
contra la pagina real (sesion persistida en `data/kdp_session`).

## Infraestructura completa

```bash
docker compose up    # Postgres + Redis + n8n + API
```

n8n recibe los posts organicos via webhooks `bookforge-{canal}` y los
publica con las credenciales sociales (fuera de este repo).

## Estructura

```
bookforge/core/        models (contratos), llm, storage, config
bookforge/orchestrator/ main (FastAPI), flags
bookforge/pipelines/   p1_market .. p5_marketing
brands/                un YAML por linea editorial (BrandKit)
tests/                 35 tests: state machine, flags, breakers, specs KDP, daemon
```

## Modo autonomo

```bash
# Configurar (o via PUT /autonomy)
curl -X PUT localhost:8000/autonomy -H 'Content-Type: application/json' -d '{
  "level": 1,
  "seed_interests": ["productivity for developers"],
  "monthly_budget_usd": "80",
  "brief_auto_threshold": 85,
  "max_books_in_flight": 2,
  "max_new_books_per_month": 4,
  "notify_webhook": "http://localhost:5678/webhook/bookforge-notify"
}'

python -m bookforge.daemon          # loop infinito
python -m bookforge.daemon --once   # un tick (debug o cron)
```

Niveles:
- **0 MANUAL**: el daemon no hace nada (default).
- **1 AUTO_BRIEF**: scouting semanal + auto-aprueba briefs GO con score
  >= 85 + produce hasta HUMAN_REVIEW. Te notifica para el gate 2.
- **2 AUTO_BOOK**: ademas auto-aprueba manuscritos con QA >= 90 y avanza
  visual + EPUB hasta READY_TO_PUBLISH. El click de Publish sigue
  respetando kdp_dry_run.

Frenos siempre activos: presupuesto mensual (si se supera, no inicia
trabajo nuevo), maximo de libros en vuelo, maximo de libros nuevos al
mes. El daemon NUNCA anula kdp_dry_run.

## Economia

Cost tracker registra cada llamada LLM por libro y fase. Objetivo:
< $25/libro (`BF_MAX_COST_PER_BOOK_USD`). `GET /costs/{book_id}`.
