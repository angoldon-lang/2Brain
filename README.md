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

## Sorgenti (si gestiscono dalla scheda **Sorgenti**)
| Sorgente | Come si aggiunge | Stato |
|---|---|---|
| OneDrive / SharePoint | Rilevati in automatico (sincronizzati dall'app OneDrive, in `~/Library/CloudStorage/`): un clic su «Aggiungi». I file «solo online» sono saltati. | ✅ |
| Cartella del Mac | «Scegli una cartella…» (finestra nativa di Finder) o percorso a mano | ✅ |
| Outlook PST | «Scegli un file .pst…» (richiede `brew install libpst`) | ✅ |
| Outlook 365 (posta online) | Accesso Microsoft dalla scheda (codice da inserire su microsoft.com/devicelogin), sola lettura, ultimi N mesi, esclusi Eliminata e Posta indesiderata. «Apri in Outlook» dal messaggio. | ✅ (provato con server Graph simulato) |
| Teams, SharePoint senza sincronizzazione | stessa app Entra, permessi aggiuntivi | ⏳ |

Le sorgenti di `config.toml` sono importate **una sola volta** al primo avvio dopo l'aggiornamento (solo se i percorsi esistono); poi si gestiscono dall'interfaccia. «Rimuovi» toglie la sorgente e il suo indice, mai i file originali.

### Outlook 365: setup una tantum (serve registrare un'app in Microsoft Entra)
Microsoft non permette a un'app di leggere la posta senza un'«app registrata» dell'azienda. In 5 minuti:
1. entra.microsoft.com → Applicazioni → Registrazioni app → **Nuova registrazione** (nome «SecondBrain», «Solo questa directory»; Redirect URI tipo *Client pubblico/nativo*: `https://login.microsoftonline.com/common/oauth2/nativeclient`).
2. **Autenticazione** → «Consenti flussi client pubblici» = Sì.
3. **Autorizzazioni API** → Microsoft Graph → Delegate → `Mail.Read` (se richiesto, consenso dell'amministratore).
4. Copia «ID applicazione (client)» e «ID directory (tenant)» nella scheda Sorgenti → **Accedi con Microsoft**.

Se l'IT blocca la registrazione di app o il consenso, l'accesso alla posta 365 non è possibile: in quel caso resta l'esportazione in PST.
Il token resta in `~/.secondbrain/m365_token.json` (permessi 600). «Disconnetti» lo cancella.

## Ricerca e indicizzazione
**Due fasi**: (1) lettura di tutti i file → subito ricercabili **per parole**; (2) in coda, la ricerca **per significato** (embedding `bge-m3`), dai file più recenti, riprendendo dopo un riavvio. L'avanzamento è sempre visibile; si può cercare in qualsiasi momento.

Qualità dei risultati:
- le parole vuote («mi», «le», «fatte»…) sono ignorate: contano solo i termini che distinguono un documento;
- prima chi contiene **tutte** le parole importanti, poi chi ne contiene alcune; prefissi (*offerte → offerta/offerto*);
- il **nome del file e il percorso** fanno parte del contenuto (es. `OFF_ADC_Bper_Solarwinds.pdf` si trova cercando «bper»);
- con «ultima/ultime/recenti» i documenti più recenti sono premiati;
- parole chiave + significato sono fuse (RRF); max 2 frammenti per documento.
Per togliere rumore (giornali, progetti di sviluppo) usa le **esclusioni** per sorgente.

Memoria: i vettori stanno in RAM (circa 4 KB per frammento: 200.000 frammenti ≈ 800 MB). Oltre, conviene un indice dedicato (sqlite-vec / LanceDB).
I fogli Excel sono indicizzati con i primi 150 frammenti (solo righe con testo); nell'anteprima si vedono come tabella con la riga trovata evidenziata.

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

## Cosa significano gli avvisi (scheda Stato file)
| Messaggio | Significato / cosa fare |
|---|---|
| **indicizzato solo in parte** | File molto grande (es. fogli di costi con migliaia di righe): sono indicizzati i primi 400 frammenti. Informativo, il file resta cercabile. |
| **scansione senza testo / OCR** | PDF fatto di immagini. Su macOS l'OCR locale (Apple Vision) si installa con `pip install -e .` (pyobjc) e legge le prime 40 pagine. |
| **protetto da password** | File Office con password vera: non leggibile. I file «sola lettura consigliata» invece si leggono. |
| formato 97-2003 (.doc/.ppt) | Salvali come .docx/.pptx. |
Righe di soli numeri nei fogli Excel e file vuoti (log, `__init__`…) vengono ignorati senza avviso. I file falliti **non** sono riprovati a ogni avvio, solo se cambiano o se migliora l'app.

### Escludere cartelle (privacy e rumore)
Scheda **Sorgenti → Sorgenti attive → «Escludi cartelle o file»**: una regola per riga (`Personale`, `Projects`, `*.tmp`).
Ciò che escludi esce anche dall'indice. Sempre esclusi: `node_modules`, `.git`, `*.log`, `package-lock.json`, `*.egg-info`…
Consiglio: escludi documenti personali (identità, dichiarazioni) prima di attivare la modalità «sostituto» su Teams.

## Test
`pip install -e ".[dev]" && pytest`
