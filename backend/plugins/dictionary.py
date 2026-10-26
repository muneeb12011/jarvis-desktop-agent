"""
Example plugin: word definitions (free dictionaryapi.dev, no key).

Every .py file in backend/plugins/ is loaded at startup and on
"Jarvis, reload integrations". Copy _template.py to start your own.
"""

import requests

from app.tools import tool


@tool(
    "define_word",
    "Dictionary lookup: meaning, part of speech and an example for an English word.",
    {"word": {"type": "string"}},
    ["word"],
    category="plugin",
)
def define_word(word: str) -> str:
    resp = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word.strip()}", timeout=10)
    if resp.status_code == 404:
        return f"No dictionary entry for '{word}'."
    resp.raise_for_status()
    entry = resp.json()[0]
    lines = [entry.get("word", word)]
    for meaning in entry.get("meanings", [])[:3]:
        d = meaning["definitions"][0]
        line = f"({meaning['partOfSpeech']}) {d['definition']}"
        if d.get("example"):
            line += f' e.g. "{d["example"]}"'
        lines.append(line)
    return "\n".join(lines)
