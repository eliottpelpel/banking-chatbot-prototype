"""Terminal chat client. Run: python3 cli.py"""
import banking
from agent import Agent, ModelUnavailable, Session
from config import DISCLAIMER

BANNER = f"""
=========================================================
  BNP Paribas - Assistant Banque de Detail (prototype)
  /login C001|C002   s'authentifier (SCA simulee)
  /logout  /reset  /quit
=========================================================
Session anonyme : informations publiques uniquement.
{DISCLAIMER}
"""

YES = ("o", "oui", "y", "yes")


def telemetry(stats):
    """The per-turn numbers, printed on every answer: what you do not measure, you cannot budget."""
    return (f"  [{stats['role']} · {stats['calls']} appel(s) modele · {stats['total_ms']} ms "
            f"dont {stats['model_ms']} ms modele · {stats['tokens_in']} tokens entree / "
            f"{stats['tokens_out']} sortie · {' -> '.join(stats['models'])}]")


def confirmed(event):
    print(f"\n  /!\\ CONFIRMATION REQUISE\n      {event['preview']}")
    return input("      Confirmer ? [o/n] ").strip().lower() in YES


def drive(agent, step):
    """Run a turn to completion; a retry resumes it without replaying a confirmed operation."""
    while True:
        try:
            event = step()
            while event["type"] == "confirm":
                event = agent.confirm(confirmed(event))
            return event
        except ModelUnavailable as exc:
            print(f"\n  /!\\ {exc.user_message}")
            if input("      Reessayer ? [o/n] ").strip().lower() not in YES:
                return None
            step = agent.retry


def main():
    print(BANNER)
    session = Session()
    agent = Agent(session)

    while True:
        try:
            text = input(f"\n[{session.name or 'anonyme'}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break

        if text.startswith("/"):
            if text == "/reset":
                banking.reset()
                print("Etat de demonstration reinitialise.")
            elif text == "/logout":
                session.logout()
                print("Session fermee, retour au niveau public.")
            elif text.startswith("/login"):
                cid = text.split()[-1].upper()
                print(f"Authentification forte validee : {session.name} ({cid})." if session.login(cid)
                      else "Client inconnu (C001 ou C002).")
            else:
                print("Commandes : /login /logout /reset /quit")
            agent = Agent(session)      # a command changes the session: start a clean turn
            continue

        event = drive(agent, lambda: agent.send(text))
        if event:
            print(f"\nAssistant : {event['text']}")
            print(telemetry(event["stats"]))
            print(f"  ({DISCLAIMER})")


if __name__ == "__main__":
    main()
