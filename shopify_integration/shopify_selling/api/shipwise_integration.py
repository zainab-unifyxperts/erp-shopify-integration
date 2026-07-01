import frappe
import requests
import time
import json
from extensiv_integration.extensiv_inventory.utils import get_nearest_warehouse
import base64
from io import BytesIO
from PIL import Image
from frappe.utils.file_manager import save_file
from frappe.utils.pdf import get_pdf
from pypdf import PdfWriter, PdfReader

# integrating shipwise with url, headers
def create_return_label(so_name, return_line_items):
    """
    Full flow:
      1. Build payload (rate + select cheapest)
      2. Call POST /api/v1/Ship/RateAndShip
      3. Extract tracking + label from response
    """
    frappe.log_error("create_return_label", "DEBUG")

    setting_doc = frappe.get_doc(
        "Extensiv Settings",
        {"enabled": 1}
    )

    # if cheapest off then call ship else call the rate and ship

    shipwise_rate_and_ship_url = setting_doc.shipwise_rate_and_ship
    api_key = setting_doc.get_password("shipwise_api_key")
    api_secret = setting_doc.get_password("shipwise_api_secret")
    headers = build_return_headers(api_key, api_secret)

    payload, nearest_warehouse = build_return_payload(so_name, return_line_items)
    if not payload:
        return None

    
    frappe.log_error(title="Debug Return Label create_return_label finsihed", message=f"Payload: {payload}, Headers: {headers}")
    # return_label_details = {
    #     "pdf_url":"https://shopify-private-shop-assets.storage.googleapis.com/s/files/1/d/b50f/0800/8957/9773/shipping_labels/4_x_6/ca381f8962d9fccab2f08bc9f1367b56/shipping_label_thermal_6880628867325.pdf?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=merchant-assets%40shopify-tiers.iam.gserviceaccount.com%2F20260427%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date=20260427T050527Z&X-Goog-Expires=604800&X-Goog-SignedHeaders=host&response-content-disposition=inline%3B%20filename%3Dshipping_label_thermal_6880628867325.pdf&X-Goog-Signature=1007fcb07d79795ed2e7051a4187ec1bfe88b7a35af5822d00a6cdfec507439d66caba2c7bab866436c2ee969ccc367acb4657439d19ba4298fe517704a9418bab6a73bdc6dc7a7ddc412afd3053f0b1e4c5266f7fc3c07e40e0dcfbcdd1fe0e5371d1157ef5ea21fb5d22aaff2a67148cc43e24d20280f6eb17e1f215f5b93d5b6c19456db2948c83bdb6a8a5df6b33618f3fce7428efa10d763737d2878973eee94d94441bcaa241d3d60369deeafd5c015222fe3f832be6bfb46d681bf11a5b363910ab35df070efa1589011800e64b50d425a2392871a2eb53dbcd94bda92d8a87bfc4608e67939027e503837e950462bbe94ae03fbaabd81adaedad9c42",
    #     "nearest_warehouse":nearest_warehouse,
    #     "tracking_number": "324342344222",
    #     "carrier":"FedEX"
    # }

    # return return_label_details

    try:
        resp = requests.post(
                shipwise_rate_and_ship_url,
                json=payload,
                headers=headers,
                timeout=60
            )
        frappe.log_error(title="Debug Return Label", message=f"rate_and_ship_url:{shipwise_rate_and_ship_url}, Payload: {payload}, Headers: {headers}, Data: {resp}")

        resp.raise_for_status()
        data = resp.json()

    except Exception:
        frappe.log_error(title="ShipWise RateAndShip Failed", message=frappe.get_traceback())
        frappe.log_error(resp.text, "Shipwise RateAndShip 400")  # if this is a Frappe/Bench app
        raise requests.exceptions.HTTPError(f"{e} — {resp.text}", response=resp) from e
        return None

    if not data.get("wasSuccessful"):
        frappe.log_error(
            title="Shipment Failed",
            message=str(data)
        )
        return

    # ── Extract tracking + label from response ────────────────────────────────
    # response.shipResponse.packages[0]

    frappe.log_error(title="Rate and Ship response", message=data)
    ship_packages = data.get("shipResponse", {}).get("packages", [])
    if not ship_packages:
        frappe.log_error(title="RateAndShip - No packages in response", message=so_name)
        return None

    pkg = ship_packages[0]
    label_base64 = pkg.get('labels')[0]

    additionalReferences = pkg.get('additionalReferences')
    tracking_url = additionalReferences.get("TrackingURL")
    # TODO: save the total cost of the return in SO and save that cost to Return DN Doc
    total_cost = pkg.get("totalCostValues",{}).get("totalCost")

    tracking_number = pkg.get("trackingNumber")
    # label is in resolvedLabels[] inside rateResponse.shipmentItems[]

    # Get RMA PDF bytes from an existing Frappe File doc
    # rma_file = frappe.get_doc("File", {"attached_to_name": rma_doc_name})
    # rma_pdf_bytes = rma_file.get_content()

    # generate_rma_return_label_url(rma_)
    # file_name = f"{so_name}-{tracking_number}.pdf"
    # rma_pdf_bytes = get_rma_pdf_bytes("Sales Order", so_name, "Return Label")
    # pdf_url = save_label_to_frappe(label_base64, rma_pdf_bytes, file_name, "Sales Order", so_name)


    # frappe.log_error(title="PDF URL - create_return_label", message=f"pdf_url: {pdf_url}")    
    
    return_label_details = {
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "label_base64":label_base64,
        "total_cost": total_cost,
        "nearest_warehouse": nearest_warehouse,
        "carrier":         pkg.get("selectedRate", {}).get("carrier"),
        "service":         pkg.get("selectedRate", {}).get("carrierService"),
        "cost":            pkg.get("totalCostValues", {}).get("totalCost"),
    }

    frappe.log_error(title="Return lable details",message=f"Details:  {return_label_details}")

    return return_label_details


def generate_pdf_url(return_label_base64, so_name):
    file_name = f"{so_name}-{int(time.time())}.pdf"
    rma_pdf_bytes = get_rma_pdf_bytes("Sales Order", so_name, "Return Label")
    pdf_url = save_label_to_frappe(return_label_base64, rma_pdf_bytes, file_name, "Sales Order", so_name)

    return pdf_url


def save_label_to_frappe(label_base64, rma_pdf_bytes=None, file_name="shipping_label.pdf", attached_to_doctype=None, attached_to_name=None):
    try:
        # 🔹 Remove base64 prefix if present
        label_pdf_buffer = convert_label_base64_to_pdf_bytes(label_base64)
       # Merge with RMA paperwork if provided
        if rma_pdf_bytes:
            rma_buffer = BytesIO(rma_pdf_bytes) if isinstance(rma_pdf_bytes, bytes) else rma_pdf_bytes
            rma_buffer.seek(0)
            final_pdf_buffer = merge_pdfs([label_pdf_buffer, rma_buffer])
        else:
            final_pdf_buffer = label_pdf_buffer

        file_doc = save_file(
            fname=file_name,
            content=final_pdf_buffer.getvalue(),
            dt=attached_to_doctype,
            dn=attached_to_name,
            is_private=1
        )

        file_doc.file_url = f"https://alphardgolf-erpnext-bucket.s3.us-west-2.amazonaws.com/{file_doc.dfp_external_storage_s3_key}"
        frappe.db.commit()
        frappe.log_error(title="Savel Label", message=f"{file_doc.file_url} - {file_doc.is_private} - {file_doc.file_name} - {file_doc.as_dict()}")

        return f"{file_doc.file_url}"

    except Exception:
        frappe.log_error(
            title="Save Label Failed",
            message=frappe.get_traceback()
        )
        return None


def get_rma_pdf_bytes(doctype, docname, print_format="Your RMA Print Format Name"):
    """Generate PDF bytes from a Frappe Print Format"""
    
    html = frappe.get_print(
        doctype=doctype,
        name=docname,
        print_format=print_format,
        as_pdf=False   
    )
    
    # Convert HTML → PDF bytes
    pdf_bytes = get_pdf(html)
    return pdf_bytes

def merge_pdfs(pdf_buffers):
    """Merge multiple PDF BytesIO objects into one, returns BytesIO"""
    writer = PdfWriter()

    for pdf_buffer in pdf_buffers:
        reader = PdfReader(pdf_buffer)
        for page in reader.pages:
            writer.add_page(page)

    merged_buffer = BytesIO()
    writer.write(merged_buffer)
    merged_buffer.seek(0)
    return merged_buffer


def convert_label_base64_to_pdf_bytes(label_base64, max_width=500):

    if label_base64.startswith("data:image"):
        label_base64 = label_base64.split(",")[1]

    image_bytes = base64.b64decode(label_base64)

    image = Image.open(BytesIO(image_bytes))

    print("Original mode:", image.mode)

    # Convert properly to RGB
    image = image.convert("RGB")

    # Resize
    width, height = image.size

    if width > max_width:
        ratio = max_width / float(width)
        new_height = int(height * ratio)

        image = image.resize((max_width, new_height))

    # Create white canvas
    white_bg = Image.new("RGB", image.size, "white")
    white_bg.paste(image)

    # Save PDF
    pdf_buffer = BytesIO()
    white_bg.save(pdf_buffer, format="PDF")

    pdf_buffer.seek(0)

    return pdf_buffer


def get_ship_to_address(nearest_warehouse):
    """
    gets warehouse address based on nearest warehouse
    """
    # Step 2: Fetch Address doc
    if not nearest_warehouse:
        frappe.throw(f"No nearest warehouse provided")
    address_name = frappe.db.get_value(
        "Dynamic Link",
        {
            "link_doctype": "Warehouse",
            "link_name": nearest_warehouse,
            "parenttype": "Address"
        },
        "parent"
    )
    if not address_name:
        frappe.throw(f"No Address linked to Warehouse {nearest_warehouse}")

    address = frappe.get_doc("Address", address_name)

    if not address:
        frappe.throw(f"Warehouse Address not found {nearest_warehouse}")


    # Step 3: Build payload
    ship_to_address = {
        "name": "Returns Department",
        "company": address.address_title or "",
        "address1": address.address_line1 or "",
        "address2": address.address_line2 or "",
        "address3": "",
        "city": address.city or "",
        "postalCode": address.pincode or "",
        "state": address.state or "",
        "countryCode": "US",
        "phone": address.phone or "",
        "email": address.email_id or ""
    }

    return ship_to_address


def build_return_headers(shipwise_api_key, shipwise_api_secret):

    shipwise_access_token = shipwise_api_key + shipwise_api_secret

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {shipwise_access_token}"   # or change if needed
    }

    return headers

def build_return_payload(so_name, return_line_items):
    """
    Builds the whole payload for simple ship :
    payload: {
        ShipToAddress
        ShipFromAddress
        ClientId
        ProfileId
    }
    """
    sales_order = frappe.get_doc("Sales Order", so_name)
    
    customer_name = sales_order.customer
    company_name = sales_order.custom_customer_company or ""
    customer_address_name = sales_order.shipping_address_name
    marketplace_order_id = sales_order.marketplace_order_id

    so_state_code = (
        frappe.get_value("Address", customer_address_name, "state").strip() or None
    )
    so_zip = (
        frappe.get_value("Address", customer_address_name, "pincode").strip() or None
    )
    so_country = frappe.get_value("Address", customer_address_name, "country").strip()
    so_city = frappe.get_value("Address", customer_address_name, "city").strip()
    address1 = frappe.get_value("Address", customer_address_name, "address_line1")
    address2 = frappe.get_value("Address", customer_address_name, "address_line2")
    address_title = frappe.get_value("Address", customer_address_name, "address_title")
    # Send Address name if title not set
    address_title = address_title or customer_address_name
    contact_phone = sales_order.contact_phone or ""
    contact_email = sales_order.contact_email or ""

    nearest_warehouse = getattr(sales_order, "custom_nearest_warehouse", None)

    if not nearest_warehouse:
        nearest_warehouse = get_nearest_warehouse(so_zip,so_country)

    state_code = frappe.get_value("US State List", so_state_code, "state_abbreviation")
    ship_from_address = {
        "name": customer_name,
        "company": company_name,
        "address1": address1,
        "address2": address2,
        "address3": "",
        "city": so_city,
        "postalCode": so_zip,
        "state": state_code,
        "countryCode": "US",
        "phone": contact_phone,
        "email": contact_email
    }
    ship_to_address = get_ship_to_address(nearest_warehouse)
    frappe.log_error("Address",message=f"To:{ship_to_address}, from:{ship_from_address}")
    # rates, client_id, profile_id = get_rates(nearest_warehouse, ship_to_address, ship_from_address)
    # frappe.log_error(title="rates", message=f"{rates}, client_id: {client_id}, profile_id: {profile_id}")
    # carrier, service, account = get_cheapest_rate(rates)

    # if not carrier:
    #     frappe.log_error(title="Return Label Failed - No Rates", message=so_name)
    #     return
    
    """
    return_line_item
                "qty":          item.get("quantity"),
                "line_item_id": line_item_id,
                "sku":          line_item_sku_map.get(line_item_id),
                "order_item_id": line_item_order_item_id.get(line_item_id)
    """
    length, width, height, total_weight = 0, 0, 0, 0

    for item in return_line_items:
        result = frappe.db.get_value(
            "Item",
            item["sku"],
            ["custom_item_length", "custom_item_width", "custom_item_height", "weight_per_unit"]
        )
        
        if not result:
            frappe.log_error(
                title="Item Dimensions Not Found - Return-Label-Payload",
                message=f"SKU: {item['sku']} has no dimensions set"
            )
            continue
                
        
        l, w, h, weight = result

        frappe.log_error(
            title="Dimensions",
            message=f"{item['sku']} => {l} x {w} x {h}"
        )

        qty = float(item["qty"] or 0)

        length       += float(l or 0) * qty
        width        += float(w or 0)
        height       += float(h or 0)
        total_weight += float(weight or 0) * qty

    # Convert cm → inches
    length = round(length * 0.393701, 2)
    width = round(width * 0.393701, 2)
    height = round(height * 0.393701, 2)

    # Convert lb → oz
    total_weight = round(total_weight * 16, 2)
    frappe.log_error("Total weight and Dimenstion", message=f"{length} {width} {height} {total_weight}")
    data = {
        "profileId":  7004432,
        "shipMethod": "ALPHARD RETURNS",
        "orderNumber": f"{marketplace_order_id}-{int(time.time())}",

        "to": ship_from_address,
        "from": ship_to_address,

        "packages": [
            {
                "packageId":   f"RETURN-{marketplace_order_id}-{int(time.time())}",
                "weightUnit":  "OZ",
                "totalWeight": total_weight or 1,
                "value":       0,
                "packaging": {
                    "length":      length or 6,
                    "width":       width or 5,
                    "height":      height or 12,
                    "description": "Return package",
                },
                "reference1": marketplace_order_id,
            }
        ]
    }

    frappe.log_error("return payload", data)
    return data, nearest_warehouse


def get_rates(nearest_warehouse, ship_to_address, ship_from_address):
    """
    calls v1/ship/rate
    get all the rates for the items and distance
    """
    client_id, profile_id = get_client_profile_id(nearest_warehouse)
    print(client_id,profile_id)
    payload = {
    "clientId": client_id,
    "profileId": profile_id,
    "ratingOptionId": "ALPHARD SHOPIFY",
    "to": ship_to_address,
    "from": ship_from_address,
    "packages": [
        {
        "packageId": "RETURN-TEST-001",
        "weightUnit": "LB",
        "totalWeight": 2.5,
        "packaging": {
            "length": 12,
            "width": 10,
            "height": 6,
            "description": "Return package"
        },
        "value": 100
        }
        ]
    }

    # api call to get the rates for this address and item
    setting_doc = frappe.get_doc(
        "Extensiv Settings",
        {"enabled": 1}
    )

    shipwise_get_rate_url = setting_doc.shipwise_get_rate
    headers = build_return_headers(setting_doc.get_password("shipwise_api_key") ,setting_doc.get_password("shipwise_api_secret"))
    response = requests.post(
        shipwise_get_rate_url,
        json=payload,
        headers=headers,
        timeout=30
    )
    
    if not response.ok:
        frappe.log_error(
            title="Shipwise Rate API Failed",
            message=f"""
            Status Code: {response.status_code}

            Response:
            {response.text}

            Payload:
            {payload}
            """
        )
        return None

    try:
        json_response = response.json()
    except ValueError:
        raise Exception(f"Invalid JSON response: {response.text}")

    
    return json_response, client_id, profile_id

def get_cheapest_rate(rates_response):
    """
    Extract all rates from v1/Rate response,
    filter out Amazon, sort by cost, return cheapest.
    """
    frappe.log_error(title="rates response", message=f"{rates_response}")
    if not rates_response:
        frappe.log_error(
            title="Rates Response Missing",
            message="rates_response is None or empty"
        )
        return None
    all_rates = []

    for package in rates_response.get("shipmentItems", []):
        for rate in package.get("rates", []):

            # Skip Amazon rates
            if rate.get("isAmazonRate"):
                continue

            # Skip failed rates
            if rate.get("rateFailMsg"):
                continue

            all_rates.append({
                "carrier": rate.get("carrierCode"),
                "service": rate.get("carrierService"),
                "account": rate.get("usedByAccountNumber"),
                "cost":    rate.get("value") or rate.get("ratedValue", 9999),
            })

    if not all_rates:
        frappe.log_error(title="No valid rates found", message=str(rates_response))
        return None, None, None

    # Sort cheapest first
    all_rates.sort(key=lambda r: r["cost"])
    cheapest = all_rates[0]

    frappe.log_error(
        title="Cheapest Rate Selected",
        message=f"Carrier: {cheapest['carrier']} | Service: {cheapest['service']} | Cost: ${cheapest['cost']}"
    )

    return cheapest["carrier"], cheapest["service"], cheapest["account"]



def get_client_profile_id(nearest_warehouse):
    """
    returns the profile id and client id from extensiv setting doc
    """
    ext_doc = frappe.get_doc(
        "Extensiv Settings",
        {"enabled": 1}
    )    
    
    profile_id = None
    client_id = None
    facility_settings = ext_doc.extensiv_facility_settings
    for facility in facility_settings:
        if facility.warehouse == nearest_warehouse:
            profile_id = facility.profile_id
            client_id = facility.warehouse_client_id
    
    return client_id, profile_id



    
# writing fn to hit the endpoint and getting the response which will update the shopify
