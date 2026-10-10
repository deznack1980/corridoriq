"""Public crawl files. Production origin is always corridoriq.pro.

Private dashboards, admin routes, verification tokens, and APIs stay out
of the sitemap. robots.txt repeats those exclusions.
"""

from __future__ import annotations

PRODUCTION_ORIGIN = "https://corridoriq.pro"

PUBLIC_PATHS = (
    "/",
    "/explore.html",
    "/for-contractors.html",
    "/for-suppliers.html",
    "/register-contractor.html",
    "/register-supplier.html",
    "/login.html",
)

DISALLOW_PATHS = (
    "/api/",
    "/admin-dashboard.html",
    "/sales-dashboard.html",
    "/team-dashboard.html",
    "/estimator-work-queue.html",
    "/readonly-dashboard.html",
    "/user-management.html",
    "/catalog-admin.html",
    "/my-companies.html",
    "/my-tasks.html",
    "/opportunities.html",
    "/activity.html",
    "/assignments.html",
    "/reports.html",
    "/product-search.html",
    "/quote-compare.html",
    "/sales-company-profile.html",
    "/contractor-welcome.html",
    "/supplier-welcome.html",
    "/contractor-dashboard.html",
    "/contractor-home.html",
    "/contractor-account.html",
    "/contractor-join.html",
    "/verify-email.html",
    "/reset-password.html",
    "/join/",
)


def canonical(path: str) -> str:
    if path == "/":
        return PRODUCTION_ORIGIN + "/"
    return PRODUCTION_ORIGIN + path


def robots_txt() -> str:
    lines = [
        "User-agent: *",
        "Allow: /",
    ]
    lines.extend(f"Disallow: {path}" for path in DISALLOW_PATHS)
    lines.append(f"Sitemap: {PRODUCTION_ORIGIN}/sitemap.xml")
    lines.append("")
    return "\n".join(lines)


def sitemap_xml() -> str:
    urls = "\n".join(
        f"  <url>\n    <loc>{canonical(path)}</loc>\n    <changefreq>weekly</changefreq>\n  </url>"
        for path in PUBLIC_PATHS
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
