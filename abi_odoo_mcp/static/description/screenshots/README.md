# 📸 Step-by-Step Connection Screenshots

This folder contains screenshot guides for connecting various AI platforms to the **Odoo MCP Server**.

---

## 🤖 1. ChatGPT Setup Screenshots (`chatgpt/`)

Step-by-step screenshots in `static/description/screenshots/chatgpt/`:

| Filename | Description | What It Shows |
| :--- | :--- | :--- |
| `step_1.png` | **Odoo Access Token & Server URL** | Odoo screen showing **Share Connection** modal with the complete Server URL (`.../mcp?token=...`). |
| `step_2.png` | **ChatGPT Plugins > Add custom MCP server** | ChatGPT sidebar showing **Plugins** and top-right **Add > Add custom MCP server**. |
| `step_3.png` | **Server URL Configuration & Warning** | ChatGPT modal with Name `odoo-mcp`, Server URL pasted, and "Create as a plugin" button. |
| `step_4.png` | **Live Chat Verification** | ChatGPT conversation showing `@odoo-mcp` tag in prompt box to invoke Odoo ERP tools. |

> ⚠️ **CRITICAL NOTE FOR STEP 3:**
> - **Must use `https://` (NOT `http://`)**: ChatGPT strictly requires an SSL HTTPS URL.
> - **Live Domain or ngrok required (NOT `localhost`)**: ChatGPT cannot reach `localhost`. Use a public Odoo domain or an ngrok tunnel (e.g., `ngrok http 8069`).

---

## 🟠 2. Claude Setup Screenshots (`claude/`)

Step-by-step screenshots in `static/description/screenshots/claude/`:

| Filename | Description | What It Shows |
| :--- | :--- | :--- |
| `step_1.png` | **Settings > Connectors > Add custom connector** | Claude settings window with **Connectors** open and **+ Add > Add custom connector** clicked. |
| `step_2.png` | **Add Custom Connector Modal** | Modal with Name `odoo-mcp`, Server URL pasted (`https://...`), and **Continue** button. |
| `step_3.png` | **Chat Input Connectors Toggle** | Claude chat box with `+` menu open, showing `odoo-mcp` toggled **ON** and live ERP data in chat. |

> ⚠️ **CRITICAL NOTE FOR STEP 2:**
> - **Must use `https://` (NOT `http://`)**: Claude strictly requires an SSL HTTPS URL.
> - **Live Domain or ngrok required (NOT `localhost`)**: Claude runs in the cloud and cannot reach `localhost`. Use a public Odoo domain or an ngrok tunnel (e.g., `ngrok http 8069`).

---

## 🔵 3. Cursor IDE Setup Screenshots (`cursor/`)

Step-by-step screenshots in `static/description/screenshots/cursor/`:

| Filename | Description | What It Shows |
| :--- | :--- | :--- |
| *(Step 1)* | **Open Tools & MCP** | Press `Ctrl + Shift + P` (or `Cmd + Shift + P`) and select `Cursor: Tools & MCP`. |
| `step_2.png` | **+ New MCP Server > Workspace** | Cursor Customize window showing `+ New MCP Server` and selecting workspace. |
| `step_3.png` | **.cursor/mcp.json Configuration** | Editor showing `.cursor/mcp.json` with HTTPS `url` highlighted. |
| `step_4.png` | **Enable Source & Reload** | "Configure odoo-mcp" dialog toggling `.cursor/mcp.json` source ON and clicking Done. |
| `step_5.png` | **Verified Status & Live Chat** | Connected status (32 tools enabled) and Cursor chat answering live sale order count. |

---

## 🪐 4. Antigravity IDE Setup Screenshots (`antigravity/`)

Step-by-step screenshots in `static/description/screenshots/antigravity/`:

| Filename | Description | What It Shows |
| :--- | :--- | :--- |
| *(Step 1)* | **Open Manage MCP Servers** | Press `Ctrl + Shift + P` (or `Cmd + Shift + P`) and select `Manage MCP servers`. |
| `step_2.png` | **View Raw Config** | Manage MCP servers panel showing **View raw config** button clicked. |
| `step_3.png` | **mcp_config.json Configuration** | Editor showing `mcp_config.json` with `odoo-mcp` HTTPS URL and `"disabled": false`. |
| `step_4.png` | **Refresh & Tool Discovery** | Clicking **Refresh** to load all 32 Odoo tools ready and enabled. |
| `step_5.png` | **Live Chat Verification** | Antigravity chat returning live count of 48 sale orders directly from Odoo. |

---

## ⚫ 5. Grok (xAI) Setup Screenshots (`grok/`)

Step-by-step screenshots in `static/description/screenshots/grok/`:

| Filename | Description | What It Shows |
| :--- | :--- | :--- |
| `step_1.png` | **Plugins > New Connector > Custom** | Grok sidebar showing **Plugins**, **New Connector** clicked, and **+ Custom (Add your own custom connector)** selected. |
| `step_2.png` | **Add Custom Connector Modal** | Modal with Name `odoo-mcp`, HTTPS Server URL pasted, and **Add Connector** clicked. |
| `step_3.png` | **Chat Input Connectors Toggle** | Grok chat prompt box with `+` menu open, showing **Connectors > odoo-mcp** toggled **ON**. |
| `step_4.png` | **Live Chat Verification** | Grok conversation returning live count of 48 sale orders directly from Odoo. |

> ⚠️ **CRITICAL NOTE FOR STEP 2:**
> - **Must use `https://` (NOT `http://`)**: Grok strictly requires an SSL HTTPS URL.
> - **Live Domain or ngrok required (NOT `localhost`)**: Grok runs in the cloud and cannot reach `localhost`. Use a public Odoo domain or an ngrok tunnel (e.g., `ngrok http 8069`).

---

> 💡 **Tip:** PNG format with 16:9 or standard window screenshot aspect ratio works best. Once you save the images into these folders, they will be automatically embedded into `index.html` and `README.md`.


