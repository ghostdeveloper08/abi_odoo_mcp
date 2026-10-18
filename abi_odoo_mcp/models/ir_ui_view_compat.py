# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

import logging
import odoo
from odoo import api, models

_logger = logging.getLogger(__name__)

IS_ODOO_18_OR_HIGHER = odoo.release.version_info[0] >= 18


class IrUiViewCompat(models.Model):
    _inherit = "ir.ui.view"

    @api.model_create_multi
    def create(self, vals_list):
        if not IS_ODOO_18_OR_HIGHER:
            for vals in vals_list:
                for arch_key in ("arch", "arch_base", "arch_db"):
                    if arch_key in vals and isinstance(vals[arch_key], str):
                        # Transpile <list> to <tree> for Odoo 17
                        vals[arch_key] = vals[arch_key].replace("<list", "<tree").replace("</list>", "</tree>")
                if vals.get("type") == "list":
                    vals["type"] = "tree"
        return super().create(vals_list)

    def write(self, vals):
        if not IS_ODOO_18_OR_HIGHER:
            for arch_key in ("arch", "arch_base", "arch_db"):
                if arch_key in vals and isinstance(vals[arch_key], str):
                    vals[arch_key] = vals[arch_key].replace("<list", "<tree").replace("</list>", "</tree>")
            if vals.get("type") == "list":
                vals["type"] = "tree"
        return super().write(vals)


class IrActionsActWindowCompat(models.Model):
    _inherit = "ir.actions.act_window"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "view_mode" in vals and isinstance(vals["view_mode"], str):
                if not IS_ODOO_18_OR_HIGHER and "list" in vals["view_mode"]:
                    vals["view_mode"] = vals["view_mode"].replace("list", "tree")
                elif IS_ODOO_18_OR_HIGHER and "tree" in vals["view_mode"]:
                    vals["view_mode"] = vals["view_mode"].replace("tree", "list")
        return super().create(vals_list)

    def write(self, vals):
        if "view_mode" in vals and isinstance(vals["view_mode"], str):
            if not IS_ODOO_18_OR_HIGHER and "list" in vals["view_mode"]:
                vals["view_mode"] = vals["view_mode"].replace("list", "tree")
            elif IS_ODOO_18_OR_HIGHER and "tree" in vals["view_mode"]:
                vals["view_mode"] = vals["view_mode"].replace("tree", "list")
        return super().write(vals)
