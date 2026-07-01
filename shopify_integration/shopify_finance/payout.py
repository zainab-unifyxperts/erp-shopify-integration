import frappe
import requests
import json
from frappe.utils import flt

def fetch_paid_shopify_payouts():
    """
    Fetch all Shopify payouts having status='paid'.

    Only paid payouts are returned because Journal Entries and
    Xero Receive Money should only be created after Shopify has
    completed the payout.
    """

    setting_name = frappe.db.get_value(
        "Shopify Integration Settings",
        {"enabled": 1},
        "name"
    )
    if not setting_name:
        frappe.throw("No enabled Shopify Integration Settings found.")

    setting_doc = frappe.get_doc(
        "Shopify Integration Settings",
        setting_name
    )
    headers = {
        "X-Shopify-Access-Token": setting_doc.get_password("access_token"),
        "Accept": "application/json"
    }
    url = (
        f"https://{setting_doc.shop_name}.myshopify.com"
        f"/admin/api/{setting_doc.api_version}"
        f"/shopify_payments/payouts.json"
    )
    response = requests.get(
        url,
        headers=headers,
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    return [
        payout
        for payout in data.get("payouts", [])
        if payout.get("status") == "paid"
    ]

def get_payout_transactions(payout_id):
    """
    Fetch all transactions for a Shopify payout.

    Args:
        payout_id (int | str): Shopify payout ID

    Returns:
        list: List of payout transactions
    """

    setting_name = frappe.db.get_value(
        "Shopify Integration Settings",
        {"enabled": 1},
        "name"
    )
    if not setting_name:
        frappe.throw("No enabled Shopify Integration Settings found.")
    setting_doc = frappe.get_doc(
        "Shopify Integration Settings",
        setting_name
    )
    headers = {
        "X-Shopify-Access-Token": setting_doc.get_password("access_token"),
        "Accept": "application/json"
    }
    url = (
        f"https://{setting_doc.shop_name}.myshopify.com"
        f"/admin/api/{setting_doc.api_version}"
        f"/shopify_payments/balance/transactions.json"
    )
    transactions = []
    params = {"payout_id": payout_id}
    while url:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )
        response.raise_for_status()
        transactions.extend(
            response.json().get("transactions", [])
        )
        # Only needed on the first request
        params = None
        if response.links.get("next"):
            url = response.links["next"]["url"]
        else:
            url = None
    return transactions

def get_shopify_payout_bank_references():
    """
    Returns a dictionary keyed by Shopify payout id.

    Example:
    {
        "132591943850": {
            "bank_reference": "111000020551058",
            "reference_date": "2026-06-25"
        }
    }
    """

    setting_name = frappe.db.get_value(
        "Shopify Integration Settings",
        {"enabled": 1},
        "name"
    )

    if not setting_name:
        frappe.throw("No enabled Shopify Integration Settings found.")

    setting_doc = frappe.get_doc(
        "Shopify Integration Settings",
        setting_name
    )

    headers = {
        "X-Shopify-Access-Token": setting_doc.get_password("access_token"),
        "Content-Type": "application/json",
    }

    query = """
    query {
      shopifyPaymentsAccount {
        payouts(first: 100, reverse: true) {
          nodes {
            legacyResourceId
            issuedAt
            externalTraceId
          }
        }
      }
    }
    """

    response = requests.post(
        f"https://{setting_doc.shop_name}.myshopify.com/admin/api/2025-10/graphql.json",
        headers=headers,
        json={"query": query},
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if result.get("errors"):
        frappe.throw(
            json.dumps(result["errors"], indent=2)
        )

    nodes = (
        result.get("data", {})
        .get("shopifyPaymentsAccount", {})
        .get("payouts", {})
        .get("nodes", [])
    )

    return {
        node["legacyResourceId"]: {
            "bank_reference": node.get("externalTraceId"),
            "reference_date": node.get("issuedAt", "")[:10],
        }
        for node in nodes
    }

def sync_shopify_payouts():
    """
    Fetch paid Shopify payouts and create Shopify Payout documents.
    """

    payouts = [
        {
            "id": 132591943850,
            "status": "paid",
            "date": "2026-06-25",
            "currency": "USD",
            "amount": "34158.56",
            "summary": {
                "charges_gross_amount": "36794.07",
                "charges_fee_amount": "939.67",
                "refunds_gross_amount": "-1419.74"
            }
        }
    ]

    bank_references = {
        "132591943850": {
            "bank_reference": "111000020551058",
            "reference_date": "2026-06-25"
        }
    }
    inserted = False
    inserted_count = 0

    for payout in payouts:
        # Skip payouts already imported.
        if frappe.db.exists(
            "Shopify Payout",
            {"payout_id": str(payout["id"])}
        ):
            continue

        try:
            bank_reference = bank_references.get(
                str(payout["id"]),
                {}
            )
            payout_doc = frappe.new_doc("Shopify Payout")
            payout_doc.payout_id = str(payout["id"])
            payout_doc.payout_date = payout.get("date")
            payout_doc.payout_status = payout.get("status")
            payout_doc.bank_reference = bank_reference.get(
                "bank_reference"
            )
            payout_doc.bank_reference_date = bank_reference.get(
                "reference_date"
            )
            payout_doc.currency = payout.get("currency")
            payout_doc.net_amount = flt(payout.get("amount", 0))
            summary = payout.get("summary", {})
            payout_doc.gross_charges = flt(
                summary.get("charges_gross_amount", 0)
            )
            payout_doc.gross_refunds = abs(
                flt(summary.get("refunds_gross_amount", 0))
            )
            payout_doc.fees = flt(
                summary.get("charges_fee_amount", 0)
            )
            payout_doc.raw_response = json.dumps(
                payout,
                indent=4
            )
            transactions = [

                # -------------------------------------------------
                # Shopify payout (ignored)
                # -------------------------------------------------
                {
                    "id": 3354653884586,
                    "type": "payout",
                    "payout_id": 132591943850,
                    "payout_status": "paid",
                    "currency": "USD",
                    "amount": "-34158.56",
                    "fee": "0.00",
                    "net": "-34158.56",
                    "source_order_id": 7057478189309
                },

                # -------------------------------------------------
                # B2B (excluded)
                # -------------------------------------------------
                {
                    "id": 3354544079018,
                    "type": "charge",
                    "amount": "520.00",
                    "fee": "15.00",
                    "net": "505.00",
                    "source_order_id": 7067224506621
                },

                # -------------------------------------------------
                # B2C Charges
                # -------------------------------------------------
                {
                    "id": 1,
                    "type": "charge",
                    "amount": "1250.00",
                    "fee": "31.25",
                    "net": "1218.75",
                    "source_order_id": 7035734753533
                },
                {
                    "id": 2,
                    "type": "charge",
                    "amount": "985.00",
                    "fee": "24.63",
                    "net": "960.37",
                    "source_order_id": 7035734753533
                },
                {
                    "id": 3,
                    "type": "charge",
                    "amount": "742.00",
                    "fee": "18.55",
                    "net": "723.45",
                    "source_order_id": 7035734753533
                },
                {
                    "id": 4,
                    "type": "charge",
                    "amount": "1635.00",
                    "fee": "40.88",
                    "net": "1594.12",
                    "source_order_id": 7035734753533
                },
                {
                    "id": 5,
                    "type": "charge",
                    "amount": "889.00",
                    "fee": "22.23",
                    "net": "866.77",
                    "source_order_id": 7035734753533
                },

                # -------------------------------------------------
                # Refunds
                # -------------------------------------------------
                {
                    "id": 6,
                    "type": "refund",
                    "amount": "-85.00",
                    "fee": "0.00",
                    "net": "-85.00",
                    "source_order_id": 7035734753533
                },
                {
                    "id": 7,
                    "type": "refund",
                    "amount": "-40.00",
                    "fee": "0.00",
                    "net": "-40.00",
                    "source_order_id": 7035734753533
                },

                # -------------------------------------------------
                # Marketplace Tax
                # -------------------------------------------------
                {
                    "id": 8,
                    "type": "debit",
                    "amount": "-52.00",
                    "fee": "0.00",
                    "net": "-52.00",
                    "adjustment_reason": "tax_adjustment"
                },

                # -------------------------------------------------
                # Shop Cash
                # -------------------------------------------------
                {
                    "id": 9,
                    "type": "credit",
                    "amount": "15.25",
                    "fee": "0.00",
                    "net": "15.25",
                    "adjustment_reason": "shop_cash"
                },

                # -------------------------------------------------
                # Shop Campaign
                # -------------------------------------------------
                {
                    "id": 10,
                    "type": "credit",
                    "amount": "30.00",
                    "fee": "0.00",
                    "net": "30.00",
                    "adjustment_reason": "shop_campaign"
                },

                # -------------------------------------------------
                # Reserve
                # -------------------------------------------------
                {
                    "id": 11,
                    "type": "debit",
                    "amount": "-100.00",
                    "fee": "0.00",
                    "net": "-100.00",
                    "adjustment_reason": "reserve"
                },

                # -------------------------------------------------
                # Reserve Release
                # -------------------------------------------------
                {
                    "id": 12,
                    "type": "credit",
                    "amount": "100.00",
                    "fee": "0.00",
                    "net": "100.00",
                    "adjustment_reason": "reserve_release"
                },

                # -------------------------------------------------
                # Chargeback
                # -------------------------------------------------
                {
                    "id": 13,
                    "type": "dispute",
                    "amount": "-250.00",
                    "fee": "15.00",
                    "net": "-265.00",
                    "source_order_id": 7035734753533
                },

                # -------------------------------------------------
                # Chargeback Reversal
                # -------------------------------------------------
                {
                    "id": 14,
                    "type": "credit",
                    "amount": "250.00",
                    "fee": "0.00",
                    "net": "250.00",
                    "adjustment_reason": "chargeback_reversal"
                }
            ]
            for transaction in transactions:
                payout_doc.append(
                    "transactions",
                    {
                        "transaction_id": str(transaction.get("id")),
                        "type": transaction.get("type"),
                        "source_order_id": str(transaction.get("source_order_id") or ""),
                        "adjustment_reason": transaction.get("adjustment_reason"),
                        "amount": flt(transaction.get("amount", 0)),
                        "fee": flt(transaction.get("fee", 0)),
                        "net": flt(transaction.get("net", 0)),
                    }
                )
            payout_doc.insert(ignore_permissions=True)
            map_shopify_payout_transactions(payout_doc.name)
            inserted = True
            inserted_count += 1
        except Exception:
            frappe.log_error(
                title=f"Shopify Payout Sync - {payout.get('id')}",
                message=frappe.get_traceback()
            )
    if inserted:
        frappe.db.commit()
    return inserted_count

def map_shopify_payout_transactions(payout_name: str):
    """
    Map Shopify payout transactions to ERP Sales Orders.
    """

    payout = frappe.get_doc("Shopify Payout", payout_name)
    updated = False

    for row in payout.transactions:
        # Payout transaction itself
        if row.type == "payout":
            row.sales_order = ""
            row.sales_type = ""
            row.included = 0
            row.remarks = "Not linked to Sales Order"
            updated = True
            continue
        # Shopify adjustment transactions
        if row.type in ("credit", "debit", "dispute"):
            row.sales_order = ""
            row.sales_type = ""
            row.included = 1

            if row.type == "dispute":
                row.remarks = "Chargeback"

            elif row.adjustment_reason == "tax_adjustment":
                row.remarks = "Marketplace Sales Tax"

            elif row.adjustment_reason == "shop_cash":
                row.remarks = "Shop Cash"

            elif row.adjustment_reason:
                row.remarks = row.adjustment_reason.replace("_", " ").title()

            elif row.type == "credit":
                row.remarks = "Shopify Credit Adjustment"

            else:
                row.remarks = "Shopify Debit Adjustment"

            updated = True
            continue

        shopify_gid = f"gid://shopify/Order/{row.source_order_id}"
        sales_order = frappe.db.get_value(
            "Sales Order",
            {
                "custom_shopify_order_id_number": shopify_gid
            },
            ["name", "custom_sales_type"],
            as_dict=True
        )
        if not sales_order:
            row.sales_order = ""
            row.sales_type = ""
            row.included = 0
            row.remarks = "Sales Order not found"
            updated = True
            continue
        row.sales_order = sales_order.name
        row.sales_type = sales_order.custom_sales_type
        if sales_order.custom_sales_type == "B2C":
            row.included = 1
            row.remarks = ""
        else:
            row.included = 0
            row.remarks = "Excluded - B2B"
        updated = True
    if updated:
        payout.save(ignore_permissions=True)
        frappe.db.commit()

def create_shopify_payout_journal_entry(payout_name: str):
    """
    Create a Journal Entry for a Shopify payout using only
    included B2C transactions.
    """

    payout = frappe.get_doc(
        "Shopify Payout",
        payout_name
    )
    if payout.journal_entry:
        frappe.throw(
            f"Journal Entry already exists: {payout.journal_entry}"
        )
    setting_name = frappe.db.get_value(
        "Shopify Integration Settings",
        {"enabled": 1},
        "name"
    )
    if not setting_name:
        frappe.throw("No enabled Shopify Integration Settings found.")
    setting_doc = frappe.get_doc(
        "Shopify Integration Settings",
        setting_name
    )
    gross_charges = 0
    refunds = 0
    fees = 0
    other_credits = 0
    other_debits = 0

    for row in payout.transactions:
        if not row.included:
            continue
        if row.type == "charge":
            gross_charges += flt(row.amount)
            fees += flt(row.fee)
        elif row.type == "refund":
            refunds += abs(flt(row.amount))
        elif row.type == "credit":
            other_credits += flt(row.amount)
            fees += flt(row.fee)
        elif row.type in ("debit", "dispute"):
            other_debits += abs(flt(row.amount))
            if row.fee:
                fees += flt(row.fee)
        else:
            frappe.log_error(
                title=f"Unhandled Shopify payout transaction: {row.type}",
                message=row.as_dict()
            )
    if gross_charges == 0 and refunds == 0:
        frappe.throw("No B2C transactions found for this payout.")

    gross_charges = flt(gross_charges, 2)
    refunds = flt(refunds, 2)
    fees = flt(fees, 2)
    other_credits = flt(other_credits, 2)
    other_debits = flt(other_debits, 2)
    bank = flt(
        gross_charges
        + other_credits
        - refunds
        - other_debits
        - fees,
        2
    )
    print({
        "gross_charges": gross_charges,
        "refunds": refunds,
        "fees": fees,
        "other_credits": other_credits,
        "other_debits": other_debits,
        "bank": bank,
        "total_debit": bank + refunds + fees + other_debits,
        "total_credit": gross_charges + other_credits,
    })
    je = frappe.new_doc("Journal Entry")
    je.multi_currency = 1
    je.voucher_type = "Journal Entry"
    je.posting_date = payout.payout_date
    je.company = "Alphard Golf"
    je.user_remark = f"Shopify Payout {payout.payout_id}"
    je.cheque_date = (
        payout.bank_reference_date
        or payout.payout_date
    )

    if payout.bank_reference:
        je.cheque_no = payout.bank_reference

    # Hardcoded accounts (TESTING ONLY)
    BANK_ACCOUNT = "Cash - AG"
    CLEARING_ACCOUNT = "Sales - AG"
    REFUND_ACCOUNT = "Sales Expenses - AG"
    FEE_ACCOUNT = "Miscellaneous Expenses - AG"
    # ADJUSTMENT_ACCOUNT = "Shopify Adjustments - AG"

    # Bank
    if bank >= 0:
        je.append(
            "accounts",
            {
                "account": BANK_ACCOUNT,
                "debit_in_account_currency": bank,
            }
        )
    else:
        je.append(
            "accounts",
            {
                "account": BANK_ACCOUNT,
                "credit_in_account_currency": abs(bank),
            }
        )
    # Refund
    if refunds:
        je.append(
            "accounts",
            {
                "account": REFUND_ACCOUNT,
                "debit_in_account_currency": refunds,
            }
        )
    # Shopify Fees
    if fees:
        je.append(
            "accounts",
            {
                "account": FEE_ACCOUNT,
                "debit_in_account_currency": fees,
            }
        )
    # Shopify Adjustments
    if other_debits:
        je.append(
            "accounts",
            {
                "account": FEE_ACCOUNT, # ADJUSTMENT_ACCOUNT,
                "debit_in_account_currency": other_debits,
            }
        )
    # Shopify Clearing
    je.append(
        "accounts",
        {
            "account": CLEARING_ACCOUNT,
            "credit_in_account_currency": (
                gross_charges + other_credits
            ),
        }
    )
    print("=" * 80)
    print("Company:", je.company)
    print("Multi Currency:", je.multi_currency)

    for row in je.accounts:
        acc = frappe.get_doc("Account", row.account)
        print({
            "account": acc.name,
            "company": acc.company,
            "currency": acc.account_currency,
            "is_group": acc.is_group,
            "debit": row.debit_in_account_currency,
            "credit": row.credit_in_account_currency,
        })
    print("=" * 80)
    je.insert(ignore_permissions=True)
    je.submit()
    payout.db_set(
        "journal_entry",
        je.name,
        update_modified=False
    )
    frappe.db.commit()
    return je.name