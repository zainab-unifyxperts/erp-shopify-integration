import shopify
import json
import datetime
from datetime import *
import frappe
from shopify_integration.shopify_selling.orders import *
from shopify_integration.shopify_selling.shopify_selling_utils import *

# from frappe.utils.background_jobs import enqueue
# from frappe.utils import add_to_date
import frappe.utils
import os


@frappe.whitelist(allow_guest=True)
def shopify_order_edit() -> None:
    """
    This is the endpoint where the order edit webhook pings to.
    Recieves and authenticates payload, then cancels old sales order and
    creates a new one with the updated infomration

    Parameters:
        None

    Retuns:
        None
    """
    # Load and Authenticate webhook payload
    if frappe.get_value("Shopify Integration Settings",{"enabled":1},"order_edit_webhook") == 1:
        client_secret = frappe.get_value(
            "Shopify Integration Settings", {"enabled": 1}, "api_secret"
        )
        hmac_header = frappe.request.headers.get("X-Shopify-Hmac-SHA256")
        data = frappe.request.get_data()
        shopify_webhook_id = frappe.request.headers.get("X-Shopify-Webhook-Id")
        verify_webhook_source = verify_webhook(data, hmac_header, client_secret)
        if verify_webhook_source:
            # frappe.set_user("fafadiatech@example.com")
            frappe.log_error(
                title="Webhook Verified",
                message="Webhook verified",
            )
        else:
            raise frappe.AuthenticationError

        order_data = json.loads(data.decode("utf-8"))
        frappe.log_error(
            title="Webhook Payload and header",
            message=f"Webhook_id:{shopify_webhook_id}\n\nPayload:\n{order_data}",
        )
        # Check for cancelled items and remove them from line items
        updated_line_items = remove_zero_quantity_items(order_data["line_items"])

        order_data["line_items"] = updated_line_items
        setting_doc_name = frappe.get_value(
            "Shopify Integration Settings", {"enabled": 1}, "name"
        )

        # Check if the webhook has already been processed
        if not frappe.db.exists(
            "Sales Order", {"custom_shopify_webhook_id": shopify_webhook_id}
        ):

            # check if the shopify order exists and get the one that is not cancelled
            marketplace_order_id = order_data["name"]
            sales_order_name = frappe.db.get_value(
                "Sales Order",
                {
                    "marketplace_order_id": marketplace_order_id,
                    "docstatus": ["!=", "2"],
                },
                "name",
            )
            # Check if the order is synced to extensiv
            if sales_order_name:
                if frappe.db.get_value(
                    "Sales Order",
                    sales_order_name,
                    "custom_extensiv_order_number",
                ):
                    frappe.log_error(
                        title="Order already synced to Extensiv",
                        message=f"Order {sales_order_name} cannot be updated since its already been synced to extensiv",
                    )
                    return
                else:
                    old_sales_order = frappe.get_doc("Sales Order", sales_order_name)
                    old_sales_order.cancel()
                    frappe.db.commit()
                    frappe.log_error(
                        title="Sales Order cancelled",
                        message=f"Sales order {sales_order_name} cancelled",
                    )
                    try:
                        update_shopify_sales_order(
                            order_data,
                            setting_doc_name,
                            sales_order_name,
                            shopify_webhook_id,
                        )
                        frappe.log_error(
                            title="SO amended", message=f"successfully edited order"
                        )
                    except:
                        frappe.log_error(
                            title="Sales Order not amended",
                            message=f"Traceback:{frappe.get_traceback()} Error: Sales Order not amended",
                        )
            else:
                frappe.log_error(
                    title="SO not found",
                    message=f"Traceback:{frappe.get_traceback()} Error:No so found for marketplace_order_id:{marketplace_order_id}",
                )
        else:
            frappe.log_error(
                title="Webhook Processed", message="Shopify Webhook Already Processed"
            )


@frappe.whitelist(allow_guest=True)
def shopify_fulfillment_create() -> None:
    """
    Endpoint to which fulfillment webhook hits. 

    Parameters:
        None

    Retuns:
        None
    """
    # Load and Authenticate webhook payload
    client_secret = frappe.get_value(
        "Shopify Integration Settings", {"enabled": 1}, "api_secret"
    )
    hmac_header = frappe.request.headers.get("X-Shopify-Hmac-SHA256")
    data = frappe.request.get_data()
    shopify_webhook_id = frappe.request.headers.get("X-Shopify-Webhook-Id")
    verify_webhook_source = verify_webhook(data, hmac_header, client_secret)
    if verify_webhook_source:
        # frappe.set_user("fafadiatech@example.com")
        frappe.log_error(
            title="Webhook Verified",
            message="Webhook verified and user set to fafadiatech",
        )
    else:
        raise frappe.AuthenticationError

    order_data = json.loads(data.decode("utf-8"))
    frappe.log_error(
        title="Webhook Payload and header Fulfillment",
        message=f"Webhook_id:{shopify_webhook_id}\n\nPayload:\n{order_data}",
    )
