"""
Webhook endpoints for Shopify order edit / fulfillment events.
"""

import json

import frappe

from .client_verify import verify_webhook  # see note below
from .mapping.items import get_shopify_item_code


def _get_setting_doc_for_request() -> str | None:
    """
    Resolves which Shopify Integration Settings doc a webhook belongs to,
    using the X-Shopify-Shop-Domain header Shopify sends on every webhook.
    """
    shop_domain = frappe.request.headers.get("X-Shopify-Shop-Domain", "")
    shop_name = shop_domain.replace(".myshopify.com", "")
    if not shop_name:
        return None
    return frappe.db.get_value(
        "Shopify Integration Settings", {"shop_name": shop_name, "enabled": 1}, "name"
    )


@frappe.whitelist(allow_guest=True)
def shopify_order_edit() -> None:
    """Endpoint the ORDERS_UPDATED webhook hits."""
    setting_doc_name = _get_setting_doc_for_request()
    if not setting_doc_name:
        frappe.log_error(
            title="Webhook: Unknown Shop",
            message=f"No matching Shopify Integration Settings for domain: "
                    f"{frappe.request.headers.get('X-Shopify-Shop-Domain')}",
        )
        raise frappe.AuthenticationError

    setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
    if not setting_doc.order_edit_webhook:
        return

    hmac_header = frappe.request.headers.get("X-Shopify-Hmac-SHA256")
    raw_data = frappe.request.get_data()
    shopify_webhook_id = frappe.request.headers.get("X-Shopify-Webhook-Id")

    if not verify_webhook(raw_data, hmac_header, setting_doc.get_password("client_secret")):
        raise frappe.AuthenticationError

    if frappe.db.exists("Sales Order", {"custom_shopify_webhook_id": shopify_webhook_id}):
        return  # already processed

    order_data = json.loads(raw_data.decode("utf-8"))
    order_data["line_items"] = [
        item for item in order_data.get("line_items", []) if item.get("current_quantity") != 0
    ]

    marketplace_order_id = order_data["name"]
    sales_order_name = frappe.db.get_value(
        "Sales Order",
        {"marketplace_order_id": marketplace_order_id, "docstatus": ["!=", 2]},
        "name",
    )

    if not sales_order_name:
        frappe.log_error(
            title="Webhook: SO Not Found",
            message=f"No Sales Order for marketplace_order_id: {marketplace_order_id}",
        )
        return

    if frappe.db.get_value("Sales Order", sales_order_name, "custom_extensiv_order_number"):
        frappe.log_error(
            title="Webhook: Order Already Fulfilled Downstream",
            message=f"SO {sales_order_name} already synced to fulfillment, refusing to amend",
        )
        return

    old_sales_order = frappe.get_doc("Sales Order", sales_order_name)
    old_sales_order.cancel()

    try:
        # local import to avoid circular import with mapping.order
        from .webhooks_update import update_shopify_sales_order

        update_shopify_sales_order(order_data, setting_doc_name, sales_order_name, shopify_webhook_id)
    except Exception:
        frappe.log_error(
            title="Webhook: SO Amend Failed",
            message=f"Traceback: {frappe.get_traceback()}",
        )


@frappe.whitelist(allow_guest=True)
def shopify_fulfillment_create() -> None:
    """Endpoint the FULFILLMENT_ORDERS_SPLIT webhook hits."""
    setting_doc_name = _get_setting_doc_for_request()
    if not setting_doc_name:
        raise frappe.AuthenticationError

    setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
    hmac_header = frappe.request.headers.get("X-Shopify-Hmac-SHA256")
    raw_data = frappe.request.get_data()

    if not verify_webhook(raw_data, hmac_header, setting_doc.get_password("client_secret")):
        raise frappe.AuthenticationError

    order_data = json.loads(raw_data.decode("utf-8"))
    frappe.logger().info(f"Fulfillment webhook received: {order_data.get('id')}")
