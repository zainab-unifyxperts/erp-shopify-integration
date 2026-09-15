# Error Log Notification Deduplication

## Problem
Frappe sends a notification email for every `Error Log` entry. When the same error occurs multiple times (e.g., 10 orders fail with the same error 10 times), it floods the inbox with duplicate emails.

## Goal
Send email only once per unique error message. If the same error has already been logged before, skip the notification.

---

## Solution Overview

1. Add two custom fields to `Error Log`
2. Use a `before_insert` hook to compute an MD5 hash of the error and check for duplicates
3. Set a flag on the doc to skip the notification if duplicate
4. Use a simple field check in the Notification condition (no DB query in condition)

---

- If this feature is not needed then keep those line commented in hooks.py

## Why Not Query in Notification Condition?

Frappe's Notification condition runs inside `safe_eval` which sandboxes the `frappe` object. The following are **not available** in the condition:

- `frappe.db` ❌
- `frappe.call` ❌

So all DB logic must happen in the `before_insert` hook, and the condition just reads a flag.

---

## Custom Fields on `Error Log`

Add via **Customize Form → Error Log**:

| Field Name | Type | Purpose |
|---|---|---|
| `custom_error_hash` | Data | MD5 hash of `method + error` |
| `custom_skip_notification` | Check | Flag to suppress notification |

---

## DB Index

Since `custom_error_hash` is queried on every insert, it must be indexed for performance.

Added via `after_migrate` hook (runs once on every `bench migrate`):

```python
def add_error_log_index():
    frappe.db.add_index("Error Log", ["custom_error_hash"])
    frappe.db.commit()
```

Verify index exists:
```sql
SHOW INDEX FROM `tabError Log` WHERE Column_name = 'custom_error_hash';
```

---

## Code

### `frappetrack/api/test.py`

```python
import hashlib
import frappe

def add_error_log_index():
    frappe.db.add_index("Error Log", ["custom_error_hash"])
    frappe.db.commit()

def execute(doc, method):
    # Compute MD5 hash of method + error
    error_hash = hashlib.md5((doc.method + doc.error).encode()).hexdigest()
    doc.custom_error_hash = error_hash

    # Check if same error already exists
    is_duplicate = frappe.db.exists("Error Log", {
        "custom_error_hash": error_hash
    })

    # Set flag to suppress notification if duplicate
    doc.custom_skip_notification = 1 if is_duplicate else 0
```

### `hooks.py`
### UnComment Below lines from hooks.py, or merge them with exisiting events 
```python
doc_events = {
    "Error Log": {
        "before_insert": "shopify_integration.shopify_selling.duplication_notification_handler_api.notification_handler.set_error_deduplication_flags"
    },
}

after_migrate = [
    "shopify_integration.shopify_selling.duplication_notification_handler_api.notification_handler.add_error_log_index"
]
```

---

## Notification Configuration

| Field | Value |
|---|---|
| Document Type | `Error Log` |
| Send Alert On | `New` |
| Condition | `doc.method == "some method name" or doc.method == "some other method name") and not doc.custom_skip_notification` |

> For testing use: `doc.method == "Test Error" and not doc.custom_skip_notification`

---

## How It Works

```
Error Log created
       ↓
before_insert fires
       ↓
MD5 hash computed from (method + error)
       ↓
DB query: does hash already exist? (fast — indexed Data field)
       ↓
Yes → custom_skip_notification = 1
No  → custom_skip_notification = 0
       ↓
Notification condition checks flag
       ↓
Flag = 1 → skip email ❌
Flag = 0 → send email ✅
```

---

## Testing

Run in bench console:

```python
import frappe

# First — should trigger email
frappe.get_doc({
    "doctype": "Error Log",
    "method": "Test Error",
    "error": "This is a test error message"
}).insert()
frappe.db.commit()

# Second — same error, should NOT trigger email
frappe.get_doc({
    "doctype": "Error Log",
    "method": "Test Error",
    "error": "This is a test error message"
}).insert()
frappe.db.commit()

# Third — different error, should trigger email
frappe.get_doc({
    "doctype": "Error Log",
    "method": "Test Error",
    "error": "This is a different error message"
}).insert()
frappe.db.commit()
```

**Expected result:**
- 1st insert → ✅ Email sent
- 2nd insert → ❌ No email (duplicate hash)
- 3rd insert → ✅ Email sent (different hash)

---

## Notes

- Hooks only need to be added in **one app** — Frappe merges `hooks.py` across all installed apps
- Error logs are deleted every 7 days — after deletion, the same error will trigger a fresh notification
- `custom_error_hash` uses MD5 which is fast and produces a fixed 32-char string — cheap to index and query
