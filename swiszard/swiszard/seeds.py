"""
seeds.py — Initial example phrasings used to bootstrap the routing database.

Each entry is (phrasing, handler_name).  On first run the router calls
_ensure_seeded() which embeds every phrasing and writes it to routes.db.
"""
from __future__ import annotations

SEED_EXAMPLES: list[tuple[str, str]] = [
    # ── handler_file_read ────────────────────────────────────────────────────
    ("read the file at /etc/hostname", "handler_file_read"),
    ("show me the contents of /tmp/test.txt", "handler_file_read"),
    ("cat this file: /var/log/syslog", "handler_file_read"),
    ("what's in /home/user/notes.txt", "handler_file_read"),
    ("open /etc/hosts and tell me what it says", "handler_file_read"),

    # ── handler_file_find ────────────────────────────────────────────────────
    ("find all python files in /home", "handler_file_find"),
    ("search for files named config.yaml under /etc", "handler_file_find"),
    ("locate all .log files in /var/log", "handler_file_find"),
    ("grep for 'error' in /var/log/syslog", "handler_file_find"),
    ("find files containing the word 'password' in /home/user", "handler_file_find"),

    # ── handler_shell ────────────────────────────────────────────────────────
    ("run `ls -la /tmp`", "handler_shell"),
    ("execute `df -h` and show me the output", "handler_shell"),
    ("run the command `ps aux | grep python`", "handler_shell"),
    ("`uptime` what does it say", "handler_shell"),
    ("run `free -m` to check memory", "handler_shell"),

    # ── handler_web_search ───────────────────────────────────────────────────
    ("search the web for python asyncio tutorial", "handler_web_search"),
    ("look up the latest news about linux kernel", "handler_web_search"),
    ("find online documentation for FastMCP", "handler_web_search"),
    ("web search: how to install docker on ubuntu", "handler_web_search"),
    ("google sentence transformers huggingface", "handler_web_search"),

    # ── handler_memory ───────────────────────────────────────────────────────
    ("memory recall what do you know about python preferences", "handler_memory"),
    ("memory recall recent decisions about the swiszard", "handler_memory"),
    ("memory recall Sean's preferences", "handler_memory"),
    ("remember that Sean prefers tabs over spaces", "handler_memory"),
    ("remember this: always use black for formatting", "handler_memory"),
    ("memory forget id 42", "handler_memory"),
    ("memory status how many memories are stored", "handler_memory"),
    ("skill view hermes-agent", "handler_skill"),
    ("list all skills", "handler_skill"),
    ("create a new skill called my-skill", "handler_skill"),
    ("patch the hermes-agent skill", "handler_skill"),
    ("delete the test skill", "handler_skill"),

]
