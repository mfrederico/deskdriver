"""desk-mcp: the desktop as MCP tools (stdio), for Claude Code and any other MCP client."""
import os

from mcp.server.mcpserver import Image, MCPServer

from .client import DeskClient, DeskError

INSTRUCTIONS = """You can see and drive this computer's desktop.
Workflow: desk_screenshot -> look -> act (desk_click / desk_type / desk_press / desk_drag / desk_scroll) -> desk_screenshot to verify.
Coordinates are pixels of the screenshot you received (origin top-left). The pointer is drawn into screenshots.
Use desk_zoom on a region before precise work. Several agents may share the desktop: take desk_lease before a
sequence of inputs and desk_release when done; a step refused with 'leased to' means another agent is driving.
"""

mcp = MCPServer("deskdriver", instructions=INSTRUCTIONS)
client = DeskClient(agent=os.environ.get("DESK_AGENT") or f"mcp-{os.getpid()}")


def _err(e: DeskError) -> str:
    return f"error: {e}"


@mcp.tool(description="Screenshot of the desktop. Coordinates for all other tools are pixels of this image.")
def desk_screenshot(scale: float = 1.0) -> list:
    try:
        data, h = client.screenshot(scale if scale != 1.0 else None)
    except DeskError as e:
        return [_err(e)]
    return [f"screen {h.get('X-Desk-View-Size')} view; image {h.get('X-Desk-Image-Size')}"
            + (f" (scaled x{scale}; divide coordinates you read off it by {scale})" if scale != 1.0 else ""),
            Image(data=data, format="png")]


@mcp.tool(description="Crop a region (x, y, w, h in screenshot pixels) at native resolution, optionally enlarged, for pixel-precise work.")
def desk_zoom(x: int, y: int, w: int, h: int, zoom: float = 2.0) -> list:
    try:
        data, hd = client.screenshot(zoom, (x, y, w, h))
    except DeskError as e:
        return [_err(e)]
    return [f"region x={x} y={y} w={w} h={h} shown at x{zoom} ({hd.get('X-Desk-Image-Size')}); "
            f"a point (px,py) in this image is screen ({x}+px/{zoom}, {y}+py/{zoom})",
            Image(data=data, format="png")]


@mcp.tool(description="Move the mouse pointer to (x, y) without clicking (hover).")
def desk_move(x: float, y: float) -> str:
    try:
        return str(client.step("move", x=x, y=y))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Click at (x, y). button: left|right|middle. count 2 = double click. modifiers e.g. ['shift'].")
def desk_click(x: float, y: float, button: str = "left", count: int = 1, modifiers: list[str] | None = None) -> str:
    try:
        return str(client.step("click", x=x, y=y, button=button, count=count, modifiers=modifiers or []))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Press the mouse at (x, y), move to (to_x, to_y) and release: moves windows, selects text, draws.")
def desk_drag(x: float, y: float, to_x: float, to_y: float, button: str = "left", steps: int = 16) -> str:
    try:
        return str(client.step("drag", x=x, y=y, to_x=to_x, to_y=to_y, button=button, steps=steps))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Scroll the wheel at (x, y). dy>0 scrolls down, dy<0 up; dx for horizontal. Units are wheel notches.")
def desk_scroll(x: float, y: float, dy: int = 3, dx: int = 0) -> str:
    try:
        return str(client.step("wheel", x=x, y=y, dy=dy, dx=dx))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Type text into whatever has keyboard focus. ASCII is typed key by key (newline=Enter, tab=Tab); other characters are pasted via the clipboard.")
def desk_type(text: str, delay_ms: float = 4) -> str:
    try:
        return str(client.step("type", text=text, delay_ms=delay_ms))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Press key combos in order, e.g. ['ctrl+l'], ['ctrl+a', 'delete'], ['super'], ['alt+f4'], ['enter'].")
def desk_press(keys: list[str]) -> str:
    try:
        return str(client.step("press", keys=keys))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Run several steps in one call. Each step is a dict with 'do' (move click dblclick rightclick down up drag wheel type press keydown keyup wait) and its fields (x y to_x to_y dx dy text keys ms button count).")
def desk_steps(steps: list[dict]) -> str:
    try:
        return str(client.steps(steps))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Wait for the screen to settle (milliseconds, max 30000).")
def desk_wait(ms: float = 500) -> str:
    try:
        return str(client.step("wait", ms=ms))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Take the input lease so other agents cannot drive the mouse/keyboard while you work (ttl seconds). wait>0 blocks until free.")
def desk_lease(ttl: float = 60, note: str | None = None, wait: float = 0) -> str:
    try:
        return str(client.lease(ttl, note, wait))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Give the input lease back.")
def desk_release() -> str:
    try:
        return str(client.release())
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Read the clipboard (no text given) or put text on it (text given). Select something and press ctrl+c first to read it.")
def desk_clipboard(text: str | None = None) -> str:
    try:
        return client.clipboard_get() if text is None else str(client.clipboard_set(text))
    except DeskError as e:
        return _err(e)


@mcp.tool(description="Daemon state, screen size, pointer position, current lease holder.")
def desk_status() -> str:
    try:
        return str(client.status())
    except DeskError as e:
        return _err(e)


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
