"""CRM, support, payments and e-commerce integrations."""

import time

import httpx

from ..declarative import AfterId, Cursor, F, Link, NextUrl, Offset, Page, op, service  # noqa: F401

CRM = dict(group="crm")
SHOP = dict(group="commerce")

# ---- HubSpot (CRM v3 objects) -------------------------------------------------


def _hubspot_object(obj: str, label: str, props: list[F]) -> list:
    base = f"/crm/v3/objects/{obj}"
    oid = F(f"{obj}_id", f"{label} id", req=True)
    return [
        op(obj, "create", "Create", "POST", base, *props, F("properties", "More properties (JSON)", type="json"),
           body=lambda v: {"properties": {**{k: x for k, x in v.items() if k != "properties"}, **(v.get("properties") or {})}}),
        op(obj, "get", "Get", "GET", f"{base}/{{{obj}_id}}", oid, F("properties", "Properties to return", type="list")),
        op(obj, "update", "Update", "PATCH", f"{base}/{{{obj}_id}}", oid, *[F(f.name, f.label) for f in props],
           F("properties", "More properties (JSON)", type="json"),
           body=lambda v, obj=obj: {"properties": {**{k: x for k, x in v.items() if k not in ("properties", f"{obj}_id")}, **(v.get("properties") or {})}}),
        op(obj, "delete", "Delete", "DELETE", f"{base}/{{{obj}_id}}", oid),
        op(obj, "list", "Get many", "GET", base, F("properties", "Properties to return", type="list"), items="results",
           paginate=Cursor("after", "paging.next.after", size_param="limit", size=100)),
        op(obj, "search", "Search", "POST", f"{base}/search", F("query", desc="Free-text search"),
           F("filterGroups", "Filter groups (JSON)", type="json"), items="results",
           paginate=Cursor("after", "paging.next.after", location="body", size_param="limit")),
    ]


service("hubspot", "HubSpot", ["hubspot", "hubspot_oauth2"], "https://api.hubapi.com", color="#ff7a59", icon="users", **CRM, ops=[
    *_hubspot_object("contacts", "Contact", [F("email"), F("firstname", "First name"), F("lastname", "Last name"), F("phone"), F("company")]),
    *_hubspot_object("companies", "Company", [F("name"), F("domain"), F("industry"), F("phone")]),
    *_hubspot_object("deals", "Deal", [F("dealname", "Deal name"), F("amount"), F("dealstage", "Stage"), F("pipeline"), F("closedate", "Close date")]),
    *_hubspot_object("tickets", "Ticket", [F("subject"), F("content", type="text"), F("hs_pipeline_stage", "Stage"), F("hs_ticket_priority", "Priority")]),
])

# ---- Pipedrive ---------------------------------------------------------------


def _pd(res: str, label: str, fields: list[F]) -> list:
    rid = F("id", f"{label} id", type="number", req=True)
    pg = Cursor("start", "additional_data.pagination.next_start", size_param="limit", size=100)
    return [
        op(res, "create", "Create", "POST", f"/{res}", *fields, out="data"),
        op(res, "get", "Get", "GET", f"/{res}/{{id}}", rid, out="data"),
        op(res, "update", "Update", "PUT", f"/{res}/{{id}}", rid, *[F(f.name, f.label, f.type) for f in fields], out="data"),
        op(res, "delete", "Delete", "DELETE", f"/{res}/{{id}}", rid, out="data"),
        op(res, "list", "Get many", "GET", f"/{res}", items="data", paginate=pg),
    ]


service("pipedrive", "Pipedrive", ["pipedrive"], "https://api.pipedrive.com/v1", color="#1a1a1a", icon="handshake", **CRM, ops=[
    *_pd("persons", "Person", [F("name", req=True), F("email", type="list"), F("phone", type="list"), F("org_id", "Organization id", type="number")]),
    *_pd("organizations", "Organization", [F("name", req=True), F("address")]),
    *_pd("deals", "Deal", [F("title", req=True), F("value", type="number"), F("currency"), F("person_id", type="number"), F("org_id", type="number"),
                           F("stage_id", type="number"), F("status", type="options", options=["", "open", "won", "lost"])]),
    *_pd("activities", "Activity", [F("subject", req=True), F("type", default="call"), F("due_date"), F("deal_id", type="number"),
                                    F("person_id", type="number"), F("note", type="text")]),
    op("notes", "create", "Create note", "POST", "/notes", F("content", type="text", req=True), F("deal_id", type="number"),
       F("person_id", type="number"), F("org_id", type="number"), out="data"),
    op("persons", "search", "Search persons", "GET", "/persons/search", F("term", req=True), F("fields", default="email,name"), items="data.items"),
])

# ---- Support desks ---------------------------------------------------------------
TID = F("ticket_id", type="number", req=True)
service("zendesk", "Zendesk", ["zendesk"], "https://{subdomain}.zendesk.com/api/v2", color="#03363d", icon="life-buoy", **CRM, ops=[
    op("ticket", "create", "Create", "POST", "/tickets", F("subject", req=True), F("comment", "Description", type="text", req=True, to="comment.body"),
       F("priority", type="options", options=["", "urgent", "high", "normal", "low"]), F("type", type="options", options=["", "problem", "incident", "question", "task"]),
       F("requester_email", "Requester email", to="requester.email"), F("tags", type="list"), wrap="ticket", out="ticket"),
    op("ticket", "get", "Get", "GET", "/tickets/{ticket_id}", TID, out="ticket"),
    op("ticket", "update", "Update", "PUT", "/tickets/{ticket_id}", TID, F("status", type="options", options=["", "open", "pending", "solved", "closed"]),
       F("priority", type="options", options=["", "urgent", "high", "normal", "low"]), F("comment", "Add comment", type="text", to="comment.body"),
       F("public", "Comment is public", type="boolean", to="comment.public"), F("tags", type="list"), wrap="ticket", out="ticket"),
    op("ticket", "delete", "Delete", "DELETE", "/tickets/{ticket_id}", TID),
    op("ticket", "list", "Get many", "GET", "/tickets", items="tickets", paginate=NextUrl("next_page")),
    op("search", "search", "Search", "GET", "/search", F("query", req=True, desc="e.g. type:ticket status:open"), items="results",
       paginate=NextUrl("next_page")),
    op("user", "create", "Create user", "POST", "/users", F("name", req=True), F("email"), F("role", type="options", options=["end-user", "agent", "admin"],
       default="end-user"), wrap="user", out="user"),
    op("user", "list", "Get many users", "GET", "/users", items="users", paginate=NextUrl("next_page")),
])
service("freshdesk", "Freshdesk", ["freshdesk"], "https://{domain}.freshdesk.com/api/v2", color="#25c16f", icon="life-buoy", **CRM, ops=[
    op("ticket", "create", "Create", "POST", "/tickets", F("email", "Requester email", req=True), F("subject", req=True), F("description", type="text", req=True),
       F("priority", type="number", default=1, desc="1 low … 4 urgent"), F("status", type="number", default=2, desc="2 open, 3 pending, 4 resolved, 5 closed"),
       F("tags", type="list")),
    op("ticket", "get", "Get", "GET", "/tickets/{id}", F("id", "Ticket id", type="number", req=True)),
    op("ticket", "update", "Update", "PUT", "/tickets/{id}", F("id", "Ticket id", type="number", req=True), F("status", type="number"),
       F("priority", type="number")),
    op("ticket", "delete", "Delete", "DELETE", "/tickets/{id}", F("id", "Ticket id", type="number", req=True)),
    op("ticket", "list", "Get many", "GET", "/tickets", paginate=Page(size_param="per_page", size=100)),
    op("ticket", "reply", "Reply", "POST", "/tickets/{id}/reply", F("id", "Ticket id", type="number", req=True), F("body", type="text", req=True)),
    op("contact", "create", "Create contact", "POST", "/contacts", F("name", req=True), F("email"), F("phone")),
    op("contact", "list", "Get many contacts", "GET", "/contacts", paginate=Page(size_param="per_page", size=100)),
])
service("intercom", "Intercom", ["intercom"], "https://api.intercom.io", color="#286efa", icon="message-circle", **CRM, ops=[
    op("contact", "create", "Create", "POST", "/contacts", F("role", type="options", options=["user", "lead"], default="user"), F("email"), F("name"),
       F("external_id"), F("phone")),
    op("contact", "get", "Get", "GET", "/contacts/{id}", F("id", "Contact id", req=True)),
    op("contact", "update", "Update", "PUT", "/contacts/{id}", F("id", "Contact id", req=True), F("email"), F("name"), F("phone")),
    op("contact", "list", "Get many", "GET", "/contacts", items="data", paginate=Cursor("starting_after", "pages.next.starting_after", size_param="per_page", size=150)),
    op("contact", "search", "Search", "POST", "/contacts/search", F("query", type="json", req=True, desc='{"field": "email", "operator": "=", "value": "a@b.c"}'),
       items="data"),
    op("message", "send", "Send message", "POST", "/messages", F("admin_id", "From admin id", req=True), F("contact_id", "To contact id", req=True),
       F("body", type="text", req=True), F("subject"), F("message_type", type="options", options=["inapp", "email"], default="inapp"),
       body=lambda v: {"message_type": v.get("message_type") or "inapp", "body": v["body"], **({"subject": v["subject"]} if v.get("subject") else {}),
                       "from": {"type": "admin", "id": str(v["admin_id"])}, "to": {"type": "user", "id": v["contact_id"]}}),
    op("conversation", "list", "Get many conversations", "GET", "/conversations", items="conversations",
       paginate=Cursor("starting_after", "pages.next.starting_after", size_param="per_page", size=150)),
])
service("zoho_crm", "Zoho CRM", ["zoho_crm"], "https://www.zohoapis.{dc}/crm/v2", color="#e42527", icon="users", **CRM, ops=[
    op("record", "create", "Create", "POST", "/{module}", F("module", req=True, default="Leads", desc="Leads, Contacts, Accounts, Deals…"),
       F("record", "Fields (JSON)", type="json", req=True), body=lambda v: {"data": [v["record"]]}, out="data.0"),
    op("record", "get", "Get", "GET", "/{module}/{id}", F("module", req=True, default="Leads"), F("id", "Record id", req=True), out="data.0"),
    op("record", "update", "Update", "PUT", "/{module}/{id}", F("module", req=True, default="Leads"), F("id", "Record id", req=True),
       F("record", "Fields (JSON)", type="json", req=True), body=lambda v: {"data": [v["record"]]}, out="data.0"),
    op("record", "delete", "Delete", "DELETE", "/{module}/{id}", F("module", req=True, default="Leads"), F("id", "Record id", req=True)),
    op("record", "list", "Get many", "GET", "/{module}", F("module", req=True, default="Leads"), items="data", paginate=Page(size_param="per_page", size=200)),
    op("record", "search", "Search", "GET", "/{module}/search", F("module", req=True, default="Leads"), F("criteria", desc="(Email:equals:a@b.c)"),
       F("email"), F("word"), items="data"),
])

# ---- Salesforce (instance URL comes with the OAuth token) ---------------------


def _sf_base(cred: dict) -> str:
    from ...credentials import OAUTH_TOKEN_KEY

    url = ((cred.get(OAUTH_TOKEN_KEY) or {}).get("instance_url") or "").rstrip("/")
    if not url:
        from ..base import NodeError

        raise NodeError("Connect the Salesforce credential first (its OAuth token carries your instance URL)")
    return f"{url}/services/data/v62.0"


SOBJ = F("sobject", "Object", req=True, default="Lead", desc="Lead, Contact, Account, Opportunity, Case…")
service("salesforce", "Salesforce", ["salesforce_oauth2"], _sf_base, color="#00a1e0", icon="cloud", **CRM, ops=[
    op("record", "create", "Create", "POST", "/sobjects/{sobject}", SOBJ, F("record", "Fields (JSON)", type="json", req=True), body=lambda v: v["record"]),
    op("record", "get", "Get", "GET", "/sobjects/{sobject}/{id}", SOBJ, F("id", "Record id", req=True)),
    op("record", "update", "Update", "PATCH", "/sobjects/{sobject}/{id}", SOBJ, F("id", "Record id", req=True), F("record", "Fields (JSON)", type="json", req=True),
       body=lambda v: v["record"]),
    op("record", "upsert", "Upsert by external id", "PATCH", "/sobjects/{sobject}/{external_field}/{external_id}", SOBJ, F("external_field", req=True),
       F("external_id", req=True), F("record", "Fields (JSON)", type="json", req=True), body=lambda v: v["record"]),
    op("record", "delete", "Delete", "DELETE", "/sobjects/{sobject}/{id}", SOBJ, F("id", "Record id", req=True)),
    op("query", "soql", "SOQL query", "GET", "/query", F("q", "SOQL", req=True, default="SELECT Id, Name FROM Account LIMIT 10"), items="records",
       paginate=NextUrl("nextRecordsUrl")),
])

# ---- Stripe (form-encoded) -----------------------------------------------------
_sp = AfterId()


def _stripe(res: str, label: str, fields: list[F], *, update=True, delete=True) -> list:
    rid = F("id", f"{label} id", req=True)
    ops = [op(res, "create", "Create", "POST", f"/{res}", *fields, send="form"),
           op(res, "get", "Get", "GET", f"/{res}/{{id}}", rid),
           op(res, "list", "Get many", "GET", f"/{res}", items="data", paginate=_sp)]
    if update:
        ops.append(op(res, "update", "Update", "POST", f"/{res}/{{id}}", rid, *[F(f.name, f.label, f.type) for f in fields], send="form"))
    if delete:
        ops.append(op(res, "delete", "Delete", "DELETE", f"/{res}/{{id}}", rid))
    return ops


service("stripe", "Stripe", ["stripe"], "https://api.stripe.com/v1", color="#635bff", icon="credit-card", **SHOP, ops=[
    *_stripe("customers", "Customer", [F("email"), F("name"), F("phone"), F("description"), F("metadata", type="json")]),
    *_stripe("products", "Product", [F("name", req=True), F("description"), F("active", type="boolean", default=True)]),
    *_stripe("prices", "Price", [F("product", "Product id", req=True), F("unit_amount", "Amount (cents)", type="number", req=True),
                                 F("currency", req=True, default="usd"), F("recurring_interval", type="options", options=["", "day", "week", "month", "year"],
                                                                         to="recurring.interval")], update=False, delete=False),
    *_stripe("payment_intents", "Payment intent", [F("amount", "Amount (cents)", type="number", req=True), F("currency", req=True, default="usd"),
                                                   F("customer"), F("description"), F("metadata", type="json")], delete=False),
    *_stripe("invoices", "Invoice", [F("customer", "Customer id", req=True), F("description"), F("collection_method",
                                     type="options", options=["charge_automatically", "send_invoice"]), F("days_until_due", type="number")]),
    op("subscriptions", "list", "Get many", "GET", "/subscriptions", F("customer"), F("status"), items="data", paginate=_sp),
    op("subscriptions", "cancel", "Cancel", "DELETE", "/subscriptions/{id}", F("id", "Subscription id", req=True)),
    op("refunds", "create", "Create refund", "POST", "/refunds", F("payment_intent", req=True), F("amount", "Amount (cents, empty = all)", type="number"),
       send="form"),
    op("charges", "list", "Get many charges", "GET", "/charges", F("customer"), items="data", paginate=_sp),
    op("balance", "get", "Get balance", "GET", "/balance"),
])

# ---- PayPal (client-credentials token) ---------------------------------------
_PP: dict[str, tuple[str, float]] = {}


def _pp_base(cred: dict) -> str:
    return "https://api-m.sandbox.paypal.com" if cred.get("environment") == "sandbox" else "https://api-m.paypal.com"


async def _pp_auth(cred: dict):
    import hashlib

    from ..base import NodeError

    if not cred.get("client_id") or not cred.get("client_secret"):
        raise NodeError("The PayPal credential needs a client id and a client secret")
    # keyed by the secret too: a credential with the right client id but a wrong secret never reuses a token
    key = hashlib.sha256(f"{cred.get('client_id')}\0{cred.get('client_secret')}\0{cred.get('environment')}".encode()).hexdigest()
    tok, exp = _PP.get(key, ("", 0))
    if time.time() > exp - 60:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{_pp_base(cred)}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                             auth=(cred["client_id"], cred["client_secret"]))
        if r.status_code >= 400:
            raise NodeError(f"PayPal authentication failed: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict) or not data.get("access_token"):
            raise NodeError(f"PayPal authentication failed: no access_token in the answer ({r.text[:200]})")
        tok, exp = data["access_token"], time.time() + float(data.get("expires_in") or 3000)
        _PP[key] = (tok, exp)
    return {"Authorization": f"Bearer {tok}"}, {}


service("paypal", "PayPal", ["paypal"], _pp_base, auth=_pp_auth, color="#003087", icon="wallet", **SHOP, ops=[
    op("order", "create", "Create order", "POST", "/v2/checkout/orders", F("amount", req=True), F("currency", default="USD", req=True),
       F("intent", type="options", options=["CAPTURE", "AUTHORIZE"], default="CAPTURE"), F("description"),
       body=lambda v: {"intent": v.get("intent") or "CAPTURE", "purchase_units": [{"amount": {"currency_code": v["currency"], "value": str(v["amount"])},
                                                                                    **({"description": v["description"]} if v.get("description") else {})}]}),
    op("order", "get", "Get order", "GET", "/v2/checkout/orders/{id}", F("id", "Order id", req=True)),
    op("order", "capture", "Capture order", "POST", "/v2/checkout/orders/{id}/capture", F("id", "Order id", req=True), extra=False),
    op("payout", "create", "Create payout", "POST", "/v1/payments/payouts", F("receiver", "Receiver email", req=True), F("amount", req=True),
       F("currency", default="USD", req=True), F("note"),
       body=lambda v: {"sender_batch_header": {"sender_batch_id": f"db-{int(time.time() * 1000)}", "email_subject": "You have a payout"},
                       "items": [{"recipient_type": "EMAIL", "receiver": v["receiver"], "note": v.get("note") or "",
                                  "amount": {"value": str(v["amount"]), "currency": v["currency"]}}]}),
])

# ---- Shopify / WooCommerce / Razorpay --------------------------------------------
service("shopify", "Shopify", ["shopify"], "https://{shop}.myshopify.com/admin/api/2024-10", color="#95bf47", icon="shopping-bag", **SHOP, ops=[
    op("product", "create", "Create", "POST", "/products.json", F("title", req=True), F("body_html", "Description (HTML)", type="text"), F("vendor"),
       F("product_type"), F("tags"), F("status", type="options", options=["", "active", "draft", "archived"]), wrap="product", out="product"),
    op("product", "get", "Get", "GET", "/products/{id}.json", F("id", "Product id", req=True), out="product"),
    op("product", "update", "Update", "PUT", "/products/{id}.json", F("id", "Product id", req=True), F("title"), F("body_html", "Description (HTML)", type="text"),
       F("tags"), F("status", type="options", options=["", "active", "draft", "archived"]), wrap="product", out="product"),
    op("product", "delete", "Delete", "DELETE", "/products/{id}.json", F("id", "Product id", req=True)),
    op("product", "list", "Get many", "GET", "/products.json", items="products", paginate=Link()),
    op("order", "get", "Get order", "GET", "/orders/{id}.json", F("id", "Order id", req=True), out="order"),
    op("order", "list", "Get many orders", "GET", "/orders.json", F("status", type="options", options=["any", "open", "closed", "cancelled"], default="any"),
       items="orders", paginate=Link()),
    op("order", "update", "Update order", "PUT", "/orders/{id}.json", F("id", "Order id", req=True), F("note", type="text"), F("tags"), F("email"),
       wrap="order", out="order"),
    op("customer", "create", "Create customer", "POST", "/customers.json", F("email"), F("first_name"), F("last_name"), F("phone"), F("tags"),
       wrap="customer", out="customer"),
    op("customer", "list", "Get many customers", "GET", "/customers.json", items="customers", paginate=Link()),
])
WC_PG = Page(size_param="per_page", size=100)
service("woocommerce", "WooCommerce", ["woocommerce"], "{url}/wp-json/wc/v3", color="#96588a", icon="shopping-cart", **SHOP, ops=[
    op("product", "create", "Create", "POST", "/products", F("name", req=True), F("type", default="simple"), F("regular_price"), F("description", type="text"),
       F("sku"), F("stock_quantity", type="number"), F("status", type="options", options=["", "publish", "draft", "pending", "private"])),
    op("product", "get", "Get", "GET", "/products/{id}", F("id", "Product id", req=True)),
    op("product", "update", "Update", "PUT", "/products/{id}", F("id", "Product id", req=True), F("name"), F("regular_price"), F("stock_quantity", type="number"),
       F("status", type="options", options=["", "publish", "draft"])),
    op("product", "delete", "Delete", "DELETE", "/products/{id}", F("id", "Product id", req=True), F("force", type="boolean")),
    op("product", "list", "Get many", "GET", "/products", F("search"), paginate=WC_PG),
    op("order", "create", "Create order", "POST", "/orders", F("line_items", type="json", req=True, desc='[{"product_id": 93, "quantity": 2}]'),
       F("billing", type="json"), F("status", default="pending"), F("set_paid", type="boolean")),
    op("order", "get", "Get order", "GET", "/orders/{id}", F("id", "Order id", req=True)),
    op("order", "update", "Update order", "PUT", "/orders/{id}", F("id", "Order id", req=True), F("status", type="options",
       options=["", "pending", "processing", "on-hold", "completed", "cancelled", "refunded"])),
    op("order", "list", "Get many orders", "GET", "/orders", F("status"), paginate=WC_PG),
    op("customer", "create", "Create customer", "POST", "/customers", F("email", req=True), F("first_name"), F("last_name"), F("username")),
    op("customer", "list", "Get many customers", "GET", "/customers", F("email"), paginate=WC_PG),
])
service("razorpay", "Razorpay", ["razorpay"], "https://api.razorpay.com/v1", color="#0c2451", icon="indian-rupee", **SHOP, ops=[
    op("order", "create", "Create order", "POST", "/orders", F("amount", "Amount (paise)", type="number", req=True), F("currency", default="INR", req=True),
       F("receipt"), F("notes", type="json")),
    op("order", "get", "Get order", "GET", "/orders/{id}", F("id", "Order id", req=True)),
    op("order", "list", "Get many orders", "GET", "/orders", items="items", paginate=Offset(param="skip", size_param="count", size=100)),
    op("payment", "get", "Get payment", "GET", "/payments/{id}", F("id", "Payment id", req=True)),
    op("payment", "list", "Get many payments", "GET", "/payments", items="items", paginate=Offset(param="skip", size_param="count", size=100)),
    op("payment", "capture", "Capture payment", "POST", "/payments/{id}/capture", F("id", "Payment id", req=True),
       F("amount", "Amount (paise)", type="number", req=True), F("currency", default="INR", req=True)),
    op("refund", "create", "Refund payment", "POST", "/payments/{id}/refund", F("id", "Payment id", req=True), F("amount", "Amount (paise, empty = full)", type="number")),
    op("customer", "create", "Create customer", "POST", "/customers", F("name", req=True), F("email"), F("contact", "Phone")),
    op("payment_link", "create", "Create payment link", "POST", "/payment_links", F("amount", "Amount (paise)", type="number", req=True),
       F("currency", default="INR", req=True), F("description"), F("customer", type="json", desc='{"name": "Ann", "email": "a@b.c"}')),
])
