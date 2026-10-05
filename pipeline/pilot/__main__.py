"""Owner CLI for the contractor pilot.

    python -m pipeline.pilot --root <pilot_root> init-supplier --tenant-id <id> --name "<display name>"
    python -m pipeline.pilot --root <pilot_root> referral-code --supplier <id> --prefix <prefix> [--label ...]
    python -m pipeline.pilot --root <pilot_root> add-supplier-user --supplier <id> --email <email> [--name ...]
    python -m pipeline.pilot qr --url <public referral URL> --out <file.svg>
    python -m pipeline.pilot qr --base-url https://corridoriq.pro --code <code> [--utm-medium qr ...] --out <file.svg>
    python -m pipeline.pilot --root <pilot_root> funnel

Passwords are read interactively (or from PILOT_NEW_USER_PASSWORD for
scripted setup); they are never echoed or written anywhere but the hash.
--root defaults to CORRIDORIQ_PILOT_ROOT.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse

from pipeline.pilot import accounts, analytics, qr
from pipeline.pilot.platform import ENV_ROOT, Platform


def referral_url(base_url: str, code: str, utm: dict | None = None) -> str:
    if not accounts.REFERRAL_CODE_RE.fullmatch(code or ""):
        raise SystemExit("invalid referral code")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.query or parsed.fragment:
        raise SystemExit("base URL must be like https://corridoriq.pro")
    clean = analytics.clean_utm(utm or {})
    url = f"{base_url.rstrip('/')}/join/{code}"
    return url + ("?" + urlencode(clean) if clean else "")


def check_public_url(url: str) -> str:
    """A QR payload must be a plain public referral URL: no credentials,
    no tokens, nothing beyond the code and UTM values."""
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.netloc or p.username or p.password or p.fragment:
        raise SystemExit("QR URL must be a plain http(s) URL")
    parts = p.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "join" or not accounts.REFERRAL_CODE_RE.fullmatch(parts[1]):
        raise SystemExit("QR URL must be a /join/<referral code> URL")
    if p.query:
        keys = {kv.split("=", 1)[0] for kv in p.query.split("&") if kv}
        if not keys <= set(analytics.UTM_KEYS):
            raise SystemExit("QR URL may carry only utm_source/utm_medium/utm_campaign/utm_content")
    return url


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.pilot")
    ap.add_argument("--root", default=os.environ.get(ENV_ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init-supplier")
    s.add_argument("--tenant-id", required=True)
    s.add_argument("--name", required=True)
    s = sub.add_parser("referral-code")
    s.add_argument("--supplier", required=True)
    s.add_argument("--prefix", required=True)
    s.add_argument("--label")
    s = sub.add_parser("add-supplier-user")
    s.add_argument("--supplier", required=True)
    s.add_argument("--email", required=True)
    s.add_argument("--name", default="")
    s = sub.add_parser("qr")
    s.add_argument("--url")
    s.add_argument("--base-url")
    s.add_argument("--code")
    for k in analytics.UTM_KEYS:
        s.add_argument("--" + k.replace("_", "-"))
    s.add_argument("--out", required=True)
    sub.add_parser("funnel")
    args = ap.parse_args(argv)

    if args.cmd == "qr":
        if args.url:
            url = args.url
        else:
            if not (args.base_url and args.code):
                raise SystemExit("give --url, or --base-url and --code")
            url = referral_url(args.base_url, args.code,
                               {k: getattr(args, k) for k in analytics.UTM_KEYS if getattr(args, k)})
        url = check_public_url(url)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(qr.svg(url), encoding="utf-8")
        print(f"QR written to {out}\nencodes: {url}")
        return 0

    platform = Platform(args.root)
    conn = platform.connect()
    try:
        if args.cmd == "init-supplier":
            t = accounts.create_supplier(platform, args.tenant_id, args.name)
            print(f"supplier tenant {t.tenant_id} ({t.display_name})")
        elif args.cmd == "referral-code":
            code = accounts.create_referral_code(conn, platform, args.supplier, args.prefix, args.label)
            print(f"referral code: {code}")
        elif args.cmd == "add-supplier-user":
            pw = os.environ.get("PILOT_NEW_USER_PASSWORD") or getpass.getpass("Password (min 8): ")
            uid = accounts.add_supplier_user(conn, platform, args.supplier, args.email, pw, args.name)
            print(f"supplier inbox user {uid} created")
        elif args.cmd == "funnel":
            for r in analytics.funnel(conn):
                print(f"{r['channel']:<18} {r['referral_code'] or '-':<22} {r['event']:<34} "
                      f"events={r['n']} visitors={r['visitors']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
