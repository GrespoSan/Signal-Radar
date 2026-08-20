# Signal Radar V2.2

V2.2 aggiunge il **prezzo corrente per asset** e un controllo di validità del setup rispetto a Entry/Zona, Stop e Target.

## Novità V2.2

- Nuovo tab **💹 Prezzi**.
- Prezzo manuale persistente per ogni asset (consigliato: stesso future/continuous usato nel segnale, ad es. TradingView `ES1!`).
- Pulsante opzionale **Aggiorna prezzi Yahoo** per alcuni futures supportati.
- I prezzi Yahoo sono sempre marcati **indicativi** e non vengono usati per invalidare automaticamente uno stop.
- Dashboard Active Signals con nuove colonne:
  - Prezzo attuale
  - Distanza dalla zona Entry
  - Validità prezzo
- Nel dettaglio setup compare una scheda che confronta prezzo corrente, Entry, Stop e Target.
- Il prezzo può essere aggiornato anche direttamente dal dettaglio del setup.
- La preview dell'import multiplo mostra il prezzo corrente e un check preliminare, se già disponibile.

## Stati del controllo prezzo

Il controllo è volutamente conservativo:

- `⚪ PREZZO N/D` → prezzo non inserito.
- `⚠ LIVELLI N/D` → prezzo presente, ma Entry/Zona non è numerica o manca.
- `🟢 IN ZONA ENTRY` → prezzo dentro la zona indicata.
- `🟡 ATTESA PULLBACK` → setup LONG con prezzo ancora sopra la zona.
- `🟡 ATTESA RIMBALZO` → setup SHORT con prezzo ancora sotto la zona.
- `🟠 SOTTO ENTRY / SOPRA ENTRY` → il prezzo ha oltrepassato la zona nella direzione sfavorevole; richiede revisione.
- `⚪ TARGET SUPERATO` → il prezzo ha già oltrepassato il target; non va considerato una nuova entry senza revisione.
- `🔴 INVALIDATO` → solo con **prezzo affidabile** e Stop numerico violato.

Il prefisso `≈` indica che il prezzo è solo indicativo (ad esempio Yahoo).

## Perché il prezzo manuale resta la fonte principale

I segnali delle immagini sono spesso costruiti su futures continuous (`ES1!`, `6N1!`, DAX/Eurex, ecc.). Un prezzo gratuito esterno può essere ritardato oppure riferito a un contratto diverso. Per questo:

1. il prezzo manuale preso dallo stesso grafico TradingView/broker è considerato affidabile;
2. Yahoo è un aiuto gratuito per il colpo d'occhio, non una fonte per invalidare automaticamente un trade;
3. per DAX e FESX non vengono usati automaticamente proxy spot, perché potrebbero non essere confrontabili con i livelli futures.

## Aggiornamento da V2.1 / V2.1.1

Sostituisci nel repository:

- `app.py`
- `requirements.txt`
- `packages.txt`
- `asset_whitelist.txt`

Mantieni le cartelle `data/` e `assets/`. Il database viene migrato automaticamente aggiungendo la tabella `prices`.

`requirements.txt` contiene ora anche `yfinance` per l'aggiornamento gratuito opzionale.

## Flusso consigliato

1. Importa e raggruppa gli screenshot.
2. Conferma manualmente Asset, Bias, Entry/Zona, Stop e Target quando il segnale è operativo.
3. Vai in **💹 Prezzi** e inserisci i prezzi correnti dei setup attivi.
4. Torna in **🎯 Active Signals**: ordina le occasioni usando `VALIDITÀ PREZZO` e `Dist. Entry`.
5. Aggiorna i prezzi quando vuoi fare una nuova revisione dei segnali.

Signal Radar organizza e controlla segnali ricevuti; non genera segnali di trading.
