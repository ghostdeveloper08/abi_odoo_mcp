# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class McpMethodWhitelist(models.Model):
    _name = "mcp.method.whitelist"
    _description = "MCP Safe Business Method Whitelist"
    _order = "name asc"

    name = fields.Char(
        string="Method Name",
        required=True,
        index=True,
        help="The technical Python method name that AI agents are permitted to invoke via odoo_execute_kw (e.g., 'action_confirm').",
    )
    model_id = fields.Many2one(
        "ir.model",
        string="Target Model",
        ondelete="cascade",
        help="Specific model to which this permission applies. If left empty, the method is permitted across all models possessing it.",
    )
    is_safe_readonly = fields.Boolean(
        string="Safe for Read-Only Tokens",
        default=False,
        help="Allow read-only tokens to invoke this method (e.g. search_count, name_get, export helpers without side-effects).",
    )
    active = fields.Boolean(
        string="Active",
        default=True,
        help="Enable or disable execution of this method.",
    )
    description = fields.Text(
        string="Description / Business Context",
        help="Documentation for why this method is safe and what business process it triggers.",
    )

    @api.constrains("name")
    def _check_method_name(self):
        for record in self:
            if not record.name or record.name.startswith("_"):
                raise ValidationError(_("Private methods (starting with '_') cannot be whitelisted for security reasons."))

    @classmethod
    def is_method_allowed(cls, env, model_name, method_name, is_readonly_token=False):
        """
        Determine if an execution of `method_name` on `model_name` is authorized by policy.
        Private methods (starting with '_') and built-in Python dunder methods are unconditionally denied.
        """
        if not method_name or method_name.startswith("_"):
            return False

        # System destructive or privilege methods that must NEVER be called via execute_kw
        forbidden_methods = {
            "write", "create", "unlink", "sudo", "with_user", "with_context",
            "init", "_init", "default_get", "load", "import_data", "eval", "exec",
            "copy", "copy_data", "flush", "invalidate_cache", "invalidate_recordset"
        }
        if method_name in forbidden_methods:
            return False

        # Read-only tokens can NEVER execute state-altering actions unless explicitly marked is_safe_readonly
        if is_readonly_token:
            domain = [
                ("name", "=", method_name),
                ("active", "=", True),
                ("is_safe_readonly", "=", True),
                "|",
                ("model_id", "=", False),
                ("model_id.model", "=", model_name),
            ]
            return bool(env["mcp.method.whitelist"].sudo().search(domain, limit=1))

        # Check explicit whitelist in database first (takes precedence; can deactivate a method)
        domain = [
            ("name", "=", method_name),
            "|",
            ("model_id", "=", False),
            ("model_id.model", "=", model_name),
        ]
        explicit_match = env["mcp.method.whitelist"].sudo().with_context(active_test=False).search(domain, limit=1)
        if explicit_match:
            return bool(explicit_match.active)

        # Dynamic Action Auto-Whitelisting:
        # Check if dynamic standard actions are enabled (default True)
        icp = env["ir.config_parameter"].sudo()
        if hasattr(icp, "get_bool"):
            allow_dynamic_actions = icp.get_bool("abi_odoo_mcp.allow_standard_actions", True)
        elif hasattr(icp, "get_param"):
            allow_dynamic_actions = icp.get_param("abi_odoo_mcp.allow_standard_actions", "True") in ("True", "true", "1")
        else:
            allow_dynamic_actions = True

        if allow_dynamic_actions:
            # Recognize standard business workflow action naming patterns across any standard or custom module
            is_action_pattern = (
                method_name.startswith("action_")
                or method_name.startswith("button_")
                or method_name.endswith("_action")
            )
            if is_action_pattern:
                return True

        return False
