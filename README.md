# BotWithUs MCP Server

MCP (Model Context Protocol) server that bridges AI assistants like Claude Code to the game via the pipe server. Exposes game state queries, actions, and cache lookups as MCP tools.

## Prerequisites

- Python 3.10+
- Game running with `agentcpp` injected (creates `\\.\pipe\BotWithUs_{PID}`)

## Installation

```bash
cd mcp_server
pip install -r requirements.txt
```

Dependencies: `mcp[cli]>=1.0.0`, `msgpack>=1.0.0`, `pywin32>=306`

## Claude Code Configuration

Add to your Claude Code MCP config (`.claude.json`, `claude_desktop_config.json`, or via `claude mcp add`):

```json
{
  "mcpServers": {
    "botwithus": {
      "command": "python",
      "args": ["C:/path/to/BotWithUs2/mcp_server/server.py"]
    }
  }
}
```

To target a specific game instance when multiple are running:

```json
{
  "args": ["C:/path/to/BotWithUs2/mcp_server/server.py", "--pid", "12345"]
}
```

## Running Standalone

```bash
# Auto-discover (fails if multiple instances running)
python server.py

# Target specific PID
python server.py --pid 12345
```

## Connection Behavior

The server discovers pipes matching `\\.\pipe\BotWithUs_*` on startup. With exactly one game instance it connects automatically. With multiple instances, `--pid` is required. The pipe connection is established lazily on the first tool call and reconnects automatically if dropped.

## Available Tools

### Entity Queries

| Tool | Description |
|------|-------------|
| `query_npcs` | Find NPCs by type, name, radius, combat state, movement |
| `query_players` | Find players with same filters |
| `query_locations` | Find game objects/locations |
| `query_ground_items` | Find ground item stacks (returns items inline) |
| `query_entities` | Generic query for any entity type |
| `query_obj_stacks` | Find ground stacks (handles only) |
| `query_projectiles` | Find active projectiles |
| `query_spot_anims` | Find active spot animations |
| `query_hint_arrows` | Find active hint arrows |

### Entity Details

| Tool | Description |
|------|-------------|
| `get_entity_info` | Full NPC/player details by handle |
| `get_entity_name` | Name by handle |
| `get_entity_health` | Current and max health |
| `get_entity_position` | Tile position |
| `get_entity_animation` | Current animation ID |
| `get_entity_hitmarks` | Active damage splats |
| `get_entity_overhead_text` | Overhead chat text |
| `is_entity_valid` | Check if handle is still valid |

### UI Components & Interfaces

| Tool | Description |
|------|-------------|
| `query_components` | Search by interface, item, sprite, text, options |
| `get_component_text` | Text content |
| `get_component_item` | Item held by component |
| `get_component_position` | Screen position and size |
| `get_component_options` | Right-click menu options |
| `get_component_sprite_id` | Sprite ID |
| `get_component_type` | Component type |
| `get_component_children` | Sub-components of a layer |
| `is_component_valid` | Check component exists |
| `get_open_interfaces` | All open interfaces |
| `is_interface_open` | Check if interface is open |

### Inventory & Items

| Tool | Description |
|------|-------------|
| `query_inventories` | List all active inventories |
| `query_inventory_items` | Search items across inventories |
| `get_inventory_item` | Item at specific slot |
| `get_item_vars` | Item variables for a slot |
| `get_item_var_value` | Specific item variable value |

### Game State

| Tool | Description |
|------|-------------|
| `get_local_player` | Local player info (position, health, animation) |
| `get_game_cycle` | Current game tick counter |
| `get_login_state` | Login state, progress, status |
| `get_mini_menu` | Current right-click menu entries |
| `get_grand_exchange_offers` | All GE offer slots |
| `get_current_world` | Current world ID |
| `query_worlds` | All available worlds |
| `get_player_stats` | All skill levels and XP |
| `get_player_stat` | Single skill stat |
| `query_chat_history` | Recent chat messages |

### Game Variables

| Tool | Description |
|------|-------------|
| `get_varp` | Player variable value |
| `get_varbit` | Varbit value |
| `get_varc_int` | Client variable (int) |
| `get_varc_string` | Client variable (string) |
| `query_varbits` | Batch read multiple varbits |

### Cache & Config Lookups

| Tool | Description |
|------|-------------|
| `get_item_type` | Item definition (name, options, price, slot) |
| `get_npc_type` | NPC definition (name, combat level, options) |
| `get_location_type` | Object definition (name, size, options) |
| `get_enum_type` | Enum key-value mapping |
| `get_struct_type` | Struct parameter bag |
| `get_sequence_type` | Animation sequence data |
| `get_quest_type` | Quest definition |
| `get_cache_file` | Raw cache file data |
| `get_cache_file_count` | File count in cache index |

### Rendering & Coordinates

| Tool | Description |
|------|-------------|
| `get_world_to_screen` | Project tile to screen position |
| `batch_world_to_screen` | Batch tile-to-screen projection |
| `get_entity_screen_positions` | Screen positions for multiple entities |
| `get_viewport_info` | Projection/view matrices and viewport |
| `get_game_window_rect` | Window position and size |
| `take_screenshot` | Capture game framebuffer as PNG |

### Actions (Unsafe)

These tools modify game state. They are prefixed `[UNSAFE]` in their descriptions.

| Tool | Description |
|------|-------------|
| `queue_action` | Queue a single game action |
| `queue_actions` | Queue multiple actions atomically |
| `clear_action_queue` | Clear all pending actions |
| `set_actions_blocked` | Block/unblock action processing |
| `get_action_queue_size` | Pending action count |
| `get_action_history` | Recent action history |
| `get_last_action_time` | Timestamp of last action |
| `are_actions_blocked` | Check if actions are blocked |

### Login & Session Control (Unsafe)

| Tool | Description |
|------|-------------|
| `set_world` | Set target world for login/hop |
| `change_login_state` | Change login state machine |
| `schedule_break` | Schedule logout break (ms) |
| `interrupt_break` | Cancel scheduled break |

### Script Execution (Unsafe)

| Tool | Description |
|------|-------------|
| `get_script_handle` | Get handle for a client script |
| `execute_script` | Execute script with int/string args |
| `destroy_script_handle` | Free a script handle |
| `fire_key_trigger` | Fire key input on a UI component |

### Utility

| Tool | Description |
|------|-------------|
| `ping` | Check connectivity |
| `list_methods` | List all registered RPC methods |
| `compute_name_hash` | Compute name hash for entity filtering |

## Wire Protocol

The MCP server communicates with the pipe server using msgpack over Windows Named Pipes. Each message is framed as:

```
[4 bytes: little-endian uint32 body length] [N bytes: msgpack-encoded JSON body]
```

See [../claudedocs/pipe_server_api.md](../claudedocs/pipe_server_api.md) for the full RPC method reference.
