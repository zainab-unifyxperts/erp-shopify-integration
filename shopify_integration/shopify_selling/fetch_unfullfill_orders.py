import shopify
import json
import frappe
import requests
from extensiv_integration.extensiv_selling.orders import get_facility_and_customer_id, get_token
from extensiv_integration.extensiv_setup.shopify_fulfillment_sync import get_all_orders_via_rql
import shopify
from collections import defaultdict
import os
from shopify_integration.shopify_selling.shopify_selling_utils import is_fulfillment_successful

# Get current directory to shopify_selling_utils.py
current_dir = os.path.dirname(os.path.abspath(__file__))

def get_shopify_setting_by_marketplace(marketplace: str) -> str | None:
    """
    Returns the Shopify Integration Settings doc name for a given marketplace.
    """
    result = frappe.db.get_value(
        "Shopify Integration Settings",
        {"marketplace": marketplace, "enabled": 1},
        "name"
    )

    if not result:
        frappe.log_error(
            title="get_shopify_setting_by_marketplace | Not Found",
            message=f"No active Shopify Integration Settings for marketplace: {marketplace}"
        )

    return result or None



def fetch_shopify_unfulfilled_orders(setting_doc_name: str, limit: int = 50):
    """
    Fetch unfulfilled orders from Shopify using GraphQL

    Args:
        setting_doc_name (str): Shopify Integration Settings docname
        limit (int): number of orders per page (max 250)

    Returns:
        list: list of order nodes
    """

    try:
        # -------------------- Get Settings --------------------
        setting = frappe.get_doc("Shopify Integration Settings", setting_doc_name)

        shop_url = f"https://{setting.shop_name}.myshopify.com"
        api_version = setting.api_version
        access_token = setting.get_password("access_token")

        # -------------------- Start Session --------------------
        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)

        # -------------------- GraphQL Query --------------------
        query = """
        query GetUnfulfilledOrders($cursor: String, $limit: Int!) {
          orders(
            first: $limit,
            after: $cursor,
            query: "fulfillment_status:unfulfilled"
          ) {
            edges {
              node {
                id
                name
                createdAt
                displayFulfillmentStatus
                lineItems(first: 10) {
                  edges {
                    node {
                      title
                      quantity
                    }
                  }
                }
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """

        all_orders = []
        cursor = None

        # -------------------- Pagination Loop --------------------
        while True:
            variables = {
                "limit": min(limit, 250),
                "cursor": cursor
            }

            response = shopify.GraphQL().execute(
                query=query,
                variables=variables,
                operation_name="GetUnfulfilledOrders"
            )

            data = json.loads(response)

            orders = data.get("data", {}).get("orders", {})
            edges = orders.get("edges", [])

            # Extract nodes
            for edge in edges:
                node = edge.get("node")
                if node:
                    all_orders.append(node)

            # Pagination info
            page_info = orders.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break

            cursor = page_info.get("endCursor")

        # -------------------- Clear Session --------------------
        shopify.ShopifyResource.clear_session()

        # -------------------- Debug Log --------------------
        frappe.log_error(
            title="Shopify Unfulfilled Orders",
            message=f"Total Orders Fetched: {len(all_orders)}\n{all_orders[:2]}"
        )

        return all_orders

    except Exception:
        frappe.log_error(
            title="Shopify Fetch Error",
            message=frappe.get_traceback()
        )
        return []

def fetch_tracking_simple(sales_order: str):

    print(f"\n🚀 START: {sales_order}")

    base_url = frappe.get_value("Extensiv Settings", {"enabled": 1}, "base_url")
    params = frappe.get_value("Extensiv Settings", {"enabled": 1}, "params")

    # ── Get warehouses ──
    settings_name = frappe.get_value("Extensiv Settings", {"enabled": 1}, "name")
    warehouses = [
        w["warehouse"] for w in frappe.get_all(
            "Extensive Facility Settings",
            {"parent": settings_name},
            ["warehouse"],
            order_by="idx asc"
        )
    ]

    so_doc = frappe.get_doc("Sales Order", sales_order)

    result = {
        "sales_order": sales_order,
        "shopify_order_gid": frappe.get_value(
            "Sales Order", sales_order, "custom_shopify_order_id_number"
        ),
        "items": {}
    }

    # ─────────────────────────────
    # LOOP EACH ITEM (MAIN LOGIC)
    # ─────────────────────────────
    for item in so_doc.items:

        item_code = item.item_code
        eoin = item.get("custom_extensiv_order_item_id")

        print(f"\n🔍 ITEM: {item_code} | EOIN: {eoin}")

        tracking_found = False

        # ── STEP 1: NORMAL WAREHOUSE CHECK ──
        for wh in warehouses:
            try:
                facilityIdentifier, _ = get_facility_and_customer_id(wh)
                token = get_token(facilityIdentifier)

                url = base_url + f"orders/{eoin}?{params}"

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }

                res = requests.get(url, headers=headers)

                if res.status_code not in [200, 201]:
                    continue

                data = res.json()

                routing = data.get("RoutingInfo") or data.get("routingInfo") or {}
                tracking = routing.get("TrackingNumber") or routing.get("trackingNumber")
                carrier = routing.get("Carrier") or routing.get("carrier") or "FedEx"

                tracking_list = [
                    t.strip() for t in str(tracking).split("\n") if t.strip()
                ] if tracking else []

                # ✅ ONLY accept if tracking exists
                if tracking_list:
                    print(f"✅ Found in {wh}: {tracking_list}")

                    result["items"][item_code] = {
                        "tracking_numbers": tracking_list,
                        "carrier": carrier,
                        "warehouse": wh
                    }

                    tracking_found = True
                    break

                else:
                    print(f"⚠️ Found in {wh} but NO tracking")

            except Exception:
                continue

        # ── STEP 2: RQL FALLBACK ──
        if not tracking_found:
            print(f"🔁 RQL fallback for {item_code}")

            reference_num = so_doc.get("marketplace_order_id")

            for wh in warehouses:
                try:
                    facilityIdentifier, _ = get_facility_and_customer_id(wh)
                    token = get_token(facilityIdentifier)

                    encoded_ref = reference_num.replace("#", "%23")
                    rql_url = base_url + f"orders?detail=all&itemdetail=all&rql=referenceNum=={encoded_ref}*"

                    headers = {
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/hal+json",
                    }

                    res = requests.get(rql_url, headers=headers)

                    if res.status_code not in [200, 201]:
                        continue

                    data = res.json()
                    embedded_key = "http://api.3plCentral.com/rels/orders/order"
                    orders = data.get("_embedded", {}).get(embedded_key, [])

                    for order_data in orders:

                        rql_eoin = order_data.get("readOnly", {}).get("orderId")

                        if not rql_eoin:
                            continue

                        # 🔥 DIRECTLY FETCH FULL ORDER (DON'T TRUST RQL ITEM STRUCTURE)
                        url = base_url + f"orders/{rql_eoin}?{params}"

                        headers_full = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        }

                        res2 = requests.get(url, headers=headers_full)

                        if res2.status_code not in [200, 201]:
                            continue

                        full_data = res2.json()

                        # 🔥 Extract SKUs from FULL ORDER (CORRECT SOURCE)
                        full_items = (
                            full_data.get("OrderItems")
                            or full_data.get("orderItems")
                            or []
                        )

                        full_skus = set()
                        for i in full_items:
                            sku = (
                                i.get("ItemIdentifier", {}).get("Sku")
                                or i.get("itemIdentifier", {}).get("sku")
                                or i.get("sku")
                            )
                            if sku:
                                full_skus.add(sku)

                        print(f"📦 FULL ORDER SKUs ({rql_eoin}): {full_skus}")

                        # ✅ MATCH ITEM CORRECTLY
                        if item_code not in full_skus:
                            continue

                        print(f"✅ RQL MATCH FOUND: {item_code} → {rql_eoin} ({wh})")

                        # ── Extract tracking ──
                        routing = full_data.get("RoutingInfo") or full_data.get("routingInfo") or {}
                        tracking = routing.get("TrackingNumber") or routing.get("trackingNumber")
                        carrier = routing.get("Carrier") or routing.get("carrier") or "FedEx"

                        tracking_list = [
                            t.strip() for t in str(tracking).split("\n") if t.strip()
                        ] if tracking else []

                        if tracking_list:
                            result["items"][item_code] = {
                                "tracking_numbers": tracking_list,
                                "carrier": carrier,
                                "warehouse": wh
                            }

                            tracking_found = True
                            break

                except Exception:
                    continue

            if not tracking_found:
                print(f"❌ No tracking found for {item_code}")
                result["items"][item_code] = {
                    "tracking_numbers": [],
                    "carrier": None,
                    "warehouse": None
                }

    print(f"\n🎯 FINAL RESULT:\n{result}\n")

    return result
   

def fulfill_shopify_from_tracking_json(tracking_json: dict, tracking_baseurl: str = None) -> dict:
    """
    Fulfills a Shopify order using tracking data from fetch_tracking_simple().
    No dependency on Delivery Note.
    """
    from collections import defaultdict

    sales_order      = tracking_json.get("sales_order")
    shopify_order_id = tracking_json.get("shopify_order_gid")
    items            = tracking_json.get("items", {})

    # ── Validate inputs ──
    if not sales_order or not shopify_order_id or not items:
        frappe.log_error(
            title="fulfill_shopify_from_tracking_json | Missing Input",
            message=f"tracking_json received: {tracking_json}"
        )
        return {}

    # ── Step 1: Filter items that actually have tracking numbers ──
    eligible_items = {
        item_code: info
        for item_code, info in items.items()
        if info.get("tracking_numbers")
    }

    if not eligible_items:
        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | No Eligible Items | {sales_order}",
            message=f"All items missing tracking numbers. Raw items: {items}"
        )
        return {}

    frappe.log_error(
        title=f"fulfill_shopify_from_tracking_json | Eligible Items | {sales_order}",
        message=str(eligible_items)
    )

    # ── Step 2: Build item_code → shopify_line_item_id from ERPNext Sales Order ──
    so_doc = frappe.get_doc("Sales Order", sales_order)

    item_code_to_shopify_lid = {}

    for row in so_doc.items:
        lid = row.get("custom_shopify_line_item_id")  # ← confirm this field name
        if lid and row.item_code in eligible_items:
            item_code_to_shopify_lid[row.item_code] = lid

    frappe.log_error(
        title=f"fulfill_shopify_from_tracking_json | SO Line Item Map | {sales_order}",
        message=str(item_code_to_shopify_lid)
    )

    if not item_code_to_shopify_lid:
        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | No LIDs Found | {sales_order}",
            message=f"No shopify_line_item_id found on SO items for eligible: {list(eligible_items.keys())}"
        )
        return {}

    # ── Step 3: Set up Shopify session ──
    marketplace = frappe.db.get_value("Sales Order", sales_order, "marketplace")
    if not marketplace:
        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | No Marketplace | {sales_order}",
            message="Sales Order has no marketplace field set."
        )
        return {}

    setting_doc_name = get_shopify_setting_by_marketplace(marketplace)
    if not setting_doc_name:
        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | No Shopify Settings | {sales_order}",
            message=f"No Shopify Integration Settings found for marketplace: {marketplace}"
        )
        return {}

    setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
    shop_url    = f"https://@{setting_doc.shop_name}.myshopify.com"
    session     = shopify.Session(shop_url, setting_doc.api_version, setting_doc.get_password("access_token"))
    shopify.ShopifyResource.activate_session(session)

    # ── Step 4: Read GraphQL files ──
    query_file_path = os.path.join(current_dir, "selling_query.graphql")
    with open(query_file_path, "r") as f:
        fulfillment_id_query = f.read()

    mutation_file_path = os.path.join(current_dir, "mutation.graphql")
    with open(mutation_file_path, "r") as f:
        fulfillment_mutation = f.read()

    # ── Step 5: Fetch fulfillment orders from Shopify ──
    raw_response = shopify.GraphQL().execute(
        query=fulfillment_id_query,
        operation_name="getOrderFulfillment",
        variables={"orderId": shopify_order_id},
    )
    fulfillment_id_json = json.loads(raw_response)

    frappe.log_error(
        title=f"fulfill_shopify_from_tracking_json | Shopify FO Response | {sales_order}",
        message=str(fulfillment_id_json)
    )

    if "data" not in fulfillment_id_json:
        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | FO Fetch Failed | {sales_order}",
            message="No 'data' key in Shopify response."
        )
        shopify.ShopifyResource.clear_session()
        return {}

    fulfillment_orders = fulfillment_id_json["data"]["order"]["fulfillmentOrders"]["edges"]

    # ── Step 6: Build shopify_lid → fo_line_item_id map from Shopify response ──
    shopify_lid_to_fo_lid = {}   # order line item GID → fulfillment order line item GID
    fo_lid_to_fo_id       = {}   # fulfillment order line item GID → fulfillment order GID

    for fo_edge in fulfillment_orders:
        fo_node = fo_edge["node"]
        fo_id   = fo_node["id"]
        for li_edge in fo_node["lineItems"]["edges"]:
            li_node         = li_edge["node"]
            fo_line_item_id = li_node["id"]
            shopify_lid     = li_node["lineItem"]["id"]

            shopify_lid_to_fo_lid[shopify_lid] = fo_line_item_id
            fo_lid_to_fo_id[fo_line_item_id]   = fo_id

    frappe.log_error(
        title=f"fulfill_shopify_from_tracking_json | FO LID Map | {sales_order}",
        message=str(shopify_lid_to_fo_lid)
    )

    # ── Step 7: Match eligible items → fulfillment order line items ──
    matched = {}  # shopify_lid → { fo_line_item_id, fo_id, tracking }

    for item_code, info in eligible_items.items():
        shopify_lid = item_code_to_shopify_lid.get(item_code)

        if not shopify_lid:
            frappe.log_error(
                title=f"fulfill_shopify_from_tracking_json | LID Missing | {sales_order}",
                message=f"item_code '{item_code}' has no shopify_line_item_id in Sales Order"
            )
            continue

        fo_lid = shopify_lid_to_fo_lid.get(shopify_lid)
        if not fo_lid:
            frappe.log_error(
                title=f"fulfill_shopify_from_tracking_json | FO LID Missing | {sales_order}",
                message=f"shopify_lid '{shopify_lid}' not found in Shopify fulfillment orders"
            )
            continue

        fo_id = fo_lid_to_fo_id.get(fo_lid)

        matched[shopify_lid] = {
            "fo_line_item_id": fo_lid,
            "fo_id":           fo_id,
            "tracking": {
                "carrier":          info.get("carrier", ""),
                "tracking_numbers": info.get("tracking_numbers", [])
            }
        }

        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | Matched | {sales_order}",
            message=f"item_code: {item_code} | shopify_lid: {shopify_lid} | fo_lid: {fo_lid} | fo_id: {fo_id}"
        )

    if not matched:
        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | Nothing Matched | {sales_order}",
            message=f"eligible_items: {eligible_items} | shopify_lid_to_fo_lid: {shopify_lid_to_fo_lid}"
        )
        shopify.ShopifyResource.clear_session()
        return {}

    # ── Step 8: Group by tracking fingerprint ──
    # Each unique (carrier + tracking_numbers) = one fulfillmentCreateV2 call
    tracking_groups = defaultdict(lambda: defaultdict(list))
    # tracking_key → { fo_id → [fo_line_item_ids] }

    for shopify_lid, data in matched.items():
        tracking_key = (
            data["tracking"].get("carrier") or "",
            tuple(data["tracking"].get("tracking_numbers") or [])
        )
        fo_id  = data["fo_id"]
        fo_lid = data["fo_line_item_id"]

        tracking_groups[tracking_key][fo_id].append(fo_lid)

    frappe.log_error(
        title=f"fulfill_shopify_from_tracking_json | Tracking Groups | {sales_order}",
        message=str({str(k): dict(v) for k, v in tracking_groups.items()})
    )

    # ── Step 9: Fire one mutation per tracking group ──
    responses   = []
    any_success = False

    for tracking_key, fo_id_map in tracking_groups.items():
        carrier, tracking_numbers = tracking_key
        tracking_numbers          = list(tracking_numbers)

        line_items_by_fo = [
            {
                "fulfillmentOrderId": fo_id,
                "fulfillmentOrderLineItems": [
                    {"id": fo_lid, "quantity": 1}
                    for fo_lid in fo_lid_list
                ]
            }
            for fo_id, fo_lid_list in fo_id_map.items()
        ]

        tracking_info = {"company": carrier or ""}
        if tracking_numbers:
            tracking_info["numbers"] = tracking_numbers
            if tracking_baseurl:
                tracking_info["url"] = f"{tracking_baseurl}{tracking_numbers[0]}"

        variables = {
            "fulfillment": {
                "lineItemsByFulfillmentOrder": line_items_by_fo,
                "notifyCustomer":              False,
                "trackingInfo":                tracking_info,
            }
        }

        frappe.log_error(
            title=f"fulfill_shopify_from_tracking_json | Mutation Payload | {sales_order}",
            message=f"Tracking: {tracking_key}\nPayload: {variables}"
        )

        try:
            raw  = shopify.GraphQL().execute(
                query=fulfillment_mutation,
                operation_name="fulfillmentCreateV2",
                variables=variables,
            )
            resp = json.loads(raw)
            responses.append(resp)

            if is_fulfillment_successful(resp):
                any_success = True
                frappe.log_error(
                    title=f"fulfill_shopify_from_tracking_json | Success | {sales_order}",
                    message=str(resp)
                )
            else:
                frappe.log_error(
                    title=f"fulfill_shopify_from_tracking_json | API Error | {sales_order}",
                    message=str(resp)
                )

        except Exception:
            frappe.log_error(
                title=f"fulfill_shopify_from_tracking_json | Exception | {sales_order}",
                message=frappe.get_traceback()
            )

    shopify.ShopifyResource.clear_session()

    frappe.log_error(
        title=f"fulfill_shopify_from_tracking_json | Done | {sales_order}",
        message=f"any_success={any_success} | total_responses={len(responses)}"
    )

    return responses[-1] if responses else {}