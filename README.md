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

### Contributing

Issues and pull requests are welcome. Please open an issue describing the bug or feature before submitting a PR where possible.

### License

mit