# SecondBrain — assistente locale su Mac

LLM **solo locali** (Ollama) che lavorano sui tuoi dati: cartelle, OneDrive/SharePoint, PST Outlook.
Ogni risposta cita le fonti con **percorso esatto, pagina e anteprima**. Nulla esce dal Mac.

## Perché non Open WebUI come base
Open WebUI è ottimo come chat, ma citazioni con anteprima del documento originale, connettori
e la modalità "sostituto" richiederebbero di forkarlo. Qui il backend è tuo (FastAPI + SQLite,
~600 righe) e usa Ollama per chat ed embedding. Resta compatibile: in fase 2 si espone un endpoint
OpenAI-compatibile se vuoi usare Open WebUI come front-end alternativo.

## Avvio (Mac Apple Silicon)
```bash
./scripts/setup_mac.sh        # ollama, libpst, modelli, venv
nano config.toml              # indica le tue sorgenti
. .venv/bin/activate && secondbrain serve   # http://127.0.0.1:8765
```

## Sorgenti
| Sorgente | Come | Stato |
|---|---|---|
| Disco locale | `type="folder"` | ✅ pdf, docx, xlsx, pptx, txt/md/csv, html, eml, msg |
| OneDrive / SharePoint | sincronizzati col client OneDrive → appaiono in `~/Library/CloudStorage/`, indicizzati come cartelle (nessuna autenticazione da gestire). I file "solo online" sono saltati. | ✅ |
| Outlook PST | `type="pst"`, convertito con `readpst` in cache, re-import se il PST cambia | ✅ (allegati: solo nomi) |
| Outlook/Mail 365 live, Teams chat | Microsoft Graph (device-code flow, token in Keychain) | ⏳ fase 1b |

Il "nuovo Outlook" per Mac non usa PST: per la posta 365 serve Graph (fase 1b).

## Ricerca
Ibrida: BM25 (SQLite FTS5) + embedding `bge-m3`, fusi con RRF; max 2 frammenti per documento.
Senza embedding disponibili degrada a sola parola chiave. Indicizzazione incrementale (mtime+size).
Il retrieval brute-force numpy va bene fino a ~200k frammenti; oltre, passare a sqlite-vec/LanceDB.

## Fase 2 — Sostituto su Teams (progetto, non ancora implementato)
- Bot Teams (Bot Framework) → tunnel in uscita verso il Mac; il Mac non apre porte.
- `/api/ask` accetta già `sources` per limitare il perimetro: ogni interlocutore avrà una **policy**
  (sorgenti consentite, cartelle escluse, es. mai HR/contratti).
- Risposte in nome tuo con etichetta "Assistente di <te>", citazione fonti, **bozza da approvare**
  per domande sensibili o a bassa confidenza, log di audit di ogni richiesta.
- Sicurezza: i contenuti indicizzati sono input non fidato (prompt injection da mail/doc): il modello
  in modalità delegata non ha strumenti con effetti, solo lettura e risposta.

## Interfaccia
Due schede: **Chiedi** (chat con fonti e anteprima) e **Stato file** (per ogni sorgente: avanzamento, file indicizzati per tipo, file saltati con il motivo, elenco filtrabile). La versione è nell’intestazione.

## Indicizzazione e anteprima
- Puoi fare domande **mentre indicizza**: la ricerca usa quanto è già nell'indice. Un avviso indica avanzamento e tempo stimato; i file **più recenti** sono indicizzati per primi.
- La prima indicizzazione di molte migliaia di file richiede ore (il collo di bottiglia sono gli embedding); poi è incrementale. I file enormi sono indicizzati solo nei primi 400 frammenti e segnalati.
- La scheda **Stato file** mostra anche lo **storico** delle indicizzazioni.
- Anteprima: "Testo trovato" (paragrafi originali, frammento evidenziato) per tutti i tipi; "Documento originale" per docx/xlsx/pptx richiede LibreOffice: `brew install --cask libreoffice` (opzionale). I PDF si aprono direttamente alla pagina giusta.

## Test
`pip install -e ".[dev]" && pytest`
