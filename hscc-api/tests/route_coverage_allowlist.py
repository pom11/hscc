"""The reasoned register of registered-but-undocumented routes.

Generated ONCE from the live dispatch table at the sha named in the changelog fragment,
then hand-curated: this file is DATA, not output, and the gate reads it as the promise.
Every entry says two things a bare path list cannot:

* ``see <path>`` — the file where this route IS written down (the gate asserts that file
  exists and actually names the route, so the pointer cannot rot into a guess);
* ``deferred: <card id>`` — the card that owes the Endpoint-reference row.

``surface`` means the route has no shipped-client caller that the reference is failing;
``ios`` means the shipped iOS client calls it and the canonical reference still does not
name it — the audit F2 class, and the reason ``t_b9808146`` is ordered iOS-first.

To close an entry: add the row to ``docs/API.md``'s Endpoint reference AND delete the
entry here in the same commit. Leaving both is reddened on purpose -- a register that
keeps rows nobody needs is the same gap with a longer name.
"""

#: ``(METHOD, display path) -> reason``. The display path is the registered pattern with
#: each named group reduced to its ``{name}`` form, so an entry can never quietly cover a
#: sibling route (``/v1/foo`` and ``/v1/foo/list`` are two entries or neither).
UNDOCUMENTED_ROUTES = {
    ("POST", "/v1/alerts"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("POST", "/v1/alerts/{id}/ack"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("POST", "/v1/autodown/cancel"): "surface: named outside the Endpoint reference only — see docs/audits/confirm-gate-audit-t_c77f243f.md; deferred: t_b9808146",
    ("POST", "/v1/autodown/disable"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("POST", "/v1/autodown/enable"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("POST", "/v1/autodown/wake"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("PATCH", "/v1/cards/{card_id}"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see docs/DESIGN-api.md; deferred: t_b9808146",
    ("POST", "/v1/cards/{card_id}/block"): "surface: named outside the Endpoint reference only — see docs/DESIGN-api.md; deferred: t_b9808146",
    ("POST", "/v1/cards/{card_id}/close"): "surface: named outside the Endpoint reference only — see docs/DESIGN-api.md; deferred: t_b9808146",
    ("POST", "/v1/cards/{card_id}/comment"): "surface: named outside the Endpoint reference only — see docs/DESIGN-api.md; deferred: t_b9808146",
    ("POST", "/v1/cluster/down"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/cluster/history"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("POST", "/v1/cluster/up"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/escalate"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/kanban/blocked/{card_id}/recover"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/kanban/task/{task_id}/kill"): "surface: named outside the Endpoint reference only — see docs/audits/confirm-gate-audit-t_c77f243f.md; deferred: t_b9808146",
    ("GET", "/v1/logs"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/memory"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/memory/{node_id}/delete"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/memory/{node_id}/edit"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/node-pauses"): "surface: named outside the Endpoint reference only — see docs/DESIGN-api.md; deferred: t_b9808146",
    ("POST", "/v1/orchestrator/chat"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/orchestrator/chat/{id}"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/orchestrator/chat/{id}/stop"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/profile/editor/{profile}"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/profile/export"): "surface: named outside the Endpoint reference only — see docs/audits/confirm-gate-audit-t_c77f243f.md; deferred: t_b9808146",
    ("GET", "/v1/profile/export/{file}"): "surface: named outside the Endpoint reference only — see docs/audits/confirm-gate-audit-t_c77f243f.md; deferred: t_b9808146",
    ("POST", "/v1/profile/install"): "surface: named outside the Endpoint reference only — see docs/audits/confirm-gate-audit-t_c77f243f.md; deferred: t_b9808146",
    ("POST", "/v1/profiles/create"): "surface: named outside the Endpoint reference only — see docs/audits/confirm-gate-audit-t_c77f243f.md; deferred: t_b9808146",
    ("POST", "/v1/profiles/{name}/delete"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/profiles/{name}/describe"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/profiles/{name}/describe"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/profiles/{name}/rename"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/projects/new"): "surface: named outside the Endpoint reference only — see docs/audits/2026-11-23-agent-surface.md; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/hygiene"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/incidents"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/metrics"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/release"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/roadmap"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/session"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/session/events"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("WS", "/v1/projects/{name}/session/ws"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/projects/{name}/standup"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/review/{card_id}/diff"): "surface: named outside the Endpoint reference only — see docs/DESIGN-api.md; deferred: t_b9808146",
    ("POST", "/v1/sessions/{session_id}/compact"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/sessions/{session_id}/retire"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/template/preview/{name}"): "surface: named outside the Endpoint reference only — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("POST", "/v1/triggers/run"): "ios: the shipped iOS client calls this route and the Endpoint reference does not name it — see ios-app/Sources/HSCC/HSCCClient.swift; deferred: t_b9808146",
    ("GET", "/v1/why/{card_id}"): "surface: named outside the Endpoint reference only — see docs/audits/GAP_PRIORITISATION_2026-09-26.md; deferred: t_b9808146",
}
