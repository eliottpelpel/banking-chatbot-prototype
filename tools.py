"""Tool registry: schema for the model, auth level, agent scope, confirmation preview.

Four declarative properties per tool, all enforced in Python and none in a prompt:
`auth` is the access level required; `agents` is the set of agents allowed to see
the tool at all; `confirm` pauses the orchestrator before a write; `preview` is
rendered from the parsed arguments, so the customer consents to exactly the call
that will run.
"""
import banking
import kb
from config import CUSTOMER, KNOWLEDGE, PUBLIC, TRANSACTION

TOOLS = {}
BOTH = (KNOWLEDGE, TRANSACTION)


def _eur(x):
    return f"{float(x):,.2f} EUR".replace(",", " ")


def _tool(name, description, run, preview, params=None, required=(), auth=CUSTOMER,
          confirm=False, agents=BOTH):
    """Register a tool. `params` maps an argument name to (json_type, description).

    `confirm=True` is reserved for tools that change state: consent gates writes,
    authentication gates reads.
    """
    TOOLS[name] = {
        "auth": auth, "confirm": confirm, "agents": agents, "run": run, "preview": preview,
        "schema": {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "required": list(required),
                           "properties": {k: {"type": t, "description": d}
                                          for k, (t, d) in (params or {}).items()}}}},
    }


def _bank(fn):
    """Bind a banking call to the session's customer_id -- never a model argument."""
    return lambda session, **kwargs: fn(session.customer_id, **kwargs)


def _search(session, query):
    hits = kb.search(query, session.access_level, session.customer_id)
    return {"ok": True, "documents": [{k: d[k] for k in ("id", "title", "text")} for d in hits]}


CARD = {"card_last4": ("string", "4 derniers chiffres de la carte.")}
def _card_name(args):
    return args.get("card_last4") or "(votre carte)"


# --- reachable from both agents --------------------------------------------

_tool("search_knowledge_base",
      "Recherche dans la base documentaire BNP Paribas : horaires d'agence, taux de credit, "
      "epargne et retraite, cartes, frais, procedures, et documents contractuels du client "
      "authentifie. A utiliser pour toute question d'information.",
      _search, lambda a: f"Recherche documentaire : {a.get('query')}",
      params={"query": ("string", "La question, reformulee en mots-cles.")},
      required=["query"], auth=PUBLIC)

_tool("get_balance", "Consulte le solde des comptes du client authentifie.",
      _bank(banking.get_balance),
      lambda a: "Consulter le solde de vos comptes"
                + (f" ({a['account_type']})" if a.get("account_type") else ""),
      params={"account_type": ("string", "Optionnel : 'cheques' ou 'Livret A'.")})

_tool("get_credit_balance",
      "Capital restant du, mensualite, taux et echeance des credits en cours.",
      _bank(banking.get_credit_balance),
      lambda a: "Consulter le capital restant du de vos credits")

_tool("list_beneficiaries",
      "Liste les beneficiaires de virement deja enregistres par le client : nom, banque et "
      "4 derniers chiffres de l'IBAN. A utiliser pour verifier qu'un beneficiaire existe "
      "avant un virement, ou quand le client demande qui il peut payer.",
      _bank(banking.list_beneficiaries),
      lambda a: "Consulter la liste de vos beneficiaires enregistres"
                + (f" (recherche : {a['name']})" if a.get("name") else ""),
      params={"name": ("string", "Optionnel : filtrer sur un nom de beneficiaire.")})

_tool("escalate_to_advisor",
      "Ouvre une demande de rappel aupres du conseiller attitre du client. A utiliser des "
      "qu'une demande sort de ton perimetre, depasse la politique bancaire, ou releve du "
      "conseil (credit, investissement, fiscalite). La prise de contact est une action, "
      "pas un numero de telephone.",
      _bank(banking.request_callback),
      lambda a: f"DEMANDER UN RAPPEL de votre conseiller - motif : {a['reason']}",
      params={"reason": ("string", "Motif du rappel, en une phrase, du point de vue du client.")},
      required=["reason"], confirm=True)


# --- the knowledge agent's exit door ---------------------------------------

_tool("handoff_to_transaction",
      "A appeler quand le client demande une operation bancaire (bloquer ou debloquer une "
      "carte, modifier un plafond, executer un virement) : tu n'as pas ces outils, le "
      "systeme passe la main a l'agent operations qui, lui, les a.",
      lambda session: {"ok": True, "handoff": TRANSACTION,
                       "message": "Passage a l'agent operations."},
      lambda a: "Passer la main a l'agent operations",
      auth=PUBLIC, agents=(KNOWLEDGE,))


# --- writes: the transaction agent only ------------------------------------

_tool("block_card", "Bloque temporairement une carte bancaire (reversible).",
      _bank(banking.block_card),
      lambda a: f"BLOQUER la carte {_card_name(a)} - paiements et retraits refuses "
                "immediatement (reversible)",
      params=CARD, confirm=True, agents=(TRANSACTION,))

_tool("unblock_card", "Reactive une carte precedemment bloquee.",
      _bank(banking.unblock_card),
      lambda a: f"DEBLOQUER la carte {_card_name(a)} - elle redevient utilisable immediatement",
      params=CARD, confirm=True, agents=(TRANSACTION,))

_tool("increase_payment_capacity",
      "Augmente le plafond de paiement mensuel d'une carte, dans la limite de la politique bancaire.",
      _bank(banking.increase_payment_capacity),
      lambda a: f"AUGMENTER le plafond de paiement de la carte {_card_name(a)} a "
                f"{_eur(a['new_limit_eur'])} sur 30 jours glissants",
      params={"new_limit_eur": ("number", "Nouveau plafond souhaite, en euros."), **CARD},
      required=["new_limit_eur"], confirm=True, agents=(TRANSACTION,))

_tool("make_transfer",
      "Execute un virement SEPA vers un beneficiaire deja enregistre. L'ajout d'un nouveau "
      "beneficiaire n'est pas possible par ce canal.",
      _bank(banking.make_transfer),
      lambda a: f"VIREMENT de {_eur(a['amount_eur'])} vers {a['beneficiary_name']}"
                + (f" - motif : {a['label']}" if a.get("label") else ""),
      params={"beneficiary_name": ("string", "Nom du beneficiaire enregistre."),
              "amount_eur": ("number", "Montant en euros."),
              "label": ("string", "Motif du virement.")},
      required=["beneficiary_name", "amount_eur"], confirm=True, agents=(TRANSACTION,))


AGENTS = {role: tuple(n for n, s in TOOLS.items() if role in s["agents"]) for role in BOTH}


def schemas(role):
    """The action space of one agent. What is not in this list does not exist for it."""
    return [TOOLS[name]["schema"] for name in AGENTS[role]]


def execute(name, args, session, role):
    """Single choke point: agent scope, then auth, then execute, then audit."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"ok": False, "error": "unknown_tool"}
    if name not in AGENTS.get(role, ()):
        return {"ok": False, "error": "out_of_scope",
                "message": f"L'outil {name} n'est pas dans le perimetre de l'agent {role}."}
    if spec["auth"] == CUSTOMER and session.access_level != CUSTOMER:
        return {"ok": False, "error": "authentication_required",
                "message": "Cette operation necessite une authentification forte du client."}
    try:
        result = spec["run"](session, **args)
    except TypeError as e:
        result = {"ok": False, "error": "bad_arguments", "message": str(e)}
    if spec["auth"] == CUSTOMER:        # every operation on customer data is traced
        banking.audit(session.customer_id, name, args, result.get("error", "ok"))
    return result
