"""Simulated core banking. The only place customer state is read or written."""
import json
import re
import unicodedata
from datetime import datetime
from config import DATA_DIR, MAX_CARD_LIMIT_EUR, MAX_LIMIT_INCREASE_RATIO

SEED = DATA_DIR / "customers.json"
STATE = DATA_DIR / "_runtime_state.json"
AUDIT = DATA_DIR / "_audit_log.jsonl"
CALLBACKS = DATA_DIR / "_callbacks.jsonl"

ADVISORS = json.loads((DATA_DIR / "advisors.json").read_text())


def _load():
    return json.loads((STATE if STATE.exists() else SEED).read_text())


def _save(db):
    STATE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


def _err(error, message, **extra):
    return {"ok": False, "error": error, "message": message, **extra}


def reset():
    for path in (STATE, AUDIT, CALLBACKS):
        path.unlink(missing_ok=True)


def audit(customer_id, action, payload, outcome):
    """Append-only trace. In production: immutable store, 10-year retention."""
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "customer_id": customer_id,
             "action": action, "payload": payload, "outcome": outcome}
    with AUDIT.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_customer(customer_id):
    return _load().get(customer_id)


def _find_card(customer, card_last4):
    """The card named by its last 4 digits, or the only one the customer holds."""
    cards = customer["cards"]
    if card_last4:
        return next((c for c in cards if c["last4"] == str(card_last4)), None)
    return cards[0] if len(cards) == 1 else None


def _normalize(text):
    """Lowercased, accent- and punctuation-free form, used to compare names."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def _match_beneficiaries(beneficiaries, query):
    """An exact name wins; otherwise every word typed must start a word of the name."""
    words = _normalize(query).split()
    if not words:
        return []
    exact = [b for b in beneficiaries if _normalize(b["name"]) == " ".join(words)]
    return exact or [b for b in beneficiaries
                     if all(any(w.startswith(word) for w in _normalize(b["name"]).split())
                            for word in words)]


# --- reads -----------------------------------------------------------------

def get_balance(customer_id, account_type=None):
    accounts = get_customer(customer_id)["accounts"]
    if account_type:
        accounts = [a for a in accounts if account_type.lower() in a["type"].lower()] or accounts
    return {"ok": True, "accounts": [
        {"type": a["type"], "iban_last4": a["iban"][-4:], "balance_eur": a["balance_eur"],
         "authorized_overdraft_eur": a["authorized_overdraft_eur"]} for a in accounts]}


def get_credit_balance(customer_id):
    keep = ("credit_id", "type", "outstanding_eur", "monthly_payment_eur",
            "rate_pct", "remaining_months", "end_date")
    credits = get_customer(customer_id)["credits"]
    out = {"ok": True, "credits": [{k: c[k] for k in keep} for c in credits]}
    if not credits:
        out["note"] = "Aucun credit en cours."
    return out


def list_beneficiaries(customer_id, name=None):
    """Registered SEPA beneficiaries, optionally filtered by name. IBANs stay truncated."""
    beneficiaries = get_customer(customer_id)["beneficiaries"]
    matches = _match_beneficiaries(beneficiaries, name) if name else beneficiaries
    out = {"ok": True, "beneficiaries": [
        {"name": b["name"], "iban_last4": b["iban"][-4:], "bank": b["bank"]} for b in matches]}
    if not matches:
        out["note"] = ("Aucun beneficiaire enregistre ne correspond a cette recherche."
                       if name else "Aucun beneficiaire enregistre.")
        out["known_beneficiaries"] = [b["name"] for b in beneficiaries]
    return out


# --- writes ----------------------------------------------------------------

_CARD_NOT_FOUND = "Carte introuvable ou plusieurs cartes : precisez les 4 derniers chiffres."


def _set_card_status(customer_id, card_last4, status, message):
    db = _load()
    card = _find_card(db[customer_id], card_last4)
    if not card:
        return _err("card_not_found", _CARD_NOT_FOUND)
    if card["status"] == status:
        return {"ok": True, "already": True, "card_last4": card["last4"],
                "message": f"Cette carte est deja {'bloquee' if status == 'blocked' else 'active'}."}
    card["status"] = status
    _save(db)
    return {"ok": True, "card_last4": card["last4"], "status": status, "message": message}


def block_card(customer_id, card_last4=None):
    return _set_card_status(customer_id, card_last4, "blocked",
                            "Blocage temporaire actif immediatement. Reversible a tout moment.")


def unblock_card(customer_id, card_last4=None):
    return _set_card_status(customer_id, card_last4, "active",
                            "Carte reactivee, utilisable immediatement.")


def increase_payment_capacity(customer_id, new_limit_eur, card_last4=None):
    """Policy check lives here, not in the prompt: deterministic and auditable."""
    db = _load()
    card = _find_card(db[customer_id], card_last4)
    if not card:
        return _err("card_not_found", _CARD_NOT_FOUND)
    current, new_limit = card["monthly_payment_limit_eur"], float(new_limit_eur)
    if new_limit <= current:
        return _err("not_an_increase", f"Votre plafond actuel est deja de {current:.0f} EUR.")
    ceiling = min(MAX_CARD_LIMIT_EUR, current * MAX_LIMIT_INCREASE_RATIO)
    if new_limit > ceiling:
        return _err("exceeds_policy",
                    f"Une hausse a {new_limit:.0f} EUR depasse ce que je peux accorder en autonomie "
                    f"(max {ceiling:.0f} EUR). Un conseiller doit valider cette demande.",
                    requires_advisor=True, current_limit_eur=current)
    card["monthly_payment_limit_eur"] = new_limit
    _save(db)
    return {"ok": True, "card_last4": card["last4"], "previous_limit_eur": current,
            "new_limit_eur": new_limit,
            "message": "Nouveau plafond de paiement actif immediatement, sur 30 jours glissants."}


def request_callback(customer_id, reason):
    """Open a callback ticket with the customer's own advisor.

    The handover has to be an action, not a phone number read aloud. In production
    this is a CRM case queued to the contact centre, carrying the conversation id so
    the advisor picks up where the assistant stopped.
    """
    customer = get_customer(customer_id)
    advisor = ADVISORS[customer["advisor_id"]]
    served = CALLBACKS.read_text().splitlines() if CALLBACKS.exists() else []
    ticket = f"CB-{len(served) + 1:05d}"
    with CALLBACKS.open("a") as f:
        f.write(json.dumps({"ticket_id": ticket, "customer_id": customer_id,
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "advisor_id": customer["advisor_id"], "reason": reason},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "ticket_id": ticket, "advisor": advisor["name"],
            "advisor_role": advisor["role"], "agency": advisor["agency"],
            "phone": advisor["phone"], "email": advisor["email"], "slots": advisor["slots"],
            "message": f"Demande de rappel enregistree sous la reference {ticket}. "
                       f"{advisor['name']} vous recontacte."}


def make_transfer(customer_id, beneficiary_name, amount_eur, label=None):
    """Four sequential checks, each with its own error code: amount, beneficiary, funds, limit."""
    db = _load()
    c = db[customer_id]
    amount = float(amount_eur)
    if amount <= 0:
        return _err("invalid_amount", "Le montant doit etre positif.")
    match = _match_beneficiaries(c["beneficiaries"], beneficiary_name)
    if not match:
        return _err("unknown_beneficiary",
                    "Beneficiaire inconnu. L'ajout d'un nouveau beneficiaire exige une "
                    "authentification forte dans l'application (delai de securite de 24h).",
                    known_beneficiaries=[b["name"] for b in c["beneficiaries"]])
    if len(match) > 1:
        return _err("ambiguous_beneficiary", "Plusieurs beneficiaires correspondent, precisez lequel.",
                    candidates=[b["name"] for b in match])
    account = c["accounts"][0]
    available = account["balance_eur"] + account["authorized_overdraft_eur"]
    if amount > available:
        return _err("insufficient_funds",
                    f"Provision insuffisante : {available:.2f} EUR disponibles (decouvert autorise inclus).",
                    available_eur=available)
    remaining = c["transfer_daily_limit_eur"] - c["transferred_today_eur"]
    if amount > remaining:
        return _err("daily_limit_exceeded",
                    f"Plafond de virement journalier atteint : {remaining:.2f} EUR restants aujourd'hui.",
                    remaining_today_eur=remaining, requires_advisor=True)
    account["balance_eur"] = round(account["balance_eur"] - amount, 2)
    c["transferred_today_eur"] = round(c["transferred_today_eur"] + amount, 2)
    _save(db)
    return {"ok": True, "beneficiary": match[0]["name"], "iban_last4": match[0]["iban"][-4:],
            "amount_eur": amount, "label": label or "Virement",
            "new_balance_eur": account["balance_eur"],
            "execution": "SEPA instantane, credite sous 10 secondes", "message": "Virement execute."}
