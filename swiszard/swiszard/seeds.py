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

    # ── Hermes natural-language phrasings (#304) ─────────────────────────────
    # file write
    ("please write a file at /tmp/test.txt", "handler_file_write"),
    ("save this to /tmp/output.txt", "handler_file_write"),
    ("write the following to /home/user/notes.txt", "handler_file_write"),
    ("create a file called /tmp/myfile.py with this content", "handler_file_write"),
    ("edit /home/user/config.yaml", "handler_file_write"),
    # file read (Hermes phrasing)
    ("show me what's in /home/user/README.md", "handler_file_read"),
    ("print the contents of /etc/hosts", "handler_file_read"),
    ("what does /home/user/.bashrc say", "handler_file_read"),
    # shell (Hermes phrasing)
    ("please run df -h", "handler_shell"),
    ("check memory usage", "handler_shell"),
    ("what processes are using the GPU", "handler_shell"),
    ("show git log for the last 5 commits", "handler_shell"),
    ("run pytest on the test suite", "handler_shell"),
    # web search (Hermes phrasing)
    ("look up swiszard architecture overview", "handler_web_search"),
    ("find the latest FastMCP docs", "handler_web_search"),
    ("search for 'EMA weight update' algorithms", "handler_web_search"),
    # memory (Hermes phrasing)
    ("what do you remember about LibbieAI pricing", "handler_memory"),
    ("recall anything about the swiszmem service", "handler_memory"),
    ("store this memory: Sean prefers JSON output", "handler_memory"),
    # AST
    ("find the swiszard_do function in router.py", "handler_ast"),
    ("rename function main to entry in server.py", "handler_ast"),
    ("wrap launch_wizard in server.py with tracelog", "handler_ast"),
    # PM
    ("add a task to the swiszard project", "handler_pm"),
    ("what is the current project status", "handler_pm"),
    ("mark node 313 as done", "handler_pm"),

]
