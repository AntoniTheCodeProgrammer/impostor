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

Ustaw w `.env` także własny `ADMIN_SEED_SECRET` (do założenia konta admina):

```bash
curl -X POST http://localhost:8000/admin/seed \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "twoje-haslo", "secret": "<twój ADMIN_SEED_SECRET>"}'
```

### 4. Uruchom frontend lokalnie

Adres backendu wybierany jest automatycznie (`localhost` → `http://localhost:8000`),
więc nie trzeba nic zmieniać w `frontend/config.js`.

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
| `ADMIN_SEED_SECRET` | ← **własny, długi sekret** (nie `impostor-secret` – ta wartość jest publiczna i jest odrzucana). Potrzebny tylko do założenia konta admina |
| `TOKEN_SECRET` | *(opcjonalnie)* klucz podpisujący sesje. Bez niego jest wyliczany z `TURSO_AUTH_TOKEN` |

#### Jak zdobyć TURSO_AUTH_TOKEN
1. Zaloguj się na [app.turso.tech](https://app.turso.tech)
2. Wybierz bazę `impostor` → zakładka **Tokens** → **Create token**
3. Skopiuj token i wklej w Render → Environment → `TURSO_AUTH_TOKEN`
4. Kliknij **Manual Deploy** → serwis powinien teraz uruchomić się poprawnie.

### Krok 2 – Stwórz konto admina (jednorazowo po deploymencie)

```bash
curl -X POST https://impostor-backend.onrender.com/admin/seed \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "twoje-haslo", "secret": "<wartość ADMIN_SEED_SECRET z Render>"}'
```

Wszystkie endpointy `/admin/*` wymagają zalogowania (token admina), a gracze
komunikują się z serwerem przez token sesji – sama znajomość nazwy gracza nie wystarcza.

### Krok 3 – Frontend na serwer hostingowy

1. Adres backendu wybierany jest automatycznie w `frontend/config.js`: lokalnie
   (`localhost` / plik `.html`) używany jest `http://localhost:8000`, na serwerze
   hostingowym adres z Render. Jeśli Twój backend ma inny adres niż
   `https://impostor-backend.onrender.com`, zmień stałą `PRODUCTION_API_URL`
   w tym pliku.

   `WS_URL` generuje się automatycznie (`https://` → `wss://`).

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
│   └── config.js            # ← JEDYNE miejsce gdzie podajesz URL backendu (wybierany automatycznie)
├── render.yaml              # Konfiguracja Render.com
├── .gitignore
└── README.md
```
