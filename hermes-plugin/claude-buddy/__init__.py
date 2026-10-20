"""Hermes Agent plugin: dangerous-command approvals on the Claude Buddy screen.

Hermes already asks a human before running a command that matches its dangerous patterns (Discord,
Telegram, the TUI...). This plugin registers an *approval transport* named `buddy`: when
`security.approval.transport: buddy` selects it, the same request goes to the buddy's listener
(`POST /approval`), shows on the Pi's touch screen as `hermes · terminal`, and a tap on Aprobar or
Denegar comes back as the decision. The plugin only presents the request; what counts as dangerous,
the allowed choices, the timeout and the final say stay Hermes' (see hermes_cli/approval_transport.py).

If the screen cannot answer (buddy off, no token, nobody tapped in `timeout` seconds) the plugin
raises, Hermes records a transport error and, with `transport_fallback: builtin`, asks the way it did
before this plugin existed. It never approves or denies on its own.

config.yaml:

    plugins:
      enabled: [claude-buddy]
      entries:
        claude-buddy:
          settings:
            url: http://100.64.0.12:8766          # the Pi's Hermes listener (Tailscale IP, or buddy.local)
            token_file: ~/.hermes/claude-buddy.token   # the listener's token, 0600, yours
            timeout: 120                           # seconds the screen gets before Hermes asks elsewhere
    security:
      approval:
        transport: buddy
        transport_fallback: builtin

Standard library only. The HTTP side lives in client.py so it can be tested without Hermes.
"""
import logging
import time

from .client import Settings, Unavailable, ask_buddy

log = logging.getLogger("hermes.plugins.claude_buddy")

TRANSPORT_NAME = "buddy"
HOST_MARGIN_S = 5.0  # answer before Hermes' own deadline, or the answer is discarded as late


def present(request, get_config):
    """Hermes' ApprovalRequest in, its ApprovalDecision out (or an exception: ask elsewhere)."""
    settings = Settings.from_mapping(get_config)
    host_timeout = getattr(request, "timeout_seconds", None)
    deadline = None
    if isinstance(host_timeout, (int, float)) and host_timeout > 0:
        deadline = time.monotonic() + max(1.0, float(host_timeout) - HOST_MARGIN_S)
    choice = ask_buddy(settings, getattr(request, "command", "") or "", getattr(request, "description", "") or "",
                       deadline=deadline)
    if choice is None:
        raise Unavailable("the screen did not answer")
    allowed = getattr(request, "allowed_choices", None)
    if allowed is not None and choice not in allowed:  # cannot happen ("once" and "deny" are always offered)
        raise Unavailable(f"{choice!r} is not an allowed choice here")
    return request.respond(choice)


def register(ctx) -> None:
    ctx.register_approval_transport(TRANSPORT_NAME, lambda request: present(request, ctx.get_config))
    log.info("claude-buddy: approval transport %r ready (select it with security.approval.transport)", TRANSPORT_NAME)
