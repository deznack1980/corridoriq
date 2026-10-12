"""Read-only diagnosis of "signup says check your email but nothing arrives".

    python scripts/ops/signup_delivery_diagnostic.py command <address>   # print the railway command
    python scripts/ops/signup_delivery_diagnostic.py local <address>     # run against local env roots

The contractor signup (pipeline.pilot.accounts.request_contractor_signup) always
answers "check_email" and records the real outcome only in the pilot database's
security_audit_log (event email_verification_requested, details.result):

  sent                -> Postmark accepted the message (check Postmark Activity,
                         suppressions, and the mailbox)
  mail_unavailable    -> Postmark rejected or was unreachable (account pending
                         approval, sender/domain not verified, inactive recipient,
                         bad token); the link was revoked
  already_registered  -> the address already has an account; nothing is sent
  (no rows)           -> the per-email throttle (5 per 10 minutes) answered before
                         the signup ran, or the request never reached production

The container script opens the database read-only and prints only timestamps,
result codes, a yes/no for an existing account and 24-hour totals. It never
prints names, other addresses, tokens or secrets.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

CONTAINER_SCRIPT = r'''
import json, os, sqlite3, sys
from datetime import datetime, timedelta, timezone
addr = sys.argv[1].strip().lower()
p = os.path.join(os.environ["CORRIDORIQ_PILOT_ROOT"], "platform.db")
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
rows = c.execute(
    "SELECT created_at, json_extract(details_json,'$.result') FROM security_audit_log "
    "WHERE event_type='email_verification_requested' AND lower(json_extract(details_json,'$.email'))=? "
    "ORDER BY id DESC LIMIT 15", (addr,)).fetchall()
since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
totals = dict(c.execute(
    "SELECT json_extract(details_json,'$.result'), COUNT(*) FROM security_audit_log "
    "WHERE event_type='email_verification_requested' AND created_at >= ? GROUP BY 1", (since,)).fetchall())
exists = c.execute("SELECT COUNT(*) FROM users WHERE normalized_email=?", (addr,)).fetchone()[0] > 0
print(json.dumps({"address_has_account": exists,
                  "attempts_for_address": [{"at": a, "result": r} for a, r in rows],
                  "all_signups_last_24h_by_result": totals}, indent=2))
'''


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in ("command", "local"):
        print(__doc__)
        return 2
    mode, address = argv
    if mode == "command":
        print(f"railway ssh -- python3 -c {shlex.quote(CONTAINER_SCRIPT)} {shlex.quote(address)}")
        return 0
    return subprocess.run([sys.executable, "-c", CONTAINER_SCRIPT, address], env=os.environ).returncode


if __name__ == "__main__":
    sys.exit(main())
