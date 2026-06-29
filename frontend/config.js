// ─────────────────────────────────────────────────────────────
//  Konfiguracja frontendu – Inpostor
//  Zmień poniższe URL-e po deploymencie na Render.com
// ─────────────────────────────────────────────────────────────

// URL backendu (HTTP) – np. https://inpostor-backend.onrender.com
// Lokalnie: http://localhost:8000
window.API_URL = "https://inpostor-backend.onrender.com";

// URL WebSocket – generowany automatycznie z API_URL
// https:// → wss://   |   http:// → ws://
window.WS_URL = window.API_URL
    .replace(/^https:\/\//, "wss://")
    .replace(/^http:\/\//, "ws://");
