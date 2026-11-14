from __future__ import annotations
import json, httpx

SYSTEM = (
    "Sei l'assistente personale dell'utente. Rispondi SOLO usando le FONTI fornite. "
    "Cita ogni affermazione con il numero della fonte tra parentesi quadre, es. [1]. "
    "Se le fonti non contengono la risposta, dì chiaramente che non l'hai trovata nei documenti. "
    "Rispondi nella lingua della domanda."
)


def build_messages(question, hits, history=None):
    ctx = "\n\n".join(f"[{i}] ({h['location']} — {h['title']}"
                      f"{', pag. ' + str(h['page']) if h['kind'] in ('pdf','pptx','xlsx') else ''})\n{h['text']}"
                      for i, h in enumerate(hits, 1))
    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += (history or [])[-6:]
    msgs.append({"role": "user", "content": f"FONTI:\n{ctx or '(nessuna fonte trovata)'}\n\nDOMANDA: {question}"})
    return msgs


def stream_chat(host, model, messages):
    with httpx.stream("POST", f"{host.rstrip('/')}/api/chat", timeout=None,
                      json={"model": model, "messages": messages, "stream": True}) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                d = json.loads(line)
                if d.get("message", {}).get("content"):
                    yield d["message"]["content"]
