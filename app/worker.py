"""Task worker process — polls the tasks table and executes claimed tasks."""
import json
import datetime
import logging
import sys
from multiprocessing import Event

logger = logging.getLogger('worker')


class TaskWorker:
    def __init__(self, app, poll_interval=2.0, stop_event=None, worker_id=1):
        from settings import get_settings
        self.app = app
        self.poll_interval = poll_interval
        self.stop_event = stop_event or Event()
        self.worker_id = worker_id

        get_settings()  # prime settings cache and Keys.keys_loaded

    def claim_task(self):
        """Atomically claim the oldest pending task whose concurrency group has a free slot.
        Returns task_id or None."""
        from db import db
        import tasks as tasks_mod
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            # Exclude task types whose concurrency group is already at its limit.
            # Only rows whose worker is alive (or unknown owner) count: a ghost
            # row from a dead worker must not pin the group's slots - with io=1
            # it would idle every remaining worker until the watchdog reaps it.
            live_ids = self._live_worker_ids()
            if live_ids:
                marks = ",".join("?" * len(live_ids))
                cursor.execute(
                    f"SELECT task_name FROM tasks WHERE status = 'running' "
                    f"AND (worker_id IS NULL OR worker_id IN ({marks}))",
                    list(live_ids))
            else:
                cursor.execute("SELECT task_name FROM tasks WHERE status = 'running'")
            blocked = tasks_mod.blocked_task_names([r[0] for r in cursor.fetchall()])

            query = ("SELECT id FROM tasks WHERE status = 'pending' "
                     "AND (run_after IS NULL OR run_after <= datetime('now'))")
            params = []
            if blocked:
                query += " AND task_name NOT IN (%s)" % ",".join("?" * len(blocked))
                params = list(blocked)
            query += " ORDER BY created_at ASC LIMIT 1"
            cursor.execute(query, params)
            row = cursor.fetchone()
            if row is None:
                connection.commit()
                return None

            task_id = row[0]
            now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                "UPDATE tasks SET status = 'running', started_at = ?, worker_id = ? WHERE id = ? AND status = 'pending'",
                (now, self.worker_id, task_id)
            )
            if cursor.rowcount == 0:
                connection.commit()
                return None

            connection.commit()
            return task_id
        except Exception as e:
            connection.rollback()
            logger.error(f"Error claiming task: {e}")
            return None
        finally:
            connection.close()

    def _live_worker_ids(self):
        """Ids of workers this process knows are alive; empty means 'no pool
        knowledge' (standalone worker), where every running row counts."""
        import app as app_mod
        pool = getattr(app_mod, 'pool', None)
        if pool is None:
            return set()
        try:
            return pool.live_worker_ids()
        except Exception:
            return set()

    def execute_task(self, task_id):
        from tasks import get_registered_task, on_task_completed
        from db import db, Task
        import tasks as tasks_mod

        task = db.session.get(Task, task_id)
        if task is None:
            # Cancelled between claim and read: nothing to run, nothing to fail.
            return
        task_func = get_registered_task(task.task_name)
        input_data = json.loads(task.input_json) if task.input_json else {}
        display_name = tasks_mod.task_display_name(task.task_name, input_data)

        try:
            tasks_mod._current_task_id = task_id
            result = task_func(**input_data)
            tasks_mod._current_task_id = None

            # Re-read task — function may have set waiting_for_children
            db.session.expire(task)
            task = db.session.get(Task, task_id)
            if task is None:
                # Cancelled mid-run and deleted: drop the result quietly.
                return

            if task.status == 'waiting_for_children':
                # Children created before the park may already all be done, and their own
                # completion checks bailed out while this row still read 'running'.
                tasks_mod._try_complete_parent(task_id)
                return

            task.status = 'completed'
            task.completion_pct = 100
            task.exit_code = 0
            task.output_json = json.dumps(result) if result else None
            task.completed_at = datetime.datetime.utcnow()
            parent_id = task.parent_id
            db.session.commit()
        except Exception as e:
            tasks_mod._current_task_id = None
            logger.error(f"Task '{display_name}' ({task_id}) failed: {e}")
            db.session.rollback()
            task = db.session.get(Task, task_id)
            if task is None:
                return  # cancelled mid-run: the row is already gone
            if task.status in ('completed', 'waiting_for_children'):
                # The work itself finished; only the post-success bookkeeping
                # (parent completion, continuation) raised. Failing the row now
                # would also run its cleanup hook and undo real output - log and
                # leave the success in place.
                logger.error(f"Post-success bookkeeping for task {task_id} failed: {e}")
                return
            task.status = 'failed'
            task.error_message = str(e)
            task.exit_code = 1
            task.completed_at = datetime.datetime.utcnow()
            task_name, input_json, parent_id = task.task_name, task.input_json, task.parent_id
            db.session.commit()
            tasks_mod._run_cleanup_hook(task_name, input_json)
            on_task_completed(task_id, parent_id)
            return
        # Parent-completion and the delete happen outside the try: a raising
        # continuation must not flip the committed success into a failure.
        try:
            on_task_completed(task_id, parent_id)
        except Exception as e:
            logger.error(f"Completing parent of task {task_id} failed: {e}")
        if not parent_id:
            try:
                db.session.delete(task)
                db.session.commit()
            except Exception as e:
                logger.error(f"Deleting completed task {task_id} failed: {e}")

    def run(self):
        with self.app.app_context():
            logger.info(f"Worker started, polling every {self.poll_interval}s")
            while not self.stop_event.is_set():
                try:
                    task_id = self.claim_task()
                    if task_id is not None:
                        self.execute_task(task_id)
                    else:
                        self.stop_event.wait(self.poll_interval)
                except Exception as e:
                    # A bookkeeping bug must never take the worker process down:
                    # a dead worker leaves a ghost 'running' row and (worse) an
                    # unreplaced process. Log and keep polling.
                    logger.error(f"Worker loop error (continuing): {e}")
            logger.info("Worker stopped")


def start_worker_process(stop_event, worker_id=1):
    """Entry point for the worker subprocess."""
    import signal
    from setproctitle import setproctitle
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    setproctitle(f'ownfoil-worker-{worker_id}')

    from app import create_app
    import tasks  # noqa: F401 — registers @register_task decorators

    from utils import ColoredFormatter
    grey = '\033[90m'
    reset = '\033[0m'
    formatter = ColoredFormatter(
        f'[%(asctime)s.%(msecs)03d] %(levelname)s (%(module)s) {grey}worker-{worker_id}{reset} %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    app = create_app()
    worker = TaskWorker(app, poll_interval=2.0, stop_event=stop_event, worker_id=worker_id)
    worker.run()
