# Codex / Gemini CLI / Custom Agent Integration

Any CLI or API-capable agent can coordinate via `agsync`:

1. **MCP (Stdio)**:
   `python -m agsync.mcp_bridge --server http://<HOST>:8765 --agent <AGENT_ID>`
2. **CLI**:
   `python -m agsync.cli register --branch <BRANCH>`
   `python -m agsync.cli lock <PATH> --reason "..."`
   `python -m agsync.cli unlock --all`
3. **HTTP REST API**:
   - `POST /agents/register`
   - `POST /leases/acquire`
   - `POST /messages` & `GET /events?since_id=0&wait=15`
   - `POST /tasks` & `POST /tasks/<id>/claim`
