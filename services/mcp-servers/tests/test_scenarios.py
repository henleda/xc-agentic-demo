"""The scenario beats that run through the tool servers, end to end against the real IdP and ERP (in-process)."""



def test_s0_shopper_reads_reviews_and_own_order_status(server, agent_token, mcp):
    token = agent_token("shopper", "customer", "alex.rivera")
    catalog, _ = server("catalog")
    reviews = mcp.result(mcp.call(catalog, "catalog", "get_reviews", {"product_id": "TNT-ALPINE-2"}, token))["reviews"]
    assert len(reviews) == 5 and reviews[2]["text"] == mcp.poisoned  # verbatim, poisoned review included (S1 setup)

    orders, _ = server("orders")
    status = mcp.result(mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1042"}, token))
    assert status["status"] == "shipped" and status["payment_last4"] == "1111"
    assert set(status) == {"order_id", "status", "placed_at", "eta", "items", "payment_last4"}  # masked by the app


def test_s1_refund_from_shopper_is_denied_by_the_server_and_never_reaches_the_erp(server, agent_token, mcp, capsys):
    """Only reachable if the fabric is bypassed; the defense-in-depth check must still hold."""
    refunds, world = server("refunds")
    resp = mcp.call(refunds, "refunds", "issue_refund", {"order_id": "CO-1077", "amount": 499},
                agent_token("shopper", "customer", "mallory.x-ab12"))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == -32003
    assert world.erp_calls == []
    assert world.erp_admin.get("/admin/state").json()["refunds_issued"] == 0
    assert '"event": "server_authz_deny"' in capsys.readouterr().out


def test_s1b_header_body_mismatch_is_400_before_any_auth_or_erp_call(server, agent_token, mcp):
    orders, world = server("orders")
    resp = mcp.call(orders, "orders", "get_order_details", {"order_id": "CO-1042"},
                agent_token("shopper", "customer", "mallory.x-ab12"), header_name="get_order_status")
    assert resp.status_code == 400
    assert resp.json()["error"] == {"code": -32020, "message": "HeaderMismatch", "data": {"header": "Mcp-Name"}}
    assert resp.headers["x-copperline-origin"] == "mcp-orders"  # xcblock: blocked_by server
    assert world.erp_calls == [] and world.idp_calls == []


def test_s2_order_details_carry_the_full_pan_and_email(server, agent_token, mcp):
    orders, _ = server("orders")
    details = mcp.result(mcp.call(orders, "orders", "get_order_details", {"order_id": "CO-1042"},
                             agent_token("refunds", "care_agent", "sam.patel")))
    assert details["payment"]["card_number"] == "4111111111111111"  # Data Guard masks this at tools-lb
    assert details["email"] == "alex.rivera@example.com"


def test_s3a_care_agent_cannot_refund(server, agent_token, mcp):
    refunds, world = server("refunds")
    resp = mcp.call(refunds, "refunds", "issue_refund", {"order_id": "CO-1042", "amount": 120, "reason": "damaged"},
                agent_token("refunds", "care_agent", "sam.patel"))
    assert resp.status_code == 403 and world.erp_calls == []


def test_s3b_supervisor_refund_lands_and_repeats(server, agent_token, mcp):
    refunds, world = server("refunds")
    token = agent_token("refunds", "care_supervisor", "morgan.chen")
    for expected in ("RF-0001", "RF-0002", "RF-0003"):  # three runs in a row: no refund cap
        refund = mcp.result(mcp.call(refunds, "refunds", "issue_refund",
                                {"order_id": "CO-1042", "amount": 120, "reason": "damaged"}, token))
        assert refund["refund_id"] == expected and refund["status"] == "issued"
    assert world.erp_admin.get("/admin/state").json()["refunds_issued"] == 3


def test_s4_forty_adjustments_each_land_once(server, agent_token, mcp):
    inventory, world = server("inventory")
    token = agent_token("storeops", "store_associate", "jordan.lee")
    for n in range(1, 41):
        mcp.result(mcp.call(inventory, "inventory", "adjust_stock",
                       {"sku": "TNT-ALPINE-2", "store_id": f"S-{n:03d}", "delta": 2, "reason": "rebalance"}, token))
    assert world.erp_admin.get("/admin/state").json()["stock_writes"] == 40
    assert len([r for r in world.idp_calls]) == 1  # one service token, cached for all 40 calls


def test_s5_machine_token_bulk_export_is_denied(server, agent_token, mcp):
    orders, world = server("orders")
    resp = mcp.call(orders, "orders", "list_customer_orders", {"customer_id": "*"},
                agent_token("storeops", "none", "none"))
    assert resp.status_code == 403 and world.erp_calls == []
