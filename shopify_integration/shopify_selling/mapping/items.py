"""
Line item + product bundle mapping: Shopify -> ERPNext Sales Order Items.
"""

from typing import Optional

import frappe
from erpnext.stock.doctype.packed_item.packed_item import get_product_bundle_items

from .taxes import _get_money_amount, get_shopify_account  # noqa: F401 (account used by callers)


def get_shopify_item_code(sku: str, name: str, setting_doc: str) -> str:
    """
    Creates an Item in ERPNext if it doesn't already exist for this SKU.
    """
    if frappe.db.exists("Item", {"item_code": sku}):
        return sku

    item_doc = frappe.new_doc("Item")
    item_doc.item_code = sku
    item_doc.item_name = name
    item_doc.stock_uom = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "default_uom"
    )
    item_doc.item_group = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "default_item_group"
    )
    item_doc.save()
    return item_doc.item_code


def create_shopify_so_item_row(data: dict, setting_doc: str) -> Optional[dict]:
    """
    Builds a single Sales Order Item row dict from a Shopify GraphQL line item node.
    Returns None if the item's current quantity is 0 (fully cancelled/refunded line).
    """
    if data.get("currentQuantity") == 0:
        return None

    return {
        "item_code": get_shopify_item_code(data["sku"], data["name"], setting_doc),
        "qty": data["currentQuantity"],
        "rate": _get_money_amount(data.get("discountedUnitPriceSet")),
        "custom_sku": data["sku"],
        "custom_shopify_line_item_id": data["id"],
    }


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


def expand_bundle_row(ir: dict, new_sales_order) -> bool:
    """
    If `ir` (an item row dict) is a Product Bundle, appends its expanded child
    rows to new_sales_order.items and returns True.
    Returns False if the item is not a bundle (caller should append ir as-is).
    """
    item_code = ir.get("item_code")
    if not frappe.db.exists("Product Bundle", {"new_item_code": item_code}):
        return False

    bundle_items = get_product_bundle_items(item_code)
    if not bundle_items:
        return False

    ordered_qty = ir.get("qty", 1)
    shopify_line_item_id = ir.get("custom_shopify_line_item_id")
    shopify_rate = ir.get("rate") or 0.0

    bundle_doc = frappe.get_doc("Product Bundle", {"new_item_code": item_code})
    total_bundle_price = sum(
        child.custom_bundle_price_ or 0.0 for child in bundle_doc.items
    )

    bundle_price_map = {}
    for child in bundle_doc.items:
        if total_bundle_price > 0:
            share = (child.custom_bundle_price_ or 0.0) / total_bundle_price
            bundle_price_map[child.item_code] = round(share * shopify_rate, 2)
        else:
            bundle_price_map[child.item_code] = 0.0

    for child in bundle_items:
        rate = bundle_price_map.get(child.item_code) or 0.0
        new_sales_order.append(
            "items",
            {
                "item_code": child.item_code,
                "item_name": child.item_name,
                "qty": child.qty * ordered_qty,
                "uom": child.uom,
                "description": child.description,
                "custom_parent_bundle_item_name": bundle_doc.name,
                "delivery_date": new_sales_order.delivery_date,
                "dont_recompute_tax": 1,
                "custom_shopify_line_item_id": shopify_line_item_id,
                "rate": rate,
                "price_list_rate": rate,
                "base_rate": rate,
                "base_price_list_rate": rate,
                "warehouse": ir.get("warehouse"),
            },
        )
    return True


def append_item_rows(new_sales_order, item_rows: list[dict]) -> None:
    """
    Appends item rows to the Sales Order, expanding any Product Bundles
    into their child items.
    """
    for ir in item_rows:
        ir["dont_recompute_tax"] = 1
        if expand_bundle_row(ir, new_sales_order):
            continue
        ir["price_list_rate"] = ir.get("rate", 0.0)
        ir["base_price_list_rate"] = ir.get("rate", 0.0)
        ir["base_rate"] = ir.get("rate", 0.0)
        new_sales_order.append("items", ir)
