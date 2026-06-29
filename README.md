# Inpostor

Aplikacja wspomagająca prowadzenie gry towarzyskiej (podobnej do Mafii/Among Us).

---

## Architektura

| Warstwa | Technologia |
|---|---|
| Backend API | Python (FastAPI) → **Render.com** |
| Baza danych | **Turso** (libSQL – SQLite w chmurze) |
| Frontend | Statyczne pliki HTML → **Serwer hostingowy** |

---

## Lokalne uruchomienie (developerskie)

### 1. Zainstaluj zależności backendu

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Skopiuj plik `.env.example` i uzupełnij token Turso

```bash
cp .env.example .env
```

Wygeneruj token Turso:
```bash
turso db tokens create impostor
```

Wklej go do pliku `.env` jako wartość `TURSO_AUTH_TOKEN`.

> **Uwaga:** Lokalnie możesz też użyć pliku SQLite zamiast Turso – wystarczy ustawić:
> ```
> TURSO_DATABASE_URL=file:users.db
> TURSO_AUTH_TOKEN=
> ```

### 3. Uruchom backend

```bash
cd backend
uvicorn main:app --reload
```

Backend startuje na `http://localhost:8000`.

### 4. Uruchom frontend lokalnie

W pliku `frontend/config.js` zmień URL na localhost:

```js
window.API_URL = "http://localhost:8000";
```

Otwórz pliki przez dowolny serwer statyczny, np.:
```bash
cd frontend
python -m http.server 8080
```

Następnie otwórz `http://localhost:8080` w przeglądarce.

---

## Deployment produkcyjny

### Krok 1 – Backend na Render.com

1. Wgraj projekt na **GitHub** (upewnij się, że `.env` jest w `.gitignore` – jest ✅).
2. Zaloguj się na [render.com](https://render.com) → **New → Web Service**.
3. Podłącz repozytorium GitHub.
4. Render automatycznie wykryje plik `render.yaml` i skonfiguruje serwis.
5. W panelu Render → **Environment** dodaj ręcznie zmienną:
   - `TURSO_AUTH_TOKEN` → twój token z `turso db tokens create impostor`
6. Kliknij **Deploy**. Po chwili serwis będzie dostępny pod adresem `https://inpostor-backend.onrender.com` (nazwa może się różnić).

### Krok 2 – Stwórz konto admina (jednorazowo po deploymencie)

```bash
curl -X POST https://twoj-backend.onrender.com/admin/seed \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "twoje-haslo", "secret": "inpostor-secret"}'
```

### Krok 3 – Frontend na serwer hostingowy

1. Otwórz plik `frontend/config.js` i podmień URL backendu na adres z Render:

```js
window.API_URL = "https://twoj-backend.onrender.com";
```

Plik `WS_URL` generuje się automatycznie (`https://` → `wss://`).

2. Wgraj trzy pliki na serwer hostingowy:
   - `frontend/index.html`
   - `frontend/admin.html`
   - `frontend/config.js`

3. Gracze wchodzą na `https://twoja-strona.pl/index.html` (lub główny URL),
   Admin wchodzi na `https://twoja-strona.pl/admin.html`.

---

## Struktura plików

```
Inpostor/
├── backend/
│   ├── main.py              # FastAPI + Turso (libsql-client)
│   ├── requirements.txt     # Zależności Pythona
│   ├── .env.example         # Przykładowy plik zmiennych środowiskowych
│   └── .venv/               # Lokalne środowisko (nie commituj)
├── frontend/
│   ├── index.html           # Panel gracza
│   ├── admin.html           # Panel Mistrza Gry
│   └── config.js            # ← JEDYNE miejsce gdzie podajesz URL backendu
├── render.yaml              # Konfiguracja Render.com
├── .gitignore
└── README.md
```
