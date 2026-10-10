# Google Search Console — production submission

Production origin: `https://corridoriq.pro`

Do not submit localhost, staging, or employee hosts. Do this only after
the Phase 2 branch is approved and the production host is serving the
generated `robots.txt` and `sitemap.xml`.

## What is published

Public marketing and account-entry pages:

- `https://corridoriq.pro/`
- `https://corridoriq.pro/explore.html`
- `https://corridoriq.pro/for-contractors.html`
- `https://corridoriq.pro/for-suppliers.html`
- `https://corridoriq.pro/register-contractor.html`
- `https://corridoriq.pro/register-supplier.html`
- `https://corridoriq.pro/login.html`

Private dashboards, verification links, password reset, APIs, and
administrator pages are listed in `robots.txt` as `Disallow` and are
absent from `sitemap.xml`. Welcome workspaces also send `noindex`.

## Operator steps

1. Confirm production is serving:
   - `https://corridoriq.pro/robots.txt`
   - `https://corridoriq.pro/sitemap.xml`
2. Confirm the sitemap `<loc>` values use `https://corridoriq.pro`, not
   a staging host.
3. In [Google Search Console](https://search.google.com/search-console):
   1. Add the URL-prefix property `https://corridoriq.pro`.
   2. Complete ownership verification (HTML file, DNS TXT, or the
      existing host meta tag — choose the method the domain operator
      already uses).
   3. Open **Sitemaps** and submit `https://corridoriq.pro/sitemap.xml`.
4. Use **URL Inspection** on `/`, `/explore.html`, and
   `/for-contractors.html` after first crawl.
5. Do not request indexing for `/contractor-welcome.html`,
   `/supplier-welcome.html`, `/verify-email.html`, `/reset-password.html`,
   `/admin-dashboard.html`, or any `/api/` path.

No paid Search Console feature is required.
