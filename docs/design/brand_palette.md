# Corridor IQ brand palette

Source of truth: the `:root` block in `portal.css`.

Public pages load `portal.css` then `site.css`. New components must use
these tokens. Do not introduce a second primary brand color.

## Approved values (recovered from Git)

Recovered from `d0b9278` (`portal.css` Sprint 6) and `style.css`.

| Token | Value | Use |
| --- | --- | --- |
| `--gold` / `--ci-primary` / `--accent` | `#c9a44c` | Primary brand, buttons |
| `--gold-strong` / `--ci-accent` | `#b8912f` | Hover / strong gold |
| `--gold-light` / `--ci-primary-hover` | `#e2c179` | Accent on dark, hover |
| `--gold-soft` / `--gold-dim` | `rgba(201, 164, 76, 0.14–0.15)` | Soft fills |
| `--gold-border` / `--ci-border` | `rgba(201, 164, 76, 0.35)` | Warm borders |
| `--ink` / `--navy-900` | `#0e1116` | Near-black text on gold, charcoal nav |
| `--navy-950` / `--ci-background` | `#0a0e1a` | Deep charcoal page chrome |
| `--ink-2` / `--navy-800` / `--ci-surface` | `#171b22` | Dark surfaces |
| `--ink-3` / `--navy-700` | `#212632` | Raised dark surfaces |
| `--charcoal` | `#1a1d27` | Supporting charcoal |
| `--on-dark` / `--ci-text` | `#e8eaf0` | Text on dark |
| `--on-dark-dim` / `--ci-text-muted` | `#9aa2b2` | Muted text on dark |

Gold buttons use dark ink text (`#0e1116`) for WCAG AA contrast.
Do not put white text on `#c9a44c`.

## Semantic colors (not brand)

| Token | Value | Use |
| --- | --- | --- |
| `--blue` | `#3b74c4` | Informational badges only |
| `--signal` / `--green` | `#13855a` | Positive / success |
| `--amber` | `#c98a1e` | Warning / attention |
| `--red` | `#d0574e` | Danger / error |

Blue is allowed for informational indicators. It is **not** Corridor IQ's
primary brand identity. Do not set `--accent` or `--gold` to `#2160d4`.

## Developer note

1. Use `var(--gold)`, `var(--accent)`, or `var(--ci-primary)` for brand
   actions — never a hardcoded blue hex.
2. Dark chrome uses `var(--navy-950)` / `var(--ink)`, not a blue navy.
3. `pipeline/tests/test_brand_tokens.py` fails if the unauthorized
   Option B primary (`#2160d4`) returns as `--accent` or `--gold`.
4. `pilot.css` duplicates the same hex values because those pages do not
   always load `portal.css`. Keep them in sync.

## Regression

Commit `6824c15` (Option B preview) retired gold and aliased `--gold` to
Corridor blue `#2160d4`. That change was not an approved brand update.
This restoration returns the Sprint 6 / `style.css` palette.
