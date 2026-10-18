# 🎬 Odoo MCP Video Demo Guide & Example Prompts

This guide outlines the 3 chosen prompts for recording live **Claude Code** demonstration sessions. Each prompt is mapped to an attractive, generic tab category designed to captivate prospective users and showcase the real power of Odoo MCP.

---

## 📑 Overview of the 3 Demo Tabs

| Tab # | Generic & Attractive Title | Focus Area | Recommended Prompt |
| :--- | :--- | :--- | :--- |
| **Tab 1** | **Multi-Model Intelligence** | Cross-Module Entity Linking & Data Audit | **Prompt 1 from `test.md`** *(Contacts + CRM + Sales Quotations)* |
| **Tab 2** | **Deep Operational Workflows** | Supply Chain, Stock Shortage & Replenishment | **Prompt 2 from `test.md`** *(Inventory + Sales Demand + POs + Bills)* |
| **Tab 3** | **Interactive Executive Dashboards** | Visual Analytics, KPI Scorecards & Odoo Spreadsheets | **Executive Dashboard Prompt** *(Sales Performance Dashboard with in-chat preview)* |

---

## 🔗 Tab 1: Multi-Model Intelligence
*Generic Title*: **Multi-Model Intelligence (Cross-Module Analysis)**  
*Subtitle*: Query and correlate live Contacts, CRM Deals, and Sales Quotations in a single conversational turn.

### 🎯 Why This Prompt?
Traditional ERP APIs require multiple endpoints and manual joins. This demo shows Claude Code querying `res.partner`, `crm.lead`, and `sale.order` concurrently, discovering relationship links, identifying missing data (e.g. leads without assigned customers), and formatting an executive summary without changing any records.

### 📋 Prompt to Copy & Paste into Claude Code:
```text
I want to test my Odoo MCP integration.

Search for the 5 most recently created contacts and show their names, email addresses, phone numbers, and company names.
List the 5 most recently updated CRM opportunities, including their opportunity name, customer, sales team or salesperson, expected revenue, and stage.
Find quotations in draft or sent status and show their reference, customer, total amount, and status.
Explain which Odoo records are linked to each other and identify any missing information.

Use live data from my Odoo instance. Do not create, modify, or delete anything. If a requested field is unavailable, tell me instead of guessing.
```

### 🎬 What to Record in Claude Code:
1. Open terminal and run `claude` (Claude Code).
2. Paste the prompt above.
3. Observe Claude Code invoking `odoo_search_read` on `res.partner`, `crm.lead`, and `sale.order`.
4. Highlight the output: clean Markdown tables, revenue sum ($128,000 across CRM deals), quotation totals ($7,210), and intelligent relationship observations (e.g., missing customer links).
5. Stop recording and save as `video_tab1.mp4`.

---

## ⚡ Tab 2: Deep Operational Workflows
*Generic Title*: **Deep Operational Workflows (Supply Chain & Replenishment)**  
*Subtitle*: Automate complex operational audits linking low stock, pending sales orders, supplier purchase orders, and vendor bills.

### 🎯 Why This Prompt?
This is a high-value real-world operational scenario. Rather than an operator clicking through 5 different Odoo apps (Inventory, Sales, Purchase, Accounting), Claude Code scans 22 zero-stock products, finds unfulfilled sales demand, checks draft/confirmed purchase orders, inspects unpaid vendor bills, and produces actionable replenishment priorities.

### 📋 Prompt to Copy & Paste into Claude Code:
```text
Act as an Odoo operations analyst. Use my connected Odoo instance to perform the following checks:

Find products with low or zero available stock and show their names, internal references, on-hand quantities, and forecasted quantities.
Identify confirmed sales orders containing those products and report the order number, customer, product, and quantity ordered.
Check whether there are purchase orders for those products and report their suppliers, order status, and expected delivery dates when available.
Identify customer invoices and vendor bills related to the relevant sales and purchase transactions, including their payment status and total amounts.
Summarize the products that may need replenishment and explain the evidence behind each recommendation.

Use actual Odoo records and clearly distinguish confirmed facts from recommendations. Do not confirm orders, validate receipts, create invoices, or modify inventory. If a relationship cannot be established from the available records, say so.
```

### 🎬 What to Record in Claude Code:
1. Paste the prompt into Claude Code.
2. Watch Claude execute multi-step tool calls: checking `stock.quant`, `sale.order.line`, `purchase.order.line`, and `account.move.line`.
3. Highlight the structured analysis:
   - 22 zero-stock products identified.
   - 7 confirmed sales lines needing delivery.
   - 9 purchase order references cross-checked.
   - Priority 1 recommendation: 5-unit shortage gap for *Corner Desk Right Sit (`E-COM06`)*.
4. Stop recording and save as `video_tab2.mp4`.

---

## 📊 Tab 3: Interactive Executive Dashboards
*Generic Title*: **Interactive Executive Dashboards & Analytics**  
*Subtitle*: Generate professional Odoo spreadsheets with real-time KPI scorecards, SVG trend charts, and side-screen HTML previews.

### 🎯 Why This Prompt?
Visual dashboards instantly wow viewers. With Odoo MCP, the AI doesn't just return plain text—it creates an actual Odoo 18 Spreadsheet Dashboard inside Odoo AND renders an interactive, responsive HTML preview with modern glassmorphism cards and charts for immediate review.

### 📋 Prompt to Copy & Paste into Claude Code (Recommended):
```text
Act as an executive BI analyst. Using my connected Odoo instance, create an Executive Sales Performance Dashboard for Q3.
Show total revenue, average order value, monthly revenue trends, top 5 customers by revenue, and an order status breakdown (draft, sent, confirmed).
Generate an interactive preview first so I can inspect the KPIs and charts directly before creating it in Odoo.
```

*(Alternative prompt from `test.md` if you prefer a text-based 7-module Business Health Report):*
```text
Generate an Odoo business health report using the live data accessible through my MCP integration.

Organize the report into these sections:
CRM: Count opportunities by stage and identify the highest-value open opportunities.
Sales: Calculate total sales order value for the current month and compare it with the previous month.
Accounting: Summarize posted customer invoices, outstanding receivables, and overdue invoices.
Projects: List active projects, their task counts, and overdue tasks.
HR and Holidays: Summarize employee headcount and approved upcoming leave.
Calendar: Summarize upcoming meetings over the next 7 days.
Point of Sale: Report POS order totals for the current month.

Finish with 5 actionable business observations based strictly on the retrieved data.
```

### 🎬 What to Record in Claude Code:
1. Paste the dashboard prompt into Claude Code.
2. Watch Claude call `odoo_create_dashboard` (or `odoo_get_dashboard_data`).
3. Claude outputs the KPI metrics, generates `dashboard_preview.html`, and provides the live preview path.
4. Briefly open/show the generated dashboard_preview.html in the browser side-by-side to showcase the interactive charts, scorecards, and data tables.
5. Stop recording and save as `video_tab3.mp4`.

---

## 📁 Where to Save the Recorded Videos

Save your recordings in:
`abi_odoo_mcp/static/description/videos/`

Recommended filenames:
- `video_tab1.mp4` (Multi-Model Intelligence)
- `video_tab2.mp4` (Deep Operational Workflows)
- `video_tab3.mp4` (Interactive Executive Dashboards)

*(WebM or MP4 format with H.264 video codec at 1080p or 720p works best across all browsers and Odoo Apps store).*
