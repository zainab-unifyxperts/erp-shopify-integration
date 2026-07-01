import shopify
import json
import datetime
from datetime import *
import frappe
from shopify_integration.shopify_selling.shopify_selling_utils import *
from frappe.utils.background_jobs import enqueue
from frappe.utils import add_to_date, add_months, now_datetime, get_datetime
import os
from extensiv_integration.extensiv_setup.utils import *
from shopify_integration.shopify_finance.finance import *
from erpnext.stock.doctype.packed_item.packed_item import get_product_bundle_items

from .utils import (
    extract_vat_info,
    build_item_rows_from_shopify,
    build_shipping_actual_row,
    build_vat_tax_rows,
    build_non_inclusive_tax_rows,
    build_duties_row,
    get_shopify_discount_details,
    _get_money_amount,
    _get_money_currency,
)

# Get current directory and the append file name at the end
current_dir = os.path.dirname(os.path.abspath(__file__))
query_dir = os.path.join(current_dir, "selling_query.graphql")
with open(query_dir, "r") as file:
    query = file.read()


def shopify_order_sync_job() -> None:
    """
    This function gets called in the cron job and syncs sales order for all the Shopify Integration Settings
    """
    for doc in frappe.get_list("Shopify Integration Settings", {"enabled": 1}):
        enqueue_shopify_sync_orders(doc.name, use_setting_date=True)


@frappe.whitelist()
def enqueue_shopify_sync_orders(doc: str, use_setting_date: bool) -> None:
    """
    Creates a background job for a setting. This also selects start date based on the use_setting_date flag.
    if use_setting_flag is True:
        gets date from Shopify Integration Settings from field "Order Syncing Start Date"
    if use_setting_flag is False:
        date is set as today's date minus number of days set in field "Order Sync Duration"

    Params:
        doc(str) -> Shopify Integration Settings Doc name
        use_setting_doc -> True/False
    """
    try:
        if use_setting_date == True:
            enqueue(
                "shopify_integration.shopify_selling.orders.sync_shopify_orders",
                setting_doc_name=doc,
                start_date=frappe.get_value(
                    "Shopify Integration Settings", doc, "order_syncing_start_date"
                ),
                timeout=1800,
            )
        else:
            start_date = frappe.utils.add_to_date(
                frappe.utils.getdate(),
                days=-(
                    frappe.get_value(
                        "Shopify Integration Settings", doc, "order_sync_duration"
                    )
                ),
            )
            enqueue(
                "shopify_integration.shopify_selling.orders.sync_shopify_orders",
                setting_doc_name=doc,
                start_date=start_date,
                timeout=1800,
            )
    except:
        frappe.log_error(
            title="Enqueuing Error", message="Cannot Enqueue sync_shopify_orders"
        )


def sync_shopify_orders(setting_doc_name: str, start_date: str) -> None:
    """
    Fetches all the orders from shopify after the start_date and creates sales order in erpnext

    Parameters:
    setting_doc_name(str) -> Setting doc stores all the necessary information in ERPNext
    start_date(str) -> Date from which you want to sync sales orders

    Return:
    None
    """
    try:
        setting_doc = frappe.get_doc("Shopify Integration Settings", setting_doc_name)
        # Get necessary information from setting doc
        shop_name = setting_doc.shop_name
        shop_url = f"https://@{shop_name}.myshopify.com"
        api_version = setting_doc.api_version
        access_token = setting_doc.get_password("access_token")
        start_date = f"(created_at:>{start_date}) AND "
        non_cancelled = "(-status:CANCELLED)"
        order_query = start_date + non_cancelled
        session = shopify.Session(shop_url, api_version, access_token)
        shopify.ShopifyResource.activate_session(session)
        res = shopify.GraphQL().execute(
            query=query,
            variables={"nos": 250, "order_query": order_query},
            operation_name="GetOrdersInfo",
        )
        response = json.loads(res)
        # frappe.log_error(title="Shopify Payload fulfillment", message=response)
        order_data = response["data"]["orders"]["edges"]
        # Look for orders in subsequent pages
        while response["data"]["orders"]["pageInfo"]["hasNextPage"] == True:
            res = shopify.GraphQL().execute(
                query=query,
                variables={
                    "nos": 250,
                    "order_query": order_query,
                    "after": response["data"]["orders"]["pageInfo"]["endCursor"],
                },
                operation_name="GetOrdersInfo",
            )
            response = json.loads(res)
            order_data.extend(response["data"]["orders"]["edges"])
        shopify.ShopifyResource.clear_session()
        for data in order_data:
            try:
                create_shopify_sales_order(data["node"], setting_doc_name, is_return=False)
            except Exception:
                frappe.log_error(
                    title="Sales Order Creation Error",
                    message=f"Traceback:\n\n{frappe.get_traceback()}\n\n Error:Shopify Order Sync Error \n\n Payload:\n{data}",
                )
    except:
        frappe.log_error(
            title="Shopify API error",
            message=f"Traceback{frappe.get_traceback()}\n\nError:Shopify Order API Call Error",
        )


def create_shopify_sales_order(data: dict, setting_doc: str, is_return:bool) -> None:
    """
    Creates a Sales Order from Shopify order data with proper discounts, VAT,
    duties, shipping, and marketplace mapping.
    """

    # ---------- Code for excluding orders with fulfillment status eg. `ON_HOLD`. -------------
    order_status = data.get("displayFulfillmentStatus") or ""
    if is_return is False:
        if get_fulfillment_status_to_exclude(
        shopify_setting_doc=frappe.get_doc("Shopify Integration Settings", setting_doc),
        order_fullfilment_status=order_status,
        ):
            frappe.log_error(
                title=f"Status found in {data['name']}",
                message=f"Status to exclude found {order_status}",
            )
            return
    # -------------------- Marketplace & Order ID --------------------
    marketplace = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "marketplace"
    )
    marketplace_order_id = get_shopify_mo_id(data["name"], setting_doc)

    # Skip if SO already exists and create payment entry if not exists.
    if frappe.db.exists(
        "Sales Order",
        {"marketplace_order_id": data["name"], "marketplace": marketplace},
    ):
        sales_order = frappe.get_value(
            "Sales Order",
            {"marketplace_order_id": data["name"], "marketplace": marketplace},
            ["name", "creation"],
            as_dict=True,
        )

        if sales_order:
            # check if older than 1 month
            if sales_order.creation < add_months(now_datetime(), -1):
                # print("Sales order is older than 1 month")
                return

            try:
                sync_payment_entries(
                    data.get("transactions", []), sales_order.name, setting_doc
                )
                # print(f"creating payment entry")
            except Exception:
                frappe.log_error(
                    title="Payment Entry creation error which are not older than 1 month",
                    message=f"Payment Entry for SO {sales_order.name} not created\nTraceback: {frappe.get_traceback()}",
                )
        return

    new_sales_order = frappe.new_doc("Sales Order")
    new_sales_order.marketplace = marketplace
    new_sales_order.marketplace_order_id = marketplace_order_id

    # ------------------- Tags From Shopify ---------------------------- (B2B)
    incoterm = None
    tags = data.get("tags", [])
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue

        incoterm = frappe.get_value("Incoterm", {"code": tag}, "name")

        if incoterm:
            break

    if incoterm:
        new_sales_order.incoterm = incoterm

    # -------------------- Dates --------------------
    date_str = data.get("createdAt", "").split("T")[0]
    transaction_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    new_sales_order.transaction_date = transaction_date
    new_sales_order.delivery_date = add_to_date(transaction_date, days=3)

    # -------------------- Currency --------------------
    # 🧠 Robust currency extraction for paid and unpaid orders
    shopify_currency = data.get("presentmentCurrencyCode")

    if not shopify_currency:
        transactions = data.get("transactions") or []

        if transactions and isinstance(transactions, list) and len(transactions) > 0:
            txn_currency = _get_money_currency(transactions[0].get("amountset"))
            if txn_currency:
                shopify_currency = txn_currency

    # # Final fallback to company default
    if not shopify_currency:
        shopify_currency = frappe.get_value(
            "Company",
            frappe.get_value("Shopify Integration Settings", setting_doc, "company"),
            "default_currency",
        )

    new_sales_order.currency = shopify_currency
    
    # -------------------- Customer Info --------------------
    customer_details = data.get("customer")
    new_sales_order.customer = get_shopify_customer(customer_details, setting_doc)
    new_sales_order.contact_email = customer_details.get("email") or ""

    shipping_details = data.get("shippingAddress")
    new_sales_order.custom_shipping_phone = shipping_details.get("phone") or ""

    billing_details = data.get("billingAddress")
    new_sales_order.contact_phone = billing_details.get("phone") or ""

    new_sales_order.contact_person = frappe.get_value(
        "Customer", new_sales_order.customer, "customer_primary_contact"
    )
    new_sales_order.company = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "company"
    )
    new_sales_order.naming_series = frappe.get_value(
        "Marketplace", marketplace, "naming_series"
    )

    # ---------- Sales type Identification ----------------------
    profiles_list = data.get("customer", {}).get("companyContactProfiles", [])

    company_info = None
    if profiles_list:
        # optionally pick the main contact
        main_contact = next(
            (p for p in profiles_list if p.get("isMainContact")), profiles_list[0]
        )
        company_info = main_contact.get("company")

    if company_info:
        company_id = company_info.get("id")
        company_name = company_info.get("name")
        # Storing customer company
        if company_name:
            new_sales_order.custom_customer_company = company_name
    else:
        company_id = None
        company_name = None

    if company_id is not None and company_name is not None:
        new_sales_order.custom_sales_type = "B2B"
    else:
        new_sales_order.custom_sales_type = "B2C"

    # -------------------- Sales Order Fields --------------------
    new_sales_order.po_no = data.get("poNumber")
    new_sales_order.selling_price_list = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "price_list"
    )
    new_sales_order.ignore_pricing_rule = 1
    new_sales_order.custom_fully_paid = data.get("fullyPaid")
    new_sales_order.custom_notes_for_extensiv = data.get("note")
    new_sales_order.custom_shopify_order_id_number = data.get("id")
    new_sales_order.custom_shopify_discount_codes = ",".join(
        data.get("discountCodes", [])
    )
    coupon_code = ""
    discount_reason = ""
    for edge in data.get("discountApplications", {}).get("edges", []):
        node = edge.get("node", {})
        if node.get("__typename") == "DiscountCodeApplication":
            coupon_code = node.get("code") or coupon_code
        elif node.get("__typename") == "ManualDiscountApplication":
            discount_reason = (
                node.get("description")
                or node.get("title")
                or ""
            )
    if coupon_code:
        new_sales_order.coupon_code = coupon_code

    if discount_reason:
        new_sales_order.custom_discount_reason = discount_reason

    new_sales_order.custom_do_not_apply_drop_ship_fee = 1
    new_sales_order.custom_do_not_apply_freight_rates = 1

    # -------------------- Taxes & Items --------------------
    taxes_included = data.get("taxesIncluded", False)
    vat_rate, vat_total_amount, tax_lines = extract_vat_info(data)

    # -------------------- Discount Logic (use helper) --------------------
    # helper expects vat_rate in percent (e.g., 21), so multiply by 100
    apply_on, discount_amount = get_shopify_discount_details(
        data, taxes_included, vat_rate * 100
    )

    if discount_amount and discount_amount > 0:
        new_sales_order.discount_amount = discount_amount
        if apply_on:
            new_sales_order.apply_discount_on = apply_on
    else:
        new_sales_order.discount_amount = 0.0


    # Build item rows using discounted price
    item_rows = build_item_rows_from_shopify(
        data, setting_doc, taxes_included, vat_rate
    )
    # new_sales_order.total_net_weight = new_sales_order.total_net_weight or 0
    for ir in item_rows:
        ir["dont_recompute_tax"] = 1
        # new_sales_order.total_net_weight += ir.get("total_weight", 0)
        item_code = ir.get("item_code")

        if frappe.db.exists("Product Bundle", {"new_item_code": item_code}):
            bundle_items = get_product_bundle_items(item_code)

            if bundle_items:
                ordered_qty = ir.get("qty", 1)
                shopify_line_item_id = ir.get("custom_shopify_line_item_id")

                # Shopify rate for the bundle
                shopify_rate = ir.get("rate") or 0.0

                # Fetch bundle doc to get custom_bundle_price_ per child
                bundle_doc = frappe.get_doc("Product Bundle", {"new_item_code": item_code})

                # Sum of all custom_bundle_price_ as denominator
                total_bundle_price = sum(
                    child.custom_bundle_price_ or 0.0
                    for child in bundle_doc.items
                )

                # Build price map — item_code -> proportional share of shopify rate
                bundle_price_map = {}
                for bundle_child in bundle_doc.items:
                    if total_bundle_price > 0:
                        percentage = (bundle_child.custom_bundle_price_ or 0.0) / total_bundle_price
                        bundle_price_map[bundle_child.item_code] = round(percentage * shopify_rate, 2)
                    else:
                        bundle_price_map[bundle_child.item_code] = 0.0

                for child in bundle_items:
                    rate = bundle_price_map.get(child.item_code) or 0.0

                    child_row = {
                        "item_code": child.item_code,
                        "item_name": child.item_name,
                        "qty": child.qty * ordered_qty,
                        "uom": child.uom,
                        "description": child.description,
                        "custom_parent_bundle_item_name": bundle_doc.name,
                        "delivery_date": new_sales_order.delivery_date,
                        "dont_recompute_tax": 1,
                        "custom_shopify_line_item_id": shopify_line_item_id,
                        "rate": rate,           
                        "price_list_rate": rate,
                        "base_rate": rate,
                        "base_price_list_rate": rate,
                    }
                    new_sales_order.append("items", child_row)
            else:
                # Bundle exists but no children — append as-is
                ir["price_list_rate"] = ir.get("rate", 0.0)
                ir["base_price_list_rate"] = ir.get("rate", 0.0)
                ir["base_rate"] = ir.get("rate", 0.0)
                new_sales_order.append("items", ir)

        else:
            # Normal item — append as-is
            ir["price_list_rate"] = ir.get("rate", 0.0)
            ir["base_price_list_rate"] = ir.get("rate", 0.0)
            ir["base_rate"] = ir.get("rate", 0.0)
            new_sales_order.append("items", ir)


    # Taxes table
    new_sales_order.taxes = []

    if taxes_included:
        row_counter = 0

        # Shipping row
        shipping_row = build_shipping_actual_row(
            data,
            setting_doc,
            taxes_included,
            vat_rate,
            marketplace,
            marketplace_order_id,
            currency=shopify_currency,
        )
        if shipping_row:
            row_counter += 1
            shipping_row["idx"] = row_counter
            shipping_row["dont_recompute_tax"] = 1
            new_sales_order.append("taxes", shipping_row)
            base_row_id = row_counter
        else:
            base_row_id = 0

        # -------------------- Duties Row --------------------
        duties_amount = 0.0
        duties_block = data.get("currentTotalDutiesSet") or {}
        duties_amount = _get_money_amount(duties_block)

        if duties_amount > 0:
            duties_row = build_duties_row(
                duties_amount,
                "Duties",
                setting_doc,
                marketplace,
                marketplace_order_id,
                taxes_included=taxes_included,
                vat_rate=vat_rate,
                currency=shopify_currency,
            )
            if duties_row:
                row_counter += 1
                duties_row["idx"] = row_counter
                duties_row["dont_recompute_tax"] = 1
                new_sales_order.append("taxes", duties_row)

        base_row_id = row_counter if row_counter > 0 else 0

        # VAT rows
        vat_rows = build_vat_tax_rows(
            data,
            setting_doc,
            vat_rate,
            tax_lines,
            taxes_included,
            marketplace,
            marketplace_order_id,
            base_row_id=base_row_id,
            currency=shopify_currency,
        )

        for vr in vat_rows:
            vr["dont_recompute_tax"] = 1
            new_sales_order.append("taxes", vr)
    else:
        # Non-inclusive taxes
        non_inclusive_rows = build_non_inclusive_tax_rows(
            data, setting_doc, marketplace_order_id, currency=shopify_currency
        )
        for r in non_inclusive_rows:
            r["dont_recompute_tax"] = 1
            new_sales_order.append("taxes", r)

    # -------------------- Addresses --------------------
    if data.get("billingAddressMatchesShippingAddress", True):
        addr = get_shopify_address(
            data.get("shippingAddress"), new_sales_order.customer
        )
        new_sales_order.customer_address = addr
        new_sales_order.shipping_address_name = addr
    else:
        new_sales_order.customer_address = get_shopify_address(
            data.get("billingAddress"), new_sales_order.customer
        )
        new_sales_order.shipping_address_name = get_shopify_address(
            data.get("shippingAddress"), new_sales_order.customer
        )

    # -------------------- Save & Submit --------------------
    try:
        frappe.log_error(
            title="Shopify Order Payload",
            message=f"Payload:{data}\nSO:{new_sales_order.as_dict()}",
        )
        new_sales_order.save()
        payment_terms = data.get("paymentTerms") or {}
        payment_term_name = payment_terms.get("paymentTermsName")
        payment_schedules = payment_terms.get(
            "paymentSchedules", {}
        ).get("nodes", [])
        if new_sales_order.payment_schedule:
            if (
                payment_term_name
                and frappe.db.exists("Payment Term", payment_term_name)
            ):
                new_sales_order.payment_schedule[0].payment_term = (
                    payment_term_name
                )
            if payment_schedules:
                due_at = payment_schedules[0].get("dueAt")
                if due_at:
                    new_sales_order.payment_schedule[0].due_date = (
                        get_datetime(due_at).date()
                    )

            if data.get("totalWeight"):
                item_weight_lb = round(
                    float(data["totalWeight"]) / 453.59237,
                    2
                )

                new_sales_order.total_net_weight = item_weight_lb

                new_sales_order.payment_schedule[0].description = (
                    f"({item_weight_lb + 1:.2f} lb: "
                    f"Items {item_weight_lb:.2f} lb, "
                    f"Package 1.0 lb)"
                )
        new_sales_order.submit()
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            title="Shopify Field Error",
            message=f"Traceback:{frappe.get_traceback()}\nPayload:{data}\nError:Sales Order Field not properly set",
        )

    # -------------------- Payment Entries --------------------
    if frappe.db.exists("Sales Order", {"marketplace_order_id": data["name"]}):
        sales_order_name = frappe.get_value(
            "Sales Order", {"marketplace_order_id": data["name"]}, "name"
        )
        try:
            sync_payment_entries(
                data.get("transactions", []), sales_order_name, setting_doc
            )
        except Exception:
            frappe.log_error(
                title="Payment Entry creation error",
                message=f"Payment Entry for SO {sales_order_name} not created\nTraceback: {frappe.get_traceback()}",
            )


def update_shopify_sales_order(
    data: dict,
    setting_doc_name: str,
    old_sales_order_name: str,
    shopify_webhook_id: str,
) -> None:
    """
    Creates Sales Order from the data recieved from Shopify

    Parameters:
    data(dict) -> This is the graphql response from shopify
    setting_doc(str) -> A document in erpnext that contains required information of our shopify store
    old_sales_order_name -> Name of the Sales Order being edited
    shopify_webhook_id -> Unique Id for webhooks. Helps to prevent multiple webhook calls

    Returns :
    None
    """
    marketplace = frappe.get_value(
        "Shopify Integration Settings", setting_doc_name, "marketplace"
    )
    if not frappe.db.exists(
        "Sales Order",
        {
            "marketplace_order_id": data["name"],
            "marketplace": marketplace,
            "docstatus": 1,
        },
    ):
        new_sales_order = frappe.new_doc("Sales Order")
        date_str = data["updated_at"].split("T")[0]
        transaction_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        new_sales_order.transaction_date = transaction_date
        new_sales_order.amended_from = old_sales_order_name
        new_sales_order.custom_shopify_webhook_id = shopify_webhook_id
        new_sales_order.custom_shopify_discount_codes = getDiscountCode(
            data["discount_codes"]
        )
        new_sales_order.discount_amount = float(data["current_total_discounts"])
        if data["financial_status"] == "paid":
            new_sales_order.custom_fully_paid = 1
        else:
            new_sales_order.custom_fully_paid = 0
        new_sales_order.delivery_date = add_to_date(transaction_date, days=3)
        new_sales_order.custom_shopify_order_id_number = data["admin_graphql_api_id"]
        new_sales_order.company = frappe.get_value(
            "Shopify Integration Settings", setting_doc_name, "company"
        )
        new_sales_order.customer = get_updated_shopify_customer(
            data["customer"], setting_doc_name
        )
        new_sales_order.marketplace_order_id = get_shopify_mo_id(
            data["name"], setting_doc_name
        )
        new_sales_order.marketplace = marketplace
        # so_state = data["shipping_address"]["province"]
        # if so_state:
        #     so_state = so_state.capitalize()
        #     nearest_warehouse = get_warehouse(so_state)
        # qty_check = True
        # for row in data["line_items"]:
        #     qty_check = qty_check & check_warehouse(row, nearest_warehouse, True)
        # item_warehouse_row = {}
        # if qty_check:
        #     new_sales_order.set_warehouse = nearest_warehouse
        #     frappe.log_error(
        #         title="Warehouse Routing",
        #         message=f"Order Number:{data['name']},Condition 1:{nearest_warehouse}",
        #     )
        # else:
        #     single_warehouse = check_all_warehouses(
        #         data["line_items"], nearest_warehouse, True
        #     )
        #     if single_warehouse:
        #         frappe.log_error(
        #             title="Warehouse Routing",
        #             message=f"Order Number:{data['name']},nearest_warehouse:{nearest_warehouse},Condition 2:{single_warehouse}",
        #         )
        #         new_sales_order.set_warehouse = single_warehouse

        #     #     # Assign different warehouses for items
        #     else:
        #         for item_dict in data["line_items"]:
        #             warehouse_row = set_individual_warehouse(
        #                 item_dict, nearest_warehouse, True
        #             )
        #             item_warehouse_row[item_dict["sku"]] = warehouse_row
        #             frappe.log_error(
        #                 title="Warehouse Routing",
        #                 message=f"Order Number:{data['name']},{item_warehouse_row},Condition 3:{item_warehouse_row}",
        #             )
        for row in data["line_items"]:
            item_row = create_updated_shopify_so_item_row(row, setting_doc_name)
            # if item_warehouse_row:
            #     item_row["warehouse"] = item_warehouse_row.get(row["sku"])
            new_sales_order.append("items", item_row)
        for row in data["tax_lines"]:
            tax_row = create_shopify_so_tax_row(
                row, setting_doc_name, new_sales_order.marketplace_order_id
            )
            new_sales_order.append("taxes", tax_row)
        if data["shipping_lines"]:
            ship_tax_row = create_shopify_so_tax_row(
                data["shipping_lines"],
                setting_doc_name,
                new_sales_order.marketplace_order_id,
            )
            new_sales_order.append("taxes", ship_tax_row)

        new_sales_order.customer_address = get_shopify_address(
            data["billing_address"], new_sales_order.customer
        )
        new_sales_order.shipping_address_name = get_shopify_address(
            data["shipping_address"], new_sales_order.customer
        )
        new_sales_order.contact_person = frappe.get_value(
            "Customer", new_sales_order.customer, "customer_primary_contact"
        )
        try:
            new_sales_order.save()
            new_sales_order.submit()
            frappe.db.commit()
        except:
            frappe.log_error(
                title="Shopify Field Error",
                message=f"Traceback:{frappe.get_traceback()}\n\nError:Sales Order Field not properly set",
            )
