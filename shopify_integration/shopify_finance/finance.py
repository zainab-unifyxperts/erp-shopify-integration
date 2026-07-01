import frappe
import datetime
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe.utils import convert_utc_to_system_timezone
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

def sync_payment_entries(data:list,sales_order_name:str, setting_doc_name: str = None):
    """
    """
    customer = frappe.get_value("Sales Order", sales_order_name, "customer")
    company = frappe.get_value("Sales Order", sales_order_name, "company")
    for transaction in data:
        payment_id = transaction["paymentId"]
        
        if frappe.db.exists(
            "Payment Entry",
            {
                "reference_no": payment_id,
                "party": customer,
                "company": company,
                "docstatus": ["=", 1]
            }
        ):
            continue

        if frappe.db.exists("Sales Invoice", {
            "sales_order": sales_order_name,
            "docstatus": 1
        }):
            continue
    
        create_shopify_payment_entry(transaction,sales_order_name, setting_doc_name)
        

def create_shopify_payment_entry(data,sales_order_name, setting_doc_name):
    """
        This function creates payment Entry for each transaction data

        Params:
            data = "transactions" data from Shopify Orders API
            sales_order_name(str) = Name of the Sales Order
            setting_doc = Shopify Integration Settings Doc name
    """
    try:
        
        payment_entry_doc = get_payment_entry(dt = "Sales Order",dn = sales_order_name)
        payment_entry_doc.paid_amount = float(data["amountSet"]["shopMoney"]["amount"])
        if data['gateway'] == "":
            payment_entry_doc.paid_to = frappe.get_value("Shopify Integration Settings",{ "name":setting_doc_name, "enabled":1},"default_payment_account")
        elif frappe.db.exists("Shopify Payment Account Table",{"gateway":data["gateway"]}):
            payment_entry_doc.paid_to = frappe.get_value("Shopify Payment Account Table",{"gateway":data["gateway"]},"account")
        else:
            payment_entry_doc.paid_to = frappe.get_value("Shopify Integration Settings",{ "name":setting_doc_name, "enabled":1},"default_payment_account")
            frappe.log_error(title="Payment not exists",message=f"Payment account for {data['gateway']} does not exists")
        payment_entry_doc.reference_no = data["paymentId"]
        date_str = data["createdAt"]
        date = datetime.datetime.strptime(date_str,"%Y-%m-%dT%H:%M:%SZ")
        payment_entry_doc.cost_center = frappe.get_value("Shopify Integration Settings",{ "name":setting_doc_name, "enabled":1},"default_cost_center")
        payment_entry_doc.reference_date = convert_utc_to_system_timezone(date).date()
        payment_entry_doc.save()
        payment_entry_doc.submit()
        frappe.db.commit()
    except:
        frappe.log_error(title="Shopify payment entry creation error",message=f"Payment ID = {data['paymentId']}\n Error: \n{frappe.get_traceback()}")

@frappe.whitelist()
def enqueue_create_sales_invoice(doc,method =None):
    frappe.enqueue(
        method="shopify_integration.shopify_finance.finance.create_sales_invoice",
        queue="short",
        doc=doc,
        function=method,
    )

@frappe.whitelist()
def create_sales_invoice(doc,function):
    delivery_note_doc_name = doc.name
    try:
        frappe.log_error(title="Sales Invoice",message=delivery_note_doc_name)
        sales_invoice_doc = make_sales_invoice(delivery_note_doc_name,None)
        sales_invoice_doc.save()
        sales_invoice_doc.submit()
        frappe.db.commit()
    except:
        frappe.log_error(title="Sales Invoice Creation Error",message =f"Sales Invoice for Delivery Note {delivery_note_doc_name} not created \n\n Traceback: \n {frappe.get_traceback()}")