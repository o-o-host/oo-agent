"""Windows service wrapper (pywin32).

Run from an elevated prompt:

    python -m oo_agent.service.windows_service install
    python -m oo_agent.service.windows_service start
    python -m oo_agent.service.windows_service stop
    python -m oo_agent.service.windows_service remove

The service runs as LocalSystem, which is what the sensor and SMART
sources need. Configuration is read from the standard path
(``C:\\ProgramData\\oo-agent\\agent.ini``).
"""

from __future__ import annotations

import sys
import threading

import pywintypes
import servicemanager
import win32service
import win32serviceutil

_ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

from oo_agent.cli import run_daemon


class OoAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "oo-agent"
    _svc_display_name_ = "oo-agent metrics collector"
    _svc_description_ = (
        "Collects host metrics, sensors and inventory and pushes them "
        "to the monitoring backend."
    )

    def __init__(self, args) -> None:
        super().__init__(args)
        self._stop = threading.Event()

    def SvcStop(self) -> None:  # noqa: N802 - pywin32 API name
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self._stop.set()

    def SvcDoRun(self) -> None:  # noqa: N802 - pywin32 API name
        servicemanager.LogInfoMsg(f"{self._svc_name_}: starting")
        try:
            run_daemon(should_stop=self._stop.is_set)
        except Exception as exc:  # noqa: BLE001 - land it in the event log
            servicemanager.LogErrorMsg(f"{self._svc_name_}: crashed: {exc}")
            raise
        servicemanager.LogInfoMsg(f"{self._svc_name_}: stopped")


def main() -> None:
    if len(sys.argv) == 1:
        # No arguments means we were launched by the Windows service
        # manager itself (this is the path a frozen exe takes): host the
        # service directly instead of parsing a command line.
        try:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(OoAgentService)
            servicemanager.StartServiceCtrlDispatcher()
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
                raise
            print("This binary hosts the oo-agent Windows service.")
            print(
                "Usage: oo-agent-service.exe "
                "[--startup auto] install | start | stop | remove"
            )
            sys.exit(1)
    else:
        win32serviceutil.HandleCommandLine(OoAgentService)


if __name__ == "__main__":
    main()
