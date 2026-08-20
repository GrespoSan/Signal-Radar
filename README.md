# Signal Radar V2.4

V2.4 aggiunge l'estrazione automatica **Entry / Zona, Stop e Target** dagli screenshot TradingView/WhatsApp, mantenendo il prezzo corrente online automatico della V2.3.

## Obiettivo

Trasformare una sequenza di screenshot in pochi setup leggibili e capire subito:

- qual è l'asset e il bias;
- qual è la zona operativa;
- dove si trova il prezzo attuale;
- quanto manca all'Entry;
- se il setup è ancora da attendere, in zona, già passato o da ricontrollare.

## Come vengono ricavati i livelli

V2.4 non usa il solo OCR del numero. Combina:

1. parole operative (`Entry`, `Entry Short`, `Entry Long`, `Zona/Area da attenzionare`, `Stop`, `Target`, `T1 Tecnico`);
2. posizione verticale della scritta nel grafico;
3. OCR mirato della colonna prezzi a destra;
4. stima della relazione **pixel verticale → prezzo**;
5. riconoscimento di bande gialle/verdi vicine alla zona operativa;
6. prezzo online e range plausibile dell'asset per eliminare letture assurde.
7. un secondo passaggio **OpenCV adaptive** per recuperare, quando possibile, le cifre bianche nei box verdi/rossi di TradingView.

## Confidence livelli

- `🟢 AUTO HIGH` → Entry sufficientemente robusta: `Livelli OK` è già selezionato e il livello può essere salvato automaticamente.
- `🟡 VERIFY ENTRY` → il motore ha una proposta, ma deve essere controllata sul grafico. Correggi se necessario e spunta `Livelli OK`.
- `🟡 PARZIALE` → è stato ricavato Stop/Target ma non una Entry abbastanza robusta.
- `⚪ LIVELLI N/D` → nessun livello affidabile: il setup può comunque essere importato senza numeri.

**Importante:** se `Livelli OK` non è selezionato, Entry/Stop/Target proposti non vengono scritti nel setup. È una protezione intenzionale contro numeri OCR errati.

## Nuovo zoom di controllo

Nell'anteprima dell'import multiplo V2.4 mostra anche uno **zoom automatico dell'area livelli / asse prezzi**, così una proposta MEDIUM può essere verificata rapidamente senza cercare manualmente la zona nello screenshot completo.

## Prezzi online

Il prezzo corrente resta automatico con cache di circa 90 secondi. Il campo manuale compare solo quando la fonte online non è disponibile o non supera i controlli di coerenza.

Per DAX e FESX il sistema non sostituisce il future con l'indice spot: se il dato future gratuito non è affidabile passa al fallback manuale.

## Aggiornamento da V2.3

Sostituisci nel repository:

- `app.py`
- `requirements.txt`
- `packages.txt`
- `asset_whitelist.txt`

Mantieni `data/` e `assets/` se vuoi conservare il database corrente. La V2.4 aggiunge `opencv-python-headless` alle dipendenze; al primo deploy Streamlit Cloud può quindi impiegare un po’ più del solito.

## Flusso consigliato

1. Carica gli screenshot in **⚡ Import multiplo**.
2. Premi **Analizza e proponi**.
3. Controlla soprattutto righe con `⚠` e `🟡 VERIFY`.
4. Se correggi Asset/Bias/Categoria, premi **Ricalcola destinazioni + livelli**.
5. Per un livello MEDIUM, usa lo zoom, correggi i numeri se necessario e spunta `Livelli OK`.
6. Controlla i **Setup finali proposti**, con prezzo corrente e validità preliminare.
7. Premi **IMPORTA TUTTO**.
8. In **Active Signals** il Radar confronta prezzo corrente con Entry/Stop/Target e mostra la distanza dalla zona.

## Limite importante

Gli screenshot WhatsApp comprimono molto le piccole etichette dei prezzi. Per questo V2.4 è volutamente conservativa: un livello dubbio resta `VERIFY` o `N/D` invece di essere promosso artificialmente a livello operativo.

Signal Radar organizza e controlla segnali ricevuti; non genera segnali di trading.
