# Local Postgres setup (macOS)

Use this when you want to run the app fully locally (database + app on your machine).

## If Postgres is already installed (e.g. Homebrew)

**1. Start Postgres**
```bash
brew services start postgresql@16
```
*(If you have a different version, use e.g. `postgresql@14` or just `postgresql`.)*

**2. Create the database**
```bash
/opt/homebrew/opt/postgresql@16/bin/createdb competitor_signals
```
*(Or add that bin to your PATH and run `createdb competitor_signals`. Default user is your macOS username, no password.)*

**3. Set `.env` in the project root**
```
DATABASE_URL=postgresql://YOUR_MAC_USERNAME@localhost:5432/competitor_signals
```
Replace `YOUR_MAC_USERNAME` with your Mac login name (or leave it blank if your Postgres accepts peer auth: `postgresql://localhost:5432/competitor_signals`).  
If you set a password for the postgres user, use: `postgresql://postgres:YOUR_PASSWORD@localhost:5432/competitor_signals`

**4. Run the app**
```bash
./scripts/run_local.sh
```

## If Postgres is not installed (macOS with Homebrew)

```bash
brew install postgresql@16
brew services start postgresql@16
/opt/homebrew/opt/postgresql@16/bin/createdb competitor_signals
```

Then create `.env` and run `./scripts/run_local.sh` as above.

## Alternative: Postgres.app

Download [Postgres.app](https://postgresapp.com/), open it, click “Initialize”, then in Terminal:
```bash
createuser -s postgres   # if you want a postgres user
createdb competitor_signals
```
Use `DATABASE_URL=postgresql://localhost:5432/competitor_signals` (or with `postgres` user if you created it).
