# Raspi Py Server

A simple local-network web dashboard for running Python (.py) and shell (.sh) scripts, showing live logs and progress, uploading/deleting scripts, and cloning Git repos into isolated folders you can run from.

This project is intended for LAN use only.

---

## Features

- Run top-level scripts from `scripts/`:
  - Python scripts: `.py`
  - Shell scripts: `.sh` (runs with `/bin/bash`)
- Live output streaming via WebSockets
- Progress parsing from script output:
  - `PROGRESS <0-100> <message>`
  - `DONE`
- Auto-detects Python `input()` prompts (for `.py`) and lets you fill inputs in the UI
- Upload and delete scripts (`.py` and `.sh`) from the web UI
- Clone Git repos into `repos/` and run `.py` / `.sh` inside the repo (runs with repo as working directory)
- Pull/update cloned repos
- Dark/light theme toggle (default: dark)
- Saves full logs to `logs/`

---

## Requirements

Hardware/OS
- Raspberry Pi (tested conceptually on Pi 5)
- Kali Linux (or any Debian-based Linux should work)

Software
- Python 3.10+ recommended (works with newer Python versions as well)
- pip
- Git (required for repo cloning)
- Network access on your LAN

Python packages (installed inside a virtual environment)
- fastapi
- uvicorn[standard]
- python-multipart

---

## Project Layout

You should have a folder like this:

raspi-py-server/
  server.py
  dashboard.html
  scripts/
  repos/
  logs/
  venv/ (created during setup)

Notes:
- Uploads and deletes are restricted to `scripts/`.
- Cloned repositories are placed under `repos/` in folders named `repo-xxxxxxxx`.

---

## Setup (Step by step)

1. Update system packages
```
sudo apt update && sudo apt full-upgrade -y
```
2. Install system dependencies
```
sudo apt install -y git python3 python3-venv python3-pip
```
3. Create the project folder
```
git clone https://github.com/vafc21/raspi-py-server.git
```
4. Go in the folder
```
cd raspi-py-server
```
5. Create the required directories
```
mkdir -p scripts repos logs
```
6. Create and activate a virtual environment
```
python3 -m venv venv
source venv/bin/activate
```
7. Install Python requirements
```
pip install fastapi "uvicorn[standard]" python-multipart
```
8. Start the server
```
uvicorn server:app --host 0.0.0.0 --port 8000
```
9. Open the dashboard
   From the Pi:
```
http://localhost:8000
```
   From another device on the same LAN:
```
http://<PI-IP>:8000
```
To find your Pi IP address:
   ip a

---

## Using the Dashboard

### Running scripts (top-level)
- Upload a `.py` or `.sh` in the "Top-level scripts" section.
- Select a script to load any detected inputs (Python only).
- Click Run.

### Progress output format (optional)
If your script prints lines like these, the UI will update progress:
- PROGRESS 10 Starting
- PROGRESS 50 Halfway
- PROGRESS 100 Done
- DONE

Example shell script (scripts/test.sh):
  #!/bin/bash
  echo "PROGRESS 10 Starting"
  sleep 1
  echo "PROGRESS 50 Halfway"
  sleep 1
  echo "PROGRESS 100 Done"
  echo "DONE"

Make it executable:
  chmod +x scripts/test.sh

### Inputs for Python scripts
- The UI scans `.py` files for `input()` calls.
- Values you enter are sent to the script as stdin lines in order.

Important limitation:
- This does not dynamically respond to prompts during runtime; it sends all inputs at start.

### Long output handling
- The UI keeps only the last N lines (configurable in the dashboard).
- Full output is always saved to a log file in `logs/`.

To view a log in the browser:
  http://<PI-IP>:8000/logs/<job_id>.log

---

## Git Repos

### Clone a repo
- Paste a Git URL (HTTPS or SSH) into the Git URL box.
- Click Clone.
- The repo is cloned into:
  repos/repo-xxxxxxxx/

### Browse runnable files
- Select a repo from the dropdown.
- The UI lists all `.py` and `.sh` files inside that repo.

### Run from a repo
- Select a file from the repo list and click Run.
- The server runs the file with the repo folder as the working directory.

### Pull updates
- Select a repo and click Pull.

### Delete a repo
- Select a repo and click Delete repo.
- This deletes the entire repo folder under `repos/`.

---

## Security Notes

This server can execute code. Treat it as a powerful local tool.

Recommendations:
- Keep it LAN-only. Do not port-forward it to the internet.
- Do not run the server as root.
- Be careful with unknown Git repos. Cloning and running code can be dangerous.
- If you need sudo for specific actions, whitelist only exact commands using sudoers instead of allowing full sudo access.

---

## Troubleshooting

### WebSocket errors or no live output
Make sure you installed the standard Uvicorn extras inside the venv:
  pip install "uvicorn[standard]"

Then restart Uvicorn:
  uvicorn server:app --host 0.0.0.0 --port 8000

### Clone fails
Ensure git is installed:
  git --version

If using SSH URLs, make sure your SSH keys are set up:
  ls -la ~/.ssh

### Script prompts for sudo password
If a script calls sudo, it may block waiting for a password.
Preferred solution is to avoid sudo in scripts, or whitelist specific commands using sudoers with NOPASSWD for only those commands.

---

## License

Add a license if you plan to share this project publicly.
