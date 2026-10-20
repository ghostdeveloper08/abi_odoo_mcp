# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

{
    'name': 'ABI Odoo MCP Server',
    'version': '20.0.1.0.0',
    'category': 'Productivity/Artificial Intelligence',
    'summary': 'Connect ChatGPT, Cursor, and AI agents with 30 tools.',
    'description': """
Odoo MCP Server — AI Assistant Integration for Odoo
===================================================
Connect your favorite AI assistants (ChatGPT, Claude, Cursor, Grok, Windsurf, and Cline)
directly to your Odoo ERP using the open standard Model Context Protocol (MCP).
Ask questions in plain English, preview live dashboards inside your chat side panel,
audit financial balances, and automate everyday workflows — with zero technical knowledge required.

Key Business Capabilities:
--------------------------
* **Executive Dashboards & Live In-Chat Previews**:
  Preview interactive HTML dashboards in your chat side panel before saving them directly
  into native Odoo Spreadsheet Dashboards with live formulas.

* **Financial Audits & Collections**:
  Perform 30-60-90 day aged receivables audits, identify top debtors, and draft follow-up
  reminders directly in record chatter.

* **Sales Pipeline & Deal Hunting**:
  Audit CRM opportunities across stages, calculate expected pipeline value, and flag
  high-value deals stalled without scheduled activities.

* **Inventory Shortages & Smart Replenishment**:
  Check on-hand vs forecasted stock, identify uncovered customer orders, and match shortages
  against pending vendor purchase orders.

* **Team Collaboration & Activities**:
  Post notes to record chatter, read message history, schedule tasks and calls, and inspect
  document attachments safely.

* **HR Team Availability & Time Off**:
  Summarize employee headcount and upcoming approved leaves without exposing private payroll data.

* **Data Quality Doctor**:
  Scan customer databases for duplicate contacts, missing email addresses, and unlinked records.

* **Zero-Trust Security & Privacy**:
  The AI operates strictly under the logged-in user's standard permissions. Features include
  1-click Read-Only mode, sliding-window rate limiting, bulk edit safeguards, and full audit logging.

* **Zero External Dependencies**:
  Runs 100% natively inside your Odoo server. No Docker containers, extra servers, or external Python packages required.

Keywords / Search Terms:
------------------------
odoo mcp, mcp server odoo, odoo ai, odoo chatgpt, odoo claude, claude desktop, cursor odoo,
grok odoo, model context protocol, ai assistant odoo, artificial intelligence, spreadsheet dashboard ai,
odoo llm, ai erp integration, odoo automation, business intelligence ai, windsurf mcp, cline odoo
    """,
    'author': 'Ghost Developer',
    'website': 'https://github.com/ghostdeveloper08/abi_odoo_mcp',
    'maintainer': 'Ghost Developer',
    'support': 'ghostdeveloper08@gmail.com',
    'license': 'LGPL-3',
    'price': 0.0,
    'currency': 'EUR',
    'depends': [
        'base',
        'web',
        'mail',
    ],
    'data': [
        'security/mcp_security.xml',
        'security/ir.access.csv',
        'data/mcp_method_whitelist_data.xml',
        'data/mcp_prompt_data.xml',
        'views/mcp_access_token_views.xml',
        'views/mcp_audit_log_views.xml',
        'views/mcp_method_whitelist_views.xml',
        'views/mcp_prompt_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_users_views.xml',
        'wizards/mcp_config_snippet_wizard_views.xml',
        'views/mcp_dashboard_views.xml',
        'views/mcp_menus.xml',
    ],
    'images': [
        'static/description/banner.gif',
        'static/description/banner.png',
        'static/description/access_token.png',
        'static/description/audit_log.png',
        'static/description/form_view.png',
    ],
    'demo': [],
    'installable': True,
    'application': True,
    'auto_install': False,
}
