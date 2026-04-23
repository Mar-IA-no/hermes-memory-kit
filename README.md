<p align="center">
  <h1 align="center">🧠 Hermes Memory Kit</h1>
</p>

<p align="center">
  <em>Sistema de memoria operativa para agentes tipo Hermes — canon durable + handoff + auto-inyección de continuidad entre sesiones.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/Hermes-v0.10.0-e710bb1f.svg" alt="Hermes pinned">
  <img src="https://img.shields.io/badge/status-hardening-orange.svg" alt="Status">
</p>

---

## TL;DR

**Hermes Memory Kit** no es solo una memorioteca SQLite. Es un **stack de memoria operativa para agentes**: canon durable (`library.db`), retrieval curado, handoff conversacional tras crash/restart, y auto-inyección de continuidad al volver a entrar.

La idea central del kit es esta:

> **La memoria del agente no es solo lo que se guarda; es también lo que el agente puede reabsorber automáticamente al volver.**

Un comando monta un workspace autocontenido. Si además corrés Hermes Agent, el plugin `dialogue-handoff` convierte ese workspace en una memoria que **sobrevive reinicios, reseteos de sesión y pérdida de hilo** sin depender de copy-paste manual.

No requiere Docker, ni Postgres, ni servicios externos. Un archivo SQLite + scripts Python + un wrapper shell.

---

## Tabla de contenidos

- [¿Para quién es?](#para-quién-es)
- [Quick start](#quick-start)
- [Qué incluye](#qué-incluye)
- [Arquitectura](#arquitectura)
- [Memoria operativa: canon + handoff + auto-inyección](#memoria-operativa-canon--handoff--auto-inyección)
- [Layout del workspace](#layout-del-workspace)
- [Comandos esenciales](#comandos-esenciales)
- [Configuración](#configuración)
- [Compatibilidad](#compatibilidad)
- [Principios de diseño](#principios-de-diseño)
- [Estado del repo](#estado-del-repo)
- [Docs](#docs)
- [Contribuir](#contribuir)
- [Licencia](#licencia)

---

## ¿Para quién es?

- Sos developer o operador de **Hermes Agent** (o un agent framework similar) y necesitás que tu agente **retome el hilo** entre sesiones sin tener que re-explicarle todo.
- Querés una capa de **memoria local**, sin mandar tus datos a servicios cloud.
- Tu hardware es modesto (ej. no querés correr Docker + Postgres + Neo4j). Un SQLite + embeddings via API ya te alcanza.
- Querés un **bibliotecario** que decida qué contexto recuperar e inyectar, no un memory dump indiscriminado.
- Te importa que la memoria también cubra el caso más molesto de producción: **crash, reset, sesión nueva, agente perdido**.

---

## Quick start

> 💡 **Requisito**: Python 3.10+.

```bash
# 1. Clonar el kit
git clone https://github.com/Mar-IA-no/hermes-memory-kit.git
cd hermes-memory-kit

# 2. Instalar deps
pip install -r requirements.txt

# 3. Montar tu workspace (autocontenido — trae scripts, plugins, templates)
python3 scripts/bootstrap_workspace.py --workspace ~/mi-workspace --with-wiki-templates

# 4. Configurar + inicializar
cd ~/mi-workspace
cp .env.example .env        # editar si querés; defaults son todos relativos al workspace
./scripts/hmk memoryctl.py init

# 5. Guardar y recuperar
./scripts/hmk memoryctl.py add-text --shelf library --title "hola" --raw "mi primer memoria" --tags nota
./scripts/hmk memoryctl.py hybrid-pack --query "hola" --limit 3
```

Eso es todo. La DB vive en `~/mi-workspace/agent-memory/library.db` y podés usar los scripts via el wrapper `./scripts/hmk`.

Si corrés Hermes Agent, el paso siguiente es activar `plugins/dialogue-handoff/`: ahí el kit deja de ser solo storage y pasa a comportarse como **memoria operativa con re-entry automático**.

---

## Qué incluye

| Componente | Archivo / path | Qué hace |
|---|---|---|
| 🧩 **dialogue-handoff plugin** | `templates/plugins/dialogue-handoff/` | capa de auto-inyección de continuidad: recupera el último arco útil al arrancar una sesión nueva |
| 📦 **memoryctl** | `scripts/memoryctl.py` | canon SQLite + FTS5 + embeddings (NVIDIA / Google / local) + retrieval lexical e híbrido |
| 🗃 **ingest_any** | `scripts/ingest_any.py` | normaliza PDFs, DOCX, HTML, MD a markdown antes de almacenar |
| 🗺 **export_obsidian** | `scripts/export_obsidian.py` | proyecta el canon a un vault Obsidian / LLM wiki |
| 🔁 **continuityctl** | `scripts/continuityctl.py` | re-hidratación táctica tras restart/crash — identity + meta_context + dialogue_handoff en un JSON consumible |
| ⚙️ **hmk wrapper** | `scripts/hmk` | shell wrapper que carga `.env`, absolutiza paths relativos, hace cd al workspace |
| 📋 **templates** | `templates/` | AGENTS.md + librarian skill + estructura de memoria lista para tu workspace |
| 🧪 **smoke-test** | `scripts/smoke-test.sh` | verificación end-to-end del kit |

---

## Arquitectura

<p align="center">
  <img src="docs/assets/architecture.svg" alt="Arquitectura de Hermes Memory Kit: workspace autocontenido, canon durable en library.db, continuidad reinyectable con dialogue-handoff y capa proyectada wiki." width="980">
</p>

**Tres capas que juntas forman la memoria operativa**:

- `library.db` → **canon durable** (fuente de verdad, FTS5 + embeddings, precisa)
- `DIALOGUE-HANDOFF.md` + `ALWAYS-CONTEXT.md` → **continuidad reinyectable** (lo que el agente necesita reabsorber al volver)
- `wiki/` → **proyección** (navegación humana tipo Obsidian, generada desde el canon)


---

## Memoria operativa: canon + handoff + auto-inyección

La parte más diferencial del kit no es solo que guarda memoria. Es que la vuelve **usable después de una interrupción real**.

El problema que resuelve no es abstracto:

> **"Abro Hermes, digo 'continua', y me pregunta de qué estábamos hablando."**

Para este repo, eso **también es memoria del agente**. No es un accesorio al costado del storage. Es la mitad faltante del sistema.

Por eso el kit se apoya en dos piezas complementarias:

- **Canon durable**: `memoryctl.py` + `library.db` guardan y recuperan conocimiento de forma curada.
- **Continuidad inyectable**: `dialogue-handoff` + `continuityctl.py` permiten que el agente vuelva a entrar con hilo conversacional, working set y recordatorios persistentes.

El plugin `dialogue-handoff` (v2.1) implementa esa segunda mitad con **dos capas**:

- **Capa volátil** (`DIALOGUE-HANDOFF.md`) — el último turno + arco reciente, escrito después de cada interacción vía `post_llm_call`. Tiered-compressed:

| Tier | Scope | Verbosidad |
|---|---|---|
| 1 | últimos 2 exchanges | verbatim, 300 chars/msg |
| 2 | exchanges 3-6 | headline, 150 chars/msg |
| 3 | exchanges 7-20 | stride 1-de-3, 80 chars/msg |
| 4 | > 20 | descartado |

Budget: 6000 chars. Position-aware (los más recientes al final para vencer "lost-in-the-middle").

- **Capa persistente** (`ALWAYS-CONTEXT.md`) — reminders imperativos sobre capacidades del workspace ("usá memoryctl ANTES de grep"). User-editable, budget 1000 chars, siempre se inyecta si existe — incluso cuando el handoff está vacío o stale. Resuelve el problema común de que el modelo "se olvida" que tiene el sistema de memoria disponible.

**Gates** para no molestar: no inyecta en turnos subsiguientes, ni en comandos `/`, ni si el handoff tiene más de 24h.

👉 Ver [docs/dialogue-handoff.md](docs/dialogue-handoff.md) para install y wiring.

Sin plugin, el kit sigue siendo una buena memorioteca local. Con plugin, pasa a ser una **memoria operativa de re-entry**: el agente no solo recuerda cosas, también **retoma conversación**.

---

## Layout del workspace

Después de `bootstrap_workspace.py --workspace ~/mi-workspace --with-wiki-templates`:

```
mi-workspace/
├── AGENTS.md                     ← guía al agente sobre cómo usar el kit
├── .env.example                  ← copiar a .env y ajustar
├── scripts/
│   ├── hmk                       ← wrapper — carga .env, cd al workspace
│   ├── memoryctl.py              ← CLI principal
│   ├── ingest_any.py
│   ├── export_obsidian.py
│   ├── continuityctl.py
│   └── smoke-test.sh
├── agent-memory/
│   ├── library.db                ← canon (creada por memoryctl init)
│   ├── state/
│   │   ├── NOW.md
│   │   └── DIALOGUE-HANDOFF.md   ← autoescrito por plugin
│   ├── identity/  plans/  episodes/  library/  evidence/  index/
├── skills/
│   └── memory/librarian/SKILL.md ← instruye al agente sobre curación
├── plugins/
│   └── dialogue-handoff/         ← capa de re-entry / continuidad para Hermes
└── wiki/
    ├── index.md
    └── maps/                     ← proyección desde el canon
```

---

## Comandos esenciales

Todos via el wrapper `./scripts/hmk` para que el `.env` se cargue solo:

```bash
# Inicializar DB
./scripts/hmk memoryctl.py init

# Ver config de embeddings
./scripts/hmk memoryctl.py embed-config

# Agregar texto directo
./scripts/hmk memoryctl.py add-text --shelf library --title "X" --raw "contenido" --tags t1,t2

# Ingestar un archivo (PDF / DOCX / HTML / MD)
./scripts/hmk ingest_any.py --source /path/file.pdf --shelf evidence --title "paper-x" --tags pdf

# Búsqueda lexical
./scripts/hmk memoryctl.py search --query "..." --limit 5

# Retrieval híbrido (lexical + semántico) con budget de tokens
./scripts/hmk memoryctl.py hybrid-pack --query "..." --budget 1800 --limit 4 --threshold 0.4

# Expandir un chunk específico
./scripts/hmk memoryctl.py expand --id 42

# Stats
./scripts/hmk memoryctl.py stats

# Re-hidratación táctica (para resumir contexto tras restart)
./scripts/hmk continuityctl.py rehydrate

# Proyección a Obsidian
./scripts/hmk export_obsidian.py --ids 1 2 3

# Smoke test del kit completo
./scripts/smoke-test.sh
```

---

## Configuración

El wrapper `./scripts/hmk` carga `.env` desde el workspace root, **absolutiza** cualquier path relativo contra ese root, y hace `cd` al workspace antes de ejecutar. Los defaults de `.env.example` usan paths relativos (`./agent-memory`) así que funcionan sin editar.

Variables (todas opcionales — hay fallbacks sensatos):

| Variable | Para qué | Default |
|---|---|---|
| `HMK_BASE_DIR` | dónde viven memory/state/DB | `./agent-memory` |
| `HMK_DB_PATH` | SQLite library | `./agent-memory/library.db` |
| `HMK_VAULT_DIR` | target para proyección Obsidian | `./wiki` |
| `HMK_HERMES_HOME` | home de Hermes Agent (para el plugin) | — |
| `HMK_AGENT_MEMORY_BASE` | alias de BASE_DIR (usado por el plugin) | — |
| `HMK_DIALOGUE_HANDOFF_PATH` | ruta directa al handoff (override) | — |
| `HERMES_EMBED_PROVIDER` | `nvidia` / `google` / `local` | `nvidia` |
| `HERMES_EMBED_MODEL` | modelo del provider | (ver providers.md) |
| `NVIDIA_API_KEY` | clave para NVIDIA NIM | — |
| `GEMINI_API_KEY` | clave para Google Gemini embeddings | — |

Ver [docs/install.md](docs/install.md) para el flujo completo, [docs/providers.md](docs/providers.md) para alternativas de embeddings.

---

## Compatibilidad

- **Python**: 3.10+ (testeado en 3.12)
- **Linux**: testeado en Ubuntu 22.04+ / Debian 12+ / Linux Mint 22
- **Hermes Agent plugin**: pinned a **v0.10.0** (upstream commit `e710bb1f`, release 2026.4.16). Requiere hooks `pre_llm_call`, `post_llm_call`, `on_session_start`. No testeado contra releases anteriores.

---

## Principios de diseño

- **Canon primero, proyección después** — `library.db` es verdad; `wiki/` es solo navegación.
- **La continuidad también es memoria** — crash recovery, session handoff y re-entry no son “nice to have”; son parte del sistema.
- **Local por default** — SQLite + embeddings por API. Sin servicios pesados.
- **Embeddings desacoplados** — podés cambiar de provider sin re-ingestar.
- **Null retrieval OK** — si el top-k no supera el threshold, devuelve vacío (no padding con ruido).
- **Zero background loops** — nada corre solo salvo que lo pidas.
- **Workspace autocontenido** — cada workspace es un dir con todo lo necesario; scripts incluidos.
- **Graceful degradation** — sin plugin tenés canon + retrieval; con plugin tenés la experiencia completa de memoria operativa.

---

## Estado del repo

| Área | Estado |
|---|---|
| dialogue-handoff plugin | ✅ v2.1 (always-context + handoff layers), testeado manual con Hermes v0.10.0 |
| memoryctl (retrieval + storage) | ✅ estable |
| bootstrap + workspace upgrade | ✅ estable (smoke test pasa) |
| ingest_any | 🟡 funciona, deps (`mammoth`, `markdownify`, `trafilatura`) deben estar instalados |
| export_obsidian | ✅ estable |
| continuityctl | ✅ portado del sistema live, estable |
| CI / pyproject.toml | ⏳ pendiente — por ahora solo smoke test local |

Este repo es una extracción portable del sistema construido en una notebook real de experimentación. Ya está desacoplado de rutas fijas gruesas y cuenta con smoke test. Sigue en fase de hardening; issues y PRs bienvenidos.

---

## Docs

- 📖 [Install](./docs/install.md) — flujo completo con wrapper + plugin install
- 🏗 [Architecture](./docs/architecture.md) — modelo de datos y decisiones
- 🧩 [Dialogue Handoff plugin](./docs/dialogue-handoff.md) — cómo funciona la auto-inyección
- 🔌 [Providers](./docs/providers.md) — embeddings (NVIDIA / Google / local)
- 📚 [Curation Pipeline](./docs/curation-pipeline.md) — workflow de curación

---

## Contribuir

PRs y issues son bienvenidos. Antes de contribuir:

1. Corré el smoke test: `./scripts/smoke-test.sh` debe pasar.
2. Si agregás un script nuevo, asegurate que funcione via el wrapper `./scripts/hmk`.
3. Si cambiás el plugin, testealo manual contra Hermes Agent (ver [docs/dialogue-handoff.md](docs/dialogue-handoff.md)).

No hay CI todavía — el smoke test local es la barrera.

---

## Licencia

[MIT](LICENSE)

---

<p align="center">
  <sub>Construido como extracción portable de una memorioteca real.<br>
  Si te sirve, una ⭐ ayuda a que otros la encuentren.</sub>
</p>
