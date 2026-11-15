from __future__ import annotations
import json, httpx

SYSTEM = (
    "Sei l'assistente personale dell'utente. Rispondi SOLO usando le FONTI fornite. "
    "Cita ogni affermazione con il numero della fonte tra parentesi quadre, es. [1]. "
    "Se le fonti non contengono la risposta, dì chiaramente che non l'hai trovata nei documenti. "
    "Rispondi nella lingua della domanda."
)


class ChatError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Ollama {status}: {body}")
        self.status, self.body = status, body

    @property
    def no_tools(self):
        return "does not support tools" in self.body.lower()


def build_messages(question, hits, history=None):
    ctx = "\n\n".join(f"[{i}] ({h['location']} — {h['title']}"
                      f"{', pag. ' + str(h['page']) if h['kind'] in ('pdf','pptx','xlsx') else ''})\n{h['text']}"
                      for i, h in enumerate(hits, 1))
    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += (history or [])[-6:]
    msgs.append({"role": "user", "content": f"FONTI:\n{ctx or '(nessuna fonte trovata)'}\n\nDOMANDA: {question}"})
    return msgs


def chat_events(host, model, messages, tools=None, options=None):
    """Flusso grezzo di /api/chat di Ollama (una riga JSON per evento), con strumenti opzionali."""
    payload = {"model": model, "messages": messages, "stream": True, "keep_alive": "30m", "options": options or {}}
    if tools:
        payload["tools"] = tools
    with httpx.stream("POST", f"{host.rstrip('/')}/api/chat", json=payload, timeout=None) as r:
        if r.status_code >= 400:
            raise ChatError(r.status_code, r.read().decode("utf8", "ignore")[:400])
        for line in r.iter_lines():
            if line:
                yield json.loads(line)


def stream_chat(host, model, messages, options=None):
    for d in chat_events(host, model, messages, options=options):
        if d.get("message", {}).get("content"):
            yield d["message"]["content"]


def list_models(host):
    r = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=10)
    r.raise_for_status()
    return sorted(m["name"] for m in r.json().get("models", []))
