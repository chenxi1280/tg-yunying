# AI Group Provider Fallback Design

## Decision

Implement the approved production chain `MiniMax-M3 -> MiniMax-M2.5 -> Grok 4.5 CLI Bridge -> static safe check-in/text emoji` for `group_ai_chat`. Keep every fallback explicit, observable, configurable, and subject to the same sanitized-input and output-quality gates.

## Product Contract

The central product contract is updated in `docs/01-product/tg-ops-platform-prd.md`. Detailed field, data-flow, failure, observability, test, release, and rollback requirements live in `docs/03-feature-designs/ai-group-provider-fallback-and-safe-prompt-design.md`.

Instructions use English; sanitized Chinese context remains data; outputs are Chinese exact JSON. Existing explicitly adult, non-explicit appearance, figure, clothing, and mild flirtatious topics may be continued. Transaction facilitation, contacts, bookings, services, explicit sexual acts, and minor or age-ambiguous content are filtered before generation and cannot use model fallback to bypass the input decision.

## Architecture

The backend owns one generation orchestrator that receives a frozen sanitized request, calls two independent OpenAI-compatible MiniMax Provider records, then calls a bounded internal Grok CLI Bridge, and finally selects a versioned static safe fallback when enabled. Every attempt returns a normalized result and gate trace; only accepted output can create a planned message.

The Bridge runs outside the main provider abstraction because the available Grok subscription is CLI-authenticated rather than an xAI API credential. It has no Telegram or database access, disables tools/web/memory/subagents, and exposes only a constrained internal generation interface.

## Acceptance

- M3 is the tenant default and succeeds without invoking later stages.
- M2.5, Grok, and static fallback activate only on documented technical or post-generation failures for already-allowed input.
- All four sources are distinguishable in task payloads and diagnostics.
- Static fallback is explicitly enabled and independently disableable.
- Production dry-runs prove all stages without Telegram sending; production recovery remains separate from deployment and Provider health.
