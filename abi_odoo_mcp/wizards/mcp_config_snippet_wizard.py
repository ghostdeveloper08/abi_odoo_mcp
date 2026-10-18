# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

import json
import os
import odoo
from odoo import api, fields, models


class McpConfigSnippetWizard(models.TransientModel):
    _name = "mcp.config.snippet.wizard"
    _description = "Share Connection"

    token_id = fields.Many2one(
        "mcp.access.token",
        string="Access Token Record",
        required=True,
    )
    user_id = fields.Many2one(
        related="token_id.user_id",
        string="Assigned User",
        readonly=True,
    )
    is_readonly = fields.Boolean(
        related="token_id.is_readonly",
        string="Read-Only Mode",
        readonly=True,
    )
    token = fields.Char(
        string="Access Token",
        readonly=True,
    )
    server_url = fields.Char(
        string="Server URL",
        readonly=True,
        help="Paste this complete URL into Claude.ai or any MCP client. It strictly verifies your user token on every request.",
    )
    base_url = fields.Char(
        string="Instance Base URL",
        readonly=True,
    )
    claude_desktop_config = fields.Text(
        string="Claude Desktop Config (claude_desktop_config.json)",
        readonly=True,
    )
    cursor_config = fields.Text(
        string="Cursor / Windsurf Config (.cursor/mcp.json)",
        readonly=True,
    )
    curl_snippet = fields.Text(
        string="cURL Verification Command",
        readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        token_id = self.env.context.get("default_token_id") or self.env.context.get("active_id")
        if token_id:
            token_rec = self.env["mcp.access.token"].browse(token_id)
            if token_rec.exists():
                res["token_id"] = token_rec.id
                token_str = token_rec.token or token_rec.raw_token or token_rec.token_prefix or ""
                base_url = token_rec._format_mcp_base_url()
                res["token"] = token_str
                res["server_url"] = token_rec.server_url or (f"{base_url}/mcp?token={token_str}" if token_str else f"{base_url}/mcp")
                res["base_url"] = base_url
        return res

    @api.onchange("token_id")
    def _onchange_token_id(self):
        if self.token_id:
            token_str = self.token_id.token or self.token_id.raw_token or self.token_id.token_prefix or ""
            base_url = self.token_id._format_mcp_base_url()
            self.base_url = base_url
            self.token = token_str
            self.server_url = self.token_id.server_url or (f"{base_url}/mcp?token={token_str}" if token_str else f"{base_url}/mcp")
