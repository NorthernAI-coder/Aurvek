import json
import subprocess
import sys
import textwrap


def test_extracted_marketplace_routes_are_registered_from_marketplace_package():
    code = textwrap.dedent(
        """
        import json
        import app

        expected = {
            ("GET", "/explore"): "marketplace.routes.discovery",
            ("GET", "/api/explore/categories"): "marketplace.routes.discovery",
            ("GET", "/api/explore/prompts"): "marketplace.routes.discovery",
            ("GET", "/api/public-prompts"): "marketplace.routes.discovery",
            ("GET", "/my-storefront"): "marketplace.routes.storefronts",
            ("GET", "/api/creator-profile"): "marketplace.routes.storefronts",
            ("PUT", "/api/creator-profile"): "marketplace.routes.storefronts",
            ("POST", "/api/creator-profile/avatar"): "marketplace.routes.storefronts",
            ("GET", "/api/creator-profile/check-slug"): "marketplace.routes.storefronts",
            ("GET", "/store/{slug}"): "marketplace.routes.storefronts",
            ("GET", "/for-creators"): "marketplace.routes.marketing",
            ("GET", "/for-agencies"): "marketplace.routes.marketing",
            ("GET", "/explore-landing"): "marketplace.routes.marketing",
            ("GET", "/user/landing-analytics"): "marketplace.routes.analytics",
            ("POST", "/api/analytics/track-visit"): "marketplace.routes.analytics",
            ("POST", "/api/analytics/mark-conversion"): "marketplace.routes.analytics",
            ("GET", "/api/user/landing-analytics"): "marketplace.routes.analytics",
            ("GET", "/api/user/landing-analytics/{prompt_id}"): "marketplace.routes.analytics",
            ("GET", "/api/user/pack-landing-analytics"): "marketplace.routes.analytics",
            ("GET", "/api/user/pack-landing-analytics/{pack_id}"): "marketplace.routes.analytics",
            ("GET", "/my-earnings"): "marketplace.routes.analytics",
            ("GET", "/api/my-earnings"): "marketplace.routes.analytics",
            ("GET", "/admin/ranking"): "marketplace.routes.ranking",
            ("GET", "/api/admin/ranking-config"): "marketplace.routes.ranking",
            ("PUT", "/api/admin/ranking-config"): "marketplace.routes.ranking",
            ("POST", "/api/admin/ranking-recalculate"): "marketplace.routes.ranking",
            ("GET", "/pack-purchase-success"): "marketplace.routes.checkout",
            ("POST", "/api/prompts/{prompt_id}/purchase"): "marketplace.routes.checkout",
            ("GET", "/prompt-purchase-success"): "marketplace.routes.checkout",
            ("GET", "/admin/geo"): "marketplace.routes.geo",
            ("GET", "/api/admin/geo/status"): "marketplace.routes.geo",
            ("PUT", "/api/admin/geo/global"): "marketplace.routes.geo",
            ("POST", "/api/admin/geo/sync"): "marketplace.routes.geo",
            ("POST", "/api/admin/geo/enable-transforms"): "marketplace.routes.geo",
            ("DELETE", "/api/admin/geo/rules"): "marketplace.routes.geo",
            ("DELETE", "/api/admin/geo/landing/{public_id}"): "marketplace.routes.geo",
        }

        found = {}
        for method, path in expected:
            for route in app.app.routes:
                if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
                    found[f"{method} {path}"] = route.endpoint.__module__
                    break
            else:
                raise SystemExit(f"Route not found: {method} {path}")

        print("ROUTE_MODULES=" + json.dumps(found, sort_keys=True))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=True,
    )
    route_modules_line = next(
        line for line in result.stdout.splitlines() if line.startswith("ROUTE_MODULES=")
    )
    route_modules = json.loads(route_modules_line.removeprefix("ROUTE_MODULES="))

    assert route_modules == {
        "GET /explore": "marketplace.routes.discovery",
        "GET /api/explore/categories": "marketplace.routes.discovery",
        "GET /api/explore/prompts": "marketplace.routes.discovery",
        "GET /api/public-prompts": "marketplace.routes.discovery",
        "GET /my-storefront": "marketplace.routes.storefronts",
        "GET /api/creator-profile": "marketplace.routes.storefronts",
        "PUT /api/creator-profile": "marketplace.routes.storefronts",
        "POST /api/creator-profile/avatar": "marketplace.routes.storefronts",
        "GET /api/creator-profile/check-slug": "marketplace.routes.storefronts",
        "GET /store/{slug}": "marketplace.routes.storefronts",
        "GET /for-creators": "marketplace.routes.marketing",
        "GET /for-agencies": "marketplace.routes.marketing",
        "GET /explore-landing": "marketplace.routes.marketing",
        "GET /user/landing-analytics": "marketplace.routes.analytics",
        "POST /api/analytics/track-visit": "marketplace.routes.analytics",
        "POST /api/analytics/mark-conversion": "marketplace.routes.analytics",
        "GET /api/user/landing-analytics": "marketplace.routes.analytics",
        "GET /api/user/landing-analytics/{prompt_id}": "marketplace.routes.analytics",
        "GET /api/user/pack-landing-analytics": "marketplace.routes.analytics",
        "GET /api/user/pack-landing-analytics/{pack_id}": "marketplace.routes.analytics",
        "GET /my-earnings": "marketplace.routes.analytics",
        "GET /api/my-earnings": "marketplace.routes.analytics",
        "GET /admin/ranking": "marketplace.routes.ranking",
        "GET /api/admin/ranking-config": "marketplace.routes.ranking",
        "PUT /api/admin/ranking-config": "marketplace.routes.ranking",
        "POST /api/admin/ranking-recalculate": "marketplace.routes.ranking",
        "GET /pack-purchase-success": "marketplace.routes.checkout",
        "POST /api/prompts/{prompt_id}/purchase": "marketplace.routes.checkout",
        "GET /prompt-purchase-success": "marketplace.routes.checkout",
        "GET /admin/geo": "marketplace.routes.geo",
        "GET /api/admin/geo/status": "marketplace.routes.geo",
        "PUT /api/admin/geo/global": "marketplace.routes.geo",
        "POST /api/admin/geo/sync": "marketplace.routes.geo",
        "POST /api/admin/geo/enable-transforms": "marketplace.routes.geo",
        "DELETE /api/admin/geo/rules": "marketplace.routes.geo",
        "DELETE /api/admin/geo/landing/{public_id}": "marketplace.routes.geo",
    }
