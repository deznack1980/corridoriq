# Trusted proxy configuration (Railway + Cloudflare)

The portal rate-limits login, registration, password reset, and
verification resend by **resolved client IP** and by **email**.

## Default (safe)

`CORRIDORIQ_TRUST_PROXY` is off. `X-Forwarded-For` and
`CF-Connecting-IP` are ignored. The TCP peer is used.

That is correct for local development. It is **incorrect** for a host
that only sees Railway's or Cloudflare's address: every customer would
share one bucket (8 registrations / hour, 5 resets / hour).

## Required for production / staging behind a reverse proxy

1. Do not expose the origin port to the public internet. Only the proxy
   may connect.
2. Set:

```
CORRIDORIQ_TRUST_PROXY=1
CORRIDORIQ_TRUSTED_PROXIES=loopback,private
```

Railway's hop is typically a private address. Cloudflare's
`CF-Connecting-IP` is then used as the client identity. If Cloudflare
is not present, the untrusted address in `X-Forwarded-For` is used.

3. Optional: replace `private` with the explicit proxy CIDRs.

Do not enable `TRUST_PROXY` on an origin that accepts connections from
the public internet. Clients could then spoof forwarded headers.

Limits themselves are unchanged. This setting only chooses which address
is keyed.
