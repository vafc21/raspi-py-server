import asyncio
import uuid
import re
import ast
from pathlib import Path
import shutil

from fastapi import FastAPI, WebSocket, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

APP_DIR = Path(__file__).parent
SCRIPTS_DIR = (APP_DIR / "scripts").resolve()
LOGS_DIR = (APP_DIR / "logs").resolve()
REPOS_DIR = (APP_DIR / "repos").resolve()

SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
REPOS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
JOBS = {}  # job_id -> state

# Output parsing:
#   PROGRESS 40 Doing something
#   DONE
progress_re = re.compile(r"^PROGRESS\s+(\d{1,3})\s*(.*)$")
done_re = re.compile(r"^DONE\b")

# Allow .py and .sh only, safe names only
SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+\.(py|sh)$")

# Git URL sanity check (basic)
GIT_URL_RE = re.compile(r"^(https://|git@)[^\s]+$")


def list_scripts():
    out = []
    for p in sorted(SCRIPTS_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith("_"):
            continue
        if p.suffix not in (".py", ".sh"):
            continue
        out.append(p.name)
    return out


def safe_script_path(filename: str) -> Path | None:
    if not filename or not SAFE_NAME.match(filename):
        return None
    p = (SCRIPTS_DIR / filename).resolve()
    if not p.exists() or not p.is_file():
        return None
    if SCRIPTS_DIR not in p.parents:
        return None
    return p


def safe_repo_dir(repo_id: str) -> Path | None:
    if not repo_id or not re.match(r"^repo-[a-f0-9]{8}$", repo_id):
        return None
    p = (REPOS_DIR / repo_id).resolve()
    if REPOS_DIR not in p.parents:
        return None
    return p


def safe_repo_file(repo_id: str, rel_path: str) -> Path | None:
    base = safe_repo_dir(repo_id)
    if not base or not base.exists():
        return None

    if not rel_path or ".." in rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
        return None

    target = (base / rel_path).resolve()
    if not target.exists() or not target.is_file():
        return None
    if base not in target.parents:
        return None
    if target.suffix not in (".py", ".sh"):
        return None
    return target


def extract_input_calls_py(script_path: Path):
    """
    AST scan for Python input(...) calls.
    Returns: [{"index": 1, "prompt": "Username:"}, ...]
    prompt exists only if input("literal string")
    """
    src = script_path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)

    inputs = []
    idx = 0

    class V(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call):
            nonlocal idx
            if isinstance(node.func, ast.Name) and node.func.id == "input":
                idx += 1
                prompt = None
                if node.args:
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        prompt = a0.value
                inputs.append({"index": idx, "prompt": prompt})
            self.generic_visit(node)

    V().visit(tree)
    return inputs


@app.get("/")
def home():
    html_path = APP_DIR / "dashboard.html"
    if not html_path.exists():
        return HTMLResponse("<h3>dashboard.html not found</h3>")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ------------------------
# Scripts (top-level)
# ------------------------
@app.get("/scripts")
def scripts():
    return JSONResponse(list_scripts())


@app.get("/script_meta/{script_name}")
def script_meta(script_name: str):
    p = safe_script_path(script_name)
    if not p:
        return JSONResponse({"error": "script not found"}, status_code=404)

    inputs = []
    if p.suffix == ".py":
        try:
            inputs = extract_input_calls_py(p)
        except Exception:
            inputs = []

    return JSONResponse({
        "script": script_name,
        "type": p.suffix.lstrip("."),
        "inputs": inputs
    })


@app.post("/run")
async def run(payload: dict):
    script = payload.get("script")
    input_vars = payload.get("input_vars", [])
    if not isinstance(input_vars, list):
        input_vars = []

    p = safe_script_path(script)
    if not p:
        return JSONResponse({"error": "script not found"}, status_code=404)

    job_id = str(uuid.uuid4())
    log_file = (LOGS_DIR / f"{job_id}.log").resolve()

    JOBS[job_id] = {
        "script": script,
        "percent": 0,
        "status": "queued",
        "step": "",
        "done": False,
        "rc": None,
        "log_file": str(log_file),
        "log_ring": [],
    }

    input_text = ""
    if input_vars:
        input_text = "\n".join(str(x) for x in input_vars) + "\n"

    asyncio.create_task(_runner(job_id, p, log_file, input_text))
    return {"job_id": job_id}


# ------------------------
# Repo cloning + running
# ------------------------
@app.get("/repos")
def repos():
    out = []
    for p in sorted(REPOS_DIR.iterdir()):
        if p.is_dir() and re.match(r"^repo-[a-f0-9]{8}$", p.name):
            out.append(p.name)
    return JSONResponse(out)


@app.post("/clone_repo")
async def clone_repo(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url or not GIT_URL_RE.match(url):
        return JSONResponse({"error": "Invalid git URL"}, status_code=400)

    repo_id = "repo-" + uuid.uuid4().hex[:8]
    dest = safe_repo_dir(repo_id)
    if not dest:
        return JSONResponse({"error": "Bad repo id"}, status_code=400)

    # Clone shallow
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    out = (await proc.stdout.read()).decode(errors="ignore")
    rc = await proc.wait()
    if rc != 0:
        try:
            if dest.exists():
                shutil.rmtree(dest)
        except Exception:
            pass
        return JSONResponse({"error": "Clone failed", "details": out[-2000:]}, status_code=400)

    return {"ok": True, "repo_id": repo_id}


@app.post("/pull_repo")
async def pull_repo(payload: dict):
    repo_id = payload.get("repo_id")
    base = safe_repo_dir(repo_id)
    if not base or not base.exists():
        return JSONResponse({"error": "repo not found"}, status_code=404)

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(base), "pull",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    out = (await proc.stdout.read()).decode(errors="ignore")
    rc = await proc.wait()
    if rc != 0:
        return JSONResponse({"error": "Pull failed", "details": out[-2000:]}, status_code=400)
    return {"ok": True, "output": out[-2000:]}


@app.delete("/delete_repo/{repo_id}")
def delete_repo(repo_id: str):
    base = safe_repo_dir(repo_id)
    if not base or not base.exists():
        return JSONResponse({"error": "repo not found"}, status_code=404)
    try:
        shutil.rmtree(base)
    except Exception as e:
        return JSONResponse({"error": f"delete failed: {e}"}, status_code=400)
    return {"ok": True, "deleted": repo_id}


@app.get("/repo_files/{repo_id}")
def repo_files(repo_id: str):
    base = safe_repo_dir(repo_id)
    if not base or not base.exists():
        return JSONResponse({"error": "repo not found"}, status_code=404)

    files = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix in (".py", ".sh"):
            rel = str(p.relative_to(base))
            files.append(rel)

    files.sort()
    return JSONResponse({"repo_id": repo_id, "files": files})


@app.get("/repo_meta/{repo_id}")
def repo_meta(repo_id: str, path: str):
    target = safe_repo_file(repo_id, path)
    if not target:
        return JSONResponse({"error": "file not found"}, status_code=404)

    inputs = []
    if target.suffix == ".py":
        try:
            inputs = extract_input_calls_py(target)
        except Exception:
            inputs = []

    return JSONResponse({
        "repo_id": repo_id,
        "path": path,
        "type": target.suffix.lstrip("."),
        "inputs": inputs
    })


@app.post("/run_repo")
async def run_repo(payload: dict):
    repo_id = payload.get("repo_id")
    rel_path = payload.get("path")
    input_vars = payload.get("input_vars", [])
    if not isinstance(input_vars, list):
        input_vars = []

    target = safe_repo_file(repo_id, rel_path)
    base = safe_repo_dir(repo_id) if repo_id else None
    if not target or not base:
        return JSONResponse({"error": "repo file not found"}, status_code=404)

    job_id = str(uuid.uuid4())
    log_file = (LOGS_DIR / f"{job_id}.log").resolve()

    JOBS[job_id] = {
        "script": f"{repo_id}:{rel_path}",
        "percent": 0,
        "status": "queued",
        "step": "",
        "done": False,
        "rc": None,
        "log_file": str(log_file),
        "log_ring": [],
    }

    input_text = ""
    if input_vars:
        input_text = "\n".join(str(x) for x in input_vars) + "\n"

    asyncio.create_task(_runner_with_cwd(job_id, target, base, log_file, input_text))
    return {"job_id": job_id}


# ------------------------
# Logs
# ------------------------
@app.get("/download/{job_id}")
def download_info(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"log_file": job["log_file"]})


@app.get("/logs/{job_id}.log")
def get_log(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    p = Path(job["log_file"])
    if not p.exists():
        return JSONResponse({"error": "log missing"}, status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="ignore"))


# ------------------------
# Upload / Delete scripts
# ------------------------
@app.post("/upload_script")
async def upload_script(file: UploadFile = File(...)):
    if not file.filename or not SAFE_NAME.match(file.filename):
        return JSONResponse({"error": "Invalid filename (only .py/.sh allowed)"}, status_code=400)

    dest = (SCRIPTS_DIR / file.filename).resolve()
    if SCRIPTS_DIR not in dest.parents:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    content = await file.read()
    if len(content) > 300_000:
        return JSONResponse({"error": "File too large"}, status_code=400)

    dest.write_bytes(content)
    dest.chmod(0o755)
    return {"ok": True, "script": file.filename}


@app.delete("/delete_script/{script_name}")
def delete_script(script_name: str):
    if not script_name or not SAFE_NAME.match(script_name):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    p = (SCRIPTS_DIR / script_name).resolve()
    if not p.exists() or not p.is_file() or (SCRIPTS_DIR not in p.parents):
        return JSONResponse({"error": "Not found"}, status_code=404)

    p.unlink()
    return {"ok": True, "deleted": script_name}


# ------------------------
# Process runners
# ------------------------
async def _runner(job_id: str, script_path: Path, log_file: Path, input_text: str):
    job = JOBS[job_id]
    job["status"] = "running"
    job["step"] = "starting"

    if script_path.suffix == ".py":
        cmd = ["python3", str(script_path)]
    elif script_path.suffix == ".sh":
        cmd = ["/bin/bash", str(script_path)]
    else:
        job["status"] = "error"
        job["done"] = True
        job["rc"] = 127
        return

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    if input_text:
        try:
            proc.stdin.write(input_text.encode())
            await proc.stdin.drain()
        except Exception:
            pass
    try:
        proc.stdin.close()
    except Exception:
        pass

    await _stream_output(job, proc, log_file)


async def _runner_with_cwd(job_id: str, target: Path, cwd: Path, log_file: Path, input_text: str):
    job = JOBS[job_id]
    job["status"] = "running"
    job["step"] = "starting"

    if target.suffix == ".py":
        cmd = ["python3", str(target)]
    elif target.suffix == ".sh":
        cmd = ["/bin/bash", str(target)]
    else:
        job["status"] = "error"
        job["done"] = True
        job["rc"] = 127
        return

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    if input_text:
        try:
            proc.stdin.write(input_text.encode())
            await proc.stdin.drain()
        except Exception:
            pass
    try:
        proc.stdin.close()
    except Exception:
        pass

    await _stream_output(job, proc, log_file)


async def _stream_output(job: dict, proc: asyncio.subprocess.Process, log_file: Path):
    ring_cap = 2500
    with log_file.open("a", encoding="utf-8") as f:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="ignore").rstrip("\n")

            f.write(text + "\n")
            f.flush()

            job["log_ring"].append(text)
            if len(job["log_ring"]) > ring_cap:
                job["log_ring"] = job["log_ring"][-ring_cap:]

            m = progress_re.match(text)
            if m:
                pct = max(0, min(100, int(m.group(1))))
                msg = (m.group(2) or "").strip()
                job["percent"] = pct
                if msg:
                    job["step"] = msg

            if done_re.match(text):
                job["percent"] = 100
                job["step"] = "done"

    rc = await proc.wait()
    job["rc"] = rc
    job["done"] = True
    job["status"] = "finished" if rc == 0 else "error"
    if rc == 0:
        job["percent"] = 100
        if job["step"] in ("", "starting"):
            job["step"] = "done"


@app.websocket("/ws/{job_id}")
async def ws(job_id: str, ws: WebSocket):
    await ws.accept()
    last_len = 0
    try:
        while True:
            job = JOBS.get(job_id)
            if not job:
                await ws.send_text("ERROR Job not found")
                break

            ring = job["log_ring"]
            new_lines = ring[last_len:]
            last_len = len(ring)

            for line in new_lines:
                await ws.send_text("LOG " + line)

            await ws.send_text(f"STATE {job['percent']}|{job['status']}|{job['step']}")

            if job["done"]:
                await ws.send_text(f"DONE rc={job['rc']}")
                break

            await asyncio.sleep(0.35)
    finally:
        await ws.close()