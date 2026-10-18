# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    mcp_token_ids = fields.One2many(
        "mcp.access.token",
        "user_id",
        string="MCP Access Tokens",
        help="Access tokens configured for AI assistant and LLM integration.",
    )
    mcp_token_count = fields.Integer(
        string="Active MCP Tokens",
        compute="_compute_mcp_token_count",
        compute_sudo=True,
    )

    def _compute_mcp_token_count(self):
        for user in self:
            user.mcp_token_count = len(user.sudo().mcp_token_ids.filtered(lambda t: t.active))

    def action_open_my_mcp_tokens(self):
        self.ensure_one()
        return {
            "name": "My MCP Access Tokens",
            "type": "ir.actions.act_window",
            "res_model": "mcp.access.token",
            "view_mode": "list,form",
            "domain": [("user_id", "=", self.id)],
            "context": {"default_user_id": self.id},
        }
