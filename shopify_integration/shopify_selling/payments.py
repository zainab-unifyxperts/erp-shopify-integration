"""
Payment Entry creation from Shopify order transactions.

Merged from your finance.py. Fixes vs original:
- bare `except:` -> `except Exception:` with real traceback logging (unchanged
  behavior, just no longer silently swallows non-Shopify errors)
- setting_doc lookups now use setting_doc_name directly instead of the
  `{"name": setting_doc_name, "enabled": 1}` filter pattern (works the same,
  just simpler and doesn't silently return None if the doc got disabled
  mid-sync)
- Sales Invoice creation (a separate concern from payments) split into
  invoice.py
"""

import datetime

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe.utils import convert_utc_to_system_timezone


def sync_payment_entries(transactions: list[dict], sales_order_name: str, setting_doc_name: str = None) -> None:
    """
    Creates a Payment Entry for each Shopify transaction on this order that
    doesn't already have one, and skips orders that already have a submitted
    Sales Invoice (invoiced orders are reconciled differently).
    """
    if not transactions:
        return

    customer = frappe.get_value("Sales Order", sales_order_name, "customer")
    company = frappe.get_value("Sales Order", sales_order_name, "company")

    if frappe.db.exists("Sales Invoice", {"sales_order": sales_order_name, "docstatus": 1}):
        return

    for transaction in transactions:
        payment_id = transaction.get("paymentId")
        if not payment_id:
            continue

        if frappe.db.exists(
            "Payment Entry",
            {
                "reference_no": payment_id,
                "party": customer,
                "company": company,
                "docstatus": 1,
            },
        ):
            continue

        create_shopify_payment_entry(transaction, sales_order_name, setting_doc_name)


def _resolve_paid_to_account(gateway: str, setting_doc_name: str) -> str:
    """
    Resolves which account a Shopify payment gateway should be booked to.
    Falls back to the settings' default_payment_account, logging when the
    fallback is used for a gateway that doesn't have an explicit mapping.
    """
    default_account = frappe.get_value(
        "Shopify Integration Settings", setting_doc_name, "default_payment_account"
    )

    if not gateway:
        return default_account

    # Per-gateway account mapping is optional. If the mapping doctype isn't
    # installed on this site, just use the default account.
    if not frappe.db.exists("DocType", "Shopify Payment Account Table"):
        return default_account

    mapped_account = frappe.db.get_value(
        "Shopify Payment Account Table", {"gateway": gateway}, "account"
    )
    if mapped_account:
        return mapped_account

    frappe.log_error(
        title="Shopify Payment Gateway Not Mapped",
        message=f"No account mapping for gateway '{gateway}' — using default_payment_account",
    )
    return default_account


def create_shopify_payment_entry(data: dict, sales_order_name: str, setting_doc_name: str) -> None:
    """
    Creates and submits a Payment Entry for a single Shopify transaction.

    data: one node from the Shopify GraphQL `transactions` list
          (amountSet, gateway, createdAt, paymentId)
    """
    try:
        payment_entry_doc = get_payment_entry(dt="Sales Order", dn=sales_order_name)
        payment_entry_doc.paid_amount = float(data["amountSet"]["shopMoney"]["amount"])
        payment_entry_doc.paid_to = _resolve_paid_to_account(data.get("gateway"), setting_doc_name)
        payment_entry_doc.reference_no = data["paymentId"]
        payment_entry_doc.cost_center = frappe.get_value(
            "Shopify Integration Settings", setting_doc_name, "default_cost_center"
        )

        date = datetime.datetime.strptime(data["createdAt"], "%Y-%m-%dT%H:%M:%SZ")
        payment_entry_doc.reference_date = convert_utc_to_system_timezone(date).date()

        payment_entry_doc.save()
        payment_entry_doc.submit()
    except Exception:
        frappe.log_error(
            title="Shopify Payment Entry Creation Error",
            message=f"Payment ID: {data.get('paymentId')}\nSO: {sales_order_name}\nTraceback:\n{frappe.get_traceback()}",
        )
