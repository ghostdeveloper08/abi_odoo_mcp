# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

from collections import deque
import hashlib
import ipaddress
import logging
import secrets
from threading import Lock
import time
from odoo import api, fields, models, _
from odoo.exceptions import AccessDenied, UserError, ValidationError

_logger = logging.getLogger(__name__)

TOKEN_PREFIX = "mcp_live_"

# In-memory sliding window cache: token_id -> deque([monotonic_timestamp, ...])
_RATE_LIMIT_STORE = {}
_RATE_LIMIT_LOCK = Lock()


class McpAccessToken(models.Model):
    _name = "mcp.access.token"
    _description = "MCP Access Token"
    _order = "create_date desc, id desc"

    name = fields.Char(
        string="Token Name / Description",
        required=True,
        help="A human-readable label identifying this token (e.g., 'Claude Desktop - Sales Assistant').",
    )
    user_id = fields.Many2one(
        "res.users",
        string="Owner User",
        required=True,
        default=lambda self: self.env.user,
        index=True,
        ondelete="cascade",
        help="The Odoo user whose access rights, record rules, and company scoping are enforced.",
    )
    token = fields.Char(
        string="Access Token",
        readonly=True,
        copy=False,
        index=True,
        help="The access token key. Keep this secret.",
    )
    server_url = fields.Char(
        string="Connection URL",
        compute="_compute_server_url",
        help="The full endpoint URL including authentication token for easy sharing.",
    )
    token_prefix = fields.Char(
        string="Key Prefix",
        readonly=True,
        index=True,
        help="The masked prefix of the token for identification purposes.",
    )
    token_hash = fields.Char(
        string="Token Hash (SHA-256)",
        readonly=True,
        index=True,
        copy=False,
        help="Cryptographically secure SHA-256 hash of the access token.",
    )
    raw_token = fields.Char(
        string="Raw Token",
        compute="_compute_raw_token",
        help="Access token string for client configuration.",
    )
    profile_preset = fields.Selection(
        [
            ("custom", "Custom Configuration"),
            ("sales", "🛒 Sales & CRM (Leads, Quotations, Orders, Customers, Products)"),
            ("sales_ro", "👁️ Sales & CRM (Read-Only)"),
            ("inventory", "📦 Inventory & Logistics (Transfers, Stock Moves, Warehouses, Products)"),
            ("accounting_ro", "📊 Financial Auditor (Invoices, Bills, Payments, Journals - Read-Only)"),
            ("executive", "👔 Executive All-Access (All Business Models + Analytics)"),
        ],
        string="Role / Preset",
        default="custom",
        help="Quickly populate recommended permissions and models tailored for standard business roles.",
    )
    active = fields.Boolean(
        string="Active",
        default=True,
        help="Instant revocation switch. If unchecked, all requests with this token are rejected.",
    )
    is_readonly = fields.Boolean(
        string="Read-Only Mode",
        default=False,
        help="If enabled, this token is strictly forbidden from creating, updating, or deleting records, "
             "or executing state-altering business methods.",
    )
    expires_at = fields.Datetime(
        string="Expiration Date",
        help="Optional datetime after which the token is automatically invalidated.",
    )
    allowed_ips = fields.Text(
        string="Allowed IP Ranges (CIDR)",
        help="Optional comma or newline-separated list of IPv4/IPv6 addresses or CIDR blocks "
             "(e.g., '127.0.0.1, 192.168.1.0/24, 10.0.0.1'). If empty, requests from any IP are accepted.",
    )
    allowed_model_ids = fields.Many2many(
        "ir.model",
        "mcp_token_model_rel",
        "token_id",
        "model_id",
        string="Model Whitelist",
        help="Optional restriction: if selected, only these specific models can be queried or modified via this token. "
             "Leave empty to permit all models accessible to the user's native permissions.",
    )
    rate_limit_per_minute = fields.Integer(
        string="Rate Limit (req/min)",
        default=120,
        help="Maximum number of requests permitted per minute for this token. Set to 0 for unlimited.",
    )
    last_used_at = fields.Datetime(
        string="Last Used",
        readonly=True,
        help="Timestamp of the most recent request authenticated with this token.",
    )
    total_calls = fields.Integer(
        string="Total Invocations",
        default=0,
        readonly=True,
        help="Total number of MCP requests handled by this token.",
    )
    audit_log_ids = fields.One2many(
        "mcp.audit.log",
        "token_id",
        string="Audit Logs",
        readonly=True,
    )
    audit_log_count = fields.Integer(
        string="Logs Count",
        compute="_compute_audit_log_count",
    )

    # In-memory transient storage for new raw tokens
    _transient_tokens = {}

    def _compute_raw_token(self):
        for record in self:
            record.raw_token = record.token or self._transient_tokens.get(record.id) or False

    @api.model
    def _format_mcp_base_url(self, base_url=None):
        """Format base URL for MCP clients.
        If the base URL starts with http:// but is accessed through a tunnel (like ngrok,
        localtunnel, cloudflare) or a public domain, or has X-Forwarded-Proto=https,
        automatically upgrade to https:// so the user can paste it directly into
        ChatGPT, Claude, etc. without manual editing.
        """
        if not base_url:
            icp = self.env["ir.config_parameter"].sudo()
            base_url = icp.get_str("web.base.url", "http://localhost:8069") if hasattr(icp, "get_str") else icp.get_param("web.base.url", "http://localhost:8069")
        base_url = (base_url or "").rstrip("/")

        proto = None
        try:
            from odoo.http import request
            if request and hasattr(request, "httprequest"):
                proto = request.httprequest.headers.get("X-Forwarded-Proto")
        except Exception:
            proto = None

        if base_url.startswith("http://"):
            is_local = any(h in base_url.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0"))
            is_tunnel = any(t in base_url.lower() for t in ("ngrok", "localtunnel", "trycloudflare", "loca.lt"))
            if proto == "https" or is_tunnel or not is_local:
                base_url = "https://" + base_url[len("http://"):]

        return base_url

    def _compute_server_url(self):
        for record in self:
            base_url = record._format_mcp_base_url()
            tok = record.token or record.raw_token or ""
            if tok:
                record.server_url = f"{base_url}/mcp?token={tok}"
            else:
                record.server_url = f"{base_url}/mcp"

    def _compute_audit_log_count(self):
        for record in self:
            record.audit_log_count = len(record.audit_log_ids)

    @api.constrains("allowed_ips")
    def _check_allowed_ips(self):
        for record in self:
            if not record.allowed_ips:
                continue
            lines = [ip.strip() for ip in record.allowed_ips.replace(",", "\n").splitlines() if ip.strip()]
            for item in lines:
                try:
                    if "/" in item:
                        ipaddress.ip_network(item, strict=False)
                    else:
                        ipaddress.ip_address(item)
                except ValueError as err:
                    raise ValidationError(_("Invalid IP or CIDR block '%(item)s': %(err)s", item=item, err=err))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("token"):
                raw_secret = secrets.token_urlsafe(32)
                full_token = f"{TOKEN_PREFIX}{raw_secret}"
                vals["token"] = full_token
            else:
                full_token = vals["token"]

            sha256_hash = hashlib.sha256(full_token.encode("utf-8")).hexdigest()
            prefix = f"{full_token[:13]}...{full_token[-4:]}"

            vals["token_hash"] = sha256_hash
            vals["token_prefix"] = prefix

        records = super().create(vals_list)
        return records

    @api.onchange("profile_preset")
    def _onchange_profile_preset(self):
        """Automatically populate sensible business models and flags based on the chosen role."""
        if not self.profile_preset or self.profile_preset == "custom":
            return

        preset_models_map = {
            "sales": ["crm.lead", "sale.order", "sale.order.line", "res.partner", "product.template", "product.product"],
            "sales_ro": ["crm.lead", "sale.order", "sale.order.line", "res.partner", "product.template", "product.product"],
            "inventory": ["stock.picking", "stock.move", "stock.quant", "stock.warehouse", "stock.location", "product.template", "product.product"],
            "accounting_ro": ["account.move", "account.move.line", "account.payment", "account.journal", "res.partner"],
            "executive": [],  # Empty means all models accessible to the user
        }

        preset_titles = {
            "sales": "AI Assistant - Sales & CRM",
            "sales_ro": "AI Viewer - Sales (Read-Only)",
            "inventory": "AI Assistant - Inventory & Logistics",
            "accounting_ro": "AI Auditor - Accounting & Finance",
            "executive": "AI Executive - Full Business Operations",
        }

        # Set friendly default name if current name is empty or default
        if not self.name or self.name.startswith("AI Assistant") or self.name.startswith("AI Viewer") or self.name.startswith("AI Auditor") or self.name.startswith("AI Executive"):
            self.name = preset_titles.get(self.profile_preset, self.name)

        # Set Read-Only flag
        if self.profile_preset in ("sales_ro", "accounting_ro"):
            self.is_readonly = True
        else:
            self.is_readonly = False

        # Set rate limit
        if self.profile_preset == "executive":
            self.rate_limit_per_minute = 240
        else:
            self.rate_limit_per_minute = 120

        # Resolve models in DB
        target_model_names = preset_models_map.get(self.profile_preset, [])
        if target_model_names:
            found_models = self.env["ir.model"].search([("model", "in", target_model_names)])
            self.allowed_model_ids = [(6, 0, found_models.ids)]
        else:
            self.allowed_model_ids = [(5, 0, 0)]

    def action_emergency_revoke_all(self):
        """Emergency killswitch: revokes all active MCP tokens immediately."""
        active_tokens = self.search([("active", "=", True)])
        count = len(active_tokens)
        active_tokens.write({"active": False})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Emergency Killswitch Activated"),
                "message": _("Successfully revoked %d active AI access token(s). All AI integrations are now blocked.", count),
                "type": "danger",
                "sticky": True,
            },
        }

    def action_revoke(self):
        self.ensure_one()
        self.write({"active": False})
        _logger.warning("MCP Access Token '%s' (ID %s) has been revoked by user %s.", self.name, self.id, self.env.user.login)
        return True

    def action_activate(self):
        self.ensure_one()
        self.write({"active": True})
        return True

    def action_open_audit_logs(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("abi_odoo_mcp.action_mcp_audit_log")
        action["domain"] = [("token_id", "=", self.id)]
        return action

    def action_open_config_wizard(self):
        self.ensure_one()
        base_url = self._format_mcp_base_url()
        token_str = self.token or self.raw_token or self.token_prefix or ""
        server_url = self.server_url or (f"{base_url}/mcp?token={token_str}" if token_str else f"{base_url}/mcp")
        wizard = self.env["mcp.config.snippet.wizard"].create({
            "token_id": self.id,
            "token": token_str,
            "server_url": server_url,
            "base_url": base_url,
        })
        return {
            "name": _("Share Connection"),
            "type": "ir.actions.act_window",
            "res_model": "mcp.config.snippet.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_token_id": self.id,
            },
        }

    @classmethod
    def validate_token_record(cls, token, client_ip=""):
        """
        Validates an existing token record: checks active status, owner status,
        expiration, and IP restrictions, then records usage statistics.
        """
        if not token or not token.active:
            _logger.warning("MCP Auth Failed: Revoked or inactive token '%s' used from IP %s", token.name if token else "None", client_ip)
            raise AccessDenied(_("This MCP access token has been revoked or deactivated."))

        if not token.user_id or not token.user_id.active:
            _logger.warning("MCP Auth Failed: Token owner '%s' is inactive", token.user_id.login if token.user_id else "None")
            raise AccessDenied(_("The user account associated with this token is inactive."))

        now = fields.Datetime.now()
        if token.expires_at and now > token.expires_at:
            _logger.warning("MCP Auth Failed: Expired token '%s' used from IP %s", token.name, client_ip)
            raise AccessDenied(_("This MCP access token expired on %s.", token.expires_at))

        # Check IP range restrictions if configured
        if token.allowed_ips and client_ip:
            if not cls._is_ip_allowed(token.allowed_ips, client_ip):
                _logger.warning("MCP Auth Failed: IP %s not in allowed ranges for token '%s'", client_ip, token.name)
                raise AccessDenied(_("Requests from IP '%s' are not authorized for this token.", client_ip))

        # Update last_used_at and total_calls
        try:
            token.write({
                "last_used_at": now,
                "total_calls": token.total_calls + 1,
            })
        except Exception as e:
            _logger.debug("Failed to update token usage stats: %s", e)

        return token

    def check_rate_limit(self):
        """
        Enforce rate_limit_per_minute across a rolling 60-second window.
        Returns:
            (allowed: bool, current_count: int, retry_after: int)
        """
        self.ensure_one()
        limit = self.rate_limit_per_minute
        if not limit or limit <= 0:
            return True, 0, 0

        now = time.time()
        window = 60.0

        with _RATE_LIMIT_LOCK:
            timestamps = _RATE_LIMIT_STORE.setdefault(self.id, deque())
            # Evict timestamps older than 60 seconds
            while timestamps and (now - timestamps[0]) > window:
                timestamps.popleft()

            in_memory_count = len(timestamps)

        # Also check committed audit logs in database within the last 60 seconds
        # for multi-worker and cross-restart consistency
        db_count = 0
        try:
            self.env.cr.execute("""
                SELECT count(*) FROM mcp_audit_log 
                WHERE token_id = %s 
                  AND status != 'blocked_rate_limit'
                  AND create_date >= (NOW() AT TIME ZONE 'UTC' - INTERVAL '60 seconds')
            """, (self.id,))
            row = self.env.cr.fetchone()
            db_count = row[0] if row else 0
        except Exception:
            pass

        effective_count = max(in_memory_count, db_count)

        if effective_count >= limit:
            retry_after = 60
            with _RATE_LIMIT_LOCK:
                if timestamps:
                    retry_after = max(1, int(window - (now - timestamps[0])))
            return False, effective_count, retry_after

        # Record this request timestamp in the sliding window
        with _RATE_LIMIT_LOCK:
            timestamps.append(now)

        return True, effective_count + 1, 0

    @classmethod
    def reset_rate_limit_cache(cls, token_id=None):
        """Reset rate limit tracking store (useful for testing and admin resets)."""
        with _RATE_LIMIT_LOCK:
            if token_id:
                _RATE_LIMIT_STORE.pop(token_id, None)
            else:
                _RATE_LIMIT_STORE.clear()

    @classmethod
    def verify_token(cls, env, raw_token, client_ip=""):
        """
        Verify incoming Bearer token against stored SHA-256 hashes.
        Enforces:
          1. Exact SHA-256 hash match
          2. Active status (active)
          3. Owner user active status
          4. Expiration date (expires_at)
          5. IP / CIDR whitelist restrictions
        Returns the matching mcp.access.token record (sudo-elevated for token metadata).
        """
        if not raw_token or not isinstance(raw_token, str):
            raise AccessDenied(_("Missing or invalid authentication token format."))

        clean_token = raw_token.strip()
        sha256_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()

        # Lookup token hash or raw token securely using sudo for the authentication handshake
        token = env["mcp.access.token"].sudo().with_context(active_test=False).search([
            "|",
            ("token_hash", "=", sha256_hash),
            ("token", "=", clean_token),
        ], limit=1)

        if not token:
            _logger.warning("MCP Auth Failed: Unrecognized token from IP %s", client_ip)
            raise AccessDenied(_("Invalid or unrecognized MCP access token."))

        return cls.validate_token_record(token, client_ip)

    @staticmethod
    def _is_ip_allowed(allowed_ips_str, client_ip_str):
        if not client_ip_str or client_ip_str in ("127.0.0.1", "::1", "localhost", "n/a"):
            # If localhost or not resolvable, permit if 127.0.0.1 or localhost is specified or testing
            pass
        try:
            client_obj = ipaddress.ip_address(client_ip_str.strip())
        except ValueError:
            return False

        lines = [ip.strip() for ip in allowed_ips_str.replace(",", "\n").splitlines() if ip.strip()]
        for pattern in lines:
            try:
                if "/" in pattern:
                    net = ipaddress.ip_network(pattern, strict=False)
                    if client_obj in net:
                        return True
                else:
                    addr = ipaddress.ip_address(pattern)
                    if client_obj == addr:
                        return True
            except ValueError:
                continue
        return False
