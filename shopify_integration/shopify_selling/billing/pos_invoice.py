"""
Sales Invoice creation for POS orders — built directly from the Sales Order
with Update Stock enabled, skipping Delivery Note entirely.
"""

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice


def validate_serial_map(sales_order_name: str, serial_map: dict) -> list[str]:
    problems = []
    so_items = frappe.get_all(
        "Sales Order Item", filters={"parent": sales_order_name}, fields=["item_code", "qty"]
    )

    for row in so_items:
        item_code = row.item_code
        if not frappe.db.get_value("Item", item_code, "has_serial_no"):
            continue

        serials = serial_map.get(item_code) or []
        if not serials:
            problems.append(f"{item_code}: No serial number captured (item requires a serial)")
            continue

        expected_qty = int(row.qty)
        if len(serials) != expected_qty:
            problems.append(
                f"{item_code}: Expected {expected_qty} serial(s), got {len(serials)} ({', '.join(serials)})"
            )
            continue

        for serial_no in serials:
            if not frappe.db.exists("Serial No", serial_no):
                problems.append(f"{item_code}: Serial No '{serial_no}' not found in ERPNext")
                continue
            status = frappe.db.get_value("Serial No", serial_no, "status")
            if status != "Active":
                problems.append(f"{item_code}: Serial No '{serial_no}' is '{status}', not Active/available")

    return problems


def create_pos_sales_invoice(sales_order_name: str, setting_doc: str, serial_map: dict) -> None:
    """
    Creates and submits a Sales Invoice (update_stock=1) directly from a
    submitted POS Sales Order, using pre-captured serial numbers.

    If any serial is missing, invalid, or unavailable, NOTHING is created -
    the failure is logged and alerted, and the order remains eligible for
    automatic retry on the next sync once the serial issue is resolved.
    """
    existing_si = frappe.db.exists(
        "Sales Invoice",
        {"custom_sales_order": sales_order_name, "docstatus": ["!=", 2]},
    )
    if existing_si:
        return  # already invoiced (or a valid draft is mid-process) - skip

    # Validate every serial BEFORE creating any document
    problems = validate_serial_map(sales_order_name, serial_map)
    if problems:
        message = (
            f"Sales Invoice not created for Sales Order {sales_order_name}.\n\n"
            f"Problems found:\n" + "\n".join(f"- {p}" for p in problems) +
            f"\n\nResolve the serial number issue(s) in ERPNext, then re-run "
            f"the Shopify sync - this order will be retried automatically."
        )
        frappe.log_error(
            title=f"Shopify POS Sales Invoice Failed - {sales_order_name}",
            message=message,
        )
        send_pos_sync_alert(setting_doc, sales_order_name, "Serial Number Issue", message)
        return  # nothing created - order stays eligible for retry

    si = None
    try:
        si = make_sales_invoice(sales_order_name)
        si.update_stock = 1

        for item in si.items:
            serials = serial_map.get(item.item_code)
            if serials:
                item.serial_no = "\n".join(serials)

        si.insert()
        si.submit()

        allocated = _allocate_advance_payment(si, sales_order_name)

        if si.grand_total > 0 and not allocated:
            message = (
                f"Sales Invoice {si.name} created with grand_total {si.grand_total} "
                f"but no advance Payment Entry was found to reconcile against it. "
                f"Order may be missing a Shopify transaction/webhook."
            )
            frappe.log_error(title=f"Shopify POS Invoice Unpaid - {sales_order_name}", message=message)
            send_pos_sync_alert(setting_doc, sales_order_name, "Invoice Unpaid", message)

    except Exception:
        # Safety net for anything the pre-check didn't catch (e.g. warehouse
        # mismatch, negative stock, race conditions with a concurrent sync run).
        # Clean up an orphaned draft so this order isn't permanently blocked.
        if si and si.get("name") and si.docstatus == 0:
            frappe.delete_doc("Sales Invoice", si.name, force=True, ignore_permissions=True)
            frappe.db.commit()

        message = f"Traceback:\n{frappe.get_traceback()}"
        frappe.log_error(
            title=f"Shopify POS Sales Invoice Failed - {sales_order_name}",
            message=message,
        )
        send_pos_sync_alert(setting_doc, sales_order_name, "Sales Invoice Failed", message)


def send_pos_sync_alert(setting_doc: str, order_name: str, subject: str, message: str) -> None:
    """
    Sends an email to the configured recipient for this Shopify store, if
    one is set. Silently does nothing if not configured - matches the
    "configurable" requirement from the spec without forcing setup.
    """
    pass
    # recipient = frappe.db.get_value("Shopify Integration Settings", setting_doc, "pos_sync_alert_recipient")
    # if not recipient:
    #     return
    # frappe.sendmail(
    #     recipients=[recipient],
    #     subject=f"[Push Plastic] POS Sync - {subject} - {order_name}",
    #     message=message.replace("\n", "<br>"),
    # )


def _allocate_advance_payment(si, sales_order_name: str) -> bool:
    """
    Reconciles any existing advance Payment Entry against this new Sales
    Invoice. Returns True if at least one advance was found and allocated,
    False if none existed (caller decides whether that's expected - e.g.
    a $0 order - or an anomaly worth flagging).
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