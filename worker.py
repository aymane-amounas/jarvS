"""The heartbeat. One job at a time, forever.

WhatsApp messages jump the queue. When the queue is empty and enough time
has passed, it picks its own work from missions.yaml.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import pathlib
import sys
import time
import uuid

from dotenv import load_dotenv

import runtime

load_dotenv(pathlib.Path(__file__).with_name(".env"))

HOME = runtime.app_home()
IDLE_MINUTES = int(os.environ.get("JARVIS_IDLE_MINUTES", "5"))
POLL_SECONDS = 20

log = logging.getLogger("jarvs")


def setup_logging() -> None:
    (HOME / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                            "%Y-%m-%dT%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(
        HOME / "logs" / "jarvs.log", maxBytes=5 << 20, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]


def send_out(reply_to: str, text: str) -> None:
    import memory
    if not reply_to:
        log.info("RESULT (no destination):\n%s", text)
        return
    memory.enqueue_outbox(reply_to, text)


def run_job(app, task: str, source: str, reply_to: str = "", want: str = "") -> str:
    import memory
    state = {"task": task, "source": source, "reply_to": reply_to,
             "want": want or ("short" if source == "whatsapp" else "report"),
             "step": 0, "history": []}

    cfg = {"configurable": {"thread_id": uuid.uuid4().hex[:12]},
           "recursion_limit": sys.maxsize}

    run_id = memory.start_run("adhoc", source, task)
    try:
        final = app.invoke(state, cfg)
        answer = (final.get("answer") or "").strip() or "(no answer produced)"
        status = "ok" if final.get("verdict") == "done" else "incomplete"
        memory.finish_run(run_id, status, answer)
        return answer
    except Exception as exc:  # noqa: BLE001
        log.exception("job failed")
        memory.finish_run(run_id, "error", str(exc))
        memory.journal_append(
            "adhoc",
            f"### Task: {task}\n- Status: failed\n\n"
            f"**What worked**\n- No completed result recorded.\n\n"
            f"**What failed**\n- Job raised: {exc}\n",
        )
        return f"job failed: {exc}"


def self_job(app) -> None:
    import memory
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex[:12]},
           "recursion_limit": sys.maxsize}
    try:
        final = app.invoke({"task": "", "source": "self", "step": 0,
                            "history": []}, cfg)
    except Exception as exc:  # noqa: BLE001
        failed_run = memory.finish_latest_run("self", "error", str(exc))
        if failed_run:
            memory.journal_append(
                failed_run["mission"],
                f"### Task: {failed_run['task']}\n- Status: failed\n\n"
                f"**What worked**\n- No completed result recorded.\n\n"
                f"**What failed**\n- Worker exception: {exc}\n",
            )
        log.exception("self-selected mission failed; recorded and moving on")
        return
    mission = final.get("mission", "idle")
    answer = (final.get("answer") or "").strip()
    if not answer or mission == "idle":
        log.info("nothing due")
        return
    status = ("ok" if final.get("verdict") == "done" else
              "needs_owner" if final.get("verdict") == "ask_owner" else "incomplete")
    if final.get("run_id"):
        memory.finish_run(final["run_id"], status, answer)
    if status == "incomplete":
        log.warning("self-selected mission %s incomplete; suppressing owner notification", mission)
        return
    send_out(os.environ.get("JARVIS_OWNER_JID", ""), answer)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", metavar="TASK", help="run one task and exit")
    ap.add_argument("--reply-to", default="", help="destination for --once results")
    ap.add_argument("--background", action="store_true",
                    help="with --once: write the answer to the outbox instead of "
                         "printing it (used by the delegate tool)")
    ap.add_argument("--self", action="store_true", help="run one self-chosen job")
    ap.add_argument("--approve", metavar="NAME",
                    help="approve a self-written tool that is pending review")
    ap.add_argument("--reject", metavar="NAME",
                    help="reject/delete a self-written tool that is pending review")
    ap.add_argument("--list-pending", action="store_true",
                    help="list self-written tools awaiting approval")
    args = ap.parse_args()

    if args.approve or args.reject or args.list_pending:
        import tools
        if args.approve:
            print(tools.approve_tool(args.approve))
        if args.reject:
            print(tools.reject_tool(args.reject))
        if args.list_pending:
            names = tools.list_pending()
            print("\n".join(names) if names else "(nothing pending)")
        return

    if os.environ.get("JARVIS_LEGACY_UNMEDIATED") != "1":
        print("worker.py no longer runs the agent by itself: tool calls must go through the\n"
              "jarvS runtime's permission checks. Use:\n"
              "  jarvs run \"<task>\"   one task\n"
              "  jarvs daemon         the queue/mission loop (WhatsApp jobs, missions)\n"
              "To run the old unmediated loop anyway, set JARVIS_LEGACY_UNMEDIATED=1.",
              file=sys.stderr)
        sys.exit(2)

    setup_logging()
    HOME.mkdir(parents=True, exist_ok=True)
    from graph import compiled

    app = compiled()

    if args.once:
        answer = run_job(app, args.once, "cli", args.reply_to)
        if args.background and args.reply_to:
            send_out(args.reply_to, answer)
        else:
            print(answer)
        return
    if args.self:
        self_job(app)
        return

    log.info("worker up (legacy, unmediated). idle=%dmin poll=%ds", IDLE_MINUTES, POLL_SECONDS)
    while True:
        try:
            if run_next(app) == "idle":
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log.info("bye")
            return
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            time.sleep(30)


_last_activity = time.time()


def run_next(app) -> str:
    """One scheduling step: a queued job first, else a self-chosen mission once
    idle long enough. Returns "job", "self", "rest" or "idle"."""
    global _last_activity
    import memory
    import tools
    from graph import autonomous_work_allowed

    tools.reload_custom_tools()  # picks up owner-approved tools live
    job = memory.take_next()
    if job:
        log.info("JOB from %s: %s", job["source"], job["task"][:120])
        try:
            answer = run_job(app, job["task"], job["source"], job["reply_to"])
            send_out(job["reply_to"], answer)
        except BaseException:  # includes cancellation by the runtime
            memory.release(job["id"])
            raise
        memory.acknowledge(job["id"])
        _last_activity = time.time()
        return "job"
    if (time.time() - _last_activity) / 60 < IDLE_MINUTES:
        return "idle"
    _last_activity = time.time()
    if not autonomous_work_allowed():
        log.info("autonomous rest window; queued user work remains enabled")
        return "rest"
    log.info("idle %d min -> picking own work", IDLE_MINUTES)
    self_job(app)
    return "self"

if __name__ == "__main__":
    main()