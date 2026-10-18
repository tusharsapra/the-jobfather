"""A tiny MCP server used as the proxy's child in tests (fictional tools only)."""
import os
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

app = MCPServer('fake-child', version='9.9.9')


@app.tool()
def echo(text: str) -> str:
    """Return the text."""
    return 'echo:' + text


@app.tool()
async def count(n: int, ctx: Context) -> str:
    """Report progress n times, log once, then return."""
    for step in range(1, n + 1):
        await ctx.report_progress(step, n, 'step %d' % step)
    await ctx.info('counted to %d' % n)
    return 'counted:%d' % n


@app.tool()
def crash() -> str:
    """Exit the process without answering (simulates a crashed server)."""
    sys.stderr.flush()
    os._exit(3)


@app.tool()
def send_message(to: str, body: str) -> str:
    """Never reachable through the proxy: not on the allowlist."""
    return 'SENT to ' + to


if __name__ == '__main__':
    app.run()
