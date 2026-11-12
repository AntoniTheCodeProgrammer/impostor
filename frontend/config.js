// ─────────────────────────────────────────────────────────────
//  Konfiguracja frontendu – impostor
//  Adres backendu wybierany jest automatycznie:
//  lokalnie (localhost / plik .html) → backend lokalny, inaczej → Render.com
// ─────────────────────────────────────────────────────────────

(function () {
    const PRODUCTION_API_URL = "https://impostor-backend.onrender.com";
    const LOCAL_API_URL = "http://localhost:8000";

    const isLocal = ["", "localhost", "127.0.0.1"].includes(window.location.hostname);
    window.API_URL = isLocal ? LOCAL_API_URL : PRODUCTION_API_URL;

    // URL WebSocket – generowany automatycznie z API_URL
    // https:// → wss://   |   http:// → ws://
    window.WS_URL = window.API_URL
        .replace(/^https:\/\//, "wss://")
        .replace(/^http:\/\//, "ws://");
})();
