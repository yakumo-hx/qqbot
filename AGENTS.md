# AGENTS.md

High-signal notes for OpenCode sessions.

## Run

```powershell
# Main bot (WS server :8080, NapCat connects in). venv + UTF-8 flag are mandatory.
cd E:\QQbot && $env:PYTHONIOENCODING="utf-8"; & ".\venv\Scripts\python" reverse_bot.py

# Private companion 0.0.1 (WS server :8081; NapCat second client). Independent process.
cd E:\QQbot && $env:PYTHONIOENCODING="utf-8"; & ".\venv\Scripts\python" private_bot.py

# Verify a change didn't break imports (no test runner — this is the main check):
cd E:\QQbot && $env:PYTHONIOENCODING="utf-8"; & ".\venv\Scripts\python" -c "import reverse_bot; print('OK')"
cd E:\QQbot && $env:PYTHONIOENCODING="utf-8"; & ".\venv\Scripts\python" -c "import private_bot; print('OK')"
```

NapCat must already be running (`E:\QQbot\NapCat\NapCat.44498.Shell\NapCatWinBootMain.exe`). Windows-only deployment; paths use `\` and `.exe`.

## Architecture (post v2.3.2 refactor, current release v2.5.0)

`scripts/command_handler.py` is now a **~72-line facade** — all 指令 logic lives in `scripts/commands/`:

- `_state.py` — shared globals, locks, constants, `init_handlers()`. Sub-modules access via `from . import _state` (attribute access, so `init_handlers` reassignment is visible).
- `_registry.py` — `@register(name, match_type, pattern)` decorator + `dispatch(ctx)`. Match types: `exact` / `prefix` (requires trailing space) / `substring` / `regex` (handler gets match obj) / `custom` (handler gets pattern's return value).
- 13 feature modules (cipher, daily, menu, yau, zhouli, pixelart, images, divination, piano, character, system, help, interaction), plus `_helpers.py`.
- `voice.py` was **disabled on 2026-10-15** (voice→text never worked: NapCat's `onebot11` has `httpServers: []`, so the 6099 WebUI 404s every OneBot action). It is now an inert tombstone — **do not import it**; nothing references it. The file itself could not be deleted from the sandbox (`scripts/` is Medium-integrity, sandboxed pwsh is Low); remove it by hand from a normal console if you want it gone.
- `commands/__init__.py` imports all modules to trigger registration. Import order = dispatch priority for same-`priority` specs.

Commands live in `scripts/commands/{cipher,daily,menu,yau,zhouli,pixelart,images,divination,piano,character,system,help}.py` and `_helpers.py` (`call_deepseek`, now routed to the pool). Total registered commands: **33**.

`reverse_bot.py` imports public symbols from `command_handler.py` (the facade re-exports them). **Do not break these re-exports**: `handle_command`, `_mystery_queue`, `_dedup_lock`, `BOT_QQ`, `VERSION`, `init_handlers`, `processed_msg_ids`, `PROCESSED_MSG_MAX`, `known_groups`, `find_valid_number`.

Adding a command: create/edit a module in `commands/`, decorate with `@register`, ensure the module is imported in `commands/__init__.py`. No need to touch the facade or any dispatch chain.

## Private companion 0.0.1 (independent, port 8081)

`private_bot.py` is a **separate process** on `:8081`; it does not import `reverse_bot.py` or `decision_engine.py` and does not handle groups. It only serves the whitelist in `config/private_chat.json` (`784427550` by default). See `docs/PRIVATE_CHAT.md`.

- `scripts/private_chat/` — `engine.py` (facade/event entry/background services), `runtime.py` (per-uid ordered queue, revision, debounce, cancellation, proactive arbitration), `dialogue_state.py` (derived dialogue state), `behavior.py` (pure BehaviorPolicy), `response.py` (one model call + structured bubbles), `sender.py` (legacy low-level send + TurnSender priority/receipt engine), `engine_legacy.py` (old reply chain, only for `reply.pipeline="legacy"`), `store.py` (profile/memories/self_state/sessions/attempts/dialogue_state), `activity.py` (current activity/schedule from LLM chat extraction), `persona.py` (compiled private persona card + prompt blocks), `style.py` (RhythmPlan), `image_vision.py` (incoming image vision, group-aligned), `rpc.py` (echo-based OneBot action RPC with ok/failed/unknown), `search.py` (search_gate/web_search wrapper), `corrections.py` (ported correction layer), `llm.py` (chat/chat_raw/chat_json + separate circuit breakers), `proactive.py` (3~5/day gates + hook dedupe), `config.py`.
- `config/private_chat.json` — port/whitelist/character/memory/image/history/search/activity/proactive/reply.pipeline. `reply.pipeline` is `structured` by default; `runtime` is the plain-text runtime fallback and `legacy` is the isolated old chain. Private persona is completely independent: `config/characters/大肥鱼/private/{identity.md,voice.md,relationship.json,corrections.json}`; the group `persona.txt` is NOT read by private chat.
- `data/private/<uid>/` — permanent JSON/JSONL, gitignored. `retention_days=0` means no deletion. New derived files: `dialogue_state.json` (cache, rebuildable) and `attempts.jsonl` (send attempt ledger; unknown results never auto-resend).
- `scripts/test_private_chat.py` + `scripts/test_private_dialogue.py` + `scripts/test_private_runtime.py` — 107 offline unittests including `test_proactive_limit` (`python -m unittest scripts.test_private_chat scripts.test_private_dialogue scripts.test_private_runtime scripts.test_proactive_limit`).
- `llm_pool.chat` gained an optional `budget=` parameter (default unchanged) so private chat caps total latency at 45s for chat and 90s for memory JSON.
- NapCat's `onebot11_2668851638.json` has a second WS client `ws://127.0.0.1:8081`; restart NapCat after editing.

Private chat is **0.0.1**, intentionally independent of the group bot's 2.5.0 versioning.

**Disabled by default since 2026-10-25.** `tools/bot_launcher.ps1` has `$EnablePrivateChat = $false`: the launcher only starts and verifies `reverse_bot.py` (:8080), skips `private_bot.py`, and no longer requires an 8081 OneBot endpoint. LLBot's `ws://127.0.0.1:8081` ws-reverse is also `enable: false`. Re-enabling needs both places changed. The private-chat code and `data/private/` are untouched.

## Gotchas

- **`VS_ENABLED` flag is a trap.** `scripts/command_handler.py` still has `VS_ENABLED = False` guarding a `from scripts.virtual_stock import ...` block, but `scripts/virtual_stock/` was removed from the working copy. Flipping it to `True` will `ImportError`. The virtual stock module lives only in git history (`git show HEAD:scripts/virtual_stock/`).
- **`group_features.json` uses whitelist semantics**: unconfigured groups get *all features OFF* (not on). `_feature_on()` returns False for missing groups.
- **Subprocess model scripts** (`model_primary.py`, `model_fallback.py`) do **not** call `load_dotenv()` — they inherit the parent process env. Running them directly for debugging will get empty API keys.
- **`archive/` is gitignored.** Moving a file there = deleting it from git's view. Do not `git add` from inside `archive/`.
- **`*.backup` files are gitignored** but `*.backup_refactor` is not — clean up temp backups manually.
- **No pytest.** Group-bot `scripts/test_*.py` are mostly exploratory/debug scripts; the private-chat suites (`test_private_chat.py`, `test_private_dialogue.py`, `test_private_runtime.py`) are real offline unittest suites. Verify group changes by import self-check and targeted dispatch tests.

## API Keys (`.env`)

Loaded once by `load_dotenv()` in `reverse_bot.py`; `llm_pool.py` also loads it with `override=False`.

**The single model egress is `scripts/llm_pool.py` → local gateway `http://127.0.0.1:8090/v1`.** No cloud fallback is kept (deliberate decision — see `qq_native_re/decisions/007_model_gateway_migration.md`).

| Env var | Used by |
|---------|---------|
| `LLM_POOL_BASE_URL` | gateway endpoint (default `http://127.0.0.1:8090/v1`) |
| `LLM_POOL_API_KEY` | gateway key — every call site ultimately resolves to this |
| `DECISION_API_KEY` / `DEEPSEEK_API_KEY` / `DIVINATION_API_KEY` | legacy cloud keys, **kept but unused**, as rollback anchors |

`.env` is gitignored; see `.env.example` for template. Never put a real key in a tracked file.

## Config files (`config/`)

| File | Written by | Read by |
|------|-----------|---------|
| `group_features.json` | `scripts/group_manager.py` (CLI) | `reverse_bot.py` (runtime, mtime-cached) |
| `group_characters.json` | `decision_engine` assign/unassign | `decision_engine` |
| `active_character.json` | `decision_engine`, `memory_manager.py` | `decision_engine`, `command_handler` |
| `active_model.json` | `decision_engine.set_active_model` | `llm_pool` + `decision_engine` — **only the `model` field**; URL/key always come from the pool config |
| `model_pool.json` | manual edit (mtime hot-reload) | `llm_pool._load_config` — priority tiers, timeouts, cooldowns, `min_max_tokens` |
| `decision_rules.json` | manual edit | `decision_engine._load_config` |
| `private_chat.json` | manual edit | `private_bot.py` / `scripts.private_chat` — port, whitelist, character, memory, image, history, search, proactive |
| `characters/*/persona.txt` | `memory_manager.py`, `@bot 设置人设` | `decision_engine._load_character` |

## Versioning

`VERSION` lives in `scripts/commands/_state.py` (was in `command_handler.py` pre-v2.3.2). Format: `主版本.次版本.修订号`. Group bot current: `2.5.0`. Git tags follow `vX.Y.Z`. The private companion is a separate product line: `scripts/private_chat/config.py` → `VERSION = "0.0.1"`.

## Git / pushing from this repo

`.git` is **not writable by non-elevated processes** on this machine (its ACL grants write only to `Administrators`), so `git add/commit/push` require an elevated console.

The machine's proxy on `127.0.0.1:7890` is usually not running; `.gitconfig` still points at it, so any network git command fails. Two working options:

```powershell
# A) use the proxy that is actually listening (:15715), with the OpenSSL TLS backend
#    (the schannel backend needs Windows credential-store access that sandboxed
#     processes do not have -> SEC_E_NO_CREDENTIALS)
git -c http.sslBackend=openssl -c http.proxy=http://127.0.0.1:15715 -c https.proxy=http://127.0.0.1:15715 ls-remote --heads origin

# B) clear the dead proxy permanently, then push normally
git config --unset http.proxy ; git config --unset https.proxy
```

`sync_to_github.ps1` (repo root, untracked local helper) performs the whole sync: hygiene commit, model-pool commit, character/vision commit, docs commit, smoke check, push and `v2.4.0` tag.
