import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap


EXPECTED_ROUTE_MODULES = {
    ("GET", "/payment"): "billing.routes.wallet",
    ("POST", "/api/stripe/create-checkout-session"): "billing.routes.wallet",
    ("GET", "/payment-success"): "billing.routes.wallet",
    ("POST", "/api/payment/free-credit"): "billing.routes.wallet",
    ("GET", "/admin/create-discount"): "billing.routes.discounts",
    ("POST", "/process-discount"): "billing.routes.discounts",
    ("POST", "/apply-discount"): "billing.routes.discounts",
    ("GET", "/admin/discount-list"): "billing.routes.discounts",
    ("GET", "/admin/get-discount/{code}"): "billing.routes.discounts",
    ("POST", "/admin/update-discount"): "billing.routes.discounts",
    ("DELETE", "/admin/delete-discount/{code}"): "billing.routes.discounts",
    ("POST", "/api/creator/request-payout"): "billing.routes.creator_payouts",
    ("POST", "/api/connect/onboard"): "billing.routes.creator_payouts",
    ("GET", "/api/connect/return"): "billing.routes.creator_payouts",
    ("GET", "/api/connect/refresh"): "billing.routes.creator_payouts",
    ("GET", "/api/connect/status"): "billing.routes.creator_payouts",
    ("POST", "/api/stripe/webhook"): "billing.stripe_webhooks",
}


def test_billing_routes_are_registered_from_billing_package():
    code = textwrap.dedent(
        """
        import json
        import app

        expected = EXPECTED_ROUTE_MODULES_PLACEHOLDER
        found = {}
        duplicates = []

        for method, path in expected:
            matches = [
                route.endpoint.__module__
                for route in app.app.routes
                if getattr(route, "path", None) == path
                and method in getattr(route, "methods", set())
            ]
            if not matches:
                raise SystemExit(f"Route not found: {method} {path}")
            if len(matches) > 1:
                duplicates.append(f"{method} {path}: {matches}")
            found[f"{method} {path}"] = matches[0]

        if duplicates:
            raise SystemExit("Duplicate billing routes: " + "; ".join(duplicates))

        webhook_matches = [
            route.endpoint.__module__
            for route in app.app.routes
            if getattr(route, "path", None) == "/api/stripe/webhook"
            and "POST" in getattr(route, "methods", set())
        ]
        if webhook_matches != ["billing.stripe_webhooks"]:
            raise SystemExit(f"Unexpected Stripe webhook registrations: {webhook_matches}")

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


def test_billing_code_does_not_import_app_or_leave_route_bodies_in_app():
    repo_root = Path(__file__).resolve().parents[1]
    app_text = (repo_root / "app.py").read_text(encoding="utf-8")

    removed_decorators = [
        '@app.get("/payment"',
        '@app.post("/api/stripe/create-checkout-session"',
        '@app.post("/api/stripe/webhook"',
        '@app.get("/payment-success"',
        '@app.post("/api/payment/free-credit"',
        '@app.get("/admin/create-discount"',
        '@app.post("/process-discount"',
        '@app.post("/apply-discount"',
        '@app.get("/admin/discount-list"',
        '@app.get("/admin/get-discount/{code}"',
        '@app.post("/admin/update-discount"',
        '@app.delete("/admin/delete-discount/{code}"',
        '@app.post("/api/creator/request-payout"',
        '@app.post("/api/connect/onboard"',
        '@app.get("/api/connect/return"',
        '@app.get("/api/connect/refresh"',
        '@app.get("/api/connect/status"',
    ]
    for decorator in removed_decorators:
        assert decorator not in app_text

    stale_imports = re.compile(
        r"^\s*(?:from\s+app\s+import\b|import\s+app\b)",
        re.MULTILINE,
    )
    checked_paths = list((repo_root / "billing").rglob("*.py"))
    checked_paths.append(repo_root / "marketplace" / "payments" / "checkout_webhooks.py")
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        assert not stale_imports.search(text), path.relative_to(repo_root)


def test_billing_payment_safety_guards_stay_in_extracted_modules():
    repo_root = Path(__file__).resolve().parents[1]

    creator_payouts = (repo_root / "billing" / "creator_payouts.py").read_text(encoding="utf-8")
    assert 'await conn.execute("BEGIN IMMEDIATE")' in creator_payouts
    assert "idempotency_key=idempotency_key" in creator_payouts
    assert "payout_pending" in creator_payouts
    assert "payout_processing" in creator_payouts
    assert "payout_reference" in creator_payouts
    assert "_is_ambiguous_stripe_error" in creator_payouts
    assert "PAYOUT_IDEMPOTENCY_RETRY_WINDOW_SECONDS" in creator_payouts
    assert "update_cursor.rowcount == 0" in creator_payouts
    assert 'txn[3] not in {"payout_completed", "payout_pending", "payout_processing"}' in creator_payouts

    connect = (repo_root / "billing" / "connect.py").read_text(encoding="utf-8")
    assert "get_auth_base_url" in connect
    assert "get_request_base_url" not in connect

    stripe_webhooks = (repo_root / "billing" / "stripe_webhooks.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(status_code=500, detail="Error processing chargeback")' in stripe_webhooks
    assert "except HTTPException:" in stripe_webhooks
    assert 'event_type == "checkout.session.expired"' in stripe_webhooks

    checkout_webhooks = (
        repo_root / "marketplace" / "payments" / "checkout_webhooks.py"
    ).read_text(encoding="utf-8")
    assert "decrement_discount_usage" not in checkout_webhooks
    assert "Original pack purchase has not been processed yet" in checkout_webhooks
    assert "Original prompt purchase has not been processed yet" in checkout_webhooks

    for relative_path in ("billing/wallet.py", "marketplace/routes/checkout.py", "marketplace/routes/packs.py"):
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "claim_discount_usage_for_checkout" in text
        assert "restore_discount_usage_for_checkout" in text
        assert "discount_claimed" in text

    wallet = (repo_root / "billing" / "wallet.py").read_text(encoding="utf-8")
    assert "Original payment transaction has not been processed yet" in wallet

    discounts = (repo_root / "billing" / "discounts.py").read_text(encoding="utf-8")
    assert "restore_discount_usage_for_expired_session" in discounts
    assert "discount_restored" in discounts
