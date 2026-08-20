# Signal Radar V1

Obiettivo: trasformare screenshot e messaggi WhatsApp di trading in un registro operativo ordinato, con **una riga per setup** e una timeline di tutti gli aggiornamenti.

## Cosa fa la V1

- Dashboard **Active Signals** con WATCH / READY / TRIGGERED.
- Raggruppamento per setup: 6N, DAX, 6J, FESX, ES.
- Colonna **PROSSIMA AZIONE**.
- Validità/scadenza automatica dei setup WATCH/READY.
- Timeline cronologica di immagini e messaggi per ogni setup.
- Upload di nuovi screenshot con tre scelte:
  1. aggiornamento di setup esistente;
  2. nuovo setup;
  3. solo INFO/didattica.
- Modifica di stato, entry, stop, target e prossima azione.
- Export CSV.
- Dataset demo già caricato usando i 17 screenshot WhatsApp forniti.

## Avvio locale

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

Carica l'intera cartella in un repository GitHub e seleziona `app.py` come file principale.

**Nota importante:** il filesystem di Streamlit Community Cloud non è pensato come archivio permanente. La V1 va benissimo per provare il flusso; per l'uso reale conviene collegare in V2 un database persistente gratuito (es. Supabase) o altro storage.

## Perché la V1 non legge ancora automaticamente i prezzi dalle immagini

Per un'app di trading è meglio non salvare automaticamente livelli numerici letti male da uno screenshot. La V1 imposta il flusso corretto: classificazione, raggruppamento, stato e cronologia; i livelli numerici vengono confermati manualmente.

La V2 può aggiungere un estrattore AI che **propone** asset, bias, entry, stop, target e collegamento al setup esistente, lasciando sempre una conferma umana prima del salvataggio.
