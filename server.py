"""
BotWithUs MCP Server

Bridges Claude Code to the game pipe server via msgpack over named pipes.
The game must be running with agentcpp injected.
Each game instance creates a pipe at \\\\.\\pipe\\BotWithUs_{PID}.
Use --pid to target a specific instance, or omit to auto-discover.
"""

import sys
import struct
import json
import argparse
import ctypes
import threading
from typing import Optional

import msgpack
import win32file
import win32pipe
import pywintypes

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("botwithus", log_level="ERROR")

PIPE_PREFIX = "BotWithUs_"
MAX_BODY_SIZE = 16 * 1024 * 1024


def discover_game_pipes() -> list[dict]:
    """Find all running BotWithUs pipe instances. Returns list of {pid, pipe_name}."""
    import os
    results = []
    try:
        pipe_dir = r"\\.\pipe"
        for name in os.listdir(pipe_dir):
            if name.startswith(PIPE_PREFIX):
                pid_str = name[len(PIPE_PREFIX):]
                try:
                    pid = int(pid_str)
                    results.append({"pid": pid, "pipe_name": f"\\\\.\\pipe\\{name}"})
                except ValueError:
                    continue
    except OSError:
        pass
    return results


def get_pipe_name(pid: Optional[int] = None) -> str:
    """Get pipe name for a specific PID, or auto-discover the first available instance."""
    if pid is not None:
        return f"\\\\.\\pipe\\{PIPE_PREFIX}{pid}"
    instances = discover_game_pipes()
    if not instances:
        raise ConnectionError(
            "No BotWithUs game instances found. Is the game running with agentcpp injected?"
        )
    if len(instances) == 1:
        return instances[0]["pipe_name"]
    pids = [str(i["pid"]) for i in instances]
    raise ConnectionError(
        f"Multiple game instances found (PIDs: {', '.join(pids)}). "
        f"Use --pid to specify which instance to connect to."
    )


class PipeClient:
    def __init__(self, pipe_name: str):
        self._pipe_name = pipe_name
        self._handle = None
        self._lock = threading.Lock()
        self._request_id = 0

    def connect(self):
        if self._handle is not None:
            return
        try:
            self._handle = win32file.CreateFile(
                self._pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None,
                win32file.OPEN_EXISTING,
                0, None,
            )
            win32pipe.SetNamedPipeHandleState(
                self._handle, win32pipe.PIPE_READMODE_BYTE, None, None
            )
        except pywintypes.error as e:
            self._handle = None
            raise ConnectionError(
                f"Cannot connect to game pipe ({self._pipe_name}). "
                f"Is the game running with agentcpp injected? ({e})"
            )

    def disconnect(self):
        if self._handle is not None:
            try:
                win32file.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    def _send(self, data: dict):
        body = msgpack.packb(data, use_bin_type=True)
        header = struct.pack("<I", len(body))
        win32file.WriteFile(self._handle, header + body)

    def _recv(self) -> dict:
        _, header = win32file.ReadFile(self._handle, 4)
        if len(header) < 4:
            raise ConnectionError("Pipe closed (incomplete header)")
        body_len = struct.unpack("<I", header)[0]
        if body_len == 0 or body_len > MAX_BODY_SIZE:
            raise ConnectionError(f"Invalid body length: {body_len}")
        chunks = []
        remaining = body_len
        while remaining > 0:
            _, chunk = win32file.ReadFile(self._handle, remaining)
            if not chunk:
                raise ConnectionError("Pipe closed during body read")
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        return msgpack.unpackb(body, raw=False, unicode_errors='replace')

    def call(self, method: str, params: Optional[dict] = None) -> object:
        with self._lock:
            self.connect()
            self._request_id += 1
            req_id = self._request_id
            msg = {"method": method, "id": req_id}
            if params:
                msg["params"] = params
            try:
                self._send(msg)
                while True:
                    resp = self._recv()
                    if "event" in resp:
                        continue
                    if resp.get("id") == req_id:
                        if "error" in resp:
                            raise Exception(f"RPC error: {resp['error']}")
                        return resp.get("result")
            except (pywintypes.error, ConnectionError, OSError):
                self.disconnect()
                raise


pipe: Optional[PipeClient] = None


def rpc(method: str, **params):
    if pipe is None:
        raise ConnectionError("PipeClient not initialized. Server startup failed.")
    return pipe.call(method, params if params else None)


# ── Connection ────────────────────────────────────────────────────────


@mcp.tool()
def ping() -> dict:
    """Ping the game pipe server. Returns {"pong": true} if connected."""
    return rpc("rpc.ping")


@mcp.tool()
def list_methods() -> list:
    """List all RPC methods registered on the game pipe server."""
    return rpc("rpc.list_methods")


# ── Entity Queries ────────────────────────────────────────────────────


@mcp.tool()
def query_npcs(
    radius: Optional[int] = None,
    tile_x: int = 0, tile_y: int = 0,
    plane: int = -1,
    type_id: int = -1,
    name_pattern: Optional[str] = None,
    match_type: str = "contains",
    case_sensitive: bool = False,
    visible_only: bool = False,
    in_combat: bool = False,
    not_in_combat: bool = False,
    option_pattern: Optional[str] = None,
    option_match_type: str = "contains",
    sort_by_distance: bool = False,
    max_results: int = 0,
) -> list:
    """Query NPCs in the game world.

    Returns list of NPCs with handle, server_index, type_id, tile_x, tile_y, name, etc.
    Use handle with get_entity_info/get_entity_health for details.

    Args:
        radius: Max distance in tiles from (tile_x, tile_y). Omit for no spatial filter.
        tile_x: Center X tile for radius/distance sorting.
        tile_y: Center Y tile for radius/distance sorting.
        plane: Filter to specific game plane (-1 = any).
        type_id: Filter by NPC type ID (-1 = any).
        name_pattern: Filter by name string.
        match_type: How to match name: exact, prefix, suffix, contains, regex.
        visible_only: Only return visible NPCs.
        in_combat: Only NPCs currently in combat.
        not_in_combat: Only NPCs not in combat.
        option_pattern: Filter by right-click option text (e.g. "Talk-to", "Attack").
        option_match_type: How to match option: exact, prefix, suffix, contains, regex.
        sort_by_distance: Sort results by distance from tile_x/tile_y.
        max_results: Limit number of results (0 = unlimited).
    """
    p = {"type": "npc"}
    if radius is not None:
        p["radius"] = radius
    if tile_x: p["tile_x"] = tile_x
    if tile_y: p["tile_y"] = tile_y
    if plane >= 0: p["plane"] = plane
    if type_id >= 0: p["type_id"] = type_id
    if name_pattern: p["name_pattern"] = name_pattern
    if match_type != "contains": p["match_type"] = match_type
    if case_sensitive: p["case_sensitive"] = True
    if visible_only: p["visible_only"] = True
    if in_combat: p["in_combat"] = True
    if not_in_combat: p["not_in_combat"] = True
    if option_pattern: p["option_pattern"] = option_pattern
    if option_match_type != "contains": p["option_match_type"] = option_match_type
    if sort_by_distance: p["sort_by_distance"] = True
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_entities", **p)


@mcp.tool()
def query_players(
    radius: Optional[int] = None,
    tile_x: int = 0, tile_y: int = 0,
    plane: int = -1,
    name_pattern: Optional[str] = None,
    match_type: str = "contains",
    in_combat: bool = False,
    sort_by_distance: bool = False,
    max_results: int = 0,
) -> list:
    """Query players in the game world.

    Returns list of players with handle, server_index, type_id, tile_x, tile_y, name, etc.

    Args:
        radius: Max distance in tiles from (tile_x, tile_y).
        tile_x: Center X tile for radius/distance sorting.
        tile_y: Center Y tile for radius/distance sorting.
        plane: Filter to specific game plane (-1 = any).
        name_pattern: Filter by player name.
        match_type: How to match name: exact, prefix, suffix, contains, regex.
        in_combat: Only players currently in combat.
        sort_by_distance: Sort results by distance from tile_x/tile_y.
        max_results: Limit number of results (0 = unlimited).
    """
    p = {"type": "player"}
    if radius is not None:
        p["radius"] = radius
    if tile_x: p["tile_x"] = tile_x
    if tile_y: p["tile_y"] = tile_y
    if plane >= 0: p["plane"] = plane
    if name_pattern: p["name_pattern"] = name_pattern
    if match_type != "contains": p["match_type"] = match_type
    if in_combat: p["in_combat"] = True
    if sort_by_distance: p["sort_by_distance"] = True
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_entities", **p)


@mcp.tool()
def query_locations(
    radius: Optional[int] = None,
    tile_x: int = 0, tile_y: int = 0,
    plane: int = -1,
    type_id: int = -1,
    name_pattern: Optional[str] = None,
    match_type: str = "contains",
    option_pattern: Optional[str] = None,
    option_match_type: str = "contains",
    sort_by_distance: bool = False,
    max_results: int = 0,
) -> list:
    """Query game objects/locations (doors, trees, rocks, etc.) in the game world.

    Returns list with handle, type_id, tile_x, tile_y, name, options, name_hash.

    Args:
        radius: Max distance in tiles from (tile_x, tile_y).
        tile_x: Center X tile for radius/distance sorting.
        tile_y: Center Y tile for radius/distance sorting.
        plane: Filter to specific game plane (-1 = any).
        type_id: Filter by location type ID (-1 = any).
        name_pattern: Filter by name string.
        match_type: How to match name: exact, prefix, suffix, contains, regex.
        option_pattern: Filter by right-click option text (e.g. "Chop down", "Mine").
        option_match_type: How to match option: exact, prefix, suffix, contains, regex.
        sort_by_distance: Sort results by distance from tile_x/tile_y.
        max_results: Limit number of results (0 = unlimited).
    """
    p = {"type": "location"}
    if radius is not None:
        p["radius"] = radius
    if tile_x: p["tile_x"] = tile_x
    if tile_y: p["tile_y"] = tile_y
    if plane >= 0: p["plane"] = plane
    if type_id >= 0: p["type_id"] = type_id
    if name_pattern: p["name_pattern"] = name_pattern
    if match_type != "contains": p["match_type"] = match_type
    if option_pattern: p["option_pattern"] = option_pattern
    if option_match_type != "contains": p["option_match_type"] = option_match_type
    if sort_by_distance: p["sort_by_distance"] = True
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_entities", **p)


@mcp.tool()
def query_ground_items(
    radius: Optional[int] = None,
    tile_x: int = 0, tile_y: int = 0,
    plane: int = -1,
    sort_by_distance: bool = False,
    max_results: int = 0,
) -> list:
    """Query ground items (dropped items on the floor).

    Returns list with handle, tile_x, tile_y, and items array [{item_id, quantity}, ...].

    Args:
        radius: Max distance in tiles from (tile_x, tile_y).
        tile_x: Center X tile for radius/distance sorting.
        tile_y: Center Y tile for radius/distance sorting.
        plane: Filter to specific game plane (-1 = any).
        sort_by_distance: Sort results by distance from tile_x/tile_y.
        max_results: Limit number of results (0 = unlimited).
    """
    p = {}
    if radius is not None:
        p["radius"] = radius
    if tile_x: p["tile_x"] = tile_x
    if tile_y: p["tile_y"] = tile_y
    if plane >= 0: p["plane"] = plane
    if sort_by_distance: p["sort_by_distance"] = True
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_ground_items", **p)


@mcp.tool()
def query_entities(
    type: str,
    radius: Optional[int] = None,
    tile_x: int = 0, tile_y: int = 0,
    plane: int = -1,
    type_id: int = -1,
    name_pattern: Optional[str] = None,
    match_type: str = "contains",
    case_sensitive: bool = False,
    visible_only: bool = False,
    moving_only: bool = False,
    stationary_only: bool = False,
    in_combat: bool = False,
    not_in_combat: bool = False,
    option_pattern: Optional[str] = None,
    option_match_type: str = "contains",
    sort_by_distance: bool = False,
    max_results: int = 0,
) -> list:
    """Generic entity query. Prefer query_npcs/query_players/query_locations for typed queries.

    Args:
        type: Entity type - "npc", "player", "location", or "obj_stack".
        radius: Max distance in tiles from (tile_x, tile_y).
        tile_x: Center X tile for radius/distance sorting.
        tile_y: Center Y tile for radius/distance sorting.
        plane: Filter to specific game plane (-1 = any).
        type_id: Filter by entity type ID (-1 = any).
        name_pattern: Filter by name string.
        match_type: How to match name: exact, prefix, suffix, contains, regex.
        case_sensitive: Case sensitive name matching.
        visible_only: Only visible entities.
        moving_only: Only moving entities.
        stationary_only: Only stationary entities.
        in_combat: Only entities in combat.
        not_in_combat: Only entities not in combat.
        option_pattern: Filter by right-click option text (NPC/location only).
        option_match_type: How to match option: exact, prefix, suffix, contains, regex.
        sort_by_distance: Sort by distance from tile_x/tile_y.
        max_results: Limit results (0 = unlimited).
    """
    p = {"type": type}
    if radius is not None: p["radius"] = radius
    if tile_x: p["tile_x"] = tile_x
    if tile_y: p["tile_y"] = tile_y
    if plane >= 0: p["plane"] = plane
    if type_id >= 0: p["type_id"] = type_id
    if name_pattern: p["name_pattern"] = name_pattern
    if match_type != "contains": p["match_type"] = match_type
    if case_sensitive: p["case_sensitive"] = True
    if visible_only: p["visible_only"] = True
    if moving_only: p["moving_only"] = True
    if stationary_only: p["stationary_only"] = True
    if in_combat: p["in_combat"] = True
    if not_in_combat: p["not_in_combat"] = True
    if option_pattern: p["option_pattern"] = option_pattern
    if option_match_type != "contains": p["option_match_type"] = option_match_type
    if sort_by_distance: p["sort_by_distance"] = True
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_entities", **p)


# ── Entity Details ────────────────────────────────────────────────────


@mcp.tool()
def get_entity_info(handle: int) -> dict:
    """Get full info for an NPC or player by handle.

    Returns: handle, server_index, type_id, tile_x, tile_y, tile_z, name, name_hash,
    is_moving, is_hidden, animation_id, stance_id, health, max_health, following_index,
    overhead_text, combat_level.
    """
    return rpc("get_entity_info", handle=handle)


@mcp.tool()
def get_entity_name(handle: int) -> dict:
    """Get the name of an entity by handle. Returns {"name": "..."}."""
    return rpc("get_entity_name", handle=handle)


@mcp.tool()
def get_entity_health(handle: int) -> dict:
    """Get current and max health of an entity. Returns {"health": int, "max_health": int}."""
    return rpc("get_entity_health", handle=handle)


@mcp.tool()
def get_entity_position(handle: int) -> dict:
    """Get tile position of an entity. Returns {"tile_x": int, "tile_y": int, "plane": int}."""
    return rpc("get_entity_position", handle=handle)


@mcp.tool()
def get_entity_animation(handle: int) -> dict:
    """Get current animation ID of an entity. Returns {"animation_id": int}."""
    return rpc("get_entity_animation", handle=handle)


@mcp.tool()
def is_entity_valid(handle: int) -> dict:
    """Check if an entity handle is still valid. Returns {"valid": bool}."""
    return rpc("is_entity_valid", handle=handle)


@mcp.tool()
def get_entity_hitmarks(handle: int) -> list:
    """Get active hitmarks (damage splats) on an entity. Returns [{damage, type, cycle}, ...]."""
    return rpc("get_entity_hitmarks", handle=handle)


@mcp.tool()
def get_animation_length(animation_id: int) -> dict:
    """Get the byte length of an animation archive. Returns {"length": int} (-1 if not found)."""
    return rpc("get_animation_length", animation_id=animation_id)


# ── Projectiles, Spot Anims, Hint Arrows ──────────────────────────────


@mcp.tool()
def query_projectiles(
    projectile_id: int = -1,
    plane: int = -1,
    max_results: int = 0,
) -> list:
    """Query active projectiles in the game world.

    Returns list with handle, projectile_id, start_x/y, end_x/y, plane,
    target_index, source_index, start_cycle, end_cycle.

    Args:
        projectile_id: Filter by projectile ID (-1 = any).
        plane: Filter by plane (-1 = any).
        max_results: Limit results (0 = unlimited).
    """
    p = {}
    if projectile_id >= 0: p["projectile_id"] = projectile_id
    if plane >= 0: p["plane"] = plane
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_projectiles", **p)


@mcp.tool()
def query_spot_anims(
    anim_id: int = -1,
    plane: int = -1,
    max_results: int = 0,
) -> list:
    """Query active spot animations (graphic effects at locations).

    Returns list with handle, anim_id, tile_x, tile_y, tile_z.

    Args:
        anim_id: Filter by animation ID (-1 = any).
        plane: Filter by plane (-1 = any).
        max_results: Limit results (0 = unlimited).
    """
    p = {}
    if anim_id >= 0: p["anim_id"] = anim_id
    if plane >= 0: p["plane"] = plane
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_spot_anims", **p)


@mcp.tool()
def query_hint_arrows(max_results: int = 0) -> list:
    """Query active hint arrows (tutorial/quest indicators).

    Returns list with handle, type, tile_x, tile_y, tile_z, target_index.

    Args:
        max_results: Limit results (0 = unlimited).
    """
    p = {}
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_hint_arrows", **p)


# ── Worlds ────────────────────────────────────────────────────────────


@mcp.tool()
def query_worlds(include_activity: bool = False) -> list:
    """Query available game worlds.

    Returns list with world_id, properties, population, ping.
    Optionally includes activity string.

    Args:
        include_activity: Include the world's activity description string.
    """
    p = {}
    if include_activity: p["include_activity"] = True
    return rpc("query_worlds", **p)


@mcp.tool()
def get_current_world() -> dict:
    """Get the current world ID. Returns {"world_id": int} (-1 if not logged in)."""
    return rpc("get_current_world")


@mcp.tool()
def compute_name_hash(name: str) -> dict:
    """Compute the name hash for a string. Useful for name_hash entity filtering.

    Args:
        name: The name string to hash.
    """
    return rpc("compute_name_hash", name=name)


# ── Components / UI ──────────────────────────────────────────────────


@mcp.tool()
def query_components(
    interface_id: int = -1,
    item_id: int = -1,
    sprite_id: int = -1,
    type: int = -1,
    text_pattern: Optional[str] = None,
    match_type: str = "contains",
    case_sensitive: bool = False,
    option_pattern: Optional[str] = None,
    visible_only: bool = False,
    max_results: int = 0,
) -> list:
    """Query UI components (interface elements like buttons, text, items).

    Returns list with handle, interface_id, component_id, sub_component_id,
    type, item_id, item_count, sprite_id.

    Args:
        interface_id: Filter to specific interface (-1 = any).
        item_id: Filter by item ID shown in component (-1 = any).
        sprite_id: Filter by sprite ID (-1 = any).
        type: Filter by component type byte (-1 = any).
        text_pattern: Filter by text content.
        match_type: How to match text: exact, contains, regex.
        case_sensitive: Case sensitive text matching.
        option_pattern: Filter by right-click option text.
        visible_only: Only visible components.
        max_results: Limit results (0 = unlimited).
    """
    p = {}
    if interface_id >= 0: p["interface_id"] = interface_id
    if item_id >= 0: p["item_id"] = item_id
    if sprite_id >= 0: p["sprite_id"] = sprite_id
    if type >= 0: p["type"] = type
    if text_pattern: p["text_pattern"] = text_pattern
    if match_type != "contains": p["match_type"] = match_type
    if case_sensitive: p["case_sensitive"] = True
    if option_pattern: p["option_pattern"] = option_pattern
    if visible_only: p["visible_only"] = True
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_components", **p)


@mcp.tool()
def is_component_valid(interface_id: int, component_id: int, sub_component_id: int = -1) -> dict:
    """Check if a UI component exists and is valid.

    Args:
        interface_id: Interface ID.
        component_id: Component ID within the interface.
        sub_component_id: Sub-component ID (-1 for top-level).
    """
    return rpc("is_component_valid",
               interface_id=interface_id, component_id=component_id, sub_component_id=sub_component_id)


@mcp.tool()
def get_component_text(interface_id: int, component_id: int) -> dict:
    """Get the text content of a UI component. Returns {"text": str|null}."""
    return rpc("get_component_text", interface_id=interface_id, component_id=component_id)


@mcp.tool()
def get_component_item(interface_id: int, component_id: int, sub_component_id: int = -1) -> dict:
    """Get item shown in a UI component. Returns {"item_id": int, "count": int}."""
    return rpc("get_component_item",
               interface_id=interface_id, component_id=component_id, sub_component_id=sub_component_id)


@mcp.tool()
def get_component_position(interface_id: int, component_id: int) -> dict:
    """Get screen position and size of a UI component. Returns {x, y, width, height}."""
    return rpc("get_component_position", interface_id=interface_id, component_id=component_id)


@mcp.tool()
def get_component_options(interface_id: int, component_id: int) -> list:
    """Get right-click menu options of a UI component. Returns list of option strings."""
    return rpc("get_component_options", interface_id=interface_id, component_id=component_id)


@mcp.tool()
def get_component_sprite_id(interface_id: int, component_id: int) -> dict:
    """Get the sprite ID of a UI component. Returns {"sprite_id": int} (-1 if none)."""
    return rpc("get_component_sprite_id", interface_id=interface_id, component_id=component_id)


@mcp.tool()
def get_component_type(interface_id: int, component_id: int) -> dict:
    """Get the type of a UI component. Returns {"type": int, "type_name": str}."""
    return rpc("get_component_type", interface_id=interface_id, component_id=component_id)


@mcp.tool()
def get_component_children(interface_id: int, component_id: int) -> list:
    """Get child sub-components of a UI component.

    Returns list with handle, interface_id, component_id, sub_component_id,
    type, item_id, item_count, sprite_id.
    """
    return rpc("get_component_children", interface_id=interface_id, component_id=component_id)


@mcp.tool()
def get_open_interfaces() -> list:
    """Get all currently open interfaces. Returns [{parent_hash, interface_id}, ...]."""
    return rpc("get_open_interfaces")


@mcp.tool()
def is_interface_open(interface_id: int) -> dict:
    """Check if a specific interface is currently open. Returns {"open": bool}."""
    return rpc("is_interface_open", interface_id=interface_id)


# ── Inventory / Items ─────────────────────────────────────────────────


@mcp.tool()
def query_inventories() -> list:
    """List all active inventories. Returns [{inventory_id, item_count, capacity}, ...]."""
    return rpc("query_inventories")


@mcp.tool()
def query_inventory_items(
    inventory_id: int = -1,
    item_id: int = -1,
    min_quantity: int = 0,
    non_empty: bool = True,
    max_results: int = 0,
) -> list:
    """Query items in inventories.

    Returns list with handle, item_id, quantity, slot.

    Args:
        inventory_id: Filter to specific inventory (-1 = all).
        item_id: Filter by item ID (-1 = any).
        min_quantity: Minimum quantity filter (0 = no filter).
        non_empty: Exclude empty slots (default true).
        max_results: Limit results (0 = unlimited).
    """
    p = {}
    if inventory_id >= 0: p["inventory_id"] = inventory_id
    if item_id >= 0: p["item_id"] = item_id
    if min_quantity > 0: p["min_quantity"] = min_quantity
    if not non_empty: p["non_empty"] = False
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_inventory_items", **p)


@mcp.tool()
def get_inventory_item(inventory_id: int, slot: int) -> dict:
    """Get a specific inventory item by slot. Returns {handle, item_id, quantity, slot}."""
    return rpc("get_inventory_item", inventory_id=inventory_id, slot=slot)


@mcp.tool()
def get_item_vars(inventory_id: int, slot: int) -> list:
    """Get item variables for an item in a specific slot. Returns [{var_id, value}, ...]."""
    return rpc("get_item_vars", inventory_id=inventory_id, slot=slot)


@mcp.tool()
def get_item_var_value(inventory_id: int, slot: int, var_id: int) -> dict:
    """Get a specific item variable value. Returns {"value": int}."""
    return rpc("get_item_var_value", inventory_id=inventory_id, slot=slot, var_id=var_id)


# ── Player Stats ──────────────────────────────────────────────────────


@mcp.tool()
def get_player_stats() -> list:
    """Get all player skill stats. Returns [{skill_id, level, boosted_level, max_level, xp}, ...]."""
    return rpc("get_player_stats")


@mcp.tool()
def get_player_stat(skill_id: int) -> dict:
    """Get a specific skill stat. Returns {skill_id, level, boosted_level, max_level, xp}."""
    return rpc("get_player_stat", skill_id=skill_id)


# ── Chat ──────────────────────────────────────────────────────────────


@mcp.tool()
def query_chat_history(message_type: int = -1, max_results: int = 50) -> list:
    """Query chat message history.

    Returns [{index, message_type, text, player_name}, ...].

    Args:
        message_type: Filter by message type (-1 = all).
        max_results: Max messages to return (default 50).
    """
    p = {}
    if message_type >= 0: p["message_type"] = message_type
    if max_results != 50: p["max_results"] = max_results
    return rpc("query_chat_history", **p)


# ── Vars ──────────────────────────────────────────────────────────────


@mcp.tool()
def get_varp(var_id: int) -> dict:
    """Get a player variable (varp) value. Returns {"value": int}."""
    return rpc("get_varp", var_id=var_id)


@mcp.tool()
def get_varbit(varbit_id: int) -> dict:
    """Get a varbit value. Returns {"value": int}."""
    return rpc("get_varbit", varbit_id=varbit_id)


@mcp.tool()
def get_varc_int(varc_id: int) -> dict:
    """Get a client variable (varc) integer value. Returns {"value": int}."""
    return rpc("get_varc_int", varc_id=varc_id)


@mcp.tool()
def get_varc_string(varc_id: int) -> dict:
    """Get a client variable (varc) string value. Returns {"value": str}."""
    return rpc("get_varc_string", varc_id=varc_id)


@mcp.tool()
def query_varbits(varbit_ids: list[int]) -> list:
    """Get multiple varbit values at once. Returns [{varbit_id, value}, ...].

    Args:
        varbit_ids: List of varbit IDs to query.
    """
    return rpc("query_varbits", varbit_ids=varbit_ids)


# ── Cache ─────────────────────────────────────────────────────────────


@mcp.tool()
def get_cache_file(index_id: int, archive_id: int, file_id: int = 0) -> dict:
    """Read a file from the game cache. Returns {"data": bytes, "size": int}.

    Args:
        index_id: Cache index ID.
        archive_id: Archive ID within the index.
        file_id: File ID within the archive (default 0).
    """
    return rpc("get_cache_file", index_id=index_id, archive_id=archive_id, file_id=file_id)


@mcp.tool()
def get_cache_file_count(index_id: int, archive_id: int = 0, shift: int = 0) -> dict:
    """Get number of files in a cache index/archive. Returns {"count": int}.

    Args:
        index_id: Cache index ID.
        archive_id: Archive ID (default 0).
        shift: Bit shift for addressing (default 0).
    """
    return rpc("get_cache_file_count", index_id=index_id, archive_id=archive_id, shift=shift)


# ── Config Type Lookups ──────────────────────────────────────────────


@mcp.tool()
def get_item_type(id: int) -> dict:
    """Get item definition by ID. Returns name, options, price, equipment slot, stackability, etc.

    Args:
        id: Item type ID.
    """
    return rpc("get_item_type", id=id)


@mcp.tool()
def get_npc_type(id: int) -> dict:
    """Get NPC definition by ID. Returns name, options, combat level, transforms, etc.

    Args:
        id: NPC type ID.
    """
    return rpc("get_npc_type", id=id)


@mcp.tool()
def get_location_type(id: int) -> dict:
    """Get location/object definition by ID. Returns name, options, size, interaction type, etc.

    Args:
        id: Location type ID.
    """
    return rpc("get_location_type", id=id)


@mcp.tool()
def get_enum_type(id: int) -> dict:
    """Get enum (key-value mapping) definition by ID. Enums map inputs to outputs (e.g. skill IDs to names).

    Returns id, input/output type IDs, default values, and entries map.

    Args:
        id: Enum type ID.
    """
    return rpc("get_enum_type", id=id)


@mcp.tool()
def get_struct_type(id: int) -> dict:
    """Get struct definition by ID. Structs are parameter bags (key-value pairs of int/string).

    Returns id and params map.

    Args:
        id: Struct type ID.
    """
    return rpc("get_struct_type", id=id)


@mcp.tool()
def get_sequence_type(id: int) -> dict:
    """Get animation sequence definition by ID. Returns frame data, priority, loop info, hand items, etc.

    Args:
        id: Animation sequence (anim) ID.
    """
    return rpc("get_sequence_type", id=id)


@mcp.tool()
def get_quest_type(id: int) -> dict:
    """Get quest definition by ID. Returns name, difficulty, requirements, progress tracking vars, etc.

    Args:
        id: Quest type ID.
    """
    return rpc("get_quest_type", id=id)


# ── Action Queue (read-only) ─────────────────────────────────────────


@mcp.tool()
def get_action_queue_size() -> dict:
    """Get the number of actions currently queued. Returns {"size": int}."""
    return rpc("get_action_queue_size")


@mcp.tool()
def get_action_history(max_results: int = 50, action_id_filter: int = -1) -> list:
    """Get recent action history (newest first).

    Returns [{action_id, param1, param2, param3, timestamp, delta}, ...].

    Args:
        max_results: Max entries to return (default 50).
        action_id_filter: Filter to specific action ID (-1 = all).
    """
    p = {}
    if max_results != 50: p["max_results"] = max_results
    if action_id_filter >= 0: p["action_id_filter"] = action_id_filter
    return rpc("get_action_history", **p)


@mcp.tool()
def get_last_action_time() -> dict:
    """Get timestamp of the last action. Returns {"timestamp": value}."""
    return rpc("get_last_action_time")


@mcp.tool()
def are_actions_blocked() -> dict:
    """Check if action processing is currently blocked. Returns {"blocked": bool}."""
    return rpc("are_actions_blocked")


# ── Game State ────────────────────────────────────────────────────────


@mcp.tool()
def get_account_info() -> dict:
    """Get account information for the current client session.

    Returns: client_type (0=jagex, 1=steam), client_state, session_id,
    ip_hash, jx_display_name (from launcher), jx_character_id,
    display_name (in-game, null if not logged in), is_member,
    server_index, logged_in, login_progress, login_status.
    """
    return rpc("get_account_info")


@mcp.tool()
def get_local_player() -> dict:
    """Get the local player's info (position, name, combat level, health, animation, etc.).

    Returns: server_index, name, tile_x, tile_y, plane, is_member, is_moving,
    animation_id, stance_id, health, max_health, combat_level, overhead_text,
    target_index, target_type.
    """
    return rpc("get_local_player")


@mcp.tool()
def get_game_cycle() -> dict:
    """Get the current game tick counter. Returns {"cycle": int}."""
    return rpc("get_game_cycle")


@mcp.tool()
def get_login_state() -> dict:
    """Get the current login state. Returns {"state": int, "login_progress": int, "login_status": int}."""
    return rpc("get_login_state")


@mcp.tool()
def get_mini_menu() -> list:
    """Get the current right-click menu entries.

    Returns list of [{option_text, action_id, type_id, item_id, param1, param2, param3}, ...].
    """
    return rpc("get_mini_menu")


@mcp.tool()
def get_grand_exchange_offers() -> list:
    """Get all GE offer slots.

    Returns list of [{slot, status, type, item_id, price, count, completed_count, completed_gold}, ...].
    """
    return rpc("get_grand_exchange_offers")


@mcp.tool()
def get_entity_overhead_text(handle: int) -> dict:
    """Get overhead text for an entity by handle. Returns {"text": str}.

    Args:
        handle: Entity handle from a query result.
    """
    return rpc("get_entity_overhead_text", handle=handle)


@mcp.tool()
def get_world_to_screen(tile_x: int, tile_y: int) -> dict:
    """Project a tile coordinate to screen position. Returns {"screen_x": float, "screen_y": float}.

    Args:
        tile_x: The tile X coordinate.
        tile_y: The tile Y coordinate.
    """
    return rpc("get_world_to_screen", tile_x=tile_x, tile_y=tile_y)


@mcp.tool()
def batch_world_to_screen(tiles: list[dict]) -> dict:
    """Batch-convert tile coordinates to screen positions in one call.

    Returns {"results": [{"screen_x": float, "screen_y": float}, ...]}.

    Args:
        tiles: List of tile dicts, each with "x" and "y" integer keys.
    """
    return rpc("batch_world_to_screen", tiles=tiles)


@mcp.tool()
def get_viewport_info() -> dict:
    """Get projection matrix, view matrix, and viewport dimensions.

    Returns viewport_width, viewport_height, projection_matrix (16 floats row-major),
    view_matrix (16 floats row-major).
    """
    return rpc("get_viewport_info")


@mcp.tool()
def get_entity_screen_positions(handles: list[int]) -> dict:
    """Get screen positions for entities by handle.

    Returns {"results": [{"handle": int, "screen_x": float, "screen_y": float, "valid": bool}, ...]}.

    Args:
        handles: List of entity handles from query results.
    """
    return rpc("get_entity_screen_positions", handles=handles)


@mcp.tool()
def get_game_window_rect() -> dict:
    """Get game window position and size on screen for external overlay alignment.

    Returns x, y, width, height (outer window) and client_x, client_y,
    client_width, client_height (client area).
    """
    return rpc("get_game_window_rect")


@mcp.tool()
def take_screenshot() -> list:
    """Take a screenshot of the game window. Returns the image as a PNG.

    Captures the game framebuffer (1280x720) before overlay rendering.
    The screenshot is taken on the next frame after the request.
    """
    import base64
    result = rpc("take_screenshot")
    if "error" in result:
        return [{"type": "text", "text": f"Screenshot failed: {result['error']}"}]
    png_bytes = result["data"]
    if isinstance(png_bytes, bytes):
        b64 = base64.b64encode(png_bytes).decode("ascii")
    else:
        b64 = base64.b64encode(bytes(png_bytes)).decode("ascii")
    return [
        {"type": "image", "data": b64, "mimeType": "image/png"},
    ]


# ── Obj Stacks ────────────────────────────────────────────────────────


@mcp.tool()
def query_obj_stacks(
    radius: Optional[int] = None,
    tile_x: int = 0, tile_y: int = 0,
    plane: int = -1,
    max_results: int = 0,
) -> list:
    """Query object stacks (piles of items on the ground).

    Returns list with handle, tile_x, tile_y, tile_z.
    Use get_obj_stack_items to get items in a stack.

    Args:
        radius: Max distance in tiles from (tile_x, tile_y).
        tile_x: Center X tile.
        tile_y: Center Y tile.
        plane: Filter by plane (-1 = any).
        max_results: Limit results (0 = unlimited).
    """
    p = {}
    if radius is not None: p["radius"] = radius
    if tile_x: p["tile_x"] = tile_x
    if tile_y: p["tile_y"] = tile_y
    if plane >= 0: p["plane"] = plane
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_obj_stacks", **p)


@mcp.tool()
def get_obj_stack_items(handle: int) -> list:
    """Get items in an object stack. Returns [{item_id, quantity}, ...]."""
    return rpc("get_obj_stack_items", handle=handle)


# ══════════════════════════════════════════════════════════════════════
# UNSAFE TOOLS (only registered with --unsafe flag)
# ══════════════════════════════════════════════════════════════════════


@mcp.tool()
def queue_action(action_id: int, param1: int = 0, param2: int = 0, param3: int = 0) -> dict:
    """[UNSAFE] Queue a single game action.

    Args:
        action_id: The action type ID.
        param1: First action parameter.
        param2: Second action parameter.
        param3: Third action parameter.
    """
    return rpc("queue_action", action_id=action_id, param1=param1, param2=param2, param3=param3)


@mcp.tool()
def queue_actions(actions: list[dict]) -> dict:
    """[UNSAFE] Queue multiple game actions at once.

    Args:
        actions: List of action dicts, each with action_id and optional param1/param2/param3.
    """
    return rpc("queue_actions", actions=actions)


@mcp.tool()
def clear_action_queue() -> dict:
    """[UNSAFE] Clear all queued actions."""
    return rpc("clear_action_queue")


@mcp.tool()
def set_actions_blocked(blocked: bool) -> dict:
    """[UNSAFE] Block or unblock action processing.

    Args:
        blocked: True to block actions, False to unblock.
    """
    return rpc("set_actions_blocked", blocked=blocked)


@mcp.tool()
def set_world(world_id: int) -> dict:
    """[UNSAFE] Set the target world for login/hop.

    Args:
        world_id: The world ID to switch to.
    """
    return rpc("set_world", world_id=world_id)


@mcp.tool()
def change_login_state(new_state: int, old_state: int = 0) -> dict:
    """[UNSAFE] Change the game login state.

    Args:
        new_state: The target login state.
        old_state: Expected current state (0 = any).
    """
    return rpc("change_login_state", new_state=new_state, old_state=old_state)


@mcp.tool()
def schedule_break(duration: int) -> dict:
    """[UNSAFE] Schedule a break (lobby/logout) for the given duration in milliseconds.

    Args:
        duration: Break duration in milliseconds.
    """
    return rpc("schedule_break", duration=duration)


@mcp.tool()
def interrupt_break() -> dict:
    """[UNSAFE] Interrupt a currently scheduled break."""
    return rpc("interrupt_break")


@mcp.tool()
def login_to_lobby() -> dict:
    """[UNSAFE] Trigger login from the login screen to lobby. Only works when client state is 10 (login screen).

    Returns ok:true on success, or an error if not on login screen, login already in progress, or account unavailable.
    """
    return rpc("login_to_lobby")


@mcp.tool()
def get_auto_login() -> dict:
    """Check if auto login is enabled."""
    return rpc("get_auto_login")


@mcp.tool()
def set_auto_login(enabled: bool) -> dict:
    """[UNSAFE] Enable or disable auto login.

    Args:
        enabled: Whether to enable auto login.
    """
    return rpc("set_auto_login", enabled=enabled)


@mcp.tool()
def get_humanization_enabled() -> dict:
    """Check if input humanization (mouse paths, fatigue model, break recommendations) is enabled."""
    return rpc("get_humanization_enabled")


@mcp.tool()
def set_humanization_enabled(enabled: bool) -> dict:
    """[UNSAFE] Enable or disable input humanization (mouse path generation, fatigue/risk model, automatic break recommendations).

    Humanization is disabled by default.

    Args:
        enabled: Whether to enable humanization.
    """
    return rpc("set_humanization_enabled", enabled=enabled)


@mcp.tool()
def execute_script(handle: int, int_args: list[int] = [], string_args: list[str] = [],
                   returns: list[str] = []) -> dict:
    """[UNSAFE] Execute a client script by handle.

    Get a handle first with get_script_handle, then execute it.

    Args:
        handle: Script handle (from get_script_handle).
        int_args: Integer arguments to pass to the script.
        string_args: String arguments to pass to the script.
        returns: Expected return types in order - "int", "long", or "string".
    """
    p = {"handle": handle}
    if int_args: p["int_args"] = int_args
    if string_args: p["string_args"] = string_args
    if returns: p["returns"] = returns
    return rpc("execute_script", **p)


@mcp.tool()
def get_script_handle(script_id: int) -> dict:
    """[UNSAFE] Get a handle to a client script for execution. Returns {"handle": int}.

    Args:
        script_id: The client script ID.
    """
    return rpc("get_script_handle", script_id=script_id)


@mcp.tool()
def destroy_script_handle(handle: int) -> dict:
    """[UNSAFE] Destroy a script handle when done with it.

    Args:
        handle: The script handle to destroy.
    """
    return rpc("destroy_script_handle", handle=handle)


@mcp.tool()
def fire_key_trigger(interface_id: int, component_id: int, input: str) -> dict:
    """[UNSAFE] Fire a key trigger on a UI component.

    Args:
        interface_id: Interface ID of the component.
        component_id: Component ID within the interface.
        input: The key input string to fire.
    """
    return rpc("fire_key_trigger", interface_id=interface_id, component_id=component_id, input=input)


# ══════════════════════════════════════════════════════════════════════
# Tool safety classification
# ══════════════════════════════════════════════════════════════════════

UNSAFE_TOOLS = [
    "queue_action",
    "queue_actions",
    "clear_action_queue",
    "set_actions_blocked",
    "set_world",
    "change_login_state",
    "login_to_lobby",
    "set_auto_login",
    "set_humanization_enabled",
    "schedule_break",
    "interrupt_break",
    "execute_script",
    "get_script_handle",
    "destroy_script_handle",
    "fire_key_trigger",
]


def main():
    global pipe

    parser = argparse.ArgumentParser(description="BotWithUs MCP Server")
    parser.add_argument(
        "--transport", type=str, default="stdio",
        help="MCP transport: stdio (default) or http://host:port for SSE"
    )
    parser.add_argument(
        "--unsafe", action="store_true",
        help="Enable action/mutation tools (queue_action, set_world, execute_script, etc.)"
    )
    parser.add_argument(
        "--pid", type=int, default=None,
        help="Target game process ID. Omit to auto-discover (fails if multiple instances running)."
    )
    parser.add_argument(
        "--config", action="store_true",
        help="Print MCP configuration JSON for .claude.json and exit"
    )
    args = parser.parse_args()

    if args.config:
        import os
        config = {
            "mcpServers": {
                mcp.name: {
                    "command": sys.executable,
                    "args": [os.path.abspath(__file__)],
                    "timeout": 1800,
                }
            }
        }
        print(json.dumps(config, indent=2))
        return

    pipe_name = get_pipe_name(args.pid)
    pipe = PipeClient(pipe_name)
    print(f"Targeting pipe: {pipe_name}", file=sys.stderr)

    if not args.unsafe:
        mcp_tools = mcp._tool_manager._tools
        for name in UNSAFE_TOOLS:
            if name in mcp_tools:
                del mcp_tools[name]

    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            from urllib.parse import urlparse
            url = urlparse(args.transport)
            if url.hostname is None or url.port is None:
                raise ValueError(f"Invalid transport URL: {args.transport}")
            mcp.settings.host = url.hostname
            mcp.settings.port = url.port
            print(f"MCP Server at http://{mcp.settings.host}:{mcp.settings.port}/sse",
                  file=sys.stderr)
            mcp.run(transport="sse")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
