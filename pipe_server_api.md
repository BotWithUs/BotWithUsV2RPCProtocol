# Pipe Server IPC/RPC API Reference

## Overview

The pipe server provides inter-process communication via Windows Named Pipes with MessagePack-encoded JSON payloads. External processes (Java clients, tooling, scripts) connect to query game state and inject actions.

**Pipe name:** `\\.\pipe\BotWithUs_{PID}`
**Serialization:** MessagePack (JSON model)
**Build flag:** `ENABLE_PIPE_SERVER`
**Namespace:** `rpc`

## Wire Protocol

### Message Framing

Every message (request and response) is framed as:

```
[4 bytes: little-endian uint32 body length] [N bytes: msgpack-encoded JSON body]
```

Maximum body size: 16 MB. Messages exceeding this or with zero length cause disconnection.

### Request Format

```json
{
  "method": "method_name",
  "id": 1,
  "params": { ... }
}
```

| Field    | Type   | Required | Description                        |
|----------|--------|----------|------------------------------------|
| `method` | string | yes      | RPC method name                    |
| `id`     | uint32 | yes      | Request ID, echoed in response     |
| `params` | object | no       | Method-specific parameters         |

The server injects `_client_id` into params before dispatching. Handlers can use this for client-specific operations (e.g., event subscriptions).

### Response Format

**Success:**
```json
{
  "id": 1,
  "result": { ... }
}
```

**Error:**
```json
{
  "id": 1,
  "error": "error message"
}
```

### Event Format (server-pushed)

```json
{
  "event": "event_name",
  "data": { ... }
}
```

Events are only sent to clients that have subscribed via `rpc.subscribe`.

---

## Transport Details

| Property              | Value                             |
|-----------------------|-----------------------------------|
| Pipe type             | `PIPE_TYPE_BYTE`, `PIPE_WAIT`     |
| Buffer size           | 65,536 bytes per direction        |
| Max clients           | Unlimited (`PIPE_UNLIMITED_INSTANCES`) |
| I/O model             | IOCP with 2 worker threads        |
| Client IDs            | Auto-incrementing uint32 from 1   |

### Connection Lifecycle

1. Client connects to `\\.\pipe\BotWithUs_{PID}`
2. Server assigns a unique client ID
3. Server begins reading 4-byte header frames
4. Header decoded → body read → `processMessage()` → response sent
5. Loop continues until client disconnects or error occurs
6. On disconnect, all event subscriptions are cleaned up

---

## RPC Methods

### System

#### `rpc.ping`
Connectivity check.

**Params:** none
**Returns:** `{"pong": true}`

#### `rpc.list_methods`
List all registered RPC method names.

**Params:** none
**Returns:** `["method_name", ...]`

#### `rpc.subscribe`
Subscribe to server-pushed events.

**Params:**

| Field   | Type   | Required | Description        |
|---------|--------|----------|--------------------|
| `event` | string | yes      | Event name         |

**Returns:** `{"subscribed": "event_name"}`

#### `rpc.unsubscribe`
Unsubscribe from an event.

**Params:**

| Field   | Type   | Required | Description        |
|---------|--------|----------|--------------------|
| `event` | string | yes      | Event name         |

**Returns:** `{"unsubscribed": "event_name"}`

#### `rpc.client_count`
Get number of connected clients.

**Params:** none
**Returns:** `{"count": 3}`

#### `rpc.list_events`
List all available event names that can be subscribed to.

**Params:** none
**Returns:** `["tick", "login_state_change", "var_change", "varbit_change", ...]`

#### `rpc.get_subscriptions`
Get the calling client's current event subscriptions.

**Params:** none (uses implicit `_client_id`)
**Returns:** `["tick", "var_change"]`

---

### Events

Events are server-pushed messages sent to subscribed clients. Subscribe with `rpc.subscribe`, unsubscribe with `rpc.unsubscribe`. Events have no `id` field — they use `{"event": "name", "data": {...}}` format.

#### `tick`
Fired each game tick when the server tick counter advances.

**Data:**

| Field  | Type  | Description              |
|--------|-------|--------------------------|
| `tick` | int32 | Server tick counter value |

#### `login_state_change`
Fired when the game client state changes (login screen, lobby, in-game, world hopping, etc.).

**Data:**

| Field       | Type | Description                     |
|-------------|------|---------------------------------|
| `old_state` | int  | Previous client state value     |
| `new_state` | int  | New client state value          |

Common states: `10` = lobby, `20` = loading, `30` = in-game.

#### `var_change`
Fired when a player variable (varp) changes value.

**Data:**

| Field       | Type | Description          |
|-------------|------|----------------------|
| `var_id`    | int  | Varp ID that changed |
| `old_value` | int  | Previous value       |
| `new_value` | int  | New value            |

#### `varbit_change`
Fired when a varbit changes value.

**Data:**

| Field       | Type | Description            |
|-------------|------|------------------------|
| `var_id`    | int  | Varbit ID that changed |
| `old_value` | int  | Previous value         |
| `new_value` | int  | New value              |

#### `key_input`
Fired when a key is pressed while ImGui has keyboard focus (overlay active).

**Data:**

| Field      | Type | Description                    |
|------------|------|--------------------------------|
| `key`      | int  | Virtual key code (VK_*)        |
| `is_alt`   | bool | ALT modifier held              |
| `is_ctrl`  | bool | CTRL modifier held             |
| `is_shift` | bool | SHIFT modifier held            |

#### `action_executed`
Fired after each queued bot action is executed on the game thread.

**Data:**

| Field       | Type | Description    |
|-------------|------|----------------|
| `action_id` | int  | Action type ID |
| `param1`    | int  | First param    |
| `param2`    | int  | Second param   |
| `param3`    | int  | Third param    |

#### `break_started`
Fired when a break begins (logout triggered by humanizer fatigue/risk or manual schedule).

**Data:**

| Field              | Type   | Description                        |
|--------------------|--------|------------------------------------|
| `duration_seconds` | int    | Scheduled break duration           |
| `fatigue`          | double | Fatigue level at break start [0,1] |
| `risk`             | double | Cumulative risk at break start     |

#### `break_ended`
Fired when a break countdown completes and the bot resumes.

**Data:** `{}` (empty)

---

### Actions

#### `queue_action`
Queue a single game action.

**Params:**

| Field       | Type | Required | Default | Description    |
|-------------|------|----------|---------|----------------|
| `action_id` | int  | yes      |         | Action type ID |
| `param1`    | int  | no       | 0       | First param    |
| `param2`    | int  | no       | 0       | Second param   |
| `param3`    | int  | no       | 0       | Third param    |

**Returns:** `{"ok": true}`

#### `queue_actions`
Queue multiple game actions atomically.

**Params:**

| Field     | Type  | Required | Description                              |
|-----------|-------|----------|------------------------------------------|
| `actions` | array | yes      | Array of `{action_id, param1, param2, param3}` |

**Returns:** `{"queued": 3}`

#### `get_action_queue_size`
Get pending action queue length.

**Params:** none
**Returns:** `{"size": 2}`

#### `clear_action_queue`
Clear all pending actions.

**Params:** none
**Returns:** `{"ok": true}`

#### `get_action_history`
Get recent action execution history.

**Params:**

| Field              | Type | Required | Default | Description                   |
|--------------------|------|----------|---------|-------------------------------|
| `max_results`      | int  | no       | 50      | Max entries to return         |
| `action_id_filter` | int  | no       | -1      | Filter by action ID (-1=all)  |

**Returns:**
```json
[
  {
    "action_id": 5,
    "param1": 100,
    "param2": 200,
    "param3": 0,
    "timestamp": 1709573400000,
    "delta": 600
  }
]
```

#### `get_last_action_time`
Get timestamp of last executed action.

**Params:** none
**Returns:** `{"timestamp": 1709573400000}`

#### `are_actions_blocked`
Check if action execution is blocked.

**Params:** none
**Returns:** `{"blocked": false}`

#### `set_actions_blocked`
Block or unblock action execution.

**Params:**

| Field     | Type | Required | Description              |
|-----------|------|----------|--------------------------|
| `blocked` | bool | yes      | Whether to block actions |

**Returns:** `{"ok": true}`

---

### Entity Queries

#### `query_entities`
Query game entities with filters. Returns an array of entity summaries.

**Params:**

| Field              | Type   | Required | Default      | Description                                          |
|--------------------|--------|----------|--------------|------------------------------------------------------|
| `type`             | string | no       | `"npc"`      | Entity type: `"npc"`, `"player"`, `"location"`, `"obj_stack"` |
| `type_id`          | int    | no       | -1 (any)     | Filter by type/config ID                             |
| `name_hash`        | uint32 | no       | 0 (any)      | Filter by name hash                                  |
| `name_pattern`     | string | no       |              | Text pattern to match entity name                    |
| `match_type`       | string | no       | `"contains"` | `"exact"`, `"contains"`, `"prefix"`, `"suffix"`, `"regex"` |
| `case_sensitive`   | bool   | no       | false        | Case-sensitive name matching                         |
| `plane`            | int    | no       | -1 (any)     | Filter by game plane                                 |
| `tile_x`           | int    | no       | 0            | Center X for radius query                            |
| `tile_y`           | int    | no       | 0            | Center Y for radius query                            |
| `radius`           | int    | no       | 0 (disabled) | Tile radius from center                              |
| `visible_only`     | bool   | no       | false        | Only visible entities                                |
| `moving_only`      | bool   | no       | false        | Only moving entities                                 |
| `stationary_only`  | bool   | no       | false        | Only stationary entities                             |
| `in_combat`        | bool   | no       | false        | Only entities in combat                              |
| `not_in_combat`    | bool   | no       | false        | Only entities not in combat                          |
| `sort_by_distance` | bool   | no       | false        | Sort results by distance (requires `tile_x`/`tile_y`) |
| `max_results`      | int    | no       | unlimited    | Limit result count                                   |

**Returns (NPC/Player):**
```json
[
  {
    "handle": 16777217,
    "server_index": 42,
    "type_id": 3010,
    "tile_x": 3200,
    "tile_y": 3200,
    "tile_z": 0,
    "name_hash": 123456,
    "name": "Guard",
    "is_moving": false,
    "is_hidden": false
  }
]
```

**Returns (Location):**
```json
[
  {
    "handle": 33554433,
    "type_id": 1234,
    "tile_x": 3200,
    "tile_y": 3200,
    "tile_z": 0,
    "name_hash": 789012
  }
]
```

**Returns (ObjStack):**
```json
[
  {
    "handle": 50331649,
    "tile_x": 3200,
    "tile_y": 3200,
    "tile_z": 0
  }
]
```

#### `get_entity_info`
Get detailed info for a pathing entity (NPC or Player) by handle.

**Params:**

| Field    | Type   | Required | Description   |
|----------|--------|----------|---------------|
| `handle` | uint32 | yes      | Entity handle |

**Returns:**
```json
{
  "handle": 16777217,
  "server_index": 42,
  "type_id": 3010,
  "tile_x": 3200,
  "tile_y": 3200,
  "tile_z": 0,
  "name": "Guard",
  "name_hash": 123456,
  "is_moving": false,
  "is_hidden": false,
  "animation_id": -1,
  "stance_id": 808,
  "health": 100,
  "max_health": 100,
  "following_index": -1,
  "overhead_text": "",
  "combat_level": 21
}
```

#### `get_entity_name`
**Params:** `handle: uint32`
**Returns:** `{"name": "Guard"}`

#### `get_entity_health`
**Params:** `handle: uint32`
**Returns:** `{"health": 80, "max_health": 100}`

#### `get_entity_position`
**Params:** `handle: uint32`
**Returns:** `{"tile_x": 3200, "tile_y": 3200, "plane": 0}`

#### `is_entity_valid`
**Params:** `handle: uint32`
**Returns:** `{"valid": true}`

#### `get_entity_hitmarks`
Get active hitmarks (damage splats) on an entity.

**Params:** `handle: uint32`
**Returns:**
```json
[
  {"damage": 15, "type": 1, "cycle": 42000}
]
```

#### `get_entity_animation`
**Params:** `handle: uint32`
**Returns:** `{"animation_id": 808}`

#### `get_entity_overhead_text`
Get overhead text for a pathing entity (NPC or Player) by handle.

**Params:** `handle: uint32`
**Returns:** `{"text": "Hello world"}` or `{"text": ""}`

#### `get_animation_length`
Get raw animation data length from cache.

**Params:**

| Field          | Type | Required | Description        |
|----------------|------|----------|--------------------|
| `animation_id` | int  | yes      | Animation config ID |

**Returns:** `{"length": 128}`

#### `query_ground_items`
Query ground item stacks. Accepts the same entity filters as `query_entities` (radius, plane, etc.).

**Returns:**
```json
[
  {
    "handle": 50331649,
    "tile_x": 3200,
    "tile_y": 3200,
    "tile_z": 0,
    "items": [
      {"item_id": 995, "quantity": 100},
      {"item_id": 526, "quantity": 1}
    ]
  }
]
```

#### `get_obj_stack_items`
Get items in a specific ground stack by handle.

**Params:** `handle: uint32`
**Returns:**
```json
[
  {"item_id": 995, "quantity": 100}
]
```

#### `query_obj_stacks`
Query ground stacks (without inline items). Same filters as `query_entities`.

**Returns:** Array of `{handle, tile_x, tile_y, tile_z}`

#### `query_projectiles`
Query active projectiles.

**Params:**

| Field           | Type | Required | Default  | Description                |
|-----------------|------|----------|----------|----------------------------|
| `projectile_id` | int  | no       | -1 (any) | Filter by projectile ID   |
| `plane`         | int  | no       | -1 (any) | Filter by plane            |
| `max_results`   | int  | no       | unlimited| Limit results              |

**Returns:**
```json
[
  {
    "handle": 67108865,
    "projectile_id": 1181,
    "start_x": 3200,
    "start_y": 3200,
    "end_x": 3210,
    "end_y": 3210,
    "plane": 0,
    "target_index": 5,
    "source_index": 0,
    "start_cycle": 42000,
    "end_cycle": 42030
  }
]
```

#### `query_spot_anims`
Query active spot animations (graphics).

**Params:**

| Field         | Type | Required | Default  | Description             |
|---------------|------|----------|----------|-------------------------|
| `anim_id`     | int  | no       | -1 (any) | Filter by animation ID  |
| `plane`       | int  | no       | -1 (any) | Filter by plane         |
| `max_results` | int  | no       | unlimited| Limit results           |

**Returns:**
```json
[
  {
    "handle": 83886081,
    "anim_id": 2187,
    "tile_x": 3200,
    "tile_y": 3200,
    "tile_z": 0
  }
]
```

#### `query_hint_arrows`
Query active hint arrows.

**Params:**

| Field         | Type | Required | Default   | Description    |
|---------------|------|----------|-----------|----------------|
| `max_results` | int  | no       | unlimited | Limit results  |

**Returns:**
```json
[
  {
    "handle": 100663297,
    "type": 1,
    "tile_x": 3200,
    "tile_y": 3200,
    "tile_z": 0,
    "target_index": 5
  }
]
```

#### `query_worlds`
Query all available game worlds.

**Params:**

| Field              | Type | Required | Default | Description                   |
|--------------------|------|----------|---------|-------------------------------|
| `include_activity` | bool | no       | false   | Include activity description  |

**Returns:**
```json
[
  {
    "world_id": 301,
    "properties": 536870912,
    "population": 423,
    "ping": 45,
    "activity": "Mining and Smithing"
  }
]
```

#### `get_current_world`
**Params:** none
**Returns:** `{"world_id": 301}`

#### `compute_name_hash`
Compute the name hash for a string (useful for filtering by name_hash).

**Params:**

| Field  | Type   | Required | Description       |
|--------|--------|----------|-------------------|
| `name` | string | yes      | Name to hash      |

**Returns:** `{"hash": 123456}`

#### `update_query_context`
Force an immediate cache update of the query context.

**Params:** none
**Returns:** `{"ok": true}`

#### `invalidate_query_context`
Invalidate the query context cache, forcing a rebuild on next query.

**Params:** none
**Returns:** `{"ok": true}`

---

### Components & Interfaces

#### `query_components`
Query UI components with filters.

**Params:**

| Field               | Type   | Required | Default      | Description                         |
|---------------------|--------|----------|--------------|-------------------------------------|
| `interface_id`      | int    | no       | -1 (any)     | Filter by interface ID              |
| `item_id`           | int    | no       | -1 (any)     | Filter by held item ID              |
| `sprite_id`         | int    | no       | -1 (any)     | Filter by sprite ID                 |
| `type`              | int    | no       | -1 (any)     | Filter by component type            |
| `text_pattern`      | string | no       |              | Text content pattern                |
| `match_type`        | string | no       | `"contains"` | `"exact"`, `"contains"`, `"regex"`  |
| `case_sensitive`    | bool   | no       | false        | Case-sensitive text matching         |
| `option_pattern`    | string | no       |              | Right-click option pattern           |
| `option_match_type` | string | no       | `"contains"` | `"exact"`, `"contains"`             |
| `visible_only`      | bool   | no       | false        | Only visible components              |
| `max_results`       | int    | no       | unlimited    | Limit results                        |

**Returns:**
```json
[
  {
    "handle": 12345678,
    "interface_id": 1473,
    "component_id": 5,
    "sub_component_id": -1,
    "type": 5,
    "item_id": 995,
    "item_count": 100,
    "sprite_id": -1
  }
]
```

#### `is_component_valid`
Check if a component exists and is valid.

**Params:**

| Field              | Type | Required | Default | Description              |
|--------------------|------|----------|---------|--------------------------|
| `interface_id`     | int  | yes      |         | Interface ID             |
| `component_id`     | int  | yes      |         | Component ID             |
| `sub_component_id` | int  | no       | -1      | Sub-component index      |

**Returns:** `{"valid": true}`

#### `get_component_text`
**Params:** `interface_id: int`, `component_id: int`
**Returns:** `{"text": "Attack"}` or `{"text": null}`

#### `get_component_item`
Get the item held by a component.

**Params:**

| Field              | Type | Required | Default | Description         |
|--------------------|------|----------|---------|---------------------|
| `interface_id`     | int  | yes      |         | Interface ID        |
| `component_id`     | int  | yes      |         | Component ID        |
| `sub_component_id` | int  | no       | -1      | Sub-component index |

**Returns:** `{"item_id": 995, "count": 100}`

#### `get_component_position`
**Params:** `interface_id: int`, `component_id: int`
**Returns:** `{"x": 100, "y": 200, "width": 32, "height": 32}`

#### `get_component_options`
Get right-click menu options for a component.

**Params:** `interface_id: int`, `component_id: int`
**Returns:** `["Use", "Drop", "Examine"]`

#### `get_component_sprite_id`
**Params:** `interface_id: int`, `component_id: int`
**Returns:** `{"sprite_id": 1234}`

#### `get_component_type`
**Params:** `interface_id: int`, `component_id: int`
**Returns:** `{"type": 5, "type_name": "graphic"}`

#### `get_component_children`
Get all dynamic sub-components of a layer component.

**Params:** `interface_id: int`, `component_id: int`
**Returns:**
```json
[
  {
    "handle": 12345678,
    "interface_id": 1473,
    "component_id": 5,
    "sub_component_id": 0,
    "type": 5,
    "item_id": 995,
    "item_count": 100,
    "sprite_id": -1
  }
]
```

#### `get_component_by_hash`
Get a component handle by interface/component/sub IDs.

**Params:**

| Field              | Type | Required | Default | Description         |
|--------------------|------|----------|---------|---------------------|
| `interface_id`     | int  | yes      |         | Interface ID        |
| `component_id`     | int  | yes      |         | Component ID        |
| `sub_component_id` | int  | no       | -1      | Sub-component index |

**Returns:** `{"handle": 12345678}`

#### `get_open_interfaces`
Get all currently open/visible interfaces.

**Params:** none
**Returns:**
```json
[
  {"parent_hash": 98304, "interface_id": 1473}
]
```

#### `is_interface_open`
**Params:** `interface_id: int`
**Returns:** `{"open": true}`

---

### Game Variables

#### `get_varp`
Read a player variable (varp).

**Params:** `var_id: int`
**Returns:** `{"value": 42}`

#### `get_varbit`
Read a varbit (packed bit field within a varp).

**Params:** `varbit_id: int`
**Returns:** `{"value": 1}`

#### `get_varc_int`
Read a client variable (integer).

**Params:** `varc_id: int`
**Returns:** `{"value": 100}`

#### `get_varc_string`
Read a client variable (string).

**Params:** `varc_id: int`
**Returns:** `{"value": "some text"}`

#### `query_varbits`
Batch-read multiple varbits.

**Params:**

| Field       | Type    | Required | Description            |
|-------------|---------|----------|------------------------|
| `varbit_ids`| int[]   | yes      | Array of varbit IDs    |

**Returns:**
```json
[
  {"varbit_id": 123, "value": 1},
  {"varbit_id": 456, "value": 3}
]
```

---

### Script Execution

#### `get_script_handle`
Create a handle for a client script.

**Params:** `script_id: int`
**Returns:** `{"handle": 140234567890}`

#### `execute_script`
Execute a client script with arguments.

**Params:**

| Field         | Type     | Required | Description                                |
|---------------|----------|----------|--------------------------------------------|
| `handle`      | uintptr  | yes      | Script handle from `get_script_handle`     |
| `int_args`    | int[]    | no       | Integer arguments                          |
| `string_args` | string[] | no       | String arguments                           |
| `returns`     | string[] | no       | Expected return types: `"int"`, `"long"`, `"string"` |

**Returns:**
```json
{
  "returns": [42, "result_text"]
}
```

#### `destroy_script_handle`
Free a script handle.

**Params:** `handle: uintptr`
**Returns:** `{"ok": true}`

#### `fire_key_trigger`
Fire a key input trigger on a component (used for input fields, chat, etc.).

**Params:**

| Field          | Type   | Required | Description          |
|----------------|--------|----------|----------------------|
| `interface_id` | int    | yes      | Interface ID         |
| `component_id` | int    | yes      | Component ID         |
| `input`        | string | yes      | Input text to send   |

**Returns:** `{"ok": true}`

---

### Game State

#### `get_account_info`
Get account and session information for the current client.

**Params:** none
**Returns:**
```json
{
  "client_type": 0,
  "client_state": 10,
  "session_id": "abc123...",
  "ip_hash": 12345678,
  "jx_display_name": "PlayerName",
  "jx_character_id": "char-uuid-here",
  "display_name": "PlayerName",
  "is_member": true,
  "server_index": 42,
  "logged_in": true,
  "login_progress": 40,
  "login_status": 2
}
```

| Field | Type | Description |
|-------|------|-------------|
| `client_type` | int | Client platform: 0 = Jagex launcher, 1 = Steam |
| `client_state` | int | Overall client state (lobby, in-game, etc.) |
| `session_id` | string\|null | Active session ID, null if none |
| `ip_hash` | uint32 | Hashed login identifier |
| `jx_display_name` | string\|null | Display name from Jagex launcher env (`JX_DISPLAY_NAME`), null if not set (e.g. Steam) |
| `jx_character_id` | string\|null | Character ID from launcher env (`JX_CHARACTER_ID`), null if not set |
| `display_name` | string\|null | In-game display name, null if not logged in |
| `is_member` | bool | Whether the account has membership |
| `server_index` | int | Player server slot index, -1 if not logged in |
| `logged_in` | bool | Whether a player is currently logged in |
| `login_progress` | int | Login handshake progress (from LoginManager) |
| `login_status` | int | Login status code (from LoginManager) |

**Notes:**
- `jx_display_name` and `jx_character_id` are read from environment variables set by the Jagex launcher at process start. They are available even before login completes. Steam clients will have these as `null`.
- `display_name` comes from the in-game `LoggedInPlayer` struct and is only available once fully logged in.
- `client_type` can be used to distinguish Steam vs Jagex launcher sessions.

#### `get_local_player`
Get the local player's info without querying the player list.

**Params:** none
**Returns:**
```json
{
  "server_index": 42,
  "name": "PlayerName",
  "tile_x": 3200,
  "tile_y": 3200,
  "plane": 0,
  "is_member": true,
  "is_moving": false,
  "animation_id": -1,
  "stance_id": 808,
  "health": 990,
  "max_health": 990,
  "combat_level": 138,
  "overhead_text": "",
  "target_index": -1,
  "target_type": -1
}
```
**Error:** `{"error": "not_logged_in"}` if no player is logged in.

#### `get_game_cycle`
Get the current game tick counter from the transmission manager.

**Params:** none
**Returns:** `{"cycle": 123456}`

#### `get_login_state`
Get the current login/client state.

**Params:** none
**Returns:**
```json
{
  "state": 10,
  "login_progress": 40,
  "login_status": 2
}
```

#### `get_mini_menu`
Get the current right-click (mini) menu entries.

**Params:** none
**Returns:**
```json
[
  {
    "option_text": "Attack Guard (level 21)",
    "action_id": 9,
    "type_id": 2,
    "item_id": -1,
    "param1": 42,
    "param2": 0,
    "param3": 0
  }
]
```

#### `get_grand_exchange_offers`
Get all Grand Exchange offer slots (3 pages x 8 slots = 24 total).

**Params:** none
**Returns:**
```json
[
  {
    "slot": 0,
    "status": 1,
    "type": 0,
    "item_id": 314,
    "price": 100,
    "count": 50,
    "completed_count": 25,
    "completed_gold": 2500
  }
]
```

#### `get_world_to_screen`
Project a tile coordinate to screen position using the scene projection matrix.

**Params:**

| Field    | Type | Required | Description      |
|----------|------|----------|------------------|
| `tile_x` | int  | yes      | Tile X coordinate |
| `tile_y` | int  | yes      | Tile Y coordinate |

**Returns:** `{"screen_x": 400.0, "screen_y": 300.0}`

#### `batch_world_to_screen`
Batch-convert tile coordinates to screen positions in one call.

**Params:**

| Field   | Type  | Required | Description                             |
|---------|-------|----------|-----------------------------------------|
| `tiles` | array | yes      | Array of `{"x": int, "y": int}` objects |

**Returns:**
```json
{
  "results": [
    {"screen_x": 400.5, "screen_y": 300.2},
    {"screen_x": 410.0, "screen_y": 305.0}
  ]
}
```

#### `get_viewport_info`
Get projection matrix, view matrix, and viewport dimensions from the active scene.

**Params:** none
**Returns:**
```json
{
  "viewport_width": 1920,
  "viewport_height": 1080,
  "projection_matrix": [1.0, 0.0, "... 16 floats row-major"],
  "view_matrix": [1.0, 0.0, "... 16 floats row-major"]
}
```
**Error:** `{"error": "no_active_scene"}` if no scene is loaded.

#### `get_entity_screen_positions`
Get screen positions for multiple entities by handle in one call.

**Params:**

| Field     | Type     | Required | Description                    |
|-----------|----------|----------|--------------------------------|
| `handles` | uint32[] | yes      | Array of entity handles        |

**Returns:**
```json
{
  "results": [
    {"handle": 12345, "screen_x": 500.0, "screen_y": 300.0, "valid": true},
    {"handle": 67890, "screen_x": 0, "screen_y": 0, "valid": false}
  ]
}
```

#### `get_game_window_rect`
Get the game window position and size on screen for external overlay alignment.

**Params:** none
**Returns:**
```json
{
  "x": 100, "y": 50,
  "width": 1920, "height": 1080,
  "client_x": 100, "client_y": 80,
  "client_width": 1920, "client_height": 1040
}
```
**Error:** `{"error": "no_window"}` if the game window handle is not available.

#### `take_screenshot`
Capture a screenshot of the game framebuffer as a PNG image. The capture runs on the OpenGL thread (before overlay rendering) and returns the result to the caller. The image is resized to 1280x720.

**Params:** none
**Returns:** `{"data": <binary PNG>, "size": 65536}`
**Errors:**
- `{"error": "not_ready"}` — bot context not initialized
- `{"error": "gl_not_ready"}` — OpenGL/ImGui not initialized yet
- `{"error": "timeout"}` — GL thread did not capture within 5 seconds

#### `start_stream`
Start continuous JPEG frame streaming over a dedicated one-way named pipe. The server creates a separate outbound-only pipe for frame data so the main RPC pipe remains unaffected. The caller should connect to the returned `pipe_name` to receive frames.

**Params:**

| Field        | Type | Required | Default | Description                              |
|--------------|------|----------|---------|------------------------------------------|
| `frame_skip` | int  | no       | 2       | Capture every Nth frame (1 = every frame) |
| `quality`    | int  | no       | 60      | JPEG quality 1-100                       |
| `width`      | int  | no       | 960     | Output width (clamped 160-1920)          |
| `height`     | int  | no       | 540     | Output height (clamped 90-1080)          |

**Returns:**
```json
{
  "ok": true,
  "pipe_name": "\\\\.\\pipe\\BotWithUs_12345_stream",
  "frame_skip": 2,
  "quality": 60,
  "width": 960,
  "height": 540
}
```

**Errors:**
- `{"error": "not_ready"}` — bot context not initialized
- `{"error": "gl_not_ready"}` — OpenGL/ImGui not initialized yet
- `{"error": "pipe_create_failed"}` — failed to create the stream pipe

**Stream pipe protocol:**

The stream pipe (`\\.\pipe\BotWithUs_{PID}_stream`) is a one-way outbound pipe (`PIPE_ACCESS_OUTBOUND`). Each frame is sent as:

```
[4 bytes: little-endian uint32 JPEG size] [N bytes: raw JPEG data]
```

The stream pipe is separate from the RPC pipe — no msgpack encoding, no event wrapping, just raw length-prefixed JPEG frames. If the previous frame write hasn't completed when a new frame is captured, the new frame is silently dropped (backpressure without blocking the GL thread). The stream automatically stops if the client disconnects from the stream pipe.

**Typical usage:**

1. Connect to the RPC pipe (`\\.\pipe\BotWithUs_{PID}`)
2. Call `start_stream` with desired parameters
3. Connect to the stream pipe returned in `pipe_name`
4. Read frames in a loop: 4-byte header → JPEG body
5. Call `stop_stream` on the RPC pipe when done

#### `stop_stream`
Stop the frame stream and close the stream pipe.

**Params:** none
**Returns:** `{"ok": true}`

#### `get_cache_file`
Read a raw file from the game cache.

**Params:**

| Field        | Type | Required | Default | Description      |
|--------------|------|----------|---------|------------------|
| `index_id`   | int  | yes      |         | Cache index ID   |
| `archive_id` | int  | yes      |         | Archive ID       |
| `file_id`    | int  | no       | 0       | File within archive |

**Returns:** `{"data": <binary>, "size": 1024}`
**Error:** `{"error": "file_not_found"}`

#### `get_cache_file_count`
Get the number of files in a cache index or archive.

**Params:**

| Field        | Type | Required | Default | Description      |
|--------------|------|----------|---------|------------------|
| `index_id`   | int  | yes      |         | Cache index ID   |
| `archive_id` | int  | no       | 0       | Archive ID       |
| `shift`      | int  | no       | 0       | Bit shift        |

**Returns:** `{"count": 256}`

#### `set_world`
Set the target world for login.

**Params:** `world_id: int`
**Returns:** `{"ok": true}`

#### `change_login_state`
Change the login state machine state.

**Params:**

| Field       | Type | Required | Default | Description          |
|-------------|------|----------|---------|----------------------|
| `old_state` | int  | no       | 0       | Expected current state |
| `new_state` | int  | yes      |         | Target state          |

**Returns:** `{"ok": true}`

#### `get_auto_login`
Check if auto login is enabled.

**Params:** none
**Returns:** `{"enabled": true}`

#### `set_auto_login`
Enable or disable auto login.

**Params:**

| Field     | Type | Required | Description                  |
|-----------|------|----------|------------------------------|
| `enabled` | bool | yes      | Whether to enable auto login |

**Returns:** `{"ok": true}`

#### `schedule_break`
Schedule a break (logout pause).

**Params:** `duration: int` (milliseconds)
**Returns:** `{"ok": true}`

#### `interrupt_break`
Cancel a scheduled break.

**Params:** none
**Returns:** `{"ok": true}`

#### `get_navigation_archive`
Get the navigation mesh archive.

**Params:** none
**Returns:** `{"data": <binary>, "size": 65536}`

---

### Config Type Lookups

These methods read game config definitions from the cache by ID. Data is lazily loaded and cached after first access.

#### `get_item_type`
Get item definition by ID.

**Params:** `id: int`
**Returns:**
```json
{
  "id": 4151,
  "name": "Abyssal whip",
  "members": true,
  "stackable": false,
  "shop_price": 120001,
  "ge_buy_limit": 10,
  "category": 150,
  "noted_id": 4152,
  "wearpos": 3,
  "exchangeable": true,
  "ground_options": ["", "", "", "", ""],
  "inventory_options": ["", "Wield", "", "", ""],
  "params": {"14": 4, "118": 7186}
}
```

#### `get_npc_type`
Get NPC definition by ID.

**Params:** `id: int`
**Returns:**
```json
{
  "id": 1,
  "name": "Man",
  "combat_level": 4,
  "visible": true,
  "clickable": true,
  "options": ["Talk to", "Attack", "Pickpocket", "", ""],
  "varbit_id": -1,
  "varp_id": -1,
  "transforms": [],
  "params": {"14": 4, "641": 120}
}
```

#### `get_location_type`
Get location/object definition by ID.

**Params:** `id: int`
**Returns:**
```json
{
  "id": 2,
  "name": "Cave Entrance",
  "size_x": 4,
  "size_y": 4,
  "interact_type": 0,
  "solid_type": 0,
  "members": false,
  "options": ["Enter", "", "", "", ""],
  "varbit_id": -1,
  "varp_id": -1,
  "transforms": [],
  "map_sprite_id": 0,
  "params": {}
}
```

#### `get_enum_type`
Get enum (key-value mapping) definition by ID. Enums map inputs to outputs (e.g., skill IDs to names).

**Params:** `id: int`
**Returns:**
```json
{
  "id": 680,
  "input_type_id": 17,
  "output_type_id": 36,
  "int_default": 0,
  "string_default": "this skill",
  "entry_count": 29,
  "entries": {
    "0": "Attack",
    "1": "Defence",
    "2": "Strength"
  }
}
```

#### `get_struct_type`
Get struct definition by ID. Structs are parameter bags (key-value pairs of int/string).

**Params:** `id: int`
**Returns:**
```json
{
  "id": 1,
  "params": {
    "2533": "Retro Dance & Joy pack",
    "8668": 450
  }
}
```

#### `get_sequence_type`
Get animation sequence definition by ID.

**Params:** `id: int`
**Returns:**
```json
{
  "id": 808,
  "frame_count": 24,
  "frame_lengths": [5, 5, 5, 5],
  "loop_offset": -1,
  "priority": 0,
  "off_hand": -1,
  "main_hand": -1,
  "max_loops": 0,
  "animating_precedence": 0,
  "walking_precedence": 0,
  "replay_mode": 0,
  "tweened": true,
  "params": {}
}
```

#### `get_quest_type`
Get quest definition by ID.

**Params:** `id: int`
**Returns:**
```json
{
  "id": 0,
  "name": "Cabin Fever",
  "list_name": "",
  "category": 0,
  "difficulty": 0,
  "members_only": false,
  "quest_points": 0,
  "quest_point_req": 0,
  "quest_item_sprite": -1,
  "start_locations": [],
  "alternate_start_location": 0,
  "dependent_quest_ids": [],
  "skill_requirements": [{"skill_id": 0, "level": 50}],
  "progress_varps": [{"varp_id": 100, "min": 0, "max": 10}],
  "progress_varbits": [],
  "params": {}
}
```

---

### Inventory & Items

#### `query_inventories`
List all loaded inventories.

**Params:** none
**Returns:**
```json
[
  {"inventory_id": 93, "item_count": 28, "capacity": 28}
]
```

#### `query_inventory_items`
Query items across inventories with filters.

**Params:**

| Field          | Type | Required | Default   | Description                 |
|----------------|------|----------|-----------|-----------------------------|
| `inventory_id` | int  | no       | all       | Filter to specific inventory |
| `item_id`      | int  | no       | -1 (any)  | Filter by item ID           |
| `min_quantity`  | int  | no       | 0         | Minimum quantity             |
| `non_empty`    | bool | no       | true      | Skip empty slots             |
| `max_results`  | int  | no       | unlimited | Limit results                |

**Returns:**
```json
[
  {"handle": 12345, "item_id": 995, "quantity": 1000, "slot": 0}
]
```

#### `get_inventory_item`
Get a specific item by inventory and slot.

**Params:** `inventory_id: int`, `slot: int`
**Returns:** `{"handle": 12345, "item_id": 995, "quantity": 1000, "slot": 0}`

#### `get_item_vars`
Get all item-specific variables for an inventory slot.

**Params:** `inventory_id: int`, `slot: int`
**Returns:**
```json
[
  {"var_id": 683, "value": 100}
]
```

#### `get_item_var_value`
Get a single item variable value.

**Params:** `inventory_id: int`, `slot: int`, `var_id: int`
**Returns:** `{"value": 100}`

#### `is_inventory_item_valid`
Check if a slot contains a valid item.

**Params:** `inventory_id: int`, `slot: int`
**Returns:** `{"valid": true}`

---

### Player Stats

#### `get_player_stats`
Get all player skill stats.

**Params:** none
**Returns:**
```json
[
  {
    "skill_id": 0,
    "level": 99,
    "boosted_level": 99,
    "max_level": 99,
    "xp": 13034431
  }
]
```

#### `get_player_stat`
Get a single skill stat.

**Params:** `skill_id: int`
**Returns:** `{"skill_id": 0, "level": 99, "boosted_level": 99, "max_level": 99, "xp": 13034431}`

#### `get_player_stat_count`
Get the number of skills.

**Params:** none
**Returns:** `{"count": 29}`

---

### Chat

#### `query_chat_history`
Query chat messages with optional type filter.

**Params:**

| Field          | Type | Required | Default  | Description                  |
|----------------|------|----------|----------|------------------------------|
| `message_type` | int  | no       | -1 (all) | Filter by message type       |
| `max_results`  | int  | no       | 50       | Max messages to return       |

**Returns:**
```json
[
  {
    "index": 0,
    "message_type": 0,
    "text": "Hello world",
    "player_name": "Player1"
  }
]
```

#### `get_chat_message_text`
**Params:** `index: int`
**Returns:** `{"text": "Hello world"}`

#### `get_chat_message_player`
**Params:** `index: int`
**Returns:** `{"player_name": "Player1"}`

#### `get_chat_message_type`
**Params:** `index: int`
**Returns:** `{"message_type": 0}`

#### `get_chat_history_size`
**Params:** none
**Returns:** `{"size": 100}`

---

## Common Errors

| Error              | Cause                                           |
|--------------------|--------------------------------------------------|
| `"not_ready"`      | Bot context (`bctx`) not initialized yet         |
| `"invalid_handle"` | Entity handle is stale or doesn't exist          |
| `"not_found"`      | Component, inventory, or resource not found      |
| `"null_handle"`    | Script handle is null                            |
| `"cache_not_ready"`| Game cache not loaded                            |
| `"file_not_found"` | Cache file doesn't exist at given index/archive  |
| `"invalid entity type"` | Unknown entity type string in `query_entities` |

---

## Architecture

### Source Layout

```
pipe_server/src/
├── pipe_server.h/.cpp          # IOCP server singleton
├── pipe_client.h/.cpp          # Per-connection handler
├── method_registry.h/.cpp      # RPC dispatch + builtin methods (subscribe, list_events, etc.)
├── event_bus.h/.cpp            # Pub/sub event system
├── stream_pipe.h/.cpp          # Dedicated one-way pipe for JPEG frame streaming
└── handlers/
    ├── handler_common.h        # Handler registration declarations
    ├── action_handlers.cpp     # Action queue and blocking
    ├── query_handlers.cpp      # Entity queries (NPC, player, location, projectile, etc.)
    ├── component_handlers.cpp  # UI component and interface queries
    ├── var_handlers.cpp        # Varp, varbit, varc reads
    ├── script_handlers.cpp     # Client script execution
    ├── game_handlers.cpp       # Cache, world, login state, auto login, navigation, streaming
    ├── inventory_handlers.cpp  # Inventories, items, stats, chat
    └── config_handlers.cpp     # Config type lookups (item, npc, location, enum, struct, sequence, quest)
```

### Event Publishing Locations

| Event                | Published From                              | Thread     |
|----------------------|---------------------------------------------|------------|
| `tick`               | `jagex_hooks.cpp` (hookedClientMainLogic)   | Game       |
| `login_state_change` | `jagex_hooks.cpp` (hookedClientMainLogic)   | Game       |
| `action_executed`    | `jagex_hooks.cpp` (hookedClientMainLogic)   | Game       |
| `var_change`         | `vars_module.cpp` (VarsModule::process)     | Game       |
| `varbit_change`      | `vars_module.cpp` (VarsModule::process)     | Game       |
| `key_input`          | `bot_context.cpp` (wndProc)                 | Window     |
| `break_started`      | `break_module.cpp` (BreakModule::process)   | Game       |
| `break_ended`        | `break_module.cpp` (BreakModule::process)   | Game       |

### Thread Safety

| Resource            | Lock                                     | Scope          |
|---------------------|------------------------------------------|----------------|
| Client map          | `SRWLOCK` (exclusive add/remove, shared get) | PipeServer     |
| Write queue         | `SRWLOCK` exclusive per client           | PipeClient     |
| Event subscriptions | `SRWLOCK` (exclusive write, shared read) | EventBus       |
| Query context       | `SharedSRWLock` for reads                | Game state     |
| Action queue        | `CriticalSectionLock` (syntheticActionSection) | BotActions |
| Action history      | `CriticalSectionLock` (actionHistorySection) | BotActions |
| Stream pipe writes  | Atomic `writeBusy_` flag (lock-free)     | StreamPipe     |

### Handle System

Entity handles are 32-bit values encoding index and generation:
- **24 bits**: entity index
- **8 bits**: generation counter (detects stale references)

Handles are only valid for the current cache tick. Call `is_entity_valid` to check before use.
