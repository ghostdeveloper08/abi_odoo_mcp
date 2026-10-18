# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

import json
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class McpAuditLog(models.Model):
    _name = "mcp.audit.log"
    _description = "MCP Protocol Audit Log"
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Operation",
        required=True,
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        index=True,
        ondelete="set null",
    )
    token_id = fields.Many2one(
        "mcp.access.token",
        string="Token",
        index=True,
        ondelete="set null",
    )
    tool_name = fields.Char(
        string="Tool / Method",
        index=True,
    )
    model_name = fields.Char(
        string="Target Model",
        index=True,
    )
    execution_time_ms = fields.Float(
        string="Latency (ms)",
        digits=(10, 2),
        help="Time taken to execute the request in milliseconds.",
    )
    tokens_estimated = fields.Integer(
        string="Estimated Tokens",
        help="Approximate count of tokens consumed by the response payload (~4 chars per token).",
    )
    status = fields.Selection(
        [
            ("success", "Success"),
            ("error", "Error"),
            ("blocked_security", "Security Blocked"),
            ("blocked_rate_limit", "Rate Limited"),
            ("safety_guard", "Safety Limit Triggered"),
        ],
        string="Status",
        required=True,
        default="success",
        index=True,
    )
    error_message = fields.Text(
        string="Error Details",
    )
    request_summary = fields.Text(
        string="Request Parameters",
    )
    response_summary = fields.Text(
        string="Response Summary",
    )
    ip_address = fields.Char(
        string="Client IP",
    )

    @classmethod
    def log_execution(cls, env, user_id, token_id, tool_name, model_name,
                      execution_time_ms, status, request_data=None,
                      response_data=None, error_msg=None, client_ip=None):
        """
        Record an MCP audit event in an isolated database transaction
        so audit trails are safely preserved even if business transactions abort.
        """
        req_str = ""
        if request_data:
            try:
                # Sanitize sensitive fields before persisting
                sanitized_req = cls._sanitize_payload(request_data)
                req_str = json.dumps(sanitized_req, default=str)[:4000]
            except Exception:
                req_str = str(request_data)[:4000]

        resp_str = ""
        tokens_est = 0
        if response_data is not None:
            try:
                resp_serialized = json.dumps(response_data, default=str)
                tokens_est = max(1, len(resp_serialized) // 4)
                resp_str = resp_serialized[:4000]
            except Exception:
                resp_str = str(response_data)[:4000]
                tokens_est = max(1, len(resp_str) // 4)

        op_name = f"{tool_name or 'rpc'} - {model_name or 'system'} [{status.upper()}]"

        vals = {
            "name": op_name,
            "user_id": user_id,
            "token_id": token_id,
            "tool_name": tool_name or "",
            "model_name": model_name or "",
            "execution_time_ms": execution_time_ms,
            "tokens_estimated": tokens_est,
            "status": status,
            "error_message": (error_msg or "")[:4000],
            "request_summary": req_str,
            "response_summary": resp_str,
            "ip_address": client_ip or "",
        }

        try:
            if getattr(env.registry, "test_cr", None) or getattr(env.cr, "readonly", False):
                env["mcp.audit.log"].sudo().create(vals)
            else:
                with env.registry.cursor() as audit_cr:
                    audit_env = api.Environment(audit_cr, 1, {})  # SUPERUSER_ID = 1
                    audit_env["mcp.audit.log"].create(vals)
                    audit_cr.commit()
        except Exception as err:
            _logger.warning("Could not write MCP audit log: %s", err)

    @staticmethod
    def _sanitize_payload(payload):
        sensitive_keys = {
            "password", "secret", "totp_secret", "credit_card", "api_key",
            "private_key", "passphrase", "raw_token", "token"
        }
        if isinstance(payload, dict):
            clean = {}
            for k, v in payload.items():
                if any(s in str(k).lower() for s in sensitive_keys):
                    clean[k] = "******"
                elif isinstance(v, (dict, list)):
                    clean[k] = McpAuditLog._sanitize_payload(v)
                else:
                    clean[k] = v
            return clean
        elif isinstance(payload, list):
            return [McpAuditLog._sanitize_payload(item) for item in payload]
        return payload
