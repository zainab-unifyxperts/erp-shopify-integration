"""
Handles the "amend Sales Order" step of the order-edit webhook flow.
"""

from datetime import datetime

import frappe
from frappe.utils import add_to_date

from .mapping.customer import get_link_row, get_shopify_address, get_shopify_mo_id
from .mapping.items import get_shopify_item_code


def get_discount_code(discount_codes: list) -> str:
    return ",".join(c["code"] for c in discount_codes)


def get_updated_shopify_contact(contact_data: dict) -> str:
    display_name = f"{contact_data['first_name']} {contact_data['last_name']}"
    if frappe.db.exists("Contact Email", {"email_id": contact_data["email"]}):
        return frappe.get_value("Contact Email", {"email_id": contact_data["email"]}, "parent")

    contact_doc = frappe.new_doc("Contact")
    contact_doc.first_name = display_name
    contact_doc.append("email_ids", {"email_id": contact_data["email"], "is_primary": 1})
    if contact_data.get("phone"):
        contact_doc.append("phone_nos", {"phone": contact_data["phone"], "is_primary_phone": 1})
    contact_doc.append("links", get_link_row("Customer", display_name))
    contact_doc.save()
    return contact_doc.name


def get_updated_shopify_customer(customer_data: dict, setting_doc: str) -> str:
    display_name = f"{customer_data['first_name']} {customer_data['last_name']}"
    existing = frappe.db.get_value("Customer", {"customer_name": display_name}, "name")
    if existing:
        return existing

    customer_doc = frappe.new_doc("Customer")
    customer_doc.customer_name = display_name
    customer_doc.customer_type = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "default_customer_type"
    )
    customer_doc.customer_primary_contact = get_updated_shopify_contact(customer_data)
    customer_doc.save()
    return customer_doc.name


def create_updated_shopify_so_item_row(data: dict, setting_doc: str) -> dict:
    return {
        "item_code": get_shopify_item_code(data["sku"], data["name"], setting_doc),
        "qty": data["current_quantity"],
        "rate": data["price_set"]["shop_money"]["amount"],
        "custom_sku": data["sku"],
    }


def update_shopify_sales_order(
    data: dict, setting_doc_name: str, old_sales_order_name: str, shopify_webhook_id: str
) -> None:
    """Creates an amended Sales Order from an ORDERS_UPDATED REST webhook payload."""
    marketplace = frappe.get_value("Shopify Integration Settings", setting_doc_name, "marketplace")

    if frappe.db.exists(
        "Sales Order",
        {"marketplace_order_id": data["name"], "marketplace": marketplace, "docstatus": 1},
    ):
        return

    new_sales_order = frappe.new_doc("Sales Order")
    transaction_date = datetime.strptime(data["updated_at"].split("T")[0], "%Y-%m-%d").date()
    new_sales_order.transaction_date = transaction_date
    new_sales_order.amended_from = old_sales_order_name
    new_sales_order.custom_shopify_webhook_id = shopify_webhook_id
    new_sales_order.custom_shopify_discount_codes = get_discount_code(data["discount_codes"])
    new_sales_order.discount_amount = float(data["current_total_discounts"])
    new_sales_order.custom_fully_paid = 1 if data["financial_status"] == "paid" else 0
    new_sales_order.delivery_date = add_to_date(transaction_date, days=3)
    new_sales_order.custom_shopify_order_id_number = data["admin_graphql_api_id"]
    new_sales_order.company = frappe.get_value("Shopify Integration Settings", setting_doc_name, "company")
    new_sales_order.customer = get_updated_shopify_customer(data["customer"], setting_doc_name)
    new_sales_order.marketplace_order_id = get_shopify_mo_id(data["name"], setting_doc_name)
    new_sales_order.marketplace = marketplace

    for row in data["line_items"]:
        new_sales_order.append("items", create_updated_shopify_so_item_row(row, setting_doc_name))

    from .mapping.taxes import create_shopify_so_tax_row

    for row in data["tax_lines"]:
        new_sales_order.append(
            "taxes",
            create_shopify_so_tax_row(row, setting_doc_name, new_sales_order.marketplace_order_id),
        )
    if data.get("shipping_lines"):
        new_sales_order.append(
            "taxes",
            create_shopify_so_tax_row(
                data["shipping_lines"], setting_doc_name, new_sales_order.marketplace_order_id
            ),
        )

    new_sales_order.customer_address = get_shopify_address(data["billing_address"], new_sales_order.customer)
    new_sales_order.shipping_address_name = get_shopify_address(
        data["shipping_address"], new_sales_order.customer
    )
    new_sales_order.contact_person = frappe.get_value(
        "Customer", new_sales_order.customer, "customer_primary_contact"
    )

    try:
        new_sales_order.save()
        new_sales_order.submit()
    except Exception:
        frappe.log_error(
            title="Amended Sales Order Save Error",
            message=f"Traceback:{frappe.get_traceback()}",
        )
