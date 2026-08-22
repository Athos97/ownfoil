"""Dynamic pool of task worker subprocesses, shared by the Gunicorn (run.py) and local (local.py) entrypoints."""
import logging
import threading
from multiprocessing import Process, Event as MPEvent

logger = logging.getLogger('main')

# How often the watchdog checks that every worker process is still alive.
WATCHDOG_INTERVAL = 30.0


class WorkerPool:
    """Manages a dynamic pool of task worker subprocesses.

    A watchdog thread replaces any worker process that died without notice
    (OOM, segfault, kill -9): without it, a dead worker leaves a 'running'
    task row forever and - worse for the io group of one - blocks every
    remaining worker from claiming group tasks while the pool looks healthy."""

    def __init__(self, app, initial_count=1):
        self.app = app  # for app-context when reaping a stopped worker's running task
        self.workers = {}  # worker_id -> (Process, MPEvent)
        self._lock = threading.RLock()
        self._next_id = 1
        self._watchdog_stop = threading.Event()
        self._scale_to(initial_count)
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name='worker-watchdog', daemon=True)
        self._watchdog.start()

    def _start_worker(self, worker_id=None):
        """Start a single worker process. Reuses worker_id if given, else allocates a new one."""
        from worker import start_worker_process
        if worker_id is None:
            worker_id = self._next_id
            self._next_id += 1
        stop_event = MPEvent()
        proc = Process(target=start_worker_process, args=(stop_event, worker_id))
        proc.start()
        self.workers[worker_id] = (proc, stop_event)
        logger.info(f'Worker-{worker_id} started (pid={proc.pid}).')
        return worker_id

    def _stop_worker(self, worker_id, force=False):
        """Stop a worker by ID. If force, terminate immediately instead of waiting for graceful exit."""
        if worker_id not in self.workers:
            return
        proc, stop_event = self.workers.pop(worker_id)
        if force:
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
        else:
            stop_event.set()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
        logger.info(f'Worker-{worker_id} stopped.')
        # Process is gone: reap any task it was running so its cleanup hook runs).
        from tasks import reap_worker_task
        with self.app.app_context():
            reap_worker_task(worker_id)

    def restart_worker(self, worker_id):
        """Forcefully stop a worker mid-task and start a replacement reusing the same id."""
        with self._lock:
            if worker_id not in self.workers:
                return False
            self._stop_worker(worker_id, force=True)
            self._start_worker(worker_id=worker_id)
            return True

    def _watchdog_loop(self):
        """Replace workers whose process died unexpectedly, reusing the id.

        _stop_worker already reaps the dead worker's 'running' task (failing it
        and running its cleanup hook), so a replacement is all that is left."""
        while not self._watchdog_stop.wait(WATCHDOG_INTERVAL):
            try:
                with self._lock:
                    for worker_id, (proc, _event) in list(self.workers.items()):
                        if not proc.is_alive():
                            logger.warning(
                                f'Worker-{worker_id} (pid={proc.pid}) died '
                                f'unexpectedly (exitcode={proc.exitcode}); restarting.')
                            self._stop_worker(worker_id, force=True)
                            self._start_worker(worker_id=worker_id)
            except Exception as e:
                logger.error(f'Worker watchdog error: {e}')

    def _scale_to(self, desired_count):
        """Scale the pool to the desired number of workers."""
        current = len(self.workers)
        if desired_count > current:
            for _ in range(desired_count - current):
                self._start_worker()
        elif desired_count < current:
            # Stop the highest-numbered workers
            ids_to_stop = sorted(self.workers.keys(), reverse=True)[:current - desired_count]
            for wid in ids_to_stop:
                self._stop_worker(wid)

    def scale(self, desired_count):
        """Thread-safe scaling."""
        with self._lock:
            self._scale_to(desired_count)

    def shutdown(self):
        """Stop all workers."""
        with self._lock:
            self._watchdog_stop.set()
            for wid in list(self.workers.keys()):
                self._stop_worker(wid)

    def live_worker_ids(self):
        """Ids whose process is still alive — callers deciding whether a 'running'
        task row still has an owner consult this, not the table alone."""
        with self._lock:
            return {wid for wid, (proc, _e) in self.workers.items() if proc.is_alive()}

    @property
    def count(self):
        return len(self.workers)
