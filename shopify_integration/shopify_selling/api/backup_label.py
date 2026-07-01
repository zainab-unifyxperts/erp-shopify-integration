
import frappe
import json
import hmac
import hashlib
import base64
import requests
from shopify_integration.shopify_selling.shopify_selling_utils import sync_order_not_found, update_return_label_and_rma_metafield, verify_webhook, create_issue
from shopify_integration.shopify_selling.api.shipwise_integration import create_return_label, generate_pdf_url
from shopify_integration.shopify_selling.api.return_item_status import return_item_to_warehouse
from extensiv_integration.extensiv_setup.shopify_fulfillment_sync import sync_delivery_note
from extensiv_integration.extensiv_selling.orders import get_token


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
    verify_webhook_source = verify_webhook(raw_data, hmac_header, client_secret)
    if verify_webhook_source:
        frappe.log_error(
            title="Webhook Verified",
            message="Webhook verified",
        )
    else:
        raise frappe.AuthenticationError

    json_data = json.loads(raw_data)

    frappe.log_error(title="handle_return payload", message=f"{json_data} -- Raw Data: {raw_data}")
    data = return_so_name_and_gid(json_data)
    shopify_order_gid = data.get("shopify_order_gid")
    so_name = data.get("so_name")

    # Check if order having returned status as 1
    already_returned = handle_is_returned(so_name) # set return_status = 0
    if already_returned == True:
        frappe.log_error(title="Return Status Closed For Earlier Return", message=f"Moving forward with next return: {so_name}")
    
    # fetch once before the loop
    so_items = frappe.get_all(
        "Sales Order Item",
        filters={"parent": so_name},
        fields=["custom_shopify_line_item_id", "item_code", "custom_extensiv_order_item_id"]
    )

    # build a lookup dict once
    line_item_sku_map = {row.get("custom_shopify_line_item_id"): row.item_code for row in so_items}
    line_item_order_item_id = {row.get("custom_shopify_line_item_id"): row.custom_extensiv_order_item_id for row in so_items}

    try:
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
                "qty":          item.get("quantity"),
                "line_item_id": line_item_id,
                "sku":          line_item_sku_map.get(line_item_id),
                "order_item_id": line_item_order_item_id.get(line_item_id)
            })
        all_item_return_note = " | ".join(notes) if notes else None

    except Exception:
        frappe.log_error(title="Parsing return payload failed", message=frappe.get_traceback())
        return


    # Setting doc name change required
    # Hand off everything to background — respond to Shopify immediately
    frappe.enqueue(
        "shopify_integration.shopify_selling.api.return_label.process_return_background",
        queue="long",
        return_id=return_id,
        return_line_items=return_line_items,
        return_reason=return_reason,
        return_reason_note=return_reason_note,
        shopify_order_gid=shopify_order_gid,
        so_name=so_name,
        setting_doc_name="alphardgolf-usa",
        all_item_return_note=all_item_return_note
    )

def handle_is_returned(so_name)-> bool | None:
    # check on item level too
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

def process_return_background(return_id, return_line_items, 
                               return_reason, return_reason_note,
                               shopify_order_gid, so_name, setting_doc_name, all_item_return_note):

    """
    this fn gets enqueue(long),
    if so_name not found then sync create required doc
    creates the return documents against SO
    gets return label via helper fn
    upload the return label on shopify
    """
    frappe.log_error("Process Enqueue started DEBUG")

    frappe.set_user("Administrator")  # ← set once here, covers everything below
    try:
        # 1. Sync order if not found
        if not so_name:
            result = sync_order_not_found(setting_doc_name, shopify_order_gid)
            if result is not True:
                create_issue("SO not found after sync attempt", shopify_order_gid)
                return

            so_name = frappe.get_value(
                "Sales Order",
                {"custom_shopify_order_id_number": shopify_order_gid},
                "name"
            )
            if not so_name:
                create_issue("SO name not found afafterter sync", shopify_order_gid)
                return
            res = store_extensiv_order_item_id(so_name)

            if res == False:
                frappe.log_error(title="Delivery Note OrderId Fetching or Saving Failed")
                create_issue("Delivery Note OrderId Fetching or Saving Failed", shopify_order_gid)
                raise

            try:
                sync_delivery_note(so_name)
            except Exception:
                frappe.log_error(title="sync_delivery_note failed", message=frappe.get_traceback())
                raise
            
        # 2. Update SO fields
        frappe.db.set_value("Sales Order", so_name, {
            "custom_shopify_return_id": return_id,
            "custom_return_reason": return_reason,
            "custom_return_description": all_item_return_note,
        })
        # 3. Create return document (your remaining work goes here)
        # create_return_document(return_line_items, so_name)

        # 4. Upload return label — last step, after everything is ready
        # return_label comes from shipwise/3PL integration (your next step)
        return_label_details = create_return_label(so_name, return_line_items)  # placeholder
        frappe.log_error(title="Return Item Debug",message=f"{return_label_details.get('nearest_warehouse')}, {so_name} , {return_reason_note}, {return_label_details}, {return_line_items}")

        # extract the url from return_label
        if return_label_details:
            receiver_id = return_item_to_warehouse(return_label_details.get('nearest_warehouse'), so_name, all_item_return_note, return_label_details, return_line_items)
            nearest_warehouse = return_label_details.get("nearest_warehouse")
            so_doc = frappe.get_doc("Sales Order", so_name)

            # storing child table values, nearest warehouse, extensiv return id
            so_doc.custom_receiver_id = receiver_id
            so_doc.custom_nearest_warehouse = nearest_warehouse

            so_items = so_doc.items
            return_line_item_ids = {
                r.get("line_item_id") for r in return_line_items
            }

            frappe.log_error("Saving details", return_line_item_ids)
            # dn_items = []
            for item in so_doc.items:
                if item.custom_shopify_line_item_id in return_line_item_ids:
                    if item.custom_item_return_id == return_id:
                        continue

                    item.custom_item_return_id = return_id
                    item.custom_reason = return_reason
                    item.custom_reason_description = return_reason_note
                    item.custom_extensiv_receiver_id = receiver_id
                    item.custom_return_shipping_cost = return_label_details.get("total_cost")
                    frappe.log_error("MATCH FOUND", f"{item.item_code} - ite {item.custom_item_return_id} return_id: {return_id}")

            frappe.log_error("Saving details of extensiv")
            try:
                so_doc.save(ignore_permissions=True)
                frappe.db.commit()
                frappe.log_error("SO saved successfully", so_name)
            except Exception:
                frappe.log_error(
                    title="SO Save Failed",
                    message=f"SO: {so_name}\n{frappe.get_traceback()}"
                )
            
            pdf_url = generate_pdf_url(return_label_details.get('label_base64'), so_name)
            is_uploaded = update_return_label_and_rma_metafield(shopify_order_gid, pdf_url, setting_doc_name)
            if is_uploaded is False:
                frappe.log_error(title="Return label not uploaded", message=f"{so_name} - rID: {return_id} - rLabel: {return_label_details}")
            
        
        else:
            frappe.log_error(title="Return label not available", message=f"{so_name} - rID: {return_id} - rLabel: {return_label}")
            raise

    except Exception:
        create_issue("Return Label Enqueue Failed", so_name)
        frappe.log_error(title="process_return_background failed", message=f"so_name: {so_name} return_line_items: {return_line_items} Error: {frappe.get_traceback()}")


# def create_return_document(return_line_items, so_name, shipping_cost):
#     """
#     Creates Return Document - DN and SI
#     """
#     frappe.log_error("Return Document creation started DEBUG")
#     all_success = True

#     for item in return_line_items:
#         line_item_id = item["line_item_id"]
#         qty = item["qty"]

#         dn_records = frappe.get_all(
#             "Delivery Note Item",
#             filters={"custom_shopify_line_item_id": line_item_id},
#             fields=["parent", "item_code"],
#         )

#         if not dn_records:
#             frappe.log_error(
#                 title="No DN records found for line item",
#                 message=f"line_item_id: {line_item_id}, so_name: {so_name}"
#             )
#             # if no dn found create the dn and sync those details from Extensiv
#             # Get item_code from SO line items using line_item_id → sku → item_code
#             sku = item.get("sku")
#             if sku:
#                 # Find item_code from SKU
#                 item_code_from_sku = frappe.get_value(
#                     "Item",
#                     {"item_code": sku},  # adjust if SKU maps differently
#                     "name"
#                 )

#                 if item_code_from_sku:
#                     # Find DN linked to this SO with this item_code
#                     dn_records = frappe.get_all(
#                         "Delivery Note Item",
#                         filters={
#                             "item_code": item_code_from_sku,
#                             "against_sales_order": so_name,
#                         },
#                         fields=["parent", "item_code"],
#                     )
                    
#         if not dn_records:
#             frappe.log_error(
#                 title="No DN records found for line item — skipping",
#                 message=f"line_item_id: {line_item_id}, sku: {item.get('sku')}, so_name: {so_name}"
#             )
#             all_success = False
#             continue
#         seen = set()
#         for row in dn_records:
#             dn_name = row["parent"]
#             item_code = row["item_code"]
#             key = (dn_name, item_code)

#             if key in seen:
#                 continue  # manual dedup — more reliable than distinct=True
#             seen.add(key)

#             frappe.log_error(
#                 title="PROCESSING ITEM",
#                 message=f"{dn_name}, {item_code}, {qty}, {line_item_id}"
#             )

#             try:
#                 is_return_dn = create_return_dn(dn_name, item_code, qty, line_item_id, shipping_cost)
#                 is_return_si = create_return_si(dn_name, item_code, qty, line_item_id)
#             except Exception:
#                 frappe.log_error(
#                     title="Exception in return doc creation",
#                     message=f"DN: {dn_name}, item: {item_code}\n{frappe.get_traceback()}"
#                 )
#                 all_success = False
#                 continue  # ← keep going for other items

#             # Check for None too, not just False
#             if not is_return_dn or not is_return_si:
#                 frappe.log_error(
#                     title="Return Document Creation Failed",
#                     message=(
#                         f"DN: {dn_name}, Item: {item_code}, "
#                         f"line_item_id: {line_item_id}\n"
#                         f"is_return_dn={is_return_dn}, is_return_si={is_return_si}, "
#                         f"so_name: {so_name}"
#                     )
#                 )
#                 all_success = False
#                 continue  # ← keep going for other items

#     # Commit once, after all items are processed
#     frappe.db.commit()
#     frappe.log_error("Return Document creation finished DEBUG")

#     return all_success
    

# def create_return_dn(submitted_dn_name, target_item_code, return_qty, line_item_id, total_shipping_cost):
#     try:
#         frappe.log_error(
#             title="creating return DN",
#             message=f"{submitted_dn_name}, {target_item_code}, {return_qty}, {line_item_id}"
#         )

#         current_user = frappe.session.user
#         frappe.set_user("Administrator")
#         return_dn = make_return_doc("Delivery Note", submitted_dn_name)
#         frappe.set_user(current_user)

#         items_to_keep = []

#         for item in return_dn.items:
#             # New orders — match by line_item_id + item_code
#             match_by_line_item = (
#                 item.custom_shopify_line_item_id == line_item_id
#                 and item.item_code == target_item_code
#             )
#             # Old orders — match by item_code only (line_item_id not set)
#             match_by_item_code = (
#                 not item.custom_shopify_line_item_id
#                 and item.item_code == target_item_code
#             )

#             if match_by_line_item or match_by_item_code:
#                 item.qty = -abs(return_qty)
#                 items_to_keep.append(item)

#         return_dn.set("items", items_to_keep)
#         return_dn.custom_return_total_cost = total_shipping_cost
#         if not return_dn.items:
#             frappe.log_error(
#                 title="No matching item found for return DN",
#                 message=f"DN: {submitted_dn_name}, item: {target_item_code}, line_item_id: {line_item_id}"
#             )
#             return False

#         return_dn.insert(ignore_permissions=True)
#         return_dn.submit()
#         return True

#     except Exception:
#         frappe.log_error(
#             title="Return DN Creation Failed",
#             message=frappe.get_traceback()
#         )
#         return False


# def create_return_si(dn_name, target_item_code, return_qty, line_item_id):
#     try:
#         si_records = frappe.get_all(
#             "Sales Invoice Item",
#             filters={"delivery_note": dn_name},
#             fields=["parent"],
#             distinct=True
#         )

#         if not si_records:
#             frappe.log_error(
#                 title="No SI found for DN",
#                 message=f"DN: {dn_name}, item: {target_item_code}"
#             )
#             return False

#         # Take only the first SI — avoid duplicate processing
#         si_docname = si_records[0]["parent"]

#         # Guard — check if return SI already exists for this SI + item
#         existing_return = frappe.get_all(
#             "Sales Invoice",
#             filters={
#                 "return_against": si_docname,
#                 "docstatus": ["!=", 2],   # not cancelled
#             },
#             fields=["name"]
#         )
#         if existing_return:
#             frappe.log_error(
#                 title="Return SI already exists — skipping",
#                 message=f"SI: {si_docname}, existing: {existing_return[0]['name']}"
#             )
#             return True   # not a failure, just already done

#         current_user = frappe.session.user
#         frappe.set_user("Administrator")
#         return_si = make_return_doc("Sales Invoice", si_docname)
#         frappe.set_user(current_user)

#         items_to_keep = []

#         for item in return_si.items:
#             # Old orders — match by item_code only
#             match_by_item_code = (
#                 not item.custom_shopify_line_item_id
#                 and item.item_code == target_item_code
#             )

#             if match_by_line_item or match_by_item_code:
#                 item.qty = -abs(return_qty)
#                 items_to_keep.append(item)

#         return_si.set("items", items_to_keep)

#         if not return_si.items:
#             frappe.log_error(
#                 title="No matching item found for return SI",
#                 message=f"SI: {si_docname}, item: {target_item_code}, line_item_id: {line_item_id}"
#             )
#             return False

#         return_si.calculate_taxes_and_totals()
#         return_si.update_outstanding_for_self = 1
#         return_si.insert(ignore_permissions=True)
#         return_si.submit()

#         return True

#     except Exception:
#         frappe.log_error(
#             title="Return SI Failed",
#             message=frappe.get_traceback()
#         )
#         return False    




def return_so_name_and_gid(json_data):
    shopify_order_gid = json_data.get("order", {}).get("admin_graphql_api_id")

    so_name = frappe.get_value(
        "Sales Order",
        {"custom_shopify_order_id_number": shopify_order_gid},
        "name"
    )
    return {
        "so_name":so_name or "",
        "shopify_order_gid":shopify_order_gid
    }

def store_extensiv_order_item_id(so_name):
    try:
        ext_doc = frappe.get_doc("Extensiv Settings", {"enabled": 1})
        base_url = ext_doc.base_url

        marketplace_order_id = frappe.get_value(
            "Sales Order", so_name, "marketplace_order_id"
        )

        if not marketplace_order_id:
            frappe.log_error(
                title="No marketplace order ID",
                message=f"SO: {so_name}"
            )
            return False

        order_id = None  # will store items once found

        # 🔁 Loop through all facilities
        for facility in ext_doc.extensiv_facility_settings:
            try:
                fid = facility.facility_id
                access_token = get_token(fid)

                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/hal+json",
                    "Authorization": f"Bearer {access_token}",
                }

                api_url = (
                    f"{base_url}orders?"
                    f"detail=all&itemdetail=all&"
                    f"rql=referenceNum=={marketplace_order_id}"
                )

                response = requests.get(api_url, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()
                print(data)
                frappe.log_error("DAta",data)
                orders = data.get("_embedded", {}).get(
                    "http://api.3plCentral.com/rels/orders/order", []
                )

                if not orders:
                    frappe.log_error(
                        title="store_extensiv_order_item_id — No order found",
                        message=f"SO: {so_name} | marketplace_order_id: {marketplace_order_id}"
                    )
                    # return False
                print('orders', orders)
                order_id = orders[0].get("readOnly", {}).get("orderId")
                if not order_id:
                    frappe.log_error(
                        title="store_extensiv_order_item_id — orderId missing",
                        message=f"SO: {so_name} | response: {orders[0]}"
                    )
                    # return False

            except Exception:
                frappe.log_error(title="Failed fetching order item id", message=frappe.get_traceback())
                continue  # try next facility

        # 🔁 Map SO items with Extensiv order items
        so_items = frappe.get_all(
            "Sales Order Item",
            filters={"parent": so_name},
            fields=["name", "item_code"]
        )

        if not so_items:
            return False

        # 🧠 Create mapping (adjust key based on actual response)
        for item in so_items:
            frappe.db.set_value(
                    "Sales Order Item",
                    item["name"],
                    "custom_extensiv_order_item_id",
                    str(order_id)
            )

        frappe.db.commit()

        frappe.log_error(
            title="Order items mapped",
            message=f"SO: {so_name} | updated: {len(so_items)}"
        )

        return True

    except Exception:
        frappe.log_error(
            title="store_extensiv_order_item_id Failed",
            message=frappe.get_traceback()
        )
        return False


