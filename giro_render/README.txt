GIRO FULLY PER RENDER (cmd 289) - NON ATTIVO
==============================================
Copia del giro Shopify <-> Fully che oggi gira sul PC (ShopifyKano, attivita' Windows "Kano Giro Fully").
Stesse regole; adattamenti: chiavi da variabili d'ambiente, interruttore GIRO_MODO, log su stdout in ora di Roma,
nessun file di log, backup o lock. Email giornaliera ed eccezioni come sul PC (primo giro dopo le 09:00 ora di Roma,
riprova al giro dopo se l'invio fallisce), con memoria nella tabella giro_fully_stato (DATABASE_URL);
senza DATABASE_URL (in locale) la memoria sono i json del PC in giro_render/logs/.
Per i comandi a mano che scrivono (es. "python avvisi_fully.py salta") serve GIRO_MODO=acceso.

Servizio Render: Cron Job
  Root directory:   giro_render
  Build command:    pip install -r requirements.txt
  Start command:    python giro_fully.py
  Pianificazione:   */10 * * * *     (Render usa l'ora UTC per il cron: ogni 10 minuti va bene comunque)

Variabili d'ambiente da impostare su Render (solo nomi, i valori NON vanno mai nel repository):
  FULLY_PRODUCTION_TOKEN
  FULLY_SHOPIFY_CLIENT_ID
  FULLY_SHOPIFY_CLIENT_SECRET
  ADMIN_SMTP_PASSWORD
  DATABASE_URL       Postgres per la memoria tra i giri (tabella giro_fully_stato, nessun'altra tabella toccata)
  GIRO_MODO          spento | prova | acceso   (mancante o sconosciuto = spento)
  TZ                 Europe/Rome
  PYTHON_VERSION     3.13.1

GIRO_MODO
  spento  esce subito, una riga di log
  prova   legge Shopify e Fully, ogni scrittura (POST Fully, tagsAdd, orderUpdate, tagsRemove, fulfillmentCreate,
          inventorySetQuantities, email) diventa una riga "AVREI FATTO: ..." nel log
  acceso  lavora davvero. PRIMA di accenderlo va spenta l'attivita' Windows "Kano Giro Fully" sul PC,
          altrimenti girano due giri insieme.

Prova in locale (Windows, Git Bash):
  GIRO_MODO=prova GIRO_ENV_FILE=C:/Users/Jepssen/ShopifyKano/.env python giro_fully.py
  GIRO_ENV_FILE serve solo in locale: su Render le chiavi arrivano dalle variabili d'ambiente.
