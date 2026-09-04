"""Independent loops for the development all-in-one worker, never a batch join."""
from contextlib import contextmanager
import logging
from threading import Event, Thread


LANE_SHUTDOWN_SECONDS = 20


@contextmanager
def independent_comment_lane(role, *, stop_event, run):
    if role != "all":
        yield stop_event
        return
    shared_stop = stop_event or Event()
    errors = []

    def execute():
        try:
            run(shared_stop)
        except BaseException as exc:
            logging.getLogger(__name__).exception("independent comment generation loop failed")
            errors.append(exc)

    thread = Thread(target=execute, name="comment-generation-loop", daemon=True)
    thread.start()
    try:
        yield shared_stop
    finally:
        shared_stop.set()
        thread.join(timeout=LANE_SHUTDOWN_SECONDS)
        if thread.is_alive():
            raise RuntimeError("comment_generation_shutdown_timeout")
        if errors:
            raise RuntimeError("comment_generation_loop_failed") from errors[0]
