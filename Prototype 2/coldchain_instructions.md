# ColdChainGuard: How to Run

## Prerequisites

- Python 3.11 or newer
- OpenSSL (comes with Git for Windows, or install separately)
- A terminal opened in the project root (`Prototype`)

## 1. Install dependencies

Create and activate a virtual environment, then install packages:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Run the full telemetry demo (recommended)

This starts the MQTT broker, cloud API, gateway, and three sensor nodes in one command. It deletes any existing `ccg.db` and creates a fresh database.

```powershell
python run_demo.py
```

Useful options:

| Option | Default | Meaning |
|--------|---------|---------|
| `--duration` | `300` | Simulated run length in seconds |
| `--compression` | `120` | Simulated seconds per real second (120 = 5 min sim time in 2.5 s real time) |
| `--db` | `ccg.db` | SQLite database path |

Example: run 12 hours of simulated time:

```powershell
python run_demo.py --duration 43200 --compression 120
```

Press `Ctrl+C` to stop all processes.

On first run, self-signed TLS certificates are generated in `config/certs/` if they are missing.

## 3. View the dashboard

While the cloud service is running (started automatically by `run_demo.py`), open:

```
http://localhost:8000/dashboard
```

The API also exposes a health check at `http://localhost:8000/health`.

To run only the cloud API against an existing database:

```powershell
python -m cloud.api --db ccg.db
```

Default port is `8000`. Change it with `--port`.

## 4. Run the dashboard with pre-seeded S3 data

Use this when you want the dashboard to show a mid-excursion refrigeration failure without running the full live demo.

**Step 1:** Seed the dashboard database (creates `ccg_dashboard.db`):

```powershell
python seed_dashboard_s3.py
```

**Step 2:** Start the MQTT broker in one terminal:

```powershell
python run_demo.py --broker
```

**Step 3:** Start the cloud API with the seeded database in another terminal:

```powershell
python -m cloud.api --db ccg_dashboard.db
```

**Step 4:** Open `http://localhost:8000/dashboard` in a browser.

Do not run `python run_demo.py` without `--broker` while using `ccg_dashboard.db`. The full demo deletes and recreates `ccg.db` by default.

## 5. Run tests

```powershell
python -m pytest
```

All acceptance tests should pass (22 tests across integrity, queue, rules, and thermal modules).

## 6. Run the evaluation harness

Runs scenarios S1 through S5, writes CSV metrics, and generates a results chart:

```powershell
python -m evaluation.runner
```

Output files go to `evaluation/results/`:

- `metrics.csv`
- `dwell_sweep.csv`
- `05_results.png`

Options:

| Option | Default | Meaning |
|--------|---------|---------|
| `--repetitions` | `10` | Repetitions per scenario |
| `--dwell-s` | `120` | Dwell time in seconds for excursion detection |
| `--base-seed` | `2026` | Random seed base |
| `--results-dir` | `evaluation/results` | Output directory |

Example with fewer repetitions for a quicker run:

```powershell
python -m evaluation.runner --repetitions 3
```

## 7. Other utilities

### Thermal engine demo (console output)

```powershell
python demo_thermal.py
```

Prints MKT, degree-minutes, and disposition at three checkpoints.

### Adversarial frame test (requires live stack)

With broker, cloud, and gateway already running:

```powershell
python -m evaluation.attack --db ccg.db
```

Sends forged and replayed MQTT frames. Check rejections in the database:

```powershell
sqlite3 ccg.db "SELECT reason, COUNT(*) FROM security_events GROUP BY reason;"
```

## 8. Manual component startup (optional)

If you prefer to start each part yourself instead of using `run_demo.py`:

**Terminal 1 - MQTT broker:**

```powershell
python run_demo.py --broker
```

**Terminal 2 - Cloud API:**

```powershell
python -m cloud.api
```

**Terminal 3 - Gateway:**

```powershell
python -m gateway.service
```

**Terminal 4+ - Sensor nodes (one per device):**

```powershell
python -m node.simulator --device NODE-01 --start-epoch 1735689600 --duration 300
python -m node.simulator --device NODE-02 --start-epoch 1735689600 --duration 300
python -m node.simulator --device NODE-03 --start-epoch 1735689600 --duration 300
```

Replace `--start-epoch` with the current Unix timestamp if you want readings to align with real time.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `OpenSSL not found` | Install Git for Windows or OpenSSL, or place `ca.crt`, `server.crt`, and `server.key` in `config/certs/` manually |
| Port 8883 or 8000 in use | Stop other MQTT or web servers, or change `--broker-port` / `--port` |
| Dashboard shows no data | Confirm the cloud API is running and pointed at a database that has readings |
| `run_demo.py` finishes immediately | Check terminal output for broker or TLS errors |
