"""Lemon Squeezy: checkout links, the customer portal, and what its webhooks mean.

Every webhook is signed (HMAC-SHA256 of the raw body with the signing secret); unsigned or
wrongly signed ones are refused. Credit is granted per order or invoice id, so a webhook
that arrives twice adds credit once.
"""

from __future__ import annotations

import hashlib
import hmac

import httpx

from .config import CloudSettings
from .store import Store, micros

API = "https://api.lemonsqueezy.com/v1"
ACTIVE = {"active", "on_trial", "past_due", "paused", "cancelled"}  # Still paid up for now.


class LemonError(RuntimeError):
    pass


def signed(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class Lemon:
    def __init__(
        self, settings: CloudSettings, store: Store, http: httpx.AsyncClient | None = None
    ):
        self.settings, self.store, self.http = settings, store, http

    def _headers(self) -> dict:
        key = self.settings.lemonsqueezy_api_key.get_secret_value()
        if not key:
            raise LemonError("Payments aren't set up on this server yet.")
        return {
            "Authorization": f"Bearer {key}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        }

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        client = self.http or httpx.AsyncClient(timeout=20)
        try:
            response = await client.request(method, API + path, headers=self._headers(), **kwargs)
        except httpx.HTTPError:
            raise LemonError("Couldn't reach the payment service. Try again.") from None
        finally:
            if self.http is None:
                await client.aclose()
        if response.status_code >= 400:
            raise LemonError("The payment service said no. Try again later.")
        return response.json()

    # For the app ----------------------------------------------------------------------------

    async def checkout(self, account: dict, plan_key: str) -> str:
        plan = self.settings.plan(key=plan_key)
        if plan is None or not plan.variant_id:
            raise LemonError("That plan isn't for sale yet.")
        body = {
            "data": {
                "type": "checkouts",
                "attributes": {
                    "checkout_data": {"custom": {"account_id": account["id"]}},
                    "product_options": {
                        "redirect_url": f"{self.settings.public_url}/thanks",
                    },
                },
                "relationships": {
                    "store": {
                        "data": {"type": "stores", "id": self.settings.lemonsqueezy_store_id}
                    },
                    "variant": {"data": {"type": "variants", "id": plan.variant_id}},
                },
            }
        }
        data = await self._call("POST", "/checkouts", json=body)
        return data["data"]["attributes"]["url"]

    async def portal(self, account: dict) -> str:
        if not account.get("subscription_id"):
            raise LemonError("There's no subscription to manage yet.")
        data = await self._call("GET", f"/subscriptions/{int(account['subscription_id'])}")
        url = ((data["data"]["attributes"].get("urls") or {}).get("customer_portal")) or ""
        if not url:
            raise LemonError("The payment service has no page for this subscription.")
        return url

    # Webhooks --------------------------------------------------------------------------------

    def _account(self, meta: dict, attributes: dict) -> dict | None:
        custom = meta.get("custom_data") or {}
        if custom.get("account_id"):
            found = self.store.account(str(custom["account_id"]))
            if found:
                return found
        subscription = attributes.get("subscription_id")
        if subscription:
            return self.store.account_for_subscription(str(subscription))
        return None

    def handle(self, payload: dict) -> str:
        """Apply one webhook. Returns what happened, in a few words (for the log)."""
        meta = payload.get("meta") or {}
        event = meta.get("event_name", "")
        data = payload.get("data") or {}
        item_id = str(data.get("id", ""))
        attributes = data.get("attributes") or {}
        if (meta.get("test_mode") or attributes.get("test_mode")) and not (
            self.settings.accept_test_payments
        ):
            return "test payment ignored"
        account = self._account(meta, attributes)
        if account is None:
            return "no account"
        if event == "order_created":
            if attributes.get("status") != "paid":
                return "unpaid"
            item = attributes.get("first_order_item") or {}
            plan = self.settings.plan(variant_id=str(item.get("variant_id", "")))
            if plan is None or plan.monthly:
                return "not a top-up"  # Plans get credit from their invoices.
            self.store.update_account(
                account["id"], customer_id=str(attributes.get("customer_id", ""))
            )
            added = self.store.add(
                account["id"], micros(plan.credit), "topup", f"order:{item_id}", plan.name
            )
            return "top-up" if added else "duplicate"
        if event == "order_refunded":
            item = attributes.get("first_order_item") or {}
            plan = self.settings.plan(variant_id=str(item.get("variant_id", "")))
            if plan is None or plan.monthly:
                return "not a top-up"
            self.store.add(
                account["id"], -micros(plan.credit), "refund", f"refund:order:{item_id}", plan.name
            )
            return "refund"
        if event.startswith("subscription_") and data.get("type") == "subscriptions":
            status = str(attributes.get("status", ""))
            self.store.update_account(
                account["id"],
                plan="plus" if status in ACTIVE else "free",
                status=status,
                subscription_id=item_id,
                customer_id=str(attributes.get("customer_id", "")),
                renews_at=attributes.get("renews_at"),
                ends_at=attributes.get("ends_at"),
            )
            return f"subscription {status}"
        if event == "subscription_payment_success":
            plan = self.settings.plan(key="plus")
            if attributes.get("status") not in (None, "paid"):
                return "unpaid"
            added = self.store.add(
                account["id"],
                micros(plan.credit),
                "plan",
                f"invoice:{item_id}",
                "Monthly AI credit",
            )
            return "monthly credit" if added else "duplicate"
        if event == "subscription_payment_refunded":
            plan = self.settings.plan(key="plus")
            self.store.add(
                account["id"], -micros(plan.credit), "refund", f"refund:invoice:{item_id}", "Refund"
            )
            return "refund"
        if event == "license_key_created":
            key = str(attributes.get("key") or "")
            if key:
                self.store.add_license(key, account["id"])
                return "license"
        return "ignored"
