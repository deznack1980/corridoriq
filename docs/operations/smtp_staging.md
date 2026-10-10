# SMTP staging — administrator action required

The application can send registration, verification, resend, and password
reset mail through SMTP. Credentials were **not present** on the
development workstation used for Phase 2. No provider was purchased and
no secrets were written to the repository.

## Safe status check

```
python -m pipeline.auth.mailer diagnose
```

The report includes `smtp_configured`, host/user/password *presence*
flags, port, and last SMTP status. It never prints the password, tokens,
or the username.

Operators with `admin.system` can also call `GET /api/admin/mail-status`.
`GET /api/health` only reports `{ configured, delivery_mode }`.

## Environment (staging or production host)

Copy `pipeline/auth/mail.example.env` into the host environment. Do not
commit a filled copy.

| Variable | Purpose |
| --- | --- |
| `CORRIDORIQ_PUBLIC_URL` | Must be `https://corridoriq.pro` in production so links in mail resolve |
| `CORRIDORIQ_SMTP_HOST` | Approved relay hostname |
| `CORRIDORIQ_SMTP_PORT` | `587` (STARTTLS) or `465` (SSL) |
| `CORRIDORIQ_SMTP_USE_SSL` | `1` only when using implicit TLS (usually port 465) |
| `CORRIDORIQ_SMTP_USER` | Relay username |
| `CORRIDORIQ_SMTP_PASSWORD` | Relay password — host secret store only |
| `CORRIDORIQ_SMTP_FROM` | Default `CorridorIQ <noreply@corridoriq.pro>` |
| `CORRIDORIQ_MAIL_OUTBOX` | Optional disk outbox. Token query strings are redacted on disk |

Until host, user, and password are all set, mail is written to the
in-memory outbox (and optional redacted disk copies). Registration and
login still succeed.

## Validation after credentials are installed

On staging, with a throwaway inbox the administrator controls:

1. Register a contractor — expect a verification message.
2. Open the link on `verify-email.html` — protected contractor routes unlock.
3. Use **Resend verification** from the welcome workspace.
4. Request a password reset and complete `reset-password.html`.
5. Register a supplier — same verification mail; live RFQs stay locked
   until an administrator approves the account.

Do not log message bodies. Do not paste credentials into tickets or git.
