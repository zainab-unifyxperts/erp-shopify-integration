"""
Customer, Contact, and Address mapping: Shopify -> ERPNext.

customers are matched by EMAIL first (unique, reliable),
falling back to display name only if email is missing. The old code matched
on displayName alone, which silently merges two different Shopify customers
that happen to share a name.
"""

import frappe


def get_shopify_customer(customer_data: dict, setting_doc: str) -> str:
    """
    Get or create an ERPNext Customer from Shopify GraphQL customer data.
    Ensures a linked Contact exists and is set as customer_primary_contact.
    """
    email = (customer_data or {}).get("email")
    display_name = (customer_data or {}).get("displayName")

    customer_name = None
    if email:
        customer_name = frappe.db.get_value(
            "Contact Email", {"email_id": email}, "parent"
        )
        if customer_name:
            # parent of Contact Email is the Contact, not the Customer - resolve link
            customer_name = frappe.db.get_value(
                "Dynamic Link",
                {"parent": customer_name, "link_doctype": "Customer"},
                "link_name",
            )

    if not customer_name and display_name:
        customer_name = frappe.db.get_value(
            "Customer", {"customer_name": display_name}, "name"
        )

    if not customer_name:
        customer_doc = frappe.new_doc("Customer")
        customer_doc.customer_name = display_name or email
        customer_doc.customer_type = frappe.get_value(
            "Shopify Integration Settings", setting_doc, "default_customer_type"
        )
        customer_doc.insert(ignore_permissions=True)
        customer_name = customer_doc.name

    contact_name = get_shopify_contact(customer_data, customer_name)

    if not frappe.get_value("Customer", customer_name, "customer_primary_contact"):
        frappe.db.set_value(
            "Customer", customer_name, "customer_primary_contact", contact_name
        )

    return customer_name


def get_shopify_contact(contact_data: dict, customer: str) -> str:
    """
    Creates a Contact doc if the given email is not already present in ERPNext,
    and ensures it is linked to `customer`.
    """
    display_name = contact_data.get("displayName") or "Unknown"
    email = contact_data.get("email")

    if email and frappe.db.exists("Contact Email", {"email_id": email}):
        contact = frappe.get_value("Contact Email", {"email_id": email}, "parent")
        contact_doc = frappe.get_doc("Contact", contact)
        already_linked = any(
            link.link_name == customer and link.link_doctype == "Customer"
            for link in contact_doc.links
        )
        if not already_linked:
            contact_doc.append("links", get_link_row("Customer", customer))
            contact_doc.save(ignore_permissions=True)
        return contact

    contact_doc = frappe.new_doc("Contact")
    contact_doc.first_name = display_name
    if email:
        contact_doc.append("email_ids", {"email_id": email, "is_primary": 1})
    if contact_data.get("phone"):
        contact_doc.append(
            "phone_nos", {"phone": contact_data["phone"], "is_primary_phone": 1}
        )
    contact_doc.append("links", get_link_row("Customer", customer))
    contact_doc.save(ignore_permissions=True)
    return contact_doc.name


def get_shopify_address(address_data: dict, customer: str) -> str | None:
    """
    Creates an Address doc for a given customer if the payload is usable.
    Returns None (and logs) if address data is missing/invalid - callers
    MUST handle the None case rather than blindly assigning it.
    """
    if not address_data or not address_data.get("address1"):
        return None

    try:
        address_doc = frappe.new_doc("Address")
        address_doc.address_title = address_data.get("name") or customer
        address_doc.address_line1 = address_data.get("address1")
        address_doc.address_line2 = address_data.get("address2")
        address_doc.city = address_data.get("city")
        address_doc.country = address_data.get("country")
        address_doc.state = address_data.get("province")
        address_doc.pincode = address_data.get("zip")
        address_doc.append("links", get_link_row("Customer", customer))
        address_doc.save(ignore_permissions=True)
        return address_doc.name
    except Exception:
        frappe.log_error(
            title="Shopify Address Creation Error",
            message=f"Traceback:\n{frappe.get_traceback()}\n\nCustomer: {customer}\nPayload: {address_data}",
        )
        return None


def get_link_row(doc_type: str, link_name: str) -> dict:
    return {"link_doctype": doc_type, "link_name": link_name}


def get_shopify_mo_id(marketplace_order_id: str, setting_doc: str) -> str:
    """
    Ensures a Marketplace Order ID doc exists for the given Shopify order name,
    returns its name.
    """
    if frappe.db.exists("Marketplace Order ID", marketplace_order_id):
        return marketplace_order_id

    mo_id_doc = frappe.new_doc("Marketplace Order ID")
    mo_id_doc.marketplace_order_id = marketplace_order_id
    mo_id_doc.marketplace = frappe.get_value(
        "Shopify Integration Settings", setting_doc, "marketplace"
    )
    mo_id_doc.save(ignore_permissions=True)
    return mo_id_doc.name
