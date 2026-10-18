#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Part of ABI Odoo MCP Server. See LICENSE file for full copyright and licensing details.
# Copyright (C) 2026 Ghost Developer

"""
Odoo MCP Stdio Bridge
=====================
Zero-dependency Python bridge that allows standard stdio-based MCP clients
(such as Claude Desktop, Zed, and CLI harnesses) to communicate seamlessly
with an Odoo instance hosting the abi_odoo_mcp module.

Usage:
    python3 odoo_mcp_bridge.py --url http://localhost:8069 --token mcp_live_...
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def log_debug(msg):
    sys.stderr.write(f"[odoo-mcp-bridge] {msg}\n")
    sys.stderr.flush()


def forward_request(endpoint_url, token, payload_bytes, db=None):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Odoo-MCP-Stdio-Bridge/1.0",
    }
    if db:
        headers["X-Odoo-Database"] = db
    req = urllib.request.Request(
        endpoint_url,
        data=payload_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            if status == 204:
                return None
            return resp.read()
    except urllib.error.HTTPError as e:
        error_body = e.read()
        log_debug(f"HTTP error {e.code}: {error_body.decode('utf-8', errors='replace')}")
        return error_body
    except Exception as e:
        log_debug(f"Transport network error: {e}")
        return json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32603, "message": f"Bridge connection error: {str(e)}"},
        }).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description="Stdio to HTTP Bridge for Odoo MCP Server")
    parser.add_argument("--url", required=True, help="Base URL of Odoo instance (e.g. http://localhost:8069)")
    parser.add_argument("--token", required=True, help="MCP Access Token (mcp_live_...)")
    parser.add_argument("--db", required=False, default=None, help="Optional Odoo Database Name for multi-db instances")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    endpoint = f"{base_url}/mcp/rpc"
    token = args.token.strip()
    db_name = args.db.strip() if args.db else None

    log_debug(f"Starting Odoo MCP stdio bridge targeting {endpoint}")

    for line in sys.stdin:
        clean_line = line.strip()
        if not clean_line:
            continue

        try:
            req_json = json.loads(clean_line)
        except json.JSONDecodeError as err:
            log_debug(f"Received invalid JSON on stdin: {err}")
            err_resp = json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(err)}"},
            })
            sys.stdout.write(err_resp + "\n")
            sys.stdout.flush()
            continue

        req_bytes = clean_line.encode("utf-8")
        resp_bytes = forward_request(endpoint, token, req_bytes, db=db_name)

        if resp_bytes:
            try:
                resp_text = resp_bytes.decode("utf-8")
                sys.stdout.write(resp_text + "\n")
                sys.stdout.flush()
            except Exception as e:
                log_debug(f"Error writing response to stdout: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
