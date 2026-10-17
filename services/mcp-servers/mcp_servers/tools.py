"""Tool handlers for the four servers. Each returns a dict that becomes structuredContent (README "Tool results").

Handlers receive validated arguments (mcp_router checks them against tools.yaml) and the request context, whose
claims were already checked by authz.py. Nothing here logs tool results: they hold PANs and emails.
"""

from copperline_common.mcp import McpRequestContext, ToolError
from copperline_common.personas import Personas

from mcp_servers.erp import ErpClient, segment

SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 25
NOT_YOUR_ORDER = "not your order"


# ---- catalog (RE, no ERP) -------------------------------------------------------------------------------------


def catalog_handlers(products: list[dict]) -> dict:
    by_sku = {p["sku"]: p for p in products}

    async def search_products(args: dict, ctx: McpRequestContext) -> dict:
        query = args["query"].strip().lower()
        limit = max(1, min(int(args.get("limit") or SEARCH_DEFAULT_LIMIT), SEARCH_MAX_LIMIT))
        hits = [p for p in products if query in f"{p['name']} {p['category']} {p['description']}".lower()]
        results = [{key: p[key] for key in ("sku", "name", "category", "price")} for p in hits[:limit]]
        return {"query": args["query"], "results": results}

    async def get_product(args: dict, ctx: McpRequestContext) -> dict:
        product = _product(by_sku, args["product_id"])
        ratings = [r["rating"] for r in product["reviews"]]
        return {
            **{key: product[key] for key in ("sku", "name", "category", "price", "description")},
            "review_count": len(ratings),
            "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        }

    async def get_reviews(args: dict, ctx: McpRequestContext) -> dict:
        # Verbatim, including the poisoned review (S1): content inspection is not this server's job.
        product = _product(by_sku, args["product_id"])
        return {"product_id": product["sku"], "reviews": product["reviews"]}

    return {"search_products": search_products, "get_product": get_product, "get_reviews": get_reviews}


def _product(by_sku: dict, product_id: str) -> dict:
    product = by_sku.get(product_id)
    if product is None:
        raise ToolError("product not found")
    return product


# ---- orders ---------------------------------------------------------------------------------------------------


def orders_handlers(erp: ErpClient, personas: Personas) -> dict:
    async def own_order(order_id: str, ctx: McpRequestContext) -> dict:
        """Fetch an order, applying the object-level rule for customers (README "Object-level authorization")."""
        is_customer = ctx.claims.get("obo_role") == "customer"
        if is_customer:
            customer_id = personas.customer_id(str(ctx.claims.get("obo_sub", "")))
            if not customer_id:
                raise ToolError(NOT_YOUR_ORDER)
        try:
            order = await erp.request("GET", f"/orders/{segment(order_id)}", what="order")
        except ToolError:
            if is_customer:
                raise ToolError(NOT_YOUR_ORDER) from None  # don't reveal whether the order exists
            raise
        if is_customer and order.get("customer_id") != customer_id:
            raise ToolError(NOT_YOUR_ORDER)
        return order

    async def get_order_status(args: dict, ctx: McpRequestContext) -> dict:
        order = await own_order(args["order_id"], ctx)
        card = str(order.get("payment", {}).get("card_number", ""))
        return {
            "order_id": order["order_id"],
            "status": order["status"],
            "placed_at": order.get("placed_at"),
            "eta": order.get("eta"),
            "items": [{key: item[key] for key in ("sku", "name", "qty")} for item in order.get("items", [])],
            "payment_last4": card[-4:] if card else None,
        }

    async def get_order_details(args: dict, ctx: McpRequestContext) -> dict:
        # The full ERP order on purpose: S2's Data Guard masks the PAN at tools-lb, not here.
        return await own_order(args["order_id"], ctx)

    async def list_customer_orders(args: dict, ctx: McpRequestContext) -> dict:
        customer_id = args["customer_id"]
        path = "/customers/*/orders" if customer_id == "*" else f"/customers/{segment(customer_id)}/orders"
        body = await erp.request("GET", path, what="customer")
        orders = body.get("orders", [])
        return {"customer_id": customer_id, "count": len(orders), "orders": orders}

    return {
        "get_order_status": get_order_status,
        "get_order_details": get_order_details,
        "list_customer_orders": list_customer_orders,
    }


# ---- inventory ------------------------------------------------------------------------------------------------


def inventory_handlers(erp: ErpClient) -> dict:
    async def check_stock(args: dict, ctx: McpRequestContext) -> dict:
        path = f"/inventory/{segment(args['sku'])}"
        if args.get("store_id"):
            path += f"?store_id={segment(args['store_id'])}"
        return await erp.request("GET", path, what="sku or store")

    async def adjust_stock(args: dict, ctx: McpRequestContext) -> dict:
        body = {"sku": args["sku"], "store_id": args["store_id"], "delta": args["delta"],
                "reason": args.get("reason") or "agent adjustment"}
        return await erp.request("POST", "/inventory/adjust", body, what="sku or store")

    async def transfer_stock(args: dict, ctx: McpRequestContext) -> dict:
        body = {key: args[key] for key in ("sku", "from_store", "to_store", "qty")}
        return await erp.request("POST", "/inventory/transfer", body, what="sku or store")

    return {"check_stock": check_stock, "adjust_stock": adjust_stock, "transfer_stock": transfer_stock}


# ---- refunds --------------------------------------------------------------------------------------------------


def refunds_handlers(erp: ErpClient) -> dict:
    async def issue_refund(args: dict, ctx: McpRequestContext) -> dict:
        body = {"order_id": args["order_id"], "amount": args["amount"], "reason": args.get("reason") or "agent refund"}
        return await erp.request("POST", "/refunds", body, what="order")

    async def get_refund_status(args: dict, ctx: McpRequestContext) -> dict:
        return await erp.request("GET", f"/refunds/{segment(args['refund_id'])}", what="refund")

    return {"issue_refund": issue_refund, "get_refund_status": get_refund_status}
