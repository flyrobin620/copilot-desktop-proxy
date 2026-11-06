"""Windows handle-based worker lifetime; never target a PID from a state file."""
import os
import subprocess
import sys
import threading
import unittest

from parent_lifetime import watch_parent
from supervisor import hidden_process_options


@unittest.skipUnless(os.name == 'nt', 'Windows process-handle monitoring')
class ParentLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                        **hidden_process_options())
        self.watch = None

    def tearDown(self):
        if self.watch:
            self.watch[0].set()
            self.watch[1].join(timeout=3)
            self.assertFalse(self.watch[1].is_alive())
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)

    def test_supervisor_handle_exit_triggers_shutdown(self):
        stopped, events = threading.Event(), []
        class Server:
            def shutdown(self): stopped.set()
        class Log:
            def event(self, event, **fields): events.append(event)
        self.watch = watch_parent(self.process.pid, Server(), Log())
        self.assertFalse(stopped.is_set())
        self.process.terminate()
        self.process.wait(timeout=5)
        self.assertTrue(stopped.wait(3))
        self.assertIn('supervisor_lost', events)

    def test_normal_worker_shutdown_cancels_monitor_without_stopping_parent(self):
        stopped = threading.Event()
        class Server:
            def shutdown(self): stopped.set()
        class Log:
            def event(self, *args, **kwargs): pass
        self.watch = watch_parent(self.process.pid, Server(), Log())
        self.watch[0].set()
        self.watch[1].join(timeout=3)
        self.assertFalse(stopped.is_set())
        self.assertIsNone(self.process.poll())


if __name__ == '__main__':
    unittest.main()
