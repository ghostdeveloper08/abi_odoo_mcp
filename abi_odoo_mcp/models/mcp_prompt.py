# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

import json
import logging
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class McpPrompt(models.Model):
    _name = "mcp.prompt"
    _description = "MCP Prompt Template"
    _order = "name asc"

    name = fields.Char(
        string="Prompt Title",
        required=True,
    )
    code = fields.Char(
        string="Technical Identifier",
        required=True,
        index=True,
        help="Unique identifier used in prompts/get requests (e.g., 'summarize_overdue_invoices').",
    )
    description = fields.Text(
        string="Description",
        help="Overview of what this prompt accomplishes for LLMs.",
    )
    arguments_json = fields.Text(
        string="Arguments Definition (JSON)",
        default="[]",
        help='JSON list of argument specifications, e.g. [{"name": "partner_id", "description": "Customer ID", "required": false}]',
    )
    template_text = fields.Text(
        string="Prompt Template",
        required=True,
        help="Template text formatted with Python-style {argument_name} placeholders.",
    )
    active = fields.Boolean(
        string="Active",
        default=True,
    )

    _code_uniq = models.Constraint(
        "unique (code)",
        "The technical prompt identifier must be unique!",
    )

    @api.constrains("arguments_json")
    def _check_arguments_json(self):
        for record in self:
            if not record.arguments_json:
                continue
            try:
                parsed = json.loads(record.arguments_json)
                if not isinstance(parsed, list):
                    raise ValidationError(_("Arguments definition must be a valid JSON array of argument objects."))
            except json.JSONDecodeError as e:
                raise ValidationError(_("Invalid JSON syntax in arguments definition: %(err)s", err=e))

    def render(self, arguments=None):
        """
        Render prompt template with provided arguments.
        """
        self.ensure_one()
        args = arguments or {}
        try:
            arg_defs = json.loads(self.arguments_json or "[]")
        except Exception:
            arg_defs = []

        # Check required arguments
        for arg_def in arg_defs:
            arg_name = arg_def.get("name")
            if arg_def.get("required") and arg_name not in args:
                raise ValidationError(_("Missing required prompt argument: %(arg)s", arg=arg_name))

        # Safe template replacement with defaults for missing optional arguments
        rendered_text = self.template_text
        for arg_def in arg_defs:
            arg_name = arg_def.get("name")
            placeholder = "{" + arg_name + "}"
            val = str(args.get(arg_name, arg_def.get("default", "")))
            rendered_text = rendered_text.replace(placeholder, val)

        return rendered_text

    def to_mcp_dict(self):
        """Return MCP specification dictionary for prompts/list."""
        self.ensure_one()
        try:
            parsed_args = json.loads(self.arguments_json or "[]")
        except Exception:
            parsed_args = []

        return {
            "name": self.code,
            "description": self.description or self.name,
            "arguments": parsed_args,
        }
