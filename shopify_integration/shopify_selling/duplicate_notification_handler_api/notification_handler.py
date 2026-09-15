import frappe
import hashlib


# shopify_selling/duplicate_notification_handler_api/notification_handler.py
def add_error_log_index():
    frappe.db.add_index("Error Log", ["custom_error_hash"])
    frappe.db.commit()

    
def set_error_deduplication_flags(doc, method):
    
    # Compute MD5 hash of method + error
    error_hash = hashlib.md5((doc.method + doc.error).encode()).hexdigest()
    doc.custom_error_hash = error_hash

    # Check if same error already exists
    is_duplicate = frappe.db.exists("Error Log", {
        "custom_error_hash": error_hash
    })

    # Set flag to suppress notification if duplicate
    doc.custom_skip_notification = 1 if is_duplicate else 0