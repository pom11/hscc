# :4000 proxy outage, 2026-11-22 22:10–22:55 — completions 500 "Connection error" while /v1/models 200

Status: **IN PROGRESS** (forensics; no fixes implemented on this card)
Card: t_f54f735a · Scope: read-only log/state forensics. The live proxy was not touched.

Redaction: LAN addresses appear as `192.0.2.x` placeholders (TEST-NET-1), home paths as
`<HOME>`, the sparkrun cache as `<CACHE>`. No attribution.

## 0. Reported symptom

- ~22:10–22:55 local: every completion through the local `:4000` LiteLLM proxy returned
  `500 InternalServerError: OpenAIException - Connection error. Received Model Group=orchestrator-model`.
- `GET /v1/models` on the same proxy kept answering `200` for the whole window.
- All four backends answered `POST /v1/chat/completions` directly in ~0.35 s.
- Restored 22:56 by a terminal-launched `sparkrun proxy start ... --restart`; current proxy PID 85087.

## 1. Evidence inventory (what exists, what was destroyed)

_Fill in as gathered._

## 2. Timeline

_Fill in as gathered._

## 3. Root cause

_Fill in as gathered._

## 4. Why the health check never fired

_Fill in as gathered._

## 5. Impact

_Fill in as gathered._

## 6. Recommended fix cards (atomic, one seam each)

_Fill in as gathered._
