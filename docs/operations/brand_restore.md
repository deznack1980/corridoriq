# Brand restore — amber-gold / charcoal

Branch: `feature/frictionless-onboarding`. Visual only. Auth, APIs, and
schema were not changed.

## Cause

`6824c15` (Option B procurement preview) retired gold in `portal.css` and
aliased `--gold` to Corridor blue `#2160d4`. Public, auth, and welcome
pages load those tokens, so the site appeared blue.

## Restore

Approved hex values came from `d0b9278` and `style.css` (`#c9a44c`,
`#b8912f`, `#e2c179`, `#0e1116`, `#0a0e1a`). `--accent` again equals
`--gold`. `--blue` (`#3b74c4`) remains informational.

See `docs/design/brand_palette.md`.
