"""
Sales Invoice creation from a submitted Delivery Note.
Split out of finance.py — this is a different concern from Payment Entry
sync and doesn't need to live in the same module.
"""

import frappe
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice


@frappe.whitelist()
def enqueue_create_sales_invoice(doc, method=None) -> None:
    frappe.enqueue(
        method="shopify_integration.shopify_selling.invoice.create_sales_invoice",
        queue="short",
        doc=doc,
        function=method,
    )


@frappe.whitelist()
def create_sales_invoice(doc, function=None) -> None:
    delivery_note_name = doc.name
    try:
        sales_invoice_doc = make_sales_invoice(delivery_note_name, None)
        sales_invoice_doc.save()
        sales_invoice_doc.submit()
    except Exception:
        frappe.log_error(
            title="Sales Invoice Creation Error",
            message=f"Delivery Note: {delivery_note_name}\nTraceback:\n{frappe.get_traceback()}",
        )
