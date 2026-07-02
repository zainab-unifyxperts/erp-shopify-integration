"""
Cron entrypoint + fetch-and-sync-orders logic.

- Session guaranteed to close via shopify_session() context manager
- Retries via client.execute_graphql()
- Narrower except blocks with real error messages
"""

import os

import frappe
from frappe.utils import add_to_date, getdate
from frappe.utils.background_jobs import enqueue

from .client import ShopifyAPIError, execute_graphql, shopify_session
from .mapping.order import create_shopify_sales_order
from .payments import sync_payment_entries

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(CURRENT_DIR, "selling_query.graphql")) as f:
    ORDERS_QUERY = f.read()


def shopify_order_sync_job() -> None:
    """Cron entrypoint: enqueues a sync job per enabled Shopify Integration Settings doc."""
    for doc in frappe.get_list("Shopify Integration Settings", {"enabled": 1}):
        enqueue_shopify_sync_orders(doc.name, use_setting_date=True)


@frappe.whitelist()
def enqueue_shopify_sync_orders(doc: str, use_setting_date: bool) -> None:
    """
    Enqueues a background sync job for one Shopify Integration Settings doc.

    use_setting_date=True  -> use "Order Syncing Start Date" field as-is
    use_setting_date=False -> use today minus "Order Sync Duration" days
    """
    try:
        if use_setting_date:
            start_date = frappe.get_value("Shopify Integration Settings", doc, "order_syncing_start_date")
        else:
            duration = frappe.get_value("Shopify Integration Settings", doc, "order_sync_duration")
            start_date = add_to_date(getdate(), days=-(duration or 0))

        enqueue(
            "shopify_integration.shopify_selling.sync.sync_shopify_orders",
            setting_doc_name=doc,
            start_date=start_date,
            timeout=1800,
        )
    except Exception:
        frappe.log_error(
            title="Enqueue Sync Error",
            message=f"Could not enqueue sync for {doc}\n{frappe.get_traceback()}",
        )


def _fetch_all_orders(setting_doc, order_query: str) -> list[dict]:
    """Fetches all order edges for the query, following pagination."""
    with shopify_session(setting_doc):
        response = execute_graphql(
            ORDERS_QUERY,
            variables={"nos": 250, "order_query": order_query},
            operation_name="GetOrdersInfo",
        )
        orders = response["data"]["orders"]["edges"]

        while response["data"]["orders"]["pageInfo"]["hasNextPage"]:
            response = execute_graphql(
                ORDERS_QUERY,
                variables={
                    "nos": 250,
                    "order_query": order_query,
                    "after": response["data"]["orders"]["pageInfo"]["endCursor"],
                },
                operation_name="GetOrdersInfo",
            )
            orders.extend(response["data"]["orders"]["edges"])

    return orders


def sync_shopify_orders(setting_doc_name: str, start_date: str) -> None:
    """
    Fetches all Shopify orders created after `start_date` (excluding cancelled)
    and creates/updates ERPNext Sales Orders + Payment Entries for each.
    """
    try:
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        order_query = f"(created_at:>{start_date}) AND (-status:CANCELLED)"
        order_data = _fetch_all_orders(setting_doc, order_query)
    except ShopifyAPIError:
        frappe.log_error(
            title="Shopify API Error",
            message=f"Setting: {setting_doc_name}\n{frappe.get_traceback()}",
        )
        return
    except Exception:
        frappe.log_error(
            title="Shopify Order Fetch Error",
            message=f"Setting: {setting_doc_name}\n{frappe.get_traceback()}",
        )
        return

    for edge in order_data:
        try:
            create_shopify_sales_order(
                edge["node"], setting_doc_name, is_return=False,
                sync_payment_entries_fn=sync_payment_entries,
            )
        except Exception:
            frappe.log_error(
                title="Sales Order Creation Error",
                message=f"Traceback:\n{frappe.get_traceback()}\nPayload:\n{edge}",
            )
