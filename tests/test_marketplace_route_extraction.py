import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


EXPECTED_ROUTE_MODULES = {
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
    ("GET", "/p/{public_id}/{slug}/static/{resource_path:path}"): "marketplace.routes.prompt_landings",
    ("GET", "/p/{public_id}/{slug}"): "marketplace.routes.prompt_landings",
    ("GET", "/p/{public_id}/{slug}/register"): "marketplace.routes.prompt_landings",
    ("GET", "/p/{public_id}/{slug}/login"): "marketplace.routes.prompt_landings",
    ("POST", "/p/{public_id}/{slug}/login"): "marketplace.routes.prompt_landings",
    ("GET", "/p/{public_id}/{slug}/"): "marketplace.routes.prompt_landings",
    ("GET", "/p/{public_id}/{slug}/{page}"): "marketplace.routes.prompt_landings",
    ("GET", "/internal/resolve-landing"): "marketplace.routes.prompt_landings",
    ("GET", "/{page:path}"): "marketplace.routes.prompt_landings",
    ("GET", "/landing/{prompt_id}"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/landing/{prompt_id}/pages"): "marketplace.routes.prompt_landing_builder",
    ("DELETE", "/api/landing/{prompt_id}/pages/{page_name}"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/landing/{prompt_id}/registration"): "marketplace.routes.prompt_landing_builder",
    ("PUT", "/api/landing/{prompt_id}/registration"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/landing/{prompt_id}/geo"): "marketplace.routes.prompt_landing_builder",
    ("PUT", "/api/landing/{prompt_id}/geo"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/landing/{prompt_id}/ai/generate"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/landing/{prompt_id}/files"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/landing/{prompt_id}/ai/status/{task_id}"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/landing/{prompt_id}/ai/active-job"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/landing/{prompt_id}/ai/modify"): "marketplace.routes.prompt_landing_builder",
    ("DELETE", "/api/landing/{prompt_id}/files"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/welcome/{prompt_id}/ai/generate"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/welcome/{prompt_id}/ai/modify"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/welcome/{prompt_id}/ai/status/{task_id}"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/welcome/{prompt_id}/ai/active-job"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/welcome/{prompt_id}/files"): "marketplace.routes.prompt_landing_builder",
    ("DELETE", "/api/welcome/{prompt_id}/files"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/landing/{prompt_id}/pages/{section}/edit"): "marketplace.routes.prompt_landing_builder",
    ("PUT", "/api/landing/{prompt_id}/pages/{section}"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/landing/{prompt_id}/components"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/landing/{prompt_id}/components/{component_type}/{component_name}/edit"): "marketplace.routes.prompt_landing_builder",
    ("PUT", "/api/landing/{prompt_id}/components/{component_type}/{component_name}"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/landing/{prompt_id}/components"): "marketplace.routes.prompt_landing_builder",
    ("DELETE", "/api/landing/{prompt_id}/components/{component_type}/{component_name}"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/api/landing/{prompt_id}/images"): "marketplace.routes.prompt_landing_builder",
    ("POST", "/api/landing/{prompt_id}/images"): "marketplace.routes.prompt_landing_builder",
    ("DELETE", "/api/landing/{prompt_id}/images/{image_id}"): "marketplace.routes.prompt_landing_builder",
    ("GET", "/claim-entitlement/{token}"): "marketplace.routes.acquisition",
    ("POST", "/api/register-pack"): "marketplace.routes.acquisition",
}


def test_extracted_marketplace_routes_are_registered_from_marketplace_package():
    code = textwrap.dedent(
        """
        import json
        import app

        expected = EXPECTED_ROUTE_MODULES_PLACEHOLDER

        found = {}
        duplicates = []
        for method, path in expected:
            matches = []
            for route in app.app.routes:
                if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
                    matches.append(route.endpoint.__module__)
            if not matches:
                raise SystemExit(f"Route not found: {method} {path}")
            if len(matches) > 1:
                duplicates.append(f"{method} {path}: {matches}")
            found[f"{method} {path}"] = matches[0]

        if duplicates:
            raise SystemExit("Duplicate extracted routes: " + "; ".join(duplicates))

        catchall_indices = [
            index
            for index, route in enumerate(app.app.routes)
            if getattr(route, "path", None) == "/{page:path}"
            and "GET" in getattr(route, "methods", set())
        ]
        if catchall_indices != [len(app.app.routes) - 1]:
            raise SystemExit(f"Custom-domain catch-all is not last: {catchall_indices}")

        print("ROUTE_MODULES=" + json.dumps(found, sort_keys=True))
        """
    ).replace("EXPECTED_ROUTE_MODULES_PLACEHOLDER", repr(EXPECTED_ROUTE_MODULES))
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
        f"{method} {path}": module
        for (method, path), module in EXPECTED_ROUTE_MODULES.items()
    }


def test_phase1_modules_do_not_import_app():
    repo_root = Path(__file__).resolve().parents[1]
    paths = [
        "auth_flows.py",
        "prompts.py",
        "marketplace/landing/cache.py",
        "marketplace/landing/paths.py",
        "marketplace/landing/rendering.py",
        "marketplace/routes/acquisition.py",
        "marketplace/routes/prompt_landing_builder.py",
        "marketplace/routes/prompt_landings.py",
        "marketplace/services/acquisition_context.py",
        "marketplace/services/landing_registration.py",
        "marketplace/services/pending_entitlements.py",
        "marketplace/services/pending_registrations.py",
    ]

    for relative_path in paths:
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "from app import" not in text, relative_path
        assert "import app" not in text, relative_path


@pytest.mark.asyncio
async def test_public_landing_static_rejects_paths_outside_static_root(tmp_path, monkeypatch):
    from marketplace.routes import prompt_landings

    prompt_dir = tmp_path / "prompt"
    static_dir = prompt_dir / "static"
    static_dir.mkdir(parents=True)
    (prompt_dir / "outside.css").write_text("body{}", encoding="utf-8")

    async def fake_landing_path(public_id):
        return {
            "prompt_id": 1,
            "prompt_name": "Demo",
            "is_unlisted": 0,
            "username": "demo",
            "path": prompt_dir,
        }

    async def fake_custom_domain(prompt_id):
        return None

    monkeypatch.setattr(prompt_landings, "require_public_landings_enabled", lambda: None)
    monkeypatch.setattr(prompt_landings, "get_landing_path_cached", fake_landing_path)
    monkeypatch.setattr(prompt_landings, "get_active_custom_domain", fake_custom_domain)

    response = await prompt_landings.public_landing_static(
        public_id="AbCd1234",
        slug="demo",
        resource_path="../outside.css",
    )

    assert response.status_code == 404
