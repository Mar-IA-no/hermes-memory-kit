# Dialogue Handoff Plugin

Auto-injects conversational continuity context into Hermes Agent at the start of every new session, so the user doesn't have to rely on "magic phrases" or manually ask the agent to look things up.

## What it does

The kit ships a user-space plugin at `templates/plugins/dialogue-handoff/` that registers **two hooks** in Hermes Agent's plugin system:

### `post_llm_call` — writes the handoff

After every non-trivial user turn (not slash-commands, not <3 char messages), the plugin writes a summary of the turn to `agent-memory/state/DIALOGUE-HANDOFF.md` (perms 600). The summary includes:

- platform, session_id, timestamp, model
- session_path (pointer to the full session JSON under `$HERMES_HOME/sessions/`)
- last user message (first line, 300 chars)
- last assistant response (first line, 300 chars)
- working set — paths extracted from `read_file`, `write_file`, `patch`, `terminal`, `execute_code` tool calls
- resume hint — first sentence of the assistant's response

### `pre_llm_call` — injects the handoff

On the **first turn of every new session** (`is_first_turn=True`), the plugin reads the handoff file + the linked session JSON and injects a tiered-compressed continuity block into the user message (never into the system prompt, to preserve prompt cache prefix).

**Tiered compression strategy**:

| Tier | Scope | Verbosity | Rationale |
|---|---|---|---|
| 1 | last 2 exchanges | verbatim, 300 chars/msg | immediate recency |
| 2 | exchanges 3-6 | headline, 150 chars/msg | arc awareness |
| 3 | exchanges 7-20 | stride 1-of-3, 80 chars/msg | sparse older context |
| 4 | older than 20 | dropped | avoid noise |

Budget: 6000 chars hard-cap. Position-aware: older sparse first, most recent verbatim last (beats "lost-in-the-middle" attention decay).

### Gates (when NOT to inject)

- `is_first_turn=False` — never inject on subsequent turns of the same session
- user_message starts with `/` — commands should not trigger context injection
- handoff file missing or has placeholder content (first install)
- handoff timestamp > 24h old (stale)

## Install in Hermes Agent

The plugin is optional. The Hermes Memory Kit works standalone without Hermes Agent. If you install Hermes Agent on top of the kit's workspace, these steps wire up the plugin:

### 1. Copy plugin into your Hermes home

```bash
# From your workspace root:
cp -r plugins/dialogue-handoff "$HERMES_HOME/plugins/"
```

### 2. Opt in via config

Edit `$HERMES_HOME/config.yaml` and add (or extend):

```yaml
plugins:
  enabled:
    - dialogue-handoff
```

### 3. Wire up environment variables

**Critical**: the plugin reads paths via env vars. If Hermes runs under systemd, add them to the service unit:

```ini
# ~/.config/systemd/user/hermes-gateway.service
[Service]
Environment="HMK_AGENT_MEMORY_BASE=/path/to/your-workspace/agent-memory"
Environment="HMK_HERMES_HOME=/path/to/hermes-prime/hermes-home"
# HMK_* have precedence over AGENT_MEMORY_BASE/HERMES_HOME legacy
```

Then reload + restart:

```bash
systemctl --user daemon-reload
systemctl --user restart hermes-gateway
```

### 4. Verify

```bash
hermes plugins list | grep dialogue-handoff
# expected: dialogue-handoff   enabled   2.0.0   Conversational continuity ...
```

Generate a real non-trivial turn, then check that the file was written:

```bash
cat /path/to/your-workspace/agent-memory/state/DIALOGUE-HANDOFF.md
```

The "Last User Message" section should contain your latest input.

### 5. Test auto-injection end-to-end

Close the Hermes CLI. Open a new session. Type `continua`. The agent should pick up the thread from the previous session naturally — without asking "what were we doing?".

## Env var cascade (reference)

The plugin resolves paths with this precedence (most-specific first):

| Setting | Order |
|---|---|
| Handoff file | `HMK_DIALOGUE_HANDOFF_PATH` → `HMK_AGENT_MEMORY_BASE/state/DIALOGUE-HANDOFF.md` → `AGENT_MEMORY_BASE/state/DIALOGUE-HANDOFF.md` → fallback default |
| Sessions dir | `HMK_SESSIONS_DIR` → `HMK_HERMES_HOME/sessions` → `HERMES_HOME/sessions` → fallback default |

## Compatibility

**Tested against Hermes Agent v0.10.0** (upstream commit `e710bb1f`, release 2026.4.16).

Requires these plugin hooks exposed by Hermes:

- `pre_llm_call`
- `post_llm_call`
- `on_session_start` (not currently used but declared)

Not tested on earlier Hermes releases. The plugin may load but behavior is undefined if the hooks are absent or have different signatures.

## Troubleshooting

**Handoff file never gets written** → check env vars in the Hermes service (systemd-show `hermes-gateway` → Environment block). If `HMK_AGENT_MEMORY_BASE` isn't set, the plugin falls back to `/home/onairam/agent-memory` (a hardcoded default that likely doesn't match your workspace).

**Plugin runs but injection never fires** → the user is hitting a gate:
- Is it really a new session? `is_first_turn` requires no prior `conversation_history`. If you're resuming a session (`hermes chat -r <id>`), first turn is false.
- Did 24h pass since the handoff was written? It's marked stale.
- Does the user message start with `/`? Commands are gated.

**Session JSON file is corrupted** (e.g., Hermes was killed with SIGTERM during write) → the plugin uses `json.JSONDecoder().raw_decode()` which tolerates trailing binary garbage. If you see `exchanges=[]` in the injected block, the file is past-recoverable by the parser. Solution: move or delete the corrupt file; the next session will produce a clean one.

**Auto-injection works but the agent ignores it** → the model is choosing to re-read things manually (old habit). The library SKILL.md tells Hermes how to consume `dialogue_handoff` vs `meta_context`; make sure the `librarian` skill is loaded or the instructions are reachable.

## Consuming the handoff from other flows

The `scripts/continuityctl.py` tool reads the same file + surrounding memory state into a structured JSON:

```bash
./scripts/hmk continuityctl.py rehydrate --skip-retrieval | jq
```

Returns (among other keys):

```json
{
  "meta_context": {...},        // engineering state (ACTIVE-CONTEXT.md + NOW.md)
  "dialogue_handoff": {...},    // conversational handoff (DIALOGUE-HANDOFF.md)
  "episode_handoff": {...},     // legacy alias (deprecated)
  "state": {...}                 // legacy alias (deprecated)
}
```

Useful as a reorientation primitive for any agent on top of the kit, not just Hermes.

## Architecture notes

The design decision to inject at the USER MESSAGE (not the system prompt) is intentional: it preserves the prompt cache prefix that most providers (Anthropic, OpenAI) charge less for on cache hits. The injection is ephemeral — it never gets persisted to the session DB, so subsequent turns of the same session won't see it replayed. See `Hermes run_agent.py:8858` for the implementation of the hook invocation.
