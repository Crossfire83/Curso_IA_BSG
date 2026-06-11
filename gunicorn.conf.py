"""Gunicorn configuration file with lifecycle logging hooks."""

import logging
import time

logger = logging.getLogger("gunicorn.error")

# Store startup timestamp for uptime tracking
_boot_time = time.time()


def on_starting(server):
    """Called just when the master process is initialized."""
    logger.info("═══ Gunicorn master starting (pid: %d) ═══", server.pid)
    logger.info("Workers: %s | Threads: %s | Timeout: %ss",
                server.cfg.workers, server.cfg.threads, server.cfg.timeout)
    logger.info("Bind: %s | Worker class: %s",
                server.cfg.bind, server.cfg.worker_class)


def when_ready(server):
    """Called just after the server is started."""
    logger.info("═══ Gunicorn master ready (pid: %d) — accepting connections ═══", server.pid)


def pre_fork(server, worker):
    """Called just before a worker is forked."""
    logger.info("Pre-fork: about to spawn worker")


def post_fork(server, worker):
    """Called just after a worker has been forked."""
    logger.info("Worker spawned (pid: %d)", worker.pid)


def post_worker_init(worker):
    """Called just after a worker has initialized the application."""
    elapsed = time.time() - _boot_time
    logger.info("Worker (pid: %d) app loaded — %.2fs since master start", worker.pid, elapsed)


def worker_exit(server, worker):
    """Called when a worker is exiting."""
    logger.info("Worker exiting (pid: %d)", worker.pid)
