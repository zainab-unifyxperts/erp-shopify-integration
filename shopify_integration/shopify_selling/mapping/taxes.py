"""
Tax, VAT, duties, shipping and discount row building.
Consolidated from utils.py + shopify_selling_utils.py (was split across two
files with overlapping responsibility).
"""

from typing import List, Optional, Tuple

import frappe


# -------------------- Money helpers --------------------

def _get_money_amount(block: Optional[dict]) -> float:
    try:
        money = (block or {}).get("presentmentMoney") or (block or {}).get("shopMoney") or {}
        return float(money.get("amount", 0.0) or 0.0)
    except Exception:
        return 0.0


def _get_money_currency(block: Optional[dict]) -> Optional[str]:
    try:
        money = (block or {}).get("presentmentMoney") or (block or {}).get("shopMoney") or {}
        return money.get("currencyCode")
    except Exception:
        return None


# -------------------- Account resolution --------------------

def get_shopify_account(name: str, setting_doc_name: str) -> str:
    """
    Returns (creating if needed) an ERPNext Account for a given Shopify tax/
    charge label, scoped to the Shopify Integration Settings' company.

    NOTE: if `name` is >140 chars, falls back to the settings' default tax
    account. This fallback is now LOGGED (the old code did this silently).
    """
    account_name = f"Shopify {name}"
    company = frappe.get_value("Shopify Integration Settings", setting_doc_name, "company")

    existing = frappe.db.get_value(
        "Account", {"account_name": account_name, "company": company}, "name"
    )
    if existing:
        return existing

    if len(account_name) > 140:
        frappe.log_error(
            title="Shopify Account Name Too Long - Using Default",
            message=f"'{account_name}' exceeds 140 chars, falling back to default_tax_account",
        )
        return frappe.get_value(
            "Shopify Integration Settings", setting_doc_name, "default_tax_account"
        )

    account_doc = frappe.new_doc("Account")
    account_doc.account_name = account_name
    account_doc.company = company
    account_doc.parent_account = frappe.get_value(
        "Shopify Integration Settings", setting_doc_name, "shopify_expense_parent_account"
    )
    account_doc.save()
    return account_doc.name


# -------------------- VAT extraction --------------------

def extract_tax_lines(data: dict) -> List[dict]:
    return data.get("currentTaxLines") or []


def extract_vat_info(data: dict) -> Tuple[float, float, List[dict]]:
    """
    Returns (vat_rate as decimal e.g. 0.23, vat_total_amount, tax_lines).
    """
    tax_lines = extract_tax_lines(data)

    vat_candidates = []
    for t in tax_lines:
        title = (t.get("title") or "").lower()
        try:
            rate = float(t.get("rate", 0.0) or 0.0)
        except Exception:
            rate = 0.0
        amount = _get_money_amount(t.get("priceSet"))
        if "vat" in title:
            vat_candidates.append((rate, amount))

    if vat_candidates:
        return max(c[0] for c in vat_candidates), sum(c[1] for c in vat_candidates), tax_lines

    if tax_lines:
        try:
            vat_rate = max(float(t.get("rate", 0.0) or 0.0) for t in tax_lines)
            vat_total = sum(_get_money_amount(t.get("priceSet")) for t in tax_lines)
            return vat_rate, vat_total, tax_lines
        except Exception:
            pass

    subtotal = _get_money_amount(data.get("currentSubtotalPriceSet"))
    total_tax = _get_money_amount(data.get("currentTotalTaxSet"))
    if subtotal > 0 and total_tax > 0:
        return total_tax / subtotal, total_tax, tax_lines

    return 0.0, 0.0, tax_lines


def compute_base_from_inclusive(price_including_tax: float, vat_rate: float, places: int = 5) -> float:
    if vat_rate and (1.0 + vat_rate) != 0:
        return round(price_including_tax / (1.0 + vat_rate), places)
    return round(price_including_tax, places)


# -------------------- Discount --------------------

def get_shopify_discount_details(
    data: dict, taxes_included: bool, vat_rate_pct: float = 0.0
) -> Tuple[Optional[str], float]:
    """
    vat_rate_pct expected as a PERCENT (e.g. 21 for 21%), matching the old
    call convention (`vat_rate * 100` was passed in by the caller).
    """
    discount_amount = _get_money_amount(data.get("cartDiscountAmountSet"))
    if not discount_amount:
        return None, 0.0

    if taxes_included and discount_amount > 0 and vat_rate_pct > 0:
        discount_net = discount_amount / (1 + (vat_rate_pct / 100.0))
        return "Net Total", round(discount_net, 5)

    return "Grand Total", round(discount_amount, 5)


# -------------------- Tax row builders --------------------

def build_shipping_actual_row(
    data: dict,
    setting_doc: str,
    taxes_included: bool,
    vat_rate: float,
    marketplace: str,
    marketplace_order_id: str,
    currency: str = None,
) -> Optional[dict]:
    shipping = data.get("shippingLine")
    if not shipping:
        return None

    price_set = shipping.get("discountedPriceSet") or shipping.get("originalPriceSet") or {}
    shipping_price = _get_money_amount(price_set)
    shipping_base = (
        compute_base_from_inclusive(shipping_price, vat_rate)
        if (taxes_included and vat_rate > 0)
        else round(shipping_price, 5)
    )

    return {
        "charge_type": "Actual",
        "account_head": get_shopify_account(shipping.get("title", "Shipping"), setting_doc),
        "description": shipping.get("title", "Shipping"),
        "marketplace": marketplace,
        "marketplace_order_id": marketplace_order_id,
        "tax_amount": shipping_base,
        "rate": 0.0,
        "included_in_print_rate": 0,
        "account_currency": currency,
        "included_in_paid_amount": 0,
        "dont_recompute_tax": 1,
    }


def build_duties_row(
    duties_amount: float,
    duties_title: str,
    setting_doc: str,
    marketplace: str,
    marketplace_order_id: str,
    taxes_included: bool,
    vat_rate: float = 0.0,
    currency: str = None,
) -> Optional[dict]:
    if duties_amount <= 0:
        return None

    if taxes_included and vat_rate > 0:
        duties_amount = duties_amount / (1 + vat_rate)

    return {
        "charge_type": "Actual",
        "account_head": get_shopify_account(duties_title, setting_doc),
        "description": duties_title,
        "marketplace": marketplace,
        "marketplace_order_id": marketplace_order_id,
        "tax_amount": float(duties_amount),
        "rate": 0.0,
        "included_in_print_rate": 0,
        "account_currency": currency,
    }


def build_vat_tax_rows(
    data: dict,
    setting_doc: str,
    vat_rate: float,
    tax_lines: List[dict],
    taxes_included: bool,
    marketplace: str,
    marketplace_order_id: str,
    base_row_id: int = 0,
    currency: str = None,
) -> List[dict]:
    if not tax_lines and vat_rate <= 0:
        return []

    charge_type = "On Previous Row Total" if base_row_id > 0 else "On Net Total"
    rows = []

    if tax_lines:
        for t in tax_lines:
            title = t.get("title", "VAT")
            try:
                rate = float(t.get("rate", 0.0) or 0.0)
            except Exception:
                rate = 0.0
            rows.append(
                {
                    "charge_type": charge_type,
                    "row_id": base_row_id if charge_type == "On Previous Row Total" else None,
                    "account_head": get_shopify_account(title, setting_doc),
                    "description": f"{title} ({int(rate * 100)}%)" if rate else title,
                    "marketplace": marketplace,
                    "marketplace_order_id": marketplace_order_id,
                    "rate": round(rate * 100, 5),
                    "account_currency": currency,
                }
            )
        return rows

    rows.append(
        {
            "charge_type": charge_type,
            "row_id": base_row_id if charge_type == "On Previous Row Total" else None,
            "account_head": get_shopify_account("VAT", setting_doc),
            "description": f"VAT ({int(vat_rate * 100)}%)" if vat_rate else "VAT",
            "marketplace": marketplace,
            "marketplace_order_id": marketplace_order_id,
            "rate": round(vat_rate * 100, 5),
        }
    )
    return rows


def create_shopify_so_tax_row(
    tax_data: dict,
    setting_doc: str,
    marketplace_order_id: str,
    taxes_included: bool = False,
    source: str = None,
    currency: str = None,
) -> dict:
    row = {
        "account_head": get_shopify_account(tax_data.get("title", "Tax"), setting_doc),
        "description": tax_data.get("title", "Tax"),
        "marketplace": frappe.get_value("Shopify Integration Settings", setting_doc, "marketplace"),
        "marketplace_order_id": marketplace_order_id,
        "tax_amount": float(
            tax_data.get("discountedPriceSet", {}).get("shopMoney", {}).get("amount")
            or tax_data.get("priceSet", {}).get("shopMoney", {}).get("amount", 0.0)
        ),
        "account_currency": currency,
    }

    if source == "tax":
        row["included_in_print_rate"] = 1 if taxes_included else 0
        row["charge_type"] = (
            "On Net Total"
            if taxes_included
            else frappe.get_value("Shopify Integration Settings", setting_doc, "default_tax_charge_type")
        )
    elif source == "shipping":
        row["charge_type"] = "Actual"
        row["included_in_print_rate"] = 0
    else:
        row["charge_type"] = frappe.get_value(
            "Shopify Integration Settings", setting_doc, "default_tax_charge_type"
        )
        row["included_in_print_rate"] = 0

    return row


def build_non_inclusive_tax_rows(
    data: dict, setting_doc: str, marketplace_order_id: str, currency: str = None
) -> List[dict]:
    rows = [
        create_shopify_so_tax_row(t, setting_doc, marketplace_order_id, taxes_included=False, source="tax")
        for t in extract_tax_lines(data)
    ]
    if data.get("shippingLine"):
        rows.append(
            create_shopify_so_tax_row(
                data["shippingLine"],
                setting_doc,
                marketplace_order_id,
                taxes_included=False,
                source="shipping",
                currency=currency,
            )
        )
    return rows
