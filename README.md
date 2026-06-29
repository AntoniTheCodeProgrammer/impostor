# impostor

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

> **Uwaga:** `render.yaml` jest wykrywany automatycznie tylko przez **Blueprint** (New → Blueprint).  
> Jeśli tworzysz serwis ręcznie przez **Web Service**, skonfiguruj poniższe dane samodzielnie.

#### Opcja A – Blueprint (automatyczna konfiguracja z render.yaml)
1. Wgraj projekt na **GitHub**.
2. Render.com → **New → Blueprint** → podłącz repo → Render wykryje `render.yaml`.
3. Dodaj tylko zmienną `TURSO_AUTH_TOKEN` (oznaczona jako `sync: false` w pliku).

#### Opcja B – Web Service (konfiguracja ręczna – tak jak zrobiłeś)
Jeśli już masz serwis, upewnij się że te wartości są ustawione:

| Pole | Wartość |
|---|---|
| **Root Directory** | `backend` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **Runtime** | Python 3 |

**Environment Variables** (Render → Environment):

| Klucz | Wartość |
|---|---|
| `TURSO_DATABASE_URL` | `libsql://impostor-antonithecodeprogrammer.aws-eu-west-1.turso.io` |
| `TURSO_AUTH_TOKEN` | ← **TOKEN Z TURSO – WYMAGANY!** bez niego deploy nie wystartuje |
| `ADMIN_SEED_SECRET` | `impostor-secret` |

#### Jak zdobyć TURSO_AUTH_TOKEN
1. Zaloguj się na [app.turso.tech](https://app.turso.tech)
2. Wybierz bazę `impostor` → zakładka **Tokens** → **Create token**
3. Skopiuj token i wklej w Render → Environment → `TURSO_AUTH_TOKEN`
4. Kliknij **Manual Deploy** → serwis powinien teraz uruchomić się poprawnie.

### Krok 2 – Stwórz konto admina (jednorazowo po deploymencie)

```bash
curl -X POST https://impostor-backend.onrender.com/admin/seed \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "twoje-haslo", "secret": "impostor-secret"}'
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
impostor/
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
