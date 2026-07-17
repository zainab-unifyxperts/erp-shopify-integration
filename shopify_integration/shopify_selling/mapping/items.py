"""
Line item mapping: Shopify -> ERPNext Sales Order Items.
"""

from typing import Optional

import frappe

from .taxes import _get_money_amount, get_shopify_account  # noqa: F401 (account used by callers)


def get_shopify_item_code(sku: str, name: str, setting_doc: str) -> str:
    """
    Creates an Item in ERPNext if it doesn't already exist for this SKU.
    """
    if frappe.db.exists("Item", {"item_code": sku}):
        return sku
    else:
        frappe.log_error(title=f"Sales Order Item does not exist", message=f"Item {sku} does not exist, please create the item.")


def extract_pos_serial_no(line_item: dict) -> str | None:
    """
    Pulls the Serial No from a Shopify POS line item's custom attributes.
    Returns None for non-POS / non-serialized line items.
    """
    for attr in line_item.get("customAttributes", []):
        if attr.get("key", "").strip().upper() in ("SN", "SERIAL NUMBER", "SERIAL NO"):
            return attr.get("value", "").strip() or None
    return None
    

def extract_pos_serial_map(data: dict) -> dict[str, str]:
    """
    Builds {item_code: serial_no} across all line items in a POS order.
    Only includes items that actually carry a serial custom attribute.
    """
    serial_map = {}
    for edge in data.get("lineItems", {}).get("edges", []):
        node = edge.get("node", {})
        serial_no = extract_pos_serial_no(node)
        if serial_no and node.get("sku"):
            serial_map[node["sku"]] = serial_no
    return serial_map

def create_shopify_so_item_row(data: dict, setting_doc: str) -> Optional[dict]:
    """
    Builds a single Sales Order Item row dict from a Shopify GraphQL line item node.
    Returns None if the item's current quantity is 0 (fully cancelled/refunded line).
    """
    if data.get("currentQuantity") == 0:
        return None

    row = {
        "item_code": get_shopify_item_code(data["sku"], data["name"], setting_doc),
        "qty": data["currentQuantity"],
        "rate": _get_money_amount(data.get("discountedUnitPriceSet")),
        "custom_sku": data["sku"],
        "custom_shopify_line_item_id": data["id"],
    }

    return row


def build_item_rows_from_shopify(
    data: dict, setting_doc: str, taxes_included: bool, vat_rate: float
) -> list[dict]:
    """
    Builds all item rows for an order, converting tax-inclusive prices to
    base (net) rate where applicable. Every stock item row gets a warehouse
    (defaults to the settings' backlog_warehouse) since ERPNext requires it
    at insert time.
    """
    default_warehouse = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "backlog_warehouse"
    )

    item_rows = []
    for edge in data.get("lineItems", {}).get("edges", []):
        node = edge.get("node", {})
        item_row = create_shopify_so_item_row(node, setting_doc)
        if not item_row:
            continue

        shop_unit_price = _get_money_amount(node.get("discountedUnitPriceSet"))
        if taxes_included and vat_rate > 0:
            item_row["rate"] = round(shop_unit_price / (1 + vat_rate), 5)
        else:
            item_row["rate"] = round(shop_unit_price, 5)

        try:
            item_row["qty"] = int(node.get("quantity", item_row.get("qty", 1)))
        except (TypeError, ValueError):
            pass

        item_row["amount"] = round(item_row["qty"] * item_row["rate"], 5)
        item_row["warehouse"] = default_warehouse
        item_rows.append(item_row)

    return item_rows


def append_item_rows(new_sales_order, item_rows: list[dict]) -> None:
    """
    Appends item rows to the Sales Order.
    """
    for ir in item_rows:
        ir["dont_recompute_tax"] = 1
        ir["price_list_rate"] = ir.get("rate", 0.0)
        ir["base_price_list_rate"] = ir.get("rate", 0.0)
        ir["base_rate"] = ir.get("rate", 0.0)
        new_sales_order.append("items", ir)