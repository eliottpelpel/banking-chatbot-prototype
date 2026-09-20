"""Permission-aware retrieval over two corpora."""
import json
import re
from config import DATA_DIR, CUSTOMER

PUBLIC_DOCS = json.loads((DATA_DIR / "public_kb.json").read_text())
CUSTOMER_DOCS = json.loads((DATA_DIR / "customer_kb.json").read_text())

_STOP = {"le", "la", "les", "de", "des", "du", "un", "une", "et", "est", "a",
         "mon", "ma", "mes", "je", "quel", "quelle", "quels", "quelles", "pour",
         "sur", "que", "qui", "the", "my", "what", "is", "are", "of", "to", "i"}


def _tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2]


def _score(tokens, doc):
    """Lexical overlap, tags weighing more than body. Production: mistral-embed + vector store."""
    tags = " ".join(doc.get("tags", [])).lower()
    body = (doc["title"] + " " + doc["text"]).lower()
    return sum(3.0 * (t in tags) + 1.0 * (t in body) for t in tokens)


def search(query, access_level, customer_id=None, top_k=3):
    """Filter by permission first, then score: a customer doc can never reach a public session."""
    corpus = list(PUBLIC_DOCS)
    if access_level == CUSTOMER and customer_id:
        corpus += [d for d in CUSTOMER_DOCS if d["customer_id"] == customer_id]
    tokens = _tokens(query)
    scored = [(_score(tokens, d), d) for d in corpus]
    return [d for s, d in sorted(scored, key=lambda x: -x[0]) if s > 0][:top_k]
