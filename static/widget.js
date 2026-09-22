/*
  Widget chat Kano Kimonos per il sito Shopify - vanilla JS, nessuna dipendenza.
  Si installa con una sola riga in layout/theme.liquid, prima di </body>:
    <script src="https://<host-del-bot>/static/widget.js" data-key="<chiave>" data-lang="it" defer></script>
  - data-key: chiave client del widget (ruolo retail, pubblica per natura).
  - data-lang: "it" o "en"; se manca si legge <html lang>.
  - base: l'origine da cui e' stato caricato questo file (il bot).
  Prima del click non carica niente: solo il bottone. Al primo click apre un
  iframe su /static/chat.html?embed=1&key=...&lang=...; i click successivi
  aprono/chiudono; la X dentro la chat manda {type:"kano-chat-close"}.
  Il chat_id lo custodisce QUESTA pagina (dal 22/09/2026): il localStorage
  dell'iframe e' storage di terza parte, che Safari iOS partiziona e cancella
  alla chiusura delle schede, e la conversazione andava persa fra una visita e
  l'altra. Qui e' storage di prima parte del negozio: si legge kano_chat_id,
  se c'e' si passa all'iframe come &chat=<id>; se non c'e' la chat ne crea uno
  e lo rimanda con postMessage {type:"kano-chat-id", id}, che viene salvato.
  Se anche questo localStorage e' bloccato si va avanti lo stesso.
*/
(function () {
  if (window.__kanoChatWidget) return;
  window.__kanoChatWidget = true;

  var script = document.currentScript;
  if (!script) {
    var tutti = document.getElementsByTagName("script");
    for (var i = tutti.length - 1; i >= 0; i--) {
      if ((tutti[i].getAttribute("src") || "").indexOf("widget.js") !== -1) { script = tutti[i]; break; }
    }
  }
  if (!script) return;

  var key = script.getAttribute("data-key") || "";
  var lang = (script.getAttribute("data-lang") || "").toLowerCase().slice(0, 2);
  if (lang !== "it" && lang !== "en") {
    lang = ((document.documentElement.lang || "").toLowerCase().indexOf("it") === 0) ? "it" : "en";
  }
  var base;
  try { base = new URL(script.src, location.href).origin; } catch (e) { return; }

  var CHIAVE_ID = "kano_chat_id";
  var ID_VALIDO = /^[A-Za-z0-9._:-]{4,120}$/;     // lo stesso filtro di chat.html
  function leggiId() {
    try { var v = localStorage.getItem(CHIAVE_ID); return (v && ID_VALIDO.test(v)) ? v : null; }
    catch (e) { return null; }
  }
  function salvaId(v) { try { localStorage.setItem(CHIAVE_ID, v); } catch (e) {} }

  var Z = 2147483000;
  var apertaChat = false;
  var iframe = null;

  var bottone = document.createElement("button");
  bottone.type = "button";
  bottone.setAttribute("aria-label", "Chat");
  bottone.setAttribute("aria-expanded", "false");
  bottone.style.cssText = [
    "position:fixed", "right:20px", "bottom:20px", "width:56px", "height:56px",
    "border-radius:50%", "border:0", "background:#111", "color:#fff", "cursor:pointer",
    "box-shadow:0 4px 14px rgba(0,0,0,.28)", "display:flex", "align-items:center",
    "justify-content:center", "padding:0", "margin:0", "z-index:" + Z, "outline-offset:3px"
  ].join(";");
  bottone.innerHTML =
    '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M21 12a8 8 0 0 1-8 8H8l-4 3v-6.5A8 8 0 0 1 12 4h1a8 8 0 0 1 8 8z"/>' +
    '<circle cx="9" cy="12" r="0.8" fill="#fff"/><circle cx="12.5" cy="12" r="0.8" fill="#fff"/>' +
    '<circle cx="16" cy="12" r="0.8" fill="#fff"/></svg>';

  function stretto() { return window.innerWidth < 640; }

  function posiziona() {
    if (!iframe) return;
    if (stretto()) {
      iframe.style.cssText = "position:fixed;left:0;top:0;width:100%;height:100%;border:0;border-radius:0;" +
        "box-shadow:none;background:#f6f6f4;z-index:" + (Z + 1) + ";";
    } else {
      iframe.style.cssText = "position:fixed;right:20px;bottom:88px;width:380px;height:600px;max-height:calc(100vh - 108px);" +
        "border:0;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.3);background:#f6f6f4;z-index:" + (Z + 1) + ";";
    }
    iframe.style.display = apertaChat ? "block" : "none";
  }

  function creaIframe() {
    iframe = document.createElement("iframe");
    iframe.title = lang === "it" ? "Chat Kano Kimonos" : "Kano Kimonos chat";
    iframe.setAttribute("allow", "clipboard-write");
    var chatId = leggiId();
    iframe.src = base + "/static/chat.html?embed=1&key=" + encodeURIComponent(key) + "&lang=" + encodeURIComponent(lang) +
      (chatId ? "&chat=" + encodeURIComponent(chatId) : "");
    document.body.appendChild(iframe);
    window.addEventListener("resize", posiziona);
  }

  function mostra(stato) {
    apertaChat = !!stato;
    if (apertaChat && !iframe) creaIframe();
    posiziona();
    bottone.setAttribute("aria-expanded", apertaChat ? "true" : "false");
    // A tutto schermo il bottone starebbe sopra la chat: si nasconde.
    bottone.style.display = (apertaChat && stretto()) ? "none" : "flex";
  }

  bottone.addEventListener("click", function () { mostra(!apertaChat); });

  window.addEventListener("message", function (ev) {
    if (ev.origin !== base) return;
    if (!ev.data) return;
    if (ev.data.type === "kano-chat-close") mostra(false);
    if (ev.data.type === "kano-chat-id" && typeof ev.data.id === "string" && ID_VALIDO.test(ev.data.id)) salvaId(ev.data.id);
  });
  window.addEventListener("resize", function () {
    if (!apertaChat) return;
    bottone.style.display = stretto() ? "none" : "flex";
  });

  function monta() { document.body.appendChild(bottone); }
  if (document.body) monta(); else document.addEventListener("DOMContentLoaded", monta);
})();
