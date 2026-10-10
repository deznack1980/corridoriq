# Trusted proxy configuration (Railway + Cloudflare)

The portal rate-limits login, registration, password reset, and
verification resend by **resolved client IP** and by **email**.

## Default (safe)

`CORRIDORIQ_TRUST_PROXY` is off. `X-Forwarded-For` and
`CF-Connecting-IP` are ignored. The TCP peer is used.

That is correct for local development. It is **incorrect** for a host
that only sees Railway's or Cloudflare's address: every customer would
share one bucket (8 registrations / hour, 5 resets / hour).

## Do not enable trust yet

`CORRIDORIQ_TRUST_PROXY` must stay **off** until the owner confirms the
origin is unreachable except through the intended proxy.

Observed from public DNS and HTTPS (2026-10-10):

- Cloudflare hosts DNS for `corridoriq.pro` but HTTP responses come from
  Railway (`server: railway-hikari`). No `cf-ray` header.
- That means Cloudflare is not in the HTTP path. `CF-Connecting-IP` is
  not a trustworthy signal today.
- `CORRIDORIQ_TRUSTED_PROXIES=loopback,private` is **too broad**. Any
  RFC1918 peer could then spoof `X-Forwarded-For`.

## Narrower setting (after origin isolation is proven)

1. Confirm the container/process only accepts connections from Railway's
   edge (no public bind, no second ingress).
2. From one application log line, record the TCP peer Railway uses.
3. Set that hop only:

```
CORRIDORIQ_TRUST_PROXY=1
CORRIDORIQ_TRUSTED_PROXIES=127.0.0.1,<observed-peer>/32
```

4. Do not add `private` and do not enable Cloudflare header trust until
   the orange-cloud proxy is actually in front of the origin **and**
   the origin IP is firewalled to Cloudflare.

Until then, keep trust off. Per-email buckets still apply. Shared-edge
IPs will share the IP bucket (8 registrations / hour). That is safer
than opening spoofing.
