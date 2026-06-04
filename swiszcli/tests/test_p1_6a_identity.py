"""P1.6a identity sentinel: write/read round-trip + edge cases."""
import os, sys, json, pathlib
sys.path.insert(0, "/home/ziggibot/swiszcli")
os.environ["SWISZCLI_USER_NAME"] = "Sean"

from swiszcli import identity
identity.reset_cache()
from swiszcli.identity import to_storage, to_render, SENTINEL, SENTINEL_LOWER, get_user_name

assert get_user_name() == "Sean"

# basic round-trip
s = "Sean wants ice cream."
stored = to_storage(s)
assert stored == "{{user}} wants ice cream.", stored
assert to_render(stored) == "Sean wants ice cream."

# lowercase preserved through sentinel
s2 = "talked to sean about it"
stored2 = to_storage(s2)
assert stored2 == "talked to {{user_lower}} about it", stored2
assert to_render(stored2) == "talked to sean about it"

# false-positive immunity: should NOT touch "the user table"
s3 = "the user table has 5 columns"
stored3 = to_storage(s3)
assert stored3 == s3, f"corrupted: {stored3}"
assert to_render(stored3) == s3

# substring of name should NOT match (word boundary)
s4 = "Seansational performance by Seanie"
stored4 = to_storage(s4)
assert "{{user}}" not in stored4, f"matched substring: {stored4}"

# Mixed case in same string
s5 = "Sean said sean was Sean"
stored5 = to_storage(s5)
# expected: lower first → user_lower, then any-case → user
assert "{{user_lower}}" in stored5
assert "{{user}}" in stored5
restored = to_render(stored5)
# should restore something equivalent in content (case approximately preserved)
assert "Sean" in restored and "sean" in restored, restored

# idempotency: to_storage twice == to_storage once
assert to_storage(stored) == stored, "to_storage not idempotent"
# to_render twice == to_render once
once = to_render(stored)
assert to_render(once) == once, "to_render not idempotent"

# empty / None safe
assert to_storage("") == ""
assert to_render("") == ""

# disabled when no user_name
os.environ.pop("SWISZCLI_USER_NAME")
identity.reset_cache()
# also prevent identity.json from breaking the test
idfile = pathlib.Path.home() / ".swiszcli" / "identity.json"
backup = None
if idfile.exists():
    backup = idfile.read_text()
    idfile.unlink()
try:
    assert get_user_name() is None
    assert to_storage("Sean is here") == "Sean is here"
    assert to_render("hi {{user}}") == "hi {{user}}"
finally:
    if backup is not None:
        idfile.write_text(backup)
    os.environ["SWISZCLI_USER_NAME"] = "Sean"
    identity.reset_cache()

print("P1.6a identity tests PASS")
