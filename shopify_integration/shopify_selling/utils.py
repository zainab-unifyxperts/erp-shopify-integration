# import math
from typing import List, Tuple, Optional
from shopify_integration.shopify_selling.shopify_selling_utils import *

def _get_money_amount(block: Optional[dict]) -> float:
    """
    Safely extract amount from a Shopify MoneyV2 block.
    Prefer presentmentMoney → fallback to shopMoney.
    Returns 0.0 if missing or invalid.
    """
    try:
        money_data = (block or {}).get("presentmentMoney") or (block or {}).get("shopMoney") or {}
        return float(money_data.get("amount", 0.0) or 0.0)
    except Exception:
        return 0.0

def _get_money_currency(block: Optional[dict]) -> Optional[str]:
    """
    Safely extract currency from a Shopify MoneyV2 block.
    Prefer presentmentMoney → fallback to shopMoney.
    """
    try:
        money_data = (block or {}).get("presentmentMoney") or (block or {}).get("shopMoney") or {}
        return money_data.get("currencyCode")
    except Exception:
        return None


def extract_tax_lines(data: dict) -> List[dict]:
    """Return a safe list of tax lines (empty list if none)."""
    return data.get("currentTaxLines") or []

def extract_vat_info(data: dict) -> Tuple[float, float, List[dict]]:
    """
    Robustly extract VAT info from Shopify order data.

    Returns:
        vat_rate (decimal, e.g. 0.23),
        vat_total_amount (monetary amount from Shopify for VAT lines),
        tax_lines (list) -> normalized list of tax lines from Shopify
    """
    tax_lines = extract_tax_lines(data)
    vat_rate = 0.0
    vat_total_amount = 0.0

    # Prefer tax lines with 'vat' in title
    vat_candidates = []
    for t in tax_lines:
        title = (t.get("title") or "").lower()
        try:
            rate = float(t.get("rate", 0.0) or 0.0)
        except Exception:
            rate = 0.0
        try:
            amount = _get_money_amount(t.get("priceSet", {}))
        except Exception:
            amount = 0.0
        if "vat" in title:
            vat_candidates.append((rate, amount, t))

    if vat_candidates:
        vat_rate = max(c[0] for c in vat_candidates)
        vat_total_amount = sum(c[1] for c in vat_candidates)
        return vat_rate, vat_total_amount, tax_lines

    # No VAT-tagged lines: pick the highest rate tax_line (best-effort)
    if tax_lines:
        try:
            vat_rate = max(float(t.get("rate", 0.0) or 0.0) for t in tax_lines)
            vat_total_amount = sum(_get_money_amount(t.get("priceSet", {})) for t in tax_lines)
            return vat_rate, vat_total_amount, tax_lines
        except Exception:
            pass

    # Fallback: estimate from subtotal and total tax (if available)
    try:
    
        subtotal = _get_money_amount(data.get("currentSubtotalPriceSet", {}))

        
        total_tax = _get_money_amount(data.get("currentTotalTaxSet", {}))

        if subtotal > 0 and total_tax > 0:
            vat_rate = total_tax / subtotal
            vat_total_amount = total_tax
    except Exception:
        vat_rate = 0.0
        vat_total_amount = 0.0

    return vat_rate, vat_total_amount, tax_lines


def compute_base_from_inclusive(price_including_tax: float, vat_rate: float, places: int = 5) -> float:
    """Given a tax-inclusive price and VAT rate (decimal), return base price rounded."""
    if vat_rate and (1.0 + vat_rate) != 0:
        base = price_including_tax / (1.0 + vat_rate)
    else:
        base = price_including_tax
    # consistent rounding -- round half away from zero like financial rounding could be desired; using round() is usual.
    return round(base, places)

def build_item_rows_from_shopify(data: dict, setting_doc: str, taxes_included: bool, vat_rate: float) -> list:
    item_rows = []
    for row in data.get("lineItems", {}).get("edges", []):
        node = row.get("node", {})
        item_row = create_shopify_so_item_row(node, setting_doc)
        if not item_row:
            continue

        shop_unit_price = _get_money_amount(node.get("discountedUnitPriceSet", {}))

        # if taxes included, convert to base rate
        if taxes_included and vat_rate > 0:
            item_row["rate"] = round(shop_unit_price / (1 + vat_rate), 5)
        else:
            item_row["rate"] = round(shop_unit_price, 5)

        # quantity fallback
        try:
            item_row["qty"] = int(node.get("quantity", item_row.get("qty", 1)))
        except Exception:
            item_row["qty"] = item_row.get("qty", 1)

        item_row["amount"] = round(item_row["qty"] * item_row["rate"], 5)
        item_rows.append(item_row)
    print("---------------Items Rows----------------\n", item_rows)
    return item_rows

def build_shipping_actual_row(data: dict, setting_doc: str, taxes_included: bool, vat_rate: float, marketplace: str, marketplace_order_id: str, currency: str = None) -> dict:
    shipping = data.get("shippingLine")
    if not shipping:
        return None

    price_set = shipping.get("discountedPriceSet") or shipping.get("originalPriceSet") or {}
    shipping_price = _get_money_amount(price_set)

    if taxes_included and vat_rate > 0:
        shipping_base = compute_base_from_inclusive(shipping_price, vat_rate)
    else:
        shipping_base = round(shipping_price, 5)

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
        "dont_recompute_tax": 1
    }



def build_vat_tax_rows(data: dict, setting_doc: str, vat_rate: float, tax_lines: List[dict], taxes_included: bool, marketplace: str, marketplace_order_id: str, base_row_id: int = 0, currency: str = None) -> List[dict]:
    """
    Build VAT tax rows that tell ERPNext to compute VAT.
    By default the rows use "On Previous Row Total" and set tax_rate (percent).
    Returns a list of tax row dicts to append to new_sales_order.taxes.
    """
    rows = []
    if not tax_lines and vat_rate <= 0:
        return rows
    
    # Decide charge type dynamically
    charge_type = "On Previous Row Total" if base_row_id > 0 else "On Net Total"

    # If tax lines present, create a corresponding ERPNext tax row for each (so titles and rates preserved).
    if tax_lines:
        for t in tax_lines:
            title = t.get("title", "VAT")
            try:
                rate = float(t.get("rate", 0.0) or 0.0)
            except Exception:
                rate = 0.0

            row = {
                "charge_type": charge_type,
                "row_id": base_row_id if charge_type == "On Previous Row Total" else None,
                "account_head": get_shopify_account(title, setting_doc),
                "description": f"{title} ({int(rate * 100)}%)" if rate else title,
                "marketplace": marketplace,
                "marketplace_order_id": marketplace_order_id,
                "rate": round(rate * 100, 5),
                "account_currency": currency
                # do not set tax_amount -> ERPNext will compute
                # "included_in_print_rate": 1 if taxes_included else 0,
            }
            rows.append(row)
        return rows

    # Fallback single VAT row (no tax_lines)
    row = {
        "charge_type": charge_type,
        "row_id": base_row_id if charge_type == "On Previous Row Total" else None,
        "account_head": get_shopify_account("VAT", setting_doc),
        "description": f"VAT ({int(vat_rate * 100)}%)" if vat_rate else "VAT",
        "marketplace": marketplace,
        "marketplace_order_id": marketplace_order_id,
        "rate": round(vat_rate * 100, 5),
        # "included_in_print_rate": 1 if taxes_included else 0,
    }
    rows.append(row)
    return rows


def build_non_inclusive_tax_rows(data: dict, setting_doc: str, marketplace_order_id: str, currency: str = None) -> List[dict]:
    """
    For the case taxes are NOT included in Shopify prices:
      - Use existing create_shopify_so_tax_row helper to map Shopify tax lines/ shippingLine.
    """
    rows = []
    tax_lines = extract_tax_lines(data)
    for t in tax_lines:
        rows.append(create_shopify_so_tax_row(t, setting_doc, marketplace_order_id, taxes_included=False, source="tax"))
    # shipping mapping
    if data.get("shippingLine"):
        rows.append(create_shopify_so_tax_row(data.get("shippingLine"), setting_doc, marketplace_order_id, taxes_included=False, source="shipping", currency=currency))
    return rows

def build_duties_row(duties_amount: float, duties_title: str, setting_doc: str, 
                     marketplace: str, marketplace_order_id: str, taxes_included: bool,vat_rate: float = 0.0, currency: str = None) -> dict:
    """
    Build an ERPNext tax row for duties (custom/actual charge at the end of taxes table).

    Args:
        duties_amount (float): Duty amount in order currency
        duties_title (str): Label for the duty
        setting_doc (str): Shopify Integration Settings docname
        marketplace (str): Marketplace name
        marketplace_order_id (str): Shopify Order ID

    Returns:
        dict: Ready to append to Sales Order.taxes
    """

    if duties_amount <= 0:
        return None
    
    if taxes_included and vat_rate > 0:
        # frappe.log_error("Duty Rate before vat", f"{duties_amount}, vat_rate: {vat_rate}")
        duties_amount = duties_amount / (1 + vat_rate)
        frappe.log_error("Duty Rate",f"Taxes included: {taxes_included}, duties_amount: {duties_amount}")

    duties_amount = float(duties_amount)
    frappe.log_error("Duty Rate",f"Taxes included: {taxes_included}, duties_amount: {duties_amount}")
    return {
        "charge_type": "Actual",
        "account_head": get_shopify_account(duties_title, setting_doc),
        "description": duties_title,
        "marketplace": marketplace,
        "marketplace_order_id": marketplace_order_id,
        "tax_amount": duties_amount,
        "rate": 0.0,
        "included_in_print_rate": 0,
        "account_currency": currency
    }

def get_shop_money_amount(data: dict, key: str) -> float:
    try:
        return _get_money_amount(data.get(key))
    except Exception:
        return 0.0

def get_shopify_discount_details(data:dict, taxes_included: bool, vat_rate: float = 0.0) -> Tuple[Optional[str], float]:
    """
    commpute Shopify discount for ERPNext order creation.

    Handles both tax-inclusive and exclusive prices.
    When taxes are included, it converts Shopify's VAT-inclusive discount to a base (net) amount.

    Args:
        data (dict) : Shopify order payload
        vat_rate (float) : VAT percentage (e.g., 21 for 21%)

    Returns:
    Tuple[str | None, float]:
        - apply_discount_on : "Net Total" if taxes included, "Grand Total" otherwise
        - discount_amount: numeric discount (converted if taxes are included)
    """
    discount_amount = get_shop_money_amount(data,"cartDiscountAmountSet")

     # If nothing found, 0.0
    if not discount_amount:
        return None, 0.0

    if taxes_included and discount_amount > 0 and vat_rate > 0:
        discount_net = discount_amount / (1 + (vat_rate / 100.0))
        # discount_amount = discount_amount / (1 + vat_rate/100)
        apply_discount_on = "Net Total"
        discount_amount_final = round(discount_net, 5)
    else:
        apply_discount_on = "Grand Total"
        discount_amount_final = round(discount_amount, 5)

    return apply_discount_on, discount_amount_final
