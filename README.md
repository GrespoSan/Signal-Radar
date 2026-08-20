# Signal Radar V2

V2 aggiunge l'import multiplo degli screenshot WhatsApp e una proposta automatica gratuita tramite OCR locale.

## Funzioni principali

- caricamento multiplo di PNG/JPG/JPEG/WEBP;
- ordinamento cronologico dal nome file WhatsApp;
- OCR locale con Tesseract, senza API a pagamento;
- proposta di Asset, Bias, Periodo, TF, Categoria, Stato e Prossima azione;
- proposta di collegamento a un setup esistente;
- raggruppamento automatico delle immagini che sembrano appartenere allo stesso nuovo setup;
- tabella modificabile prima dell'importazione;
- un solo pulsante `IMPORTA TUTTO`;
- controllo duplicati tramite SHA-256;
- timeline delle immagini per setup;
- Entry / Stop / Target volutamente da confermare manualmente;
- export CSV e backup del database SQLite.

## File da caricare nel repository Streamlit

Mantieni questa struttura:

```
app.py
requirements.txt
packages.txt
README.md
assets/
data/
  seed_signals.csv
  seed_updates.csv
  uploads/
```

La cartella `assets/` contiene il dataset dimostrativo iniziale usato per i test. Puoi mantenerla o rimuoverla dopo aver verificato la V2.

## Uso

1. Apri **⚡ Import multiplo**.
2. Trascina insieme gli screenshot WhatsApp.
3. Premi **Analizza e proponi**.
4. Controlla la tabella proposta.
5. Se più immagini appartengono allo stesso setup, devono avere lo stesso valore nella colonna **Gruppo**.
6. In **Destinazione** scegli:
   - `NUOVO` per creare un nuovo setup;
   - `INFO` per materiale didattico/non operativo;
   - un setup esistente per collegare l'immagine alla timeline corretta.
7. Conferma manualmente Entry / Stop / Target quando sono importanti.
8. Premi **IMPORTA TUTTO**.

## Nota importante sull'OCR

L'OCR serve a classificare e organizzare gli screenshot. Non va considerato affidabile al 100% per numeri di prezzo, stop o target. Per questo la V2 non trascrive automaticamente quei livelli come dati operativi confermati.

## Streamlit Community Cloud

`packages.txt` installa Tesseract. Il primo deploy può essere più lento del normale.

Il database SQLite è locale al filesystem dell'app. Su Community Cloud il filesystem può essere temporaneo: usa periodicamente **Backup database SQLite**. Una futura V3 può usare un database persistente esterno.
