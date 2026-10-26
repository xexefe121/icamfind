"""On-device speech for reminders and the "leaving without your keys" alert."""

from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger("icamfind.speech")


class Speaker:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._q: queue.Queue[str] = queue.Queue()
        if enabled:
            threading.Thread(target=self._run, name="speech", daemon=True).start()

    def say(self, text: str) -> None:
        log.info("SAY: %s", text)
        print(f"\a[iCamFind says] {text}", flush=True)
        if self.enabled:
            self._q.put(text)

    def _run(self) -> None:
        try:
            import pyttsx3
        except Exception:  # pragma: no cover - optional dependency
            log.warning("pyttsx3 unavailable; speech printed only")
            return
        while True:
            text = self._q.get()
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", 165)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
            except Exception as exc:  # pragma: no cover - audio device issues
                log.warning("speech failed: %s", exc)
