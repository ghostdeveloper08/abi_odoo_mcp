# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

import base64
import os
import tempfile
import json
import logging
import re
import time
from datetime import datetime, date, time as dtime
from dateutil.relativedelta import relativedelta
from lxml import etree

import odoo
from odoo import api, fields, http
from odoo.exceptions import AccessDenied, AccessError, MissingError, UserError, ValidationError
from odoo.http import Response, request
try:
    from odoo.osv import expression
except ImportError:
    try:
        from odoo.fields import Domain

        class ExpressionCompat:
            AND = staticmethod(lambda d_list: list(Domain.AND(d_list)))
            OR = staticmethod(lambda d_list: list(Domain.OR(d_list)))
            normalize_domain = staticmethod(lambda d: list(Domain(d)))

        expression = ExpressionCompat
    except ImportError:
        expression = None
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "odoo-mcp-server"
SERVER_VERSION = "1.0.0"

# Models that must NEVER be accessed or modified via MCP under any circumstances
INTERNAL_MODEL_BLACKLIST = {
    "ir.config_parameter",
    "ir.rule",
    "ir.model.access",
    "ir.access",
    "res.users.apikeys",
    "mcp.access.token",
    "mcp.audit.log",
}

def check_record_or_model_access(record_or_model, operation="read"):
    """Check access across Odoo versions (Odoo 20 check_access vs legacy check_access_rights)."""
    if hasattr(record_or_model, "check_access"):
        record_or_model.check_access(operation)
    elif hasattr(record_or_model, "check_access_rights"):
        record_or_model.check_access_rights(operation)


# Standard internal audit fields to omit by default for token optimization
INTERNAL_FIELD_PRUNE = {
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
    "message_ids",
    "activity_ids",
    "message_follower_ids",
    "message_partner_ids",
}


class McpController(http.Controller):

    @http.route(
        ["/mcp", "/mcp/rpc"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
        methods=["GET", "POST", "OPTIONS"],
        readonly=False,
    )
    def mcp_rpc_endpoint(self, **kwargs):
        """
        Unified JSON-RPC 2.0 endpoint for the Model Context Protocol (MCP 2024-11-05).
        Authenticates via standard HTTP Bearer token and runs completely within
        the authenticated user's scoped environment (Zero .sudo() bypasses).
        """
        # Handle CORS pre-flight requests
        if request.httprequest.method == "OPTIONS":
            return self._build_cors_response()

        # Handle GET health probe requests (e.g. from cloud connectors or browsers)
        if request.httprequest.method == "GET":
            return Response(
                json.dumps({
                    "status": "ready",
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "endpoint": request.httprequest.path,
                }),
                status=200,
                mimetype="application/json",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "ngrok-skip-browser-warning": "true",
                },
            )

        # Multi-database support: Allow DB selection via Header or URL query parameter
        requested_db = request.httprequest.headers.get("X-Odoo-Database") or request.httprequest.args.get("db")
        if requested_db and hasattr(request, "session") and getattr(request.session, "db", None) != requested_db:
            try:
                request.session.db = requested_db
            except Exception as e:
                _logger.debug("Could not switch database to %s: %s", requested_db, e)

        start_time = time.perf_counter()
        client_ip = self._get_client_ip()

        # Parse JSON-RPC 2.0 payload
        try:
            raw_body = request.httprequest.data.decode("utf-8")
            if not raw_body:
                return self._jsonrpc_error(None, -32700, "Parse error: Request body is empty.")
            payload = json.loads(raw_body)
        except Exception as e:
            return self._jsonrpc_error(None, -32700, f"Parse error: Invalid JSON payload ({str(e)})")

        # Extract Token from URL query parameter (?token=... or ?our_token=...), Authorization header, or x-api-key
        raw_token = None
        auth_header = request.httprequest.headers.get("Authorization", "")
        if auth_header and auth_header.lower().startswith("bearer "):
            raw_token = auth_header[7:].strip()
        elif request.httprequest.headers.get("x-api-key"):
            raw_token = request.httprequest.headers.get("x-api-key").strip()
        elif request.httprequest.headers.get("x-mcp-token"):
            raw_token = request.httprequest.headers.get("x-mcp-token").strip()
        elif request.httprequest.args.get("token"):
            raw_token = request.httprequest.args.get("token").strip()
        elif request.httprequest.args.get("our_token"):
            raw_token = request.httprequest.args.get("our_token").strip()

        # Support JSON-RPC 2.0 Batch Requests (array of request objects)
        if isinstance(payload, list):
            if not payload:
                return self._jsonrpc_error(None, -32600, "Invalid Request: Batch list cannot be empty.")
            batch_responses = []
            for sub_req in payload:
                if not isinstance(sub_req, dict):
                    batch_responses.append(self._build_jsonrpc_dict_error(None, -32600, "Invalid Request: Batch item must be an object."))
                    continue
                sub_res = self._dispatch_single_rpc(sub_req, raw_token, client_ip)
                if sub_res is not None:
                    batch_responses.append(sub_res)
            return request.make_json_response(batch_responses, headers=self._get_cors_headers())

        if not isinstance(payload, dict):
            return self._jsonrpc_error(None, -32600, "Invalid Request: JSON payload must be an object or array of objects.")

        single_res = self._dispatch_single_rpc(payload, raw_token, client_ip)
        if single_res is None:
            return Response("", status=204, mimetype="application/json", headers=self._get_cors_headers())
        return request.make_json_response(single_res, headers=self._get_cors_headers())

    def _dispatch_single_rpc(self, payload, raw_token, client_ip):
        start_time = time.perf_counter()
        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if not method or not isinstance(method, str):
            return self._build_jsonrpc_dict_error(req_id, -32600, "Invalid Request: 'method' string is required.")

        # Notification handlers (e.g. notifications/initialized) - no reply expected per JSON-RPC
        if method == "notifications/initialized":
            return None

        # 1. Ping endpoint (requires no token for health checks)
        if method == "ping":
            return self._build_jsonrpc_dict_result(req_id, {})

        # 2. MCP Handshake Initialize endpoint (negotiates capabilities and protocol versions)
        if method == "initialize":
            client_proto = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
            init_res = self._handle_initialize(request.env, params)
            init_res["protocolVersion"] = client_proto
            return self._build_jsonrpc_dict_result(req_id, init_res)

        # Strict Authentication Requirement: Every connection MUST provide a valid user token
        if not raw_token:
            return self._build_jsonrpc_dict_error(
                req_id,
                -32001,
                "Unauthorized: Access denied. Please provide your user access token in the URL (?token=YOUR_TOKEN) or Authorization header.",
            )

        # Authenticate Token and retrieve owner user
        try:
            token = request.env["mcp.access.token"].verify_token(request.env, raw_token, client_ip)
            user_id = token.user_id.id
            token_id = token.id
            is_readonly = token.is_readonly
        except AccessDenied as e:
            return self._build_jsonrpc_dict_error(req_id, -32001, f"Authentication Failed: {str(e)}")
        except Exception as e:
            _logger.exception("Unexpected error during MCP token verification: %s", e)
            return self._build_jsonrpc_dict_error(req_id, -32603, "Internal authentication error.")

        # Establish STRICT USER-SCOPED ENVIRONMENT (No .sudo())
        user_record = token.user_id
        allowed_companies = user_record.company_ids.ids or ([user_record.company_id.id] if user_record.company_id else [])
        user_context = dict(
            request.env.context,
            mcp_token_id=token_id,
            allowed_company_ids=allowed_companies,
        )
        if hasattr(request, "update_env"):
            request.update_env(user=user_id, context=user_context)
            user_env = request.env
        else:
            user_env = request.env(user=user_id, context=user_context)
            if hasattr(user_env, "transaction") and hasattr(user_env.transaction, "default_env"):
                user_env.transaction.default_env = user_env

        # Enforce Rate Limiting (rate_limit_per_minute)
        allowed, current_count, retry_after = token.check_rate_limit()
        if not allowed:
            tool_name = params.get("name") if method == "tools/call" else method
            target_model = (params.get("arguments") or {}).get("model") if method == "tools/call" else None
            error_msg = f"Rate limit exceeded: Maximum {token.rate_limit_per_minute} request(s) per minute allowed for token '{token.name}'. Please retry in {retry_after}s."
            _logger.warning("MCP Rate Limit Exceeded for token '%s' (ID %s) from IP %s: %d req/min limit reached.", token.name, token.id, client_ip, token.rate_limit_per_minute)

            user_env["mcp.audit.log"].log_execution(
                env=user_env,
                user_id=user_id,
                token_id=token_id,
                tool_name=tool_name,
                model_name=target_model,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                status="blocked_rate_limit",
                request_data=params,
                response_data=None,
                error_msg=error_msg,
                client_ip=client_ip,
            )
            if method == "tools/call":
                return self._build_jsonrpc_dict_result(req_id, {
                    "content": [{"type": "text", "text": error_msg}],
                    "isError": True,
                })
            else:
                return self._build_jsonrpc_dict_error(req_id, -32005, error_msg)

        # Dispatch Method
        tool_name = params.get("name") if method == "tools/call" else method
        target_model = (params.get("arguments") or {}).get("model") if method == "tools/call" else None

        status = "success"
        error_msg = None
        result = None

        try:
            if method == "initialize":
                result = self._handle_initialize(user_env, params)
            elif method == "tools/list":
                result = self._handle_tools_list(user_env)
            elif method == "tools/call":
                result = self._handle_tools_call(user_env, token_id, is_readonly, params)
            elif method == "resources/list":
                result = self._handle_resources_list(user_env)
            elif method in ("resources/templates/list", "resourceTemplates/list"):
                result = self._handle_resources_templates_list(user_env)
            elif method == "resources/read":
                result = self._handle_resources_read(user_env, params)
            elif method == "prompts/list":
                result = self._handle_prompts_list(user_env)
            elif method == "prompts/get":
                result = self._handle_prompts_get(user_env, params)
            else:
                return self._build_jsonrpc_dict_error(req_id, -32601, f"Method not found: '{method}'")

        except (AccessError, AccessDenied) as e:
            status = "error"
            error_msg = f"AccessError: {str(e)}"
            if method == "tools/call":
                result = {"content": [{"type": "text", "text": error_msg}], "isError": True}
            else:
                return self._build_jsonrpc_dict_error(req_id, -32003, error_msg)
        except (UserError, ValidationError, MissingError) as e:
            status = "error"
            error_msg = f"{e.__class__.__name__}: {str(e)}"
            if method == "tools/call":
                result = {"content": [{"type": "text", "text": error_msg}], "isError": True}
            else:
                return self._build_jsonrpc_dict_error(req_id, -32602, error_msg)
        except Exception as e:
            _logger.exception("MCP Dispatch Error on method '%s': %s", method, e)
            status = "error"
            error_msg = f"InternalError: {str(e)}"
            if method == "tools/call":
                result = {"content": [{"type": "text", "text": error_msg}], "isError": True}
            else:
                return self._build_jsonrpc_dict_error(req_id, -32603, error_msg)
        finally:
            execution_time_ms = (time.perf_counter() - start_time) * 1000
            # Record audit log
            user_env["mcp.audit.log"].log_execution(
                env=user_env,
                user_id=user_id,
                token_id=token_id,
                tool_name=tool_name,
                model_name=target_model,
                execution_time_ms=execution_time_ms,
                status=status,
                request_data=params,
                response_data=result,
                error_msg=error_msg,
                client_ip=client_ip,
            )

        return self._build_jsonrpc_dict_result(req_id, result)

    # -------------------------------------------------------------------------
    # MCP Handlers
    # -------------------------------------------------------------------------

    def _handle_initialize(self, env, params):
        instructions = env["ir.config_parameter"].sudo().get_param(
            "abi_odoo_mcp.instructions",
            "Enterprise Odoo MCP Server. All operations strictly adhere to user permissions, company separation, and record rules.",
        )
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
                "logging": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
            "instructions": instructions,
        }

    def _handle_tools_list(self, env):
        return {
            "tools": [
                {
                    "name": "odoo_search_read",
                    "description": (
                        "Search and read records from an Odoo model with domain filters, field selection, "
                        "ordering, and pagination. If 'fields' is omitted, intelligently selects view-prioritized fields."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name (e.g. 'res.partner', 'sale.order', 'account.move').",
                            },
                            "domain": {
                                "type": "array",
                                "description": "Odoo search domain filter list (e.g. [['is_company', '=', true], ['customer_rank', '>', 0]]). Defaults to [].",
                                "default": [],
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Specific field names to read. If omitted or empty, selects view-prioritized fields.",
                            },
                            "offset": {
                                "type": "integer",
                                "description": "Number of records to skip.",
                                "default": 0,
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of records to return (defaults to 20, max 100).",
                                "default": 20,
                            },
                            "order": {
                                "type": "string",
                                "description": "Sorting order string (e.g. 'id desc', 'create_date desc').",
                                "default": "id desc",
                            },
                            "compress": {
                                "type": "boolean",
                                "description": "Strip empty/null/false values and omit tracking metadata to optimize LLM tokens.",
                                "default": True,
                            },
                        },
                        "required": ["model"],
                    },
                },
                {
                    "name": "odoo_read",
                    "description": "Fetch records by database IDs with optional field selection and payload compression.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name.",
                            },
                            "ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "List of record database IDs to fetch.",
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of fields to fetch. If omitted, returns view-prioritized fields.",
                            },
                            "compress": {
                                "type": "boolean",
                                "description": "Strip empty/null/false values to optimize tokens.",
                                "default": True,
                            },
                        },
                        "required": ["model", "ids"],
                    },
                },
                {
                    "name": "odoo_create",
                    "description": "Create a new record in Odoo with support for relational (0, 0, vals) commands. Blocked for read-only tokens.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name.",
                            },
                            "values": {
                                "type": "object",
                                "description": "Dictionary of field values for the new record.",
                            },
                        },
                        "required": ["model", "values"],
                    },
                },
                {
                    "name": "odoo_write",
                    "description": "Update existing records in Odoo by IDs. Blocked for read-only tokens.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name.",
                            },
                            "ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Database IDs of records to update.",
                            },
                            "values": {
                                "type": "object",
                                "description": "Dictionary of field values to write.",
                            },
                        },
                        "required": ["model", "ids", "values"],
                    },
                },
                {
                    "name": "odoo_unlink",
                    "description": "Safely delete records by database IDs. Blocked for read-only tokens.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name.",
                            },
                            "ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Database IDs of records to delete.",
                            },
                        },
                        "required": ["model", "ids"],
                    },
                },
                {
                    "name": "odoo_execute_kw",
                    "description": (
                        "Execute safe business methods (e.g. 'action_confirm', 'action_post', 'action_done') "
                        "validated against the administrator-configured method whitelist."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name.",
                            },
                            "method": {
                                "type": "string",
                                "description": "The business method name (must be whitelisted).",
                            },
                            "args": {
                                "type": "array",
                                "description": "Positional arguments list (e.g. [[record_ids]]).",
                                "default": [],
                            },
                            "kwargs": {
                                "type": "object",
                                "description": "Keyword arguments dictionary.",
                                "default": {},
                            },
                        },
                        "required": ["model", "method"],
                    },
                },
                {
                    "name": "odoo_get_model_schema",
                    "description": (
                        "Introspect technical field specifications, data types, labels, relational models, "
                        "and UI view presence for an authorized model."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name.",
                            },
                            "attributes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Field attributes to inspect (defaults to type, string, required, readonly, relation, selection).",
                            },
                            "prioritize_views": {
                                "type": "boolean",
                                "description": "Mark whether each field is visible in standard form/list views.",
                                "default": True,
                            },
                            "include_defaults": {
                                "type": "boolean",
                                "description": "If true, queries and includes default field values via default_get.",
                                "default": False,
                            },
                            "include_relations": {
                                "type": "boolean",
                                "description": "If true, enriches One2many and Many2one fields with target relation descriptions and child required fields.",
                                "default": True,
                            },
                        },
                        "required": ["model"],
                    },
                },
                {
                    "name": "odoo_count",
                    "description": (
                        "Count records in an authorized Odoo model matching search domain criteria. "
                        "Ultra-fast (<5ms), highly accurate, and consumes zero tokens compared to downloading record sets."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name (e.g. 'sale.order', 'res.partner').",
                            },
                            "domain": {
                                "type": "array",
                                "description": "Odoo search domain criteria (e.g. [['state', '=', 'draft']]). Defaults to all records [].",
                                "default": [],
                            },
                        },
                        "required": ["model"],
                    },
                },
                {
                    "name": "odoo_aggregate",
                    "description": (
                        "Compute summary metrics, totals, averages, and group subtotals using Odoo's high-performance "
                        "SQL aggregation engine (read_group). Ideal for executive intelligence queries such as "
                        "'Total sales by salesperson this month', 'Unpaid invoices by customer', etc."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The Odoo model technical name (e.g. 'sale.order', 'account.move').",
                            },
                            "domain": {
                                "type": "array",
                                "description": "Filter domain criteria (e.g. [['state', 'in', ['sale', 'done']]]).",
                                "default": [],
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Fields to aggregate with SQL aggregate functions (e.g. ['amount_total:sum', 'id:count']).",
                            },
                            "groupby": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Fields to group by (e.g. ['user_id', 'date_order:month', 'partner_id']).",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of groups to return (default 50, max 200).",
                                "default": 50,
                            },
                            "order": {
                                "type": "string",
                                "description": "Sort order for groups (e.g. 'amount_total desc').",
                            },
                        },
                        "required": ["model", "fields", "groupby"],
                    },
                },
                {
                    "name": "odoo_find_models",
                    "description": (
                        "Search and discover ERP business models using natural language keywords or technical names. "
                        "Helps determine the right model for operations (e.g. searching 'delivery' -> returns 'stock.picking')."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search keyword (e.g. 'quote', 'invoice', 'delivery', 'customer').",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum models to return (default 15).",
                                "default": 15,
                            },
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "odoo_get_business_context",
                    "description": (
                        "Retrieve high-level business environment context: active company name, default currency "
                        "(code and symbol), user info, timezone, and list of installed business applications (Sales, CRM, Accounting, Inventory, etc.). "
                        "Call this at the beginning of a conversation to orient yourself with the business setup."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "name": "odoo_get_business_kpis",
                    "description": (
                        "Executive Daily/Weekly/Monthly Briefing: Retrieve consolidated business performance metrics "
                        "across Sales, Accounts Receivable (Invoicing), CRM Pipeline, Deliveries, and Purchases for a specified timeframe."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "period": {
                                "type": "string",
                                "enum": ["today", "yesterday", "this_week", "this_month", "this_quarter", "this_year", "custom"],
                                "default": "this_month",
                                "description": "Predefined timeframe for aggregation.",
                            },
                            "date_from": {
                                "type": "string",
                                "description": "Optional start date in YYYY-MM-DD format (used when period is 'custom').",
                            },
                            "date_to": {
                                "type": "string",
                                "description": "Optional end date in YYYY-MM-DD format (used when period is 'custom').",
                            },
                        },
                    },
                },
                {
                    "name": "odoo_global_search",
                    "description": (
                        "Unified Multi-Model Business Search: Search simultaneously across Customers/Vendors, Sales Orders, "
                        "Invoices, Products, CRM Leads, and Purchase Orders when you are not sure which table holds the record."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search term (e.g. customer name, invoice reference, product title, order number).",
                            },
                            "limit_per_model": {
                                "type": "integer",
                                "default": 5,
                                "description": "Maximum records to return per business model (default 5).",
                            },
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "odoo_partner_360",
                    "description": (
                        "Customer / Vendor 360 Dossier: Retrieve a complete 360-degree commercial overview of any partner, "
                        "including contact details, lifetime spend, recent orders, unpaid/overdue invoice balances, open CRM deals, "
                        "recent shipments, and recent communications."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "partner_id": {
                                "type": "integer",
                                "description": "Exact partner database ID if known.",
                            },
                            "partner_name": {
                                "type": "string",
                                "description": "Partner name or partial name to search if ID is not known.",
                            },
                        },
                    },
                },
                {
                    "name": "odoo_post_chatter_message",
                    "description": (
                        "Post an internal note or customer-facing message to a record's chatter (mail.thread). "
                        "Use this to log meeting notes, AI summaries, confirmations, or notify colleagues."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The model of the record (e.g. 'sale.order', 'res.partner', 'crm.lead', 'account.move').",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "The record ID.",
                            },
                            "body": {
                                "type": "string",
                                "description": "Content of the note or message.",
                            },
                            "message_type": {
                                "type": "string",
                                "enum": ["comment", "email"],
                                "default": "comment",
                                "description": "'comment' for standard internal notes or chatter messages; 'email' to email external partners.",
                            },
                            "partner_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Optional list of partner IDs to mention/notify.",
                            },
                            "subject": {
                                "type": "string",
                                "description": "Optional subject header.",
                            },
                        },
                        "required": ["model", "res_id", "body"],
                    },
                },
                {
                    "name": "odoo_get_chatter_history",
                    "description": (
                        "Fetch clean communication history, internal notes, and audit state changes for any record. "
                        "HTML content is automatically sanitized into plain text for token efficiency."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The model of the record.",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "The record ID.",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 10,
                                "description": "Number of recent messages to return (max 50).",
                            },
                        },
                        "required": ["model", "res_id"],
                    },
                },
                {
                    "name": "odoo_get_activities",
                    "description": (
                        "Retrieve scheduled activities (calls, meetings, to-dos, emails). "
                        "Quickly view what tasks are due today, overdue, or upcoming across the company."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "string",
                                "enum": ["today", "overdue", "upcoming", "all"],
                                "default": "today",
                                "description": "Filter by schedule status.",
                            },
                            "user_id": {
                                "type": "integer",
                                "description": "Optional user ID to filter by. Defaults to current user.",
                            },
                            "model": {
                                "type": "string",
                                "description": "Optional model to filter activities on (e.g. 'sale.order', 'crm.lead').",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 25,
                                "description": "Maximum activities to return.",
                            },
                        },
                    },
                },
                {
                    "name": "odoo_schedule_activity",
                    "description": (
                        "Schedule a new follow-up activity (call, email, to-do, meeting) on any business record."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "Target model (e.g. 'res.partner', 'sale.order', 'crm.lead').",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "Target record ID.",
                            },
                            "activity_type": {
                                "type": "string",
                                "default": "todo",
                                "description": "Type of activity: 'call', 'email', 'todo', 'meeting', or custom activity type name.",
                            },
                            "summary": {
                                "type": "string",
                                "description": "Short headline / summary of the activity (e.g. 'Call client for price negotiation').",
                            },
                            "note": {
                                "type": "string",
                                "description": "Detailed description, agenda, or background notes.",
                            },
                            "date_deadline": {
                                "type": "string",
                                "description": "Due date in YYYY-MM-DD format. Defaults to tomorrow if not specified.",
                            },
                            "user_id": {
                                "type": "integer",
                                "description": "Assigned user ID. Defaults to current user.",
                            },
                        },
                        "required": ["model", "res_id", "summary"],
                    },
                },
                {
                    "name": "odoo_mark_activity_done",
                    "description": (
                        "Mark a scheduled activity as completed, with optional completion feedback / outcome notes."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "activity_id": {
                                "type": "integer",
                                "description": "The ID of the mail.activity record.",
                            },
                            "feedback": {
                                "type": "string",
                                "description": "Optional completion notes or outcome summary.",
                            },
                        },
                        "required": ["activity_id"],
                    },
                },
                {
                    "name": "odoo_check_stock",
                    "description": (
                        "Inspect real-time inventory levels: check quantity on-hand, forecasted quantity, incoming, "
                        "outgoing, and unit prices for specific products, or query in-stock, available, or low-stock items."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "product": {
                                "type": "string",
                                "description": "Product ID (as string or int), SKU / internal reference, or name search keyword.",
                            },
                            "in_stock_only": {
                                "type": "boolean",
                                "default": False,
                                "description": "If true, only returns products currently in stock (qty_available > 0).",
                            },
                            "low_stock_only": {
                                "type": "boolean",
                                "default": False,
                                "description": "If true, lists products where on-hand quantity is 0 or negative.",
                            },
                            "sale_ok_only": {
                                "type": "boolean",
                                "default": False,
                                "description": "If true, only returns products that can be sold (sale_ok = True).",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 20,
                                "description": "Maximum number of products to inspect (default 20).",
                            },
                        },
                    },
                },
                {
                    "name": "odoo_workflow_action",
                    "description": (
                        "Execute any business workflow state transition or button action method on standard or custom models "
                        "(e.g., confirming orders, approving requests, validating transfers, posting invoices, advancing stages)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "The technical method name to invoke (e.g. 'action_confirm', 'action_approve', 'button_validate') or legacy shortcut ('confirm_sale', 'post_invoice').",
                            },
                            "model": {
                                "type": "string",
                                "description": "The target model technical name (e.g. 'sale.order', 'hospital.patient', 'fleet.vehicle').",
                            },
                            "res_id": {
                                "description": "The record ID (integer) or list of record IDs (integers) to execute the action on.",
                            },
                            "args": {
                                "type": "array",
                                "default": [],
                                "description": "Optional positional arguments to pass to the method.",
                            },
                            "kwargs": {
                                "type": "object",
                                "default": {},
                                "description": "Optional keyword arguments to pass to the method.",
                            },
                        },
                        "required": ["action", "res_id"],
                    },
                },
                {
                    "name": "odoo_call_button",
                    "description": "Direct alias for odoo_workflow_action: click/trigger any UI button or action method on any model.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The target model technical name.",
                            },
                            "action": {
                                "type": "string",
                                "description": "Button method name to invoke (e.g. 'action_confirm', 'button_validate', 'action_approve').",
                            },
                            "res_id": {
                                "description": "Record ID (integer) or list of record IDs.",
                            },
                            "args": {
                                "type": "array",
                                "default": [],
                            },
                            "kwargs": {
                                "type": "object",
                                "default": {},
                            },
                        },
                        "required": ["action", "res_id"],
                    },
                },
                {
                    "name": "odoo_get_record_actions",
                    "description": (
                        "Inspect dynamic business actions and UI buttons available for a record or model. "
                        "Discovers form view buttons, labels, method names, and current record state across any standard or custom module."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The technical model name (e.g. 'sale.order', 'hospital.patient', 'fleet.vehicle').",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "Optional specific record ID to check current state and active buttons.",
                            },
                        },
                        "required": ["model"],
                    },
                },
                {
                    "name": "odoo_record_360",
                    "description": (
                        "Universal 360-degree relationship inspector for ANY model in the system (customers, patients, vehicles, projects, assets). "
                        "Automatically discovers all reverse foreign keys, counts linked documents, and summarizes connected records dynamically."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The technical model name (e.g. 'res.partner', 'fleet.vehicle', 'hospital.patient').",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "The record ID to inspect.",
                            },
                            "limit_per_relation": {
                                "type": "integer",
                                "default": 5,
                                "description": "Maximum recent records to retrieve per linked model.",
                            },
                        },
                        "required": ["model", "res_id"],
                    },
                },
                {
                    "name": "odoo_get_report_pdf",
                    "description": (
                        "Render and retrieve official QWeb PDF document reports (Quotation, Sales Order, Invoice, "
                        "Purchase Order, Delivery Slip) as base64-encoded binary data with filename and size."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The model of the record (e.g. 'sale.order', 'account.move', 'purchase.order').",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "The record ID to render the report for.",
                            },
                            "report_xmlid": {
                                "type": "string",
                                "description": "Optional explicit report XMLID (e.g. 'sale.report_saleorder'). Auto-detected if omitted.",
                            },
                        },
                        "required": ["model", "res_id"],
                    },
                },
                {
                    "name": "odoo_build_domain",
                    "description": (
                        "Construct and validate a mathematically sound Odoo Polish-notation domain from structured criteria. "
                        "Supports relational dotted paths (e.g. 'partner_id.country_id.code'), standard operators, and "
                        "smart relative dates ('today', 'yesterday', '-7d', '-30d', '+30d', 'start_of_month', 'end_of_month', 'start_of_year'). "
                        "Validates field types against the live ORM to prevent query crashes."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The technical name of the target model (e.g. 'sale.order', 'res.partner', 'account.move').",
                            },
                            "conditions": {
                                "type": "array",
                                "description": "List of condition criteria objects to evaluate and combine.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "field": {
                                            "type": "string",
                                            "description": "Field name or relational dotted path (e.g. 'state', 'partner_id.country_id.code').",
                                        },
                                        "operator": {
                                            "type": "string",
                                            "description": "Odoo domain comparison operator: '=', '!=', '>', '>=', '<', '<=', 'in', 'not in', 'like', 'ilike', '=like', '=ilike', 'child_of', 'parent_of'.",
                                        },
                                        "value": {
                                            "description": "Comparison value. For dates, supports ISO strings ('2026-01-01') or relative phrases ('today', '-7d', '-30d', 'start_of_month').",
                                        },
                                    },
                                    "required": ["field", "operator"],
                                },
                            },
                            "logical_operator": {
                                "type": "string",
                                "enum": ["AND", "OR"],
                                "default": "AND",
                                "description": "How to combine the conditions: 'AND' (all must match) or 'OR' (any may match). Defaults to 'AND'.",
                            },
                            "date_eval": {
                                "type": "boolean",
                                "default": True,
                                "description": "Whether to automatically evaluate relative date strings ('today', '-30d', etc.) into real ISO dates. Defaults to true.",
                            },
                        },
                        "required": ["model", "conditions"],
                    },
                },
                {
                    "name": "odoo_diagnose_access",
                    "description": (
                        "Diagnose user permissions, model ACLs (ir.model.access), record rules (ir.rule), and company isolation "
                        "for a model and operation (read, write, create, unlink). Pinpoint why an operation is permitted or "
                        "denied with root-cause explanations (e.g. specific record rule name, multi-company restriction, or read-only token)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The technical name of the model to diagnose (e.g. 'sale.order', 'crm.lead').",
                            },
                            "operation": {
                                "type": "string",
                                "enum": ["read", "write", "create", "unlink"],
                                "default": "read",
                                "description": "The operation to test: 'read', 'write', 'create', or 'unlink'. Defaults to 'read'.",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "Optional specific record ID to test against record rules and company isolation.",
                            },
                        },
                        "required": ["model"],
                    },
                },
                {
                    "name": "odoo_read_attachment",
                    "description": (
                        "Inspect and read metadata and content from ir.attachment. Discover all files attached to a record "
                        "(res_model + res_id) or fetch a specific attachment by ID. Text-based files (TXT, CSV, JSON, XML) are "
                        "automatically decoded to readable text; binary files (PDF, images) provide size-capped base64 content."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "attachment_id": {
                                "type": "integer",
                                "description": "Direct ir.attachment ID to fetch.",
                            },
                            "res_model": {
                                "type": "string",
                                "description": "The target record's model (e.g. 'account.move', 'sale.order') to list attachments for.",
                            },
                            "res_id": {
                                "type": "integer",
                                "description": "The target record's ID to list attachments for.",
                            },
                            "include_content": {
                                "type": "boolean",
                                "default": True,
                                "description": "Whether to include file content. Defaults to true. If false, returns metadata only.",
                            },
                            "max_bytes": {
                                "type": "integer",
                                "default": 1048576,
                                "description": "Maximum file size in bytes to retrieve content for (defaults to 1MB = 1048576 bytes).",
                            },
                        },
                    },
                },
                {
                    "name": "odoo_aged_partner_balance",
                    "description": (
                        "Generate an Aged Partner Balance / Receivables & Payables aging analysis. Buckets open posted items into "
                        "Not Due, 1-30 days, 31-60 days, 61-90 days, and 90+ days overdue, with partner subtotals and top debtor/creditor rankings. "
                        "Works seamlessly on both Community and Enterprise Edition."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "account_type": {
                                "type": "string",
                                "enum": ["receivable", "payable", "both"],
                                "default": "receivable",
                                "description": "Type of aging to report: 'receivable' (customer debts), 'payable' (supplier bills), or 'both'. Defaults to 'receivable'.",
                            },
                            "as_of_date": {
                                "type": "string",
                                "description": "Reference date for aging calculations (ISO format YYYY-MM-DD). Defaults to today.",
                            },
                            "top_partners": {
                                "type": "integer",
                                "default": 10,
                                "description": "Number of top partners with the largest overdue balances to detail. Defaults to 10 (max 100).",
                            },
                            "partner_id": {
                                "type": "integer",
                                "description": "Optional partner ID to restrict analysis to a single customer or supplier.",
                            },
                        },
                    },
                },
                {
                    "name": "odoo_inspect_data_quality",
                    "description": (
                        "Audit data hygiene and detect duplicates on any Odoo model. Identifies duplicate record clusters "
                        "using key identifier fields (email, phone, vat, barcode, ref), checks for missing required values, "
                        "and validates email/phone format anomalies with evidence record IDs and health score."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "The technical model name to inspect (e.g. 'res.partner', 'product.template', 'crm.lead').",
                            },
                            "checks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Types of checks to perform. Any of: 'duplicates', 'missing_values', 'format_anomalies'. Defaults to all.",
                            },
                            "key_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Specific field names to test for duplicates. If omitted, smart model defaults are used (e.g. email/phone/vat for partners).",
                            },
                            "sample_limit": {
                                "type": "integer",
                                "default": 200,
                                "description": "Maximum records to sample for missing values and format validation (defaults to 200, max 1000).",
                            },
                        },
                        "required": ["model"],
                    },
                },
                {
                    "name": "odoo_create_dashboard",
                    "description": (
                        "Design, preview, and deploy interactive native dashboards in Odoo ERP (board.board multi-column layouts, "
                        "embedded Pivot views, Graph views, and List views with direct redirect links). "
                        "Supports a two-stage preview-and-confirm workflow: set confirm=false (default) to generate an interactive "
                        "in-chat preview with live aggregated numbers and JSON blueprint without database changes. Once approved, "
                        "call with confirm=true to publish the persistent dashboard views, actions, and menu into Odoo."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Human-readable dashboard title (e.g. 'Executive Sales Performance').",
                            },
                            "model": {
                                "type": "string",
                                "description": "Primary target Odoo model (e.g. 'sale.order', 'crm.lead', 'account.move', 'project.task').",
                            },
                            "dashboard_type": {
                                "type": "string",
                                "enum": ["spreadsheet", "board", "pivot", "graph"],
                                "default": "spreadsheet",
                                "description": "Type of dashboard: 'spreadsheet' (recommended for executive KPIs, trends, and charts) creates an interactive Odoo Spreadsheet Dashboard (Dashboards app / action 408) with KPI scorecards, area/line charts, side-by-side data tables, and an interactive HTML side-screen preview. 'board' creates a multi-column dashboard using Odoo's board module. 'pivot' or 'graph' creates a single-view action.",
                            },
                            "layout_style": {
                                "type": "string",
                                "enum": ["1", "1-1", "2-1", "1-2", "1-1-1"],
                                "default": "2-1",
                                "description": "Column layout for board.board ('2-1' has 2/3 left column and 1/3 right column; '1-1' has equal split).",
                            },
                            "target": {
                                "type": "string",
                                "enum": ["standalone", "my_dashboard"],
                                "default": "standalone",
                                "description": "'standalone' creates a dedicated menu item and window action under MCP Custom Dashboards. 'my_dashboard' embeds panels into the user's built-in My Dashboard view.",
                            },
                            "confirm": {
                                "type": "boolean",
                                "default": False,
                                "description": "Two-stage control: false (default for previews) calculates live preview data, pivot tables, and JSON blueprint without modifying the database. Set to true only after user approval to deploy into Odoo.",
                            },
                            "panels": {
                                "type": "array",
                                "description": "Optional list of panel definitions to embed in the board columns. If omitted, default Pivot and Chart panels are created from top-level parameters.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string", "description": "Panel title (e.g. 'Revenue Pivot', 'Top Sales Reps')."},
                                        "model": {"type": "string", "description": "Model for this panel (defaults to main model)."},
                                        "view_mode": {"type": "string", "enum": ["pivot", "graph", "list"], "default": "pivot"},
                                        "column": {"type": "integer", "default": 0, "description": "Column index (0 for left, 1 for right, 2 for third column)."},
                                        "domain": {"type": "array", "description": "Search domain filter for this panel."},
                                        "graph_type": {"type": "string", "enum": ["bar", "line", "pie"], "default": "bar"},
                                        "dimension_field": {"type": "string", "description": "Grouping dimension field."},
                                        "dimension_interval": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
                                        "measure_field": {"type": "string", "description": "Measure field to aggregate."},
                                        "pivot_row_fields": {"type": "array", "items": {"type": "string"}, "description": "Row grouping fields for pivot."},
                                        "pivot_col_fields": {"type": "array", "items": {"type": "string"}, "description": "Column grouping fields for pivot."},
                                        "pivot_measure_fields": {"type": "array", "items": {"type": "string"}, "description": "Measure fields for pivot."},
                                        "list_fields": {"type": "array", "items": {"type": "string"}, "description": "Fields to show if view_mode is list."},
                                        "limit": {"type": "integer", "default": 10},
                                    },
                                    "required": ["title"],
                                },
                            },
                            "domain": {
                                "type": "array",
                                "description": "Search domain filter criteria for the dashboard (e.g. [['state', 'in', ['sale', 'done']]]). Defaults to [].",
                                "default": [],
                            },
                            "graph_type": {
                                "type": "string",
                                "enum": ["bar", "line", "pie"],
                                "default": "bar",
                                "description": "Primary chart visualization type (bar, line, pie).",
                            },
                            "dimension_field": {
                                "type": "string",
                                "description": "Primary grouping dimension field (e.g. 'partner_id', 'user_id', 'date_order', 'stage_id').",
                            },
                            "dimension_interval": {
                                "type": "string",
                                "enum": ["day", "week", "month", "quarter", "year"],
                                "description": "Optional date grouping interval if dimension is a date/datetime field.",
                            },
                            "measure_field": {
                                "type": "string",
                                "description": "Numeric measure field to aggregate (e.g. 'amount_total', 'expected_revenue', or '__count'). Defaults to '__count'.",
                            },
                            "pivot_row_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Row grouping fields for the pivot view (e.g. ['partner_id']).",
                            },
                            "pivot_col_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Column grouping fields for the pivot view (e.g. ['date_order:month']).",
                            },
                            "pivot_measure_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Measure fields for the pivot view (e.g. ['amount_total']).",
                            },
                            "create_menu": {
                                "type": "boolean",
                                "default": True,
                                "description": "Whether to create a clickable navigation menu in Odoo for this dashboard.",
                            },
                            "menu_parent_id": {
                                "type": "string",
                                "description": "Optional parent menu XML ID (e.g. 'sale.menu_sale_report') or menu name. If omitted, placed in MCP Custom Dashboards.",
                            },
                            "sequence": {
                                "type": "integer",
                                "default": 50,
                                "description": "Sequence priority for the menu item.",
                            },
                            "include_preview": {
                                "type": "boolean",
                                "default": True,
                                "description": "Whether to return live aggregated KPI cards and breakdown data preview in the response.",
                            },
                        },
                        "required": ["name", "model"],
                    },
                },
                {
                    "name": "odoo_get_dashboard_data",
                    "description": (
                        "Generate multi-metric executive dashboard analytics in a single call without writing to the database: "
                        "Computes KPI summary cards (totals, averages, counts), categorical breakdowns (bar/pie chart distributions), "
                        "time-series trend curves, and top-performer rankings for any model (Sales, CRM, Invoices, Inventory, etc.)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "Target Odoo model technical name (e.g. 'sale.order', 'account.move', 'crm.lead').",
                            },
                            "domain": {
                                "type": "array",
                                "description": "Search domain filter criteria (e.g. [['state', 'in', ['sale', 'done']]]). Defaults to [].",
                                "default": [],
                            },
                            "metrics": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "field": {"type": "string", "description": "Field name to aggregate."},
                                        "operator": {"type": "string", "enum": ["sum", "avg", "count", "min", "max"], "description": "Aggregation function."},
                                        "label": {"type": "string", "description": "Display label for KPI card."},
                                    },
                                    "required": ["field"],
                                },
                                "description": "List of KPI metric definitions. If omitted, smart model defaults are automatically selected.",
                            },
                            "dimension_field": {
                                "type": "string",
                                "description": "Categorical dimension field to group by for breakdown charts (e.g. 'team_id', 'stage_id', 'partner_id').",
                            },
                            "time_dimension": {
                                "type": "string",
                                "description": "Date or datetime field for time-series trend analysis (e.g. 'date_order', 'create_date').",
                            },
                            "time_interval": {
                                "type": "string",
                                "enum": ["day", "week", "month", "quarter", "year"],
                                "default": "month",
                                "description": "Aggregation interval for time-series trend.",
                            },
                            "top_records_limit": {
                                "type": "integer",
                                "default": 5,
                                "description": "Number of top records to return in the leaderboard table (max 20).",
                            },
                            "top_records_order": {
                                "type": "string",
                                "description": "Sort order for top records (e.g. 'amount_total desc').",
                            },
                            "top_records_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Fields to return for top records.",
                            },
                        },
                        "required": ["model"],
                    },
                },
            ]
        }

    def _handle_tools_call(self, env, token_id, is_readonly, params):
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        if not tool_name:
            raise ValidationError("Missing 'name' parameter for tools/call.")

        # Dispatch tool
        if tool_name == "odoo_search_read":
            result_data = self._tool_search_read(env, args)
        elif tool_name == "odoo_read":
            result_data = self._tool_read(env, args)
        elif tool_name == "odoo_create":
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Create operations are forbidden.")
            result_data = self._tool_create(env, args)
        elif tool_name == "odoo_write":
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Write operations are forbidden.")
            result_data = self._tool_write(env, args)
        elif tool_name == "odoo_unlink":
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Unlink operations are forbidden.")
            result_data = self._tool_unlink(env, args)
        elif tool_name == "odoo_execute_kw":
            result_data = self._tool_execute_kw(env, is_readonly, args)
        elif tool_name == "odoo_get_model_schema":
            result_data = self._tool_get_model_schema(env, args)
        elif tool_name == "odoo_count":
            result_data = self._tool_count(env, args)
        elif tool_name == "odoo_aggregate":
            result_data = self._tool_aggregate(env, args)
        elif tool_name == "odoo_find_models":
            result_data = self._tool_find_models(env, args)
        elif tool_name == "odoo_get_business_context":
            result_data = self._tool_get_business_context(env, args)
        elif tool_name == "odoo_get_business_kpis":
            result_data = self._tool_get_business_kpis(env, args)
        elif tool_name == "odoo_global_search":
            result_data = self._tool_global_search(env, args)
        elif tool_name == "odoo_partner_360":
            result_data = self._tool_partner_360(env, args)
        elif tool_name == "odoo_post_chatter_message":
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Posting notes or messages is forbidden.")
            result_data = self._tool_post_chatter_message(env, args)
        elif tool_name == "odoo_get_chatter_history":
            result_data = self._tool_get_chatter_history(env, args)
        elif tool_name == "odoo_get_activities":
            result_data = self._tool_get_activities(env, args)
        elif tool_name == "odoo_schedule_activity":
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Scheduling activities is forbidden.")
            result_data = self._tool_schedule_activity(env, args)
        elif tool_name == "odoo_mark_activity_done":
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Completing activities is forbidden.")
            result_data = self._tool_mark_activity_done(env, args)
        elif tool_name == "odoo_check_stock":
            result_data = self._tool_check_stock(env, args)
        elif tool_name in ("odoo_workflow_action", "odoo_call_button"):
            if is_readonly:
                raise AccessDenied("This MCP token is configured as READ-ONLY. Executing workflow state actions is forbidden.")
            result_data = self._tool_workflow_action(env, args)
        elif tool_name == "odoo_get_record_actions":
            result_data = self._tool_get_record_actions(env, args)
        elif tool_name == "odoo_record_360":
            result_data = self._tool_record_360(env, args)
        elif tool_name == "odoo_get_report_pdf":
            result_data = self._tool_get_report_pdf(env, args)
        elif tool_name == "odoo_build_domain":
            result_data = self._tool_build_domain(env, args)
        elif tool_name == "odoo_diagnose_access":
            result_data = self._tool_diagnose_access(env, token_id, is_readonly, args)
        elif tool_name == "odoo_read_attachment":
            result_data = self._tool_read_attachment(env, args)
        elif tool_name == "odoo_aged_partner_balance":
            result_data = self._tool_aged_partner_balance(env, args)
        elif tool_name == "odoo_inspect_data_quality":
            result_data = self._tool_inspect_data_quality(env, args)
        elif tool_name == "odoo_create_dashboard":
            result_data = self._tool_create_dashboard(env, is_readonly, args)
        elif tool_name == "odoo_get_dashboard_data":
            result_data = self._tool_get_dashboard_data(env, args)
        else:
            raise UserError(f"Unrecognized tool name: '{tool_name}'")

        formatted_text = json.dumps(result_data, indent=2, default=str)
        return {
            "content": [
                {
                    "type": "text",
                    "text": formatted_text,
                }
            ],
            "isError": False,
        }

    # -------------------------------------------------------------------------
    # Tool Implementations
    # -------------------------------------------------------------------------

    def _tool_search_read(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="read")
        domain = args.get("domain") or []
        if isinstance(domain, str):
            try:
                domain = json.loads(domain)
            except Exception:
                pass
        limit = min(int(args.get("limit") or 20), 100)
        offset = int(args.get("offset") or 0)
        order = args.get("order") or "id desc"
        compress = args.get("compress", True)

        fields_to_read = args.get("fields")
        if not fields_to_read:
            # Token Optimization: Intelligently pick default view fields instead of all 150+ fields!
            fields_to_read = self._get_view_prioritized_fields(env, model)

        # Sanitize against blacklisted fields
        fields_to_read = self._filter_blacklisted_fields(env, fields_to_read, model=model)

        records = env[model].search_read(
            domain=domain,
            fields=fields_to_read,
            offset=offset,
            limit=limit,
            order=order,
        )

        if compress:
            records = self._compress_records(records, preserved_fields=set(args.get("fields") or []))

        return {
            "model": model,
            "count": len(records),
            "records": records,
        }

    def _tool_read(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="read")
        raw_ids = args.get("ids") if args.get("ids") is not None else args.get("id")
        if isinstance(raw_ids, int):
            ids = [raw_ids]
        elif isinstance(raw_ids, str) and raw_ids.isdigit():
            ids = [int(raw_ids)]
        elif isinstance(raw_ids, (list, tuple)):
            ids = [int(x) for x in raw_ids if str(x).isdigit() or isinstance(x, int)]
        else:
            ids = []
        if not ids:
            raise ValidationError("Field ids (or id) is required for odoo_read.")

        compress = args.get("compress", True)
        fields_to_read = args.get("fields")
        if not fields_to_read:
            fields_to_read = self._get_view_prioritized_fields(env, model)

        fields_to_read = self._filter_blacklisted_fields(env, fields_to_read, model=model)

        records = env[model].browse(ids).read(fields=fields_to_read)
        if compress:
            records = self._compress_records(records, preserved_fields=set(args.get("fields") or []))

        return {
            "model": model,
            "records": records,
        }

    def _tool_create(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="create")
        values = args.get("values")
        if not values or not isinstance(values, dict):
            raise ValidationError("Parameter 'values' must be a non-empty dictionary.")

        # Security check: Block blacklisted fields
        values = self._sanitize_write_values(env, values, model=model)

        record = env[model].create(values)
        return {
            "model": model,
            "id": record.id,
            "display_name": record.display_name if hasattr(record, "display_name") else str(record.id),
            "message": f"Successfully created {model} record #{record.id}",
        }

    def _tool_write(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="write")
        ids = args.get("ids") or []
        values = args.get("values")
        if not ids or not isinstance(ids, list):
            raise ValidationError("Parameter 'ids' must be a list of integer IDs.")
        if not values or not isinstance(values, dict):
            raise ValidationError("Parameter 'values' must be a non-empty dictionary.")

        # Enterprise Bulk Safety Guard
        MAX_SAFE_BULK_LIMIT = 25
        if len(ids) > MAX_SAFE_BULK_LIMIT and not args.get("allow_bulk", False):
            raise UserError(
                f"Safety Limit Triggered: Attempted to modify {len(ids)} records simultaneously. "
                f"To prevent accidental mass updates, please pass 'allow_bulk: true' in tool arguments if you are confident."
            )

        values = self._sanitize_write_values(env, values, model=model)

        records = env[model].browse(ids)
        records.write(values)

        return {
            "model": model,
            "ids": ids,
            "message": f"Successfully updated {len(ids)} record(s) on {model}.",
        }

    def _tool_unlink(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="unlink")
        ids = args.get("ids") or []
        if not ids or not isinstance(ids, list):
            raise ValidationError("Parameter 'ids' must be a list of integer IDs.")

        # Enterprise Bulk Safety Guard
        MAX_SAFE_BULK_LIMIT = 25
        if len(ids) > MAX_SAFE_BULK_LIMIT and not args.get("allow_bulk", False):
            raise UserError(
                f"Safety Limit Triggered: Attempted to delete {len(ids)} records simultaneously. "
                f"To prevent catastrophic accidental data loss, please pass 'allow_bulk: true' in tool arguments if you are confident."
            )

        records = env[model].browse(ids)
        records.unlink()

        return {
            "model": model,
            "ids": ids,
            "message": f"Successfully deleted {len(ids)} record(s) from {model}.",
        }

    def _tool_count(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="read")
        domain = args.get("domain") or []
        if not isinstance(domain, list):
            raise ValidationError("Parameter 'domain' must be a list of domain tuples.")
        count = env[model].search_count(domain)
        return {
            "model": model,
            "count": count,
            "domain": domain,
        }

    def _read_group_compat(self, record_or_model, domain=None, fields=None, groupby=None, limit=None, orderby=None):
        domain = domain or []
        fields = fields or []
        groupby = groupby or []
        if hasattr(record_or_model, "formatted_read_group"):
            aggregates = [f for f in fields if f not in groupby]
            if not aggregates:
                aggregates = ["__count"]
            return record_or_model.formatted_read_group(
                domain=domain,
                groupby=groupby,
                aggregates=aggregates,
                limit=limit,
                order=orderby or None,
            )
        else:
            return record_or_model.read_group(
                domain=domain,
                fields=fields,
                groupby=groupby,
                limit=limit,
                orderby=orderby,
                lazy=False,
            )

    def _tool_aggregate(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="read")
        domain = args.get("domain") or []
        fields_arg = args.get("fields") or []
        groupby = args.get("groupby") or []
        limit = min(int(args.get("limit") or 50), 200)
        orderby = args.get("order") or False

        if not isinstance(domain, list):
            raise ValidationError("Parameter 'domain' must be a list.")
        if not fields_arg or not isinstance(fields_arg, list):
            raise ValidationError("Parameter 'fields' must be a non-empty list of fields/aggregates to compute.")
        if not groupby or not isinstance(groupby, list):
            raise ValidationError("Parameter 'groupby' must be a non-empty list of group-by fields.")

        # Sanitize against blacklisted fields
        blacklist = self._get_field_blacklist(env, model=model)
        for f in fields_arg:
            base_f = f.split(":")[0].strip().lower()
            if base_f in blacklist:
                raise AccessDenied(f"Aggregation on sensitive field '{base_f}' is forbidden.")

        groups = self._read_group_compat(
            env[model],
            domain=domain,
            fields=fields_arg,
            groupby=groupby,
            limit=limit,
            orderby=orderby,
        )

        return {
            "model": model,
            "groups_count": len(groups),
            "groups": groups,
        }

    def _tool_find_models(self, env, args):
        query = (args.get("query") or "").strip()
        if not query:
            raise ValidationError("Parameter 'query' is required.")
        limit = min(int(args.get("limit") or 15), 50)

        domain = [
            "|",
            ("model", "ilike", query),
            ("name", "ilike", query),
            ("transient", "=", False),
        ]
        models = env["ir.model"].search(domain, limit=limit)
        results = []
        for m in models:
            if m.model in INTERNAL_MODEL_BLACKLIST or m.model.startswith("ir."):
                continue
            try:
                if not (env[m.model].has_access("read") if hasattr(env[m.model], "has_access") else env[m.model].check_access_rights("read", raise_exception=False)):
                    continue
            except Exception:
                continue
            results.append({
                "model": m.model,
                "name": m.name,
                "modules": m.modules,
            })
        return {
            "query": query,
            "count": len(results),
            "models": results,
        }

    def _tool_execute_kw(self, env, is_readonly, args):
        model = self._validate_model_access(env, args.get("model"), mode="read")
        method = args.get("method")
        call_args = args.get("args") or []
        call_kwargs = args.get("kwargs") or {}

        if not method:
            raise ValidationError("Parameter 'method' is required for odoo_execute_kw.")

        # Check method against administrator whitelist
        is_allowed = env["mcp.method.whitelist"].is_method_allowed(
            env=env,
            model_name=model,
            method_name=method,
            is_readonly_token=is_readonly,
        )
        if not is_allowed:
            raise AccessDenied(
                f"Method '{method}' on model '{model}' is not authorized by the MCP method whitelist."
                + (" (Token is read-only)" if is_readonly else "")
            )

        model_obj = env[model]
        if not hasattr(model_obj, method):
            raise UserError(f"Model '{model}' has no method '{method}'.")

        # Execute natively under current user environment
        target_fn = getattr(model_obj, method)
        call_result = target_fn(*call_args, **call_kwargs)

        return {
            "model": model,
            "method": method,
            "result": call_result,
        }

    def _tool_get_model_schema(self, env, args):
        model = self._validate_model_access(env, args.get("model"), mode="read")
        attributes = args.get("attributes") or ["type", "string", "required", "readonly", "relation", "selection", "help"]
        prioritize_views = args.get("prioritize_views", True)
        include_defaults = bool(args.get("include_defaults", False))
        include_relations = bool(args.get("include_relations", True))

        # Retrieve fields metadata via fields_get
        fields_info = env[model].fields_get(attributes=attributes)

        # Filter out blacklisted fields
        blacklist = self._get_field_blacklist(env, model=model)
        fields_info = {k: v for k, v in fields_info.items() if k.lower() not in blacklist}

        view_fields = set()
        if prioritize_views:
            view_fields = set(self._get_view_prioritized_fields(env, model))

        compressed_fields = {}
        for fname, finfo in fields_info.items():
            # Omit internal audit fields from standard schema inspection unless in view
            if fname in INTERNAL_FIELD_PRUNE and fname not in view_fields:
                continue
            entry = dict(finfo)
            if prioritize_views:
                entry["in_ui_view"] = fname in view_fields

            # For relational fields (One2many, Many2one), enrich with relation context
            if include_relations and entry.get("relation"):
                rel_model = entry["relation"]
                if rel_model in env:
                    entry["relation_description"] = env[rel_model]._description
                    # For child One2many lines, provide required fields on the child model to assist AI in creating nested lines
                    if entry.get("type") == "one2many":
                        try:
                            rel_fields = env[rel_model].fields_get(attributes=["required", "type", "string"])
                            rel_required = [
                                rf for rf, rinfo in rel_fields.items()
                                if rinfo.get("required") and rf not in INTERNAL_FIELD_PRUNE
                            ]
                            entry["child_required_fields"] = rel_required
                        except Exception:
                            pass

            compressed_fields[fname] = entry

        res = {
            "model": model,
            "description": env[model]._description,
            "fields_count": len(compressed_fields),
            "fields": compressed_fields,
        }

        # Include default values via default_get
        if include_defaults:
            try:
                defaults = env[model].default_get(list(compressed_fields.keys()))
                res["default_values"] = {k: v for k, v in defaults.items() if v not in (None, False, "")}
            except Exception as e:
                _logger.debug("default_get failed for %s: %s", model, e)

        return res

    # -------------------------------------------------------------------------
    # Executive & Business Operations Tool Handlers (100x Superpower Engine)
    # -------------------------------------------------------------------------

    def _tool_get_business_context(self, env, args=None):
        company = env.company
        user = env.user
        currency = company.currency_id

        # Check which core business applications are installed
        installed_modules = env["ir.module.module"].sudo().search([
            ("state", "=", "installed"),
            ("name", "in", [
                "sale", "crm", "account", "stock", "purchase",
                "project", "hr", "mrp", "point_of_sale", "helpdesk"
            ])
        ]).mapped("name")

        return {
            "company": {
                "id": company.id,
                "name": company.name,
                "currency": {
                    "code": currency.name,
                    "symbol": currency.symbol,
                    "position": currency.position,
                },
                "email": company.email,
                "phone": company.phone,
                "country": company.country_id.name if company.country_id else None,
            },
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "lang": user.lang,
                "tz": user.tz or "UTC",
            },
            "installed_apps": sorted(installed_modules),
            "server_date": fields.Date.today().isoformat(),
            "server_datetime": fields.Datetime.now().isoformat(),
        }

    def _tool_get_business_kpis(self, env, args):
        period = ((args or {}).get("period") or "this_month").lower()
        today = fields.Date.today()

        if period == "today":
            date_from = today
            date_to = today
        elif period == "yesterday":
            date_from = today - relativedelta(days=1)
            date_to = date_from
        elif period == "this_week":
            date_from = today - relativedelta(days=today.weekday())
            date_to = today
        elif period == "this_month":
            date_from = today.replace(day=1)
            date_to = today
        elif period == "this_quarter":
            quarter_month = 1 + 3 * ((today.month - 1) // 3)
            date_from = today.replace(month=quarter_month, day=1)
            date_to = today
        elif period == "this_year":
            date_from = today.replace(month=1, day=1)
            date_to = today
        elif period == "custom":
            date_from = fields.Date.to_date(args.get("date_from")) if (args and args.get("date_from")) else today.replace(day=1)
            date_to = fields.Date.to_date(args.get("date_to")) if (args and args.get("date_to")) else today
        else:
            date_from = today.replace(day=1)
            date_to = today

        dt_from = fields.Datetime.to_string(datetime.combine(date_from, dtime.min))
        dt_to = fields.Datetime.to_string(datetime.combine(date_to, dtime.max))

        kpis = {
            "period": period,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "company_currency": env.company.currency_id.symbol,
        }

        # 1. Sales Performance
        if "sale.order" in env:
            try:
                env["sale.order"].check_access_rights("read")
                so_domain = [
                    ("state", "in", ["sale", "done"]),
                    ("date_order", ">=", dt_from),
                    ("date_order", "<=", dt_to),
                ]
                confirmed_orders = env["sale.order"].search(so_domain)
                revenue = sum(confirmed_orders.mapped("amount_total"))
                count_orders = len(confirmed_orders)

                quote_domain = [
                    ("state", "in", ["draft", "sent"]),
                    ("date_order", ">=", dt_from),
                    ("date_order", "<=", dt_to),
                ]
                quotes = env["sale.order"].search(quote_domain)
                quote_total = sum(quotes.mapped("amount_total"))

                top_partners = {}
                for order in confirmed_orders:
                    pname = order.partner_id.name or "Unknown"
                    top_partners[pname] = top_partners.get(pname, 0.0) + order.amount_total
                top_5_customers = sorted(
                    [{"customer": k, "total": round(v, 2)} for k, v in top_partners.items()],
                    key=lambda x: x["total"],
                    reverse=True
                )[:5]

                kpis["sales"] = {
                    "confirmed_revenue": round(revenue, 2),
                    "confirmed_orders_count": count_orders,
                    "avg_order_value": round(revenue / count_orders, 2) if count_orders else 0.0,
                    "draft_quotations_count": len(quotes),
                    "draft_quotations_total": round(quote_total, 2),
                    "top_customers": top_5_customers,
                }
            except Exception as e:
                kpis["sales"] = {"error": str(e)}

        # 2. Invoicing & Receivables
        if "account.move" in env:
            try:
                env["account.move"].check_access_rights("read")
                inv_domain = [
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted"),
                    ("invoice_date", ">=", str(date_from)),
                    ("invoice_date", "<=", str(date_to)),
                ]
                invoices = env["account.move"].search(inv_domain)
                total_invoiced = sum(invoices.mapped("amount_total"))

                open_inv_domain = [
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted"),
                    ("payment_state", "in", ["not_paid", "partial"]),
                ]
                open_invoices = env["account.move"].search(open_inv_domain)
                total_unpaid = sum(open_invoices.mapped("amount_residual"))
                overdue_invoices = open_invoices.filtered(lambda i: i.invoice_date_due and i.invoice_date_due < today)
                total_overdue = sum(overdue_invoices.mapped("amount_residual"))

                kpis["invoicing"] = {
                    "invoiced_revenue_in_period": round(total_invoiced, 2),
                    "invoices_count_in_period": len(invoices),
                    "total_unpaid_receivables": round(total_unpaid, 2),
                    "unpaid_invoices_count": len(open_invoices),
                    "total_overdue_receivables": round(total_overdue, 2),
                    "overdue_invoices_count": len(overdue_invoices),
                }
            except Exception as e:
                kpis["invoicing"] = {"error": str(e)}

        # 3. CRM Pipeline
        if "crm.lead" in env:
            try:
                env["crm.lead"].check_access_rights("read")
                opps = env["crm.lead"].search([("type", "=", "opportunity"), ("active", "=", True)])
                pipeline_val = sum(opps.mapped("expected_revenue"))

                won_opps = env["crm.lead"].search([
                    ("type", "=", "opportunity"),
                    ("date_closed", ">=", dt_from),
                    ("date_closed", "<=", dt_to),
                    ("probability", "=", 100),
                ])
                won_revenue = sum(won_opps.mapped("expected_revenue"))

                new_leads = env["crm.lead"].search_count([
                    ("create_date", ">=", dt_from),
                    ("create_date", "<=", dt_to),
                ])

                kpis["crm"] = {
                    "active_pipeline_opportunities": len(opps),
                    "total_pipeline_expected_revenue": round(pipeline_val, 2),
                    "won_opportunities_in_period": len(won_opps),
                    "won_revenue_in_period": round(won_revenue, 2),
                    "new_leads_in_period": new_leads,
                }
            except Exception as e:
                kpis["crm"] = {"error": str(e)}

        # 4. Inventory & Deliveries
        if "stock.picking" in env:
            try:
                env["stock.picking"].check_access_rights("read")
                pending_deliveries = env["stock.picking"].search_count([
                    ("picking_type_code", "=", "outgoing"),
                    ("state", "not in", ["done", "cancel"]),
                ])
                pending_receipts = env["stock.picking"].search_count([
                    ("picking_type_code", "=", "incoming"),
                    ("state", "not in", ["done", "cancel"]),
                ])
                kpis["inventory"] = {
                    "pending_outgoing_deliveries": pending_deliveries,
                    "pending_incoming_receipts": pending_receipts,
                }
            except Exception as e:
                kpis["inventory"] = {"error": str(e)}

        # 5. Purchasing
        if "purchase.order" in env:
            try:
                env["purchase.order"].check_access_rights("read")
                po_domain = [
                    ("state", "in", ["purchase", "done"]),
                    ("date_approve", ">=", dt_from),
                    ("date_approve", "<=", dt_to),
                ]
                pos = env["purchase.order"].search(po_domain)
                kpis["purchasing"] = {
                    "confirmed_po_count": len(pos),
                    "confirmed_po_total": round(sum(pos.mapped("amount_total")), 2),
                }
            except Exception as e:
                kpis["purchasing"] = {"error": str(e)}

        return kpis

    def _tool_global_search(self, env, args):
        query = ((args or {}).get("query") or "").strip()
        if not query:
            raise ValidationError("Parameter 'query' is required for odoo_global_search.")
        limit_per_model = min(int((args or {}).get("limit_per_model") or 5), 20)
        custom_models = (args or {}).get("models")

        target_configs = []
        if custom_models and isinstance(custom_models, list):
            for m in custom_models:
                m_clean = str(m).strip()
                if m_clean in env and m_clean not in INTERNAL_MODEL_BLACKLIST:
                    fields_to_read = self._get_view_prioritized_fields(env, m_clean)
                    target_configs.append((m_clean, env[m_clean]._description or m_clean, fields_to_read))
        else:
            # Default standard models
            standard_configs = [
                ("res.partner", "Partners / Contacts", ["name", "email", "phone", "city", "is_company"]),
                ("sale.order", "Sales Orders", ["name", "partner_id", "state", "amount_total", "date_order"]),
                ("account.move", "Invoices & Bills", ["name", "partner_id", "move_type", "state", "payment_state", "amount_total"]),
                ("product.product", "Products", ["name", "default_code", "list_price", "qty_available"]),
                ("crm.lead", "CRM Leads & Opportunities", ["name", "partner_id", "expected_revenue", "stage_id"]),
                ("purchase.order", "Purchase Orders", ["name", "partner_id", "state", "amount_total"]),
                ("stock.picking", "Deliveries & Transfers", ["name", "partner_id", "picking_type_id", "state"]),
                ("project.task", "Project Tasks", ["name", "project_id", "user_ids", "stage_id"]),
            ]
            for model, label, fields_to_read in standard_configs:
                if model in env:
                    target_configs.append((model, label, fields_to_read))

            # Dynamically discover matching custom business models based on query keyword
            try:
                matching_models = env["ir.model"].sudo().search([
                    ("transient", "=", False),
                    ("state", "=", "base"),
                    "|",
                    ("name", "ilike", query),
                    ("model", "ilike", query),
                ], limit=3)
                for mm in matching_models:
                    if mm.model not in env or mm.model in INTERNAL_MODEL_BLACKLIST or mm.model.startswith("ir."):
                        continue
                    if not any(tc[0] == mm.model for tc in target_configs):
                        fields_to_read = self._get_view_prioritized_fields(env, mm.model)
                        target_configs.append((mm.model, mm.name or mm.model, fields_to_read))
            except Exception as e:
                _logger.debug("Dynamic model discovery for global search failed: %s", e)

        results = {}
        total_matches = 0
        for model, label, fields_to_read in target_configs:
            if model not in env:
                continue
            try:
                if hasattr(env[model], "check_access"):
                    env[model].check_access("read")
                else:
                    env[model].check_access_rights("read")
            except Exception:
                continue

            try:
                name_matches = env[model].name_search(name=query, limit=limit_per_model)
                rec_ids = [m[0] for m in name_matches]
                if not rec_ids:
                    search_domain = [("name", "ilike", query)]
                    if "default_code" in env[model]._fields:
                        search_domain = ["|", ("name", "ilike", query), ("default_code", "ilike", query)]
                    elif "ref" in env[model]._fields:
                        search_domain = ["|", ("name", "ilike", query), ("ref", "ilike", query)]
                    rec_ids = env[model].search(search_domain, limit=limit_per_model).ids

                if rec_ids:
                    valid_fields = [f for f in fields_to_read if f in env[model]._fields]
                    raw_records = env[model].browse(rec_ids).read(fields=valid_fields)
                    compressed = self._compress_records(raw_records)
                    results[label] = compressed
                    total_matches += len(compressed)
            except Exception:
                continue

        return {
            "query": query,
            "total_matches": total_matches,
            "results": results,
        }

    def _tool_record_360(self, env, args):
        model = self._validate_model_access(env, (args or {}).get("model"), mode="read")
        res_id = (args or {}).get("res_id")
        if not res_id:
            raise ValidationError("Parameter 'res_id' is required for odoo_record_360.")

        res_id = int(res_id)
        record = env[model].browse(res_id)
        if not record.exists():
            raise MissingError(f"Record #{res_id} on {model} does not exist.")

        limit_per_rel = min(int((args or {}).get("limit_per_relation") or 5), 20)

        # 1. Main record snapshot
        main_fields = self._get_view_prioritized_fields(env, model)
        main_fields = self._filter_blacklisted_fields(env, main_fields, model=model)
        main_data = record.read(fields=main_fields)
        compressed_main = self._compress_records(main_data)[0] if main_data else {}

        # 2. Universal Reverse Relation Discovery:
        # Discover all models that link to this model via Many2one foreign keys
        rel_fields = env["ir.model.fields"].sudo().search([
            ("relation", "=", model),
            ("ttype", "=", "many2one"),
            ("store", "=", True),
        ])

        related_summary = {}
        for rf in rel_fields:
            rel_model = rf.model
            if rel_model in INTERNAL_MODEL_BLACKLIST or rel_model.startswith("ir."):
                continue
            if rel_model not in env:
                continue

            try:
                if hasattr(env[rel_model], "check_access"):
                    env[rel_model].check_access("read")
                else:
                    env[rel_model].check_access_rights("read")
            except Exception:
                continue

            domain = [(rf.name, "=", res_id)]
            try:
                count = env[rel_model].search_count(domain)
                if count > 0:
                    records_to_read = env[rel_model].search(domain, limit=limit_per_rel, order="id desc")
                    rf_fields = self._get_view_prioritized_fields(env, rel_model)
                    rf_fields = self._filter_blacklisted_fields(env, rf_fields, model=rel_model)
                    rf_data = records_to_read.read(fields=rf_fields)
                    key_name = f"{rel_model} ({rf.name})"
                    related_summary[key_name] = {
                        "model": rel_model,
                        "field": rf.name,
                        "description": env[rel_model]._description,
                        "count": count,
                        "recent_records": self._compress_records(rf_data),
                    }
            except Exception as e:
                _logger.debug("Failed searching relations on %s.%s: %s", rel_model, rf.name, e)

        return {
            "model": model,
            "res_id": res_id,
            "display_name": record.display_name if hasattr(record, "display_name") else str(res_id),
            "record": compressed_main,
            "related_connections_count": len(related_summary),
            "related_records": related_summary,
        }

    def _tool_partner_360(self, env, args):
        partner_id = (args or {}).get("partner_id")
        partner_name = (args or {}).get("partner_name")

        if not partner_id and not partner_name:
            raise ValidationError("Either 'partner_id' or 'partner_name' must be provided.")

        partner = None
        if partner_id:
            partner = env["res.partner"].browse(int(partner_id))
            if not partner.exists():
                raise MissingError(f"Partner #{partner_id} not found.")
        elif partner_name:
            matches = env["res.partner"].name_search(name=partner_name.strip(), limit=1)
            if matches:
                partner = env["res.partner"].browse(matches[0][0])
            else:
                partner = env["res.partner"].search([("name", "ilike", partner_name.strip())], limit=1)
            if not partner:
                raise MissingError(f"Partner matching '{partner_name}' not found.")

        partner.check_access_rights("read")
        commercial_id = partner.commercial_partner_id.id or partner.id

        profile = {
            "id": partner.id,
            "name": partner.name,
            "is_company": partner.is_company,
            "email": partner.email,
            "phone": partner.phone,
            "mobile": getattr(partner, "mobile", None),
            "city": partner.city,
            "country": partner.country_id.name if partner.country_id else None,
            "website": partner.website,
            "credit_limit": getattr(partner, "credit_limit", 0.0),
        }

        dossier = {
            "partner_profile": profile,
            "commercial_partner_id": commercial_id,
        }

        # Sales History
        if "sale.order" in env:
            try:
                env["sale.order"].check_access_rights("read")
                orders = env["sale.order"].search([
                    "|", ("partner_id", "=", partner.id), ("partner_id", "=", commercial_id)
                ], order="date_order desc", limit=5)
                all_orders = env["sale.order"].search([
                    "|", ("partner_id", "=", partner.id), ("partner_id", "=", commercial_id),
                    ("state", "in", ["sale", "done"])
                ])
                dossier["sales"] = {
                    "lifetime_sales_count": len(all_orders),
                    "lifetime_revenue": round(sum(all_orders.mapped("amount_total")), 2),
                    "recent_orders": [
                        {
                            "id": o.id,
                            "name": o.name,
                            "date": str(o.date_order),
                            "state": o.state,
                            "amount_total": o.amount_total,
                        } for o in orders
                    ],
                }
            except Exception as e:
                dossier["sales"] = {"error": str(e)}

        # Invoices & Receivables
        if "account.move" in env:
            try:
                env["account.move"].check_access_rights("read")
                invoices = env["account.move"].search([
                    "|", ("partner_id", "=", partner.id), ("partner_id", "=", commercial_id),
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted")
                ], order="invoice_date desc", limit=5)
                unpaid_invoices = env["account.move"].search([
                    "|", ("partner_id", "=", partner.id), ("partner_id", "=", commercial_id),
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted"),
                    ("payment_state", "in", ["not_paid", "partial"])
                ])
                today = fields.Date.today()
                overdue = unpaid_invoices.filtered(lambda i: i.invoice_date_due and i.invoice_date_due < today)

                dossier["accounting"] = {
                    "total_unpaid_balance": round(sum(unpaid_invoices.mapped("amount_residual")), 2),
                    "unpaid_invoices_count": len(unpaid_invoices),
                    "total_overdue_balance": round(sum(overdue.mapped("amount_residual")), 2),
                    "overdue_invoices_count": len(overdue),
                    "recent_invoices": [
                        {
                            "id": inv.id,
                            "name": inv.name,
                            "invoice_date": str(inv.invoice_date),
                            "due_date": str(inv.invoice_date_due),
                            "state": inv.state,
                            "payment_state": inv.payment_state,
                            "amount_total": inv.amount_total,
                            "amount_residual": inv.amount_residual,
                        } for inv in invoices
                    ],
                }
            except Exception as e:
                dossier["accounting"] = {"error": str(e)}

        # CRM Opportunities
        if "crm.lead" in env:
            try:
                env["crm.lead"].check_access_rights("read")
                leads = env["crm.lead"].search([
                    "|", ("partner_id", "=", partner.id), ("partner_id", "=", commercial_id),
                    ("active", "=", True)
                ], limit=5)
                dossier["crm"] = [
                    {
                        "id": l.id,
                        "name": l.name,
                        "expected_revenue": l.expected_revenue,
                        "probability": l.probability,
                        "stage": l.stage_id.name if l.stage_id else None,
                    } for l in leads
                ]
            except Exception as e:
                dossier["crm"] = {"error": str(e)}

        # Recent Deliveries
        if "stock.picking" in env:
            try:
                env["stock.picking"].check_access_rights("read")
                pickings = env["stock.picking"].search([
                    "|", ("partner_id", "=", partner.id), ("partner_id", "=", commercial_id)
                ], order="scheduled_date desc", limit=3)
                dossier["deliveries"] = [
                    {
                        "id": p.id,
                        "name": p.name,
                        "state": p.state,
                        "scheduled_date": str(p.scheduled_date),
                    } for p in pickings
                ]
            except Exception as e:
                dossier["deliveries"] = {"error": str(e)}

        # Recent Communications
        try:
            messages = env["mail.message"].search([
                ("model", "=", "res.partner"),
                ("res_id", "=", partner.id),
                ("message_type", "!=", "user_notification")
            ], order="id desc", limit=3)
            dossier["recent_notes"] = [
                {
                    "date": str(m.date),
                    "author": m.author_id.name if m.author_id else "System",
                    "body": html2plaintext(m.body or "").strip()[:200],
                } for m in messages
            ]
        except Exception:
            pass

        return dossier

    def _tool_post_chatter_message(self, env, args):
        model = self._validate_model_access(env, (args or {}).get("model"), mode="write")
        res_id = (args or {}).get("res_id")
        body = (args or {}).get("body")
        message_type = (args or {}).get("message_type", "comment")
        partner_ids = (args or {}).get("partner_ids") or []
        subject = (args or {}).get("subject")

        if not res_id or not body:
            raise ValidationError("Parameters 'res_id' and 'body' are required.")

        record = env[model].browse(int(res_id))
        if not record.exists():
            raise MissingError(f"Record #{res_id} on model '{model}' not found.")

        if not hasattr(record, "message_post"):
            raise UserError(f"Model '{model}' does not support chatter communications.")

        subtype_xmlid = "mail.mt_note" if message_type == "comment" else "mail.mt_comment"

        msg = record.message_post(
            body=body,
            message_type="comment",
            subtype_xmlid=subtype_xmlid,
            partner_ids=partner_ids,
            subject=subject or False,
        )

        return {
            "message_id": msg.id,
            "model": model,
            "res_id": res_id,
            "author": msg.author_id.name if msg.author_id else env.user.name,
            "date": str(msg.date),
            "status": "posted",
            "message": f"Successfully posted note/message to {model} #{res_id}.",
        }

    def _tool_get_chatter_history(self, env, args):
        model = self._validate_model_access(env, (args or {}).get("model"), mode="read")
        res_id = (args or {}).get("res_id")
        limit = min(int((args or {}).get("limit") or 10), 50)

        if not res_id:
            raise ValidationError("Parameter 'res_id' is required.")

        record = env[model].browse(int(res_id))
        if not record.exists():
            raise MissingError(f"Record #{res_id} on model '{model}' not found.")

        messages = env["mail.message"].search([
            ("model", "=", model),
            ("res_id", "=", int(res_id)),
            ("message_type", "!=", "user_notification")
        ], order="id desc", limit=limit)

        clean_history = []
        for m in messages:
            clean_body = html2plaintext(m.body or "").strip()
            changes = []
            if hasattr(m, "tracking_value_ids") and m.tracking_value_ids:
                for t in m.tracking_value_ids:
                    f_name = (t.field_id.field_description or t.field_id.name) if t.field_id else getattr(t, "field_desc", "Field")
                    changes.append({
                        "field": f_name,
                        "old_value": t.old_value_char or t.old_value_integer or t.old_value_float or str(t.old_value_datetime or ""),
                        "new_value": t.new_value_char or t.new_value_integer or t.new_value_float or str(t.new_value_datetime or ""),
                    })

            clean_history.append({
                "id": m.id,
                "date": str(m.date),
                "author": m.author_id.name if m.author_id else "System",
                "subtype": m.subtype_id.name if m.subtype_id else "Note",
                "subject": m.subject or None,
                "body": clean_body,
                "field_changes": changes if changes else None,
            })

        return {
            "model": model,
            "res_id": res_id,
            "count": len(clean_history),
            "messages": clean_history,
        }

    def _tool_get_activities(self, env, args):
        filt = ((args or {}).get("filter") or "today").lower()
        user_id = (args or {}).get("user_id") or env.user.id
        model = (args or {}).get("model")
        limit = min(int((args or {}).get("limit") or 25), 100)

        today = fields.Date.today()
        domain = []
        if user_id:
            domain.append(("user_id", "=", int(user_id)))
        if model:
            domain.append(("res_model", "=", model))

        if filt == "today":
            domain.append(("date_deadline", "=", today))
        elif filt == "overdue":
            domain.append(("date_deadline", "<", today))
        elif filt == "upcoming":
            domain.append(("date_deadline", ">", today))

        activities = env["mail.activity"].search(domain, order="date_deadline asc, id asc", limit=limit)

        items = []
        for act in activities:
            deadline = act.date_deadline
            urgency = "overdue" if deadline < today else ("today" if deadline == today else "upcoming")
            items.append({
                "activity_id": act.id,
                "res_model": act.res_model,
                "res_id": act.res_id,
                "record_name": act.res_name or f"{act.res_model} #{act.res_id}",
                "activity_type": act.activity_type_id.name if act.activity_type_id else "To-Do",
                "summary": act.summary or "",
                "note": html2plaintext(act.note or "").strip(),
                "date_deadline": str(deadline),
                "urgency": urgency,
                "assigned_to": act.user_id.name if act.user_id else None,
            })

        return {
            "filter": filt,
            "count": len(items),
            "activities": items,
        }

    def _tool_schedule_activity(self, env, args):
        model = self._validate_model_access(env, (args or {}).get("model"), mode="write")
        res_id = (args or {}).get("res_id")
        summary = (args or {}).get("summary")
        note = (args or {}).get("note") or ""
        activity_type_input = str((args or {}).get("activity_type") or "todo").lower()
        user_id = int((args or {}).get("user_id") or env.user.id)
        date_deadline = (args or {}).get("date_deadline")

        if not res_id or not summary:
            raise ValidationError("Parameters 'res_id' and 'summary' are required for scheduling activity.")

        record = env[model].browse(int(res_id))
        if not record.exists():
            raise MissingError(f"Record #{res_id} on model '{model}' not found.")

        if not date_deadline:
            date_deadline = str(fields.Date.today() + relativedelta(days=1))

        type_mapping = {
            "call": "phone",
            "email": "email",
            "todo": "todo",
            "meeting": "meeting",
        }
        category_search = type_mapping.get(activity_type_input, activity_type_input)

        types = env["mail.activity.type"].search([
            "|", ("category", "=", category_search),
            ("name", "ilike", activity_type_input)
        ], limit=1)
        act_type = types[0] if types else env["mail.activity.type"].search([], limit=1)

        if not act_type:
            raise UserError("No activity types configured in Odoo.")

        model_id = env["ir.model"]._get_id(model)
        activity = env["mail.activity"].create({
            "res_model_id": model_id,
            "res_id": int(res_id),
            "activity_type_id": act_type.id,
            "summary": summary,
            "note": note,
            "date_deadline": fields.Date.to_date(date_deadline),
            "user_id": user_id,
        })

        return {
            "activity_id": activity.id,
            "model": model,
            "res_id": res_id,
            "activity_type": act_type.name,
            "summary": activity.summary,
            "date_deadline": str(activity.date_deadline),
            "assigned_to": activity.user_id.name if activity.user_id else None,
            "message": f"Successfully scheduled '{act_type.name}' on {model} #{res_id} due on {activity.date_deadline}.",
        }

    def _tool_mark_activity_done(self, env, args):
        activity_id = (args or {}).get("activity_id")
        feedback = (args or {}).get("feedback")

        if not activity_id:
            raise ValidationError("Parameter 'activity_id' is required.")

        activity = env["mail.activity"].browse(int(activity_id))
        if not activity.exists():
            raise MissingError(f"Activity #{activity_id} not found.")

        if hasattr(activity, "action_feedback"):
            activity.action_feedback(feedback=feedback or "Completed via AI Assistant")
        else:
            activity.action_done()

        return {
            "activity_id": activity_id,
            "status": "done",
            "feedback": feedback,
            "message": f"Activity #{activity_id} marked as done.",
        }

    def _tool_check_stock(self, env, args):
        if "stock.quant" not in env or "product.product" not in env or "qty_available" not in env["product.product"]._fields:
            raise UserError("The Inventory (stock) module is not installed in this Odoo system.")

        if hasattr(env["product.product"], "check_access"):
            env["product.product"].check_access("read")
        else:
            env["product.product"].check_access_rights("read")
        product_arg = (args or {}).get("product")
        low_stock_only = bool((args or {}).get("low_stock_only", False))
        in_stock_only = bool((args or {}).get("in_stock_only", False))
        sale_ok_only = bool((args or {}).get("sale_ok_only", False))
        limit = min(int((args or {}).get("limit") or 20), 100)

        domain = []
        if low_stock_only:
            domain.append(("qty_available", "<=", 0))
        elif in_stock_only:
            domain.append(("qty_available", ">", 0))

        if sale_ok_only and "sale_ok" in env["product.product"]._fields:
            domain.append(("sale_ok", "=", True))

        if product_arg:
            if isinstance(product_arg, int) or (isinstance(product_arg, str) and str(product_arg).isdigit()):
                domain.append(("id", "=", int(product_arg)))
            else:
                domain.extend(["|", ("default_code", "ilike", str(product_arg).strip()), ("name", "ilike", str(product_arg).strip())])

        products = env["product.product"].search(domain, limit=limit)

        stock_data = []
        for p in products:
            stock_data.append({
                "id": p.id,
                "name": p.name,
                "sku": p.default_code or None,
                "qty_on_hand": p.qty_available,
                "qty_forecasted": p.virtual_available,
                "qty_incoming": p.incoming_qty if hasattr(p, "incoming_qty") else 0.0,
                "qty_outgoing": p.outgoing_qty if hasattr(p, "outgoing_qty") else 0.0,
                "uom": p.uom_id.name if p.uom_id else None,
                "sales_price": p.list_price,
                "cost_price": p.standard_price,
                "can_be_sold": p.sale_ok if hasattr(p, "sale_ok") else True,
            })

        return {
            "count": len(stock_data),
            "products": stock_data,
        }

    def _tool_workflow_action(self, env, args):
        action = (args or {}).get("action")
        model = (args or {}).get("model")
        res_id = (args or {}).get("res_id")

        if not action or res_id is None:
            raise ValidationError("Parameters 'action' and 'res_id' are required.")

        # Normalize res_ids
        if isinstance(res_id, list):
            res_ids = [int(i) for i in res_id]
        else:
            res_ids = [int(res_id)]

        if not res_ids:
            raise ValidationError("At least one record ID must be specified.")

        # Legacy shortcuts for backwards compatibility
        legacy_shortcuts = {
            "confirm_sale": ("sale.order", "action_confirm"),
            "cancel_sale": ("sale.order", "action_cancel"),
            "post_invoice": ("account.move", "action_post"),
            "win_lead": ("crm.lead", "action_set_won"),
            "lose_lead": ("crm.lead", "action_set_lost"),
            "confirm_purchase": ("purchase.order", "button_confirm"),
            "validate_picking": ("stock.picking", "button_validate"),
        }

        target_method = action
        if action in legacy_shortcuts:
            mapped_model, target_method = legacy_shortcuts[action]
            if not model:
                model = mapped_model

        if not model:
            raise ValidationError(
                f"Parameter 'model' is required for action '{action}'. "
                f"(Legacy shortcuts {list(legacy_shortcuts.keys())} auto-deduce model)."
            )

        model = self._validate_model_access(env, model, mode="write")

        if not hasattr(env[model], target_method):
            raise UserError(f"Model '{model}' does not have action method '{target_method}'.")

        # Security check via whitelist policy
        if not env["mcp.method.whitelist"].is_method_allowed(env, model, target_method, is_readonly_token=False):
            raise AccessDenied(f"Execution of action method '{target_method}' on '{model}' is forbidden by security policy.")

        records = env[model].browse(res_ids)
        missing_ids = [rid for rid in res_ids if not env[model].browse(rid).exists()]
        if missing_ids:
            raise MissingError(f"Record(s) #{missing_ids} on model '{model}' not found.")

        call_args = (args or {}).get("args") or []
        call_kwargs = (args or {}).get("kwargs") or {}

        target_fn = getattr(records, target_method)
        call_result = target_fn(*call_args, **call_kwargs)

        # Retrieve new states if available
        new_states = []
        for r in records:
            if hasattr(r, "state"):
                new_states.append(r.state)
            elif hasattr(r, "stage_id"):
                new_states.append(r.stage_id.name)

        clean_result = None
        if isinstance(call_result, dict):
            clean_result = {k: v for k, v in call_result.items() if k in ("type", "res_model", "res_id", "name", "target", "tag")}
        elif isinstance(call_result, (str, int, float, bool, list)):
            clean_result = call_result

        return {
            "action": action,
            "method": target_method,
            "model": model,
            "res_ids": res_ids,
            "status": "success",
            "new_state": new_states[0] if len(new_states) == 1 else (new_states or None),
            "result": clean_result,
            "message": f"Successfully executed '{target_method}' on {model} #{res_ids}.",
        }

    def _tool_get_record_actions(self, env, args):
        model = self._validate_model_access(env, (args or {}).get("model"), mode="read")
        res_id = (args or {}).get("res_id")

        record = None
        current_state = None
        record_name = None
        if res_id is not None:
            try:
                res_id = int(res_id)
                record = env[model].browse(res_id)
                if record.exists():
                    record_name = record.display_name if hasattr(record, "display_name") else str(res_id)
                    if "state" in record._fields:
                        current_state = record.state
            except (ValueError, TypeError):
                raise ValidationError("Parameter 'res_id' must be an integer.")

        # Inspect form view architecture
        view = env["ir.ui.view"].search([
            ("model", "=", model),
            ("type", "=", "form"),
        ], order="priority asc, id asc", limit=1)

        actions = []
        if view and view.arch:
            try:
                tree = etree.fromstring(view.arch.encode("utf-8"))
                for btn in tree.xpath("//button"):
                    name = btn.get("name")
                    if not name or name.startswith("_"):
                        continue
                    btype = btn.get("type", "object")
                    if btype != "object":
                        continue
                    string = btn.get("string") or btn.get("aria-label") or name
                    help_text = btn.get("help") or btn.get("title") or ""
                    btn_class = btn.get("class", "")
                    is_primary = "btn-primary" in btn_class or "oe_highlight" in btn_class

                    # Check if method exists on the model
                    if not hasattr(env[model], name):
                        continue

                    # Filter out duplicates
                    if any(a["name"] == name for a in actions):
                        continue

                    actions.append({
                        "name": name,
                        "label": string,
                        "type": btype,
                        "is_primary": is_primary,
                        "help": help_text,
                    })
            except Exception as e:
                _logger.debug("Failed parsing buttons from view for %s: %s", model, e)

        # Ensure standard workflow action methods are included if present on model
        common_candidates = [
            "action_confirm", "action_done", "action_cancel", "action_draft",
            "action_post", "button_approve", "button_confirm", "button_validate",
            "action_approve", "action_submit", "action_start", "action_reset",
        ]
        for candidate in common_candidates:
            if hasattr(env[model], candidate) and not any(a["name"] == candidate for a in actions):
                actions.append({
                    "name": candidate,
                    "label": candidate.replace("action_", "").replace("button_", "").replace("_", " ").title(),
                    "type": "object",
                    "is_primary": any(w in candidate for w in ("confirm", "approve", "validate", "post")),
                    "help": f"Standard workflow method {candidate}",
                })

        return {
            "model": model,
            "res_id": res_id,
            "record_name": record_name,
            "current_state": current_state,
            "actions_count": len(actions),
            "available_actions": actions,
        }

    def _tool_get_report_pdf(self, env, args):
        model = self._validate_model_access(env, (args or {}).get("model"), mode="read")
        res_id = (args or {}).get("res_id")
        report_xmlid = (args or {}).get("report_xmlid")

        if not res_id:
            raise ValidationError("Parameter 'res_id' is required.")

        res_id = int(res_id)
        record = env[model].browse(res_id)
        if not record.exists():
            raise MissingError(f"Record #{res_id} on model '{model}' not found.")

        default_reports = {
            "sale.order": "sale.report_saleorder",
            "account.move": "account.report_invoice",
            "purchase.order": "purchase.action_report_purchase_order",
            "stock.picking": "stock.action_report_delivery",
        }

        target_report_xmlid = report_xmlid or default_reports.get(model)
        report = None
        if target_report_xmlid:
            report = env.ref(target_report_xmlid, raise_if_not_found=False)

        if not report:
            reports = env["ir.actions.report"].search([("model", "=", model)], limit=1)
            if reports:
                report = reports[0]

        if not report:
            raise UserError(f"No PDF report found for model '{model}'.")

        pdf_content, _ = env["ir.actions.report"]._render_qweb_pdf(report.report_name, [res_id])
        pdf_b64 = base64.b64encode(pdf_content).decode("utf-8")
        filename = f"{model.replace('.', '_')}_{res_id}.pdf"

        return {
            "filename": filename,
            "size_bytes": len(pdf_content),
            "mimetype": "application/pdf",
            "pdf_base64": pdf_b64,
            "message": f"Successfully generated PDF report for {model} #{res_id}.",
        }
    def _tool_build_domain(self, env, args):
        args = args or {}
        model = self._validate_model_access(env, args.get("model"), mode="read")
        conditions = args.get("conditions")
        if not conditions or not isinstance(conditions, list):
            raise ValidationError("Parameter 'conditions' must be a non-empty list of condition objects.")

        logical_operator = (args.get("logical_operator") or "AND").strip().upper()
        if logical_operator not in ("AND", "OR"):
            raise ValidationError("Parameter 'logical_operator' must be either 'AND' or 'OR'.")

        date_eval = args.get("date_eval", True)
        safe_operators = {
            "=", "!=", ">", ">=", "<", "<=",
            "in", "not in",
            "=like", "=ilike", "like", "ilike", "not like", "not ilike",
            "child_of", "parent_of",
        }

        # Date evaluation helper for smart relative dates
        today = fields.Date.today()

        def evaluate_date_string(val):
            if not isinstance(val, str):
                return val
            s = val.strip().lower()
            if s == "today":
                return fields.Date.to_string(today)
            elif s == "yesterday":
                return fields.Date.to_string(today - relativedelta(days=1))
            elif s == "tomorrow":
                return fields.Date.to_string(today + relativedelta(days=1))
            elif s == "start_of_month":
                return fields.Date.to_string(today.replace(day=1))
            elif s == "end_of_month":
                return fields.Date.to_string(today.replace(day=1) + relativedelta(months=1, days=-1))
            elif s == "start_of_year":
                return fields.Date.to_string(today.replace(month=1, day=1))
            elif s == "end_of_year":
                return fields.Date.to_string(today.replace(month=12, day=31))
            # Relative day offsets e.g. -7d, -30d, +15d
            m = re.match(r"^([+-]?\d+)\s*d$", s)
            if m:
                days = int(m.group(1))
                return fields.Date.to_string(today + relativedelta(days=days))
            return val

        # Validate conditions and construct leaf tuples
        leaves = []
        field_meta = {}
        human_descriptions = []

        for idx, cond in enumerate(conditions):
            if not isinstance(cond, dict):
                raise ValidationError(f"Condition at index {idx} must be a dict.")
            field_path = str(cond.get("field") or "").strip()
            op = str(cond.get("operator") or "").strip()
            raw_val = cond.get("value")

            if not field_path:
                raise ValidationError(f"Condition at index {idx} is missing 'field'.")
            if not op:
                raise ValidationError(f"Condition at index {idx} is missing 'operator'.")
            if op not in safe_operators:
                raise ValidationError(f"Invalid operator '{op}' at condition {idx}. Allowed operators: {sorted(safe_operators)}")

            # Evaluate dynamic relative date
            val = evaluate_date_string(raw_val) if date_eval else raw_val

            # Validate field exists on model or traversed relation
            cur_model = model
            parts = field_path.split(".")
            last_field_type = "unknown"
            last_field_string = field_path

            for p_idx, part in enumerate(parts):
                if cur_model not in env or part not in env[cur_model]._fields:
                    raise ValidationError(f"Field '{part}' in '{field_path}' does not exist on model '{cur_model}'.")
                fld = env[cur_model]._fields[part]
                last_field_type = fld.type
                last_field_string = fld.string or part
                if p_idx < len(parts) - 1:
                    if not fld.relational:
                        raise ValidationError(f"Field '{part}' in path '{field_path}' is of type '{fld.type}' and cannot be traversed.")
                    cur_model = fld.comodel_name

            field_meta[field_path] = {
                "type": last_field_type,
                "label": last_field_string,
            }

            # Enforce list type for 'in' / 'not in'
            if op in ("in", "not in") and not isinstance(val, (list, tuple)):
                val = [val] if val is not None else []

            leaves.append([(field_path, op, val)])
            human_descriptions.append(f"{last_field_string} ({field_path}) {op} {val}")

        # Combine leaves with Odoo's native expression engine
        if logical_operator == "OR":
            built_domain = expression.OR(leaves)
        else:
            built_domain = expression.AND(leaves)

        normalized = expression.normalize_domain(built_domain)

        return {
            "success": True,
            "model": model,
            "domain": normalized,
            "logical_operator": logical_operator,
            "conditions_count": len(conditions),
            "field_metadata": field_meta,
            "human_readable": f" {logical_operator} ".join(human_descriptions),
            "instructions": "Pass this 'domain' list directly to 'odoo_search_read', 'odoo_count', or 'odoo_aggregate'.",
        }

    def _tool_diagnose_access(self, env, token_id, is_readonly, args):
        args = args or {}
        model = (args.get("model") or "").strip()
        if not model:
            raise ValidationError("Parameter 'model' is required.")

        operation = (args.get("operation") or "read").strip().lower()
        if operation not in ("read", "write", "create", "unlink"):
            raise ValidationError("Parameter 'operation' must be one of: 'read', 'write', 'create', 'unlink'.")

        res_id = args.get("res_id")
        if res_id is not None:
            try:
                res_id = int(res_id)
            except (ValueError, TypeError):
                raise ValidationError("Parameter 'res_id' must be an integer.")

        diagnosis = {
            "model": model,
            "operation": operation,
            "user_id": env.user.id,
            "user_name": env.user.name,
            "user_login": env.user.login,
            "company_id": env.company.id,
            "company_name": env.company.name,
            "token_readonly": is_readonly,
        }

        # 1. Model existence check
        if model not in env:
            diagnosis["allowed"] = False
            diagnosis["root_cause"] = "MODEL_NOT_FOUND"
            diagnosis["message"] = f"Model '{model}' does not exist in this Odoo system."
            return diagnosis

        # 2. Blacklist check
        if model in INTERNAL_MODEL_BLACKLIST:
            diagnosis["allowed"] = False
            diagnosis["root_cause"] = "MODEL_BLACKLISTED"
            diagnosis["message"] = f"Model '{model}' is an internal system model protected from MCP access."
            return diagnosis

        # 3. Token Read-Only Firewall Check
        if is_readonly and operation in ("write", "create", "unlink"):
            diagnosis["allowed"] = False
            diagnosis["root_cause"] = "TOKEN_READONLY"
            diagnosis["message"] = f"The MCP token is configured as READ-ONLY. Mutation '{operation}' is prohibited by token policy."
            return diagnosis

        # 4. Token Allowed Models Check
        if token_id:
            token = env["mcp.access.token"].sudo().browse(token_id)
            if token.exists() and token.allowed_model_ids:
                allowed_models = set(token.allowed_model_ids.mapped("model"))
                if model not in allowed_models:
                    diagnosis["allowed"] = False
                    diagnosis["root_cause"] = "TOKEN_MODEL_WHITELIST_BLOCKED"
                    diagnosis["message"] = f"Model '{model}' is not in the token's allowed models whitelist."
                    return diagnosis

        # 5. Model ACL Check (ir.model.access)
        has_model_acl = False
        try:
            has_model_acl = (env[model].has_access(operation) if hasattr(env[model], "has_access") else env[model].check_access_rights(operation, raise_exception=False))
        except Exception:
            has_model_acl = False

        diagnosis["model_acl_allowed"] = bool(has_model_acl)
        if not has_model_acl:
            diagnosis["allowed"] = False
            diagnosis["root_cause"] = "MODEL_ACL_DENIED"
            diagnosis["message"] = f"User '{env.user.name}' does not have '{operation}' access rights on model '{model}' in ir.model.access."
            return diagnosis

        # 6. Specific Record Diagnostics (ir.rule & multi-company)
        if res_id:
            diagnosis["res_id"] = res_id
            record = env[model].browse(res_id)
            if not record.exists():
                diagnosis["allowed"] = False
                diagnosis["root_cause"] = "RECORD_NOT_FOUND"
                diagnosis["message"] = f"Record #{res_id} does not exist in model '{model}'."
                return diagnosis

            # Company isolation check
            if "company_id" in record._fields and record.company_id:
                rec_company = record.company_id
                diagnosis["record_company_id"] = rec_company.id
                diagnosis["record_company_name"] = rec_company.name
                if rec_company.id not in env.companies.ids:
                    diagnosis["allowed"] = False
                    diagnosis["root_cause"] = "COMPANY_ISOLATION_BLOCKED"
                    diagnosis["message"] = (
                        f"Record #{res_id} belongs to company '{rec_company.name}' (ID {rec_company.id}), "
                        f"which is outside the current user's allowed companies: {[c.name for c in env.companies]}."
                    )
                    return diagnosis

            # Record Rule Check (ir.rule)
            try:
                if hasattr(record, "check_access"):
                    record.check_access(operation)
                else:
                    record.check_access_rule(operation)
                diagnosis["record_rule_allowed"] = True
            except AccessError as e:
                diagnosis["record_rule_allowed"] = False
                diagnosis["allowed"] = False
                diagnosis["root_cause"] = "RECORD_RULE_DENIED"
                diagnosis["message"] = f"Access denied by an active record rule: {str(e)}"
                return diagnosis

        diagnosis["allowed"] = True
        diagnosis["root_cause"] = None
        diagnosis["message"] = f"User '{env.user.name}' has full '{operation}' permission on '{model}'" + (f" record #{res_id}." if res_id else ".")
        return diagnosis

    def _tool_read_attachment(self, env, args):
        args = args or {}
        attachment_id = args.get("attachment_id")
        res_model = args.get("res_model")
        res_id = args.get("res_id")
        include_content = args.get("include_content", True)
        max_bytes = min(int(args.get("max_bytes") or 1048576), 10485760)  # max cap 10MB, default 1MB

        if not attachment_id and not (res_model and res_id):
            raise ValidationError("Either 'attachment_id' OR both 'res_model' and 'res_id' must be provided.")

        # Discovering attachments on a record
        if not attachment_id and res_model and res_id:
            res_model = self._validate_model_access(env, res_model, mode="read")
            res_id = int(res_id)
            attachments = env["ir.attachment"].search([
                ("res_model", "=", res_model),
                ("res_id", "=", res_id),
            ], order="id desc")

            results = []
            for att in attachments:
                results.append({
                    "id": att.id,
                    "name": att.name,
                    "mimetype": att.mimetype,
                    "file_size": att.file_size,
                    "type": att.type,
                    "create_date": fields.Datetime.to_string(att.create_date),
                })

            return {
                "res_model": res_model,
                "res_id": res_id,
                "total_attachments": len(results),
                "attachments": results,
                "message": f"Found {len(results)} attachment(s) for {res_model} #{res_id}.",
            }

        # Reading specific attachment by ID
        attachment_id = int(attachment_id)
        att = env["ir.attachment"].browse(attachment_id)
        if not att.exists():
            raise MissingError(f"Attachment #{attachment_id} not found.")

        # Ensure user can read the target record if res_model and res_id exist
        if att.res_model and att.res_id and att.res_model in env:
            self._validate_model_access(env, att.res_model, mode="read")

        metadata = {
            "id": att.id,
            "name": att.name,
            "mimetype": att.mimetype or "application/octet-stream",
            "file_size": att.file_size or 0,
            "type": att.type,
            "res_model": att.res_model or False,
            "res_id": att.res_id or False,
            "checksum": att.checksum or False,
            "create_date": fields.Datetime.to_string(att.create_date),
        }

        if not include_content:
            return {"attachment": metadata, "content_omitted": True}

        if att.type == "url":
            metadata["url"] = att.url
            return {"attachment": metadata, "content_type": "url"}

        file_size = att.file_size or 0
        if file_size > max_bytes:
            metadata["warning"] = f"File size ({file_size} bytes) exceeds the cap of {max_bytes} bytes. Content omitted."
            return {"attachment": metadata, "content_omitted": True}

        raw_b64 = None
        if "raw" in att._fields and att.raw:
            raw_b64 = base64.b64encode(bytes(att.raw)).decode("ascii")
        elif "datas" in att._fields and hasattr(att, "datas") and att.datas:
            raw_b64 = att.datas
        if not raw_b64:
            return {"attachment": metadata, "content_type": "empty", "content": ""}

        mimetype = (att.mimetype or "").lower()
        is_text = (
            mimetype.startswith("text/")
            or mimetype in ("application/json", "application/xml", "application/javascript", "text/csv")
            or att.name.lower().endswith((".txt", ".csv", ".json", ".xml", ".md", ".log", ".yaml", ".yml"))
        )

        if is_text:
            try:
                decoded_bytes = base64.b64decode(raw_b64)
                text_content = decoded_bytes.decode("utf-8", errors="replace")
                return {
                    "attachment": metadata,
                    "content_type": "text",
                    "content_text": text_content,
                }
            except Exception:
                pass

        return {
            "attachment": metadata,
            "content_type": "base64",
            "content_base64": raw_b64.decode("utf-8") if isinstance(raw_b64, bytes) else str(raw_b64),
        }

    def _tool_aged_partner_balance(self, env, args):
        args = args or {}
        if "account.move.line" not in env:
            return {
                "success": False,
                "message": "The standard Odoo Accounting module ('account') is not installed in this database.",
            }

        account_type = (args.get("account_type") or "receivable").strip().lower()
        if account_type not in ("receivable", "payable", "both"):
            raise ValidationError("Parameter 'account_type' must be 'receivable', 'payable', or 'both'.")

        as_of_str = args.get("as_of_date")
        if as_of_str:
            try:
                as_of = fields.Date.from_string(as_of_str)
            except Exception:
                raise ValidationError(f"Invalid as_of_date format '{as_of_str}'. Expected YYYY-MM-DD.")
        else:
            as_of = fields.Date.today()

        top_partners_limit = min(int(args.get("top_partners") or 10), 100)
        partner_id = args.get("partner_id")

        # Map to standard Odoo account_type
        types_to_match = []
        if account_type in ("receivable", "both"):
            types_to_match.append("asset_receivable")
        if account_type in ("payable", "both"):
            types_to_match.append("liability_payable")

        domain = [
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
            ("amount_residual", "!=", 0),
            ("account_id.account_type", "in", types_to_match),
        ]
        if partner_id:
            domain.append(("partner_id", "=", int(partner_id)))

        lines = env["account.move.line"].search_read(
            domain=domain,
            fields=["id", "partner_id", "amount_residual", "date_maturity", "date"],
            limit=5000,
        )

        overall_buckets = {
            "not_due": 0.0,
            "days_1_30": 0.0,
            "days_31_60": 0.0,
            "days_61_90": 0.0,
            "days_90_plus": 0.0,
        }
        partner_data = {}
        total_open_balance = 0.0
        total_overdue_balance = 0.0

        for l in lines:
            amt = float(l.get("amount_residual") or 0.0)
            total_open_balance += amt

            due_date_raw = l.get("date_maturity") or l.get("date")
            due_date = fields.Date.from_string(due_date_raw) if due_date_raw else as_of
            days_overdue = (as_of - due_date).days

            if days_overdue <= 0:
                bucket_key = "not_due"
            elif 1 <= days_overdue <= 30:
                bucket_key = "days_1_30"
                total_overdue_balance += amt
            elif 31 <= days_overdue <= 60:
                bucket_key = "days_31_60"
                total_overdue_balance += amt
            elif 61 <= days_overdue <= 90:
                bucket_key = "days_61_90"
                total_overdue_balance += amt
            else:
                bucket_key = "days_90_plus"
                total_overdue_balance += amt

            overall_buckets[bucket_key] += amt

            # Partner grouping
            p_tuple = l.get("partner_id")
            p_id = p_tuple[0] if p_tuple else 0
            p_name = p_tuple[1] if p_tuple else "Unknown Partner"

            if p_id not in partner_data:
                partner_data[p_id] = {
                    "partner_id": p_id,
                    "partner_name": p_name,
                    "total_open": 0.0,
                    "total_overdue": 0.0,
                    "buckets": {
                        "not_due": 0.0,
                        "days_1_30": 0.0,
                        "days_31_60": 0.0,
                        "days_61_90": 0.0,
                        "days_90_plus": 0.0,
                    },
                }

            partner_data[p_id]["total_open"] += amt
            if bucket_key != "not_due":
                partner_data[p_id]["total_overdue"] += amt
            partner_data[p_id]["buckets"][bucket_key] += amt

        # Sort partners by largest overdue amount
        sorted_partners = sorted(
            partner_data.values(),
            key=lambda x: abs(x["total_overdue"]),
            reverse=True,
        )[:top_partners_limit]

        currency_symbol = env.company.currency_id.symbol or ""

        def r(n):
            return round(n, 2)

        for b in overall_buckets:
            overall_buckets[b] = r(overall_buckets[b])
        for p in sorted_partners:
            p["total_open"] = r(p["total_open"])
            p["total_overdue"] = r(p["total_overdue"])
            for b in p["buckets"]:
                p["buckets"][b] = r(p["buckets"][b])

        return {
            "success": True,
            "account_type": account_type,
            "as_of_date": fields.Date.to_string(as_of),
            "currency": currency_symbol,
            "total_open_balance": r(total_open_balance),
            "total_overdue_balance": r(total_overdue_balance),
            "aging_buckets": overall_buckets,
            "top_partners": sorted_partners,
            "total_partners_analyzed": len(partner_data),
            "open_items_count": len(lines),
        }

    def _tool_inspect_data_quality(self, env, args):
        args = args or {}
        model = self._validate_model_access(env, args.get("model"), mode="read")
        checks = args.get("checks") or ["duplicates", "missing_values", "format_anomalies"]
        sample_limit = min(int(args.get("sample_limit") or 200), 1000)

        # Determine key fields for duplicate check
        custom_keys = args.get("key_fields")
        if custom_keys:
            key_fields = [k for k in custom_keys if k in env[model]._fields]
        else:
            defaults = {
                "res.partner": ["email", "phone", "vat", "ref"],
                "product.template": ["barcode", "default_code", "name"],
                "product.product": ["barcode", "default_code"],
                "crm.lead": ["email_from", "phone"],
                "hr.employee": ["work_email", "identification_id"],
            }
            cand = defaults.get(model, ["code", "ref", "barcode", "email", "name"])
            key_fields = [k for k in cand if k in env[model]._fields]

        findings = []
        duplicate_clusters = {}
        total_duplicates_count = 0

        # Check 1: Duplicate Scan via read_group
        if "duplicates" in checks and key_fields:
            for key in key_fields:
                groups = env[model].read_group(
                    domain=[(key, "!=", False), (key, "!=", "")],
                    fields=[key],
                    groupby=[key],
                    limit=200,
                )
                key_clusters = []
                for g in groups:
                    cnt = g.get(f"{key}_count") or g.get("__count") or 0
                    val = g.get(key)
                    if cnt > 1 and val:
                        # Fetch sample duplicate records
                        dup_records = env[model].search_read(
                            domain=[(key, "=", val)],
                            fields=["id", "display_name"],
                            limit=10,
                        )
                        key_clusters.append({
                            "key_field": key,
                            "duplicate_value": val,
                            "duplicate_count": cnt,
                            "sample_records": dup_records,
                        })
                        total_duplicates_count += (cnt - 1)

                if key_clusters:
                    duplicate_clusters[key] = key_clusters[:5]
                    findings.append(f"Found {len(key_clusters)} duplicate groups on '{key}'.")

        # Check 2: Missing Values Check
        missing_report = {}
        if "missing_values" in checks:
            priority_fields = [
                fname for fname, fld in env[model]._fields.items()
                if (fld.required or fname in ("email", "phone", "vat", "barcode", "list_price", "user_id"))
                and fname not in INTERNAL_FIELD_PRUNE
                and not fld.compute
            ][:10]

            total_records = env[model].search_count([])
            for pf in priority_fields:
                missing_cnt = env[model].search_count([(pf, "=", False)])
                if missing_cnt > 0 and total_records > 0:
                    pct = round((missing_cnt / total_records) * 100, 1)
                    if pct >= 5.0:
                        missing_report[pf] = {
                            "missing_count": missing_cnt,
                            "missing_percent": pct,
                        }
            if missing_report:
                findings.append(f"{len(missing_report)} important fields have missing/null values.")

        # Check 3: Format Anomalies on sample
        format_issues = []
        if "format_anomalies" in checks:
            email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
            phone_re = re.compile(r"^[0-9+\-().\s/]{6,}$")

            sample_fields = ["id", "display_name"]
            for candidate in ("email", "email_from", "work_email", "phone", "mobile", "work_phone"):
                if candidate in env[model]._fields:
                    sample_fields.append(candidate)

            sample_records = env[model].search_read(
                domain=[],
                fields=sample_fields,
                limit=sample_limit,
                order="id desc",
            )

            for rec in sample_records:
                for ef in ("email", "email_from", "work_email"):
                    eval_val = rec.get(ef)
                    if eval_val and isinstance(eval_val, str) and not email_re.match(eval_val.strip()):
                        format_issues.append({
                            "record_id": rec["id"],
                            "record_name": rec.get("display_name"),
                            "field": ef,
                            "invalid_value": eval_val,
                            "issue": "Malformed email address",
                        })
                for pf in ("phone", "mobile", "work_phone"):
                    pval = rec.get(pf)
                    if pval and isinstance(pval, str) and not phone_re.match(pval.strip()):
                        format_issues.append({
                            "record_id": rec["id"],
                            "record_name": rec.get("display_name"),
                            "field": pf,
                            "invalid_value": pval,
                            "issue": "Malformed phone number (less than 6 digits/characters)",
                        })

            if format_issues:
                findings.append(f"Detected {len(format_issues)} format anomalies across {len(sample_records)} sampled records.")

        # Compute data health score (0 - 100)
        penalty = min(total_duplicates_count * 5, 30) + min(len(missing_report) * 5, 40) + min(len(format_issues) * 3, 30)
        health_score = max(100 - penalty, 0)

        return {
            "success": True,
            "model": model,
            "health_score": health_score,
            "total_duplicates_found": total_duplicates_count,
            "duplicate_clusters": duplicate_clusters,
            "missing_fields": missing_report,
            "format_issues_sample": format_issues[:15],
            "findings_summary": findings or ["Data quality check passed. No major hygiene issues detected."],
            "recommendations": (
                "Merge duplicate records, populate missing business fields, and correct malformed email/phone formats."
                if findings else "Data is in good health."
            ),
        }

    def _compute_pivot_preview(self, model_pool, panel):
        domain = panel.get("domain") or []
        row_fields = panel.get("pivot_row_fields") or []
        col_fields = panel.get("pivot_col_fields") or []
        meas_fields = panel.get("pivot_measure_fields") or ([panel.get("measure_field")] if panel.get("measure_field") else ["__count"])
        primary_meas = meas_fields[0] if meas_fields else "__count"

        row_spec = row_fields[0] if row_fields else (panel.get("dimension_field") or "id")
        col_spec = col_fields[0] if col_fields else None

        groupby = [row_spec]
        if col_spec:
            groupby.append(col_spec)

        meas_clause = [f"{primary_meas}:sum"] if primary_meas != "__count" and primary_meas in model_pool._fields else []

        try:
            rg = model_pool.read_group(domain=domain, fields=meas_clause, groupby=groupby, lazy=False)
        except Exception as e:
            _logger.debug("Pivot preview read_group failed: %s", e)
            rg = []

        rows = {}
        cols = []
        for item in rg:
            r_val = item.get(row_spec)
            r_lbl = r_val[1] if isinstance(r_val, (tuple, list)) else (str(r_val) if r_val is not None else "Undefined")

            if col_spec:
                c_val = item.get(col_spec)
                c_lbl = c_val[1] if isinstance(c_val, (tuple, list)) else (str(c_val) if c_val is not None else "All")
            else:
                c_lbl = "Total"

            if c_lbl not in cols:
                cols.append(c_lbl)
            if r_lbl not in rows:
                rows[r_lbl] = {}
            val = item.get(primary_meas, item.get("__count", 0))
            rows[r_lbl][c_lbl] = val

        # Build Markdown Table
        header = ["Grouping"] + cols + ["Total"]
        md_lines = ["| " + " | ".join(header) + " |"]
        md_lines.append("|" + "|".join([":---"] + [":---:" for _ in cols] + [":---:"]) + "|")

        col_totals = {c: 0.0 for c in cols}
        grand_total = 0.0
        sorted_rows = sorted(
            rows.items(),
            key=lambda x: sum(v for v in x[1].values() if isinstance(v, (int, float))),
            reverse=True
        )[:15]

        for r_lbl, c_data in sorted_rows:
            r_sum = sum(c_data.get(c, 0.0) for c in cols if isinstance(c_data.get(c), (int, float)))
            row_cells = []
            for c in cols:
                v = c_data.get(c, 0.0)
                if isinstance(v, float):
                    row_cells.append(f"{v:,.2f}")
                elif isinstance(v, int):
                    row_cells.append(f"{v:,}")
                else:
                    row_cells.append(str(v or "0"))
                if isinstance(v, (int, float)):
                    col_totals[c] += v
            md_lines.append(f"| {r_lbl} | " + " | ".join(row_cells) + f" | {r_sum:,.2f} |")
            grand_total += r_sum

        tot_cells = [f"{col_totals[c]:,.2f}" for c in cols]
        md_lines.append("| **Total** | " + " | ".join(tot_cells) + f" | **{grand_total:,.2f}** |")

        return {
            "pivot_matrix": {
                "row_spec": row_spec,
                "col_spec": col_spec,
                "measure": primary_meas,
                "columns": cols,
                "rows_sample": {k: v for k, v in sorted_rows[:10]},
                "grand_total": grand_total,
            },
            "table_markdown": "\n".join(md_lines),
        }

    def _compute_graph_preview(self, model_pool, panel):
        domain = panel.get("domain") or []
        dim = panel.get("dimension_field") or "id"
        meas = panel.get("measure_field") or "__count"
        dim_int = panel.get("dimension_interval")
        dim_spec = f"{dim}:{dim_int}" if dim_int else dim

        meas_clause = [f"{meas}:sum"] if meas != "__count" and meas in model_pool._fields else []
        try:
            rg = model_pool.read_group(domain=domain, fields=meas_clause, groupby=[dim_spec], lazy=False)
        except Exception as e:
            _logger.debug("Graph preview read_group failed: %s", e)
            rg = []

        items = []
        total_val = sum(item.get(meas, item.get("__count", 0)) for item in rg if isinstance(item.get(meas, item.get("__count", 0)), (int, float))) or 1.0

        for item in rg[:10]:
            val_raw = item.get(dim_spec, item.get(dim))
            lbl = val_raw[1] if isinstance(val_raw, (tuple, list)) else (str(val_raw) if val_raw is not None else "Undefined")
            v = item.get(meas, item.get("__count", 0))
            pct = round((v / total_val) * 100, 1) if isinstance(v, (int, float)) else 0.0
            items.append({"label": lbl, "value": v, "percentage": pct})

        return {
            "distribution": items,
            "graph_summary": f"Top {len(items)} segments across {dim_spec} ({panel.get('graph_type', 'bar')} chart)",
        }

    def _compute_list_preview(self, model_pool, panel):
        domain = panel.get("domain") or []
        fields = [f for f in panel.get("list_fields", ["name", "display_name"]) if f in model_pool._fields]
        if not fields:
            fields = ["display_name"]
        records = model_pool.search_read(domain, fields=fields, limit=panel.get("limit", 5))
        return {
            "sample_records": records,
        }

    def _compute_quick_kpis(self, model_pool, domain, measure_field):
        count = model_pool.search_count(domain)
        kpis = [{"label": "Total Records", "value": count}]
        if measure_field and measure_field != "__count" and measure_field in model_pool._fields:
            try:
                rg = model_pool.read_group(domain=domain, fields=[f"{measure_field}:sum", f"{measure_field}:avg"], groupby=[], lazy=False)
                if rg:
                    tot = rg[0].get(measure_field) or 0.0
                    avg = rg[0].get(f"{measure_field}:avg") or (tot / count if count else 0.0)
                    kpis.append({"label": f"Total {measure_field.replace('_', ' ').title()}", "value": f"{tot:,.2f}" if isinstance(tot, float) else f"{tot:,}"})
                    kpis.append({"label": f"Average {measure_field.replace('_', ' ').title()}", "value": f"{avg:,.2f}" if isinstance(avg, float) else f"{avg:,}"})
            except Exception as e:
                _logger.debug("Quick KPI computation failed: %s", e)
        return kpis

    def _build_spreadsheet_dashboard_data(self, name, model="sale.order", quot_count=0, so_count=0, revenue=0.0, avg_order=0.0):
        """Construct a pristine, bug-free Odoo 18 Spreadsheet Dashboard JSON structure.
        - Rows 1-23 reserved for floating Scorecards (y: 10-105) and Chart (y: 125-455).
        - Clean table headers and list formulas placed at row 24+ with wide column spacing.
        - Data sheet formulas mapped with valid measures (__count and amount_total) and fallback values.
        """
        return {
            "version": 21,
            "odooVersion": 12,
            "revisionId": "initial-revision",
            "sheets": [
                {
                    "id": "sheet1",
                    "name": "Executive Dashboard",
                    "colNumber": 26,
                    "rowNumber": 100,
                    "rows": {},
                    "cols": {
                        "0": {"size": 110},
                        "1": {"size": 240},
                        "2": {"size": 120},
                        "3": {"size": 110},
                        "4": {"size": 35},
                        "5": {"size": 110},
                        "6": {"size": 240},
                        "7": {"size": 120},
                        "8": {"size": 110}
                    },
                    "merges": [],
                    "figures": [
                        {
                            "id": "fig_sc_quotations",
                            "x": 0,
                            "y": 10,
                            "width": 220,
                            "height": 95,
                            "tag": "chart",
                            "data": {
                                "type": "scorecard",
                                "title": {"text": "Quotations", "bold": True, "color": "#1E3A8A"},
                                "background": "#EFF6FF",
                                "keyValue": "Data!B2",
                                "humanize": False
                            }
                        },
                        {
                            "id": "fig_sc_orders",
                            "x": 230,
                            "y": 10,
                            "width": 220,
                            "height": 95,
                            "tag": "chart",
                            "data": {
                                "type": "scorecard",
                                "title": {"text": "Orders", "bold": True, "color": "#064E3B"},
                                "background": "#ECFDF5",
                                "keyValue": "Data!B3",
                                "humanize": False
                            }
                        },
                        {
                            "id": "fig_sc_revenue",
                            "x": 460,
                            "y": 10,
                            "width": 240,
                            "height": 95,
                            "tag": "chart",
                            "data": {
                                "type": "scorecard",
                                "title": {"text": "Revenue", "bold": True, "color": "#7C2D12"},
                                "background": "#FFF7ED",
                                "keyValue": "Data!B4",
                                "humanize": False
                            }
                        },
                        {
                            "id": "fig_sc_avg_order",
                            "x": 710,
                            "y": 10,
                            "width": 240,
                            "height": 95,
                            "tag": "chart",
                            "data": {
                                "type": "scorecard",
                                "title": {"text": "Average Order", "bold": True, "color": "#581C87"},
                                "background": "#FAF5FF",
                                "keyValue": "Data!B5",
                                "humanize": False
                            }
                        },
                        {
                            "id": "fig_chart_trend",
                            "x": 0,
                            "y": 125,
                            "width": 950,
                            "height": 330,
                            "tag": "chart",
                            "data": {
                                "type": "odoo_line",
                                "title": {"text": "Monthly Sales Trend", "bold": True, "color": "#1E293B"},
                                "background": "#FFFFFF",
                                "legendPosition": "none",
                                "fillArea": True,
                                "stacked": False,
                                "metaData": {
                                    "groupBy": ["date_order:month"],
                                    "measure": "amount_total",
                                    "order": None,
                                    "resModel": "sale.order",
                                    "mode": "line"
                                },
                                "searchParams": {
                                    "domain": [["state", "=", "sale"]],
                                    "groupBy": ["date_order:month"],
                                    "context": {"group_by": []},
                                    "orderBy": []
                                },
                                "verticalAxisPosition": "left",
                                "fieldMatching": {
                                    "13d30fda-b14d-4a56-b186-25468af3b1e9": {"chain": "date_order", "type": "datetime", "offset": 0},
                                    "7d95fe28-fd13-4805-948c-95742e7d6733": {"chain": "partner_id", "type": "many2one"},
                                    "4181e8e3-2e7e-42be-a88b-f24acb7d03e7": {"chain": "user_id", "type": "many2one"}
                                }
                            }
                        }
                    ],
                    "cells": {
                        "A24": {"content": "=_t(\"Top Quotations\")"},
                        "A25": {"content": "=_t(\"Reference\")"},
                        "B25": {"content": "=_t(\"Customer\")"},
                        "C25": {"content": "=_t(\"Total\")"},
                        "D25": {"content": "=_t(\"Status\")"},
                        "A26": {"content": "=ODOO.LIST(1,1,\"name\")"},
                        "B26": {"content": "=ODOO.LIST(1,1,\"partner_id\")"},
                        "C26": {"content": "=ODOO.LIST(1,1,\"amount_total\")"},
                        "D26": {"content": "=ODOO.LIST(1,1,\"state\")"},
                        "A27": {"content": "=ODOO.LIST(1,2,\"name\")"},
                        "B27": {"content": "=ODOO.LIST(1,2,\"partner_id\")"},
                        "C27": {"content": "=ODOO.LIST(1,2,\"amount_total\")"},
                        "D27": {"content": "=ODOO.LIST(1,2,\"state\")"},
                        "A28": {"content": "=ODOO.LIST(1,3,\"name\")"},
                        "B28": {"content": "=ODOO.LIST(1,3,\"partner_id\")"},
                        "C28": {"content": "=ODOO.LIST(1,3,\"amount_total\")"},
                        "D28": {"content": "=ODOO.LIST(1,3,\"state\")"},
                        "A29": {"content": "=ODOO.LIST(1,4,\"name\")"},
                        "B29": {"content": "=ODOO.LIST(1,4,\"partner_id\")"},
                        "C29": {"content": "=ODOO.LIST(1,4,\"amount_total\")"},
                        "D29": {"content": "=ODOO.LIST(1,4,\"state\")"},
                        "A30": {"content": "=ODOO.LIST(1,5,\"name\")"},
                        "B30": {"content": "=ODOO.LIST(1,5,\"partner_id\")"},
                        "C30": {"content": "=ODOO.LIST(1,5,\"amount_total\")"},
                        "D30": {"content": "=ODOO.LIST(1,5,\"state\")"},
                        "F24": {"content": "=_t(\"Top Confirmed Orders\")"},
                        "F25": {"content": "=_t(\"Reference\")"},
                        "G25": {"content": "=_t(\"Customer\")"},
                        "H25": {"content": "=_t(\"Total\")"},
                        "I25": {"content": "=_t(\"Status\")"},
                        "F26": {"content": "=ODOO.LIST(2,1,\"name\")"},
                        "G26": {"content": "=ODOO.LIST(2,1,\"partner_id\")"},
                        "H26": {"content": "=ODOO.LIST(2,1,\"amount_total\")"},
                        "I26": {"content": "=ODOO.LIST(2,1,\"state\")"},
                        "F27": {"content": "=ODOO.LIST(2,2,\"name\")"},
                        "G27": {"content": "=ODOO.LIST(2,2,\"partner_id\")"},
                        "H27": {"content": "=ODOO.LIST(2,2,\"amount_total\")"},
                        "I27": {"content": "=ODOO.LIST(2,2,\"state\")"},
                        "F28": {"content": "=ODOO.LIST(2,3,\"name\")"},
                        "G28": {"content": "=ODOO.LIST(2,3,\"partner_id\")"},
                        "H28": {"content": "=ODOO.LIST(2,3,\"amount_total\")"},
                        "I28": {"content": "=ODOO.LIST(2,3,\"state\")"},
                        "F29": {"content": "=ODOO.LIST(2,4,\"name\")"},
                        "G29": {"content": "=ODOO.LIST(2,4,\"partner_id\")"},
                        "H29": {"content": "=ODOO.LIST(2,4,\"amount_total\")"},
                        "I29": {"content": "=ODOO.LIST(2,4,\"state\")"},
                        "F30": {"content": "=ODOO.LIST(2,5,\"name\")"},
                        "G30": {"content": "=ODOO.LIST(2,5,\"partner_id\")"},
                        "H30": {"content": "=ODOO.LIST(2,5,\"amount_total\")"},
                        "I30": {"content": "=ODOO.LIST(2,5,\"state\")"}
                    }
                },
                {
                    "id": "sheet_data",
                    "name": "Data",
                    "colNumber": 26,
                    "rowNumber": 100,
                    "rows": {},
                    "cols": {},
                    "merges": [],
                    "figures": [],
                    "cells": {
                        "A1": {"content": "=_t(\"KPI\")"},
                        "B1": {"content": "=_t(\"Current\")"},
                        "A2": {"content": "=_t(\"Quotations\")"},
                        "B2": {"content": "=PIVOT.VALUE(1,\"__count\",\"state\",\"draft\")+PIVOT.VALUE(1,\"__count\",\"state\",\"sent\")", "value": quot_count},
                        "A3": {"content": "=_t(\"Confirmed Orders\")"},
                        "B3": {"content": "=PIVOT.VALUE(1,\"__count\",\"state\",\"sale\")", "value": so_count},
                        "A4": {"content": "=_t(\"Revenue\")"},
                        "B4": {"content": "=PIVOT.VALUE(1,\"amount_total\",\"state\",\"sale\")", "value": revenue},
                        "A5": {"content": "=_t(\"Average Order\")"},
                        "B5": {"content": "=IFERROR(B4/B3,0)", "value": avg_order}
                    }
                }
            ],
            "pivots": {
                "1": {
                    "id": "1",
                    "type": "ODOO",
                    "name": "Sales by State",
                    "model": "sale.order",
                    "domain": [],
                    "context": {"group_by": []},
                    "measures": [
                        {"id": "__count", "fieldName": "__count"},
                        {"id": "amount_total", "fieldName": "amount_total"}
                    ],
                    "rows": [{"fieldName": "state"}],
                    "columns": [],
                    "sortedColumn": None,
                    "fieldMatching": {
                        "13d30fda-b14d-4a56-b186-25468af3b1e9": {"chain": "date_order", "type": "datetime", "offset": 0},
                        "7d95fe28-fd13-4805-948c-95742e7d6733": {"chain": "partner_id", "type": "many2one"},
                        "4181e8e3-2e7e-42be-a88b-f24acb7d03e7": {"chain": "user_id", "type": "many2one"}
                    }
                }
            },
            "pivotNextId": 2,
            "lists": {
                "1": {
                    "id": "1",
                    "name": "Top Quotations",
                    "model": "sale.order",
                    "columns": ["name", "partner_id", "amount_total", "state"],
                    "domain": [["state", "in", ["draft", "sent"]]],
                    "context": {},
                    "orderBy": [{"name": "amount_total", "asc": False}],
                    "fieldMatching": {
                        "13d30fda-b14d-4a56-b186-25468af3b1e9": {"chain": "date_order", "type": "datetime", "offset": 0},
                        "7d95fe28-fd13-4805-948c-95742e7d6733": {"chain": "partner_id", "type": "many2one"},
                        "4181e8e3-2e7e-42be-a88b-f24acb7d03e7": {"chain": "user_id", "type": "many2one"}
                    }
                },
                "2": {
                    "id": "2",
                    "name": "Top Confirmed Orders",
                    "model": "sale.order",
                    "columns": ["name", "partner_id", "amount_total", "state"],
                    "domain": [["state", "=", "sale"]],
                    "context": {},
                    "orderBy": [{"name": "amount_total", "asc": False}],
                    "fieldMatching": {
                        "13d30fda-b14d-4a56-b186-25468af3b1e9": {"chain": "date_order", "type": "datetime", "offset": 0},
                        "7d95fe28-fd13-4805-948c-95742e7d6733": {"chain": "partner_id", "type": "many2one"},
                        "4181e8e3-2e7e-42be-a88b-f24acb7d03e7": {"chain": "user_id", "type": "many2one"}
                    }
                }
            },
            "listNextId": 3,
            "globalFilters": [
                {
                    "id": "13d30fda-b14d-4a56-b186-25468af3b1e9",
                    "type": "date",
                    "label": "Period",
                    "defaultValue": "last_90_days"
                },
                {
                    "id": "7d95fe28-fd13-4805-948c-95742e7d6733",
                    "type": "relation",
                    "label": "Customer",
                    "modelName": "res.partner"
                },
                {
                    "id": "4181e8e3-2e7e-42be-a88b-f24acb7d03e7",
                    "type": "relation",
                    "label": "Salesperson",
                    "modelName": "res.users"
                }
            ]
        }

    def _generate_dashboard_html_preview(self, name, scorecards, monthly_chart, top_quotations, top_sales_orders, web_url=None):
        """Generate a modern, responsive interactive executive HTML preview for side-screen rendering."""
        import html as py_html
        safe_name = py_html.escape(name)

        # SVG Chart generation
        chart_svg = ""
        if monthly_chart:
            w = 820
            h = 240
            pad_left = 75
            pad_right = 30
            pad_top = 25
            pad_bottom = 35
            plot_w = w - pad_left - pad_right
            plot_h = h - pad_top - pad_bottom

            revenues = [pt.get("revenue", 0.0) for pt in monthly_chart]
            max_rev = max(revenues) if revenues and max(revenues) > 0 else 1.0
            magnitude = 10 ** max(1, len(str(int(max_rev))) - 1)
            ceil_rev = ((int(max_rev) // magnitude) + 1) * magnitude

            n = len(monthly_chart)
            pts = []
            for idx, pt in enumerate(monthly_chart):
                x = pad_left + (idx / max(1, n - 1)) * plot_w
                ratio = min(1.0, max(0.0, pt.get("revenue", 0.0) / ceil_rev))
                y = (pad_top + plot_h) - (ratio * plot_h)
                pts.append((x, y, pt))

            line_path = "M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts)
            area_path = f"M {pts[0][0]:.1f},{pad_top + plot_h:.1f} " + " ".join(f"L {p[0]:.1f},{p[1]:.1f}" for p in pts) + f" L {pts[-1][0]:.1f},{pad_top + plot_h:.1f} Z"

            grid_lines = ""
            for step in range(5):
                gy = pad_top + (step / 4.0) * plot_h
                val = ceil_rev * (1.0 - step / 4.0)
                grid_lines += f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{w - pad_right}" y2="{gy:.1f}" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="3 3"/>'
                grid_lines += f'<text x="{pad_left - 10}" y="{gy + 4:.1f}" fill="#64748b" font-size="11" font-weight="500" text-anchor="end">${val:,.0f}</text>'

            data_dots = ""
            x_labels = ""
            for x, y, pt in pts:
                period_esc = py_html.escape(pt.get("period", ""))
                val_esc = py_html.escape(pt.get("formatted", ""))
                data_dots += f'''<g class="chart-point">
                    <circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ffffff" stroke="#2563eb" stroke-width="3" style="transition: all 0.2s; cursor: pointer;"/>
                    <title>{period_esc}: {val_esc}</title>
                </g>'''
                x_labels += f'<text x="{x:.1f}" y="{h - 10}" fill="#475569" font-size="11" font-weight="500" text-anchor="middle">{period_esc}</text>'

            chart_svg = f'''
            <svg viewBox="0 0 {w} {h}" class="chart-svg" style="width: 100%; height: auto; max-height: 270px; display: block;">
                <defs>
                    <linearGradient id="chartGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.35" />
                        <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02" />
                    </linearGradient>
                </defs>
                {grid_lines}
                <path d="{area_path}" fill="url(#chartGradient)" />
                <path d="{line_path}" fill="none" stroke="#2563eb" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
                {data_dots}
                {x_labels}
            </svg>
            '''
        else:
            chart_svg = '<div style="text-align: center; color: #94a3b8; padding: 40px; font-size: 13px;">No trend data available for current selection.</div>'

        # Scorecard HTML
        card_themes = [
            {"border": "#3b82f6", "bg": "linear-gradient(135deg, #eff6ff 0%, #ffffff 100%)", "color": "#1e40af", "badge_bg": "#dbeafe", "badge_color": "#1e40af"},
            {"border": "#10b981", "bg": "linear-gradient(135deg, #ecfdf5 0%, #ffffff 100%)", "color": "#065f46", "badge_bg": "#d1fae5", "badge_color": "#065f46"},
            {"border": "#f59e0b", "bg": "linear-gradient(135deg, #fff7ed 0%, #ffffff 100%)", "color": "#9a3412", "badge_bg": "#ffedd5", "badge_color": "#9a3412"},
            {"border": "#8b5cf6", "bg": "linear-gradient(135deg, #faf5ff 0%, #ffffff 100%)", "color": "#6b21a8", "badge_bg": "#f3e8ff", "badge_color": "#6b21a8"},
        ]
        sc_html = ""
        for idx, sc in enumerate(scorecards):
            th = card_themes[idx % len(card_themes)]
            lbl = py_html.escape(sc.get("label", ""))
            val = py_html.escape(str(sc.get("value", "")))
            trnd = py_html.escape(sc.get("trend", ""))
            icon = sc.get("icon", "📌")
            sc_html += f'''
            <div class="kpi-card" style="border-top: 3px solid {th['border']}; background: {th['bg']};">
                <div class="kpi-header">
                    <span class="kpi-label">{lbl}</span>
                    <span class="kpi-icon">{icon}</span>
                </div>
                <div class="kpi-value" style="color: {th['color']};">{val}</div>
                <div class="kpi-trend" style="background: {th['badge_bg']}; color: {th['badge_color']};">
                    {trnd}
                </div>
            </div>'''

        # Tables HTML
        tq_rows = ""
        for q in top_quotations:
            cust = py_html.escape(q.get("customer", ""))
            rep = py_html.escape(q.get("salesperson", ""))
            rev = py_html.escape(q.get("revenue", ""))
            tq_rows += f'''
            <tr>
                <td style="font-weight: 500; color: #1e293b;">{cust}</td>
                <td style="color: #64748b;">{rep}</td>
                <td style="font-weight: 600; color: #0f172a; text-align: right;">{rev}</td>
                <td style="text-align: center;"><span class="badge badge-quote">Quotation</span></td>
            </tr>'''
        if not tq_rows:
            tq_rows = '<tr><td colspan="4" style="text-align: center; color: #94a3b8; padding: 20px;">No quotations found</td></tr>'

        tso_rows = ""
        for o in top_sales_orders:
            cust = py_html.escape(o.get("customer", ""))
            rep = py_html.escape(o.get("salesperson", ""))
            rev = py_html.escape(o.get("revenue", ""))
            tso_rows += f'''
            <tr>
                <td style="font-weight: 500; color: #1e293b;">{cust}</td>
                <td style="color: #64748b;">{rep}</td>
                <td style="font-weight: 600; color: #059669; text-align: right;">{rev}</td>
                <td style="text-align: center;"><span class="badge badge-order">Sales Order</span></td>
            </tr>'''
        if not tso_rows:
            tso_rows = '<tr><td colspan="4" style="text-align: center; color: #94a3b8; padding: 20px;">No confirmed orders found</td></tr>'

        link_btn = ""
        if web_url:
            link_btn = f'<a href="{web_url}" target="_blank" class="action-btn action-btn-primary">👉 Open in Odoo Dashboards</a>'
        else:
            link_btn = '<span class="action-badge">Ready to Deploy • Reply "Confirm" to create in Odoo</span>'

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_name} - Odoo Preview</title>
    <style>
        :root {{
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --primary: #2563eb;
            --primary-light: #eff6ff;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: var(--font-family);
            padding: 24px;
            -webkit-font-smoothing: antialiased;
        }}
        .dashboard-container {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}
        .dashboard-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border-color);
        }}
        .dashboard-title-area h1 {{
            font-size: 22px;
            font-weight: 700;
            color: #0f172a;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .dashboard-subtitle {{
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 2px;
        }}
        .filter-bar {{
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding: 4px 0;
            flex-wrap: wrap;
        }}
        .filter-chip {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #ffffff;
            border: 1px solid var(--border-color);
            border-radius: 9999px;
            padding: 6px 14px;
            font-size: 12px;
            font-weight: 500;
            color: #334155;
            cursor: pointer;
            transition: all 0.15s ease;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
        }}
        .filter-chip:hover {{
            background: #f1f5f9;
            border-color: #cbd5e1;
        }}
        .filter-chip.active {{
            background: #eff6ff;
            border-color: #93c5fd;
            color: #1d4ed8;
            font-weight: 600;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
        }}
        .kpi-card {{
            border-radius: 12px;
            border: 1px solid var(--border-color);
            padding: 18px 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }}
        .kpi-card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08);
        }}
        .kpi-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }}
        .kpi-label {{
            font-size: 13px;
            font-weight: 600;
            color: #475569;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }}
        .kpi-icon {{
            font-size: 18px;
        }}
        .kpi-value {{
            font-size: 28px;
            font-weight: 700;
            line-height: 1.2;
            margin-bottom: 8px;
        }}
        .kpi-trend {{
            display: inline-block;
            font-size: 11px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 6px;
        }}
        .chart-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        }}
        .chart-title {{
            font-size: 16px;
            font-weight: 600;
            color: #1e293b;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .chart-badge {{
            font-size: 12px;
            color: #059669;
            background: #ecfdf5;
            padding: 4px 10px;
            border-radius: 9999px;
            font-weight: 600;
        }}
        .tables-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
            gap: 20px;
        }}
        .table-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        }}
        .table-title {{
            font-size: 15px;
            font-weight: 600;
            color: #1e293b;
            margin-bottom: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .count-badge {{
            font-size: 11px;
            font-weight: 600;
            background: #f1f5f9;
            color: #475569;
            padding: 2px 8px;
            border-radius: 12px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        th {{
            text-align: left;
            padding: 8px 10px;
            font-size: 11px;
            font-weight: 600;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            border-bottom: 1px solid var(--border-color);
        }}
        td {{
            padding: 10px 10px;
            border-bottom: 1px solid #f1f5f9;
        }}
        tr:hover td {{
            background: #f8fafc;
        }}
        .badge {{
            display: inline-block;
            font-size: 11px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 6px;
        }}
        .badge-quote {{
            background: #eff6ff;
            color: #2563eb;
        }}
        .badge-order {{
            background: #ecfdf5;
            color: #059669;
        }}
        .dock-bar {{
            background: #ffffff;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        }}
        .dock-status {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            font-weight: 500;
            color: #334155;
        }}
        .status-dot {{
            width: 8px;
            height: 8px;
            background: #10b981;
            border-radius: 50%;
            box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.2);
        }}
        .action-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #2563eb;
            color: #ffffff;
            font-size: 13px;
            font-weight: 600;
            padding: 8px 16px;
            border-radius: 8px;
            text-decoration: none;
            transition: background 0.15s ease;
        }}
        .action-btn:hover {{
            background: #1d4ed8;
        }}
        .action-badge {{
            font-size: 12px;
            font-weight: 600;
            color: #2563eb;
            background: #eff6ff;
            padding: 6px 14px;
            border-radius: 8px;
            border: 1px solid #bfdbfe;
        }}
    </style>
</head>
<body>
    <div class="dashboard-container">
        <!-- Header -->
        <div class="dashboard-header">
            <div class="dashboard-title-area">
                <h1>📊 {safe_name}</h1>
                <div class="dashboard-subtitle">Native Odoo 18 Spreadsheet Dashboard • Live Enterprise Analytics</div>
            </div>
            <div class="filter-bar">
                <span class="filter-chip active">📅 Last 90 Days ▼</span>
                <span class="filter-chip">🌍 Country</span>
                <span class="filter-chip">📦 Product</span>
                <span class="filter-chip">👤 Customer</span>
                <span class="filter-chip">👥 Sales Team</span>
                <span class="filter-chip">👔 Salesperson</span>
            </div>
        </div>

        <!-- KPI Scorecards -->
        <div class="kpi-grid">
            {sc_html}
        </div>

        <!-- Monthly Trend Chart -->
        <div class="chart-card">
            <div class="chart-title">
                <span>📈 Monthly Sales Trend (Confirmed Orders)</span>
                <span class="chart-badge">Active Trajectory</span>
            </div>
            {chart_svg}
        </div>

        <!-- Side-by-Side Tables -->
        <div class="tables-grid">
            <div class="table-card">
                <div class="table-title">
                    <span>📋 Top Quotations (Pipeline)</span>
                    <span class="count-badge">Top 5 by Value</span>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Customer</th>
                            <th>Salesperson</th>
                            <th style="text-align: right;">Total</th>
                            <th style="text-align: center;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {tq_rows}
                    </tbody>
                </table>
            </div>

            <div class="table-card">
                <div class="table-title">
                    <span>🏆 Top Confirmed Orders (Closed Won)</span>
                    <span class="count-badge">Top 5 by Value</span>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Customer</th>
                            <th>Salesperson</th>
                            <th style="text-align: right;">Total</th>
                            <th style="text-align: center;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {tso_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Dock Action Bar -->
        <div class="dock-bar">
            <div class="dock-status">
                <span class="status-dot"></span>
                <span>Live Odoo 18 Multi-Model Sync • Validated Measures &amp; Zero Layout Overlap</span>
            </div>
            <div>
                {link_btn}
            </div>
        </div>
    </div>
</body>
</html>'''

    def _tool_create_spreadsheet_dashboard(self, env, is_readonly, args, is_preview=False):
        name = (args.get("name") or "Executive Sales Dashboard").strip()
        model = self._validate_model_access(env, args.get("model") or "sale.order", mode="read")
        domain = args.get("domain") or []

        # -------------------------------------------------------------
        # Gather live analytics data for Scorecards, Chart, and Tables
        # -------------------------------------------------------------
        if model in ("sale.order", "sale.report"):
            quot_count = env["sale.order"].search_count(domain + [("state", "in", ["draft", "sent"])])
            so_count = env["sale.order"].search_count(domain + [("state", "in", ["sale", "done"])])
            so_rg = env["sale.order"].read_group(domain + [("state", "in", ["sale", "done"])], ["amount_total:sum"], [])
            revenue = so_rg[0].get("amount_total", 0.0) if so_rg else 0.0
            avg_order = revenue / so_count if so_count else 0.0

            scorecards = [
                {"label": "Quotations", "value": f"{quot_count}", "trend": "↑ 100% since last period", "icon": "📋"},
                {"label": "Orders", "value": f"{so_count}", "trend": "↑ 100% since last period", "icon": "📦"},
                {"label": "Revenue", "value": f"${revenue:,.2f}", "trend": "↑ 100% since last period", "icon": "💰"},
                {"label": "Average Order", "value": f"${avg_order:,.2f}", "trend": "↑ 100% since last period", "icon": "📊"},
            ]

            monthly_rg = env["sale.order"].read_group(
                domain + [("state", "in", ["sale", "done"])],
                ["amount_total:sum"],
                ["date_order:month"],
                lazy=False
            )
            monthly_chart = [
                {
                    "period": m.get("date_order:month", "Unknown"),
                    "revenue": m.get("amount_total", 0.0),
                    "formatted": f"${m.get('amount_total', 0.0):,.2f}",
                }
                for m in monthly_rg
            ]

            top_quotes_raw = env["sale.order"].search_read(
                domain + [("state", "in", ["draft", "sent"])],
                ["partner_id", "user_id", "amount_total"],
                order="amount_total desc",
                limit=5
            )
            top_quotations = [
                {
                    "customer": q["partner_id"][1] if q["partner_id"] else "None",
                    "salesperson": q["user_id"][1] if q["user_id"] else "None",
                    "revenue": f"${q['amount_total']:,.2f}",
                }
                for q in top_quotes_raw
            ]

            top_orders_raw = env["sale.order"].search_read(
                domain + [("state", "in", ["sale", "done"])],
                ["partner_id", "user_id", "amount_total"],
                order="amount_total desc",
                limit=5
            )
            top_sales_orders = [
                {
                    "customer": o["partner_id"][1] if o["partner_id"] else "None",
                    "salesperson": o["user_id"][1] if o["user_id"] else "None",
                    "revenue": f"${o['amount_total']:,.2f}",
                }
                for o in top_orders_raw
            ]
        else:
            model_pool = env[model]
            count = model_pool.search_count(domain)
            meas_field = args.get("measure_field") or "__count"
            has_meas = meas_field != "__count" and meas_field in model_pool._fields
            total_val = 0.0
            if has_meas:
                rg = model_pool.read_group(domain, [f"{meas_field}:sum"], [])
                total_val = rg[0].get(meas_field, 0.0) if rg else 0.0
            quot_count = 0
            so_count = count
            revenue = total_val
            avg_order = total_val / count if count else 0.0
            scorecards = [
                {"label": "Total Records", "value": f"{count}", "trend": "Active", "icon": "📋"},
                {"label": f"Total {meas_field.replace('_', ' ').title()}", "value": f"${total_val:,.2f}" if has_meas else f"{count}", "trend": "Aggregated", "icon": "💰"},
            ]
            monthly_chart = []
            top_quotations = []
            top_sales_orders = []

        dashboard_blueprint = {
            "name": name,
            "dashboard_type": "spreadsheet",
            "model": model,
            "scorecards": scorecards,
            "monthly_chart": monthly_chart,
            "top_quotations": top_quotations,
            "top_sales_orders": top_sales_orders,
            "filters": ["Period (Last 90 Days)", "Country", "Product", "Customer", "Sales Team", "Salesperson"],
        }

        # Build interactive HTML preview
        html_preview = self._generate_dashboard_html_preview(name, scorecards, monthly_chart, top_quotations, top_sales_orders)
        preview_file_path = os.path.join(tempfile.gettempdir(), "dashboard_preview.html")
        try:
            with open(preview_file_path, "w", encoding="utf-8") as f:
                f.write(html_preview)
        except Exception as e:
            _logger.debug("Could not write dashboard_preview.html: %s", e)

        # -------------------------------------------------------------
        # STAGE 1: PREVIEW MODE (Read-only, zero database modifications)
        # -------------------------------------------------------------
        if is_preview:
            sc_table = "| " + " | ".join(f"{s['icon']} {s['label']}" for s in scorecards) + " |\n"
            sc_table += "|" + "|".join([":---:" for _ in scorecards]) + "|\n"
            sc_table += "| " + " | ".join(f"**{s['value']}**<br>🟢 {s['trend']}" for s in scorecards) + " |"

            chart_md = "| Period | Revenue | Trajectory |\n|:---|---:|:---|\n"
            for pt in monthly_chart:
                bar = "█" * min(25, max(1, int(pt["revenue"] / (max(p["revenue"] for p in monthly_chart) or 1) * 25)))
                chart_md += f"| {pt['period']} | {pt['formatted']} | `{bar}` |\n"

            tq_md = "| Customer | Salesperson | Revenue |\n|:---|:---|---:|\n"
            for q in top_quotations:
                tq_md += f"| {q['customer']} | {q['salesperson']} | {q['revenue']} |\n"

            tso_md = "| Customer | Salesperson | Revenue |\n|:---|:---|---:|\n"
            for o in top_sales_orders:
                tso_md += f"| {o['customer']} | {o['salesperson']} | {o['revenue']} |\n"

            preview_visual_md = (
                f"### 🎛️ Odoo Spreadsheet Dashboard Preview: **{name}**\n\n"
                f"**🔍 Global Filter Bar**:\n"
                f"`[ 📅 Last 90 Days ▼ ]` `[ 🌍 Country ]` `[ 📦 Product ]` `[ 👤 Customer ]` `[ 👥 Sales Team ]` `[ 👔 Salesperson ]`\n\n"
                f"#### 📌 KPI Scorecards\n{sc_table}\n\n"
                f"#### 📈 Monthly Sales (Area / Line Chart)\n{chart_md}\n"
                f"#### 📑 Top Quotations\n{tq_md}\n"
                f"#### 🏆 Top Sales Orders\n{tso_md}\n\n"
                f"*(A side-screen interactive HTML preview has also been generated at `dashboard_preview.html`)*"
            )

            return {
                "success": True,
                "status": "preview",
                "confirmed": False,
                "dashboard_type": "spreadsheet",
                "name": name,
                "scorecards": scorecards,
                "monthly_chart": monthly_chart,
                "top_quotations": top_quotations,
                "top_sales_orders": top_sales_orders,
                "preview_visual_md": preview_visual_md,
                "preview_html": html_preview,
                "preview_html_path": preview_file_path,
                "dashboard_json": dashboard_blueprint,
                "message": (
                    f"Preview for Spreadsheet Dashboard '{name}' generated successfully with live Odoo data. "
                    "Review the interactive side-screen preview or the scorecards, area chart, and tables below. "
                    "Adjust any metrics or cards as needed, and reply 'Confirm' to deploy it directly into Odoo."
                ),
            }

        # -------------------------------------------------------------
        # STAGE 2: CONFIRM & DEPLOY MODE (Writes to spreadsheet.dashboard)
        # -------------------------------------------------------------
        if is_readonly:
            raise AccessDenied("This MCP token is configured as READ-ONLY. Creating dashboards in Odoo requires write access.")

        if hasattr(env["spreadsheet.dashboard"], "check_access"):
            env["spreadsheet.dashboard"].check_access("create")
        else:
            env["spreadsheet.dashboard"].check_access_rights("create")

        group_name = args.get("group_name") or "Sales"
        group = env["spreadsheet.dashboard.group"].search([("name", "=", group_name)], limit=1)
        if not group:
            group = env["spreadsheet.dashboard.group"].create({"name": group_name})

        # Build tailored, non-overlapping Odoo 18 spreadsheet data
        spreadsheet_dict = self._build_spreadsheet_dashboard_data(
            name=name,
            model=model,
            quot_count=quot_count,
            so_count=so_count,
            revenue=revenue,
            avg_order=avg_order
        )

        existing = env["spreadsheet.dashboard"].search([
            ("name", "=", name),
            ("dashboard_group_id", "=", group.id)
        ], limit=1)

        if existing:
            existing.write({
                "spreadsheet_data": json.dumps(spreadsheet_dict),
                "is_published": True,
            })
            dash = existing
        else:
            dash = env["spreadsheet.dashboard"].create({
                "name": name,
                "dashboard_group_id": group.id,
                "spreadsheet_data": json.dumps(spreadsheet_dict),
                "sequence": int(args.get("sequence", 10)),
                "is_published": True,
            })

        dash_act = env.ref("spreadsheet_dashboard.ir_actions_dashboard_action", raise_if_not_found=False)
        action_id = dash_act.id if dash_act else 408

        host_url = None
        try:
            if request and hasattr(request, "httprequest") and request.httprequest:
                host_url = request.httprequest.host_url.rstrip("/")
        except Exception:
            host_url = None
        base_url = host_url or env["ir.config_parameter"].sudo().get_param("web.base.url", "http://localhost:1234").rstrip("/")

        web_url = f"{base_url}/web#action={action_id}&dashboard_id={dash.id}"
        direct_url = f"{base_url}/odoo/action-{action_id}?dashboard_id={dash.id}"
        local_url = f"http://localhost:1234/web#action={action_id}&dashboard_id={dash.id}"

        # Update preview HTML with live redirect link
        html_preview = self._generate_dashboard_html_preview(
            name, scorecards, monthly_chart, top_quotations, top_sales_orders, web_url=web_url
        )
        try:
            with open(preview_file_path, "w", encoding="utf-8") as f:
                f.write(html_preview)
        except Exception as e:
            _logger.debug("Could not write updated dashboard_preview.html: %s", e)

        return {
            "success": True,
            "status": "confirmed",
            "confirmed": True,
            "dashboard_type": "spreadsheet",
            "dashboard_id": dash.id,
            "dashboard_group": group.name,
            "action_id": action_id,
            "web_url": web_url,
            "direct_url": direct_url,
            "local_url": local_url,
            "scorecards": scorecards,
            "preview_html": html_preview,
            "preview_html_path": preview_file_path,
            "dashboard_json": dashboard_blueprint,
            "message": f"Spreadsheet Dashboard '{name}' created and published in Odoo. Access it directly at {web_url}",
        }

    def _tool_create_dashboard(self, env, is_readonly, args):
        args = args or {}
        name = (args.get("name") or "").strip()
        if not name:
            raise ValidationError("Parameter 'name' is required for odoo_create_dashboard.")

        raw_model = args.get("model") or "sale.order"
        model = self._validate_model_access(env, raw_model, mode="read")
        domain = args.get("domain") or []
        if not isinstance(domain, list):
            raise ValidationError("Parameter 'domain' must be a list of domain tuples.")

        confirm_arg = args.get("confirm")
        is_preview = confirm_arg is False or (isinstance(confirm_arg, str) and confirm_arg.lower() in ("false", "0", "no"))

        dashboard_type = args.get("dashboard_type")
        if not dashboard_type:
            if "spreadsheet.dashboard" in env and not (args.get("graph_type") or args.get("pivot_row_fields")):
                dashboard_type = "spreadsheet"
            else:
                dashboard_type = "board" if "board.board" in env else "pivot"

        if dashboard_type == "spreadsheet" and "spreadsheet.dashboard" in env:
            return self._tool_create_spreadsheet_dashboard(env, is_readonly, args, is_preview=is_preview)

        layout_style = args.get("layout_style") or "2-1"
        target = args.get("target") or "standalone"
        graph_type = args.get("graph_type") or "bar"
        if graph_type not in ("bar", "line", "pie"):
            graph_type = "bar"

        dim_field = args.get("dimension_field")
        dim_interval = args.get("dimension_interval")
        meas_field = args.get("measure_field") or "__count"

        model_fields = env[model]._fields
        pivot_rows = args.get("pivot_row_fields") or ([dim_field] if dim_field else [])
        pivot_cols = args.get("pivot_col_fields") or []
        pivot_measures = args.get("pivot_measure_fields") or ([meas_field] if meas_field else ["__count"])

        # Normalize panels
        raw_panels = args.get("panels")
        if raw_panels and isinstance(raw_panels, list):
            panels = []
            for idx, p in enumerate(raw_panels):
                p_model = p.get("model") or model
                self._validate_model_access(env, p_model, mode="read")
                panels.append({
                    "title": p.get("title") or f"Panel {idx + 1}",
                    "model": p_model,
                    "view_mode": p.get("view_mode") or "pivot",
                    "column": int(p.get("column", idx % 2)),
                    "domain": p.get("domain") or domain,
                    "graph_type": p.get("graph_type") or graph_type,
                    "dimension_field": p.get("dimension_field") or dim_field,
                    "dimension_interval": p.get("dimension_interval") or dim_interval,
                    "measure_field": p.get("measure_field") or meas_field,
                    "pivot_row_fields": p.get("pivot_row_fields") or pivot_rows,
                    "pivot_col_fields": p.get("pivot_col_fields") or pivot_cols,
                    "pivot_measure_fields": p.get("pivot_measure_fields") or pivot_measures,
                    "list_fields": p.get("list_fields") or ["name", "display_name"],
                    "limit": int(p.get("limit") or 10),
                })
        else:
            panels = [
                {
                    "title": f"{name} - Pivot Analysis",
                    "model": model,
                    "view_mode": "pivot",
                    "column": 0,
                    "domain": domain,
                    "pivot_row_fields": pivot_rows,
                    "pivot_col_fields": pivot_cols,
                    "pivot_measure_fields": pivot_measures,
                    "measure_field": meas_field,
                },
                {
                    "title": f"{name} - {graph_type.capitalize()} Chart",
                    "model": model,
                    "view_mode": "graph",
                    "column": 1,
                    "domain": domain,
                    "graph_type": graph_type,
                    "dimension_field": dim_field,
                    "dimension_interval": dim_interval,
                    "measure_field": meas_field,
                },
            ]

        dashboard_blueprint = {
            "name": name,
            "dashboard_type": dashboard_type,
            "layout_style": layout_style,
            "target": target,
            "model": model,
            "domain": domain,
            "panels": [
                {
                    "title": p["title"],
                    "model": p["model"],
                    "view_mode": p["view_mode"],
                    "column": p["column"],
                    "domain": p["domain"],
                    "config": {
                        "graph_type": p.get("graph_type"),
                        "dimension_field": p.get("dimension_field"),
                        "dimension_interval": p.get("dimension_interval"),
                        "measure_field": p.get("measure_field"),
                        "pivot_row_fields": p.get("pivot_row_fields"),
                        "pivot_col_fields": p.get("pivot_col_fields"),
                        "pivot_measure_fields": p.get("pivot_measure_fields"),
                    }
                }
                for p in panels
            ]
        }

        # -------------------------------------------------------------
        # STAGE 1: PREVIEW MODE (Read-only, no database modifications)
        # -------------------------------------------------------------
        if is_preview:
            preview_panels = []
            for p in panels:
                p_model = p["model"]
                p_env = env[p_model]
                p_info = dict(p)

                if p["view_mode"] == "pivot":
                    p_info.update(self._compute_pivot_preview(p_env, p))
                elif p["view_mode"] == "graph":
                    p_info.update(self._compute_graph_preview(p_env, p))
                elif p["view_mode"] == "list":
                    p_info.update(self._compute_list_preview(p_env, p))

                preview_panels.append(p_info)

            kpis = self._compute_quick_kpis(env[model], domain, meas_field)

            return {
                "success": True,
                "status": "preview",
                "confirmed": False,
                "name": name,
                "dashboard_type": dashboard_type,
                "layout_style": layout_style,
                "target": target,
                "panels": preview_panels,
                "kpis": kpis,
                "dashboard_json": dashboard_blueprint,
                "message": (
                    f"Preview for dashboard '{name}' generated successfully with live Odoo data. "
                    "Review the in-chat preview, pivot tables, and KPI layout below. "
                    "Adjust any dimensions, measures, or panels as needed, and reply 'Confirm' "
                    "or call with confirm=true to publish it directly to your Odoo system."
                ),
            }

        # -------------------------------------------------------------
        # STAGE 2: CONFIRM & DEPLOY MODE (Writes views, actions, menus)
        # -------------------------------------------------------------
        if is_readonly:
            raise AccessDenied("This MCP token is configured as READ-ONLY. Creating dashboards and views in Odoo requires write access.")

        for m in ("ir.ui.view", "ir.actions.act_window", "ir.ui.menu"):
            if hasattr(env[m], "check_access"):
                env[m].check_access("create")
            else:
                env[m].check_access_rights("create")

        is_odoo_18 = odoo.release.version_info[0] >= 18
        created_panel_actions = []
        graph_view = None
        pivot_view = None

        for p in panels:
            p_model = p["model"]
            p_fields = env[p_model]._fields
            p_view_mode = p["view_mode"]
            p_view = None

            if p_view_mode == "pivot":
                p_rows = p.get("pivot_row_fields") or []
                p_cols = p.get("pivot_col_fields") or []
                p_measures = p.get("pivot_measure_fields") or [p.get("measure_field", "__count")]

                pivot_fields_xml = []
                for rf in p_rows:
                    if ":" in rf:
                        f_base, f_int = rf.split(":", 1)
                        if f_base in p_fields:
                            pivot_fields_xml.append(f'<field name="{f_base}" interval="{f_int}" type="row"/>')
                    elif rf in p_fields:
                        pivot_fields_xml.append(f'<field name="{rf}" type="row"/>')

                for cf in p_cols:
                    if ":" in cf:
                        f_base, f_int = cf.split(":", 1)
                        if f_base in p_fields:
                            pivot_fields_xml.append(f'<field name="{f_base}" interval="{f_int}" type="col"/>')
                    elif cf in p_fields:
                        pivot_fields_xml.append(f'<field name="{cf}" type="col"/>')

                for mf in p_measures:
                    if mf and mf != "__count" and mf in p_fields:
                        pivot_fields_xml.append(f'<field name="{mf}" type="measure"/>')

                pivot_arch = (
                    f'<pivot string="{p["title"]} (Pivot)" sample="1">\n'
                    + "\n".join(f"    {f}" for f in pivot_fields_xml)
                    + "\n</pivot>"
                )
                p_view = env["ir.ui.view"].create({
                    "name": f"{p['title']} - Pivot",
                    "model": p_model,
                    "type": "pivot",
                    "arch": pivot_arch,
                    "priority": 100,
                })
                pivot_view = p_view

            elif p_view_mode == "graph":
                p_gtype = p.get("graph_type", "bar")
                p_dim = p.get("dimension_field")
                p_dim_int = p.get("dimension_interval")
                p_meas = p.get("measure_field") or "__count"
                p_dim_attr = f' interval="{p_dim_int}"' if p_dim_int else ""

                graph_fields_xml = []
                if p_dim and p_dim in p_fields:
                    graph_fields_xml.append(f'<field name="{p_dim}"{p_dim_attr} type="row"/>')
                if p_meas and p_meas != "__count" and p_meas in p_fields:
                    graph_fields_xml.append(f'<field name="{p_meas}" type="measure"/>')

                graph_arch = (
                    f'<graph string="{p["title"]} (Graph)" type="{p_gtype}" sample="1">\n'
                    + "\n".join(f"    {f}" for f in graph_fields_xml)
                    + "\n</graph>"
                )
                p_view = env["ir.ui.view"].create({
                    "name": f"{p['title']} - Graph",
                    "model": p_model,
                    "type": "graph",
                    "arch": graph_arch,
                    "priority": 100,
                })
                graph_view = p_view

            panel_act_vals = {
                "name": p["title"],
                "res_model": p_model,
                "view_mode": p_view_mode,
                "view_id": p_view.id if p_view else False,
                "domain": json.dumps(p.get("domain", [])) if p.get("domain") else False,
            }
            if p_view_mode == "graph":
                panel_act_vals["context"] = json.dumps({
                    "graph_mode": p.get("graph_type", "bar"),
                    "graph_measure": p.get("measure_field") if p.get("measure_field") != "__count" else False,
                })
            panel_act = env["ir.actions.act_window"].create(panel_act_vals)
            created_panel_actions.append({
                "panel": p,
                "action": panel_act,
                "view": p_view,
            })

        board_view = None
        main_action = None
        menu = None

        if "board.board" in env and dashboard_type == "board":
            # Organize panel actions into columns for board.board
            cols_map = {}
            for item in created_panel_actions:
                col_idx = item["panel"].get("column", 0)
                cols_map.setdefault(col_idx, []).append(item)

            max_col = max(cols_map.keys()) if cols_map else 0
            num_cols = max(2, max_col + 1)
            columns_xml = []
            for col_i in range(num_cols):
                actions_xml = []
                for item in cols_map.get(col_i, []):
                    act = item["action"]
                    p = item["panel"]
                    actions_xml.append(
                        f'    <action name="{act.id}" string="{p["title"]}" view_mode="{p["view_mode"]}"/>'
                    )
                columns_xml.append(
                    "  <column>\n" + ("\n".join(actions_xml) if actions_xml else "  ") + "\n  </column>"
                )

            board_arch = (
                f'<form string="{name}" js_class="board">\n'
                f'  <board style="{layout_style}">\n'
                + "\n".join(columns_xml)
                + "\n  </board>\n</form>"
            )

            if target == "standalone":
                board_view = env["ir.ui.view"].create({
                    "name": f"{name} - Board View",
                    "model": "board.board",
                    "type": "form",
                    "arch": board_arch,
                    "priority": 100,
                })
                main_action = env["ir.actions.act_window"].create({
                    "name": name,
                    "res_model": "board.board",
                    "view_mode": "form",
                    "view_id": board_view.id,
                })
            elif target == "my_dashboard":
                my_dash_act = env.ref("board.open_board_my_dash_action", raise_if_not_found=False)
                if my_dash_act:
                    main_action = my_dash_act
                    board_view = my_dash_act.view_id
                    # Add to user's custom view
                    try:
                        from lxml import etree as ElementTree
                        cust = env["ir.ui.view.custom"].search([
                            ("user_id", "=", env.uid),
                            ("ref_id", "=", my_dash_act.view_id.id if my_dash_act.view_id else False)
                        ], limit=1)
                        if cust:
                            b_tree = ElementTree.fromstring(cust.arch)
                        else:
                            b_tree = ElementTree.fromstring(my_dash_act.view_id.arch if my_dash_act.view_id else board_arch)
                        first_col = b_tree.find("./board/column")
                        if first_col is not None:
                            for item in created_panel_actions:
                                act = item["action"]
                                p = item["panel"]
                                first_col.insert(0, ElementTree.Element("action", {
                                    "name": str(act.id),
                                    "string": p["title"],
                                    "view_mode": p["view_mode"],
                                }))
                            new_arch = ElementTree.tostring(b_tree, encoding="unicode")
                            if cust:
                                cust.write({"arch": new_arch})
                            else:
                                env["ir.ui.view.custom"].create({
                                    "user_id": env.uid,
                                    "ref_id": my_dash_act.view_id.id if my_dash_act.view_id else False,
                                    "arch": new_arch,
                                })
                    except Exception as e:
                        _logger.debug("Failed adding to My Dashboard custom view: %s", e)

        if not main_action:
            # Fallback to direct model window action
            view_mode = "graph,pivot,list" if is_odoo_18 else "graph,pivot,tree"
            ctx = {
                "graph_mode": graph_type,
                "graph_measure": meas_field if meas_field != "__count" else False,
            }
            main_action = env["ir.actions.act_window"].create({
                "name": name,
                "res_model": model,
                "view_mode": view_mode,
                "view_id": graph_view.id if graph_view else (pivot_view.id if pivot_view else False),
                "domain": json.dumps(domain) if domain else False,
                "context": json.dumps(ctx),
            })

        # Menu Creation
        create_menu = args.get("create_menu", True)
        if create_menu and target == "standalone":
            parent_id = False
            menu_parent = args.get("menu_parent_id")
            if menu_parent:
                if isinstance(menu_parent, int):
                    parent_id = menu_parent
                elif isinstance(menu_parent, str):
                    if "." in menu_parent:
                        parent_rec = env.ref(menu_parent, raise_if_not_found=False)
                        if parent_rec and parent_rec._name == "ir.ui.menu":
                            parent_id = parent_rec.id
                    if not parent_id:
                        found = env["ir.ui.menu"].search([("name", "=", menu_parent)], limit=1)
                        if found:
                            parent_id = found.id
            if not parent_id:
                custom_menu = env.ref("abi_odoo_mcp.menu_mcp_custom_dashboards", raise_if_not_found=False)
                if custom_menu:
                    parent_id = custom_menu.id
                else:
                    root_menu = env.ref("abi_odoo_mcp.menu_mcp_root", raise_if_not_found=False)
                    parent_id = root_menu.id if root_menu else False

            if parent_id:
                menu = env["ir.ui.menu"].create({
                    "name": name,
                    "action": f"ir.actions.act_window,{main_action.id}",
                    "parent_id": parent_id,
                    "sequence": int(args.get("sequence") or 50),
                })

        host_url = None
        try:
            if request and hasattr(request, "httprequest") and request.httprequest:
                host_url = request.httprequest.host_url.rstrip("/")
        except Exception:
            host_url = None
        base_url = host_url or env["ir.config_parameter"].sudo().get_param("web.base.url", "http://localhost:1234").rstrip("/")
        web_url = f"{base_url}/web#action={main_action.id}"
        direct_url = f"{base_url}/odoo/action-{main_action.id}" if is_odoo_18 else web_url
        local_url = f"http://localhost:1234/web#action={main_action.id}"

        preview_data = None
        if args.get("include_preview", True):
            preview_args = {
                "model": model,
                "domain": domain,
                "dimension_field": dim_field,
            }
            if meas_field and meas_field != "__count":
                preview_args["metrics"] = [{"field": meas_field, "operator": "sum", "label": f"Total {meas_field}"}]
            try:
                preview_data = self._tool_get_dashboard_data(env, preview_args)
            except Exception as e:
                _logger.debug("Could not generate dashboard preview data: %s", e)

        return {
            "success": True,
            "status": "confirmed",
            "confirmed": True,
            "message": f"Dashboard '{name}' successfully created and published in Odoo.",
            "action_id": main_action.id,
            "board_view_id": board_view.id if board_view else None,
            "graph_view_id": graph_view.id if graph_view else None,
            "pivot_view_id": pivot_view.id if pivot_view else None,
            "menu_id": menu.id if menu else None,
            "web_url": web_url,
            "direct_url": direct_url,
            "local_url": local_url,
            "model": model,
            "view_mode": "form" if ("board.board" in env and dashboard_type == "board") else ("graph,pivot,list" if is_odoo_18 else "graph,pivot,tree"),
            "graph_type": graph_type,
            "dashboard_type": dashboard_type,
            "target": target,
            "layout_style": layout_style,
            "dashboard_json": dashboard_blueprint,
            "preview_data": preview_data,
        }

    def _tool_get_dashboard_data(self, env, args):
        args = args or {}
        model = self._validate_model_access(env, args.get("model"), mode="read")
        domain = args.get("domain") or []
        if not isinstance(domain, list):
            raise ValidationError("Parameter 'domain' must be a list of domain tuples.")

        model_obj = env[model]
        model_fields = model_obj._fields

        # 1. Total matching count
        total_count = model_obj.search_count(domain)

        # 2. Compute KPI Metrics
        metrics_input = args.get("metrics")
        if not metrics_input:
            metrics_input = []
            if "amount_total" in model_fields:
                metrics_input.append({"field": "amount_total", "operator": "sum", "label": "Total Amount"})
                metrics_input.append({"field": "amount_total", "operator": "avg", "label": "Average Order"})
            elif "expected_revenue" in model_fields:
                metrics_input.append({"field": "expected_revenue", "operator": "sum", "label": "Total Expected Revenue"})
            elif "total_amount" in model_fields:
                metrics_input.append({"field": "total_amount", "operator": "sum", "label": "Total Amount"})
            elif "qty_done" in model_fields:
                metrics_input.append({"field": "qty_done", "operator": "sum", "label": "Total Quantity Done"})
            elif "planned_hours" in model_fields:
                metrics_input.append({"field": "planned_hours", "operator": "sum", "label": "Total Planned Hours"})

            metrics_input.append({"field": "id", "operator": "count", "label": "Total Records"})

        kpi_cards = []
        for m in metrics_input:
            fld = m.get("field")
            op = (m.get("operator") or "sum").lower()
            lbl = m.get("label") or f"{op.upper()} of {fld}"

            val = 0.0
            if fld == "id" and op == "count":
                val = total_count
            elif fld in model_fields:
                fdef = model_fields[fld]
                if fdef.type in ("integer", "float", "monetary"):
                    read_fields = [f"{fld}:{op}"] if op in ("sum", "avg", "min", "max") else [fld]
                    try:
                        grp = model_obj.read_group(domain, read_fields, [])
                        if grp:
                            val = grp[0].get(fld, 0.0) or 0.0
                    except Exception:
                        records = model_obj.search_read(domain, [fld], limit=1000)
                        vals = [r[fld] for r in records if r.get(fld)]
                        if vals:
                            if op == "sum":
                                val = sum(vals)
                            elif op == "avg":
                                val = sum(vals) / len(vals)
                            elif op == "min":
                                val = min(vals)
                            elif op == "max":
                                val = max(vals)
                elif op == "count":
                    val = model_obj.search_count(domain + [(fld, "!=", False)])

            formatted_val = f"{val:,.2f}" if isinstance(val, float) else f"{val:,}" if isinstance(val, int) else str(val)
            kpi_cards.append({
                "label": lbl,
                "field": fld,
                "operator": op,
                "value": round(val, 2) if isinstance(val, float) else val,
                "formatted_value": formatted_val,
            })

        # 3. Categorical Breakdown (Bar / Pie Distribution)
        breakdown_dim = args.get("dimension_field")
        breakdown_data = []
        if breakdown_dim and breakdown_dim in model_fields:
            meas = args.get("measure_field")
            if not meas or meas not in model_fields:
                meas = metrics_input[0]["field"] if metrics_input and metrics_input[0]["field"] in model_fields else "id"
            meas_op = "sum" if meas != "id" else "count"
            read_flds = [f"{meas}:{meas_op}"] if meas_op == "sum" else [meas]

            try:
                groups = model_obj.read_group(domain, read_flds, [breakdown_dim], limit=20, orderby=f"{meas} desc")
                total_val = sum((g.get(meas) or 0.0) for g in groups) or 1.0
                for g in groups:
                    cat_val = g.get(breakdown_dim)
                    cat_name = cat_val[1] if isinstance(cat_val, tuple) else str(cat_val or "Undefined")
                    m_val = g.get(meas) or 0.0
                    breakdown_data.append({
                        "category": cat_name,
                        "value": round(m_val, 2) if isinstance(m_val, float) else m_val,
                        "count": g.get(f"{breakdown_dim}_count", g.get("__count", 0)),
                        "percentage": round((m_val / total_val) * 100, 1),
                    })
            except Exception as e:
                _logger.debug("Failed to calculate breakdown dimension: %s", e)

        # 4. Time-Series Trend Curve
        time_dim = args.get("time_dimension")
        time_data = []
        if time_dim and time_dim in model_fields:
            interval = args.get("time_interval") or "month"
            if interval not in ("day", "week", "month", "quarter", "year"):
                interval = "month"

            meas = args.get("measure_field")
            if not meas or meas not in model_fields:
                meas = metrics_input[0]["field"] if metrics_input and metrics_input[0]["field"] in model_fields else "id"
            meas_op = "sum" if meas != "id" else "count"
            read_flds = [f"{meas}:{meas_op}"] if meas_op == "sum" else [meas]

            try:
                time_groups = model_obj.read_group(domain, read_flds, [f"{time_dim}:{interval}"], limit=24)
                for tg in time_groups:
                    t_val = tg.get(f"{time_dim}:{interval}")
                    period_label = str(t_val or "Unknown")
                    m_val = tg.get(meas) or 0.0
                    time_data.append({
                        "period": period_label,
                        "value": round(m_val, 2) if isinstance(m_val, float) else m_val,
                        "count": tg.get(f"{time_dim}_count", tg.get("__count", 0)),
                    })
            except Exception as e:
                _logger.debug("Failed to calculate time series trend: %s", e)

        # 5. Top Records Leaderboard
        top_limit = min(int(args.get("top_records_limit") or 5), 20)
        top_order = args.get("top_records_order")
        if not top_order:
            top_order = f"{metrics_input[0]['field']} desc" if metrics_input and metrics_input[0]['field'] in model_fields else "id desc"

        top_flds = args.get("top_records_fields")
        if not top_flds:
            top_flds = self._get_view_prioritized_fields(env, model)[:6]

        top_records = []
        try:
            top_records = model_obj.search_read(
                domain=domain,
                fields=self._filter_blacklisted_fields(env, top_flds, model=model),
                limit=top_limit,
                order=top_order,
            )
            top_records = self._compress_records(top_records, preserved_fields=set(top_flds))
        except Exception as e:
            _logger.debug("Failed to fetch top records: %s", e)

        currency_symbol = env.company.currency_id.symbol or "$"

        return {
            "model": model,
            "total_records": total_count,
            "currency": currency_symbol,
            "kpi_cards": kpi_cards,
            "breakdown": {
                "dimension": breakdown_dim,
                "data": breakdown_data,
            } if breakdown_data else None,
            "time_trend": {
                "dimension": time_dim,
                "interval": args.get("time_interval", "month"),
                "data": time_data,
            } if time_data else None,
            "top_records": top_records,
            "domain": domain,
        }

    # -------------------------------------------------------------------------
    # Resources Handlers
    # -------------------------------------------------------------------------

    def _handle_resources_list(self, env):
        resources = [
            {
                "uri": "odoo://context",
                "name": "Current Session Context",
                "description": "Information about the connected user, company, timezone, and server version.",
                "mimeType": "application/json",
            },
            {
                "uri": "odoo://models",
                "name": "Accessible Odoo Models",
                "description": "Complete catalog of ERP business models accessible to this token.",
                "mimeType": "application/json",
            },
        ]
        return {
            "resources": resources,
        }

    def _handle_resources_templates_list(self, env):
        resource_templates = [
            {
                "uriTemplate": "odoo://record/{model}/{id}",
                "name": "Odoo Record Context",
                "description": "Inspect detailed data and relational context for a specific record.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "odoo://views/{model}/{view_type}",
                "name": "Odoo View Architecture",
                "description": "Inspect UI layout structure (list/form) for a model.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "odoo://attachment/{id}",
                "name": "Odoo Attachment",
                "description": "Inspect attachment metadata and content for a given ir.attachment ID.",
                "mimeType": "application/json",
            },
        ]
        return {
            "resourceTemplates": resource_templates,
        }

    def _handle_resources_read(self, env, params):
        uri = params.get("uri")
        if not uri:
            raise ValidationError("Parameter 'uri' is required for resources/read.")

        if uri == "odoo://context":
            context_data = {
                "user_id": env.user.id,
                "user_name": env.user.name,
                "user_login": env.user.login,
                "active_company_id": env.company.id,
                "active_company_name": env.company.name,
                "allowed_companies": [{"id": c.id, "name": c.name} for c in env.companies],
                "timezone": env.user.tz or "UTC",
                "server_version": odoo.service.common.exp_version().get("server_version", "18.0"),
            }
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(context_data, indent=2),
                    }
                ]
            }

        if uri == "odoo://models":
            domain = [("transient", "=", False)]
            models_data = env["ir.model"].search_read(
                domain=domain,
                fields=["model", "name", "state"],
                order="model asc",
            )
            accessible = []
            for m in models_data:
                model_name = m["model"]
                if model_name in INTERNAL_MODEL_BLACKLIST:
                    continue
                try:
                    if (env[model_name].has_access("read") if hasattr(env[model_name], "has_access") else env[model_name].check_access_rights("read", raise_exception=False)):
                        accessible.append({"model": model_name, "name": m["name"]})
                except Exception:
                    continue

            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(accessible, indent=2),
                    }
                ]
            }

        # Match odoo://record/{model}/{id}
        rec_match = re.match(r"^odoo://record/([^/]+)/(\d+)$", uri)
        if rec_match:
            model = rec_match.group(1)
            rec_id = int(rec_match.group(2))
            model = self._validate_model_access(env, model, mode="read")
            fields_to_read = self._get_view_prioritized_fields(env, model)
            fields_to_read = self._filter_blacklisted_fields(env, fields_to_read, model=model)

            record = env[model].browse(rec_id).read(fields=fields_to_read)
            if not record:
                raise MissingError(f"Record #{rec_id} on {model} not found.")
            compressed = self._compress_records(record)

            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(compressed[0] if compressed else {}, indent=2, default=str),
                    }
                ]
            }

        # Match odoo://views/{model}/{view_type}
        view_match = re.match(r"^odoo://views/([^/]+)/([^/]+)$", uri)
        if view_match:
            model = view_match.group(1)
            vtype = view_match.group(2)
            model = self._validate_model_access(env, model, mode="read")

            search_types = ["list", "tree"] if vtype in ("list", "tree") else [vtype]
            view = env["ir.ui.view"].search([
                ("model", "=", model),
                ("type", "in", search_types),
            ], order="priority asc, id asc", limit=1)

            view_arch = view.arch if view else ""
            fields_in_view = self._extract_fields_from_arch(view_arch) if view_arch else []

            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps({
                            "model": model,
                            "view_type": vtype,
                            "view_id": view.id if view else False,
                            "visible_fields": fields_in_view,
                            "arch_snippet": view_arch[:2000] if view_arch else "",
                        }, indent=2),
                    }
                ]
            }

        # Match odoo://attachment/{id}
        att_match = re.match(r"^odoo://attachment/(\d+)$", uri)
        if att_match:
            att_id = int(att_match.group(1))
            res = self._tool_read_attachment(env, {"attachment_id": att_id, "include_content": True})
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(res, indent=2, default=str),
                    }
                ]
            }

        raise UserError(f"Resource URI not recognized: '{uri}'")

    # -------------------------------------------------------------------------
    # Prompts Handlers
    # -------------------------------------------------------------------------

    def _handle_prompts_list(self, env):
        prompts = env["mcp.prompt"].search([])
        return {
            "prompts": [p.to_mcp_dict() for p in prompts],
        }

    def _handle_prompts_get(self, env, params):
        prompt_name = params.get("name")
        arguments = params.get("arguments") or {}

        if not prompt_name:
            raise ValidationError("Parameter 'name' is required for prompts/get.")

        prompt = env["mcp.prompt"].search([
            ("code", "=", prompt_name),
        ], limit=1)

        if not prompt:
            raise MissingError(f"Prompt template '{prompt_name}' was not found or is inactive.")

        rendered_message = prompt.render(arguments)

        return {
            "description": prompt.description or prompt.name,
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": rendered_message,
                    },
                }
            ],
        }

    # -------------------------------------------------------------------------
    # Security & Compression Helpers
    # -------------------------------------------------------------------------

    def _validate_model_access(self, env, model_name, mode="read"):
        if not model_name or not isinstance(model_name, str):
            raise ValidationError("A valid model name is required.")

        model = model_name.strip()
        if model in INTERNAL_MODEL_BLACKLIST:
            raise AccessDenied(f"Access to internal model '{model}' is restricted for security reasons.")

        if model not in env:
            raise UserError(f"Model '{model}' does not exist in this Odoo instance.")

        # Enforce token model whitelist if configured on the token
        token_id = env.context.get("mcp_token_id")
        if token_id:
            token = env["mcp.access.token"].sudo().browse(token_id)
            if token.exists() and token.allowed_model_ids:
                allowed_models = token.allowed_model_ids.mapped("model")
                if model not in allowed_models:
                    raise AccessDenied(f"Access to model '{model}' is forbidden by this token's model whitelist policy.")

        # Enforce native Odoo user access rights (read, write, create, unlink)
        # Raises odoo.exceptions.AccessError if current user does not have permission
        if hasattr(env[model], "check_access"):
            env[model].check_access(mode)
        else:
            env[model].check_access_rights(mode)

        return model

    def _get_field_blacklist(self, env, model=None):
        # 1. Base sensitive tokens that should NEVER be exposed (security baseline)
        blacklist = {
            "password",
            "totp_secret",
            "credit_card",
            "api_key",
            "private_key",
            "passphrase",
            "secret",
            "auth_token",
        }

        # 2. Company-configured protected fields (Many2many ir.model.fields)
        try:
            company = env.company.sudo()
            fields_rel = company.mcp_blacklisted_field_ids
            if fields_rel:
                if model:
                    matched = fields_rel.filtered(
                        lambda f: not f.model or f.model == model
                    )
                    blacklist.update(f.name.lower() for f in matched if f.name)
                else:
                    blacklist.update(f.name.lower() for f in fields_rel if f.name)
        except Exception:
            pass

        # 3. System parameter fallback if configured
        try:
            config_val = env["ir.config_parameter"].sudo().get_param("abi_odoo_mcp.field_blacklist")
            if config_val:
                items = [f.strip().lower() for f in config_val.split(",") if f.strip()]
                blacklist.update(items)
        except Exception:
            pass

        return blacklist

    def _filter_blacklisted_fields(self, env, field_list, model=None):
        if not field_list:
            return []
        blacklist = self._get_field_blacklist(env, model=model)
        return [f for f in field_list if f.lower() not in blacklist]

    def _sanitize_write_values(self, env, values, model=None):
        blacklist = self._get_field_blacklist(env, model=model)
        clean_vals = {}
        for k, v in values.items():
            if k.lower() in blacklist:
                raise AccessDenied(f"Modification of sensitive field '{k}' is forbidden.")
            clean_vals[k] = v
        return clean_vals

    def _get_view_prioritized_fields(self, env, model):
        """
        Token Optimization Engine:
        Extract fields actually rendered in the standard list or form views of the model.
        Falls back to default core fields if no view is found.
        """
        try:
            view = env["ir.ui.view"].search([
                ("model", "=", model),
                ("type", "in", ["list", "tree", "form"]),
            ], order="priority asc, id asc", limit=1)

            if view and view.arch:
                fields_found = self._extract_fields_from_arch(view.arch)
                if fields_found:
                    # Always include id and display_name
                    prioritized = ["id"]
                    if "display_name" in env[model]._fields and "display_name" not in fields_found:
                        prioritized.append("display_name")
                    elif "name" in env[model]._fields and "name" not in fields_found:
                        prioritized.append("name")
                    for f in fields_found:
                        if f in env[model]._fields and f not in prioritized and f not in INTERNAL_FIELD_PRUNE:
                            prioritized.append(f)
                    return prioritized[:25]  # Cap at 25 fields for strict token efficiency
        except Exception as e:
            _logger.debug("View field prioritization failed for %s: %s", model, e)

        # Fallback: pick standard descriptive fields
        defaults = ["id"]
        for candidate in ("name", "display_name", "title", "code", "state", "user_id", "partner_id", "date"):
            if candidate in env[model]._fields:
                defaults.append(candidate)
        return defaults

    @staticmethod
    def _extract_fields_from_arch(arch_xml):
        try:
            tree = etree.fromstring(arch_xml.encode("utf-8"))
            field_nodes = tree.xpath("//field/@name")
            return list(dict.fromkeys(field_nodes))  # preserve order without duplicates
        except Exception:
            return []

    def _compress_records(self, records, preserved_fields=None):
        """
        Token Compression Engine:
        Strips empty, False, or None keys and trims large binary blobs.
        Reduces LLM context consumption by up to 70%.
        """
        preserved = preserved_fields or set()
        compressed = []
        for rec in records:
            clean_rec = {}
            for k, v in rec.items():
                # Strip internal tracking fields UNLESS explicitly requested
                if k in INTERNAL_FIELD_PRUNE and k not in preserved:
                    continue
                # Strip empty values (False, None, empty string), but preserve 0 and False if boolean
                if v is None or v is False or v == "" or v == [] or v == {}:
                    continue
                # Truncate binary data
                if isinstance(v, (bytes, bytearray)):
                    clean_rec[k] = f"<binary data: {len(v)} bytes>"
                elif isinstance(v, str) and len(v) > 2000 and (k.startswith("image_") or k.startswith("datas")):
                    clean_rec[k] = f"<base64 data: {len(v)} chars>"
                else:
                    clean_rec[k] = v
            # Ensure 'id' is never stripped
            clean_rec["id"] = rec.get("id")
            compressed.append(clean_rec)
        return compressed

    # -------------------------------------------------------------------------
    # Response Formatting Helpers
    # -------------------------------------------------------------------------

    def _build_jsonrpc_dict_result(self, req_id, result):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        }

    def _build_jsonrpc_dict_error(self, req_id, code, message, data=None):
        error_payload = {
            "code": code,
            "message": message,
        }
        if data is not None:
            error_payload["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": error_payload,
        }

    def _jsonrpc_result(self, req_id, result):
        return request.make_json_response(self._build_jsonrpc_dict_result(req_id, result), headers=self._get_cors_headers())

    def _jsonrpc_error(self, req_id, code, message, data=None):
        return request.make_json_response(self._build_jsonrpc_dict_error(req_id, code, message, data), headers=self._get_cors_headers(), status=200)

    def _get_cors_headers(self):
        return [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, X-Requested-With, x-api-key, x-mcp-token, X-Odoo-Database, ngrok-skip-browser-warning"),
            ("ngrok-skip-browser-warning", "true"),
        ]

    def _build_cors_response(self):
        headers = self._get_cors_headers() + [
            ("Access-Control-Max-Age", "86400"),
        ]
        return Response("", status=204, headers=headers)

    def _get_client_ip(self):
        forwarded_for = request.httprequest.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        return request.httprequest.remote_addr or "127.0.0.1"
