# Signal Radar V2.1

V2.1 corregge i problemi emersi nel primo test reale della V2: falsi asset OCR, setup duplicati, anni letti male e collegamenti troppo deboli tra immagini dello stesso segnale.

## Novità V2.1

- **Whitelist asset** in `asset_whitelist.txt`: l'OCR non può più inventare ticker casuali da frammenti di testo.
- OCR a **due passaggi**: titolo/header per asset e bias, immagine completa per contesto.
- Bias più conservativo: `Emo Long/Short` non viene usato da solo per decidere LONG/SHORT.
- Correzione anni OCR improbabili usando l'anno del file WhatsApp come riferimento.
- Raggruppamento cronologico per completare asset/periodo mancanti in una sequenza ravvicinata.
- Collegamento più aggressivo a un **setup esistente** quando asset e periodo coincidono.
- Colonna **Verifica**: le righe incerte vengono evidenziate e, se manca l'asset, non sono importate automaticamente.
- Pulsante **Ricalcola destinazioni dopo le correzioni**: utile quando correggi manualmente un asset (es. 6J) e vuoi che l'app lo colleghi al setup già esistente.
- Anteprima **Setup finali proposti** prima dell'importazione.
- Deduplica corretta anche per gli screenshot iniziali: V2.1 calcola gli hash mancanti delle immagini seed.
- Manutenzione: **Ripristina baseline test** per eliminare i duplicati creati dal primo test V2 e tornare ai setup iniziali corretti.

## Aggiornamento da V2

Sostituisci nel repository almeno:

- `app.py`
- `requirements.txt`
- `packages.txt`
- `asset_whitelist.txt`

Mantieni anche le cartelle `data/` e `assets/` presenti nello ZIP.

Dopo il deploy, se nella dashboard vedi ancora i falsi setup/duplicati creati dalla V2:

1. vai in **Archivio**;
2. apri **Manutenzione V2.1 — ripristino test iniziale**;
3. spunta la conferma;
4. premi **RIPRISTINA BASELINE TEST**.

Questo reset è pensato per il test iniziale attuale. Non usarlo in futuro dopo aver iniziato ad archiviare segnali reali che vuoi conservare.

## Flusso Import multiplo

1. Carica gli screenshot WhatsApp insieme.
2. Premi **Analizza e proponi**.
3. Controlla soprattutto le righe con `⚠` nella colonna **Verifica**.
4. Se correggi Asset/Bias/Periodo, premi **Ricalcola destinazioni dopo le correzioni**.
5. Controlla la tabella **Setup finali proposti**.
6. Conferma Entry/Stop/Target manualmente quando servono.
7. Premi **IMPORTA TUTTO**.

## Nota importante

Signal Radar organizza segnali ricevuti; non genera segnali di trading e non considera mai affidabili automaticamente i prezzi numerici letti da OCR.
