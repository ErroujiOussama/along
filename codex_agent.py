"""Al's slow path: one headless `codex exec --json` process per task, no terminal UI.

State lives in .along_codex.json (any process may read it; writes hold a lock):
  {"thread": <codex thread id or null>, "pid": <running turn or null>, "task": ..., "started": t,
   "last": {"task", "reply", "secs", "ok", "t"}}
A task continues `thread` (`codex exec resume`) unless fresh; fresh = forget the thread id. The brief (prompts/along.md)
goes in as developer instructions on every call, so it is a cached prefix and the conversation holds only tasks.
Interrupt = kill the turn's process group (any process can, via the pid). One turn at a time: a new task interrupts the
running one and continues the same thread (latest request wins, the interrupted turn stays in context).
A readable transcript of every turn goes to .along_codex.log (the dashboard tails it).
"""
import fcntl, json, os, signal, subprocess, threading, time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
STATE, LOG, BRIEF = f"{HERE}/.along_codex.json", f"{HERE}/.along_codex.log", f"{HERE}/prompts/along.md"
ERR = f"{HERE}/.along_codex.err"                         # stderr of the latest turn
EMPTY = {"thread": None, "pid": None, "task": "", "started": 0.0, "last": None}


API_KEY = None                                           # set by along: OpenAI API key -> gpt-6.1-sol on the API, fast tier


def flags():
    """codex exec flags shared by fresh and resumed turns (resume takes no -C: the process runs in HERE).
    With API_KEY: the OpenAI API as provider (key via env, the global Codex login stays untouched), gpt-6.1-sol, low
    reasoning, Codex's Fast mode (service tier priority; ultrafast is not in Codex's tier list for the model).
    Without: the ChatGPT login and gpt-6.1-terra."""
    api = ["-c", 'model_providers.along_api={name="OpenAI API",base_url="https://api.openai.com/v1",env_key="OPENAI_API_KEY",wire_api="responses"}',
           "-c", "model_provider=along_api", "-c", f'service_tier="{os.environ.get("ALONG_CODEX_TIER", "priority")}"'] if API_KEY else []
    return ["--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", *api,
            "-m", os.environ.get("ALONG_MODEL", "gpt-6.1-sol" if API_KEY else "gpt-6.1-terra"), "-c", "model_reasoning_effort=low",
            "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',      # no AGENTS.md (the brief has it), no web
            "-c", "developer_instructions=" + json.dumps(open(BRIEF).read()),    # a JSON string is a valid TOML string
            *[x for f in ("hooks", "plugins", "apps", "multi_agent", "image_generation", "browser_use", "computer_use",
                          "goals", "skill_search", "in_app_browser", "tool_suggest") for x in ("--disable", f)]]


@contextmanager
def _locked():
    with open(STATE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def state():
    try:
        s = {**EMPTY, **json.load(open(STATE))}
    except (FileNotFoundError, ValueError):
        s = dict(EMPTY)
    if s["pid"] and not _alive(s["pid"]):
        s["pid"] = None                                  # the process died without cleaning up (daemon restart etc.)
    return s


def _save(**k):
    with _locked():
        s = state(); s.update(k)
        json.dump(s, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)
        return s


def _alive(pid):
    try:
        return b"codex" in open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return False


def busy():
    return state()["pid"] is not None


def interrupt():
    """Kill the running turn (its shells and the commands it started go with the process group). True if one ran."""
    pid = state()["pid"]
    if not pid:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _save(pid=None)
    return True


def fresh():
    """Forget the conversation: the next task starts a new thread with only the brief."""
    interrupt(); _save(thread=None)


def _log(line):
    with open(LOG, "a") as f:
        f.write(time.strftime("%H:%M:%S ") + line.rstrip() + "\n")


def run(task, fresh_ctx=False, on_event=lambda kind, text: None, on_reply=lambda text: None, wait=False):
    """Start a turn (interrupting a running one). on_event("codex"|"error", text) per command / output / turn end,
    on_reply(final message) once the turn completes. Returns the reader thread (join it, or wait=True)."""
    if interrupt():
        on_event("codex", "turn stopped (a newer task replaces it)")
    if fresh_ctx:
        _save(thread=None)
    prov = "api" if API_KEY else "chatgpt"
    if state().get("provider") != prov:                  # a thread lives with its provider: switching starts fresh
        _save(thread=None, provider=prov)
    thread = state()["thread"]
    cmd = ["codex", "exec", *(["resume"] if thread else []), *flags(), *([thread] if thread else []), "-"]
    p = subprocess.Popen(cmd, cwd=HERE, env={**os.environ, **({"OPENAI_API_KEY": API_KEY} if API_KEY else {})}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(ERR, "w"),
                         text=True, start_new_session=True)       # own process group: interrupt kills it whole
    t0 = time.time()
    _save(pid=p.pid, task=task, started=t0)
    _log(f"── task ({'resume ' + thread[:8] if thread else 'new thread'}): {task}")
    p.stdin.write(task); p.stdin.close()

    def views(tid, stop):
        """`codex exec --json` emits nothing for view_image: follow the rollout file to log the images Codex opens."""
        import glob, re
        f = None
        while not stop.is_set() and f is None:
            m = glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*{tid}.jsonl"))
            f = open(m[0]) if m else None
            if f is None:
                time.sleep(0.2)
        if f is None:
            return
        f.seek(0, 2)
        while True:
            ln = f.readline()
            if not ln:
                if stop.is_set():
                    break
                time.sleep(0.2); continue
            if "view_image" not in ln:
                continue
            try:
                code = str((json.loads(ln).get("payload") or {}).get("input") or "")
            except ValueError:
                continue
            for path in re.findall(r"view_image\(\{\s*path\s*:\s*[\"']([^\"']+)", code):
                _log("👁 " + os.path.basename(path)); on_event("codex", "views " + os.path.basename(path))

    def read():
        reply, done = "", False
        stop = threading.Event()
        if thread:
            threading.Thread(target=views, args=(thread, stop), daemon=True).start()
        for ln in p.stdout:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            ty, it = e.get("type"), e.get("item") or {}
            if ty == "thread.started" and e.get("thread_id") != thread:
                _save(thread=e["thread_id"])
                threading.Thread(target=views, args=(e["thread_id"], stop), daemon=True).start()
            elif ty == "item.started" and it.get("type") == "command_execution":
                c = it.get("command", "")
                c = c[len("/bin/bash -lc "):].strip("'") if c.startswith("/bin/bash -lc ") else c
                _log("$ " + c); on_event("codex", "$ " + c)
            elif ty == "item.completed" and it.get("type") == "command_execution":
                out = " ".join((it.get("aggregated_output") or "").split())
                _log(f"→ (exit {it.get('exit_code')}) {out[:600]}"); on_event("codex", "→ " + out[:300])
            elif ty == "item.completed" and it.get("type") == "agent_message":
                reply = (it.get("text") or "").strip(); _log("Al: " + reply)
            elif ty == "turn.completed":
                done = True
            elif ty in ("turn.failed", "error"):
                msg = (e.get("error") or {}).get("message") or e.get("message") or ln
                _log("error: " + str(msg)); on_event("error", f"codex: {str(msg)[:300]}")
        p.wait(); time.sleep(0.3); stop.set()               # let the tailer read the last lines
        secs = time.time() - t0
        with _locked():                                  # only clear the pid if it is still ours (not a newer turn)
            s = state()
            if s["pid"] in (p.pid, None):
                s["pid"] = None
            s["last"] = {"task": task, "reply": reply, "secs": round(secs, 1), "ok": done, "t": time.time()}
            json.dump(s, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)
        if done:
            _log(f"── done in {secs:.1f} s"); on_event("codex", f"turn done in {secs:.1f} s")
            if reply:
                on_reply(reply)
        else:
            err = [l for l in open(ERR).read().splitlines() if l.strip() and not l.startswith("Reading")][-1:] if p.returncode > 0 else []
            _log(f"── stopped after {secs:.1f} s {' '.join(err)}")
            on_event("error" if err else "codex", f"turn {'failed: ' + err[0][:200] if err else 'stopped'} after {secs:.1f} s")

    th = threading.Thread(target=read, daemon=True); th.start()
    if wait:
        th.join()
    return th
