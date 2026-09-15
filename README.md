## Shopify Integration

This app fetches and sends data from and to shopify via GraphQL.

### Requirements

- Ubuntu v22.04
- ERPNext v15
- [Marketplace](https://github.com/zainab-unifyxperts/erp-marketplace) — **must be installed first**, this app depends on the `Marketplace` doctype
- Install the ShopifyAPI `bench pip install --upgrade ShopifyAPI`

### Installation Guide

1. Install [erp-marketplace](https://github.com/zainab-unifyxperts/erp-marketplace) first (see its README for install steps).
2. `bench get-app https://github.com/zainab-unifyxperts/erp-shopify-integration.git` - Downloads this app from the repository
3. `bench install-app shopify_integration` - Installs Shopify Integration on current site

### Configuration

We store the following configurations in the setting doc for Shopify Integrations in erpnext which is required while syncing sales order.

1. The doctype `Shopify Integration Settings`, should automatically appear if the installation went smoothly

- We need all of the following fields in `Shopify Integration Settings` to sync orders.

    | Name                       | Type                 | Description                                                                                                                                                                |
    | -------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
    | `API Version`              | Data                 | It is the version of [shopify-GraphQL API](https://shopify.github.io/shopify_python_api/?shpxid=eb6a310c-A486-480D-464E-A00114B79B08). Our code supports version `2024-04` |
    | `API Key`                  | Password             | You can generate the credentials directly in your admin, this function can be located in your admin under the Apps section                                                 |
    | `API Secret`               | Password             | You can generate the credentials directly in your admin, this function can be located in your admin under the Apps section                                                 |
    | `Access Token`             | Password             | [Access Token](https://shopify.dev/docs/apps/auth/access-token-types/admin-app-access-tokens) is required to send or receive queries using GraphQL                         |
    | `Company`                  | Link(to Company)     | Setup your company in ERPNext                                                                                                                                              |
    | `Default Customer Type`    | Data                 | If your customers are individuals set it to `Individuals`                                                                                                                  |
    | `Marketplace`              | Link(to Marketplace) | Create marketplace "Shopify"                                                                                                                                               |
    | `Default UOM`              | Link (to UOM)        | We have set default UOM(is a ERPNext doctype) to Nos                                                                                                                       |
    | `Default Tax Charge Type`  | Select               | 1) Actual 2) On Net Total 3) On Previous Row Amount 4) On Previous Row 5) Total On Item Quantity                                                                           |
    | `Default Item Group`       | Link(item group)     | Is set to Products in our settings                                                                                                                                         |
    | `Default Tax Account`      | Link(to Account)     | Set our default tax account                                                                                                                                                |
    | `Order Syncing Start Date` | Date                 | The date from which we want to sync orders                                                                                                                                 |
    | `Order Sync Duration`      | Int                  | Offset from current day (in number of days)                                                                                                                                |

- Make sure you create a setting document that has all the above fields

2. Customisations done for ERPNext Doctypes.

- **Note** Use customize form to change or add fields to the core doctypes

    | Doctype Name            | Modification Type | Fieldname                 | FieldType |
    | ------------------------ | ------------------ | -------------------------- | ---------- |
    | `Sales Order`            | Add                | `Shopify Order Id Number`  | Data       |
    | `Accounting Dimensions`  | Add                | `Marketplace`               | Link       |

3. Setup Shopify shop for GraphQL

- The sales orders won't start syncing until you setup your shopify shop to send and receive information via GraphQL
- Get credentials stored in Shopify Integration Settings
    - `api_key` You can generate the credentials directly in your admin, this function can be located from your admin under the Apps section
    - `api_secret` You can generate the credentials directly in your admin, this function can be located from your admin under the Apps section
    - `shop_name` Shop name, can be found in the site url
- Import and run the function `setup_shop` in the `shopify_selling_utils.py` in bench console
- This should set up our shop and now we can use our GraphQL queries
- **Note**: If you get an importError, make sure you have installed ShopifyAPI using `bench pip install --upgrade ShopifyAPI`

4. Customizing Cron

- Currently the cron is setup to sync orders every 3 minutes
- You can enter a valid cron string in `scheduler_events = { "cron": { "*/3 * * * *": [ "shopify_integration.shopify_selling.orders.shopify_order_sync_job" ] }, }` which can be found in `shopify_integration/hooks.py`

- **Note**: run `bench migrate` every time you install a new app on your site or make changes to hooks.py

### Release Note

- This app syncs sales order from Shopify to ERPNext.
- The Sales orders are fetched using GraphQL queries and a cron is setup via hooks to sync order with Shopify for every 3 minutes
- We can also set the order sync start date in the Shopify Integration Settings
- **Note**: run `bench migrate` every time you install a new app on your site


# ERP Shopify Integration — Codebase Analysis

## Overview

This document covers two Frappe/ERPNext apps that together sync Shopify orders into ERPNext:

| App | Repo | Purpose |
|---|---|---|
| `marketplace` | `erp-marketplace` | Stores Marketplace and Marketplace Order ID doctypes; adds custom fields to core ERPNext doctypes (Sales Order, Customer, Item, etc.) |
| `shopify_integration` | `erp-shopify-integration` | Fetches orders from Shopify via GraphQL, maps them to ERPNext Sales Orders, and creates Payment Entries and Sales Invoices |

**Stack:** Ubuntu 22.04 · ERPNext v15 · Frappe · ShopifyAPI (GraphQL, version `2026-01`)

**Cron:** Uncomment the code(configurable in `hooks.py`) — `"*/x * * * *"` → `shopify_integration.shopify_selling.sync.shopify_order_sync_job`

---

## High-Level Flow

```
Shopify GraphQL API
       ↓
shopify_order_sync_job()          ← cron entrypoint
       ↓
enqueue_shopify_sync_orders()     ← whitelisted; also called by the Sync Orders button
       ↓  (background job)
sync_shopify_orders()             ← fetches all orders, paginates
       ↓  (per order)
create_shopify_sales_order()      ← maps one Shopify order → ERPNext SO
       ↓
  ┌────────────────────────────────────────────┐
  │  Customer   Address   Items   Taxes   Tags │
  └────────────────────────────────────────────┘
       ↓
   SO.save() + SO.submit()
       ↓
  sync_payment_entries()          ← PE per transaction
       ↓ (if POS)
  create_pos_sales_invoice()      ← SI with update_stock=1
```

---

## 1. Sync Orders Button — Specific Store

**Location:** `shopify_integration/shopify_integration/doctype/shopify_integration_settings/`

**Doctype field:**
```json
{ "fieldname": "sync_orders", "fieldtype": "Button", "label": "Sync Orders" }
```

**JS handler** (`shopify_integration_settings.js`):
```javascript
sync_orders(frm){}
```

The button is **per-store** because it passes `frm.doc.name` — whichever `Shopify Integration Settings` document is currently open in the UI. Each store has its own settings document with its own API credentials, company, and sync dates.

---

## 2. Function That Gets Enqueued

**File:** `shopify_integration/shopify_selling/sync.py`

### Cron entrypoint
```python
    def shopify_order_sync_job() -> None:
```
Loops all enabled stores and enqueues one sync job per store.

### Button / API entrypoint
```python
    @frappe.whitelist()
    def enqueue_shopify_sync_orders(doc: str, use_setting_date: bool) -> None:
```
Resolves the `start_date` (either from the settings field or calculated from `order_sync_duration`), then puts the actual worker on the Frappe background queue.

### Actual background worker (enqueued fn)
```python
    def sync_shopify_orders(setting_doc_name: str, start_date: str) -> None:
```
Fetches all Shopify orders created after `start_date` (excluding `CANCELLED`), paginates via `pageInfo.hasNextPage`, and calls `create_shopify_sales_order()` for each order node.

---

## 3. Auto Item Creation in ERP

**File:** `shopify_integration/shopify_selling/mapping/items.py`

**Function:** `get_shopify_item_code(sku, name, setting_doc)`

```python
def get_shopify_item_code(sku: str, name: str, setting_doc: str) -> str | None:

```

**Setting that controls this:** `auto_create_items` (checkbox on `Shopify Integration Settings`)

**If disabled:** `get_shopify_item_code` returns `None` → `build_item_rows_from_shopify` returns `(None, missing_sku, missing_item_name)` → `create_shopify_sales_order` logs an error ("Missing Item / Create Missing Items is disabled") and **skips the order entirely**.

**Call chain:**
`build_item_rows_from_shopify()` → `create_shopify_so_item_row()` → `get_shopify_item_code()`

---

## 4. Address Doc Creation for Each Order

**File:** `shopify_integration/shopify_selling/mapping/customer.py`

**Function:** `get_shopify_address(address_data, customer, setting_doc)`

```python
def get_shopify_address(address_data, customer, setting_doc) -> str | None:
```

**Setting that controls this:** `auto_create_address` (checkbox on `Shopify Integration Settings`)

**Billing vs Shipping:** If `billingAddressMatchesShippingAddress` is `True` on the Shopify order, the same Address doc is set for both `customer_address` and `shipping_address_name`. Otherwise, `get_shopify_address` is called separately for billing and shipping.

**POS orders:** Address resolution is **skipped entirely** — walk-in sales have no address.

Called from `_set_customer_and_addresses()` in `mapping/order.py`.

---

## 5. Payment Entry (PE) Creation if Order is Paid

**File:** `shopify_integration/shopify_selling/payments.py`

**Function:** `sync_payment_entries(transactions, sales_order_name, setting_doc_name)`

```python
def sync_payment_entries(transactions, sales_order_name, setting_doc_name=None):

        create_shopify_payment_entry(transaction, sales_order_name, setting_doc_name)
```

**Note:** The trigger is the presence of Shopify **transaction records** (each with a `paymentId`), not just a `fullyPaid` boolean. A payment entry is created for every distinct, not-yet-recorded transaction.

**Function:** `create_shopify_payment_entry(data, sales_order_name, setting_doc_name)`

```python
def create_shopify_payment_entry(data, sales_order_name, setting_doc_name):

```

`_resolve_paid_to_account` checks the `Shopify Payment Account Table` child table for a gateway-to-account mapping; falls back to `default_payment_account` on the settings doc.

`sync_payment_entries` is called **twice** in `create_shopify_sales_order`:
- For a **new SO** — after SO is submitted
- For an **existing SO** — on re-sync, to catch late/missed transactions

---

## 6. Online Store vs POS (Offline Store) Flow

### Detection

**File:** `shopify_integration/shopify_selling/mapping/order.py`

**Function:** `is_pos_order(data: dict) -> bool`

```python
def is_pos_order(data: dict) -> bool:
```

Shopify sets `sourceName = "pos"` on all orders placed through the POS channel (in-store / offline). Online orders have a different `sourceName` (e.g., `"web"`, `"shopify_draft_order"`) or an empty value.

---

### Online Store Flow → SO → DN → SI

1. `create_shopify_sales_order()` creates and submits the **Sales Order**.
2. `sync_payment_entries()` creates a **Payment Entry** against the SO.
3. A **Delivery Note** is created separately from the SO (manually or via fulfillment webhook). The DN updates stock on hand.
4. On DN submission, a **Sales Invoice** is created from the DN.

```
SO  →  DN  →  SI
         ↑
    stock updated here
```

The `hooks.py` has a commented-out `doc_events` hook for `Delivery Note → on_submit → create_sales_invoice` in `shopify_finance/finance.py`, indicating this SI creation was previously automatic on DN submit.

---

### Offline Store (POS) Flow → SO → SI (no DN)

1. `create_shopify_sales_order()` creates and submits the **Sales Order**.
2. `sync_payment_entries()` creates an advance **Payment Entry** against the SO.
3. `create_pos_sales_invoice()` is called immediately after SO submit.

**File:** `shopify_integration/shopify_selling/billing/pos_invoice.py`

**Function:** `create_pos_sales_invoice(sales_order_name, setting_doc, serial_map)`

```python
def create_pos_sales_invoice(sales_order_name, setting_doc, serial_map):
```

With `update_stock = 1` set on the SI, **no Delivery Note is created** — ERPNext updates stock directly via the Sales Invoice.

Serial numbers are pulled from Shopify POS order line item `customAttributes` (key `"SN"` / `"SERIAL NUMBER"` / `"SERIAL NO"`) by `extract_pos_serial_map()` and validated before the SI is created.

```
SO  →  SI (update_stock=1)
         ↑
    stock updated here (no DN)
```

---

### Side-by-Side Comparison

| | Online Store | POS (Offline Store) |
|---|---|---|
| **Shopify `sourceName`** | `"web"` / other | `"pos"` |
| **Detection fn** | `is_pos_order() == False` | `is_pos_order() == True` |
| **Document flow** | SO → DN → SI | SO → SI |
| **Delivery Note** | Created (updates stock) | **Not created** |
| **Stock update mechanism** | Delivery Note | `update_stock = 1` on SI |
| **Address resolution** | Required (billing + shipping) | **Skipped** (walk-in) |
| **Serial numbers** | Not handled in this flow | Captured from POS custom attributes |
| **PE reconciliation** | PE stays against SO | Advance PE allocated to SI via `_allocate_advance_payment()` |
| **SI field: `update_stock`** | Not set (default 0) | Set to `1` |

---

## Key Settings in `Shopify Integration Settings`

| Field | Type | Purpose |
|---|---|---|
| `enabled` | Check | Whether this store is picked up by the cron |
| `api_key`, `api_secret`, `access_token` | Password | Shopify GraphQL credentials |
| `company` | Link → Company | ERPNext company for all docs |
| `marketplace` | Link → Marketplace | Links to the `erp-marketplace` Marketplace doc |
| `default_item_group` | Link | Used when auto-creating items |
| `default_uom` | Link | Used when auto-creating items |
| `auto_create_items` | Check | Whether to create missing ERPNext Items automatically |
| `auto_create_address` | Check | Whether to create missing Address docs automatically |
| `default_payment_account` | Link | Fallback account for Payment Entries |
| `order_syncing_start_date` | Date | Start date used when button is clicked (`use_setting_date=True`) |
| `order_sync_duration` | Int | Days offset from today; used by cron |
| `backlog_warehouse` | Link | Default warehouse set on SO item rows |
| `price_list` | Link | Selling price list applied to SO |
| `shopify_fulfillment_statuses_to_exclude` | Table | Fulfillment statuses that skip SO creation |

---

## File Reference

```
erp-shopify-integration/
└── shopify_integration/
    ├── hooks.py                              ← cron scheduler config
    ├── shopify_integration/
    │   └── doctype/
    │       └── shopify_integration_settings/
    │           ├── shopify_integration_settings.json   ← "Sync Orders" Button field
    │           └── shopify_integration_settings.js     ← Button handler → enqueue_shopify_sync_orders
    └── shopify_selling/
        ├── sync.py                           ← shopify_order_sync_job, enqueue_shopify_sync_orders, sync_shopify_orders
        ├── payments.py                       ← sync_payment_entries, create_shopify_payment_entry
        ├── billing/
        │   └── pos_invoice.py               ← create_pos_sales_invoice (POS flow, update_stock=1)
        └── mapping/
            ├── order.py                      ← create_shopify_sales_order, is_pos_order
            ├── items.py                      ← get_shopify_item_code (auto item creation)
            └── customer.py                   ← get_shopify_customer, get_shopify_address

erp-marketplace/
└── marketplace/
    └── (Marketplace, Marketplace Order ID doctypes + custom fields on SO, Customer, Item, etc.)
```

### Contributing

Issues and pull requests are welcome. Please open an issue describing the bug or feature before submitting a PR where possible.

### License

mit
