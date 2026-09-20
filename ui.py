"""Streamlit chat interface. Run: streamlit run ui.py"""
import streamlit as st

import banking
from agent import Agent, ModelUnavailable, Session
from config import CUSTOMER, DISCLAIMER, MODEL

st.set_page_config(page_title="BNP Paribas - Assistant", page_icon="🏦")


def start(session=None):
    st.session_state.session = session or Session()
    st.session_state.agent = Agent(st.session_state.session)
    st.session_state.history = []
    st.session_state.pending = None
    st.session_state.stalled = None
    st.session_state.stats = None


def decide(approved):
    """Answer a pending confirmation."""
    st.session_state.pending = None
    run(lambda: st.session_state.agent.confirm(approved))
    st.rerun()


def run(step):
    """One agent step. A rate limit becomes a banner with a retry button: the turn is resumed, not replayed."""
    try:
        event = step()
    except ModelUnavailable as exc:
        st.session_state.stalled = exc.user_message
        return
    st.session_state.stalled = None
    if event["type"] == "confirm":
        st.session_state.pending = event
    else:
        st.session_state.history.append(("assistant", event["text"]))
        st.session_state.stats = event["stats"]


if "session" not in st.session_state:
    start()
sess = st.session_state.session

with st.sidebar:
    st.subheader("Session")
    st.caption(f"Modele : {MODEL}")
    if sess.access_level == CUSTOMER:
        st.success(f"Authentifie : {sess.name} ({sess.customer_id})")
        if st.button("Se deconnecter"):
            sess.logout()
            start(sess)
            st.rerun()
    else:
        st.warning("Session anonyme - informations publiques uniquement")
        cid = st.selectbox("Simuler une authentification forte", ["C001", "C002"])
        if st.button("S'authentifier"):
            sess.login(cid)
            start(sess)
            st.rerun()
    stats = st.session_state.get("stats")
    if stats:
        st.divider()
        st.subheader("Dernier tour")
        st.caption(f"Agent : **{stats['role']}** · {stats['calls']} appel(s) modele")
        left, right = st.columns(2)
        left.metric("Latence", f"{stats['total_ms']} ms", f"{stats['model_ms']} ms modele",
                    delta_color="off")
        right.metric("Tokens", f"{stats['tokens_in'] + stats['tokens_out']}",
                     f"{stats['tokens_in']} in / {stats['tokens_out']} out", delta_color="off")
        st.caption(" → ".join(stats["models"]))
    st.divider()
    if st.button("Reinitialiser la demo"):
        banking.reset()
        st.session_state.clear()
        st.rerun()

st.title("Assistant Banque de Detail")

for role, text in st.session_state.history:
    st.chat_message(role).write(text)

# A pending confirmation blocks the conversation until the user decides.
if st.session_state.pending:
    with st.chat_message("assistant"):
        st.warning(f"**Confirmation requise**\n\n{st.session_state.pending['preview']}")
        left, right, _ = st.columns([1, 1, 3])
        if left.button("Confirmer", type="primary"):
            decide(True)
        if right.button("Annuler"):
            decide(False)

# Transient API failure: the turn is held, not lost.
if st.session_state.stalled:
    st.error(st.session_state.stalled)
    if st.button("Reessayer"):
        run(st.session_state.agent.retry)
        st.rerun()

prompt = st.chat_input("Posez votre question...", disabled=bool(st.session_state.pending))
st.caption(DISCLAIMER)
if prompt:
    st.session_state.history.append(("user", prompt))
    run(lambda: st.session_state.agent.send(prompt))
    st.rerun()
