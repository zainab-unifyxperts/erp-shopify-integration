from extensiv_integration.extensiv_selling.orders import get_facility_access_token
from shopify_integration.shopify_selling.shopify_selling_utils import close_return_status, addNoteToOrder, create_issue
# from shopify_integration.shopify_selling.api.return_label import create_return_document
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice, make_delivery_note
from erpnext.controllers.sales_and_purchase_return import make_return_doc
import json
import frappe
import requests
import time

def return_item_to_warehouse(nearest_warehouse, so_name, return_reason_note, return_label_details, return_line_items):
    frappe.log_error("Returning item to warehouse started DEBUG")
    # https://secure-wms.com/inventory/receivers
    ext_doc = frappe.get_doc("Extensiv Settings", {"enabled": 1})
    api_url  = ext_doc.base_url + "inventory/receivers"
    facility_row = next(
        (row for row in ext_doc.extensiv_facility_settings if row.warehouse == nearest_warehouse),
        None
    )

    marketplace_order_id = frappe.get_value("Sales Order", so_name, "marketplace_order_id")
    # extract tracking number from return_label_details - imp
    tracking_number = return_label_details.get("tracking_number") # dummy do the real thing
    carrier = return_label_details.get("carrier")
    """
        [
            {
            "itemIdentifier": {
                "sku": "PT-RMT-V2"
            },
            "expectedQty": "1"
            }
        ]
    """
    receiptItems = []
    if facility_row.facility_id == "1":
        for item in return_line_items:
            receiptItems.append({
                "itemIdentifier": {
                    "sku": item.get("sku")
                },
                "qty": item.get("qty"),
                "locationInfo": {
                "locationId": 2
            }

            })
        frappe.log_error("facility id == 1")
    else:
        for item in return_line_items:
            receiptItems.append({
                "itemIdentifier": {
                    "sku": item.get("sku")
                },
                "qty": item.get("qty"),
            #     "locationInfo": {
            #     "locationId": 2
            # }
            })
    
    # loop through return_line_items and get the recieptItems
    if not facility_row:
        frappe.throw(f"No Extensiv facility settings found for warehouse: {nearest_warehouse}")
    try:
        fid = facility_row.facility_id
        access_token = get_facility_access_token(nearest_warehouse)
    except:
        frappe.log_error(
            title="Access Token Error",
            message=f"Traceback:{frappe.get_traceback()} \n\n Error:Access token has not been generated",
        )
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + access_token,
    }
    body = {
        "customerIdentifier": {
            "id": facility_row.customer_id
        },
        "facilityIdentifier": {
            "id": fid
        },
        "referenceNum": f"RETURN-{marketplace_order_id}-{int(time.time())}",      
        "poNum": "",
        "carrier":carrier,
        "trackingNumber": tracking_number,
        "notes": return_reason_note,
        "isReturn": True,
        "receiveItems": receiptItems
    }
    frappe.log_error(title="Returning item to warehouse finished Paylod", message=f"BODY: {body}, HEADER: {headers}, API_URL: {api_url}")

    response = requests.post(api_url, headers=headers, json=body)
    try:
        if response.status_code in [200,201]:
            response_json = response.json()
            frappe.log_error(title="Returning item to warehouse finished DEBUG", message=f"{response_json}")
            receiver_id = response_json['ReadOnly']['ReceiverId']
            return receiver_id
        else:
            frappe.log_error(title="Extensiv API error",message=f"API:POST Return Item \n API Status Code:{response.status_code}\n\n data\n {response.json()}")
    except requests.exceptions.HTTPError as e:
        frappe.log_error(title="HTTP Error", message=f"HTTP error occurred: {e}")
    except Exception:
        frappe.log_error(title="General Error", message=f"An error occurred: {e}")


# ----- Backward Flow of Shipwise -----
@frappe.whitelist()
def trigger_return_status_check():
    
    """
    Whitelisted — called by cron every hour.
    Just enqueues the actual work.
    """
    frappe.enqueue(
        "shopify_integration.shopify_selling.api.return_item_status.check_return_item_status",
        queue="long",
        timeout=600,
    )

    
def check_return_item_status():
    """
    Scheduled job — checks Extensiv receiver status for all pending returns.
    """

    # fetchign all the return which are pending
    pending_returns = frappe.get_all(
        "Sales Order",
        filters={
            "custom_receiver_id": ["not in", ["", None]],
            "custom_shopify_return": 0,
        },
        fields=["name", "custom_receiver_id"]
    )


    if not pending_returns:
        return

    frappe.log_error(title="Checking return status started DEBUG")

    # Build receiver_id → so_name map
    receiver_map = {
        str(so["custom_receiver_id"]): so["name"]
        for so in pending_returns
    }
    

    # Loop over facilities — each has its own token
    ext_doc = frappe.get_doc("Extensiv Settings", {"enabled": 1})
    # Setting doc name change required
    setting_doc_name = "alphardgolf-usa"

    all_receivers = []
    for facility_row in ext_doc.extensiv_facility_settings:
        try:
            fid = facility_row.facility_id
            access_token = get_facility_access_token(facility_row.warehouse)
        except Exception:
            frappe.log_error(
                title="Access Token Error",
                message=f"Facility: {fid}\n{frappe.get_traceback()}"
            )
            continue

        frappe.log_error(
            "FACILITY DEBUG",
            f"facility_id={fid}, warehouse={facility_row.warehouse}"
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/hal+json",
            "Authorization": f"Bearer {access_token}",
        }

        # Poll Extensiv for closed receivers (status==1 = closed per your comment)
        page = 1
        while True:
            try:
                api_url = f"{ext_doc.base_url}inventory/receivers?pgsiz=100&pgnum={page}&rql=status==1"

                response = requests.get(api_url, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()

                receivers_data = data.get("_embedded", {}).get(
                    "http://api.3plCentral.com/rels/inventory/receiver", []
                )

                if not receivers_data:
                    break

                all_receivers.extend(receivers_data)
                page += 1

            except Exception:
                frappe.log_error(title="Error while fetching close return data", message=f"{frappe.get_traceback()}")
                break   # ⚠️ don't continue infinitely
        
        frappe.log_error("Recievers", all_receivers)

        # Find SO against the receiver ID
    if not all_receivers:
        frappe.log_error(title="Receivers Empty", message="No receivers from Extensiv")
        return

    all_receivers_map = {
        str(ar["readOnly"]["receiverId"]): ar["readOnly"]
        for ar in all_receivers
        if ar.get("readOnly", {}).get("receiverId")
    }

    all_receivers_notes = {
        str(ar["readOnly"]["receiverId"]): ar.get("notes", "")
        for ar in all_receivers
        if ar.get("readOnly", {}).get("receiverId")
    }

    print(all_receivers_notes)
    for receiver in all_receivers:
        extensiv_response_currrent = ""
        extensiv_response_other_receiver = ""
        read_only = receiver.get("readOnly", {})
        receiver_id = str(read_only.get("receiverId"))  # str from the get-go
        status = read_only.get("status")
        # extensiv_response = receiver.get("notes") or ""
        if not receiver_map.get(str(receiver_id)):
            print("reciever id not found in receiver map")
            continue

        so_name = receiver_map.get(str(receiver_id))
        so_doc = frappe.get_doc("Sales Order", so_name)

        print("debug 1: found", "receiver_id: ", receiver_id, "so_name: ", so_name)
        return_line_items = []
        other_receiver_line_items = []
        pending_items = []
        total_shipping_cost = 0
        other_receiver_total_shipping_cost = 0


        for item in so_doc.items:
            classification = classify_item(item, receiver_id, all_receivers_map)

            if classification == "skip":
                continue

            if classification == "current":
                print("current", receiver_id)
                notes = all_receivers_notes.get(str(item.custom_extensiv_receiver_id), {})
                item.db_set("custom_item_return", 1)   # writes straight to the DB
                total_shipping_cost += float(item.custom_return_shipping_cost or 0)
                return_line_items.append(build_return_line_item(item))
                extensiv_response_currrent += f"\n{notes}"

            elif classification == "other_receiver":
                print("other receiver", receiver_id)
                notes = all_receivers_notes.get(str(item.custom_extensiv_receiver_id), {})
                # notes = all_receivers_notes.get(str(2496579), {})

                item.db_set("custom_item_return", 1)
                other_receiver_total_shipping_cost += float(item.custom_return_shipping_cost or 0)
                other_receiver_line_items.append(build_return_line_item(item))
                extensiv_response_other_receiver += f"\n{notes}"

            elif classification == "pending":
                print("pending", receiver_id)
                pending_items.append(item)

        frappe.log_error(title="Debug return and note",message=f"return_line_items: {return_line_items},pending_items: {pending_items},total_shipping_cost: {total_shipping_cost}, so_name: {so_name}")
        # fix this section in first half tommorrow
        if "RSTK" in extensiv_response_currrent:

            return_doc_success = create_return_document(return_line_items, so_name, total_shipping_cost)
            if not return_doc_success:
                create_issue(
                    subject=f"Return document creation failed for {so_name}",
                    reference=so_name,
                )

        if "RSTK" in extensiv_response_other_receiver:
            return_doc_success = create_return_document(other_receiver_line_items, so_name, other_receiver_total_shipping_cost)
            if not return_doc_success:
                create_issue(
                    subject=f"Return document creation failed for {so_name}",
                    reference=so_name,
                )

            frappe.log_error(
                title="classify debug",
                message=(
                    f"so={so_name} receiver_id={receiver_id!r} ({type(receiver_id).__name__})\n"
                    f"item_rid={item.custom_extensiv_receiver_id!r} "
                    f"({type(item.custom_extensiv_receiver_id).__name__}) "
                    f"item_return={item.custom_item_return} "
                    f"in_map={str(item.custom_extensiv_receiver_id) in all_receivers_map}"
                ),
            )

        # merge current and other receiver response and also add which return was this
        ext_res = f"{extensiv_response_currrent} \n {extensiv_response_other_receiver}"
        print
        response = addNoteToOrder(so_name, ext_res, setting_doc_name)
        if response is False:
            frappe.log_error(title="addNoteToOrder Failed", message=f"SO: {so_name}")
            continue

        if pending_items:
            next_item = pending_items[0]
            so_doc.db_set("custom_shopify_return", 0)
            so_doc.db_set("custom_return_description", next_item.custom_reason_description)
            so_doc.db_set("custom_receiver_id", next_item.custom_extensiv_receiver_id)
            so_doc.db_set("custom_shopify_return_id", next_item.custom_item_return_id)
        else:
            so_doc.db_set("custom_shopify_return", 1)

        frappe.log_error(title="pending items", message=f"So_Name, {so_name}, Pending items, {pending_items}")
    
    frappe.db.commit()
    frappe.log_error(title="Checking return status finished DEBUG")


def classify_item(item, receiver_id, all_receivers_map):
    if item.custom_item_return == 1:
        return "skip"

    item_rid = str(item.custom_extensiv_receiver_id or "")

    if item_rid == str(receiver_id):
        return "current"
    if item_rid and all_receivers_map.get(item_rid):
        return "other_receiver"
    if item_rid:
        return "pending"
    return "skip"
    

def build_return_line_item(item):
    return {
        "qty":           item.qty,
        "line_item_id":  item.custom_shopify_line_item_id,
        "sku":           item.item_code,
        "order_item_id": item.custom_extensiv_order_item_id
    }


# DN Creation and SI Creation - Inventory Adjustment

def create_return_document(return_line_items, so_name, shipping_cost):
    """
    Creates Return Document - DN and SI
    """
    frappe.log_error("Return Document creation started DEBUG")
    all_success = True

    for item in return_line_items:
        line_item_id = item["line_item_id"]
        qty = item["qty"]

        dn_records = frappe.get_all(
            "Delivery Note Item",
            filters={"custom_shopify_line_item_id": line_item_id},
            fields=["parent", "item_code"],
        )

        if not dn_records:
            frappe.log_error(
                title="No DN records found for line item",
                message=f"line_item_id: {line_item_id}, so_name: {so_name}"
            )
            # if no dn found create the dn and sync those details from Extensiv
            # Get item_code from SO line items using line_item_id → sku → item_code
            sku = item.get("sku")
            if sku:
                # Find item_code from SKU
                item_code_from_sku = frappe.get_value(
                    "Item",
                    {"item_code": sku},  # adjust if SKU maps differently
                    "name"
                )

                if item_code_from_sku:
                    # Find DN linked to this SO with this item_code
                    dn_records = frappe.get_all(
                        "Delivery Note Item",
                        filters={
                            "item_code": item_code_from_sku,
                            "against_sales_order": so_name,
                        },
                        fields=["parent", "item_code"],
                    )
                    
        if not dn_records:
            frappe.log_error(
                title="No DN records found for line item — skipping",
                message=f"line_item_id: {line_item_id}, sku: {item.get('sku')}, so_name: {so_name}"
            )
            all_success = False
            continue
        seen = set()
        for row in dn_records:
            dn_name = row["parent"]
            item_code = row["item_code"]
            key = (dn_name, item_code)

            if key in seen:
                continue  # manual dedup — more reliable than distinct=True
            seen.add(key)

            frappe.log_error(
                title="PROCESSING ITEM",
                message=f"{dn_name}, {item_code}, {qty}, {line_item_id}"
            )

            try:
                is_return_dn = create_return_dn(dn_name, item_code, qty, line_item_id, shipping_cost)
                is_return_si = create_return_si(dn_name, item_code, qty, line_item_id)
            except Exception:
                frappe.log_error(
                    title="Exception in return doc creation",
                    message=f"DN: {dn_name}, item: {item_code}\n{frappe.get_traceback()}"
                )
                all_success = False
                continue  # ← keep going for other items

            # Check for None too, not just False
            if not is_return_dn or not is_return_si:
                frappe.log_error(
                    title="Return Document Creation Failed",
                    message=(
                        f"DN: {dn_name}, Item: {item_code}, "
                        f"line_item_id: {line_item_id}\n"
                        f"is_return_dn={is_return_dn}, is_return_si={is_return_si}, "
                        f"so_name: {so_name}"
                    )
                )
                all_success = False
                continue  # ← keep going for other items

    # Commit once, after all items are processed
    frappe.db.commit()
    frappe.log_error("Return Document creation finished DEBUG")

    return all_success
    

def create_return_dn(submitted_dn_name, target_item_code, return_qty, line_item_id, total_shipping_cost):
    try:
        frappe.log_error(
            title="creating return DN",
            message=f"{submitted_dn_name}, {target_item_code}, {return_qty}, {line_item_id}"
        )

        current_user = frappe.session.user
        frappe.set_user("Administrator")
        return_dn = make_return_doc("Delivery Note", submitted_dn_name)
        frappe.set_user(current_user)

        items_to_keep = []

        for item in return_dn.items:
            # New orders — match by line_item_id + item_code
            match_by_line_item = (
                item.custom_shopify_line_item_id == line_item_id
                and item.item_code == target_item_code
            )
            # Old orders — match by item_code only (line_item_id not set)
            match_by_item_code = (
                not item.custom_shopify_line_item_id
                and item.item_code == target_item_code
            )

            if match_by_line_item or match_by_item_code:
                item.qty = -abs(return_qty)
                items_to_keep.append(item)

        return_dn.set("items", items_to_keep)
        return_dn.custom_return_total_cost = total_shipping_cost
        if not return_dn.items:
            frappe.log_error(
                title="No matching item found for return DN",
                message=f"DN: {submitted_dn_name}, item: {target_item_code}, line_item_id: {line_item_id}"
            )
            return False

        return_dn.insert(ignore_permissions=True)
        return_dn.submit()
        return True

    except Exception:
        frappe.log_error(
            title="Return DN Creation Failed",
            message=frappe.get_traceback()
        )
        return False


def create_return_si(dn_name, target_item_code, return_qty, line_item_id):
    try:
        si_records = frappe.get_all(
            "Sales Invoice Item",
            filters={"delivery_note": dn_name},
            fields=["parent"],
            distinct=True
        )

        if not si_records:
            frappe.log_error(
                title="No SI found for DN",
                message=f"DN: {dn_name}, item: {target_item_code}"
            )
            return False

        # Take only the first SI — avoid duplicate processing
        si_docname = si_records[0]["parent"]

        # Guard — check if return SI already exists for this SI + item
        existing_return = frappe.get_all(
            "Sales Invoice",
            filters={
                "return_against": si_docname,
                "docstatus": ["!=", 2],   # not cancelled
            },
            fields=["name"]
        )
        if existing_return:
            frappe.log_error(
                title="Return SI already exists — skipping",
                message=f"SI: {si_docname}, existing: {existing_return[0]['name']}"
            )
            return True   # not a failure, just already done

        current_user = frappe.session.user
        frappe.set_user("Administrator")
        return_si = make_return_doc("Sales Invoice", si_docname)
        frappe.set_user(current_user)

        items_to_keep = []

        for item in return_si.items:
            # Old orders — match by item_code only
            match_by_item_code = (
                not item.custom_shopify_line_item_id
                and item.item_code == target_item_code
            )

            if match_by_line_item or match_by_item_code:
                item.qty = -abs(return_qty)
                items_to_keep.append(item)

        return_si.set("items", items_to_keep)

        if not return_si.items:
            frappe.log_error(
                title="No matching item found for return SI",
                message=f"SI: {si_docname}, item: {target_item_code}, line_item_id: {line_item_id}"
            )
            return False

        return_si.calculate_taxes_and_totals()
        return_si.update_outstanding_for_self = 1
        return_si.insert(ignore_permissions=True)
        return_si.submit()

        return True

    except Exception:
        frappe.log_error(
            title="Return SI Failed",
            message=frappe.get_traceback()
        )
        return False    

