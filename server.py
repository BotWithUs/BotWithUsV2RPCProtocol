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


@mcp.tool()
def call_rpc(method: str, params: Optional[dict] = None) -> object:
    """Escape hatch — call any RPC method by name with arbitrary params.

    Use this when:
    - A method's signature isn't yet known and you want to probe it.
    - You need a server-side method that doesn't have a typed wrapper yet.
    - You hit "method not found" via a typed wrapper and want to verify it
      against rpc.list_methods.

    Args:
        method: Server-side RPC name (e.g. "get_components").
        params: Optional parameter dict (msgpack-encoded over the wire).
    """
    return rpc(method, **(params or {}))


# ── Components / UI (NEW tree API) ────────────────────────────────────


@mcp.tool()
def get_component(
    interface_id: int,
    component_id: int,
    sub_component_id: int = -1,
) -> dict:
    """Look up a single component by (interface, component, sub) tuple.

    Returns a node descriptor (handle, type, item_id, sprite_id, text, options, …).
    """
    p = {"interface_id": interface_id, "component_id": component_id}
    if sub_component_id >= 0:
        p["sub_component_id"] = sub_component_id
    return rpc("get_component", **p)


@mcp.tool()
def get_components(interface_id: int = -1) -> list:
    """List components, optionally filtered to one interface.

    Replacement for the old `query_components` — the live server's
    component API is tree-based; this returns a flat snapshot.
    """
    p = {}
    if interface_id >= 0:
        p["interface_id"] = interface_id
    return rpc("get_components", **p)


@mcp.tool()
def get_static_children(interface_id: int, component_id: int = -1) -> list:
    """Static (compile-time) children of a component or interface root.

    Pass component_id = -1 to fetch the interface's top-level static children.
    """
    p = {"interface_id": interface_id}
    if component_id >= 0:
        p["component_id"] = component_id
    return rpc("get_static_children", **p)


@mcp.tool()
def get_dynamic_children(interface_id: int, component_id: int = -1) -> list:
    """Dynamic (runtime-generated) children — e.g. inventory grid items."""
    p = {"interface_id": interface_id}
    if component_id >= 0:
        p["component_id"] = component_id
    return rpc("get_dynamic_children", **p)


@mcp.tool()
def get_interface_tree(interface_id: int) -> dict:
    """Dump the full component tree for an interface."""
    return rpc("get_interface_tree", interface_id=interface_id)


@mcp.tool()
def find_component_at(x: int, y: int) -> dict:
    """Find the topmost component under screen coordinates (x, y)."""
    return rpc("find_component_at", x=x, y=y)


# ── Game state ────────────────────────────────────────────────────────


@mcp.tool()
def get_game_cycle() -> dict:
    """Get the current game cycle counter."""
    return rpc("get_game_cycle")


@mcp.tool()
def get_login_state() -> dict:
    """Get the current login state and progress."""
    return rpc("get_login_state")


@mcp.tool()
def get_current_world() -> dict:
    """Get the current world ID."""
    return rpc("get_current_world")


@mcp.tool()
def get_account_info() -> dict:
    """Get account/session info (display_name, jx ids, logged_in, is_member, …)."""
    return rpc("get_account_info")


# ── Variables (varps / varcs / obj vars) ──────────────────────────────


@mcp.tool()
def get_varp(var_id: int) -> dict:
    """Read a single player variable (varp)."""
    return rpc("get_varp", var_id=var_id)


@mcp.tool()
def get_varps(var_ids: list[int]) -> list:
    """Read multiple varps in one call."""
    return rpc("get_varps", var_ids=var_ids)


@mcp.tool()
def get_varc_int(varc_id: int) -> dict:
    """Read a single client int variable (varc)."""
    return rpc("get_varc_int", varc_id=varc_id)


@mcp.tool()
def get_varcs_int(varc_ids: list[int]) -> list:
    """Read multiple varc ints in one call."""
    return rpc("get_varcs_int", varc_ids=varc_ids)


@mcp.tool()
def get_varc_string(varc_id: int) -> dict:
    """Read a single client string variable."""
    return rpc("get_varc_string", varc_id=varc_id)


@mcp.tool()
def get_varcs_string(varc_ids: list[int]) -> list:
    """Read multiple varc strings in one call."""
    return rpc("get_varcs_string", varc_ids=varc_ids)


@mcp.tool()
def get_obj_vars(handle: int) -> list:
    """Read all object variables attached to a component / inventory slot handle."""
    return rpc("get_obj_vars", handle=handle)


# ── Varbits (computed client-side; live server has no get_varbit RPC) ──
#
# Varbits are bit ranges inside a varp. The cache stores
# varbit_id → (varp_id, lsb, msb). The live RPC no longer exposes the
# cache, so callers supply that triple explicitly. To learn a triple, look
# it up in a wiki/cache dump or read it from the Java side
# (`api.getVarbit(...)` inside a bwu-api script).


def _extract_varp_value(varp_result) -> int:
    """Coerce a `get_varp` response into an int.

    The RPC may return a bare int or a dict like {"value": N}; handle both.
    """
    if isinstance(varp_result, dict):
        v = varp_result.get("value", 0)
        return int(v) if v is not None else 0
    if isinstance(varp_result, (int, bool)):
        return int(varp_result)
    return 0


def _extract_varbit(varp_value: int, lsb: int, msb: int) -> int:
    width = msb - lsb + 1
    if width <= 0 or width > 31:
        return 0
    mask = (1 << width) - 1
    return (varp_value >> lsb) & mask


@mcp.tool()
def get_varbit_value(varp_id: int, lsb: int, msb: int) -> dict:
    """Compute a varbit value client-side by reading its owning varp and
    extracting bits [lsb..msb] inclusive.

    Args:
        varp_id: The owning varp's id.
        lsb:     Low bit (0-based, inclusive).
        msb:     High bit (0-based, inclusive).

    Returns: {"varp_id", "lsb", "msb", "varp_value", "value"}.
    """
    varp = _extract_varp_value(rpc("get_varp", var_id=varp_id))
    return {
        "varp_id":    varp_id,
        "lsb":        lsb,
        "msb":        msb,
        "varp_value": varp,
        "value":      _extract_varbit(varp, lsb, msb),
    }


@mcp.tool()
def get_varbit_values(specs: list[dict]) -> list:
    """Batch varbit extraction. Each spec dict must contain `varp_id`,
    `lsb`, `msb`. Reads each distinct varp once via `get_varps`, then
    extracts the bits per spec.

    Returns a list parallel to `specs`, each entry shaped like the
    single-shot `get_varbit_value` response.
    """
    if not specs:
        return []
    ids = sorted({int(s["varp_id"]) for s in specs})
    raw = rpc("get_varps", var_ids=ids)
    by_id: dict[int, int] = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                vid = entry.get("var_id", entry.get("varp_id"))
                if vid is not None:
                    by_id[int(vid)] = _extract_varp_value(entry)
    out = []
    for s in specs:
        vid = int(s["varp_id"])
        lsb = int(s["lsb"])
        msb = int(s["msb"])
        varp_value = by_id.get(vid, 0)
        out.append({
            "varp_id":    vid,
            "lsb":        lsb,
            "msb":        msb,
            "varp_value": varp_value,
            "value":      _extract_varbit(varp_value, lsb, msb),
        })
    return out


# ── Movement / Pathing ────────────────────────────────────────────────


@mcp.tool()
def walk_to(x: int, y: int) -> dict:
    """[UNSAFE] Walk to a local tile (minimap-style click)."""
    return rpc("walk_to", x=x, y=y)


@mcp.tool()
def walk_world_path(x: int, y: int, plane: int = 0) -> dict:
    """[UNSAFE] Walk to a world tile via the path solver."""
    return rpc("walk_world_path", x=x, y=y, plane=plane)


@mcp.tool()
def walk_cancel() -> dict:
    """[UNSAFE] Cancel the in-flight walk request."""
    return rpc("walk_cancel")


@mcp.tool()
def walk_status() -> dict:
    """Current walk-request status."""
    return rpc("walk_status")


@mcp.tool()
def is_reachable(x: int, y: int, plane: int = 0) -> dict:
    """Check whether (x, y, plane) is reachable from the player's current tile."""
    return rpc("is_reachable", x=x, y=y, plane=plane)


@mcp.tool()
def find_path(x: int, y: int) -> dict:
    """Compute a local path to (x, y) — does not execute, just returns the route."""
    return rpc("find_path", x=x, y=y)


@mcp.tool()
def find_world_path(x: int, y: int, plane: int = 0) -> dict:
    """Compute a world path to (x, y, plane) — does not execute, just returns the route."""
    return rpc("find_world_path", x=x, y=y, plane=plane)


@mcp.tool()
def region_cache_info() -> dict:
    """Inspect the region-cache contents (for path-solver debugging)."""
    return rpc("region_cache_info")


@mcp.tool()
def region_cache_clear() -> dict:
    """[UNSAFE] Clear the region cache — forces re-fetch on next path query."""
    return rpc("region_cache_clear")


# ── Input ─────────────────────────────────────────────────────────────


@mcp.tool()
def send_key(key_code: int) -> dict:
    """[UNSAFE] Inject a keyboard event into the game window."""
    return rpc("send_key", key_code=key_code)


@mcp.tool()
def send_click(x: int, y: int, button: int = 0) -> dict:
    """[UNSAFE] Inject a mouse click at (x, y). button 0 = left, 1 = right."""
    return rpc("send_click", x=x, y=y, button=button)


@mcp.tool()
def record_move_path() -> dict:
    """[UNSAFE] Toggle/record human mouse-move path data."""
    return rpc("record_move_path")


# ── World map ─────────────────────────────────────────────────────────


@mcp.tool()
def query_world_map_elements(
    text_pattern: Optional[str] = None,
    max_results: int = 0,
) -> list:
    """Search labelled world-map elements (towns, banks, dungeons, …)."""
    p = {}
    if text_pattern: p["text_pattern"] = text_pattern
    if max_results > 0: p["max_results"] = max_results
    return rpc("query_world_map_elements", **p)


# ── Action queue ──────────────────────────────────────────────────────


@mcp.tool()
def queue_action(action_id: int, param1: int = 0, param2: int = 0, param3: int = 0) -> dict:
    """[UNSAFE] Queue a single game action.

    Args:
        action_id: Action type ID (see ActionTypes in the Java framework).
        param1: First parameter (semantics depend on action_id).
        param2: Second parameter.
        param3: Third parameter.
    """
    return rpc("queue_action", action_id=action_id, param1=param1, param2=param2, param3=param3)


@mcp.tool()
def queue_actions(actions: list[dict]) -> dict:
    """[UNSAFE] Queue multiple game actions atomically.

    Args:
        actions: List of {action_id, param1, param2, param3} dicts.
    """
    return rpc("queue_actions", actions=actions)


@mcp.tool()
def get_action_queue_size() -> dict:
    """Pending action queue length."""
    return rpc("get_action_queue_size")


@mcp.tool()
def clear_action_queue() -> dict:
    """[UNSAFE] Clear all pending actions."""
    return rpc("clear_action_queue")


@mcp.tool()
def get_action_history(max_results: int = 50, action_id_filter: int = -1) -> list:
    """Recent action executions, newest first."""
    p = {}
    if max_results != 50: p["max_results"] = max_results
    if action_id_filter >= 0: p["action_id_filter"] = action_id_filter
    return rpc("get_action_history", **p)


@mcp.tool()
def get_last_action_time() -> dict:
    """Timestamp of the most recent action execution."""
    return rpc("get_last_action_time")


@mcp.tool()
def are_actions_blocked() -> dict:
    """Check whether action dispatch is currently blocked."""
    return rpc("are_actions_blocked")


@mcp.tool()
def set_actions_blocked(blocked: bool) -> dict:
    """[UNSAFE] Toggle the action-dispatch block flag."""
    return rpc("set_actions_blocked", blocked=blocked)


# ── Login / world ─────────────────────────────────────────────────────


@mcp.tool()
def set_world(world_id: int) -> dict:
    """[UNSAFE] Set the target world (used during login)."""
    return rpc("set_world", world_id=world_id)


@mcp.tool()
def change_login_state(new_state: int, old_state: int = 0) -> dict:
    """[UNSAFE] Advance the login state machine."""
    p = {"new_state": new_state}
    if old_state != 0: p["old_state"] = old_state
    return rpc("change_login_state", **p)


@mcp.tool()
def login_to_lobby() -> dict:
    """[UNSAFE] Execute the lobby-login client script (only valid in state 10)."""
    return rpc("login_to_lobby")


@mcp.tool()
def get_auto_login() -> dict:
    """Auto-login toggle state."""
    return rpc("get_auto_login")


@mcp.tool()
def set_auto_login(enabled: bool) -> dict:
    """[UNSAFE] Toggle auto-login."""
    return rpc("set_auto_login", enabled=enabled)


# ── Token refresher (Jagex auth) ──────────────────────────────────────


@mcp.tool()
def get_token_refresher() -> dict:
    """Token-refresher configuration."""
    return rpc("get_token_refresher")


@mcp.tool()
def set_token_refresher(config: dict) -> dict:
    """[UNSAFE] Update token-refresher configuration."""
    return rpc("set_token_refresher", **config)


@mcp.tool()
def trigger_token_refresh() -> dict:
    """[UNSAFE] Force an immediate token refresh."""
    return rpc("trigger_token_refresh")


# ── Breaks ────────────────────────────────────────────────────────────


@mcp.tool()
def schedule_break(duration: int) -> dict:
    """[UNSAFE] Schedule a humanization break of `duration` ms."""
    return rpc("schedule_break", duration=duration)


@mcp.tool()
def interrupt_break() -> dict:
    """[UNSAFE] Cancel any in-progress break."""
    return rpc("interrupt_break")


# ── Capture / stream ──────────────────────────────────────────────────


@mcp.tool()
def take_screenshot() -> list:
    """Capture a PNG framebuffer (1280x720). Returns raw bytes."""
    return rpc("take_screenshot")


@mcp.tool()
def start_stream() -> dict:
    """[UNSAFE] Begin continuous JPEG frame streaming on a separate pipe."""
    return rpc("start_stream")


@mcp.tool()
def stop_stream() -> dict:
    """[UNSAFE] End frame streaming."""
    return rpc("stop_stream")


# ── Script execution ──────────────────────────────────────────────────


@mcp.tool()
def get_script_handle(script_id: int) -> dict:
    """[UNSAFE] Acquire a handle to a client script."""
    return rpc("get_script_handle", script_id=script_id)


@mcp.tool()
def execute_script(
    handle: int,
    int_args: Optional[list[int]] = None,
    string_args: Optional[list[str]] = None,
    returns: Optional[list[str]] = None,
) -> dict:
    """[UNSAFE] Execute a script handle with the given args.

    Args:
        handle: Script handle from get_script_handle.
        int_args: Int args to pass.
        string_args: String args to pass.
        returns: Expected return types ("int" | "long" | "string").
    """
    p = {"handle": handle}
    if int_args:    p["int_args"] = int_args
    if string_args: p["string_args"] = string_args
    if returns:     p["returns"] = returns
    return rpc("execute_script", **p)


@mcp.tool()
def destroy_script_handle(handle: int) -> dict:
    """[UNSAFE] Release a script handle."""
    return rpc("destroy_script_handle", handle=handle)


# ── Debug pub/sub ─────────────────────────────────────────────────────


@mcp.tool()
def debug_subscribe(channel: str) -> dict:
    """[UNSAFE] Subscribe to a debug pub/sub channel."""
    return rpc("_debug.subscribe", channel=channel)


@mcp.tool()
def debug_unsubscribe(channel: str) -> dict:
    """[UNSAFE] Unsubscribe from a debug pub/sub channel."""
    return rpc("_debug.unsubscribe", channel=channel)


@mcp.tool()
def debug_publish(channel: str, payload: Optional[dict] = None) -> dict:
    """[UNSAFE] Publish a payload on a debug pub/sub channel."""
    p = {"channel": channel}
    if payload: p["payload"] = payload
    return rpc("_debug.publish", **p)


# ── Agent / license ───────────────────────────────────────────────────


@mcp.tool()
def agent_set_license(license_key: str) -> dict:
    """[UNSAFE] Set the BotWithUs agent license key."""
    return rpc("agent.set_license", license_key=license_key)



# ══════════════════════════════════════════════════════════════════════
# Tool safety classification — methods that mutate game state
# ══════════════════════════════════════════════════════════════════════

UNSAFE_TOOLS = [
    # action queue
    "queue_action", "queue_actions", "clear_action_queue", "set_actions_blocked",
    # movement / input
    "walk_to", "walk_world_path", "walk_cancel",
    "send_key", "send_click", "record_move_path",
    # login / world / tokens
    "set_world", "change_login_state", "login_to_lobby", "set_auto_login",
    "set_token_refresher", "trigger_token_refresh",
    # breaks / scripts
    "schedule_break", "interrupt_break",
    "execute_script", "get_script_handle", "destroy_script_handle",
    # streaming / region cache
    "start_stream", "stop_stream", "region_cache_clear",
    # debug pub/sub + agent
    "debug_subscribe", "debug_unsubscribe", "debug_publish",
    "agent_set_license",
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
