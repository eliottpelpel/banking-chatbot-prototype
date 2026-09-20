"""Orchestration: route -> model -> tool calls -> (pause for consent) -> results -> answer.

The supervisor is deterministic Python, never a model. Agents do not talk to each
other: one of them *requests* a handoff, and this layer performs it. Every control
decision -- authentication, agent scope, banking policy, consent -- lives here or
in `tools.py`, and none of it lives in a prompt.
"""
import json
import re
import time
from dataclasses import dataclass

import banking
import guardrails
import router
import tools
from config import (API_KEY, CUSTOMER, KNOWLEDGE, MODEL, PUBLIC, RETRY_EXPONENT,
                    RETRY_INITIAL_MS, RETRY_MAX_ELAPSED_MS, RETRY_MAX_INTERVAL_MS,
                    TRANSACTION)

MAX_STEPS = 6
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

try:
    import httpx
    TRANSIENT_NETWORK = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
except ImportError:
    TRANSIENT_NETWORK = ()

SYSTEM = """Tu es l'assistant clientele de BNP Paribas Banque de Detail (France).

{role}

PERIMETRE : banque de detail uniquement (comptes, cartes, credits, epargne,
virements, agences, tarifs). Tu reponds en francais, de maniere concise et
professionnelle, en vouvoyant le client.

REGLES ABSOLUES
1. Tu n'inventes JAMAIS un chiffre, un taux, un horaire, un solde ou une
   procedure. Toute information factuelle doit venir d'un appel d'outil.
   Si l'outil ne renvoie rien d'utile, tu le dis et tu renvoies vers le conseiller.
2. Pour toute question d'information, appelle search_knowledge_base avant de repondre.
3. Pour toute operation bancaire, appelle l'outil correspondant. La confirmation
   du client est demandee par le systeme, pas par toi : n'ecris pas "confirmez-vous ?",
   appelle simplement l'outil, le systeme s'occupe du consentement.
4. Si un outil renvoie une erreur ou requires_advisor, explique la raison en clair
   et propose escalate_to_advisor pour ouvrir une demande de rappel.
5. Tu ne donnes aucun conseil en investissement personnalise, aucune recommandation
   fiscale ou juridique, et tu ne t'engages sur aucune decision d'octroi de credit.
6. Tu ne demandes jamais un mot de passe, un code secret ou un numero de carte complet.
7. Hors perimetre ou information indisponible : dis-le explicitement, sans deviner,
   et donne le contact du conseiller.

{contact}"""

ROLES = {
    KNOWLEDGE: (
        "TU ES L'AGENT INFORMATION. Tu reponds aux questions et tu consultes les donnees "
        "du client. Tu n'as AUCUN outil d'operation bancaire. Si le client demande de "
        "bloquer ou debloquer une carte, de modifier un plafond ou d'executer un virement, "
        "appelle handoff_to_transaction : le systeme passe la main a l'agent operations."),
    TRANSACTION: (
        "TU ES L'AGENT OPERATIONS. Tu executes les operations bancaires demandees par le "
        "client, apres verification par les outils. Tu peux aussi consulter et rechercher."),
}


class ModelUnavailable(RuntimeError):
    """Raised instead of the raw SDK error once retries are exhausted; the turn is kept intact."""

    def __init__(self, exc):
        headers = getattr(exc, "headers", None) or getattr(
            getattr(exc, "raw_response", None), "headers", None) or {}
        self.status = _status(exc)
        self.retry_after = _number(headers.get("retry-after"))
        # Kept for the logs: a capacity throttle reports a limit of 0 even when the key's quota is intact.
        self.quota = {k.replace("x-ratelimit-", ""): v
                      for k, v in headers.items() if "ratelimit" in k.lower()}
        self.user_message = "Le service est momentanement surcharge." + (
            f" Nouvelle tentative possible dans {int(self.retry_after)} secondes." if self.retry_after
            else " Merci de reessayer dans quelques instants.")
        super().__init__(f"modele indisponible (status={self.status}, quota={self.quota})")


@dataclass
class Session:
    """Access level is server-side state."""
    access_level: str = PUBLIC
    customer_id: str = None
    name: str = None

    def login(self, customer_id):
        """Simulates strong customer authentication (production: OIDC + SCA)."""
        customer = banking.get_customer(customer_id)
        if not customer:
            return False
        self.access_level, self.customer_id, self.name = CUSTOMER, customer_id, customer["name"]
        return True

    def logout(self):
        self.access_level, self.customer_id, self.name = PUBLIC, None, None


def contact_block(session):
    """Real advisor details, injected every turn so the model never invents a fallback."""
    g = banking.ADVISORS["general"]
    header = "CONTACT DE REPLI (a donner quand tu ne peux pas repondre) :\n"
    if session.access_level != CUSTOMER:
        return (f"{header}{g['name']} - {g['phone']}, {g['hours']}. "
                f"Urgence carte : {g['emergency_card']}.\n"
                "Le client n'est PAS authentifie : aucune donnee personnelle ne peut etre consultee. "
                "Pour toute demande personnelle, invite-le a s'identifier dans l'application.")
    c = banking.get_customer(session.customer_id)
    a = banking.ADVISORS[c["advisor_id"]]
    return (f"{header}{a['name']}, {a['role']}, {a['agency']} - {a['phone']} - {a['email']}. "
            f"{a['slots']}.\nUrgence carte 24h/24 : {g['emergency_card']}.\n"
            f"Le client authentifie est {c['name']} (segment : {c['segment']}).")


class Agent:
    def __init__(self, session=None, client=None):
        self.session = session or Session()
        self.messages = []
        self.role = None
        self.trace = []
        self._elapsed = 0.0
        self._queue = []
        self._pending = None
        self.client = client or _sdk_client()

    # public API: send(), confirm() and retry() all return an event

    def send(self, text):
        self.messages.append({"role": "user", "content": text})
        self.role, self.trace, self._elapsed = None, [], 0.0     # a new turn
        return self._advance()

    def confirm(self, approved):
        """Resume a paused turn with the customer's decision."""
        call, self._pending = self._pending, None
        if approved:
            result = tools.execute(call["name"], call["args"], self.session, self.role)
        else:
            result = {"ok": False, "error": "user_declined",
                      "message": "Le client a refuse l'operation. Ne pas la relancer, proposer une alternative."}
            banking.audit(self.session.customer_id, call["name"], call["args"], "user_declined")
        self._record(call, result)
        return self._advance()

    def retry(self):
        """Resume a turn interrupted by ModelUnavailable: nothing is appended, no tool replayed."""
        return self._advance()

    def stats(self):
        """Per-turn latency and token cost -- the two numbers a deployment owner asks for first."""
        return {"calls": len(self.trace), "role": self.role,
                "total_ms": round(self._elapsed * 1000),
                "model_ms": sum(t["ms"] for t in self.trace),
                "tokens_in": sum(t["in"] for t in self.trace),
                "tokens_out": sum(t["out"] for t in self.trace),
                "models": [t["model"] for t in self.trace]}

    # the loop

    def _advance(self):
        """Route if needed, run pending tool calls, ask the model, until it answers."""
        started = time.perf_counter()
        try:
            if self.role is None:
                self.role = router.route(self._complete, self.messages[-1]["content"])

            for _ in range(MAX_STEPS):
                while self._queue:
                    call = self._queue.pop(0)
                    spec = tools.TOOLS.get(call["name"])
                    # An unauthenticated call is not worth a confirmation prompt: run it, and let
                    # tools.execute return the refusal to the model.
                    needs_auth = spec and spec["auth"] == CUSTOMER and self.session.access_level != CUSTOMER
                    if spec and spec["confirm"] and not needs_auth:
                        self._pending = call
                        return {"type": "confirm", "tool": call["name"], "args": call["args"],
                                "preview": self._preview(call["name"], call["args"])}
                    self._record(call, tools.execute(call["name"], call["args"],
                                                     self.session, self.role))

                msg = self._call_model()
                calls = getattr(msg, "tool_calls", None) or []
                entry = {"role": "assistant", "content": msg.content or ""}
                if calls:
                    entry["tool_calls"] = [{"id": c.id, "type": "function",
                                            "function": {"name": c.function.name,
                                                         "arguments": c.function.arguments}}
                                           for c in calls]
                self.messages.append(entry)
                if not calls:
                    return {"type": "answer", "text": guardrails.mask(msg.content),
                            "stats": self.stats()}
                self._queue = [{"id": c.id, "name": c.function.name,
                                "args": _parse(c.function.arguments)} for c in calls]

            return {"type": "answer", "stats": self.stats(),
                    "text": "Je ne parviens pas a traiter cette demande. "
                            + contact_block(self.session).split("\n")[1]}
        finally:
            self._elapsed += time.perf_counter() - started

    def _record(self, call, result):
        """Append a tool result -- and perform the handoff, if the tool asked for one."""
        self.role = result.get("handoff", self.role)
        self.messages.append({"role": "tool", "name": call["name"], "tool_call_id": call["id"],
                              "content": json.dumps(result, ensure_ascii=False)})

    def _preview(self, name, args):
        try:
            return tools.TOOLS[name]["preview"](args)
        except Exception:
            return f"{name} {args}"

    def _call_model(self):
        system = {"role": "system",
                  "content": SYSTEM.format(role=ROLES[self.role],
                                           contact=contact_block(self.session))}
        return self._complete(MODEL, [system] + self.messages,
                              tools=tools.schemas(self.role),   # the agent's whole action space
                              tool_choice="auto",
                              parallel_tool_calls=False,        # one banking operation at a time
                              temperature=0.1).choices[0].message

    def _complete(self, model, messages, **kwargs):
        """Every model call goes through here: error typing and per-turn measurement."""
        started = time.perf_counter()
        try:
            resp = self.client.chat.complete(model=model, messages=messages, **kwargs)
        except Exception as exc:
            if _status(exc) in RETRYABLE_STATUS or isinstance(exc, TRANSIENT_NETWORK):
                raise ModelUnavailable(exc) from exc
            raise
        usage = getattr(resp, "usage", None)
        self.trace.append({"model": model, "ms": round((time.perf_counter() - started) * 1000),
                           "in": getattr(usage, "prompt_tokens", 0) or 0,
                           "out": getattr(usage, "completion_tokens", 0) or 0})
        return resp


def _sdk_client():
    """Mistral client with exponential backoff on 429/5xx, honouring Retry-After.

    Imported here and not at module level: the control tests must run without the
    SDK installed.
    """
    from mistralai import Mistral
    from mistralai.utils import BackoffStrategy, RetryConfig
    if not API_KEY:
        raise RuntimeError("MISTRAL_API_KEY manquant.")
    backoff = BackoffStrategy(RETRY_INITIAL_MS, RETRY_MAX_INTERVAL_MS,
                              RETRY_EXPONENT, RETRY_MAX_ELAPSED_MS)
    return Mistral(api_key=API_KEY,
                   retry_config=RetryConfig("backoff", backoff, retry_connection_errors=True))


def _status(exc):
    """HTTP status of an SDK error, from the attribute if present, else from its text."""
    if isinstance(getattr(exc, "status_code", None), int):
        return exc.status_code
    match = re.search(r"Status (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
