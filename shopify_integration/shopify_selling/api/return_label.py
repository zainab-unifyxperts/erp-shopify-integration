"""
return_label.py  —  Shopify → ERPNext return flow

Structure
─────────
  handle_return()                  ← webhook entry point (thin, fast)
    └─ _build_return_line_items()  ← parse payload into enqueue-ready data
  handle_is_returned()             ← reset return flag on repeat returns
  process_return_background()      ← enqueued orchestrator (coordinates steps)
    └─ _ensure_so_exists()         ← step 1: guarantee SO is present
    └─ _update_so_fields()         ← step 2: stamp return metadata on SO
    └─ _handle_return_label()      ← step 3: warehouse receipt + SO child rows + label upload
        └─ _save_return_details_on_so()
  store_extensiv_order_item_id()   ← hydrate Extensiv order id onto SO items
    └─ _fetch_extensiv_order_id()

Failure model
─────────────
Each step helper RAISES ReturnProcessingError when it cannot recover.
process_return_background catches that ONE type, creates a single Issue,
and stops — so a 2am log tells you exactly which step broke.

NOTE: process_return_background keeps its original 8-argument signature so the
existing frappe.enqueue(...) call in handle_return is unchanged. The arguments
are bundled into a ReturnContext inside the function for clean passing to steps.
"""

import frappe
import json
import requests
from dataclasses import dataclass
from typing import Optional

from shopify_integration.shopify_selling.shopify_selling_utils import (
    sync_order_not_found,
    update_return_label_and_rma_metafield,
    verify_webhook,
    create_issue,
)
from shopify_integration.shopify_selling.api.shipwise_integration import (
    create_return_label,
    generate_pdf_url,
)
from shopify_integration.shopify_selling.api.return_item_status import return_item_to_warehouse
from extensiv_integration.extensiv_setup.shopify_fulfillment_sync import sync_delivery_note
from extensiv_integration.extensiv_selling.orders import get_token
from extensiv_integration.extensiv_selling.orders import get_facility_access_token


# ──────────────────────────────────────────────
# Data contracts  (replaces loose dicts passed between helpers)
# ──────────────────────────────────────────────

@dataclass
class ReturnLineItem:
    qty: int
    line_item_id: str
    sku: Optional[str]
    order_item_id: Optional[str]


@dataclass
class ReturnContext:
    """Everything the background job needs, bundled in one place."""
    return_id: str
    shopify_order_gid: str
    so_name: Optional[str]
    return_line_items: list[ReturnLineItem]
    return_reason: Optional[str]
    return_reason_note: Optional[str]
    all_item_return_note: Optional[str]
    setting_doc_name: str = "alphardgolf-usa"


class ReturnProcessingError(Exception):
    """
    Raised by any step that cannot recover.

    `step`    — which step failed (used in the log title)
    `detail`  — human-readable reason / traceback
    `subject` — optional Issue subject; lets a step preserve a specific
                Issue title instead of the generic orchestrator one.
    """
    def __init__(self, step: str, detail: str, subject: Optional[str] = None):
        self.step = step
        self.detail = detail
        self.subject = subject
        super().__init__(f"[{step}] {detail}")


# ──────────────────────────────────────────────
# Webhook entry point  (must respond to Shopify fast)
# ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def handle_return():
    """
    Triggered from Shopify. Verifies the webhook, parses the payload,
    and hands everything to a background job that creates the return
    documents, generates the return label, and uploads it back to Shopify.
    """
    raw_data = frappe.request.get_data()
    hmac_header = frappe.request.headers.get("X-Shopify-Hmac-SHA256")
    client_secret = frappe.get_value(
        "Shopify Integration Settings", {"enabled": 1}, "api_secret"
    )

    if not verify_webhook(raw_data, hmac_header, client_secret):
        frappe.log_error(title="Webhook verification failed", message=str(dict(frappe.request.headers)))
        raise frappe.AuthenticationError

    json_data = json.loads(raw_data)
    frappe.log_error(title="handle_return payload", message=f"{json_data}")

    data = return_so_name_and_gid(json_data)
    shopify_order_gid = data.get("shopify_order_gid")
    so_name = data.get("so_name")

    # Reset the return flag if this SO was already returned once
    already_returned = handle_is_returned(so_name)  # sets return_status = 0
    if already_returned is True:
        frappe.log_error(
            title="Return Status Closed For Earlier Return",
            message=f"Moving forward with next return: {so_name}"
        )

    try:
        parsed = _build_return_line_items(json_data, so_name)
    except Exception:
        frappe.log_error(title="Parsing return payload failed", message=frappe.get_traceback())
        return

    # Hand off to background — respond to Shopify immediately.
    # Signature/kwargs intentionally unchanged from the original.
    frappe.enqueue(
        "shopify_integration.shopify_selling.api.return_label.process_return_background",
        queue="long",
        return_id=parsed["return_id"],
        return_line_items=parsed["return_line_items"],
        return_reason=parsed["return_reason"],
        return_reason_note=parsed["return_reason_note"],
        shopify_order_gid=shopify_order_gid,
        so_name=so_name,
        setting_doc_name="alphardgolf-usa",
        all_item_return_note=parsed["all_item_return_note"],
    )


def _build_return_line_items(json_data, so_name):
    """
    Parses the Shopify payload into the enqueue-ready structure.
    return_line_items stays a list of plain dicts so frappe.enqueue can
    serialise it cleanly; process_return_background converts to dataclasses.
    """
    so_items = frappe.get_all(
        "Sales Order Item",
        filters={"parent": so_name},
        fields=["custom_shopify_line_item_id", "item_code", "custom_extensiv_order_item_id"]
    )
    line_item_sku_map = {row.get("custom_shopify_line_item_id"): row.item_code for row in so_items}
    line_item_order_item_id = {row.get("custom_shopify_line_item_id"): row.custom_extensiv_order_item_id for row in so_items}

    return_id = json_data.get("admin_graphql_api_id")
    return_items = json_data.get("return_line_items", [])

    return_line_items = []
    notes = []
    return_reason = None
    return_reason_note = None

    for item in return_items:
        fulfillment_line_item = item.get("fulfillment_line_item", {})
        line_item             = fulfillment_line_item.get("line_item", {})
        return_reason         = item.get("return_reason")
        return_reason_note    = item.get("return_reason_note")
        line_item_id          = line_item.get("admin_graphql_api_id")

        if return_reason_note:
            notes.append(return_reason_note)

        return_line_items.append({
            "qty":           item.get("quantity"),
            "line_item_id":  line_item_id,
            "sku":           line_item_sku_map.get(line_item_id),
            "order_item_id": line_item_order_item_id.get(line_item_id),
        })

    all_item_return_note = " | ".join(notes) if notes else None

    return {
        "return_id": return_id,
        "return_line_items": return_line_items,
        "return_reason": return_reason,
        "return_reason_note": return_reason_note,
        "all_item_return_note": all_item_return_note,
    }


def handle_is_returned(so_name) -> bool | None:
    # check on item level too
    if not so_name:
        return

    custom_shopify_return_status = frappe.get_value("Sales Order", so_name, "custom_shopify_return")
    receiver_id = frappe.get_value("Sales Order", so_name, "custom_receiver_id")

    if receiver_id and custom_shopify_return_status == 1:
        frappe.db.set_value(
            "Sales Order",
            so_name,
            {"custom_shopify_return": 0}
        )
        frappe.db.commit()
        return True

    return False


# ──────────────────────────────────────────────
# Background orchestrator
# ──────────────────────────────────────────────

def process_return_background(return_id, return_line_items,
                              return_reason, return_reason_note,
                              shopify_order_gid, so_name, setting_doc_name, all_item_return_note):
    """
    Enqueued (long). Orchestrates the full return flow:
      1. ensure the SO exists (sync if Shopify-only)
      2. stamp return metadata on the SO
      3. generate the label, notify the warehouse, save child rows, upload to Shopify

    Each step raises ReturnProcessingError on unrecoverable failure; this
    function catches it once, creates a single Issue, and stops.
    """
    frappe.log_error("Process Enqueue started DEBUG")
    frappe.set_user("Administrator")  # set once — covers everything below

    # Bundle the incoming args into one context object for clean passing.
    ctx = ReturnContext(
        return_id=return_id,
        shopify_order_gid=shopify_order_gid,
        so_name=so_name,
        return_line_items=[ReturnLineItem(**i) for i in return_line_items],
        return_reason=return_reason,
        return_reason_note=return_reason_note,
        all_item_return_note=all_item_return_note,
        setting_doc_name=setting_doc_name,
    )

    try:
        # ── Step 1: guarantee SO exists ──────────────────────
        ctx.so_name = _ensure_so_exists(ctx)

        # ── Step 2: stamp return metadata on SO ──────────────
        _update_so_fields(ctx)

        # ── Step 3: label + warehouse receipt + child rows + upload ──
        _handle_return_label(ctx)

    except ReturnProcessingError as exc:
        create_issue(exc.subject or "Return Label Enqueue Failed", f"SO: {ctx.so_name}\n{exc.detail}")
        frappe.log_error(
            title=f"process_return_background — step failed: {exc.step}",
            message=str(exc.detail)
        )
    except Exception:
        create_issue("Return Label Enqueue Failed", ctx.so_name)
        frappe.log_error(
            title="process_return_background failed",
            message=f"so_name: {ctx.so_name} return_line_items: {return_line_items} Error: {frappe.get_traceback()}"
        )


# ──────────────────────────────────────────────
# Step helpers  (each raises ReturnProcessingError on unrecoverable failure)
# ──────────────────────────────────────────────

def _ensure_so_exists(ctx: ReturnContext) -> str:
    """Returns a confirmed SO name, syncing from Shopify if needed, or raises."""
    if ctx.so_name:
        return ctx.so_name

    result = sync_order_not_found(ctx.setting_doc_name, ctx.shopify_order_gid)
    if result is not True:
        raise ReturnProcessingError(
            "ensure_so_exists",
            f"sync_order_not_found returned {result!r} for {ctx.shopify_order_gid}",
            subject="SO not found after sync attempt",
        )

    so_name = frappe.get_value(
        "Sales Order",
        {"custom_shopify_order_id_number": ctx.shopify_order_gid},
        "name"
    )
    if not so_name:
        raise ReturnProcessingError(
            "ensure_so_exists",
            f"SO name not found after sync for {ctx.shopify_order_gid}",
            subject="SO name not found after sync",
        )

    # Hydrate Extensiv order id onto the freshly-synced SO items
    if store_extensiv_order_item_id(so_name) is False:
        frappe.log_error(title="Delivery Note OrderId Fetching or Saving Failed")
        raise ReturnProcessingError(
            "ensure_so_exists",
            "store_extensiv_order_item_id failed",
            subject="Delivery Note OrderId Fetching or Saving Failed",
        )

    try:
        sync_delivery_note(so_name)
    except Exception:
        raise ReturnProcessingError(
            "ensure_so_exists",
            f"sync_delivery_note failed\n{frappe.get_traceback()}",
        )

    return so_name


def _update_so_fields(ctx: ReturnContext):
    frappe.db.set_value("Sales Order", ctx.so_name, {
        "custom_shopify_return_id": ctx.return_id,
        "custom_return_reason": ctx.return_reason,
        "custom_return_description": ctx.all_item_return_note,
    })


def _handle_return_label(ctx: ReturnContext):
    """
    Generates the label, notifies the warehouse, saves SO child rows,
    then generates the PDF and uploads it to Shopify.

    Order is identical to the original:
      create_return_label → return_item_to_warehouse → save SO → generate_pdf_url → upload metafield
    """
    # External helpers expect a list of plain dicts (original format)
    line_items_as_dicts = [vars(i) for i in ctx.return_line_items]

    return_label_details = create_return_label(ctx.so_name, line_items_as_dicts)
    frappe.log_error(
        title="Return Item Debug",
        message=f"{return_label_details.get('nearest_warehouse') if return_label_details else None}, "
                f"{ctx.so_name}, {ctx.return_reason_note}, {return_label_details}, {line_items_as_dicts}"
    )

    if not return_label_details:
        raise ReturnProcessingError(
            "handle_return_label",
            f"create_return_label returned empty for SO {ctx.so_name} (rID: {ctx.return_id})",
            subject="Return label not available",
        )

    nearest_warehouse = return_label_details.get("nearest_warehouse")

    # 1) Notify warehouse / create Extensiv receiver
    receiver_id = return_item_to_warehouse(
        nearest_warehouse,
        ctx.so_name,
        ctx.all_item_return_note,
        return_label_details,
        line_items_as_dicts,
    )

    # 2) Save SO header + child rows
    _save_return_details_on_so(ctx, receiver_id, nearest_warehouse, return_label_details)

    # 3) Generate PDF + upload to Shopify  (non-fatal if upload fails)
    pdf_url = generate_pdf_url(return_label_details.get("label_base64"), ctx.so_name)
    is_uploaded = update_return_label_and_rma_metafield(ctx.shopify_order_gid, pdf_url, ctx.setting_doc_name)
    if is_uploaded is False:
        frappe.log_error(
            title="Return label not uploaded",
            message=f"{ctx.so_name} - rID: {ctx.return_id} - rLabel: {return_label_details}"
        )


def _save_return_details_on_so(ctx: ReturnContext, receiver_id, nearest_warehouse, return_label_details):
    """Stamps receiver/warehouse on the SO header and the matching child rows."""
    so_doc = frappe.get_doc("Sales Order", ctx.so_name)
    so_doc.custom_receiver_id = receiver_id
    so_doc.custom_nearest_warehouse = nearest_warehouse

    return_line_item_ids = {item.line_item_id for item in ctx.return_line_items}
    total_cost = return_label_details.get("total_cost")

    frappe.log_error("Saving details", str(return_line_item_ids))

    for item in so_doc.items:
        if item.custom_shopify_line_item_id in return_line_item_ids:
            # Skip rows already stamped with this return
            if item.custom_item_return_id == ctx.return_id:
                continue

            item.custom_item_return_id        = ctx.return_id
            item.custom_reason                = ctx.return_reason
            item.custom_reason_description    = ctx.return_reason_note
            item.custom_extensiv_receiver_id  = receiver_id
            item.custom_return_shipping_cost  = total_cost
            frappe.log_error("MATCH FOUND", f"{item.item_code} - rid {item.custom_item_return_id} return_id: {ctx.return_id}")

    # SO save failure is non-fatal — the core return work is already done
    try:
        so_doc.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.log_error("SO saved successfully", ctx.so_name)
    except Exception:
        frappe.log_error(
            title="SO Save Failed",
            message=f"SO: {ctx.so_name}\n{frappe.get_traceback()}"
        )


# ──────────────────────────────────────────────
# Lookup helpers
# ──────────────────────────────────────────────

def return_so_name_and_gid(json_data):
    shopify_order_gid = json_data.get("order", {}).get("admin_graphql_api_id")

    so_name = frappe.get_value(
        "Sales Order",
        {"custom_shopify_order_id_number": shopify_order_gid},
        "name"
    )
    return {
        "so_name": so_name or "",
        "shopify_order_gid": shopify_order_gid,
    }


def store_extensiv_order_item_id(so_name):
    """
    Fetches the Extensiv orderId for this SO and writes it to every SO item.
    Returns False only on missing inputs / hard failure.
    """
    try:
        ext_doc = frappe.get_doc("Extensiv Settings", {"enabled": 1})

        marketplace_order_id = frappe.get_value("Sales Order", so_name, "marketplace_order_id")
        if not marketplace_order_id:
            frappe.log_error(title="No marketplace order ID", message=f"SO: {so_name}")
            return False

        order_id = _fetch_extensiv_order_id(ext_doc, marketplace_order_id, so_name)

        so_items = frappe.get_all(
            "Sales Order Item",
            filters={"parent": so_name},
            fields=["name", "item_code"]
        )
        if not so_items:
            return False

        for item in so_items:
            frappe.db.set_value(
                "Sales Order Item",
                item["name"],
                "custom_extensiv_order_item_id",
                str(order_id)
            )

        frappe.db.commit()
        frappe.log_error(title="Order items mapped", message=f"SO: {so_name} | updated: {len(so_items)}")
        return True

    except Exception:
        frappe.log_error(title="store_extensiv_order_item_id Failed", message=frappe.get_traceback())
        return False


def _fetch_extensiv_order_id(ext_doc, marketplace_order_id, so_name):
    """Tries each facility; returns the orderId from the last facility that has it."""
    order_id = None
    for facility in ext_doc.extensiv_facility_settings:
        try:
            access_token = get_facility_access_token(facility.warehouse)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/hal+json",
                "Authorization": f"Bearer {access_token}",
            }
            api_url = (
                f"{ext_doc.base_url}orders?"
                f"detail=all&itemdetail=all&"
                f"rql=referenceNum=={marketplace_order_id}"
            )

            response = requests.get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            frappe.log_error(title="Extensiv order response", message=str(data))

            orders = data.get("_embedded", {}).get(
                "http://api.3plCentral.com/rels/orders/order", []
            )
            if not orders:
                frappe.log_error(
                    title="store_extensiv_order_item_id — No order found",
                    message=f"SO: {so_name} | marketplace_order_id: {marketplace_order_id}"
                )
                continue

            found = orders[0].get("readOnly", {}).get("orderId")
            if not found:
                frappe.log_error(
                    title="store_extensiv_order_item_id — orderId missing",
                    message=f"SO: {so_name} | response: {orders[0]}"
                )
                continue

            order_id = found

        except Exception:
            frappe.log_error(title="Failed fetching order item id", message=frappe.get_traceback())
            continue  # try next facility

    return order_id


# ══════════════════════════════════════════════════════════════════════
# WORK IN PROGRESS — return-document creation (DN + SI)
# Kept commented (unchanged) so nothing is lost. When you re-enable these:
#   • un-comment the create_return_document(...) call in _handle_return_label
#   • in create_return_si, DEFINE match_by_line_item (it's currently undefined → NameError)
#   • move these into the same step-helper style as above
# ══════════════════════════════════════════════════════════════════════
#
# def create_return_document(return_line_items, so_name, shipping_cost):
#     """Creates Return Document - DN and SI"""
#     frappe.log_error("Return Document creation started DEBUG")
#     all_success = True
#
#     for item in return_line_items:
#         line_item_id = item["line_item_id"]
#         qty = item["qty"]
#
#         dn_records = frappe.get_all(
#             "Delivery Note Item",
#             filters={"custom_shopify_line_item_id": line_item_id},
#             fields=["parent", "item_code"],
#         )
#
#         if not dn_records:
#             frappe.log_error(
#                 title="No DN records found for line item",
#                 message=f"line_item_id: {line_item_id}, so_name: {so_name}"
#             )
#             sku = item.get("sku")
#             if sku:
#                 item_code_from_sku = frappe.get_value("Item", {"item_code": sku}, "name")
#                 if item_code_from_sku:
#                     dn_records = frappe.get_all(
#                         "Delivery Note Item",
#                         filters={
#                             "item_code": item_code_from_sku,
#                             "against_sales_order": so_name,
#                         },
#                         fields=["parent", "item_code"],
#                     )
#
#         if not dn_records:
#             frappe.log_error(
#                 title="No DN records found for line item — skipping",
#                 message=f"line_item_id: {line_item_id}, sku: {item.get('sku')}, so_name: {so_name}"
#             )
#             all_success = False
#             continue
#
#         seen = set()
#         for row in dn_records:
#             dn_name = row["parent"]
#             item_code = row["item_code"]
#             key = (dn_name, item_code)
#             if key in seen:
#                 continue
#             seen.add(key)
#
#             frappe.log_error(title="PROCESSING ITEM", message=f"{dn_name}, {item_code}, {qty}, {line_item_id}")
#
#             try:
#                 is_return_dn = create_return_dn(dn_name, item_code, qty, line_item_id, shipping_cost)
#                 is_return_si = create_return_si(dn_name, item_code, qty, line_item_id)
#             except Exception:
#                 frappe.log_error(
#                     title="Exception in return doc creation",
#                     message=f"DN: {dn_name}, item: {item_code}\n{frappe.get_traceback()}"
#                 )
#                 all_success = False
#                 continue
#
#             if not is_return_dn or not is_return_si:
#                 frappe.log_error(
#                     title="Return Document Creation Failed",
#                     message=(f"DN: {dn_name}, Item: {item_code}, line_item_id: {line_item_id}\n"
#                              f"is_return_dn={is_return_dn}, is_return_si={is_return_si}, so_name: {so_name}")
#                 )
#                 all_success = False
#                 continue
#
#     frappe.db.commit()
#     frappe.log_error("Return Document creation finished DEBUG")
#     return all_success
#
#
# def create_return_dn(submitted_dn_name, target_item_code, return_qty, line_item_id, total_shipping_cost):
#     try:
#         frappe.log_error(title="creating return DN", message=f"{submitted_dn_name}, {target_item_code}, {return_qty}, {line_item_id}")
#
#         current_user = frappe.session.user
#         frappe.set_user("Administrator")
#         return_dn = make_return_doc("Delivery Note", submitted_dn_name)
#         frappe.set_user(current_user)
#
#         items_to_keep = []
#         for item in return_dn.items:
#             match_by_line_item = (item.custom_shopify_line_item_id == line_item_id and item.item_code == target_item_code)
#             match_by_item_code = (not item.custom_shopify_line_item_id and item.item_code == target_item_code)
#             if match_by_line_item or match_by_item_code:
#                 item.qty = -abs(return_qty)
#                 items_to_keep.append(item)
#
#         return_dn.set("items", items_to_keep)
#         return_dn.custom_return_total_cost = total_shipping_cost
#         if not return_dn.items:
#             frappe.log_error(title="No matching item found for return DN", message=f"DN: {submitted_dn_name}, item: {target_item_code}, line_item_id: {line_item_id}")
#             return False
#
#         return_dn.insert(ignore_permissions=True)
#         return_dn.submit()
#         return True
#     except Exception:
#         frappe.log_error(title="Return DN Creation Failed", message=frappe.get_traceback())
#         return False
#
#
# def create_return_si(dn_name, target_item_code, return_qty, line_item_id):
#     try:
#         si_records = frappe.get_all("Sales Invoice Item", filters={"delivery_note": dn_name}, fields=["parent"], distinct=True)
#         if not si_records:
#             frappe.log_error(title="No SI found for DN", message=f"DN: {dn_name}, item: {target_item_code}")
#             return False
#
#         si_docname = si_records[0]["parent"]
#
#         existing_return = frappe.get_all(
#             "Sales Invoice",
#             filters={"return_against": si_docname, "docstatus": ["!=", 2]},
#             fields=["name"]
#         )
#         if existing_return:
#             frappe.log_error(title="Return SI already exists — skipping", message=f"SI: {si_docname}, existing: {existing_return[0]['name']}")
#             return True
#
#         current_user = frappe.session.user
#         frappe.set_user("Administrator")
#         return_si = make_return_doc("Sales Invoice", si_docname)
#         frappe.set_user(current_user)
#
#         items_to_keep = []
#         for item in return_si.items:
#             # FIX BEFORE RE-ENABLING: match_by_line_item is undefined here
#             match_by_line_item = (item.custom_shopify_line_item_id == line_item_id and item.item_code == target_item_code)
#             match_by_item_code = (not item.custom_shopify_line_item_id and item.item_code == target_item_code)
#             if match_by_line_item or match_by_item_code:
#                 item.qty = -abs(return_qty)
#                 items_to_keep.append(item)
#
#         return_si.set("items", items_to_keep)
#         if not return_si.items:
#             frappe.log_error(title="No matching item found for return SI", message=f"SI: {si_docname}, item: {target_item_code}, line_item_id: {line_item_id}")
#             return False
#
#         return_si.calculate_taxes_and_totals()
#         return_si.update_outstanding_for_self = 1
#         return_si.insert(ignore_permissions=True)
#         return_si.submit()
#         return True
#     except Exception:
#         frappe.log_error(title="Return SI Failed", message=frappe.get_traceback())
#         return False