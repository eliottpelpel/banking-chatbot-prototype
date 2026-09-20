"""Intent triage: a small model proposes an agent, Python decides.

One `mistral-small` call per turn in front of a `mistral-medium` conversation:
the cheapest lever on both cost and time-to-first-token. Anything the router
says that is not exactly `transaction` degrades to the least-privileged agent --
a bad classification can only ever narrow the action space, never widen it.
"""
from config import KNOWLEDGE, ROUTER_MODEL, TRANSACTION

PROMPT = """Tu classes la demande d'un client de banque de detail.

Reponds UNIQUEMENT par un de ces deux mots, sans ponctuation :

transaction : le client demande une operation (bloquer ou debloquer une carte,
modifier un plafond de paiement, executer un virement, etre rappele par son
conseiller).

knowledge : tout le reste (information, tarifs, horaires, procedures, et
consultation de solde, de credits ou de beneficiaires).

En cas de doute, reponds knowledge."""


def pick(reply):
    """Map a router reply onto a role. Anything unexpected falls back to knowledge."""
    return TRANSACTION if TRANSACTION in (reply or "").strip().lower() else KNOWLEDGE


def route(complete, text):
    """`complete` is the agent's instrumented, retry-aware model call."""
    reply = complete(ROUTER_MODEL,
                     [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
                     temperature=0, max_tokens=4)
    return pick(reply.choices[0].message.content)
