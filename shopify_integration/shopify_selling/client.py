"""
Shopify session + GraphQL execution helper.

Fixes vs old code:
- Session is guaranteed to clear even on exception (context manager, no leaks)
- Retries on rate limit (429) and transient network errors with backoff
- One place that knows how to build a shop_url / session -> no more copy-pasted
  session boilerplate in every function
"""

import json
import time
from contextlib import contextmanager

import frappe
import shopify


class ShopifyAPIError(Exception):
    """Raised when Shopify GraphQL returns a top-level error we can't recover from."""


@contextmanager
def shopify_session(setting_doc):
    """
    Context manager that activates a Shopify session for the given
    Shopify Integration Settings doc and guarantees cleanup.

    Usage:
        with shopify_session(setting_doc):
            result = execute_graphql(query, variables, operation_name)
    """
    shop_url = f"https://@{setting_doc.shop_name}.myshopify.com"
    session = shopify.Session(
        shop_url,
        setting_doc.api_version,
        setting_doc.get_password("access_token"),
    )
    shopify.ShopifyResource.activate_session(session)
    try:
        yield
    finally:
        shopify.ShopifyResource.clear_session()


def execute_graphql(
    query: str,
    variables: dict | None = None,
    operation_name: str | None = None,
    max_retries: int = 3,
) -> dict:
    """
    Executes a GraphQL call against the currently active Shopify session,
    with retry/backoff on rate limiting (429) and throttling errors.

    Must be called from inside a `with shopify_session(setting_doc):` block.

    Returns the parsed JSON response (dict).
    Raises ShopifyAPIError if all retries are exhausted or a non-retryable
    top-level error is returned.
    """
    attempt = 0
    backoff_seconds = 2

    while True:
        attempt += 1
        try:
            raw = shopify.GraphQL().execute(
                query=query,
                variables=variables or {},
                operation_name=operation_name,
            )
            response = json.loads(raw)
        except Exception as e:
            if attempt >= max_retries:
                raise ShopifyAPIError(f"Shopify API call failed after {attempt} attempts: {e}") from e
            time.sleep(backoff_seconds)
            backoff_seconds *= 2
            continue

        errors = response.get("errors") or []
        throttled = any(
            "throttle" in json.dumps(err).lower() or "rate limit" in json.dumps(err).lower()
            for err in errors
        )

        if throttled and attempt < max_retries:
            frappe.logger().warning(f"Shopify throttled, retrying (attempt {attempt})")
            time.sleep(backoff_seconds)
            backoff_seconds *= 2
            continue

        if errors and not response.get("data"):
            raise ShopifyAPIError(f"Shopify GraphQL error: {errors}")

        return response
