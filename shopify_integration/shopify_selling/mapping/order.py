"""
Builds an ERPNext Sales Order from a Shopify GraphQL order node.

The entrypoint is still `create_shopify_sales_order`.
"""

import datetime

import frappe
from frappe.utils import add_months, add_to_date, get_datetime, now_datetime

from .customer import get_shopify_address, get_shopify_customer, get_shopify_mo_id
from .items import append_item_rows, build_item_rows_from_shopify
from .taxes import (
    _get_money_amount,
    _get_money_currency,
    build_duties_row,
    build_non_inclusive_tax_rows,
    build_shipping_actual_row,
    build_vat_tax_rows,
    extract_vat_info,
    get_shopify_discount_details,
)


def get_fulfillment_status_to_exclude(shopify_setting_doc, order_fulfillment_status: str) -> bool:
    status = (order_fulfillment_status or "").strip().upper()
    if not status:
        return False
    excluded = {
        (row.shopify_fulfillment_status or "").strip().upper()
        for row in shopify_setting_doc.shopify_fulfillment_statuses_to_exclude
        if row.shopify_fulfillment_status
    }
    return status in excluded


def _existing_sales_order(marketplace_order_id: str, marketplace: str) -> dict | None:
    return frappe.db.get_value(
        "Sales Order",
        {"marketplace_order_id": marketplace_order_id, "marketplace": marketplace},
        ["name", "creation"],
        as_dict=True,
    )


def _resolve_currency(data: dict, setting_doc: str) -> str:
    currency = data.get("presentmentCurrencyCode")
    if currency:
        return currency

    transactions = data.get("transactions") or []
    if transactions:
        txn_currency = _get_money_currency(transactions[0].get("amountSet"))
        if txn_currency:
            return txn_currency

    return frappe.get_value(
        "Company",
        frappe.get_value("Shopify Integration Settings", setting_doc, "company"),
        "default_currency",
    )


def _set_incoterm_from_tags(new_sales_order, tags: list) -> None:
    for tag in tags or []:
        tag = (tag or "").strip()
        if not tag:
            continue
        incoterm = frappe.get_value("Incoterm", {"code": tag}, "name")
        if incoterm:
            new_sales_order.incoterm = incoterm
            return


def _set_customer_and_addresses(new_sales_order, data: dict, setting_doc: str) -> None:
    customer_details = data.get("customer") or {}
    new_sales_order.customer = get_shopify_customer(customer_details, setting_doc)
    new_sales_order.contact_email = customer_details.get("email") or ""

    shipping_details = data.get("shippingAddress") or {}
    new_sales_order.custom_shipping_phone = shipping_details.get("phone") or ""

    billing_details = data.get("billingAddress") or {}
    new_sales_order.contact_phone = billing_details.get("phone") or ""

    new_sales_order.contact_person = frappe.get_value(
        "Customer", new_sales_order.customer, "customer_primary_contact"
    )

    if data.get("billingAddressMatchesShippingAddress", True):
        addr = get_shopify_address(data.get("shippingAddress"), new_sales_order.customer)
        new_sales_order.customer_address = addr
        new_sales_order.shipping_address_name = addr
    else:
        new_sales_order.customer_address = get_shopify_address(
            data.get("billingAddress"), new_sales_order.customer
        )
        new_sales_order.shipping_address_name = get_shopify_address(
            data.get("shippingAddress"), new_sales_order.customer
        )

    if not new_sales_order.customer_address:
        frappe.log_error(
            title="Sales Order Missing Address",
            message=f"Customer {new_sales_order.customer} / Order {data.get('name')} has no resolvable address",
        )


def _set_sales_type(new_sales_order, data: dict) -> None:
    profiles = (data.get("customer") or {}).get("companyContactProfiles", [])
    company_info = None
    if profiles:
        main_contact = next((p for p in profiles if p.get("isMainContact")), profiles[0])
        company_info = main_contact.get("company")

    if company_info and company_info.get("id") and company_info.get("name"):
        new_sales_order.custom_customer_company = company_info["name"]
        new_sales_order.custom_sales_type = "B2B"
    else:
        new_sales_order.custom_sales_type = "B2C"


def _set_discount_codes(new_sales_order, data: dict) -> None:
    new_sales_order.custom_shopify_discount_codes = ",".join(data.get("discountCodes", []))

    coupon_code, discount_reason = "", ""
    for edge in data.get("discountApplications", {}).get("edges", []):
        node = edge.get("node", {})
        if node.get("__typename") == "DiscountCodeApplication":
            coupon_code = node.get("code") or coupon_code
        elif node.get("__typename") == "ManualDiscountApplication":
            discount_reason = node.get("description") or node.get("title") or discount_reason

    if coupon_code:
        new_sales_order.coupon_code = coupon_code
    if discount_reason:
        new_sales_order.custom_discount_reason = discount_reason


def _apply_discount(new_sales_order, data: dict, taxes_included: bool, vat_rate: float) -> None:
    apply_on, discount_amount = get_shopify_discount_details(data, taxes_included, vat_rate * 100)
    new_sales_order.discount_amount = discount_amount or 0.0
    if discount_amount and apply_on:
        new_sales_order.apply_discount_on = apply_on


def _build_taxes(new_sales_order, data, setting_doc, taxes_included, vat_rate, tax_lines, marketplace, marketplace_order_id, currency):
    new_sales_order.taxes = []

    if not taxes_included:
        for row in build_non_inclusive_tax_rows(data, setting_doc, marketplace_order_id, currency=currency):
            row["dont_recompute_tax"] = 1
            new_sales_order.append("taxes", row)
        return

    row_counter = 0
    base_row_id = 0

    shipping_row = build_shipping_actual_row(
        data, setting_doc, taxes_included, vat_rate, marketplace, marketplace_order_id, currency=currency
    )
    if shipping_row:
        row_counter += 1
        shipping_row.update({"idx": row_counter, "dont_recompute_tax": 1})
        new_sales_order.append("taxes", shipping_row)
        base_row_id = row_counter

    duties_amount = _get_money_amount(data.get("currentTotalDutiesSet"))
    if duties_amount > 0:
        duties_row = build_duties_row(
            duties_amount, "Duties", setting_doc, marketplace, marketplace_order_id,
            taxes_included=taxes_included, vat_rate=vat_rate, currency=currency,
        )
        if duties_row:
            row_counter += 1
            duties_row.update({"idx": row_counter, "dont_recompute_tax": 1})
            new_sales_order.append("taxes", duties_row)
            base_row_id = row_counter

    for vr in build_vat_tax_rows(
        data, setting_doc, vat_rate, tax_lines, taxes_included, marketplace,
        marketplace_order_id, base_row_id=base_row_id, currency=currency,
    ):
        vr["dont_recompute_tax"] = 1
        new_sales_order.append("taxes", vr)


def _set_payment_schedule(new_sales_order, data: dict) -> None:
    if not new_sales_order.payment_schedule:
        return

    payment_terms = data.get("paymentTerms") or {}
    payment_term_name = payment_terms.get("paymentTermsName")
    payment_schedules = payment_terms.get("paymentSchedules", {}).get("nodes", [])

    if payment_term_name and frappe.db.exists("Payment Term", payment_term_name):
        new_sales_order.payment_schedule[0].payment_term = payment_term_name

    if payment_schedules and payment_schedules[0].get("dueAt"):
        new_sales_order.payment_schedule[0].due_date = get_datetime(
            payment_schedules[0]["dueAt"]
        ).date()

    if data.get("totalWeight"):
        item_weight_lb = round(float(data["totalWeight"]) / 453.59237, 2)
        new_sales_order.total_net_weight = item_weight_lb
        new_sales_order.payment_schedule[0].description = (
            f"({item_weight_lb + 1:.2f} lb: Items {item_weight_lb:.2f} lb, Package 1.0 lb)"
        )


def create_shopify_sales_order(data: dict, setting_doc: str, is_return: bool, sync_payment_entries_fn=None) -> None:
    """
    Creates (or, if already existing, syncs payments for) an ERPNext Sales
    Order from Shopify GraphQL order data.

    `sync_payment_entries_fn` is injected so this module doesn't have a hard
    import dependency on the payments module (avoids circular imports).
    Pass `shopify_selling.payments.sync_payment_entries`.
    """
    order_status = data.get("displayFulfillmentStatus") or ""
    if not is_return:
        setting = frappe.get_doc("Shopify Integration Settings", setting_doc)
        if get_fulfillment_status_to_exclude(setting, order_status):
            frappe.logger().info(f"Skipping {data.get('name')}: status '{order_status}' excluded")
            return

    marketplace = frappe.get_value("Shopify Integration Settings", setting_doc, "marketplace")
    marketplace_order_id = get_shopify_mo_id(data["name"], setting_doc)

    existing = _existing_sales_order(data["name"], marketplace)
    if existing:
        if existing.creation < add_months(now_datetime(), -1):
            return
        if sync_payment_entries_fn:
            try:
                sync_payment_entries_fn(data.get("transactions", []), existing.name, setting_doc)
            except Exception:
                frappe.log_error(
                    title="Payment Entry Sync Error (existing SO)",
                    message=f"SO {existing.name}\nTraceback: {frappe.get_traceback()}",
                )
        return

    new_sales_order = frappe.new_doc("Sales Order")
    new_sales_order.marketplace = marketplace
    new_sales_order.marketplace_order_id = marketplace_order_id

    _set_incoterm_from_tags(new_sales_order, data.get("tags", []))

    date_str = data.get("createdAt", "").split("T")[0]
    transaction_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    new_sales_order.transaction_date = transaction_date
    new_sales_order.delivery_date = add_to_date(transaction_date, days=3)

    new_sales_order.currency = _resolve_currency(data, setting_doc)

    _set_customer_and_addresses(new_sales_order, data, setting_doc)

    new_sales_order.company = frappe.get_value("Shopify Integration Settings", setting_doc, "company")
    new_sales_order.naming_series = frappe.get_value("Marketplace", marketplace, "naming_series")

    _set_sales_type(new_sales_order, data)

    #new_sales_order.po_no = data.get("poNumber")
    new_sales_order.selling_price_list = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "price_list"
    )
    new_sales_order.ignore_pricing_rule = 1
    new_sales_order.custom_fully_paid = data.get("fullyPaid")
    new_sales_order.custom_notes_for_extensiv = data.get("note")
    new_sales_order.custom_shopify_order_id_number = data.get("id")
    new_sales_order.custom_do_not_apply_drop_ship_fee = 1
    new_sales_order.custom_do_not_apply_freight_rates = 1

    _set_discount_codes(new_sales_order, data)

    taxes_included = data.get("taxesIncluded", False)
    vat_rate, _, tax_lines = extract_vat_info(data)

    _apply_discount(new_sales_order, data, taxes_included, vat_rate)

    item_rows = build_item_rows_from_shopify(data, setting_doc, taxes_included, vat_rate)
    append_item_rows(new_sales_order, item_rows)

    _build_taxes(
        new_sales_order, data, setting_doc, taxes_included, vat_rate, tax_lines,
        marketplace, marketplace_order_id, new_sales_order.currency,
    )

    try:
        new_sales_order.save()
        _set_payment_schedule(new_sales_order, data)
        new_sales_order.submit()
    except Exception:
        frappe.log_error(
            title="Sales Order Save/Submit Error",
            message=f"Traceback:{frappe.get_traceback()}\nOrder: {data.get('name')}",
        )
        return

    if sync_payment_entries_fn:
        try:
            sync_payment_entries_fn(data.get("transactions", []), new_sales_order.name, setting_doc)
        except Exception:
            frappe.log_error(
                title="Payment Entry Creation Error",
                message=f"SO {new_sales_order.name}\nTraceback: {frappe.get_traceback()}",
            )
