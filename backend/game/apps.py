import os
import threading

from django.apps import AppConfig


class GameConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "game"

    def ready(self) -> None:
        """Start the room sweeper, but only when compose says so.

        THE ENV VAR IS THE GUARD AND NOT A CONVENIENCE: `ready()` also runs under
        `manage.py test`, and a thread deleting rows under the suite is a suite that passes or fails
        according to the wall clock. Absent the variable nothing starts, so the destructor is
        opt-in and the suite is deterministic.

        IT ALSO KEEPS THE THREAD IN THE RIGHT PROCESS. The sweeper reads `_presence`, a dict in
        memory of the process that serves the streams, so it has to run there and nowhere else; a
        `manage.py` loop would see no heartbeat anywhere and delete games being played.

        `game.views` IS IMPORTED HERE AND NOT AT MODULE LEVEL, because `views` imports this app's
        models: importing it at import time of `apps.py` would make the app's own load depend on a
        module that depends on it.
        """
        if os.environ.get("GENAI_SWEEP") != "1":
            return
        from game.views import _sweep_forever

        threading.Thread(target=_sweep_forever, daemon=True, name="room-sweep").start()