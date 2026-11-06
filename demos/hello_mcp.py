"""A tiny MCP server with one tool, and a client that calls it.

    python demos/hello_mcp.py            # self-test: start the server, call its tool
    python demos/hello_mcp.py --serve    # just be the server (what Claude Code runs)

The self-test launches this same file with --serve as a subprocess and talks
to it over stdin/stdout — exactly how Claude Code talks to an MCP server. To
hand it to Claude Code for real:

    claude mcp add hello -- python demos/hello_mcp.py --serve

Note: the mcp 2.x SDK renamed FastMCP to MCPServer. Tutorials that import
`mcp.server.fastmcp` were written for 1.x and will fail here.
"""

import asyncio
import sys

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

server = MCPServer("hello")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def self_test():
    params = StdioServerParameters(command=sys.executable, args=[__file__, "--serve"])
    async with Client(params) as client:
        tools = await client.list_tools()
        print("Tools the server offers:", [tool.name for tool in tools.tools])

        result = await client.call_tool("add", {"a": 2, "b": 3})
        print("add(2, 3) =", result.content[0].text)

    print("✅ MCP works.")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        server.run()  # stdio
    else:
        asyncio.run(self_test())
