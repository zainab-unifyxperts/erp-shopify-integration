"""
Sales Invoice creation from a submitted Delivery Note.
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


def _allocate_advance_payment(si, sales_order_name: str) -> bool:
    """
    Reconciles any existing advance Payment Entry against this new Sales
    Invoice. Returns True if at least one advance was found and allocated,
    False if none existed (caller decides whether that's expected — e.g.
    a $0 order — or an anomaly worth flagging).
    """
    advance_entries = frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Order", "reference_name": sales_order_name, "docstatus": 1},
        fields=["parent", "allocated_amount"],
    )
    if not advance_entries:
        return False

    for row in advance_entries:
        pe = frappe.get_doc("Payment Entry", row.parent)
        pe.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": si.name,
            "allocated_amount": row.allocated_amount,
        })
        for ref in pe.references:
            if ref.reference_doctype == "Sales Order" and ref.reference_name == sales_order_name:
                ref.allocated_amount = 0
        pe.save()
        pe.submit()

    return True
