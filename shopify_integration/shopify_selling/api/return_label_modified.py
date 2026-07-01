import frappe
import json
import hmac
import hashlib
import base64
import requests
from shopify_integration.shopify_selling.shopify_selling_utils import sync_order_not_found, upload_return_label_rma, verify_webhook
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice, make_delivery_note
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from shopify_integration.shopify_selling.api.shipwise_integration import create_return_label
from shopify_integration.shopify_selling.api.return_item_status import return_item_to_warehouse
from extensiv_integration.extensiv_setup.shopify_fulfillment_sync import sync_delivery_note
from extensiv_integration.extensiv_selling.orders import get_token
from dataclasses import dataclass
from typing import Optional


# ──────────────────────────────────────────────
# Data contracts  (replaces loose dicts)
# ──────────────────────────────────────────────

@dataclass
class ReturnLineItem:
    qty:int
    line_item_id: str
    sku: str
    order_item_id: str


@dataclass
class ReturnContext:
    """Everything the background job needs, assembled once in handle_return."""
    return_id: str
    shopify_order_gid: str
    so_name: Optional[str]
    return_line_items: list[ReturnLineItem]
    return_reason: Optional[str]
    return_reason_note: Optional[str]
    setting_doc_name: str = "alphardgolf-usa"


class ReturnProcessingError(Exception):
    """
    Raised by any step that cannot recover.
    Carries a human-readable step name so the orchestrator
    can log and create an Issue without guessing where it failed.
    """

    def __init__(self, step: str, detail:str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")

# ──────────────────────────────────────────────
# Webhook entry point  (must respond in < 5 s)
# ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def handle_return():
    """
    it triggers from shopify, gets it payload,
    and calls the enqueue fn to create return doc, 
    which eventually generates the return label and uploads on shopify
    """
    frappe.log_error(title="incoming headers", message=dict(frappe.request.headers))
    raw_data = frappe.request.get_data()
    frappe.log_error(title="raw bytes", message=f"len={len(raw_data)} | data={raw_data}")
    hmac_header = frappe.request.headers.get("X-Shopify-Hmac-SHA256")
    client_secret = frappe.get_value(
            "Shopify Integration Settings", {"enabled": 1}, "api_secret"
        )
    
    if not verify_webhook(raw_data, hmac_header, client_secret):
        frappe.log_error(title="Webhook verification failed", message=str(frappe.request.headers))
        raise frappe.AuthenticationError
    

    try:
        json_data = json.loads(raw_data)
    except json.JSONDecodeError:
        frappe.log_error(title="handle_return: invalid JSON", message=raw_data)
        raise frappe.ValidationError("Invalid JSON payload")

    # check if the doctype is already returned or not.
    already_returned = handle_is_returned(so_name) # set return_status = 0
    if already_returned == True:
        frappe.log_error(title="Return Status Closed For Earlier Return", message=f"Moving forward with next return: {so_name}")
   
    try:
        ctx = _build_return_context(json_data)
        frappe.log_error("CTX_data", ctx)
    except Exception:
        frappe.log_error(title="handle_return: payload parsing failed", message=frappe.get_traceback())
        raise frappe.ValidationError("Could not parse return payload")

    frappe.enqueue(
        "shopify_integration.shopify_selling.api.return_label.process_return_background",
        queue="long",
        # pass as plain dict — frappe serialises args to JSON for the queue
        ctx=_ctx_to_dict(ctx),
    )

    return {"status": "queued"}


def handle_is_returned(so_name)-> bool | None:
    
    if not so_name: return

    custom_shopify_return_status = frappe.get_value("Sales Order", so_name, "custom_shopify_return")
    receiver_id = frappe.get_value("Sales Order", so_name, "custom_receiver_id")

    if receiver_id and custom_shopify_return_status == 1:
        frappe.db.set_value(
            "Sales Order",
            so_name,
            {
            "custom_shopify_return": 0,
            }
        )
        frappe.db.commit()
        return True


    return False   



# ──────────────────────────────────────────────
# Background orchestrator
# ──────────────────────────────────────────────

def process_return_background(ctx: dict):
    """
    Orchestrates the full return flow.
    Each step is isolated; failure in one step raises ReturnProcessingError
    which is caught here so we always log + create an Issue instead of
    silently swallowing the error.
    """
    frappe.set_user("Administrator")
    ctx = _ctx_from_dict(ctx)

    try:
        # ── Step 1: guarantee SO exists ──────────────────────
        ctx.so_name = _ensure_so_exists(ctx)

        # ── Step 3: stamp return metadata on SO ──────────────
        _update_so_fields(ctx)

        # ── Step 5: generate label, upload, notify Extensiv ──
        _handle_return_label(ctx)

    except ReturnProcessingError as exc:
        _create_issue(f"Return failed at [{exc.step}]", f"SO: {ctx.so_name}\n{exc.detail}")
        frappe.log_error(
            title=f"process_return_background — step failed: {exc.step}",
            message=str(exc.detail)
        )
    except Exception:
        _create_issue("process_return_background: unexpected error", ctx.so_name or ctx.shopify_order_gid)
        frappe.log_error(
            title="process_return_background: unexpected error",
            message=frappe.get_traceback()
        )



# ──────────────────────────────────────────────
# Payload parsing helpers
# ──────────────────────────────────────────────

def _build_return_context(json_data: dict) -> ReturnContext:
    """Parses the raw Shopify webhook payload into a typed ReturnContext."""
    shopify_order_gid = json_data.get("order", {}).get("admin_graphql_api_id")
    so_name = frappe.get_value(
        "Sales Order",
        {"custom_shopify_order_id_number": shopify_order_gid},
        "name"
    ) or ""

    so_items = frappe.get_all(
        "Sales Order Item",
        filters={"parent": so_name} if so_name else {"parent": "__nonexistent__"},
        fields=["custom_shopify_line_item_id", "item_code", "custom_extensiv_order_item_id"]
    )
    line_item_sku_map = {r.custom_shopify_line_item_id: r.item_code for r in so_items}
    line_item_order_item_map = {r.custom_shopify_line_item_id: r.custom_extensiv_order_item_id for r in so_items}

    return_line_items = []
    return_reason = return_reason_note = None

    for raw_item in json_data.get("return_line_items", []):
        fulfillment_li = raw_item.get("fulfillment_line_item", {})
        line_item = fulfillment_li.get("line_item", {})
        lid = line_item.get("admin_graphql_api_id")
        return_reason = raw_item.get("return_reason")
        return_reason_note = raw_item.get("return_reason_note")

        return_line_items.append(ReturnLineItem(
            qty=raw_item.get("quantity"),
            line_item_id=lid,
            sku=line_item_sku_map.get(lid),
            order_item_id=line_item_order_item_map.get(lid),
        ))

    return ReturnContext(
        return_id=json_data.get("admin_graphql_api_id"),
        shopify_order_gid=shopify_order_gid,
        so_name=so_name,
        return_line_items=return_line_items,
        return_reason=return_reason,
        return_reason_note=return_reason_note,
    )


def _ctx_to_dict(ctx: ReturnContext) -> dict:

    return {
        "return_id": ctx.return_id,
        "shopify_order_gid":ctx.shopify_order_gid,
        "so_name": ctx.so_name,
        "return_line_items": [vars(i) for i in ctx.return_line_items],
        "return_reason": ctx.return_reason,
        "return_reason_note": ctx.return_reason_note,
        "setting_doc_name": ctx.setting_doc_name
    }



class ReturnContext:
    """Everything the background job needs, assembled once in handle_return."""
    return_id: str
    shopify_order_gid: str
    so_name: Optional[str]
    return_line_items: list[ReturnLineItem]
    return_reason: Optional[str]
    return_reason_note: Optional[str]
    setting_doc_name: str = "alphardgolf-usa"

























