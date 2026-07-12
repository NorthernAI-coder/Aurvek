"""Enable Cloudflare development mode for the configured Aurvek zones."""

from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv


API_URL_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/zones/{zone_id}/settings/"
    "development_mode"
)


def _load_configuration() -> tuple[dict[str, str], list[str]]:
    load_dotenv()
    api_key = os.getenv("CLOUDFLARE_API_KEY")
    email = os.getenv("CLOUDFLARE_EMAIL")
    zone_ids = [
        zone_id
        for zone_id in (
            os.getenv("CLOUDFLARE_ZONE_ID"),
            os.getenv("CLOUDFLARE_ZONE_ID_2"),
        )
        if zone_id
    ]

    if not api_key or not email:
        raise RuntimeError("Cloudflare credentials are not configured")
    if not zone_ids:
        raise RuntimeError("No Cloudflare zones are configured")

    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    return headers, zone_ids


def activate_development_mode(zone_id: str, headers: dict[str, str]) -> None:
    response = requests.patch(
        API_URL_TEMPLATE.format(zone_id=zone_id),
        headers=headers,
        json={"value": "on"},
        timeout=(5, 15),
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Cloudflare returned an invalid response") from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError("Cloudflare rejected the development mode request")

    print(f"Development mode enabled for zone {zone_id}.")


def main() -> int:
    try:
        headers, zone_ids = _load_configuration()
        for zone_id in zone_ids:
            activate_development_mode(zone_id, headers)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
