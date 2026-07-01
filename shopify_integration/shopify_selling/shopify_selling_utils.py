import frappe
import requests
from requests.auth import HTTPBasicAuth
import json
import shopify
import datetime
from datetime import *
import frappe
from frappe.utils.background_jobs import enqueue
from frappe.utils import add_to_date, flt
import frappe.utils
import os
import hmac
import hashlib
import base64
import pycountry
from typing import List
from typing import Union, List
import traceback




# Get current directory to shopify_selling_utils.py
current_dir = os.path.dirname(os.path.abspath(__file__))


def setup_shop(setting_doc_name: str) -> None:
    """
    Sets up our shop for GraphQL

    Parameters:
    setting_doc_name(str) Setting doc stores all the necessary information in ERPNext

    Returns:
    None
    """
    key = frappe.get_value("Shopify Integration Settings", setting_doc_name, "api_key")
    secret = frappe.get_value(
        "Shopify Integration Settings", setting_doc_name, "api_secret"
    )
    shop_name = frappe.get_value(
        "Shopify Integration Settings", setting_doc_name, "shop_name"
    )
    shop_url = f"https://{key}{secret}@{shop_name}.myshopify.com/admin"
    shopify.ShopifyResource.set_site(shop_url)


def get_shopify_customer(customer_data: dict, setting_doc: str) -> str:
    """
    Get or create ERPNext Customer from Shopify data.
    Ensures a linked Contact is created and set as customer_primary_contact.
    """
    # Check if Customer exists
    customer_name = frappe.get_value(
        "Customer", {"customer_name": customer_data["displayName"]}, "name"
    )

    if not customer_name:
        # Create new Customer
        customer_doc = frappe.new_doc("Customer")
        customer_doc.customer_name = customer_data["displayName"]
        customer_doc.customer_type = frappe.get_value(
            "Shopify Integration Settings", setting_doc, "default_customer_type"
        )
        customer_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        customer_name = customer_doc.name

    # ✅ Always ensure a contact exists and is linked
    contact_name = get_shopify_contact(customer_data, customer_name)

    # ✅ Always set primary contact if missing
    if not frappe.get_value("Customer", customer_name, "customer_primary_contact"):
        frappe.db.set_value("Customer", customer_name, "customer_primary_contact", contact_name)
        frappe.db.commit()

    return customer_name


def get_shopify_mo_id(
    marketplace_order_id: str, setting_doc: str
) -> "marketplace_order_id":
    """
    Creates Marketplace Order ID doc if not exists

    Params:
        marketplace_order_id(str) = Marketplace Order ID
        setting_doc = Shopify Integration Settings Doc name

    Return:
        moid = Marketplace Order ID Doc name

    """
    if frappe.db.exists("Marketplace Order ID", marketplace_order_id):
        marketplace_orderId = marketplace_order_id
    else:
        mo_id_doc = frappe.new_doc("Marketplace Order ID")
        mo_id_doc.marketplace_order_id = marketplace_order_id
        mo_id_doc.marketplace = frappe.get_value(
            "Shopify Integration Settings", setting_doc, "marketplace"
        )
        mo_id_doc.save(ignore_permissions=True)
        frappe.db.commit()
        marketplace_orderId = mo_id_doc.name
    return marketplace_orderId


def create_shopify_so_item_row(data: dict, setting_doc: str) -> "item_row":
    """
    This function creates Item row for sales order

    Params:
        data(Dict) = Line Item data from Shopify Orders API
        setting_doc = Shopify Integration Settings Doc name

    Return:
        row(Dict) = returns item row with filed name as key and it's value
    """
    if data["currentQuantity"] == 0:
        return
    row = {}
    row["item_code"] = get_shopify_item_code(data["sku"], data["name"], setting_doc)
    # row["qty"] = data["quantity"]
    # will consider currentQuantity as it is the most updated qty
    row["qty"] = data["currentQuantity"]
    row["rate"] = data["discountedUnitPriceSet"]["shopMoney"]["amount"]
    row["custom_sku"] = data["sku"]
    row['custom_shopify_line_item_id'] = data['id']
    return row




def get_shopify_item_code(sku: str, name: str, setting_doc: str) -> "item_code":
    """
    Creates Item in ERP if Item code does not exists.

    Params:
        sku (str) = item code/sku
        name(str) = Name of the item
        setting_doc = Shopify Integration Settings Doc name

    Return:
        item_code(str) = value of item_code field from Item doc
    """
    # sku = sku.strip().replace(" ","")
    if sku == 'V2PUGK-warranty':
        sku = "V2PUGK- warranty"
    if frappe.db.exists("Item", {"item_code": sku}):
        item_code = sku
    else:
        # if "giftcard" in name.strip().lower().replace(" ", ""):
        #     sku = "giftcard"
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
        frappe.db.commit()
        item_code = item_doc.item_code
    return item_code


def create_shopify_so_tax_row(
    tax_data: dict, setting_doc: str, marketplace_order_id: str, taxes_included: bool = False, source: str = None, currency: str = None
) -> dict:
    row = {}
    row["account_head"] = get_shopify_account(tax_data.get("title", "Tax"), setting_doc)
    row["description"] = tax_data.get("title", "Tax")
    row["marketplace"] = frappe.get_value("Shopify Integration Settings", setting_doc, "marketplace")
    row["marketplace_order_id"] = marketplace_order_id

    # discounted price takes precedence
    row["tax_amount"] = float(
        tax_data.get("discountedPriceSet", {})
        .get("shopMoney", {})
        .get("amount") or
        tax_data.get("priceSet", {})
        .get("shopMoney", {})
        .get("amount", 0.0)
    )

    # taxes_included logic
    if source == "tax":
        if taxes_included:
            row["included_in_print_rate"] = 1
            row["charge_type"] = "On Net Total"
        else:
            row["included_in_print_rate"] = 0
            row["charge_type"] = frappe.get_value("Shopify Integration Settings", setting_doc, "default_tax_charge_type")
    elif source == "shipping":
        row["charge_type"] = "Actual"
        row["included_in_print_rate"] = 0
    else:
        row["charge_type"] = frappe.get_value("Shopify Integration Settings", setting_doc, "default_tax_charge_type")
        row["included_in_print_rate"] = 0
    
    row["account_currency"] = currency

    return row



def get_shopify_account(name: str, setting_doc_name: str):
    """
    This function creates Shopify Account for taxes if the given Account name does not exists

    Params:
        name(str) = Name of the Account
        setting_doc = Shopify Integration Settings Doc name

    Returns:
        account(str) = Name of the Acoount in ERP
    """
    account_name = f"Shopify {name}"
    company = frappe.get_value(
        "Shopify Integration Settings", setting_doc_name, "company"
    )
    if frappe.db.exists("Account", {"account_name": account_name, "company": company}):
        account = frappe.get_value(
            "Account", {"account_name": account_name, "company": company}, "name"
        )
    elif len(name) > 140:
        account = frappe.get_value(
            "Shopify Integration Settings", setting_doc_name, "default_tax_account"
        )
    else:
        account_doc = frappe.new_doc("Account")
        account_doc.account_name = account_name
        account_doc.company = company
        account_doc.parent_account = frappe.get_value(
            "Shopify Integration Settings",
            setting_doc_name,
            "shopify_expense_parent_account",
        )
        account_doc.save()
        frappe.db.commit()
        account = account_doc.name
    return account




def get_shopify_address(address_data: dict, customer: str):
    """
    Creates Address doc for a given customer if it does not exist

    Params:
        address_data(dict) = billingAddress/shippingAddress from Shopify Orders API
        customer(str) = Name of the Customer

    Return:
        address(str) = Name of the Address Doc.
    """
    try:
        if not address_data or not address_data.get("address1"):
            frappe.log_error(
                title="Shopify Address Missing",
                message=f"Address1 missing for Customer: {customer}\nPayload: {address_data}",
            )
            return None
        address_doc = frappe.new_doc("Address")
        address_doc.address_title = address_data.get("name")
        address_doc.address_line1 = address_data.get("address1")

        address_doc.address_line2 = address_data.get("address2")
        address_doc.city = address_data.get("city")
        address_doc.country = address_data.get("country")
        address_doc.state = address_data.get("province")
        address_doc.pincode = address_data.get("zip")

        link_row = get_link_row("Customer", customer)
        address_doc.append("links", link_row)

        
        address_doc.save(ignore_permissions=True)
        frappe.db.commit()

        return address_doc.name

    except Exception:
        frappe.log_error(
            title="Shopify Address Creation Error",
            message=f"Traceback:\n{frappe.get_traceback()}\n\nCustomer: {customer}\nPayload: {address_data}",
        )
        return None



def get_shopify_contact(contact_data: dict, customer: str) -> str: # customer: str
    """
    This function creates Contact doc if given email is not present in ERP

    Params:
        data(dict) = Customer data from Shopify Orders API

    Return:
        contact = Name of the contact doc
    """
    displayName = contact_data["displayName"]
    if frappe.db.exists("Contact Email", {"email_id": contact_data["email"]}):
        contact = frappe.get_value(
            "Contact Email", {"email_id": contact_data["email"]}, "parent"
        )

        # ✅ Ensure customer is linked to this contact
        contact_doc = frappe.get_doc("Contact", contact)
        already_linked = any(
            link.link_name == customer and link.link_doctype == "Customer"
            for link in contact_doc.links
        )

        if not already_linked:
            link_row = get_link_row("Customer", customer)
            contact_doc.append("links", link_row)
            contact_doc.save(ignore_permissions=True)
            frappe.db.commit()
    else:
        contact_doc = frappe.new_doc("Contact")
        contact_doc.first_name = displayName
        email_row = {}
        email_row["email_id"] = contact_data["email"]
        email_row["is_primary"] = 1
        contact_doc.append("email_ids", email_row)
        if contact_data["phone"] is not None:
            num_row = {}
            num_row["phone"] = contact_data["phone"]
            num_row["is_primary_phone"] = 1
            contact_doc.append("phone_nos", num_row)
        link_row = get_link_row("Customer", displayName)
        contact_doc.append("links", link_row)
        contact_doc.save()
        frappe.db.commit()
        contact = contact_doc.name
    return contact


def get_link_row(doc_type: str, link_name: str) -> "row_dictionary":
    """
    Creates link doctype table row

    Params:
        doc_type(str) = Name of the Doctype
        link_name(str) = Name of the document

    Return:
        row(dict) = returns links row with filed name as key and it's value
    """
    row = {}
    row["link_doctype"] = doc_type
    row["link_name"] = link_name
    return row

def get_shopify_setting_by_marketplace(marketplace: str) -> str:
    """
    This function returns the shopify Integration Settings doc based on marketplace.
    """
    return frappe.db.get_value(
        "Shopify Integration Settings", 
        {"marketplace": marketplace, 
         "enabled": 1},
        "name"
    )

def get_all_tracking_numbers_for_so(sales_order: str) -> list:
    all_delivery_notes = frappe.get_all(
        "Delivery Note",
        filters={
            "custom_fulfilled_on_shopify": 0,
            "against_sales_order": sales_order,
            "docstatus": 1
        },
        fields=["name", "custom_tracking_number", "grand_total", "custom_carrier"]
    )

    all_delivery_notes = sorted(all_delivery_notes, key=lambda x: x.grand_total or 0, reverse=True)
    
    tracking_numbers = []
    primary_carrier = None

    for i, delivery_note in enumerate(all_delivery_notes):
        if i == 0:
            primary_carrier = delivery_note.get("custom_carrier") or "FedEx"

        if delivery_note.custom_tracking_number:
            tracking_list = delivery_note.custom_tracking_number.split("\n")
            tracking_numbers.extend([t.strip() for t in tracking_list if t.strip()])

    seen = set()
    unique_tracking_numbers = []
    for t in tracking_numbers:
        if t not in seen:
            seen.add(t)
            unique_tracking_numbers.append(t)

    return unique_tracking_numbers, primary_carrier


def get_fulfillment_structure(sales_order: str) -> dict:
    """
    Returns structured JSON of SO items grouped by bundle/standalone,
    enriched with DN data and consolidated tracking info.
    """

    so_doc = frappe.get_doc("Sales Order", sales_order)

    # ── Step 1: Build SO item structure ──
    bundles = {}
    standalones = {}

    for item in so_doc.items:
        item_code      = item.item_code
        qty_ordered    = int(item.qty)
        shopify_lid    = item.get("custom_shopify_line_item_id")
        parent_bundle  = item.get("custom_parent_bundle_item_name")

        # use SO row id instead of item_code
        item_key = item.name

        if parent_bundle:
            if parent_bundle not in bundles:
                bundles[parent_bundle] = {
                    "shopify_line_item_id": None,
                    "children": {}
                }

            if shopify_lid and bundles[parent_bundle]["shopify_line_item_id"] is None:
                bundles[parent_bundle]["shopify_line_item_id"] = shopify_lid

            bundles[parent_bundle]["children"][item_key] = {
                "item_code": item_code,
                "qty_ordered": qty_ordered,
                "delivered_dns": [],
                "total_delivered_qty": 0,
                "fully_delivered": False,
                "_seen_dns": set()
            }

        else:
            standalones[item_key] = {
                "item_code": item_code,
                "shopify_line_item_id": shopify_lid,
                "qty_ordered": qty_ordered,
                "delivered_dns": [],
                "total_delivered_qty": 0,
                "fully_delivered": False,
                "_seen_dns": set()
            }

    # ── Step 2: Fetch all DNs for this SO with grand_total for sorting ──
    all_dns = frappe.get_all(
        "Delivery Note",
        filters={"against_sales_order": sales_order},
        fields=["name", "custom_tracking_number", "custom_carrier", "grand_total", "docstatus"],
        order_by="grand_total desc"         # highest value DN first
    )

    # ── Step 3: Enrich each item with its DN entries (deduplicated) ──
    for dn in all_dns:
        dn_items = frappe.get_all(
            "Delivery Note Item",
            filters={"parent": dn.name},
            fields=["item_code", "qty", "so_detail"]
        )

        tracking_numbers = [
            t.strip()
            for t in (dn.custom_tracking_number or "").split("\n")
            if t.strip()
        ]

        dn_entry = {
            "dn":              dn.name,
            "tracking_numbers": tracking_numbers,
            "carrier":         dn.custom_carrier or None,
            "grand_total":     dn.grand_total or 0,
            "docstatus":       dn.docstatus,
        }

        for dn_item in dn_items:
            item_code = dn_item.item_code
            qty       = int(dn_item.qty)

            # ── Standalone ──
            if dn_item.so_detail in standalones:
                entry = standalones[dn_item.so_detail]
                if dn.name not in entry["_seen_dns"]:
                    entry["_seen_dns"].add(dn.name)
                    entry["delivered_dns"].append({**dn_entry, "qty": qty})
                    if dn.docstatus == 1:
                        entry["total_delivered_qty"] += qty

            # ── Bundle child ──
            else:
                for bundle_data in bundles.values():
                    if dn_item.so_detail in bundle_data["children"]:
                        child = bundle_data["children"][dn_item.so_detail]
                        if dn.name not in child["_seen_dns"]:
                            child["_seen_dns"].add(dn.name)
                            child["delivered_dns"].append({**dn_entry, "qty": qty})
                            if dn.docstatus == 1:
                                child["total_delivered_qty"] += qty
                        break

    # ── Step 4: Compute flags + consolidated tracking ──

    # Standalone: tracking comes directly from its own (single) DN
    for item_code, data in standalones.items():
        data["fully_delivered"] = data["total_delivered_qty"] >= data["qty_ordered"]

        # Pick the highest-value submitted DN for tracking
        submitted = [d for d in data["delivered_dns"] if d["docstatus"] == 1]
        submitted.sort(key=lambda x: x["grand_total"], reverse=True)

        if submitted:
            best = submitted[0]
            data["tracking"] = {
                "carrier":          best["carrier"],
                "tracking_numbers": best["tracking_numbers"]
            }
        else:
            data["tracking"] = {"carrier": None, "tracking_numbers": []}

        # Clean up internal field
        del data["_seen_dns"]

    # Bundle: tracking collected from ALL DNs that contain any child item
    for bundle_parent, bundle_data in bundles.items():
        all_dns_involved = set()
        all_children_delivered = True

        for item_code, child in bundle_data["children"].items():
            child["fully_delivered"] = child["total_delivered_qty"] >= child["qty_ordered"]
            if not child["fully_delivered"]:
                all_children_delivered = False
            for d in child["delivered_dns"]:
                all_dns_involved.add(d["dn"])
            del child["_seen_dns"]

        bundle_data["bundle_fully_delivered"] = all_children_delivered
        bundle_data["delivery_notes_involved"] = sorted(all_dns_involved)

        # Collect tracking from ALL involved DNs (sorted by grand_total desc)
        # Build a lookup from the dns list we already fetched
        involved_dns = [d for d in all_dns if d.name in all_dns_involved and d.docstatus == 1]
        involved_dns.sort(key=lambda x: x.grand_total or 0, reverse=True)

        seen_tracking = set()
        merged_tracking_numbers = []
        carrier = None

        for dn in involved_dns:
            if carrier is None:
                carrier = dn.custom_carrier or None       # highest-value DN sets the carrier

            for t in (dn.custom_tracking_number or "").split("\n"):
                t = t.strip()
                if t and t not in seen_tracking:
                    seen_tracking.add(t)
                    merged_tracking_numbers.append(t)

        bundle_data["tracking"] = {
            "carrier":          carrier,
            "tracking_numbers": merged_tracking_numbers
        }

    # ── Step 5: Final output ──
    result = {
        "sales_order": sales_order,
        "bundles":     bundles,
        "standalones": standalones
    }

    return result

def send_tracking_info_updated(
    shopify_order_id: str, sales_order: str, tracking_baseurl: str = None
) -> dict:

    mutation_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(mutation_file_path, "r") as file:
        fulfillment_mutation = file.read()

    query_file_path = os.path.join(current_dir, "selling_query.graphql")
    with open(query_file_path, "r") as file:
        fulfillment_id_query = file.read()

    # ── Step 1: Get fulfillment structure ──
    structure = get_fulfillment_structure(sales_order)
    bundles    = structure["bundles"]
    standalones = structure["standalones"]

    # ── Step 2: Build eligible shopify_line_item_id → tracking map ──
    # Only include items that are fully delivered
    eligible = {}
    # eligible[shopify_line_item_id] = {"carrier": ..., "tracking_numbers": [...]}

    for bundle_name, bundle in bundles.items():
        if not bundle["bundle_fully_delivered"]:
            frappe.log_error(
                title=f"Bundle Skipped {sales_order}",
                message=f"Bundle '{bundle_name}' not fully delivered — skipping"
            )
            continue
        lid = bundle["shopify_line_item_id"]
        if lid:
            eligible[lid] = bundle["tracking"]

    for item_code, item in standalones.items():
        if not item["fully_delivered"]:
            # frappe.log_error(
            #     title=f"Standalone Skipped {sales_order}",
            #     message=f"Item '{item_code}' not fully delivered — skipping"
            # )
            continue
        lid = item["shopify_line_item_id"]
        if lid:
            eligible[lid] = item["tracking"]

    if not eligible:
        frappe.log_error(
            title=f"Nothing Eligible {sales_order}",
            message="No fully delivered items found — aborting Shopify fulfillment"
        )
        return {}

    # ── Step 3: Set up Shopify session ──
    marketplace = frappe.db.get_value("Sales Order", sales_order, "marketplace")
    if not marketplace:
        frappe.log_error(title="Marketplace Missing", message=f"SO {sales_order} has no marketplace set")
        return {}

    setting_doc_name = get_shopify_setting_by_marketplace(marketplace)
    if not setting_doc_name:
        frappe.log_error(title="Missing Shopify Settings", message=f"No settings for marketplace '{marketplace}'")
        return {}

    setting_doc  = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
    shop_url     = f"https://@{setting_doc.shop_name}.myshopify.com"
    session      = shopify.Session(shop_url, setting_doc.api_version, setting_doc.get_password("access_token"))
    shopify.ShopifyResource.activate_session(session)

    # ── Step 4: Fetch Shopify fulfillment order line items ──
    fulfillment_id_response = shopify.GraphQL().execute(
        query=fulfillment_id_query,
        operation_name="getOrderFulfillment",
        variables={"orderId": shopify_order_id},
    )
    fulfillment_id_json = json.loads(fulfillment_id_response)

    if "data" not in fulfillment_id_json:
        frappe.log_error(title="Fulfillment ID Error", message="Could not fetch fulfillment orders from Shopify")
        return {}

    fulfillment_orders = fulfillment_id_json["data"]["order"]["fulfillmentOrders"]["edges"]

    # ── Step 5: Match eligible line items to Shopify fulfillment order line items ──
    # Shopify groups line items under fulfillment orders — we must respect that grouping.
    # Each fulfillment order needs its own fulfillmentCreate call (or combined into one payload).
    # We group by fulfillment order and collect matching line items with their qty.

    # Build SO item map: shopify_line_item_id → qty (from DN, i.e. what was actually shipped)
    # We derive qty from the structure itself — sum of submitted DN qtys per line item
    lid_to_qty = {}

    for bundle_name, bundle in bundles.items():
        if not bundle["bundle_fully_delivered"]:
            continue
        lid = bundle["shopify_line_item_id"]
        if not lid:
            continue
        first_child = next(iter(bundle["children"].values()))
        lid_to_qty[lid] = first_child["qty_ordered"]

    for item_code, item in standalones.items():
        if not item["fully_delivered"]:
            continue
        lid = item["shopify_line_item_id"]
        if not lid:
            continue
        lid_to_qty[lid] = item["total_delivered_qty"]

    # ── Step 6: Build lineItemsByFulfillmentOrder payload ──
    line_items_by_fulfillment_order = []

    for fulfillment_order_edge in fulfillment_orders:
        node                  = fulfillment_order_edge["node"]
        fulfillment_order_id  = node["id"]
        fulfillment_items     = []

        for line_item_edge in node["lineItems"]["edges"]:
            shopify_fulfillment_line_item_id = line_item_edge["node"]["id"]
            shopify_line_item_id             = line_item_edge["node"]["lineItem"]["id"]

            if shopify_line_item_id not in eligible:
                continue

            qty = lid_to_qty.get(shopify_line_item_id, 1)
            if qty <= 0:
                continue

            fulfillment_items.append({
                "id":       shopify_fulfillment_line_item_id,
                "quantity": int(qty)
            })

        if fulfillment_items:
            line_items_by_fulfillment_order.append({
                "fulfillmentOrderId":           fulfillment_order_id,
                "fulfillmentOrderLineItems":    fulfillment_items
            })

    if not line_items_by_fulfillment_order:
        frappe.log_error(
            title=f"No Line Items Matched {sales_order}",
            message=(
                f"Eligible LIDs: {list(eligible.keys())}\n"
                f"Fulfillment Orders: {fulfillment_orders}"
            )
        )
        return {}

    # ── Step 7: Resolve tracking info ──
    # If there are multiple eligible line items with different tracking, we need to pick one
    # set of tracking for the fulfillment call. Priority: bundle tracking wins (higher value),
    # fallback to first standalone tracking found.
    #
    # Shopify's fulfillmentCreateV2 takes a single trackingInfo for the whole fulfillment.
    # If you need per-line tracking, you must fire separate fulfillment calls — handled below.

    # Group eligible line items by their tracking fingerprint so we can batch same-tracking together
    from collections import defaultdict

    tracking_groups = defaultdict(list)  # tracking_key → list of fulfillment order line item groups

    # Build a per-lid tracking lookup from eligible
    for fulfillment_order_group in line_items_by_fulfillment_order:
        fo_id    = fulfillment_order_group["fulfillmentOrderId"]
        fo_items = fulfillment_order_group["fulfillmentOrderLineItems"]

        # Partition items in this FO by their tracking
        tracking_to_items = defaultdict(list)

        for fi in fo_items:
            # Reverse-lookup: find which eligible lid this fulfillment line item belongs to
            # We need the original shopify_line_item_id — find it from the fulfillment_orders data
            matching_lid = None
            for foe in fulfillment_orders:
                for lie in foe["node"]["lineItems"]["edges"]:
                    if lie["node"]["id"] == fi["id"]:
                        matching_lid = lie["node"]["lineItem"]["id"]
                        break
                if matching_lid:
                    break

            tracking = eligible.get(matching_lid, {})
            tracking_key = (
                tracking.get("carrier") or "",
                tuple(tracking.get("tracking_numbers") or [])
            )
            tracking_to_items[tracking_key].append(fi)

        for tracking_key, items in tracking_to_items.items():
            tracking_groups[tracking_key].append({
                "fulfillmentOrderId":        fo_id,
                "fulfillmentOrderLineItems": items
            })

    # ── Step 8: Fire one fulfillment mutation per tracking group ──
    responses = []
    fulfilled = False

    for tracking_key, fo_groups in tracking_groups.items():
        carrier, tracking_numbers = tracking_key
        tracking_numbers = list(tracking_numbers)

        tracking_info = {"company": carrier or ""}
        if tracking_numbers:
            tracking_info["numbers"] = tracking_numbers
            if tracking_baseurl:
                tracking_info["url"] = f"{tracking_baseurl}{tracking_numbers[0]}"

        # one Shopify fulfillment order per API call
        for fo_group in fo_groups:
            variables = {
                "fulfillment": {
                    "lineItemsByFulfillmentOrder": [fo_group],
                    "notifyCustomer": True,
                    "trackingInfo": tracking_info,
                }
            }

            try:
                res = shopify.GraphQL().execute(
                    query=fulfillment_mutation,
                    operation_name="fulfillmentCreateV2",
                    variables=variables,
                )
                response = json.loads(res)
                responses.append(response)

                if is_fulfillment_successful(response):
                    fulfilled = True
                    frappe.log_error(
                        title=f"Fulfillment Success {sales_order}",
                        message=str(response)
                    )
                else:
                    frappe.log_error(
                        title=f"Fulfillment Failed {sales_order}",
                        message=str(response)
                    )

            except Exception:
                frappe.log_error(
                    title=f"Fulfillment Exception {sales_order}",
                    message=frappe.get_traceback()
                )

    shopify.ShopifyResource.clear_session()

    # ── Step 9: Mark DN as fulfilled if at least one fulfillment succeeded ──
    if fulfilled:
        # Mark ALL submitted DNs involved in this fulfillment
        all_involved_dns = set()

        for bundle in bundles.values():
            if bundle["bundle_fully_delivered"]:
                for dn_name in bundle["delivery_notes_involved"]:
                    all_involved_dns.add(dn_name)

        for item in standalones.values():
            if item["fully_delivered"]:
                for dn_entry in item["delivered_dns"]:
                    if dn_entry["docstatus"] == 1:
                        all_involved_dns.add(dn_entry["dn"])

        for dn_name in all_involved_dns:
            try:
                dn_doc = frappe.get_doc("Delivery Note", dn_name)
                dn_doc.custom_fulfilled_on_shopify = 1
                dn_doc.save()
            except Exception:
                frappe.log_error(
                    title=f"DN Mark Failed {dn_name}",
                    message=frappe.get_traceback()
                )

        frappe.db.commit()

    return responses[-1] if responses else {}

def map_lineitem_id_from_so(so_name:str) -> dict:
    shopify_line_item_map = {}  # {shopify_line_item_id: erpnext_item_code}
    sales_order_items = frappe.get_all(
        "Sales Order Item",
        filters={"parent": so_name},
        fields=["item_code", "custom_shopify_line_item_id"]
    )

    for item in sales_order_items:
        if item.custom_shopify_line_item_id:
            shopify_line_item_map[item.custom_shopify_line_item_id] = item.item_code
    return shopify_line_item_map 

def get_item_qty_from_extensiv(extensiv_json:json) -> dict:
    """
    Make this `{'SPT-NN6': 3.0, 'V2WOB': 2.0, 'WLBR': 3.0}` from extensiv line items API 

    Parameters:
    extensiv_json : The line item json from extensiv order lineitem api

    Returns:
    A dictionary 
    """
    data = extensiv_json['ResourceList']
    item_qty_dict = {}
    for item in data:
        item_qty_dict[item['ItemIdentifier']['Sku']] = item.get('Qty') 
    return item_qty_dict

def make_fulfillment_order_items(fulfillment_line_item:dict):
    """
    Create the dict for fuflfillment order line item 

    Parameters:
    fulfillment_line_item: 
    """
    fulfillment_list = list()
    for item in fulfillment_line_item:
        fulfillment_dict = {}
        fulfillment_dict['id'] = fulfillment_line_item[item]['id']
        fulfillment_dict['quantity'] = fulfillment_line_item[item]['quantity']
        fulfillment_list.append(fulfillment_dict)
    return fulfillment_list
 
def send_post_order_tag(shopify_order_id: str) -> dict:
    """
    Sends tag to shopify via GraphQL

    Parameters
    shopify_setting_doc(str): Setting of shopify that stores necessary shop info
    shopify_order_id(str): The shopify_order_id recieved while syncing orders

    returns
    response(dict): response from shopify
    """
    mutation_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(mutation_file_path, "r") as file:
        tags_add_mutation = file.read()

    # setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    so_marketplace = get_so_name_marketplace_by_shopify_id(shopify_order_id=shopify_order_id)

    if not so_marketplace:
        frappe.log_error(
            title="Marketplace Missing",
            message=f"No Sales Order found for Shopify Order ID: {shopify_order_id}"
        )
        return {}

    # Case 2: Sales Order found but marketplace is empty
    if not so_marketplace.marketplace:
        frappe.log_error(
            title="Marketplace Missing",
            message=f"SO {so_marketplace.name} has no marketplace set"
        )
        return {}

    setting_doc_name = get_shopify_setting_by_marketplace(so_marketplace.marketplace)
    setting_doc = frappe.get_doc("Shopify Integration Settings",setting_doc_name)

    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)

    shopify.ShopifyResource.activate_session(session)
    variables = {
        "id": shopify_order_id,
        "tags": "Synced to Extensiv",
    }
    res = shopify.GraphQL().execute(
        query=tags_add_mutation,
        operation_name="tagsAdd",
        variables=variables,
    )
    shopify.ShopifyResource.clear_session()
    response = json.loads(res)
    frappe.log_error(title="Tag sent to shopify", message=f"Response:{response}")
    return response


# Functions for webhook subscription create,update and delete
def get_webhook_subscriptions() -> None:
    """
    Gets a list of webhook subscription for order change between our erpnext instance and shopify

    Parameters:
    shopify_setting_doc(str): The name of the shopify setting doc

    Returns:
    None
    """

    query = """ query {
            webhookSubscriptions(first: 2) {
            edges {
            node {
                id
                topic
                    }
                }
            }
        }"""
    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    # Get necessary information from setting doc
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=query,
    )
    shopify.ShopifyResource.clear_session()
    frappe.log_error(title="Subscription Lists", message=subscription_response)


def order_update_webhook_subscription():
    """
    Creates a webhook subscription for order change between our erpnext instance and shopify

    Parameters:
    shopify_setting_doc(str): The name of the shopify setting doc

    Returns:
    None
    """
    subscription_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(subscription_file_path, "r") as file:
        webhook_subscription_mutation = file.read()

    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)
    webhook_endpoint = setting_doc.order_edit_webhook_endpoint
    variables = {
        "topic": "ORDERS_UPDATED",
        "webhookSubscription": {
            "callbackUrl": webhook_endpoint,
            "includeFields": [
                "admin_graphql_api_id",
                "name",
                "updated_at",
                "line_items",
                "customer",
                "tax_lines",
                "shipping_lines",
                "shipping_address",
                "billing_address",
                "refunds",
                "current_total_discounts" "tags" "discount_codes",
            ],
            "format": "JSON",
        },
    }
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=webhook_subscription_mutation,
        operation_name="webhookSubscriptionCreate",
        variables=variables,
    )
    shopify.ShopifyResource.clear_session()
    frappe.log_error(title="Webhook Subscription Create", message=subscription_response)


def order_create_webhook_subscription():
    """
    Creates a webhook subscription for order change between our erpnext instance and shopify

    Parameters:
    shopify_setting_doc(str): The name of the shopify setting doc

    Returns:
    None
    """
    subscription_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(subscription_file_path, "r") as file:
        webhook_subscription_mutation = file.read()

    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)
    webhook_endpoint = setting_doc.order_edit_webhook_endpoint
    variables = {
        "topic": "ORDERS_CREATE",
        "webhookSubscription": {
            "callbackUrl": webhook_endpoint,
            "format": "JSON",
        },
    }
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=webhook_subscription_mutation,
        operation_name="webhookSubscriptionCreate",
        variables=variables,
    )
    shopify.ShopifyResource.clear_session()
    frappe.log_error(title="Webhook Subscription Create", message=subscription_response)


def delete_subscription(subscription_id: str) -> None:
    """
    Deletes a webhook subscription

    Parameters:
    shopify_setting_doc (str): Name of the shopify setting_doc
    subscription_id (str) : Can be found in error logs ,search for subscriptions

    Returns :
    None
    """
    subscription_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(subscription_file_path, "r") as file:
        webhook_subscription_mutation = file.read()

    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)
    variables = {"id": subscription_id}
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=webhook_subscription_mutation,
        operation_name="webhookSubscriptionDelete",
        variables=variables,
    )
    shopify.ShopifyResource.clear_session()
    frappe.log_error(title="Webhook Subscription Delete", message=subscription_response)


def update_subscription(subscription_id: str) -> None:
    """
    Updates a webhook subscription

    Parameters:
    shopify_setting_doc (str): Name of the shopify setting_doc
    subscription_id (str) : Can be found in error logs ,search for subscriptions

    Returns :
    None
    """
    subscription_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(subscription_file_path, "r") as file:
        webhook_subscription_mutation = file.read()

    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)
    webhook_endpoint = setting_doc.order_edit_webhook_endpoint
    variables = {
        "id": subscription_id,
        "webhookSubscription": {
            "callbackUrl": webhook_endpoint,
            "format": "JSON",
            "includeFields": [
                "admin_graphql_api_id",
                "name",
                "updated_at",
                "line_items",
                "customer",
                "tax_lines",
                "shipping_lines",
                "shipping_address",
                "billing_address",
                "refunds",
                "financial_status",
                "current_total_discounts",
                "tags",
                "discount_codes",
            ],
        },
    }
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=webhook_subscription_mutation,
        operation_name="webhookSubscriptionUpdate",
        variables=variables,
    )
    shopify.ShopifyResource.clear_session()
    frappe.log_error(title="Webhook Subscription update", message=subscription_response)


def verify_webhook(webhook_data: dict, hmac_header: str, client_secret: str) -> bool:
    """
    Authenticates the webhook payload using Shopify Secret. Allow only if its sent by our shopify store

    Parameters:
    webhook_data (dict):Incoming payload from the webhook
    hmac_header (str): hmac from the header of the shopify payload
    client_secret(str): API secret from the Shopify settings doc

    Returns :
    True :if authenticated
    False : if cannot be authenticated

    """
    digest = hmac.new(
        client_secret.encode("utf-8"), webhook_data, digestmod=hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest)
    return hmac.compare_digest(computed_hmac, hmac_header.encode("utf-8"))


def getDiscountCode(discountCodes: list) -> str:
    """ """
    discount_code = ""
    n = len(discountCodes) - 1
    for i, code in enumerate(discountCodes):
        current_code = code["code"]
        if i != n:
            current_code += ","
        discount_code += current_code
    return discount_code


# Util functions for order update
def get_updated_shopify_customer(
    customer_data: dict, setting_doc: str
) -> "customer_name":
    """
    Gets the customer based on customer name for the order edit payload. This payload is in different format than ordersync so different function

    Params:
        customer_data: Customer dictionary from Shopify Orders
        setting_doc: Shopify Integration Settings Doc name

    Returns:
        customer_name = Name of the Customer

    """
    displayName = customer_data["first_name"] + " " + customer_data["last_name"]
    if frappe.db.exists("Customer", {"customer_name": displayName}):
        customer_name = frappe.get_value(
            "Customer", {"customer_name": displayName}, "name"
        )
    else:

        customer_doc = frappe.new_doc("Customer")
        customer_doc.customer_name = displayName
        customer_doc.customer_type = frappe.get_value(
            "Shopify Integration Settings", setting_doc, "default_customer_type"
        )
        customer_doc.save()
        frappe.db.commit()
        # customer_doc.customer_primary_address = get_shopify_address(data["addresses"][0],customer_doc.customer_name)
        customer_doc.customer_primary_contact = get_updated_shopify_contact(
            customer_data
        )
        customer_doc.save()
        frappe.db.commit()
        customer_name = customer_doc.name
    return customer_name


def create_updated_shopify_so_item_row(data: dict, setting_doc: str) -> "item_row":
    """
    This function creates Item row for the updated sales order. There are syntactical changes in shopify order edit payload.

    Params:
        data(Dict) = Line Item data from Shopify Orders API
        setting_doc = Shopify Integration Settings Doc name

    Return:
        row(Dict) = returns item row with filed name as key and it's value
    """
    row = {}
    row["item_code"] = get_shopify_item_code(data["sku"], data["name"], setting_doc)
    row["qty"] = data["current_quantity"]
    row["rate"] = data["price_set"]["shop_money"]["amount"]
    row["custom_sku"] = data["sku"]
    return row


def getTotalDiscount(lineitems_data: list) -> float:
    """ """
    total_discount = 0
    for item in lineitems_data:
        discount_allocation = item["discount_allocations"]
        for discount in discount_allocation:
            total_discount += float(discount["amount_set"]["shop_money"]["amount"])
    return total_discount


def get_updated_shopify_contact(contact_data: dict) -> "contact_name":
    """
    This function creates Contact doc if given email is not present in ERP

    Params:
        data(dict) = Customer data from Shopify Orders API

    Return:
        contact = Name of the contact doc
    """
    displayName = contact_data["first_name"] + " " + contact_data["last_name"]
    if frappe.db.exists("Contact Email", {"email_id": contact_data["email"]}):
        contact = frappe.get_value(
            "Contact Email", {"email_id": contact_data["email"]}, "parent"
        )
    else:
        contact_doc = frappe.new_doc("Contact")
        contact_doc.first_name = displayName
        email_row = {}
        email_row["email_id"] = contact_data["email"]
        email_row["is_primary"] = 1
        contact_doc.append("email_ids", email_row)
        if contact_data["phone"] is not None:
            num_row = {}
            num_row["phone"] = contact_data["phone"]
            num_row["is_primary_phone"] = 1
            contact_doc.append("phone_nos", num_row)
        link_row = get_link_row("Customer", displayName)
        contact_doc.append("links", link_row)
        contact_doc.save()
        frappe.db.commit()
        contact = contact_doc.name
    return contact


def remove_zero_quantity_items(line_item_data: list) -> list:
    """
    Remove Items that have 0 in current_quantity field in the line_items list from order edit payload

    Parameters:
    line_item_data (list) : A list from the payload that contains all the items that were and are in the edited sales order

    Retuns :
    filtered_line_item_data (list) : Filtered list of line_items

    """
    filtered_line_items = []
    for item in line_item_data:
        if item["current_quantity"] == 0:
            continue
        else:
            filtered_line_items.append(item)
    return filtered_line_items


def get_updated_shopify_contact(contact_data: dict) -> "contact_name":
    """
    This function creates Contact doc if given email is not present in ERP

    Params:
        data(dict) = Customer data from Shopify Orders API

    Return:
        contact = Name of the contact doc
    """
    displayName = contact_data["first_name"] + " " + contact_data["last_name"]
    if frappe.db.exists("Contact Email", {"email_id": contact_data["email"]}):
        contact = frappe.get_value(
            "Contact Email", {"email_id": contact_data["email"]}, "parent"
        )
    else:
        contact_doc = frappe.new_doc("Contact")
        contact_doc.first_name = displayName
        email_row = {}
        email_row["email_id"] = contact_data["email"]
        email_row["is_primary"] = 1
        contact_doc.append("email_ids", email_row)
        if contact_data["phone"] is not None:
            num_row = {}
            num_row["phone"] = contact_data["phone"]
            num_row["is_primary_phone"] = 1
            contact_doc.append("phone_nos", num_row)
        link_row = get_link_row("Customer", displayName)
        contact_doc.append("links", link_row)
        contact_doc.save()
        frappe.db.commit()
        contact = contact_doc.name
    return contact


def test_get_order(start_date) -> None:
    """
    Deletes a webhook subscription

    Parameters:
    shopify_setting_doc (str): Name of the shopify setting_doc
    subscription_id (str) : Can be found in error logs ,search for subscriptions

    Returns :
    None
    """
    subscription_file_path = os.path.join(current_dir, "selling_query.graphql")
    with open(subscription_file_path, "r") as file:
        webhook_subscription_mutation = file.read()

    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    start_date = f"(created_at:>{start_date}) AND "
    non_cancelled = "(-status:CANCELLED)"
    order_query = start_date + non_cancelled

    session = shopify.Session(shop_url, api_version, access_token)
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=webhook_subscription_mutation,
        variables={"nos": 250, "order_query": order_query},
        operation_name="GetOrdersTestInfo",
    )
    shopify.ShopifyResource.clear_session()
    response = json.loads(subscription_response)
    frappe.log_error(title="Webhook Subscription Delete", message=response)


def fulfillment_subscription() -> None:
    """
    Creates a webhook subscription for order fulfillment 

    Parameters:
    shopify_setting_doc(str): The name of the shopify setting doc

    Returns:
    None
    """
    subscription_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(subscription_file_path, "r") as file:
        webhook_subscription_mutation = file.read()

    setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    session = shopify.Session(shop_url, api_version, access_token)
    webhook_endpoint = setting_doc.order_fulfillment_webhook_endpoint
    variables = {
        "topic": "FULFILLMENT_ORDERS_SPLIT",
        "webhookSubscription": {
            "callbackUrl": webhook_endpoint,
            "format": "JSON",
        },
    }
    shopify.ShopifyResource.activate_session(session)
    subscription_response = shopify.GraphQL().execute(
        query=webhook_subscription_mutation,
        operation_name="webhookSubscriptionCreate",
        variables=variables,
    )
    shopify.ShopifyResource.clear_session()
    frappe.log_error(
        title="Webhook Subscription Create",
        message=f"Response:\n{subscription_response}\n\nURL:{webhook_endpoint}",
    )


# def getFulfillment(shopify_order_id):
#     query_file_path = os.path.join(current_dir, "selling_query.graphql")
#     with open(query_file_path, "r") as file:
#         fulfillment_id_query = file.read()

#     setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
#     shop_name = setting_doc.shop_name
#     shop_url = f"https://@{shop_name}.myshopify.com"
#     api_version = setting_doc.api_version
#     access_token = setting_doc.get_password("access_token")
#     session = shopify.Session(shop_url, api_version, access_token)
#     # get fulfillment Id, needed to create a fulfillment
#     shopify.ShopifyResource.activate_session(session)
#     fulfillmentId_response = shopify.GraphQL().execute(
#         query=fulfillment_id_query,
#         operation_name="getOrderFulfillment",
#         variables={"orderId": shopify_order_id},
#     )
#     fulfillment_id_json = json.loads(fulfillmentId_response)
#     return fulfillment_id_json


def convert_country_code_to_name(country_code):
    try:
        country = pycountry.countries.get(alpha_2=country_code.upper())
        if country:
            return country.name
    except KeyError:
        return None


def make_customers_from_master(customer_data: dict):
    for data in customer_data:
        if not frappe.db.exists("Customer", {"custom_customer_id": data.get("ID")}):
            new_customer = frappe.new_doc("Customer")
            new_customer.custom_customer_id = data["ID"]
            new_customer.customer_name = data["Name"]
            new_customer.customer_type = "Company"
            new_customer.payment_terms = data["Location: Checkout Payment Terms"]
            new_customer.save()
            frappe.db.commit()


def make_customer_address_from_master(customer_data: dict):
    for data in customer_data:
        address_1_billing = data.get("Location: Billing Address 1")
        if address_1_billing:
            if not frappe.db.exists("Address", {"address_line1": address_1_billing}):
                address_doc = frappe.new_doc("Address")
                address_doc.address_title = data["Name"]
                address_doc.address_line1 = data["Location: Billing Address 1"]
                address_doc.address_line2 = data["Location: Billing Address 2"]
                address_doc.city = data["Location: Billing City"]
                country_name = convert_country_code_to_name(
                    data["Location: Billing Country Code"]
                )
                address_doc.country = country_name
                address_doc.state = convert_code_to_name(
                    data.get("Location: Billing Province Code")
                )
                address_doc.pincode = data.get("Location: Billing Zip")
                link_row = get_link_row("Customer", data["Name"])
                address_doc.append("links", link_row)
                address_doc.is_primary_address = 1
                try:
                    address_doc.save()
                    frappe.db.commit()
                except:
                    frappe.log_error(
                        title="Address save error",
                        message=f"Traceback:\n\n{frappe.get_traceback()}",
                    )
            else:
                address_doc = frappe.get_doc(
                    "Address", {"address_line1": address_1_billing}
                )
                link_row = get_link_row("Customer", data["Name"])
                address_doc.append("links", link_row)
                address_doc.is_primary_address = 1
                try:
                    address_doc.save()
                    frappe.db.commit()
                except:
                    frappe.log_error(
                        title="Address save error",
                        message=f"Traceback:\n\n{frappe.get_traceback()}",
                    )

            address_1_shipping = data.get("Location: Shipping Address 1")
            if address_1_shipping:
                if not frappe.db.exists(
                    "Address", {"address_line1": address_1_shipping}
                ):
                    address_doc = frappe.new_doc("Address")
                    address_doc.address_title = data["Name"]
                    address_doc.address_line1 = data["Location: Shipping Address 1"]
                    address_doc.address_line2 = data["Location: Shipping Address 2"]
                    address_doc.city = data["Location: Shipping City"]
                    country_name = convert_country_code_to_name(
                        data["Location: Shipping Country Code"]
                    )
                    address_doc.country = country_name
                    address_doc.state = convert_code_to_name(
                        data.get("Location: Shipping Province Code")
                    )
                    address_doc.pincode = data["Location: Shipping Zip"]
                    link_row = get_link_row("Customer", data["Name"])
                    address_doc.append("links", link_row)
                    address_doc.is_shipping_address = 1
                    try:
                        address_doc.save()
                        frappe.db.commit()
                    except:
                        frappe.log_error(
                            title="Address save error",
                            message=f"Traceback:\n\n{frappe.get_traceback()}",
                        )
                else:
                    address_doc = frappe.get_doc(
                        "Address", {"address_line1": address_1_shipping}
                    )
                    link_row = get_link_row("Customer", data["Name"])
                    address_doc.append("links", link_row)
                    address_doc.is_shipping_address = 1
                    try:
                        address_doc.save()
                        frappe.db.commit()
                    except:
                        frappe.log_error(
                            title="Address save error",
                            message=f"Traceback:\n\n{frappe.get_traceback()}",
                        )


def make_contact_from_master(contact_data):
    for data in contact_data:
        customer_email = data.get("Customer: Email")
        primary_email = data.get("Main Contact: Customer Email")
        first_name = data.get("Customer: First Name")
        last_name = data.get("Customer: Last Name")
        ship_phone_number_raw = data.get("Location: Shipping Phone")
        bill_phone_number_raw = data.get("Location: Billing Phone")
        ship_phone_number = (
            ship_phone_number_raw.split("'")[1] if ship_phone_number_raw else None
        )
        bill_phone_number = (
            bill_phone_number_raw.split("'")[1] if bill_phone_number_raw else None
        )

        contact_doc = frappe.new_doc("Contact")
        if first_name and last_name:
            if not frappe.db.exists(
                "Contact", {"first_name": first_name, "last_name": last_name}
            ):
                contact_doc.first_name = first_name or data["Name"]
                contact_doc.last_name = last_name
            else:
                contact_doc = frappe.get_doc(
                    "Contact", {"first_name": first_name, "last_name": last_name}
                )
        # Check if both numbers are the same and that they are none and also check if they are already set
        if (ship_phone_number == bill_phone_number or ship_phone_number) and (
            ship_phone_number and bill_phone_number
        ):
            already_set = False
            phone_numbers = contact_doc.phone_nos
            for number in phone_numbers:
                if number in [bill_phone_number, ship_phone_number]:
                    already_set = True
            if not already_set:
                num_row = {}
                num_row["phone"] = ship_phone_number
                num_row["is_primary_phone"] = 1
                contact_doc.append("phone_nos", num_row)
        elif bill_phone_number:
            if not already_set:
                num_row = {}
                num_row["phone"] = bill_phone_number
                num_row["is_primary_phone"] = 1
                contact_doc.append("phone_nos", num_row)
        if customer_email and not (
            (frappe.db.exists("Contact Email", {"email_id": customer_email}))
        ):
            email_row = {}
            email_row["email_id"] = customer_email
            if (customer_email == primary_email) or (primary_email == None):
                email_row["is_primary"] = 1
            if not customer_email and primary_email:
                customer_doc = frappe.get_doc(
                    "Customer", {"custom_customer_id": data.get("ID")}
                )
                customer_doc.email_id = primary_email
                customer_doc.save()
                frappe.db.commit()
            contact_doc.append("email_ids", email_row)
        link_row = get_link_row("Customer", data["Name"])
        contact_doc.append("links", link_row)
        try:
            contact_doc.save()
            frappe.db.commit()
        except:
            frappe.log_error(
                title="Contact Save Error", message=f"{frappe.get_traceback()}"
            )


def convert_code_to_name(abv):
    us_state_to_abbrev = {
        "AL": "Alabama",
        "AK": "Alaska",
        "AS": "American Samoa",
        "AZ": "Arizona",
        "AR": "Arkansas",
        "CA": "California",
        "CO": "Colorado",
        "CT": "Connecticut",
        "DE": "Delaware",
        "DC": "District Of Columbia",
        "FM": "Federated States Of Micronesia",
        "FL": "Florida",
        "GA": "Georgia",
        "GU": "Guam",
        "HI": "Hawaii",
        "ID": "Idaho",
        "IL": "Illinois",
        "IN": "Indiana",
        "IA": "Iowa",
        "KS": "Kansas",
        "KY": "Kentucky",
        "LA": "Louisiana",
        "ME": "Maine",
        "MH": "Marshall Islands",
        "MD": "Maryland",
        "MA": "Massachusetts",
        "MI": "Michigan",
        "MN": "Minnesota",
        "MS": "Mississippi",
        "MO": "Missouri",
        "MT": "Montana",
        "NE": "Nebraska",
        "NV": "Nevada",
        "NH": "New Hampshire",
        "NJ": "New Jersey",
        "NM": "New Mexico",
        "NY": "New York",
        "NC": "North Carolina",
        "ND": "North Dakota",
        "MP": "Northern Mariana Islands",
        "OH": "Ohio",
        "OK": "Oklahoma",
        "OR": "Oregon",
        "PW": "Palau",
        "PA": "Pennsylvania",
        "PR": "Puerto Rico",
        "RI": "Rhode Island",
        "SC": "South Carolina",
        "SD": "South Dakota",
        "TN": "Tennessee",
        "TX": "Texas",
        "UT": "Utah",
        "VT": "Vermont",
        "VI": "Virgin Islands",
        "VA": "Virginia",
        "WA": "Washington",
        "WV": "West Virginia",
        "WI": "Wisconsin",
        "WY": "Wyoming",
    }

    # invert the dictionary
    return us_state_to_abbrev.get(abv)


def fulfill_entire_order(shopify_order_id: str, tracking_number: str, carrier: str, dn_created:str, tracking_baseurl:str = None):
    """
    TEMPORARY FUNCTION: Fulfills all items in an order without matching lineitem.id.
    This is needed for old orders that don't have lineitem.id in ERPNext.

    Parameters:
    shopify_order_id (str): The Shopify Order ID
    tracking_number (str): Tracking number from Extensiv
    carrier (str): Carrier name from Extensiv

    Returns:
    dict: Shopify fulfillment response
    """
    # setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
    so_marketplace = get_so_name_marketplace_by_shopify_id(shopify_order_id=shopify_order_id)

    if not so_marketplace:
        frappe.log_error(
            title="Marketplace Missing",
            message=f"No Sales Order found for Shopify Order ID: {shopify_order_id}"
        )
        return {}

    if isinstance(tracking_number, list):
        tracking_number = tracking_number[0]

    # Case 2: Sales Order found but marketplace is empty
    if not so_marketplace.marketplace:
        frappe.log_error(
            title="Marketplace Missing",
            message=f"SO {so_marketplace.name} has no marketplace set"
        )
        return {}

    setting_doc_name = get_shopify_setting_by_marketplace(so_marketplace.marketplace)
    setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)

    shop_name = setting_doc.shop_name
    shop_url = f"https://@{shop_name}.myshopify.com"
    api_version = setting_doc.api_version
    access_token = setting_doc.get_password("access_token")
    # dn = frappe.get_doc("Delivery Note",dn_created)
    session = shopify.Session(shop_url, api_version, access_token)
    shopify.ShopifyResource.activate_session(session)

    # Fetch all fulfillment orders
    mutation_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(mutation_file_path, "r") as file:
        fulfillment_mutation = file.read()
    query_file_path = os.path.join(current_dir, "selling_query.graphql")
    with open(query_file_path, "r") as file:
        fulfillment_id_query = file.read()
    fulfillment_response = shopify.GraphQL().execute(
        query=fulfillment_id_query,
        operation_name="getOrderFulfillment",
        variables={"orderId": shopify_order_id},
    )
    fulfillment_data = json.loads(fulfillment_response)

    if "data" not in fulfillment_data or not fulfillment_data["data"]["order"]["fulfillmentOrders"]["edges"]:
        frappe.log_error(
            title="Fulfillment Error",
            message=f"Could not retrieve fulfillment orders for Order: {shopify_order_id}",
        )
        return {"error": "No fulfillment orders found"}

    fulfillment_orders = fulfillment_data["data"]["order"]["fulfillmentOrders"]["edges"]
    fulfillment_items = []

    for fulfillment_order in fulfillment_orders:
        fulfillment_id = fulfillment_order["node"]["id"]
        line_items = fulfillment_order["node"]["lineItems"]["edges"]

        for item in line_items:
            item_title = item["node"]["lineItem"]["title"]
            if item_title == "V2Pro Upgrade Kit":
                if not frappe.db.exists("Delivery Note Item",{"parent":dn_created,"item_code":"V2PUGK"}):
                    continue
            fulfillment_items.append(
                {
                    "id": item["node"]["id"],  
                    "quantity": item["node"]["lineItem"]["quantity"], 
                }
            )

        # Send fulfillment request for the order
        mutation_file_path = os.path.join(current_dir, "mutation.graphql")

        tracking_info = {"company": carrier, "number": tracking_number, "url":f"{tracking_baseurl}{tracking_number}"}

        variables = {
            "fulfillment": {
                "lineItemsByFulfillmentOrder": {
                    "fulfillmentOrderId": fulfillment_id,
                    "fulfillmentOrderLineItems": fulfillment_items,
                },
                "notifyCustomer": True,
                "trackingInfo": tracking_info,
            }
        }
        frappe.log_error(title = "Fulfillment Variables",message = variables)
        try:
            response = shopify.GraphQL().execute(
                query=fulfillment_mutation,
                operation_name="fulfillmentCreateV2",
                variables=variables,
            )
            return json.loads(response)
        except Exception as e:
            frappe.log_error(
                title="Shopify Fulfillment Error",
                message=f"Error fulfilling order {shopify_order_id}: {str(e)}",
            )
            return {"error": str(e)}

    shopify.ShopifyResource.clear_session()

def fulfill_order_by_dn(dn_name:str) -> None:
    dn = frappe.get_doc("Delivery Note",dn_name)
    tracking_number = dn.custom_tracking_number
    carrier = dn.custom_carrier
    so = dn.items[0].against_sales_order 
    sales_order = frappe.get_doc("Sales Order",so)
    shopify_order_id = frappe.get_value("Sales Order",so,'custom_shopify_order_id_number')
    line_item_id = sales_order.items[0].custom_shopify_line_item_id if sales_order.items else None
    if line_item_id:
        graphql_response = send_tracking_info_updated(shopify_order_id,tracking_number,carrier,dn.name,so)
        if is_fulfillment_successful(graphql_response):
            frappe.db.set_value("Delivery Note",dn_name,"custom_fulfilled_on_shopify",1)
            frappe.db.commit()
        frappe.log_error(
            title="GraphQL tracking number response",
            message=f"Shopify Line Item Id\n\n\Order Number:{so}\nResponse : {graphql_response}",
        )
    else:
        graphql_response = fulfill_entire_order(shopify_order_id,tracking_number,carrier,dn_name)
        if is_fulfillment_successful(graphql_response):
            frappe.db.set_value("Delivery Note",dn_name,"custom_fulfilled_on_shopify",1)
            frappe.db.commit()
        frappe.log_error(
            title="GraphQL tracking number response",
            message=f"Fulfilled Entire Order\n\nOrder Number:{so}\nResponse : {graphql_response}",
        )
                    

def is_fulfillment_successful(response: dict) -> bool:
    try:
        return response['data']['fulfillmentCreateV2']['fulfillment']['status'] == 'SUCCESS'
    except (KeyError, TypeError):
        return False

def fulfill_order_by_dn_list():
    dn_list = frappe.get_list("Delivery Note",filters={"marketplace":"Shopify","custom_fulfilled_on_shopify":0,"creation": [">", "2025-03-18"]},fields=["name"])
    for dn in dn_list:
        fulfill_order_by_dn(dn['name'])

def get_warehouse_location_ID(warehouse: str):
    """
    The function return location id for fullfillment the order.
    Args:
        warehouse: str
    Return:
        Location ID: str
    """
    extensive_setting_doc = frappe.get_doc("Extensiv Settings", {"enabled": 1})
    warehouse_location_id = None
    for item in extensive_setting_doc.extensiv_facility_settings:
        if item.warehouse.strip().lower() == warehouse.strip().lower():
            warehouse_location_id =  item.location_id
    return warehouse_location_id

def get_warehouse_shopify_name(warehouse: str):
    """
    The function return location id for fullfillment the order.
    Args:
        warehouse: str
    Return:
        Location ID: str
    """
    extensive_setting_doc = frappe.get_doc("Extensiv Settings", {"enabled": 1})
    warehouse_name = None
    for item in extensive_setting_doc.extensiv_facility_settings:
        if item.warehouse == warehouse:
            warehouse_name =  item.warehouse
    return warehouse_name

        
        
@frappe.whitelist()    
def allocate_fullfillment_order(doc, method):
    try:
        mutation_file_path = os.path.join(current_dir, "mutation.graphql")
        with open(mutation_file_path, "r") as file:
            allocate_fullfillment_mutation = file.read()
        # print("Loaded mutation.graphql")

        query_file_path = os.path.join(current_dir, "selling_query.graphql")
        with open(query_file_path, "r") as file:
            fulfillment_id_query = file.read()
        # print("Loaded selling_query.graphql")

        # setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
        # print("Fetched Shopify Integration Settings")
        
        # so = frappe.get_doc("Sales Order", doc)
        so = doc
        if not so.marketplace:
            frappe.log_error(
                title="No Marketplace Found",
                message=f"No marketplace found while allocating fulfillment order on SO: {so.name}"
            )
            return
        
        setting_doc = frappe.get_doc("Shopify Integration Settings", get_shopify_setting_by_marketplace(so.marketplace))
        print(f"Processing Sales Order: {so.name}, Shopify Order ID: {so.custom_shopify_order_id_number}")

        shop_url = f"https://{setting_doc.shop_name}.myshopify.com"
        session = shopify.Session(shop_url, setting_doc.api_version, setting_doc.get_password("access_token"))
        shopify.ShopifyResource.activate_session(session)
        # print("Activated Shopify session")

        # Step 1: Fetch fulfillment orders for the sales order
        print("Fetching fulfillment orders for Shopify Order ID...")
        fulfillmentId_response = shopify.GraphQL().execute(
            query=fulfillment_id_query,
            operation_name="getOrderFulfillment",
            variables={"orderId": so.custom_shopify_order_id_number},
        )
        fulfillment_data = json.loads(fulfillmentId_response)
        # print("Fulfillment Orders Fetched:", json.dumps(fulfillment_data, indent=2))
        
        # order_data = fulfillment_data.get("data", {}).get("order")
        # if not order_data:
        #     frappe.log_error(
        #         title="Shopify Fulfillment Fetch Error",
        #         message=f"Order not found in Shopify. Shopify Order ID: {so.custom_shopify_order_id_number}\n\nResponse: {json.dumps(fulfillment_data, indent=2)}"
        #     )
        #     print(f"Order not found in Shopify for ID {so.custom_shopify_order_id_number}")
        #     return  # exit gracefully

        fulfillment_orders = fulfillment_data["data"]["order"]["fulfillmentOrders"]["edges"]

        for fulfillment in fulfillment_orders:
            fulfillment_order_id = fulfillment["node"]["id"]
            current_order_location = fulfillment["node"].get("assignedLocation", {}).get("location", {}).get("name", "Unknown")
            current_order_location_id = fulfillment["node"].get("assignedLocation", {}).get("location", {}).get("id", None)
            print(f"\nFulfillment Order ID: {fulfillment_order_id}")
            print(f"Current Location (Old): {current_order_location}")

            line_items = fulfillment["node"]["lineItems"]["edges"]

            for so_item in so.items:
                shopify_line_item_id = so_item.custom_shopify_line_item_id
                # print(f"\nProcessing Item: {so_item.item_name} | Shopify Line Item ID: {shopify_line_item_id}")

                # Find the matching fulfillment line item
                for li in line_items:
                    node = li["node"]
                    if node["lineItem"]["id"] == shopify_line_item_id:
                        fulfillment_line_item_id = node["id"]
                        # print(f"Matched Fulfillment Line Item ID: {fulfillment_line_item_id}")

                        # Step 2: Get new location ID from ERPNext warehouse
                        new_location_id = get_warehouse_location_ID(so_item.warehouse)
                        if new_location_id:
                            new_location_name = get_warehouse_shopify_name(so_item.warehouse)  # You must define this
                            print(f"Expected Location from Warehouse '{so_item.warehouse}': {new_location_name} (ID: {new_location_id})")
                            formatted_location_id = f"gid://shopify/Location/{new_location_id}"
                            # Compare with current location
                            if formatted_location_id == current_order_location_id:
                                print("Skipping move: Current Shopify location does not match expected warehouse location.")
                                continue

                            # Format the Shopify Location ID
                            print(f"Moving from Old Location: {current_order_location}")
                            print(f"To New Location: {new_location_name} (ID: {formatted_location_id})")

                            # Step 3: Move the fulfillment order to the new location
                            variables = {
                                "id": fulfillment_order_id,
                                "newLocationId": formatted_location_id,
                                "fulfillmentOrderLineItems": [
                                    {
                                        "id": fulfillment_line_item_id,
                                        "quantity": int(so_item.qty)
                                    }
                                ]
                            }


                            frappe.log_error(title = "Mutation Variables Location Update",message = f"Item:{so_item.item_code}\n{variables}")

                            response = shopify.GraphQL().execute(
                                query=allocate_fullfillment_mutation,
                                operation_name="fulfillmentOrderMove",
                                variables=variables
                            )
                            data = json.loads(response)
                            print("Mutation Response:", json.dumps(data, indent=2))

                            if data["data"]["fulfillmentOrderMove"]["userErrors"]:
                                error_msg = str(data["data"]["fulfillmentOrderMove"]["userErrors"])
                                frappe.log_error(
                                    title="Shopify Fulfillment Move Error",
                                    message=error_msg
                                )
                                print("Error moving fulfillment:", error_msg)
                            else:
                                success_msg = f"Moved fulfillment for item {so_item.item_name} from {current_order_location} to {new_location_name}."
                                print(success_msg)
                                frappe.msgprint(success_msg)
    except Exception as e:
        error_message = f"{str(e)}"
        frappe.log_error(
            title="Allocating Fulfillment Order Error",
            message=error_message
        )
        print("Exception occurred:", error_message)


# def updated_tracking_info(
#     shopify_order_id: str, tracking_number: List[str] | str, carrier: str, dn_created:str ,sales_order:str
# ) -> dict:
#     """
#     Sends tracking number and carrier info to shopify via GraphQL. Takes the lineitems from the 
#     extensiv order and fulfill items only in the order. 

#     Parameters
#     shopify_order_id (str): Order Id recieved from shopify sales order
#     tracking_number (str) | List[str]: Tracking number recieved from extensiv
#     carrier (str): Carrier name recieved from extensiv
#     shopify_setting_doc(str): Setting of shopify that stores necessary shop info

#     returns
#     response(dict): response from shopify
#     """
#     mutation_file_path = os.path.join(current_dir, "mutation.graphql")
#     with open(mutation_file_path, "r") as file:
#         fulfillment_mutation = file.read()
#     query_file_path = os.path.join(current_dir, "selling_query.graphql")
#     with open(query_file_path, "r") as file:
#         fulfillment_id_query = file.read()
#     dn = frappe.get_doc("Delivery Note",dn_created)
#     # setting_doc = frappe.get_doc("Shopify Integration Settings", {"enabled": 1})
#     # getting setting_doc based on marketplace
#     marketplace = frappe.db.get_value("Sales Order", sales_order, "marketplace")

#     if not marketplace:
#         frappe.log_error(
#             title="Marketplace Missing",
#             message=f"SO {sales_order} has no marketplace set"
#         )
#         return {}


#     setting_doc_name_by_marketplace = get_shopify_setting_by_marketplace(marketplace)

#     if not setting_doc_name_by_marketplace:
#         frappe.log_error(
#             title="Missing Shopify Settings",
#             message=f"No Shopify Integration Settings found for marketplace '{marketplace}'"
#         )
#         return {}

#     setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name_by_marketplace)

#     shop_name = setting_doc.shop_name
#     shop_url = f"https://@{shop_name}.myshopify.com"
#     api_version = setting_doc.api_version
#     access_token = setting_doc.get_password("access_token")
#     session = shopify.Session(shop_url, api_version, access_token)
#     # get fulfillment Id, needed to create a fulfillment
#     shopify.ShopifyResource.activate_session(session)
#     fulfillmentId_response = shopify.GraphQL().execute(
#         query=fulfillment_id_query,
#         operation_name="getOrderFulfillment",
#         variables={"orderId": shopify_order_id},
#     )
#     fulfillment_id_json = json.loads(fulfillmentId_response)
#     frappe.log_error(
#         title="Fulfillment Order ID",
#         message=fulfillment_id_json
#     )

#     if "data" in fulfillment_id_json.keys():
#         fulfillment_query = fulfillment_id_json["data"]["order"]["fulfillmentOrders"]['edges']
#         line_item_fulfillment = {}
#         # make a dictionary {item_name:qty} 
#         # extensiv_line_item = get_item_qty_from_extensiv(extensiv_order_line_item)
#         # Get the items from the Delivery Note
#         dn_items = frappe.get_all(
#             "Delivery Note Item",
#             filters={"parent": dn_created},
#             fields=["item_code", "qty"]
#         )
#         shopify_line_item_map  = map_lineitem_id_from_so(sales_order)
#         # Create a dictionary {erpnext_item_code: qty}
#         dn_item_map = {item.item_code: int(item.qty) for item in dn_items}
#         frappe.log_error(title = "Extensiv Line Item in Fulfillment",message = f'ERPNExt Items:{dn_item_map}')
#         frappe.log_error(title = "Fulfillment Query",message = fulfillment_query)
#         # line_item_fulfillment = {}
#         # for fulfillment_order in fulfillment_query:
#         #     fulfillment_order_id = fulfillment_order['node']['id']
#         #     for line_item in fulfillment_order['node']['lineItems']['edges']:
#         #         shopify_fulfillment_line_item_id = line_item['node']['id']  
#         #         shopify_line_item_id = line_item['node']['lineItem']['id'] 
#         #         shopify_item_title = line_item['node']['lineItem']['title']
#         #         # Find corresponding ERPNext item code
#         #         erpnext_item_code = shopify_line_item_map.get(shopify_line_item_id)
#         #         if erpnext_item_code and erpnext_item_code in dn_item_map:
#         #             qty = dn_item_map[erpnext_item_code]
#         #             line_item_fulfillment[shopify_fulfillment_line_item_id] = {
#         #                 'id': shopify_fulfillment_line_item_id,  
#         #                 'quantity': int(qty)
#         #             }
#         #         else:
#         #             frappe.log_error(
#         #                 title="Item Not Found in DN",
#         #                 message=f"ERPNext Item {erpnext_item_code} (Shopify Line Item ID: {shopify_line_item_id}) "
#         #                         f"not found in Delivery Note {dn_created}"
#         #             )
        
#         # Match fulfillment items to their fulfillment groups
#         line_items_by_fulfillment_order = []

#         for fulfillment_order in fulfillment_query:
#             fulfillment_order_id = fulfillment_order['node']['id']
#             fulfillment_items = []

#             for line_item in fulfillment_order['node']['lineItems']['edges']:
#                 shopify_fulfillment_line_item_id = line_item['node']['id']
#                 shopify_line_item_id = line_item['node']['lineItem']['id']
#                 erpnext_item_code = shopify_line_item_map.get(shopify_line_item_id)

#                 if erpnext_item_code and erpnext_item_code in dn_item_map:
#                     qty = dn_item_map[erpnext_item_code]
#                     fulfillment_items.append({
#                         'id': shopify_fulfillment_line_item_id,
#                         'quantity': int(qty)
#                     })

#             if fulfillment_items:
#                 line_items_by_fulfillment_order.append({
#                     "fulfillmentOrderId": fulfillment_order_id,
#                     "fulfillmentOrderLineItems": fulfillment_items
#                 })

        
#         # Fulfill the order using fulfillment id ,tracking_number ,carrier
#         variables = {
#                         "fulfillment": {
#                             "lineItemsByFulfillmentOrder": line_items_by_fulfillment_order,
#                             "trackingInfo": {
#                                 "company": carrier,
#                                 "numbers": tracking_number
#                             }
#                         }
#                     }

#                             # "notifyCustomer": True,

#         # fulfillment_order_lineitems = make_fulfillment_order_items(line_item_fulfillment)
# #         variables = {
# #             "fulfillment": {
# #                 "lineItemsByFulfillmentOrder": {"fulfillmentOrderId": fulfillment_order_id
# # ,
# #                                                 "fulfillmentOrderLineItems": fulfillment_order_lineitems},
# #                 "notifyCustomer": True,
# #                 "trackingInfo": {"company": carrier, "number": tracking_number},
# #             }
# #         }
#         frappe.log_error(title="Fulfillment Variables",message=f"Shopify Order Number:{shopify_order_id}\n{variables}")
#         try:
#             res = shopify.GraphQL().execute(
#                 query=fulfillment_mutation,
#                 operation_name="fulfillmentTrackingInfoUpdate",
#                 variables=variables,
#             )
#             shopify.ShopifyResource.clear_session()
#             response = json.loads(res)
#             # if is_fulfillment_successful(response):
#             #     frappe.db.set_value("Delivery Note",dn_created,"custom_fulfilled_on_shopify",1)
#             return response
#         except:
#             frappe.log_error(
#                 title="Carrier or Tracking number Error",
#                 message=f"Traceback:{frappe.get_traceback()}\n\nError:Tracking Number or Carrier not sent",
#             )
#     else:
#         frappe.log_error(
#             title="Fulfillment Id Error", message="Could not generate fulfillment id"
#         )

# def updated_tracking_info(
#     shopify_order_id: str,
#     tracking_number: str,
#     carrier: str,
#     dn_created: str,
#     sales_order: str
# ) -> dict:
#     """
#     Updates tracking number on Shopify for an EXISTING fulfillment.
#     (Tracking applies to all items already associated with that fulfillment)
#     """

#     # 1. Load the update mutation file
#     mutation_path = os.path.join(current_dir, "mutation.graphql")
#     with open(mutation_path, "r") as f:
#         update_mutation = f.read()

#     # 2. Load Shopify settings
#     marketplace = frappe.db.get_value("Sales Order", sales_order, "marketplace")
#     setting_name = get_shopify_setting_by_marketplace(marketplace)
#     setting_doc = frappe.get_doc("Shopify Integration Settings", setting_name)

#     shop_url = f"https://{setting_doc.shop_name}.myshopify.com"
#     api_version = setting_doc.api_version
#     access_token = setting_doc.get_password("access_token")

#     # 3. Activate Shopify Session
#     session = shopify.Session(shop_url, api_version, access_token)
#     shopify.ShopifyResource.activate_session(session)

#     # 4. Query current fulfillments (NOT fulfillmentOrders)
#     fulfillment_query = """
#     query GetFulfillments($orderId: ID!) {
#       order(id: $orderId) {
#         fulfillments(first: 10) {
#           edges {
#             node {
#               id
#             }
#           }
#         }
#       }
#     }
#     """

#     result = shopify.GraphQL().execute(
#         query=fulfillment_query,
#         variables={"orderId": shopify_order_id}
#     )
#     result_json = json.loads(result)

#     fulfillments = (
#         result_json.get("data", {})
#                   .get("order", {})
#                   .get("fulfillments", {})
#                   .get("edges", [])
#     )

#     if not fulfillments:
#         frappe.log_error(
#             title="No Fulfillment Found",
#             message=f"No fulfillment exists in Shopify for {shopify_order_id}"
#         )
#         return {}

#     # Use the FIRST fulfillment (Shopify always uses one fulfillment per DN)
#     fulfillment_id = fulfillments[0]["node"]["id"]

#     # 5. Correct update variables
#     variables = {
#         "fulfillmentId": fulfillment_id,
#         "trackingInfo": {
#             "company": carrier,
#             "number": tracking_number,
#             "numbers": [tracking_number],
#         },
#         "notifyCustomer": False
#     }

#     # "url": f"https://www.{carrier.lower()}.com/track/{tracking_number}"

#     # 6. Execute update mutation
#     response = shopify.GraphQL().execute(
#         query=update_mutation,
#         operation_name="fulfillmentTrackingInfoUpdate",
#         variables=variables
#     )
#     response_json = json.loads(response)

#     frappe.log_error(
#         title="Tracking Update Response",
#         message=response_json
#     )

#     shopify.ShopifyResource.clear_session()
#     return response_json

def get_so_name_marketplace_by_shopify_id(shopify_order_id: str):
    return frappe.db.get_value(
        "Sales Order",
        {"custom_shopify_order_id_number": shopify_order_id},
        ["name", "marketplace"],
        as_dict=True
    )

def get_fulfillment_status_to_exclude(shopify_setting_doc, order_fullfilment_status: str) -> bool:
    
    status = (order_fullfilment_status or "").strip().upper()
    if not status:
        return False
    excluded_statuses = {
        (row.shopify_fulfillment_status or "").strip().upper()
        for row in shopify_setting_doc.shopify_fulfillment_statuses_to_exclude
        if row.shopify_fulfillment_status
    }

    return status in excluded_statuses


def get_order_status(extensiv_sales_orders, shopify_marketplace):
    """
    This fn takes extensiv order which are being send to Extensiv 3PL
    makes call to shopify 
    and returns order which are not on hold
    """
    try:
        if not len(extensiv_sales_orders): return
    
        gids = []
        for so in extensiv_sales_orders:
            if not so.custom_shopify_order_id_number: 
                frappe.log_error(title="Gid not found in Order", message=f"order name: {so.name}")
                continue
            gids.append(so.custom_shopify_order_id_number)

        query_file_path = os.path.join(current_dir, "selling_query.graphql")
        with open(query_file_path, "r") as file:
            order_status_query = file.read()

        setting_doc_name = frappe.db.get_value(
            "Shopify Integration Settings",
            {"marketplace": shopify_marketplace,"enabled":1},
            "name"
        )
        if not setting_doc_name:
            frappe.log_error("Shopify settings not found", shopify_marketplace)
            return [so.name for so in extensiv_sales_orders]

        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)

        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")
        session = shopify.Session(shop_url, api_version, access_token)

        shopify.ShopifyResource.activate_session(session)
        order_status_response = shopify.GraphQL().execute(
            query=order_status_query,
            operation_name="getOrdersStatus",
            variables={"ids": gids},
        )
        order_status_json = json.loads(order_status_response)

        nodes = order_status_json.get("data", {}).get("nodes", [])
        filtered_extensiv_so = []
        on_hold_extensiv_so = []

        # optimized version of code
        status_map = {node["id"]: node["displayFulfillmentStatus"] for node in nodes if node}
        for so in extensiv_sales_orders:
            status = status_map.get(so.custom_shopify_order_id_number)

            if status != "ON_HOLD":
                filtered_extensiv_so.append(so.name)
            else:
                on_hold_extensiv_so.append(so.name)
                

        if on_hold_extensiv_so:
            frappe.log_error(
                title="On hold Extensiv SO Found while sending to 3PL",
                message=f"orders: {on_hold_extensiv_so}"
            ) 
              
        return filtered_extensiv_so

    except Exception as e:
        frappe.log_error(
            title="Error in get_order_status",
            message=frappe.get_traceback()
        )
        return [so.name for so in extensiv_sales_orders]
    finally:
        shopify.ShopifyResource.clear_session()

# US - return label logic - if SO not found
def sync_order_not_found(setting_doc_name, shopify_order_gid)-> bool | dict:
    """
        create SO in ERP via shopify_order_gid and returns the shipping detail
    """
    try:
        frappe.log_error("SYNC FN HIT", "DEBUG")
        query_dir = os.path.join(current_dir, "selling_query.graphql")
        with open(query_dir, "r") as file:
            query = file.read()
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        # Get necessary information from setting doc
        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")

        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)
        order_id = shopify_order_gid.split("/")[-1]
        res = shopify.GraphQL().execute(
            query=query,
            variables={"nos": 1,"order_query": f"id:{order_id}","after": None},
            operation_name="GetOrdersInfo",
        )
        response = json.loads(res)
        frappe.log_error(title="shopify order res", message=f"{response}")
        print("res",response)
        # frappe.log_error(title="Shopify Payload fulfillment", message=response)
        order_data = response["data"]["orders"]["edges"]
        shopify.ShopifyResource.clear_session()

        if not order_data:
            frappe.log_error(
            title=f"No Shopify order found for GID: {shopify_order_gid}",
            message=f"Response: {response}"
            )
            return False

        data = order_data[0]["node"]
        frappe.log_error(title="shopify order----data", message=f"{data}")

        try:
            from shopify_integration.shopify_selling.orders import create_shopify_sales_order
            create_shopify_sales_order(data, setting_doc_name, is_return=True)
        except Exception:
            frappe.log_error(
                title=f"SO creation failed for {shopify_order_gid}",
                message=frappe.get_traceback()
            )
            return False  # don't proceed if SO wasn't created

        frappe.db.commit()

        so_name = frappe.get_value(
            "Sales Order",
            {"custom_shopify_order_id_number": shopify_order_gid},
            "name"
        )

        if so_name:
            try:
                frappe.db.set_value(
                    "Sales Order",
                    so_name,
                    "custom_synced_to_extensiv",
                    1,
                    update_modified=False
                )
                frappe.db.commit()
            except Exception:
                frappe.log_error(
                    title=f"SO update failed for {so_name}",
                    message=frappe.get_traceback()
                )

        if not so_name:
            # SO wasn't created — return shipping details as fallback
            shipping_details = data.get("shippingAddress")
            return shipping_details

        return True

    except Exception:
        frappe.log_error(
            title=f"Order syncing failed for gid: {shopify_order_gid}",
            message=frappe.get_traceback()
        )
        return False


def update_return_label_and_rma_metafield(
    shopify_gid: str,
    return_label_and_rma_url: str,
    setting_doc_name: str,
) -> bool:
    """
    Store return label URL in Shopify metafield:
    custom.rma_return_label_url
    """
    try:
        query_dir = os.path.join(current_dir, "return_label_query.graphql")
        with open(query_dir, "r") as file:
            query = file.read()

        setting_doc = frappe.get_doc(
            "Shopify Integration Settings",
            setting_doc_name,
        )

        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")

        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)


        variables = {
            "metafields": [
                {
                    "ownerId": shopify_gid,
                    "namespace": "custom",
                    "key": "return_label_and_rma",
                    "type": "url",
                    "value": return_label_and_rma_url,
                }
            ]
        }

        response = shopify.GraphQL().execute(
            query=query,
            variables=variables,
            operation_name="MetafieldsSet"
        )

        data = json.loads(response)

        frappe.log_error("metafield label update",data)
        user_errors = (
            data.get("data", {})
            .get("metafieldsSet", {})
            .get("userErrors", [])
        )

        if user_errors:
            frappe.log_error(
                title="Shopify Metafield Update Failed",
                message=json.dumps(user_errors, indent=2),
            )
            return False

        frappe.log_error(
            title="Shopify Metafield Updated",
            message=f"ownerId={shopify_gid}, url={return_label_and_rma_url}",
        )

        return True

    except Exception:
        frappe.log_error(
            title="Shopify Metafield Update Exception",
            message=frappe.get_traceback(),
        )
        return False


def create_issue(subject, reference):
    """
    create issue doctype if any error occurs while creating return document
    """
    new_issue = frappe.new_doc("Issue")
    new_issue.subject = subject
    new_issue.description = str(reference)
    new_issue.save(ignore_permissions=True)
    frappe.db.commit()
    

# depricated
def upload_return_label_rma(return_id, return_label_and_rma, setting_doc_name)-> bool:
    """
        upload return laben to shoify, which gets send to customer
    """
    try:
        frappe.log_error("upload_return_label started", "DEBUG")
        query_dir = os.path.join(current_dir, "return_label_query.graphql")
        with open(query_dir, "r") as file:
            query = file.read()
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        # Get necessary information from setting doc
        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")

        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)


        res = shopify.GraphQL().execute(
            query=query,
            variables={"returnId":return_id},
            operation_name="GetReverseDelivery",
        )
        data = json.loads(res)
        frappe.log_error(title="Reverse Delivery", message=f"{data}")

        rfo_node = data["data"]["return"]["reverseFulfillmentOrders"]["edges"][0]["node"]
        rfo_id = rfo_node["id"]
        line_items = [
            {
                "reverseFulfillmentOrderLineItemId": edge["node"]["id"],
                "quantity": edge["node"]["totalQuantity"]
            }
            for edge in rfo_node["lineItems"]["edges"]
        ]

        variables={
            "rfoId": rfo_id,
            "lineItems": line_items,
            "label": {"fileUrl": return_label}

        }
        res = shopify.GraphQL().execute(
            query=query,
            variables=variables,
            operation_name="reverseDeliveryShippingCreate",
        )
        data = json.loads(res)
        frappe.log_error(title="reverseDeliveryShippingCreate", message=f"{data}")

        # Check for errors
        user_errors = data.get("data", {}).get("reverseDeliveryCreateWithShipping", {}).get("userErrors", [])
        if user_errors:
            frappe.log_error(title="Reverse Delivery UserErrors", message=f"{user_errors}")
            return

        reverse_delivery_id = data["data"]["reverseDeliveryCreateWithShipping"]["reverseDelivery"]["id"]
        frappe.log_error(title="Reverse Delivery Created", message=f"ID: {reverse_delivery_id}")
        frappe.log_error("upload_return_label finished", "DEBUG")

        return True

    except Exception:
        frappe.log_error(
            title="Return Label Upload Failed",
            message=frappe.get_traceback()
        )
        return False

# depricated
def close_return_status(so_name, return_id, setting_doc_name):
    """
    closes return status on shopify

    later add this in some graphql file
    mutation returnClose($id: ID!) {
        returnClose(id: $id) {
            return {
            # Return fields
            }
            userErrors {
            field
            message
            }
        }
        }
    """
    try:

        query_dir = os.path.join(current_dir, "return_label_query.graphql")
        with open(query_dir, "r") as file:
            query = file.read()
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        # Get necessary information from setting doc
        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")

        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)

        res = shopify.GraphQL().execute(
            query=query,
            variables={"id":return_id},
            operation_name="returnClose",
        )

        data = json.loads(res)
        frappe.log_error(title="shopify return status - close", message=data)
        user_errors = data.get("data", {}).get("returnClose", {}).get("userErrors", [])
        if user_errors:
            frappe.log_error(
                title="returnClose UserErrors",
                message=f"SO: {so_name} | errors: {user_errors}"
            )
            return False

        status = data.get("data", {}).get("returnClose", {}).get("return", {}).get("status")
        frappe.log_error(
            title="Return Closed on Shopify",
            message=f"SO: {so_name} | return_id: {return_id} | status: {status}"
        )
        return True
    except Exception:
        frappe.log_error(title="Error while closing the return status on shopify", message=f"{frappe.get_traceback()}")
        return False


def addNoteToOrder(so_name:str, extensiv_response: str, setting_doc_name: str):
    """
    first fetch the older notes and merge them with the extensiv response then push
    adds the 3pl response to shopify order in timeline
    """
    try:
        shopify_id = frappe.get_value("Sales Order", so_name, "custom_shopify_order_id_number")
        note_payload =   {
            "id": shopify_id,
            "note": extensiv_response
        }


        query_dir = os.path.join(current_dir, "return_label_query.graphql")
        with open(query_dir, "r") as file:
            query = file.read()
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        # Get necessary information from setting doc
        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")

        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)

        # ---------------------------------------
        # Fetch existing Shopify order note
        # ---------------------------------------
        res = shopify.GraphQL().execute(
            query=query,
            variables={"id": shopify_id},
            operation_name="GetOrderNote",
        )

        data = json.loads(res)

        existing_note = (
            data.get("data", {})
            .get("order", {})
            .get("note")
        ) or ""

        # ---------------------------------------
        # Merge notes
        # ---------------------------------------
        if existing_note:
            updated_note = (
                f"{existing_note}\n"
                f"{extensiv_response}"
            )
        else:
            updated_note = extensiv_response

        note_payload = {
            "id": shopify_id,
            "note": updated_note,
        }

        res = shopify.GraphQL().execute(
            query=query,
            variables={"input": note_payload},
            operation_name="OrderUpdate",
        )

        data = json.loads(res)
        frappe.log_error(title="Added time line - close", message=data)
        user_errors = data.get("data", {}).get("orderUpdate", {}).get("userErrors", [])
        if user_errors:
            frappe.log_error(
                title="OrderUpdate UserErrors",
                message=f"SO: {so_name} | errors: {user_errors}"
            )
            return False

        order = data.get("data", {}).get("orderUpdate", {}).get("order", {})
        frappe.log_error(
            title="Added Note to Shopify Order",
            message=f"SO: {so_name} | order_id: {order.get('id')} | note: {order.get('note')}"
        )
        return True
    except Exception:
        frappe.log_error(title="Error while adding Timeline on shopify", message=f"{frappe.get_traceback()}")
        return False


## Depricated : not in use
def fetch_return_label_details(return_gid, setting_doc_name):
    try:
        if not return_gid:
            frappe.throw("Return_GID not found for fetching details")
            raise

        query_dir = os.path.join(current_dir, "return_label_query.graphql")
        with open(query_dir, "r") as file:
            query = file.read()

        if not str(return_gid).startswith("gid://"):
            return_gid = f"gid://shopify/Return/{return_gid}"
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        # Get necessary information from setting doc
        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")

        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)

        print("return_gid",return_gid)
        res = shopify.GraphQL().execute(
            query=query,
            variables={"id": return_gid},
            operation_name="GetReturnDetails",
        )
        response = json.loads(res)

        frappe.log_error(title="shopify res", message=f"{response}")
        print("res",response)
        # frappe.log_error(title="Shopify Payload fulfillment", message=response)
        return_data = response["data"]["return"]
        shopify.ShopifyResource.clear_session()
        
        return return_data
    except Exception:
        frappe.log_error(title="Fetching return details failed", message=frappe.get_traceback())
        


# EU - inventory logic
# def fetch_products_location(setting_doc_name):

#     setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)

#     try:
#         query_file_path = os.path.join(current_dir, "selling_query.graphql")
#         with open(query_file_path, "r") as file:
#             order_status_query = file.read()
#     except FileNotFoundError:
#         frappe.log_error(
#             title="Shopify Inventory Sync — Query File Missing",
#             message=f"selling_query.graphql not found at: {query_file_path}"
#         )
#         return


#     try:
#         shop_name = setting_doc.shop_name
#         shop_url = f"https://@{shop_name}.myshopify.com"
#         api_version = setting_doc.api_version
#         access_token = setting_doc.get_password("access_token")
#         session = shopify.Session(shop_url, api_version, access_token)
#         shopify.ShopifyResource.activate_session(session)
#     except Exception:
#         frappe.log_error(
#             title="Shopify Inventory Sync — Session Init Failed",
#             message=frappe.get_traceback()
#         )
#         return

#     inventory_response = shopify.GraphQL().execute(
#         query=order_status_query,
#         operation_name="getProductInventoryId",
#     )
#     shopify.ShopifyResource.clear_session()
    
#     inventory_response_json = json.loads(inventory_response)

#     return inventory_response_json


# def fetch_inventory_location_id(setting_doc_name, manhattan_inventory: dict):
#     """
#     Takes setting_doc_name and inventory {sku: qty}.
#     Fetches inventoryItemId and locationId of all products on Shopify.
#     Maps sku → inventoryItemId, builds payload and calls update_shopify_inventory.
#     """

#     # --- Guard: setting doc name ---
#     if not setting_doc_name:
#         frappe.log_error(
#             title="Shopify Inventory Sync — Missing Setting Doc",
#             message="setting_doc_name is empty or None"
#         )
#         return

#     # --- Guard: inventory ---
#     if not manhattan_inventory:
#         frappe.log_error(
#             title="Shopify Inventory Sync — Empty Inventory",
#             message="manhattan_inventory dict is empty, nothing to sync"
#         )
#         return

    
#     inventory_response_json = fetch_products_location(setting_doc_name)

#     products = inventory_response_json.get("data", {}).get("products", {}).get("edges", [])
#     locations_list = inventory_response_json.get("data", {}).get("locations", {}).get("edges", [])
#     print(f"length of products: {len(products)}")
#     location_id = None

#     for location in locations_list:
#         if location["node"]["name"]:
#             location_id = location["node"]["id"]
#             break

    
#     if not location_id:
#         frappe.log_error(
#             title="Shopify Inventory Sync — Location Not Found",
#             message=f"No Shopify location matched '{inventory_location_name}'. Available: {[l['node']['name'] for l in locations_list]}"
#         )
#         return

#     sku_to_inventory_id = {}
#     missing_sku_products = []

#     for product in products:
#         title = product["node"]["title"]
#         variants = product["node"]["variants"]["edges"]

#         for variant in variants:
#             sku = variant["node"].get("sku", "").strip()
#             inventory_id = variant["node"]["inventoryItem"]["id"]

#             if sku:
#                 sku_to_inventory_id[sku] = inventory_id
                

#     # --- Build quantities payload ---
#     quantities = []
#     skus_not_found = []

#     for sku, qty in manhattan_inventory.items():
#         inventory_item_id = sku_to_inventory_id.get(sku)
#         if inventory_item_id:
#             quantities.append({
#                 "inventoryItemId": inventory_item_id,
#                 "locationId": location_id,
#                 "quantity": int(qty)
#             })
#         else:
#             skus_not_found.append(sku)

#     print(f"qty: {quantities}")
#     if skus_not_found:
#         frappe.log_error(
#             title="Shopify Inventory Sync — SKUs Not in Shopify",
#             message=f"{len(skus_not_found)} SKUs from Manhattan not found in Shopify: {skus_not_found}"
#         )

#     # --- Guard: nothing to update ---
#     if not quantities:
#         frappe.log_error(
#             title="Shopify Inventory Sync — No Quantities to Update",
#             message="quantities list is empty after SKU mapping — mutation not called"
#         )
#         return

#     frappe.logger().info(f"Syncing {len(quantities)} SKUs to Shopify location '{inventory_location_name}'")

#     # --- Run mutation ---
#     # try:
#     #     result = update_shopify_inventory(setting_doc_name, quantities)
#     #     return result
#     # except Exception:
#     #     frappe.log_error(
#     #         title="Shopify Inventory Sync — Mutation Failed",
#     #         message=frappe.get_traceback()
#     #     )


# def update_shopify_inventory(setting_doc_name,quantities: list):

#     variables = {
#         "input": {
#             "name": "available",
#             "reason": "correction",
#             "referenceDocumentUri": "erpnext://3pl-inventory-sync",
#             "ignoreCompareQuantity": True,
#             "quantities": quantities
#         }
#     }
#     mutation_file_path = os.path.join(current_dir, "mutation.graphql")
#     with open(mutation_file_path, "r") as file:
#         mutation = file.read()
#     setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)

#     shop_name = setting_doc.shop_name
#     shop_url = f"https://@{shop_name}.myshopify.com"
#     api_version = setting_doc.api_version
#     access_token = setting_doc.get_password("access_token")
#     session = shopify.Session(shop_url, api_version, access_token)

#     shopify.ShopifyResource.activate_session(session)
#     response = shopify.GraphQL().execute(
#         query=mutation,
#         variables=variables,
#         operation_name="InventorySet"
#     )
#     shopify.ShopifyResource.clear_session()

#     response_json = json.loads(response)
#     # --- Step 6: Handle errors ---
#     user_errors = (
#         response_json.get("data", {})
#         .get("inventorySetQuantities", {})
#         .get("userErrors", [])
#     )
#     if user_errors:
#         frappe.log_error(
#             message=str(user_errors),
#             title="Shopify Inventory Mutation Errors"
#         )

#     return response_json


def get_current_shopify_order_items(shopify_order_id):

    import shopify

    query = """
    query getOrder($id: ID!) {
    order(id: $id) {
        lineItems(first : 10) {
            edges {
                node {
                    id
                    sku
                    quantity
                    currentQuantity

                    discountedUnitPriceSet {
                        shopMoney {
                            amount
                        }
                    }
                }
            }
        }
    }
    }
    """

    marketplace = "Shopify"
    setting_doc_name = get_shopify_setting_by_marketplace(
        marketplace
    )
    setting_doc = frappe.get_doc(
        "Shopify Integration Settings",
        setting_doc_name
    )
    shop_url = f"https://@{setting_doc.shop_name}.myshopify.com"
    session = shopify.Session(
        shop_url,
        setting_doc.api_version,
        setting_doc.get_password("access_token"),
    )
    shopify.ShopifyResource.activate_session(session)
    response = shopify.GraphQL().execute(
        query=query,
        variables={"id": shopify_order_id},
    )
    response = json.loads(response)
    shopify.ShopifyResource.clear_session()
    edges = (
        response.get("data", {})
        .get("order", {})
        .get("lineItems", {})
        .get("edges", [])
    )
    items = []

    for edge in edges:
        node = edge["node"]
        items.append({
            "id": node["id"],
            "sku": node["sku"],
            "quantity": node["currentQuantity"],
            "rate": flt(
                node.get("discountedUnitPriceSet", {})
                .get("shopMoney", {})
                .get("amount", 0)
            )
        })

    return items