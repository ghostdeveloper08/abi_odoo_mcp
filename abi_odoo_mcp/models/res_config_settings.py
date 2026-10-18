# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from odoo import fields, models

DEFAULT_FIELD_BLACKLIST = "password,totp_secret,credit_card,api_key,private_key,passphrase,secret"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    mcp_blacklisted_field_ids = fields.Many2many(
        related="company_id.mcp_blacklisted_field_ids",
        readonly=False,
        string="Sensitive Fields",
        help="Select sensitive database fields that will be hidden from AI query results and blocked from updates.",
    )
    mcp_audit_logging_enabled = fields.Boolean(
        string="Enable Audit Logging",
        default=True,
        config_parameter="abi_odoo_mcp.audit_logging_enabled",
        help="Record detailed execution metrics, latency, and sanitized payloads in mcp.audit.log.",
    )
    mcp_default_rate_limit = fields.Integer(
        string="Default Rate Limit (req/min)",
        default=120,
        config_parameter="abi_odoo_mcp.default_rate_limit",
        help="Default requests per minute applied to newly issued tokens.",
    )
    mcp_allow_standard_actions = fields.Boolean(
        string="Allow Standard Action & Button Methods",
        default=True,
        config_parameter="abi_odoo_mcp.allow_standard_actions",
        help="Permit AI agents to dynamically invoke public business workflow methods (action_*, button_*) across standard and custom modules.",
    )
    mcp_server_instructions = fields.Text(
        string="Custom Server Instructions",
        default="Enterprise Odoo MCP Server. All operations strictly adhere to user permissions, company separation, and record rules.",
        help="System instructions passed to AI clients during MCP initialization.",
    )

    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()
        default_inst = "Enterprise Odoo MCP Server. All operations strictly adhere to user permissions, company separation, and record rules."
        instructions = (
            icp.get_str("abi_odoo_mcp.instructions", default=default_inst)
            if hasattr(icp, "get_str")
            else icp.get_param("abi_odoo_mcp.instructions", default=default_inst)
        )
        res.update(mcp_server_instructions=instructions)
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()
        val = self.mcp_server_instructions or ""
        if hasattr(icp, "set_str"):
            icp.set_str("abi_odoo_mcp.instructions", val)
        else:
            icp.set_param("abi_odoo_mcp.instructions", val)

