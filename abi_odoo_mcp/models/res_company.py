# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    mcp_blacklisted_field_ids = fields.Many2many(
        "ir.model.fields",
        "res_company_mcp_blacklisted_field_rel",
        "company_id",
        "field_id",
        string="Protected Fields",
        domain="[('model_id.transient', '=', False)]",
        default=lambda self: self._default_mcp_blacklisted_field_ids(),
        help="Database fields that are strictly shielded from AI query results and blocked from updates.",
    )

    @api.model
    def _default_mcp_blacklisted_field_ids(self):
        default_names = ["password", "totp_secret", "credit_card", "api_key", "private_key", "passphrase", "secret"]
        return self.env["ir.model.fields"].sudo().search([
            ("name", "in", default_names),
            ("model_id.transient", "=", False),
        ])
