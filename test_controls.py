"""Tests of the controls: permissions, auth gate, banking policy, audit, masking.

No model is called and none is simulated: every rule that protects the customer
is deterministic Python, so it is tested as deterministic Python.

Run: python3 test_controls.py
"""
import json

import banking
import guardrails
import kb
import router
import tools
from agent import Agent, ModelUnavailable, Session
from config import (CUSTOMER, KNOWLEDGE, MAX_CARD_LIMIT_EUR, MAX_LIMIT_INCREASE_RATIO,
                    TRANSACTION)

BANKING_WRITES = ("block_card", "unblock_card", "increase_payment_capacity", "make_transfer")
WRITES = BANKING_WRITES + ("escalate_to_advisor",)
READS = ("search_knowledge_base", "get_balance", "get_credit_balance", "list_beneficiaries")


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def customer(customer_id="C001"):
    """A fresh authenticated session over the seed state."""
    banking.reset()
    session = Session()
    session.login(customer_id)
    return session


def ids(hits):
    return [d["id"] for d in hits]


# --- 1. permission-aware retrieval -----------------------------------------

def test_retrieval_is_filtered_before_it_is_scored():
    print("\n[1] Retrieval a deux niveaux")
    check("un document public remonte en session anonyme",
          "PUB-001" in ids(kb.search("horaires agence samedi", "public")))
    check("aucun document client n'entre dans une session anonyme",
          not any(i.startswith("CUS-") for i in
                  ids(kb.search("remboursement anticipe credit immobilier", "public"))))
    check("le client authentifie voit son propre contrat",
          "CUS-C001-01" in ids(kb.search("remboursement anticipe credit immobilier",
                                         CUSTOMER, "C001")))
    check("un client ne voit jamais le contrat d'un autre",
          not any(i.startswith("CUS-C001") for i in
                  ids(kb.search("remboursement anticipe credit immobilier",
                                CUSTOMER, "C002"))))


# --- 2. the auth gate -------------------------------------------------------

def test_banking_tools_are_closed_to_anonymous_sessions():
    print("\n[2] Porte d'authentification dans tools.execute")
    anonymous = Session()
    for name in ("get_balance", "list_beneficiaries", "block_card", "make_transfer"):
        result = tools.execute(name, {}, anonymous, TRANSACTION)
        check(f"{name} refuse sans authentification",
              result["error"] == "authentication_required")
    check("la recherche documentaire reste ouverte",
          tools.execute("search_knowledge_base", {"query": "horaires"}, anonymous, KNOWLEDGE)["ok"])
    check("une carte n'a pas ete bloquee au passage",
          banking.get_customer("C001")["cards"][0]["status"] == "active")


def test_customer_id_is_never_a_model_argument():
    print("\n[3] Le modele ne peut pas designer un autre client")
    exposed = {arg for role in (KNOWLEDGE, TRANSACTION) for schema in tools.schemas(role)
               for arg in schema["function"]["parameters"]["properties"]}
    check("aucun outil n'expose customer_id au modele", "customer_id" not in exposed)
    check("ni un identifiant de compte", not any("account_id" in a for a in exposed))


def test_login_is_the_only_way_to_raise_the_access_level():
    print("\n[4] Le niveau d'acces est un etat serveur")
    session = Session()
    check("la session demarre publique", session.access_level == "public")
    check("un identifiant inconnu est rejete", session.login("C999") is False)
    check("et ne change rien", session.access_level == "public" and session.customer_id is None)
    check("un identifiant connu authentifie", session.login("C001") is True)
    check("le nom vient de la base, pas du modele", session.name == "Camille Dubois")


# --- 3. the tool registry ---------------------------------------------------

def test_registry_invariants():
    print("\n[5] Invariants du registre : le consentement couvre les ecritures")
    for name in WRITES:
        spec = tools.TOOLS[name]
        check(f"{name} exige authentification et consentement",
              spec["auth"] == CUSTOMER and spec["confirm"] is True)
    for name in READS:
        check(f"{name} ne demande pas de consentement", tools.TOOLS[name]["confirm"] is False)
    check("seuls la recherche et le passage de main sont publics",
          [n for n, s in tools.TOOLS.items() if s["auth"] != CUSTOMER]
          == ["search_knowledge_base", "handoff_to_transaction"])


def test_preview_is_rendered_from_the_arguments():
    print("\n[6] Le texte soumis au client vient des arguments parses")
    preview = tools.TOOLS["make_transfer"]["preview"](
        {"beneficiary_name": "Lucas Dubois", "amount_eur": 800, "label": "Loyer"})
    check("le montant exact apparait", "800.00 EUR" in preview)
    check("le beneficiaire exact apparait", "Lucas Dubois" in preview)
    check("le motif apparait", "Loyer" in preview)
    check("le blocage est annonce comme tel",
          "BLOQUER" in tools.TOOLS["block_card"]["preview"]({"card_last4": "4417"}))


# --- 4. deterministic banking policy ----------------------------------------

def test_payment_limit_policy():
    print("\n[7] Plafond de paiement : seuils en Python")
    session = customer()
    current = banking.get_customer("C001")["cards"][0]["monthly_payment_limit_eur"]
    ceiling = min(MAX_CARD_LIMIT_EUR, current * MAX_LIMIT_INCREASE_RATIO)

    under = tools.execute("increase_payment_capacity", {"new_limit_eur": ceiling - 1000}, session, TRANSACTION)
    check("une hausse sous le plafond est accordee", under["ok"])
    session = customer()

    over = tools.execute("increase_payment_capacity", {"new_limit_eur": 20000}, session, TRANSACTION)
    check("une hausse hors politique est refusee", over["error"] == "exceeds_policy")
    check("l'escalade conseiller est signalee au modele", over["requires_advisor"] is True)
    check("le plafond autonome est annonce en clair", f"{ceiling:.0f} EUR" in over["message"])
    check("rien n'a ete ecrit",
          banking.get_customer("C001")["cards"][0]["monthly_payment_limit_eur"] == current)

    check("une baisse deguisee est refusee",
          tools.execute("increase_payment_capacity", {"new_limit_eur": 100},
                        session, TRANSACTION)["error"] == "not_an_increase")


def test_transfer_rules():
    print("\n[8] Virement : quatre controles sequentiels")
    session = customer()
    unknown = tools.execute("make_transfer",
                            {"beneficiary_name": "Jean Martin", "amount_eur": 100}, session, TRANSACTION)
    check("un beneficiaire non enregistre est refuse", unknown["error"] == "unknown_beneficiary")
    check("les beneficiaires connus sont rendus au modele",
          "Lucas Dubois" in unknown["known_beneficiaries"])

    check("un montant negatif est refuse",
          tools.execute("make_transfer", {"beneficiary_name": "Lucas", "amount_eur": -50},
                        session, TRANSACTION)["error"] == "invalid_amount")

    over = tools.execute("make_transfer", {"beneficiary_name": "Lucas", "amount_eur": 9000}, session, TRANSACTION)
    check("la provision est verifiee, decouvert autorise inclus",
          over["error"] == "insufficient_funds" and over["available_eur"] == 4347.32)

    ok = tools.execute("make_transfer",
                       {"beneficiary_name": "lucas", "amount_eur": 800, "label": "Loyer"}, session, TRANSACTION)
    check("un nom approximatif retrouve le beneficiaire", ok["beneficiary"] == "Lucas Dubois")
    check("l'IBAN n'est jamais rendu en entier", ok["iban_last4"] == "0189")
    check("le solde est debite", ok["new_balance_eur"] == 2047.32)

    db = banking._load()
    db["C001"]["transferred_today_eur"] = 4900.0
    banking._save(db)
    capped = tools.execute("make_transfer",
                           {"beneficiary_name": "Lucas", "amount_eur": 300}, session, TRANSACTION)
    check("le plafond journalier est oppose", capped["error"] == "daily_limit_exceeded")
    check("et renvoie vers le conseiller", capped["requires_advisor"] is True)


def test_card_operations():
    print("\n[9] Cartes : blocage, idempotence, desambiguisation")
    session = customer()
    check("le blocage est effectif",
          tools.execute("block_card", {}, session, TRANSACTION)["status"] == "blocked")
    again = tools.execute("block_card", {}, session, TRANSACTION)
    check("un second blocage ne casse pas le dialogue", again["ok"] and again["already"])
    check("un numero de carte inconnu est refuse",
          tools.execute("block_card", {"card_last4": "0000"},
                        session, TRANSACTION)["error"] == "card_not_found")
    check("la carte deja bloquee de C002 est reactivable",
          tools.execute("unblock_card", {}, customer("C002"), TRANSACTION)["status"] == "active")


def test_beneficiaries_never_expose_a_full_iban():
    print("\n[10] Beneficiaires : IBAN tronque, filtre par nom")
    session = customer()
    listing = tools.execute("list_beneficiaries", {}, session, TRANSACTION)
    payload = json.dumps(listing, ensure_ascii=False)
    check("les trois beneficiaires remontent", len(listing["beneficiaries"]) == 3)
    check("aucun IBAN complet dans la charge utile",
          "FR7630006000011234567890189" not in payload and "0189" in payload)
    filtered = tools.execute("list_beneficiaries", {"name": "lucas"}, session, TRANSACTION)
    check("filtre insensible a la casse et aux accents",
          [b["name"] for b in filtered["beneficiaries"]] == ["Lucas Dubois"])
    empty = tools.execute("list_beneficiaries", {"name": "Jean Martin"}, session, TRANSACTION)
    check("une recherche vide explique ce qui existe",
          empty["beneficiaries"] == [] and "Lucas Dubois" in empty["known_beneficiaries"])


# --- 4 bis. multi-agent scoping ---------------------------------------------

def test_the_knowledge_agent_cannot_touch_money():
    print("\n[11] Perimetre des agents : isolation par construction")
    knowledge = set(tools.AGENTS[KNOWLEDGE])
    check("l'agent information ne voit aucune operation bancaire",
          knowledge.isdisjoint(BANKING_WRITES))
    check("l'agent operations les voit toutes",
          set(BANKING_WRITES) <= set(tools.AGENTS[TRANSACTION]))
    check("la consultation reste ouverte aux deux", all(
        name in tools.AGENTS[KNOWLEDGE] and name in tools.AGENTS[TRANSACTION] for name in READS))
    check("l'escalade est joignable depuis les deux",
          all("escalate_to_advisor" in tools.AGENTS[r] for r in (KNOWLEDGE, TRANSACTION)))
    exposed = {schema["function"]["name"] for schema in tools.schemas(KNOWLEDGE)}
    check("le schema envoye au modele ne contient pas make_transfer",
          "make_transfer" not in exposed)


def test_scope_is_re_checked_at_execution():
    print("\n[12] Defense en profondeur : le perimetre est reverifie a l'execution")
    session = customer()
    blocked = tools.execute("make_transfer",
                            {"beneficiary_name": "Lucas", "amount_eur": 100}, session, KNOWLEDGE)
    check("un outil hors perimetre est refuse meme s'il est appele",
          blocked["error"] == "out_of_scope")
    check("et rien n'a ete debite",
          banking.get_customer("C001")["accounts"][0]["balance_eur"] == 2847.32)
    check("le passage de main est refuse a l'agent operations",
          tools.execute("handoff_to_transaction", {}, session,
                        TRANSACTION)["error"] == "out_of_scope")


def test_routing_failure_degrades_to_least_privilege():
    print("\n[13] Le routage ne peut qu'etreindre le perimetre")
    check("une reponse claire est respectee", router.pick("transaction") == TRANSACTION)
    for reply in ("knowledge", "Je pense qu'il s'agit d'une operation", "", None, "42"):
        check(f"une reponse illisible ({reply!r}) retombe sur l'agent information",
              router.pick(reply) == KNOWLEDGE)


def test_handoff_is_performed_by_the_supervisor():
    print("\n[14] Le passage de main est execute par Python, pas par un modele")
    session = customer()
    result = tools.execute("handoff_to_transaction", {}, session, KNOWLEDGE)
    check("l'agent demande le passage de main", result["handoff"] == TRANSACTION)
    agent = Agent.__new__(Agent)          # no client: only the supervisor's own logic is exercised
    agent.messages, agent.role = [], KNOWLEDGE
    agent._record({"name": "handoff_to_transaction", "id": "x"}, result)
    check("le superviseur applique le changement de perimetre", agent.role == TRANSACTION)
    check("et la trace du passage reste dans la conversation",
          agent.messages[-1]["name"] == "handoff_to_transaction")


def test_escalation_opens_a_ticket_with_the_right_advisor():
    print("\n[15] L'escalade est une action, pas un numero de telephone")
    session = customer()
    first = tools.execute("escalate_to_advisor",
                          {"reason": "Renegociation du taux immobilier"}, session, KNOWLEDGE)
    check("un ticket est ouvert", first["ticket_id"] == "CB-00001")
    check("il porte le conseiller attitre du client", first["advisor"] == "Sophie Marchand")
    check("avec ses creneaux, pour que le modele les relaie", "visio" in first["slots"])
    ticket = json.loads(banking.CALLBACKS.read_text().splitlines()[0])
    check("le motif est persiste pour le conseiller",
          ticket["reason"] == "Renegociation du taux immobilier")
    second = tools.execute("escalate_to_advisor", {"reason": "Question fiscale"},
                           customer("C002"), TRANSACTION)
    check("un autre client est route vers son propre conseiller",
          second["advisor"] == "Karim Benali")
    check("l'escalade exige un consentement comme toute ecriture",
          tools.TOOLS["escalate_to_advisor"]["confirm"] is True)


# --- 5. traceability and output guardrail -----------------------------------

def test_every_operation_on_customer_data_is_audited():
    print("\n[16] Piste d'audit")
    session = customer()
    tools.execute("search_knowledge_base", {"query": "horaires"}, session, TRANSACTION)
    check("la recherche documentaire n'est pas tracee comme une operation",
          not banking.AUDIT.exists())
    tools.execute("get_balance", {}, session, TRANSACTION)
    tools.execute("block_card", {"card_last4": "4417"}, session, TRANSACTION)
    entries = [json.loads(line) for line in banking.AUDIT.read_text().splitlines()]
    check("la consultation du solde est tracee", entries[0]["action"] == "get_balance")
    check("le blocage est trace avec client, arguments et issue",
          (entries[1]["customer_id"], entries[1]["action"],
           entries[1]["payload"], entries[1]["outcome"])
          == ("C001", "block_card", {"card_last4": "4417"}, "ok"))
    tools.execute("increase_payment_capacity", {"new_limit_eur": 20000}, session, TRANSACTION)
    last = json.loads(banking.AUDIT.read_text().splitlines()[-1])
    check("un refus est trace au meme titre qu'un succes", last["outcome"] == "exceeds_policy")


def test_output_guardrail_masks_iban_and_card_numbers():
    print("\n[17] Garde-fou de sortie")
    masked = guardrails.mask("Votre IBAN est FR76 3000 4000 0312 3456 7890 143, "
                             "carte 4970 1234 5678 9010.")
    check("l'IBAN est masque", "3000 4000 0312" not in masked and "0143" in masked)
    check("le numero de carte est masque", "4970 1234 5678 9010" not in masked)
    check("un texte sans donnee sensible est intact",
          guardrails.mask("Bonjour, que puis-je pour vous ?")
          == "Bonjour, que puis-je pour vous ?")


# --- 6. API unavailability --------------------------------------------------

class RateLimited(Exception):
    """The shape of the SDKError raised by mistralai on HTTP 429."""

    def __init__(self):
        super().__init__('API error occurred: Status 429. Body: {"message":"Rate limit exceeded"}')
        self.status_code = 429
        # A capacity throttle reports a limit of 0 even when the key's own quota is intact.
        self.headers = {"retry-after": "2", "x-ratelimit-limit-req-minute": "0"}


def test_rate_limit_becomes_a_typed_recoverable_error():
    print("\n[18] HTTP 429 -> erreur typee, message client sans jargon")
    err = ModelUnavailable(RateLimited())
    check("le statut est reconnu", err.status == 429)
    check("le delai Retry-After est remonte", err.retry_after == 2.0)
    check("le quota observe est conserve pour les logs",
          err.quota == {"limit-req-minute": "0"})
    check("le message client est en francais et sans jargon",
          "surcharge" in err.user_message and "429" not in err.user_message)
    check("le delai est annonce au client", "2 secondes" in err.user_message)


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    banking.reset()
    print("\nTous les controles passent.")
