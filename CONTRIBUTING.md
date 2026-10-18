# Contributing to Odoo MCP Server

Thank you for your interest in contributing to **Odoo MCP Server**! Open source thrives on community collaboration.

## Getting Started

1. **Fork the Repository** on GitHub.
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/ghostdeveloper08/abi_odoo_mcp.git
   cd abi_odoo_mcp
   ```
3. **Set up Odoo Development Environment**:
   - Make sure you have Odoo 16.0, 17.0, 18.0, 19.0, or 20.0 installed.
   - Symlink or add `abi_odoo_mcp` to your addons path.
   - Install the module on a test database:
     ```bash
     odoo-bin -d test-odoo-mcp -i abi_odoo_mcp
     ```

## Code Guidelines

- **Clean Architecture**: Never hardcode `.sudo()` in MCP tool execution. All tool operations must run within `user_env = request.env(user=user_id)`.
- **Soft Detection**: Use dynamic checks (`if "model.name" in env:`) to ensure compatibility across Community and Enterprise editions.
- **Python Style**: Adhere to PEP 8 and standard Odoo coding conventions.
- **Tests**: When adding new tools or features, add automated unit and HTTP test cases in `tests/test_mcp_server.py`.

## Running the Automated Test Suite

Before submitting a PR, ensure all tests pass:
```bash
odoo-bin -d <your_test_db> -i abi_odoo_mcp --test-enable --test-tags=/abi_odoo_mcp --stop-after-init
```

## Submitting Pull Requests

1. Create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Commit your changes with a descriptive message.
3. Push to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```
4. Open a Pull Request on GitHub against the `main` branch.
