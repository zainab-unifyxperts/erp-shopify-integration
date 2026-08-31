"""
Line item mapping: Shopify -> ERPNext Sales Order Items.
"""

from typing import Optional

import frappe

from .taxes import _get_money_amount, get_shopify_account  # noqa: F401 (account used by callers)


def get_shopify_item_code(sku: str, name: str, setting_doc: str) -> str | None:
    """
    Returns the ERPNext Item code.

    If the Item does not exist:
    - Creates it when Create Missing Items is enabled.
    - Returns None when disabled.
    """
    if frappe.db.exists("Item", {"item_code": sku}):
        return sku
    else:
        frappe.log_error(title=f"Sales Order Item does not exist", message=f"Item {sku} does not exist, please create the item.")


def extract_pos_serial_list(line_item: dict) -> list[str]:
    """
    Pulls Serial No(s) from a Shopify POS line item's custom attributes.
    Serializer writes multiple serials for qty > 1 as a single
    comma-separated value - this splits and cleans them into a list.
    Returns an empty list for non-POS / non-serialized line items.
    """
    for attr in line_item.get("customAttributes", []):
        if attr.get("key", "").strip().upper() in ("SN", "SERIAL NUMBER", "SERIAL NO"):
            raw = attr.get("value", "").strip()
            if not raw:
                return []
            return [s.strip() for s in raw.split(",") if s.strip()]
    return []


def extract_pos_serial_map(data: dict) -> dict[str, list[str]]:
    """
    Builds {item_code: [serial_no, ...]} across all line items in a POS order.
    Only includes items that actually carry serial custom attributes.
    """
    serial_map = {}
    for edge in data.get("lineItems", {}).get("edges", []):
        node = edge.get("node", {})
        serials = extract_pos_serial_list(node)
        if serials and node.get("sku"):
            serial_map[node["sku"]] = serials
    return serial_map

def create_shopify_so_item_row(
    data: dict, setting_doc: str
) -> tuple[dict | None, str | None, str | None]:
    """
    Builds a single Sales Order Item row.
    Returns:
        item_row, missing_sku, missing_item_name
    """
    if data.get("currentQuantity") == 0:
        return None, None, None

    sku = data.get("sku")
    name = data.get("name")
    item_code = get_shopify_item_code(
        sku,
        name,
        setting_doc,
    )
    if not item_code:
        return None, sku, name
    return {
        "item_code": item_code,
        "qty": data["currentQuantity"],
        "rate": _get_money_amount(data.get("discountedUnitPriceSet")),
        "custom_sku": sku,
        "custom_shopify_line_item_id": data["id"],
    }, None, None


def build_item_rows_from_shopify(
    data: dict,
    setting_doc: str,
    taxes_included: bool,
    vat_rate: float,
) -> tuple[list[dict] | None, str | None, str | None]:
    """
    Builds all Sales Order item rows.
    Returns:
        item_rows, missing_sku, missing_item_name

    If an Item is missing and automatic Item creation is disabled,
    item_rows will be None.
    """
    default_warehouse = frappe.get_value(
        "Shopify Integration Settings",
        setting_doc,
        "backlog_warehouse",
    )

    item_rows = []
    for edge in data.get("lineItems", {}).get("edges", []):
        node = edge.get("node", {})
        item_row, missing_sku, missing_item_name = (
            create_shopify_so_item_row(
                node,
                setting_doc,
            )
        )
        if missing_sku:
            return None, missing_sku, missing_item_name
        if not item_row:
            continue

        shop_unit_price = _get_money_amount(
            node.get("discountedUnitPriceSet")
        )
        if taxes_included and vat_rate > 0:
            item_row["rate"] = round(
                shop_unit_price / (1 + vat_rate),
                5,
            )
        else:
            item_row["rate"] = round(
                shop_unit_price,
                5,
            )

        try:
            item_row["qty"] = int(
                node.get(
                    "quantity",
                    item_row.get("qty", 1),
                )
            )
        except (TypeError, ValueError):
            pass

        item_row["amount"] = round(
            item_row["qty"] * item_row["rate"],
            5,
        )
        item_row["warehouse"] = default_warehouse
        item_rows.append(item_row)

    return item_rows, None, None


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