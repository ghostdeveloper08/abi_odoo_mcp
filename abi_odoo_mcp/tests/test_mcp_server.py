# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from datetime import datetime, timedelta
import base64
import hashlib
import json
from odoo import fields
from odoo.exceptions import AccessDenied, UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase
from odoo.addons.abi_odoo_mcp.controllers.main import McpController


class TestMcpServer(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Use standard demo user or admin
        demo_user = cls.env["res.users"].search([("login", "=", "demo")], limit=1)
        if not demo_user:
            demo_user = cls.env.ref("base.user_demo", raise_if_not_found=False)
        if not demo_user:
            demo_user = cls.env.ref("base.user_admin")
        cls.test_user = demo_user

        # Create active MCP token for test user
        cls.token_rec = cls.env["mcp.access.token"].create({
            "name": "Integration Test Token",
            "user_id": cls.test_user.id,
            "is_readonly": False,
        })
        cls.raw_token = cls.token_rec.raw_token
        cls.controller = McpController()

    def test_01_token_generation_and_verification(self):
        """Verify token generation, SHA-256 hash storage, and successful verification."""
        self.assertTrue(self.raw_token, "Raw token must be available immediately after creation.")
        self.assertTrue(self.raw_token.startswith("mcp_live_"), "Token must begin with 'mcp_live_'.")
        self.assertTrue(self.token_rec.token_prefix, "Token prefix must be populated.")

        # Compute hash and verify DB stored hash
        expected_hash = hashlib.sha256(self.raw_token.encode("utf-8")).hexdigest()
        self.assertEqual(self.token_rec.token_hash, expected_hash, "Token hash must match SHA-256 computation.")

        # Verify token lookup
        verified_token = self.env["mcp.access.token"].verify_token(
            env=self.env,
            raw_token=self.raw_token,
            client_ip="127.0.0.1",
        )
        self.assertEqual(verified_token.id, self.token_rec.id, "Verified token ID must match created token.")
        self.assertEqual(verified_token.user_id.id, self.test_user.id, "Verified token owner must be test user.")

    def test_02_token_revocation(self):
        """Ensure revoked tokens fail verification immediately."""
        self.token_rec.action_revoke()
        self.assertFalse(self.token_rec.active, "Token must be inactive after revocation.")

        with self.assertRaises(AccessDenied):
            self.env["mcp.access.token"].verify_token(
                env=self.env,
                raw_token=self.raw_token,
                client_ip="127.0.0.1",
            )

    def test_03_token_expiration(self):
        """Ensure expired tokens are rejected by verification."""
        # Create token with expiration in the past
        past_date = fields.Datetime.now() - timedelta(days=1)
        expired_token = self.env["mcp.access.token"].create({
            "name": "Expired Test Token",
            "user_id": self.test_user.id,
            "expires_at": past_date,
        })
        expired_raw = expired_token.raw_token

        with self.assertRaises(AccessDenied):
            self.env["mcp.access.token"].verify_token(
                env=self.env,
                raw_token=expired_raw,
                client_ip="127.0.0.1",
            )

    def test_04_ip_range_restriction(self):
        """Ensure IP address and CIDR range restrictions are strictly enforced."""
        ip_token = self.env["mcp.access.token"].create({
            "name": "IP Restricted Token",
            "user_id": self.test_user.id,
            "allowed_ips": "192.168.1.0/24, 10.50.0.1",
        })
        ip_raw = ip_token.raw_token

        # Subnet match (192.168.1.42 in 192.168.1.0/24)
        verified = self.env["mcp.access.token"].verify_token(
            env=self.env,
            raw_token=ip_raw,
            client_ip="192.168.1.42",
        )
        self.assertEqual(verified.id, ip_token.id)

        # Single IP match (10.50.0.1)
        verified_single = self.env["mcp.access.token"].verify_token(
            env=self.env,
            raw_token=ip_raw,
            client_ip="10.50.0.1",
        )
        self.assertEqual(verified_single.id, ip_token.id)

        # Unauthorized IP (172.16.0.99)
        with self.assertRaises(AccessDenied):
            self.env["mcp.access.token"].verify_token(
                env=self.env,
                raw_token=ip_raw,
                client_ip="172.16.0.99",
            )

    def test_05_readonly_token_firewall(self):
        """Ensure read-only tokens are barred from mutating operations."""
        ro_token = self.env["mcp.access.token"].create({
            "name": "Read-Only Token",
            "user_id": self.test_user.id,
            "is_readonly": True,
        })
        user_env = self.env(user=self.test_user)

        # Check create is blocked
        with self.assertRaises(AccessDenied):
            self.controller._handle_tools_call(
                env=user_env,
                token_id=ro_token.id,
                is_readonly=True,
                params={
                    "name": "odoo_create",
                    "arguments": {"model": "res.partner", "values": {"name": "Should Fail"}},
                },
            )

        # Check write is blocked
        with self.assertRaises(AccessDenied):
            self.controller._handle_tools_call(
                env=user_env,
                token_id=ro_token.id,
                is_readonly=True,
                params={
                    "name": "odoo_write",
                    "arguments": {"model": "res.partner", "ids": [1], "values": {"name": "Should Fail"}},
                },
            )

        # Check unlink is blocked
        with self.assertRaises(AccessDenied):
            self.controller._handle_tools_call(
                env=user_env,
                token_id=ro_token.id,
                is_readonly=True,
                params={
                    "name": "odoo_unlink",
                    "arguments": {"model": "res.partner", "ids": [1]},
                },
            )

    def test_06_server_url_and_native_acl(self):
        """Ensure token server_url is generated correctly and native ACL is enforced."""
        token = self.env["mcp.access.token"].create({
            "name": "User Scoped Token",
            "user_id": self.test_user.id,
        })
        self.assertTrue(token.token)
        self.assertIn("?token=", token.server_url)
        self.assertIn(token.token, token.server_url)

        # Test tunnel and live domain auto-upgrade to https://
        ngrok_url = token._format_mcp_base_url("http://achiness-encore-delighted.ngrok-free.dev")
        self.assertEqual(ngrok_url, "https://achiness-encore-delighted.ngrok-free.dev")

        public_url = token._format_mcp_base_url("http://erp.mycompany.com")
        self.assertEqual(public_url, "https://erp.mycompany.com")

        local_url = token._format_mcp_base_url("http://localhost:8069")
        self.assertEqual(local_url, "http://localhost:8069")

        user_env = self.env(user=self.test_user)
        # Valid model accessible to user
        res = self.controller._tool_search_read(
            env=user_env,
            args={"model": "res.partner", "limit": 1},
        )
        self.assertEqual(res["model"], "res.partner")

    def test_07_internal_model_blacklist(self):
        """Ensure sensitive system configuration models are permanently blacklisted."""
        user_env = self.env(user=self.test_user)
        for blacklisted in ("ir.config_parameter", "mcp.access.token", "res.users.apikeys"):
            with self.assertRaises(AccessDenied):
                self.controller._tool_search_read(
                    env=user_env,
                    args={"model": blacklisted, "limit": 1},
                )

    def test_08_field_blacklist(self):
        """Ensure sensitive fields like password, totp_secret, and company-selected fields are redacted and blocked."""
        user_env = self.env(user=self.test_user)
        # Search read with baseline sensitive fields requested
        filtered = self.controller._filter_blacklisted_fields(
            user_env,
            ["name", "email", "password", "totp_secret", "credit_card"],
        )
        self.assertNotIn("password", filtered)
        self.assertNotIn("totp_secret", filtered)
        self.assertNotIn("credit_card", filtered)
        self.assertIn("name", filtered)
        self.assertIn("email", filtered)

        # Write attempting to set password
        with self.assertRaises(AccessDenied):
            self.controller._sanitize_write_values(
                user_env,
                {"name": "Alice", "password": "hacked"},
            )

        # Test company-level Many2many ir.model.fields configuration
        phone_field = self.env["ir.model.fields"].sudo().search([
            ("model", "=", "res.partner"),
            ("name", "=", "phone"),
        ], limit=1)
        if phone_field:
            self.env.company.sudo().mcp_blacklisted_field_ids = [(4, phone_field.id)]
            partner_filtered = self.controller._filter_blacklisted_fields(
                user_env,
                ["name", "phone"],
                model="res.partner",
            )
            self.assertNotIn("phone", partner_filtered)
            self.assertIn("name", partner_filtered)

            # Attempt write on custom-blacklisted field
            with self.assertRaises(AccessDenied):
                self.controller._sanitize_write_values(
                    user_env,
                    {"name": "Alice", "phone": "1234567890"},
                    model="res.partner",
                )

    def test_09_execute_kw_whitelist(self):
        """Verify business method execution against the admin whitelist."""
        user_env = self.env(user=self.test_user)

        # name_get is whitelisted and readonly safe
        is_allowed = self.env["mcp.method.whitelist"].is_method_allowed(
            env=user_env,
            model_name="res.partner",
            method_name="name_get",
            is_readonly_token=True,
        )
        self.assertTrue(is_allowed, "name_get should be permitted by default whitelist.")

        # action_confirm is whitelisted but NOT safe for readonly token
        is_confirm_ro = self.env["mcp.method.whitelist"].is_method_allowed(
            env=user_env,
            model_name="sale.order",
            method_name="action_confirm",
            is_readonly_token=True,
        )
        self.assertFalse(is_confirm_ro, "action_confirm must NOT be permitted for read-only tokens.")

        # Private or unwhitelisted method
        is_private = self.env["mcp.method.whitelist"].is_method_allowed(
            env=user_env,
            model_name="res.partner",
            method_name="_compute_display_name",
            is_readonly_token=False,
        )
        self.assertFalse(is_private, "Private methods starting with '_' must never be permitted.")

    def test_10_schema_compression_engine(self):
        """Verify empty and False value stripping to optimize token consumption."""
        raw_records = [
            {
                "id": 1,
                "name": "Global Tech",
                "create_uid": [1, "Admin"],
                "write_date": "2026-01-01 12:00:00",
                "street2": False,
                "vat": False,
                "website": None,
                "comment": "",
                "child_ids": [],
            }
        ]
        compressed = self.controller._compress_records(raw_records)
        self.assertEqual(len(compressed), 1)
        rec = compressed[0]

        # Tracking fields omitted
        self.assertNotIn("create_uid", rec)
        self.assertNotIn("write_date", rec)

        # Empty fields stripped
        self.assertNotIn("street2", rec)
        self.assertNotIn("vat", rec)
        self.assertNotIn("website", rec)
        self.assertNotIn("comment", rec)
        self.assertNotIn("child_ids", rec)

        # Valid fields preserved
        self.assertEqual(rec["id"], 1)
        self.assertEqual(rec["name"], "Global Tech")

    def test_11_prompt_rendering(self):
        """Test prompt template variable interpolation and validation."""
        prompt = self.env["mcp.prompt"].create({
            "name": "Test Partner Audit",
            "code": "test_partner_audit",
            "arguments_json": '[{"name": "partner_id", "required": true}, {"name": "limit", "default": "10"}]',
            "template_text": "Audit partner {partner_id} with limit {limit}.",
        })

        rendered = prompt.render({"partner_id": 42})
        self.assertEqual(rendered, "Audit partner 42 with limit 10.")

        # Missing required parameter raises ValidationError
        with self.assertRaises(ValidationError):
            prompt.render({})

    def test_12_profile_preset(self):
        """Test role-based presets auto-populating permissions and models."""
        token = self.env["mcp.access.token"].new({
            "name": "",
            "user_id": self.test_user.id,
            "profile_preset": "sales_ro",
        })
        token._onchange_profile_preset()
        self.assertTrue(token.is_readonly, "sales_ro preset must set is_readonly=True")
        self.assertIn("Sales", token.name)
        allowed_models = [m.model for m in token.allowed_model_ids]
        self.assertIn("res.partner", allowed_models)

    def test_13_emergency_revoke_all(self):
        """Test emergency killswitch deactivates all active tokens company-wide."""
        self.token_rec.write({"active": True})
        self.assertTrue(self.token_rec.active)
        self.env["mcp.access.token"].action_emergency_revoke_all()
        self.token_rec.invalidate_recordset()
        self.assertFalse(self.token_rec.active, "Token must be inactive after emergency killswitch")

    def test_14_odoo_count(self):
        """Test high-performance record counting tool."""
        res = self.controller._tool_count(self.env, {"model": "res.partner", "domain": []})
        expected_count = self.env["res.partner"].search_count([])
        self.assertEqual(res["count"], expected_count)
        self.assertEqual(res["model"], "res.partner")

    def test_15_odoo_aggregate(self):
        """Test SQL aggregation tool (read_group)."""
        res = self.controller._tool_aggregate(
            self.env,
            {"model": "res.partner", "fields": ["id:count"], "groupby": ["active"]}
        )
        self.assertTrue("groups" in res)
        self.assertTrue(res["groups_count"] >= 1)

    def test_16_odoo_find_models(self):
        """Test natural language ERP model discovery tool."""
        res = self.controller._tool_find_models(self.env, {"query": "partner"})
        found_models = [m["model"] for m in res["models"]]
        self.assertIn("res.partner", found_models)

    def test_17_bulk_safety_guard(self):
        """Test that updating/deleting >25 records triggers safety guard unless allow_bulk is set."""
        fake_ids = list(range(1, 30))
        with self.assertRaises(UserError) as cm:
            self.controller._tool_unlink(self.env, {"model": "res.partner", "ids": fake_ids})
        self.assertIn("Safety Limit Triggered", str(cm.exception))

    def test_18_business_context(self):
        """Test executive environmental context discovery."""
        res = self.controller._tool_get_business_context(self.env)
        self.assertIn("company", res)
        self.assertIn("currency", res["company"])
        self.assertIn("code", res["company"]["currency"])
        self.assertIn("user", res)
        self.assertIn("installed_apps", res)
        self.assertIn("server_date", res)

    def test_19_business_kpis(self):
        """Test executive KPI briefing across timeframes."""
        res = self.controller._tool_get_business_kpis(self.env, {"period": "this_month"})
        self.assertEqual(res["period"], "this_month")
        self.assertIn("date_from", res)
        self.assertIn("date_to", res)
        if "sales" in res:
            self.assertIn("confirmed_revenue", res["sales"])
        if "invoicing" in res:
            self.assertIn("total_unpaid_receivables", res["invoicing"])

    def test_20_global_search(self):
        """Test unified multi-model search across ERP entities."""
        res = self.controller._tool_global_search(self.env, {"query": "YourCompany", "limit_per_model": 3})
        self.assertEqual(res["query"], "YourCompany")
        self.assertIn("results", res)

    def test_21_partner_360(self):
        """Test 360-degree commercial customer dossier."""
        partner = self.env["res.partner"].search([], limit=1)
        res = self.controller._tool_partner_360(self.env, {"partner_id": partner.id})
        self.assertIn("partner_profile", res)
        self.assertEqual(res["partner_profile"]["id"], partner.id)
        self.assertEqual(res["partner_profile"]["name"], partner.name)

        # Test search by name
        res_by_name = self.controller._tool_partner_360(self.env, {"partner_name": partner.name})
        self.assertEqual(res_by_name["partner_profile"]["id"], partner.id)

    def test_22_chatter_tools(self):
        """Test posting notes to chatter and reading conversation history."""
        partner = self.env["res.partner"].search([], limit=1)
        
        # Post internal note
        post_res = self.controller._tool_post_chatter_message(
            self.env,
            {
                "model": "res.partner",
                "res_id": partner.id,
                "body": "Test AI note: Customer approved terms.",
                "message_type": "comment",
            }
        )
        self.assertEqual(post_res["status"], "posted")
        self.assertEqual(post_res["res_id"], partner.id)

        # Retrieve history
        hist_res = self.controller._tool_get_chatter_history(
            self.env,
            {"model": "res.partner", "res_id": partner.id, "limit": 5}
        )
        self.assertEqual(hist_res["model"], "res.partner")
        self.assertTrue(hist_res["count"] >= 1)
        bodies = [m["body"] for m in hist_res["messages"]]
        self.assertTrue(any("Customer approved terms" in b for b in bodies))

    def test_23_activities_lifecycle(self):
        """Test scheduling, listing, and completing activities."""
        partner = self.env["res.partner"].search([], limit=1)

        # 1. Schedule activity
        sched_res = self.controller._tool_schedule_activity(
            self.env,
            {
                "model": "res.partner",
                "res_id": partner.id,
                "activity_type": "call",
                "summary": "AI Follow-up Call",
                "note": "Discuss contract renewal",
            }
        )
        activity_id = sched_res["activity_id"]
        self.assertTrue(activity_id)

        # 2. Get activities
        act_list = self.controller._tool_get_activities(
            self.env,
            {"filter": "all", "model": "res.partner"}
        )
        self.assertTrue(act_list["count"] >= 1)
        act_ids = [a["activity_id"] for a in act_list["activities"]]
        self.assertIn(activity_id, act_ids)

        # 3. Mark activity done
        done_res = self.controller._tool_mark_activity_done(
            self.env,
            {"activity_id": activity_id, "feedback": "Call completed successfully."}
        )
        self.assertEqual(done_res["status"], "done")

    def test_24_stock_check(self):
        """Test stock check tool."""
        if "stock.quant" in self.env and "product.product" in self.env:
            res = self.controller._tool_check_stock(self.env, {"limit": 5})
            self.assertIn("products", res)

    def test_25_workflow_actions(self):
        """Test business workflow state actions and read-only blocking."""
        # Test readonly firewall
        with self.assertRaises(AccessDenied):
            self.controller._handle_tools_call(
                env=self.env,
                token_id=self.token_rec.id,
                is_readonly=True,
                params={"name": "odoo_workflow_action", "arguments": {"action": "confirm_sale", "res_id": 1}}
            )

    def test_26_report_pdf(self):
        """Test PDF report generation."""
        partner = self.env["res.partner"].search([], limit=1)
        reports = self.env["ir.actions.report"].search([("model", "=", "res.partner")], limit=1)
        if reports:
            res = self.controller._tool_get_report_pdf(
                self.env,
                {"model": "res.partner", "res_id": partner.id, "report_xmlid": reports[0].xml_id}
            )
            self.assertIn("pdf_base64", res)
            self.assertEqual(res["mimetype"], "application/pdf")
            self.assertTrue(res["size_bytes"] > 0)

    def test_27_dynamic_method_whitelist(self):
        """Test dynamic action method auto-whitelisting."""
        whitelist = self.env["mcp.method.whitelist"]
        # Public action methods should be allowed
        self.assertTrue(whitelist.is_method_allowed(self.env, "sale.order", "action_confirm"))
        self.assertTrue(whitelist.is_method_allowed(self.env, "purchase.order", "button_approve"))
        self.assertTrue(whitelist.is_method_allowed(self.env, "custom.model", "action_custom_start"))

        # Dangerous or private methods must NEVER be allowed
        self.assertFalse(whitelist.is_method_allowed(self.env, "sale.order", "_private_action"))
        self.assertFalse(whitelist.is_method_allowed(self.env, "sale.order", "sudo"))
        self.assertFalse(whitelist.is_method_allowed(self.env, "sale.order", "unlink"))
        self.assertFalse(whitelist.is_method_allowed(self.env, "sale.order", "write"))

    def test_28_token_model_whitelist_enforcement(self):
        """Test strict model whitelist firewall on tokens."""
        partner_model = self.env["ir.model"].search([("model", "=", "res.partner")], limit=1)
        restricted_token = self.env["mcp.access.token"].create({
            "name": "Restricted Token",
            "user_id": self.test_user.id,
            "allowed_model_ids": [(6, 0, [partner_model.id])],
        })
        restricted_env = self.env(user=self.test_user.id, context=dict(self.env.context, mcp_token_id=restricted_token.id))

        # Allowed model should pass
        model = self.controller._validate_model_access(restricted_env, "res.partner", mode="read")
        self.assertEqual(model, "res.partner")

        # Non-whitelisted model must be blocked by firewall
        with self.assertRaises(AccessDenied):
            self.controller._validate_model_access(restricted_env, "res.users", mode="read")

    def test_29_schema_defaults_and_relations(self):
        """Test schema introspection with defaults and relational hints."""
        schema = self.controller._tool_get_model_schema(
            self.env,
            {"model": "res.partner", "include_defaults": True, "include_relations": True}
        )
        self.assertIn("fields", schema)
        self.assertIn("default_values", schema)
        # Check relational hints
        country_field = schema["fields"].get("country_id")
        if country_field:
            self.assertEqual(country_field.get("relation"), "res.country")

    def test_30_get_record_actions(self):
        """Test dynamic record and model action/button discovery."""
        partner = self.env["res.partner"].search([], limit=1)
        res = self.controller._tool_get_record_actions(
            self.env,
            {"model": "res.partner", "res_id": partner.id}
        )
        self.assertEqual(res["model"], "res.partner")
        self.assertIn("available_actions", res)
        self.assertTrue(isinstance(res["available_actions"], list))

    def test_31_record_360(self):
        """Test universal 360 relationship discovery on any model."""
        partner = self.env["res.partner"].search([], limit=1)
        res = self.controller._tool_record_360(
            self.env,
            {"model": "res.partner", "res_id": partner.id}
        )
        self.assertEqual(res["model"], "res.partner")
        self.assertIn("record", res)
        self.assertIn("related_records", res)
        self.assertTrue(res["related_connections_count"] >= 0)

    def test_32_universal_workflow_action(self):
        """Test universal workflow action execution with dynamic models."""
        partner = self.env["res.partner"].search([], limit=1)
        # Calling an invalid or non-existent action method should raise UserError
        with self.assertRaises(UserError):
            self.controller._tool_workflow_action(
                self.env,
                {"model": "res.partner", "res_id": partner.id, "action": "non_existent_method_xyz"}
            )

    def test_33_build_domain(self):
        """Test smart domain builder with relative dates, validation, and expression combining."""
        # 1. Basic AND domain with relative date
        res = self.controller._tool_build_domain(self.env, {
            "model": "res.partner",
            "conditions": [
                {"field": "is_company", "operator": "=", "value": True},
                {"field": "create_date", "operator": ">=", "value": "-30d"},
            ],
            "logical_operator": "AND",
        })
        self.assertTrue(res["success"])
        self.assertEqual(res["model"], "res.partner")
        self.assertTrue(isinstance(res["domain"], list))
        self.assertIn("create_date", res["field_metadata"])

        # 2. OR domain with multiple conditions
        res_or = self.controller._tool_build_domain(self.env, {
            "model": "res.partner",
            "conditions": [
                {"field": "is_company", "operator": "=", "value": True},
                {"field": "active", "operator": "=", "value": True},
            ],
            "logical_operator": "OR",
        })
        self.assertTrue(res_or["success"])
        self.assertIn("|", res_or["domain"])

        # 3. Invalid operator raises ValidationError
        with self.assertRaises(ValidationError):
            self.controller._tool_build_domain(self.env, {
                "model": "res.partner",
                "conditions": [{"field": "name", "operator": "INVALID_OP", "value": "test"}],
            })

    def test_34_diagnose_access(self):
        """Test deep security and access diagnostics."""
        partner = self.env["res.partner"].search([], limit=1)
        res = self.controller._tool_diagnose_access(
            self.env,
            token_id=self.token_rec.id,
            is_readonly=False,
            args={"model": "res.partner", "operation": "read", "res_id": partner.id},
        )
        self.assertTrue(res["allowed"])
        self.assertTrue(res["model_acl_allowed"])
        self.assertEqual(res["user_id"], self.env.user.id)

        # Non-existent model check
        res_fake = self.controller._tool_diagnose_access(
            self.env,
            token_id=self.token_rec.id,
            is_readonly=False,
            args={"model": "non.existent.model.xyz"},
        )
        self.assertFalse(res_fake["allowed"])
        self.assertEqual(res_fake["root_cause"], "MODEL_NOT_FOUND")

    def test_35_read_attachment(self):
        """Test attachment metadata reading and text decoding."""
        partner = self.env["res.partner"].search([], limit=1)
        test_content = "MCP attachment content test 12345"
        att_vals = {
            "name": "mcp_test_doc.txt",
            "type": "binary",
            "mimetype": "text/plain",
            "res_model": "res.partner",
            "res_id": partner.id,
        }
        if "raw" in self.env["ir.attachment"]._fields:
            att_vals["raw"] = test_content.encode("utf-8")
        else:
            att_vals["datas"] = base64.b64encode(test_content.encode("utf-8"))
        att = self.env["ir.attachment"].create(att_vals)

        # Read by attachment_id
        res = self.controller._tool_read_attachment(self.env, {"attachment_id": att.id, "include_content": True})
        self.assertEqual(res["attachment"]["name"], "mcp_test_doc.txt")
        self.assertEqual(res["content_type"], "text")
        self.assertEqual(res["content_text"], test_content)

        # Discover attachments by record
        res_list = self.controller._tool_read_attachment(self.env, {"res_model": "res.partner", "res_id": partner.id})
        self.assertTrue(res_list["total_attachments"] >= 1)

    def test_36_aged_partner_balance(self):
        """Test aged partner balance / receivables aging calculation."""
        res = self.controller._tool_aged_partner_balance(self.env, {
            "account_type": "receivable",
            "top_partners": 5,
        })
        if "account.move.line" in self.env:
            self.assertTrue(res["success"])
            self.assertIn("aging_buckets", res)
            self.assertIn("not_due", res["aging_buckets"])
            self.assertIn("days_1_30", res["aging_buckets"])
        else:
            self.assertFalse(res["success"])

    def test_37_inspect_data_quality(self):
        """Test data hygiene and duplicate inspection tool."""
        res = self.controller._tool_inspect_data_quality(self.env, {
            "model": "res.partner",
            "checks": ["duplicates", "missing_values", "format_anomalies"],
            "sample_limit": 50,
        })
        self.assertTrue(res["success"])
        self.assertEqual(res["model"], "res.partner")
        self.assertTrue(0 <= res["health_score"] <= 100)
        self.assertIn("findings_summary", res)
        self.assertIn("recommendations", res)

    def test_38_rate_limiting(self):
        """Test sliding-window rate limit enforcement on tokens."""
        self.token_rec.reset_rate_limit_cache(self.token_rec.id)
        self.token_rec.rate_limit_per_minute = 2

        # 1st request: allowed
        allowed, count, retry_after = self.token_rec.check_rate_limit()
        self.assertTrue(allowed)
        self.assertEqual(count, 1)

        # 2nd request: allowed
        allowed, count, retry_after = self.token_rec.check_rate_limit()
        self.assertTrue(allowed)
        self.assertEqual(count, 2)

        # 3rd request: BLOCKED!
        allowed, count, retry_after = self.token_rec.check_rate_limit()
        self.assertFalse(allowed)
        self.assertTrue(retry_after > 0)

        # Reset cache
        self.token_rec.reset_rate_limit_cache(self.token_rec.id)
        # Test rate_limit_per_minute = 0 (unlimited)
        self.token_rec.rate_limit_per_minute = 0
        allowed, count, retry_after = self.token_rec.check_rate_limit()
        self.assertTrue(allowed)
        self.assertEqual(count, 0)

    def test_35_create_dashboard(self):
        """Test dynamic dashboard creation with graph, pivot views, window action, and menu."""
        admin_user = self.env.ref("base.user_admin")
        admin_env = self.env(user=admin_user.id)

        res = self.controller._tool_create_dashboard(admin_env, False, {
            "name": "Partner Commercial Dashboard",
            "model": "res.partner",
            "graph_type": "bar",
            "dimension_field": "country_id",
            "measure_field": "__count",
            "pivot_row_fields": ["country_id"],
            "pivot_col_fields": ["is_company"],
            "create_menu": True,
            "include_preview": True,
        })
        self.assertTrue(res.get("success"))
        self.assertTrue(res.get("action_id"))
        self.assertTrue(res.get("graph_view_id"))
        self.assertTrue(res.get("pivot_view_id"))
        self.assertIn("/web#action=", res.get("web_url"))
        self.assertIn("preview_data", res)

        # Test Preview Mode (confirm=False): No write operations, returns live preview data
        res_preview = self.controller._tool_create_dashboard(admin_env, False, {
            "name": "Partner Preview Dashboard",
            "model": "res.partner",
            "confirm": False,
            "dimension_field": "country_id",
            "pivot_row_fields": ["country_id"],
            "pivot_col_fields": ["is_company"],
        })
        self.assertTrue(res_preview.get("success"))
        self.assertEqual(res_preview.get("status"), "preview")
        self.assertFalse(res_preview.get("confirmed"))
        self.assertTrue(len(res_preview.get("panels", [])) > 0)
        self.assertIn("dashboard_json", res_preview)
        self.assertIn("table_markdown", res_preview["panels"][0])

        # Read-only token must be rejected when deploying (confirm=True)
        with self.assertRaises(AccessDenied):
            self.controller._tool_create_dashboard(admin_env, True, {
                "name": "Forbidden Dashboard",
                "model": "res.partner",
                "confirm": True,
            })

    def test_36_get_dashboard_data(self):
        """Test multi-metric dashboard analytics computation."""
        res = self.controller._tool_get_dashboard_data(self.env, {
            "model": "res.partner",
            "dimension_field": "is_company",
            "top_records_limit": 5,
        })
        self.assertEqual(res.get("model"), "res.partner")
        self.assertIn("kpi_cards", res)
        self.assertIn("currency", res)
        self.assertTrue(res.get("total_records") >= 0)
        self.assertIn("top_records", res)

    def test_37_preserve_explicit_tracking_fields(self):
        """Ensure explicitly requested tracking fields (e.g. create_date) are preserved by compression engine."""
        read_res = self.controller._tool_search_read(self.env, {
            "model": "res.partner",
            "fields": ["name", "create_date"],
            "limit": 2,
        })
        records = read_res.get("records", [])
        if records:
            self.assertIn("create_date", records[0], "create_date must be preserved when explicitly requested in fields.")

        # When fields is omitted or does not include create_date, it must be pruned for token optimization
        read_default = self.controller._tool_search_read(self.env, {
            "model": "res.partner",
            "limit": 2,
        })
        default_records = read_default.get("records", [])
        if default_records:
            self.assertNotIn("create_date", default_records[0], "create_date should be pruned by default to save tokens.")


class TestMcpHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        demo_user = cls.env["res.users"].search([("login", "=", "demo")], limit=1)
        if not demo_user:
            demo_user = cls.env.ref("base.user_demo", raise_if_not_found=False)
        if not demo_user:
            demo_user = cls.env.ref("base.user_admin")
        cls.test_user = demo_user

        cls.token_rec = cls.env["mcp.access.token"].create({
            "name": "HTTP Integration Token",
            "user_id": cls.test_user.id,
            "is_readonly": False,
        })
        cls.raw_token = cls.token_rec.raw_token

    def test_http_01_ping(self):
        """Test healthcheck ping endpoint."""
        resp = self.url_open(
            "/mcp/rpc",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("result"), {})

    def test_http_02_unauthorized(self):
        """Test rejected requests when providing an invalid Bearer token."""
        resp = self.url_open(
            "/mcp/rpc",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "odoo_search_read",
                    "arguments": {"model": "res.partner"},
                },
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer invalid_secret_token_xyz",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32001)

    def test_http_03_tools_list_and_call(self):
        """Test tools/list discovery and subsequent tools/call execution via HTTP."""
        auth_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.raw_token}",
        }
        # 1. tools/list
        resp = self.url_open(
            "/mcp/rpc",
            data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode("utf-8"),
            headers=auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        tools = data.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        self.assertIn("odoo_search_read", tool_names)
        self.assertIn("odoo_read", tool_names)
        self.assertIn("odoo_create", tool_names)
        self.assertIn("odoo_write", tool_names)
        self.assertIn("odoo_unlink", tool_names)
        self.assertIn("odoo_execute_kw", tool_names)
        self.assertIn("odoo_get_model_schema", tool_names)
        self.assertIn("odoo_get_business_context", tool_names)
        self.assertIn("odoo_get_business_kpis", tool_names)
        self.assertIn("odoo_global_search", tool_names)
        self.assertIn("odoo_partner_360", tool_names)
        self.assertIn("odoo_get_activities", tool_names)

        # 2. tools/call odoo_search_read
        call_payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "odoo_search_read",
                "arguments": {
                    "model": "res.partner",
                    "limit": 2,
                },
            },
        }
        resp_call = self.url_open(
            "/mcp/rpc",
            data=json.dumps(call_payload).encode("utf-8"),
            headers=auth_headers,
        )
        self.assertEqual(resp_call.status_code, 200)
        call_res = resp_call.json()
        self.assertFalse(call_res.get("result", {}).get("isError"))
        content = call_res.get("result", {}).get("content", [])
        self.assertTrue(len(content) > 0)
        parsed_content = json.loads(content[0]["text"])
        self.assertEqual(parsed_content.get("model"), "res.partner")
        self.assertTrue("records" in parsed_content)

    def test_http_04_query_token_auth(self):
        """Test authenticating via URL query parameters (?token=... and ?our_token=...)."""
        # 1. Test ?token=
        resp = self.url_open(
            f"/mcp?token={self.raw_token}",
            data=json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("error", data)
        self.assertIn("tools", data.get("result", {}))

        # 2. Test ?our_token=
        resp2 = self.url_open(
            f"/mcp?our_token={self.raw_token}",
            data=json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertNotIn("error", data2)
        self.assertIn("tools", data2.get("result", {}))

        # 3. Test unauthenticated request without token is strictly rejected (-32001)
        resp3 = self.url_open(
            "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertIn("error", data3)
        self.assertEqual(data3["error"]["code"], -32001)

    def test_http_05_jsonrpc_batch(self):
        """Test JSON-RPC 2.0 array batch requests."""
        batch_payload = [
            {"jsonrpc": "2.0", "id": 101, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 102, "method": "tools/list", "params": {}},
        ]
        resp = self.url_open(
            f"/mcp?token={self.raw_token}",
            data=json.dumps(batch_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(isinstance(data, list))
        self.assertEqual(len(data), 2)
        ids = [item.get("id") for item in data]
        self.assertIn(101, ids)
        self.assertIn(102, ids)

    def test_http_06_resource_templates_list(self):
        """Test resources/templates/list protocol method."""
        resp = self.url_open(
            f"/mcp?token={self.raw_token}",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 201,
                "method": "resources/templates/list",
                "params": {},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("error", data)
        templates = data.get("result", {}).get("resourceTemplates", [])
        template_uris = [t["uriTemplate"] for t in templates]
        self.assertIn("odoo://attachment/{id}", template_uris)

    def test_http_07_context_resource(self):
        """Test reading odoo://context resource."""
        resp = self.url_open(
            f"/mcp?token={self.raw_token}",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 301,
                "method": "resources/read",
                "params": {"uri": "odoo://context"},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("error", data)
        contents = data.get("result", {}).get("contents", [])
        self.assertTrue(len(contents) > 0)
        context_dict = json.loads(contents[0]["text"])
        self.assertIn("user_name", context_dict)
        self.assertIn("active_company_name", context_dict)
        self.assertIn("timezone", context_dict)

    def test_http_08_rate_limit_enforcement(self):
        """Test HTTP rate limit blocking when token limit is exceeded."""
        self.token_rec.reset_rate_limit_cache(self.token_rec.id)
        self.token_rec.rate_limit_per_minute = 1

        auth_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.raw_token}",
        }
        call_payload = {
            "jsonrpc": "2.0",
            "id": 401,
            "method": "tools/call",
            "params": {
                "name": "odoo_search_read",
                "arguments": {"model": "res.partner", "limit": 1},
            },
        }

        # 1st HTTP request: must succeed
        resp1 = self.url_open("/mcp/rpc", data=json.dumps(call_payload).encode("utf-8"), headers=auth_headers)
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertFalse(data1.get("result", {}).get("isError"))

        # 2nd HTTP request immediately: must be RATE LIMITED
        call_payload["id"] = 402
        resp2 = self.url_open("/mcp/rpc", data=json.dumps(call_payload).encode("utf-8"), headers=auth_headers)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2.get("result", {}).get("isError"))
        err_text = data2.get("result", {}).get("content", [{}])[0].get("text", "")
        self.assertIn("Rate limit exceeded", err_text)

        # Non-tool RPC call (e.g. tools/list) must return -32005 error
        resp3 = self.url_open("/mcp/rpc", data=json.dumps({
            "jsonrpc": "2.0",
            "id": 403,
            "method": "tools/list",
            "params": {},
        }).encode("utf-8"), headers=auth_headers)
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertIn("error", data3)
        self.assertEqual(data3["error"]["code"], -32005)

        # Restore default rate limit
        self.token_rec.reset_rate_limit_cache(self.token_rec.id)
        self.token_rec.rate_limit_per_minute = 120

