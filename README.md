# BNP Paribas Retail Banking Assistant (prototype)

A chatbot for retail banking customers. It answers questions from a knowledge
base, runs banking operations after the customer confirms them, and hands over
to a human advisor when it cannot help.

The assistant talks to the customer in French. The code and the docs are in
English.

## Getting started

```bash
pip install -r requirements.txt      # mistralai, streamlit
cp .env.example .env                 # then fill in MISTRAL_API_KEY
```

```bash
python cli.py                        # terminal chat
streamlit run ui.py                  # web chat
python test_controls.py              # 18 suites, 89 assertions, no LLM call
```

Two demo customers: `C001` (Camille Dubois, Paris Opéra, has a mortgage) and
`C002` (Yanis Lefèvre, Lyon, card already blocked). In the terminal, use
`/login C001`, `/logout`, `/reset` and `/quit`.

`config.py` reads the key from `.env`. You can change the models with
`MISTRAL_MODEL` and `MISTRAL_ROUTER_MODEL`.

## How it works

```
   cli.py / ui.py          asks the customer to confirm, shows latency and tokens
        |
   agent.py                supervisor: picks the agent, runs the loop,
        |                  pauses before writes, measures every call
        |
   router.py   tools.py    kb.py        guardrails.py
   triage      registry    retrieval    masks IBANs and card numbers
                  |
              banking.py   stands in for core banking, keeps the audit log
```

One turn goes like this: the router classifies the message, the supervisor gives
the chosen agent its tools, the model answers or asks for a tool, and anything
that changes state stops and waits for a yes or no from the customer.

The main idea: the model suggests, Python decides. Authentication, tool scope,
banking rules and consent are all code, never prompt text.

## Two agents

A small model routes each turn to one of two agents, and each agent only sees
part of the tool registry.

| Tools | knowledge | transaction |
|---|---|---|
| search, balance, credits, beneficiaries | yes | yes |
| escalate_to_advisor | yes | yes |
| block card, unblock card, change limit, transfer | no | yes |

So the knowledge agent has no way to move money, even if someone talks it into
trying. `tools.execute` checks the scope again when the call comes back, and if
the router returns anything unexpected it falls back to the knowledge agent.
The agents do not talk to each other. The knowledge agent calls
`handoff_to_transaction`, and the supervisor does the switch.

## The controls

| What | Where |
|---|---|
| Authentication | `Session.access_level`, checked in `tools.execute` before any call |
| Data isolation | `customer_id` comes from the session, never from the model |
| Retrieval permissions | `kb.search` filters the corpus before scoring it |
| Consent | the orchestrator pauses, and the preview is built from the parsed arguments |
| Banking rules | thresholds in `config.py`, checked in `banking.py` |
| Tool scope | `agents` per tool, checked twice |
| Audit trail | `_audit_log.jsonl`: time, customer, action, arguments, outcome |
| PII in answers | `guardrails.mask` hides IBANs and card numbers |
| Escalation | `escalate_to_advisor` opens a real callback ticket |
| Loops | `MAX_STEPS = 6`, then it points the customer to their advisor |
| API failures | backoff on 429 and 5xx, then a typed error and a resume that does not replay the operation |
| Cost and latency | every model call is timed and counted, per turn |

The tests cover all of this without calling a model, so they can block CI.

## What changes in production

- **Retrieval**: `mistral-embed` and a vector store, with permissions as a
  metadata pre-filter. Same function signature.
- **Models**: `mistral-small` already does the routing, `mistral-medium` the
  conversation, `mistral-large` for harder cases. VPC or on-premise deployment
  keeps customer data inside the bank.
- **Core banking**: `banking.py` becomes calls to the real APIs through the ESB,
  with an idempotency key per write and a circuit breaker per domain.
- **Authentication**: OIDC and SCA (PSD2). The access level stays server side.
- **Observability**: latency, cost, tools used, confirm and decline rates, and
  the advisor fallback rate. That last one is the KPI that matters.
- **Evaluation**: a frozen business test set replayed whenever the prompt or the
  model changes.