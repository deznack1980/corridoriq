"""The only module that talks to Stripe.

Everything else in pipeline.billing works on plain dicts and a gateway object,
so tests substitute a fake gateway and never reach the network. The real
gateway wraps the official SDK with certificate verification left at its
default (on); it is never disabled.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class BillingGatewayError(Exception):
    """Stripe could not be reached or refused the request. Fail closed."""


class WebhookSignatureError(Exception):
    """The webhook signature header is missing, malformed or does not match."""


# Hosted pages the server is willing to send a browser to.
CHECKOUT_HOSTS = frozenset({"checkout.stripe.com"})
PORTAL_HOSTS = frozenset({"billing.stripe.com"})

WEBHOOK_TOLERANCE_SECONDS = 300


def is_hosted_url(url, hosts) -> bool:
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in hosts and not parts.username


def verify_webhook_signature(payload: bytes, sig_header: str | None, secret: str) -> None:
    """Raise WebhookSignatureError unless `payload` was signed by Stripe with
    `secret` within the replay tolerance. Uses the SDK's constant-time check."""
    from stripe import SignatureVerificationError, WebhookSignature

    if not sig_header or not secret:
        raise WebhookSignatureError("missing signature")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebhookSignatureError("payload is not UTF-8") from exc
    try:
        WebhookSignature.verify_header(text, sig_header, secret, WEBHOOK_TOLERANCE_SECONDS)
    except SignatureVerificationError as exc:
        raise WebhookSignatureError("signature verification failed") from exc


class StripeGateway:
    """Thin adapter over stripe.StripeClient returning plain dicts."""

    def __init__(self, secret_key: str):
        import stripe

        self._stripe = stripe
        self._client = stripe.StripeClient(secret_key, max_network_retries=2)

    def _call(self, fn, *args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs).to_dict()
        except self._stripe.StripeError as exc:
            # Only the Stripe error class and request ID: never the message
            # body, which can echo request parameters.
            request_id = getattr(exc, "request_id", None)
            raise BillingGatewayError(f"stripe {type(exc).__name__} request_id={request_id}") from None

    def create_customer(self, *, name: str, metadata: dict, idempotency_key: str) -> dict:
        return self._call(self._client.v1.customers.create,
                          params={"name": name, "metadata": metadata},
                          options={"idempotency_key": idempotency_key})

    def create_checkout_session(self, params: dict, *, idempotency_key: str) -> dict:
        return self._call(self._client.v1.checkout.sessions.create, params=params,
                          options={"idempotency_key": idempotency_key})

    def retrieve_checkout_session(self, session_id: str) -> dict:
        # line_items expanded so a resumed session can be checked against the plan's price.
        return self._call(self._client.v1.checkout.sessions.retrieve, session_id,
                          params={"expand": ["line_items"]})

    def create_portal_session(self, *, customer: str, return_url: str) -> dict:
        return self._call(self._client.v1.billing_portal.sessions.create,
                          params={"customer": customer, "return_url": return_url})

    def retrieve_subscription(self, subscription_id: str) -> dict:
        return self._call(self._client.v1.subscriptions.retrieve, subscription_id)

    def retrieve_price(self, price_id: str) -> dict:
        return self._call(self._client.v1.prices.retrieve, price_id)


def default_gateway(config) -> StripeGateway:
    return StripeGateway(config.secret_key)
