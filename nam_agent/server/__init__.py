"""The NAM harness server: one HTTP process, one port (7777 by default).

    nam-agent serve            # http://127.0.0.1:7777

  agent.py       the one agent that runs the chat turns, and its lock
  chat.py        a chat turn (plain JSON or streamed), the chats and who owns them
  nam_routes.py  the /api/nam/*, /api/nit/* and /api/tender/* routes
  http.py        the HTTP handler: the routes above, /v1/nam/* (nam_agent/nam/api.py)
                 and the officer's chat page (static/)

docs/backend-integration.md describes every endpoint.
"""
