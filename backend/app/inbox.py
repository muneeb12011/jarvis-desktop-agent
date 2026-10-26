"""
File inbox: drop a .txt file into backend/inbox/ and Jarvis runs it as a
command; the answer is appended to backend/inbox/replies.log.

Handy for automation that can write files but can't call an API: a phone
sync folder (OneDrive/Dropbox) pointed at the inbox, a scheduled script, a
build step that tells Jarvis "announce: tests passed".
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from . import config

logger = logging.getLogger(__name__)

INBOX = config.DATA_DIR / "inbox"


async def run_inbox(agent, broadcast: Callable[[dict], Awaitable[None]]) -> None:
    INBOX.mkdir(exist_ok=True)
    while True:
        try:
            for path in sorted(INBOX.glob("*.txt"), key=lambda p: p.stat().st_mtime):
                try:
                    text = path.read_text(encoding="utf-8-sig").strip()
                    path.unlink()
                except OSError:
                    continue  # still being written; try next tick
                if not text:
                    continue
                logger.info("Inbox command from %s: %s", path.name, text[:120])
                while agent.busy:
                    await asyncio.sleep(0.5)
                await broadcast({"type": "external", "text": text, "source": f"inbox/{path.name}"})
                await agent.submit(text, voice=False)
                if agent.task:
                    try:
                        await asyncio.wait_for(asyncio.shield(agent.task), timeout=300)
                    except TimeoutError:
                        pass
                stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with (INBOX / "replies.log").open("a", encoding="utf-8") as f:
                    f.write(f"[{stamp}] {path.name}\n> {text}\n{agent.last_reply}\n\n")
        except Exception:
            logger.exception("Inbox tick failed")
        await asyncio.sleep(1.0)
