"""
Plugin template. Files starting with "_" are skipped, so copy this to e.g.
plugins/my_stuff.py, edit, then say "Jarvis, reload integrations".

Rules of thumb:
- The description is what the AI reads to decide when to use your tool.
  Say WHEN to use it, not just what it does.
- Return a short string (or ToolResult). Jarvis reads it and answers you.
- Anything risky? Pass confirm=always("...reason...") and Jarvis will ask
  you before running it. mutates=True makes strict mode ask too.
- Secrets: put them in backend/.env and read them with os.environ["NAME"].
"""

import os  # noqa: F401

import requests  # noqa: F401

from app.tools import ToolResult, always, tool  # noqa: F401


@tool(
    "my_tool_name",  # letters, numbers, underscores
    "What it does and WHEN Jarvis should use it.",
    {
        "city": {"type": "string", "description": "Shown to the AI so it fills this correctly"},
        "count": {"type": "integer"},
    },
    ["city"],  # required parameters
    category="plugin",
    # confirm=always("This will post publicly. Continue?"),
)
def my_tool_name(city: str, count: int = 3) -> str:
    return f"Pretend result for {city} x{count}"
