# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from odoo import api, models


class IrConfigParameter(models.Model):
    _inherit = "ir.config_parameter"

    @api.model
    def get_param(self, key, default=False):
        """Compatibility layer for Odoo 20: redirect legacy get_param to get_str."""
        if hasattr(self, "get_str"):
            val = self.get_str(key, default=default)
            return default if val is None else val
        if hasattr(super(), "get_param"):
            return super().get_param(key, default=default)
        return default

    @api.model
    def set_param(self, key, value):
        """Compatibility layer for Odoo 20: redirect legacy set_param to set_str."""
        if hasattr(self, "set_str"):
            return self.set_str(key, str(value) if value is not None else "")
        if hasattr(super(), "set_param"):
            return super().set_param(key, value)
        return None
