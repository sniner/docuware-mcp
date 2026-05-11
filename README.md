# docuware-mcp

A Model Context Protocol (MCP) server that exposes a DocuWare DMS to
LLM-based agents through a database-style API.

This is an independent project with no affiliation to DocuWare GmbH.

## Configuration

Credentials are read from the `docuware-client` standard environment
variables:

```
DW_URL=https://dms.example.com
DW_USERNAME=service_account
DW_PASSWORD=<secret>
DW_ORG=<org>
```

Alternatively, point `DW_CREDENTIALS_FILE` at a JSON file — useful for
switching between test and production systems, or for keeping secrets
out of shell history:

```
DW_CREDENTIALS_FILE=/path/to/credentials.json
```

The file uses the same keys as the environment variables:

```json
{
    "url": "https://dms.example.com",
    "username": "service_account",
    "password": "<secret>",
    "organization": "Acme GmbH"
}
```

`organization` is optional if the service account belongs to a single
organization. Make sure the file is not world-readable (`chmod 600`).

For internal DocuWare installations with self-signed or private-CA
certificates, TLS verification can be disabled with
`DW_VERIFY_CERT=false`. **Do not use this against production systems** —
it disables protection against man-in-the-middle attacks.

OAuth2 requires DocuWare 7.10 or later.

## Use with an MCP client

`docuware-mcp` is a stdio-based MCP server: an MCP client (Claude
Desktop, Claude Code, …) launches it as a subprocess and talks to it
over stdin/stdout. You don't run it yourself — the client does.

The recommended install path is via [`uv`](https://docs.astral.sh/uv/),
because `uvx` will fetch and run the package on demand without a global
install. Install `uv` once (`brew install uv` on macOS,
`curl -LsSf https://astral.sh/uv/install.sh | sh` on Linux,
`irm https://astral.sh/uv/install.ps1 | iex` in PowerShell on Windows),
then add this entry to your client's MCP config:

```json
{
  "mcpServers": {
    "docuware": {
      "command": "uvx",
      "args": ["docuware-mcp"],
      "env": {
        "DW_CREDENTIALS_FILE": "/path/to/credentials.json"
      }
    }
  }
}
```

The config file lives at:

- **Claude Desktop**: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows)
- **Claude Code**: `.mcp.json` in your project root (or run `claude mcp add docuware -- uvx docuware-mcp`)

For [OpenCode](https://opencode.ai/) the shape is slightly different —
add to `opencode.json` (project) or `~/.config/opencode/opencode.json`
(user):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "docuware": {
      "type": "local",
      "command": ["uvx", "docuware-mcp"],
      "environment": {
        "DW_CREDENTIALS_FILE": "/path/to/credentials.json"
      },
      "enabled": true
    }
  }
}
```

Restart the client after editing. The `docuware` server should then
appear in the available-tools list, exposing `list_archives`,
`describe_archive`, `search`, `get_document`, `get_document_text`,
and `status`.

### Running directly (for debugging)

If you've cloned this repo and want to poke at the server with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector)
or call it from a script:

```
docuware-mcp
```

Speaks MCP over stdio — same protocol the clients above use.

## License

BSD-3-Clause.
