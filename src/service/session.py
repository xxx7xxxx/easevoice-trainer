import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from http import HTTPStatus
from typing import Dict, Any
from typing import Optional

import psutil
import torch
from fastapi import HTTPException
from logger import logger

from src.utils import config
from src.utils.helper.connector import ConnectorDataLoss, MultiProcessOutputConnector, ConnectorDataType
from src.utils.response import EaseVoiceResponse, ResponseStatus


class Status(Enum):
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


class SessionManager:
    """Manages training session, ensuring single GPU task execution and tracking task state."""

    _instance = None
    _lock = threading.Lock()
    MAX_SESSIONS = 10
    MAX_LOSS = 50
    session_list = dict()
    session_uuids = list()
    session_task = dict()
    session_subprocess = dict()
    # exist_session is the current running session. When session is completed, it will be None.
    # last_runned_session is the last completed session. It will be None if no session is completed.
    exist_session: Optional[str] = None
    last_runned_session: Optional[str] = None

    def __new__(cls):
        """Singleton pattern to ensure only one instance of SessionManager exists."""
        if not cls._instance:
            psutil.cpu_percent()
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(SessionManager, cls).__new__(cls)
                    cls._instance.exist_session = None
                    cls._instance.session_list = dict()
                    cls._instance.session_uuids = list()
                    cls._instance.session_task = dict()
                    cls._instance.session_subprocess = dict()
        return cls._instance

    def _check_session_limit(self):
        while len(self.session_uuids) > self.MAX_SESSIONS:
            # not remove the current running session
            if self.exist_session is not None and self.exist_session == self.session_uuids[0]:
                uuid = self.session_uuids.pop(1)
            else:
                uuid = self.session_uuids.pop(0)
            self.session_list.pop(uuid)

    def start_session(self, uuid: str, task_name: str, request: Optional[dict] = None):
        """Attempts to start a new session; rejects if another task is already running."""
        # if start session failed, raise exception, not store it
        if self.exist_session is not None:
            raise RuntimeError(
                f"A task is already running. Cannot submit another task!"
            )

        if is_dataclass(request):
            request = asdict(request)

        self.session_list[uuid] = {
            "uuid": uuid,
            "task_name": task_name,
            "request": request,
            "status": Status.RUNNING,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": None,  # Stores error details if task fails
        }
        self.session_uuids.append(uuid)
        self._check_session_limit()
        self.exist_session = uuid

    def add_session_task(self, uuid: str, task: asyncio.Task[Any]):
        self.session_task[uuid] = task

    def get_session_task(self, uuid: str):
        return self.session_task.get(uuid)

    def remove_session_task(self, uuid: str):
        self.session_task.pop(uuid)

    def add_session_subprocess(self, uuid: str, pid: int):
        self.session_subprocess[uuid] = pid

    def remove_session_subprocess(self, uuid: str):
        """
        The voice clone task not use subprocess. It may cause error when remove subprocess.
        """
        if uuid in self.session_subprocess:
            self.session_subprocess.pop(uuid)

    def get_session_subprocess(self, uuid: str) -> Optional[int]:
        return self.session_subprocess.get(uuid)

    def end_session(self, uuid: str, result: Any):
        """Marks task as completed successfully."""
        if uuid in self.session_list:
            session = self.session_list[uuid]
            session["status"] = Status.COMPLETED
            session["result"] = result
            self.session_list[uuid] = session

        if self.exist_session is not None and self.exist_session == uuid:
            self.exist_session = None
            self.last_runned_session = uuid

    def end_session_with_ease_voice_response(self, uuid: str, result: EaseVoiceResponse):
        """Marks task as completed successfully."""
        if uuid in self.session_list:
            session = self.session_list[uuid]
            if result.status == ResponseStatus.SUCCESS:
                session["status"] = Status.COMPLETED
                session["message"] = result.message
            else:
                session["status"] = Status.FAILED
                session["message"] = result.message
                session["error"] = result.message
            if result.data and len(result.data) > 0:
                session["data"] = result.data
            self.session_list[uuid] = session

        if self.exist_session is not None and self.exist_session == uuid:
            self.exist_session = None
            self.last_runned_session = uuid

    def fail_session(self, uuid: str, error: str):
        """Marks task as failed and stores error information."""
        if uuid in self.session_list:
            session = self.session_list[uuid]
            session["status"] = Status.FAILED
            session["error"] = error
            self.session_list[uuid] = session

        if self.exist_session is not None and self.exist_session == uuid:
            self.exist_session = None
            self.last_runned_session = uuid

    def update_session_info(self, uuid: str, info: Dict[str, Any]):
        """Updates task session with arbitrary info."""
        if not self.session_list[uuid]:
            raise RuntimeError("No active task to update session info!")
        self.session_list[uuid].update(info)

    def update_session_loss(self, uuid: str, loss: ConnectorDataLoss):
        if not self.session_list[uuid]:
            raise RuntimeError("No active task to update session loss!")
        losses = self.session_list[uuid].get("losses", [])
        losses.append(asdict(loss))
        if len(losses) > self.MAX_LOSS:
            losses.pop(0)
        self.session_list[uuid]["losses"] = losses

    def get_session_info(self) -> Dict[str, Any]:
        """Returns current task state information."""
        self.session_list.update(self._inject_monitor_metrics())
        return self.session_list

    def exist_running_session(self):
        """Returns whether there is a running session."""
        return self.exist_session is not None

    def get_current_session_info(self):
        """Returns the current running session or last completed session."""
        if self.exist_session is not None:
            # prevent update monitor metrics to current session
            metrics = self._inject_monitor_metrics()
            session = self.session_list.get(self.exist_session, {})
            metrics.update(session)
            return metrics
        if self.last_runned_session is not None:
            metrics = self._inject_monitor_metrics()
            session = self.session_list.get(self.last_runned_session, {})
            metrics.update(session)
            return metrics
        return {}

    @staticmethod
    def _inject_monitor_metrics() -> Dict[str, Any]:
        session = dict()
        if torch.cuda.is_available():
            session["gpu_percentage"] = f"{torch.cuda.utilization()}%"
            session["memory_allocated_percentage"] = f"{torch.cuda.memory_allocated() / max(torch.cuda.max_memory_allocated(), 1) * 100:.2f}%"
        session["cpu_percentage"] = f"{psutil.cpu_percent()}%"
        return {
            "monitor_metrics": session
        }


session_manager = SessionManager()


def backtask_with_session_guard(uuid: str, task_name: str, request_params: dict, func, **kwargs):
    try:
        session_manager.start_session(uuid, task_name, request_params)
    except Exception as e:
        logger.error(f"Failed to start session for task {task_name}: {e}", exc_info=True)
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail="There is an another task running.")

    def wrapper():
        try:
            func(**kwargs)
        except Exception as e:
            logger.error(f"Failed to do task for {task_name} with {request_params}: {e}", exc_info=True)
            session_manager.fail_session(uuid, str(e))
        finally:
            session_manager.remove_session_subprocess(uuid)

    thread = threading.Thread(target=wrapper)
    thread.start()


def start_task_with_subprocess(uid: str, cmd_file: str, request: Any):
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False) as fp:
        params = asdict(request)
        params = json.dumps(params)
        fp.write(params)
        temp_file_path = fp.name

    proc = subprocess.Popen(
        [sys.executable, os.path.join(config.cmd_path, cmd_file), "-c", temp_file_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=config.base_path,
    )
    session_manager.add_session_subprocess(uid, proc.pid)
    connector = MultiProcessOutputConnector()
    for data in connector.read_data(proc):
        if data.dataType == ConnectorDataType.RESP:
            session_manager.end_session_with_ease_voice_response(uid, data.response)
        elif data.dataType == ConnectorDataType.SESSION_DATA:
            session_manager.update_session_info(uid, data.session_data)
        elif data.dataType == ConnectorDataType.LOSS:
            session_manager.update_session_loss(uid, data.loss)


def _check_session(uid: str, task_name: str) -> Optional[EaseVoiceResponse]:
    session_info = session_manager.get_session_info()
    if not session_info:
        response = EaseVoiceResponse(ResponseStatus.FAILED, "No active task to stop.")
        session_manager.end_session_with_ease_voice_response(uid, response)
        session_manager.remove_session_task(uid)
        return response
    current_session = session_info.get(uid, {})
    if current_session.get("task_name") != task_name or current_session.get("status") != Status.RUNNING:
        response = EaseVoiceResponse(ResponseStatus.FAILED, "Task name does not match.")
        session_manager.end_session_with_ease_voice_response(uid, response)
        session_manager.remove_session_task(uid)
        return response
    return None


def async_stop_session(uuid: str, task_name: str):
    """Stops a session started in coroutine."""
    response = _check_session(uuid, task_name)
    if response:
        return response

    task = session_manager.get_session_task(uuid)
    if task:
        task.cancel()
        session_manager.remove_session_task(uuid)

        response = EaseVoiceResponse(ResponseStatus.SUCCESS, "Task stopped by user.")
        session_manager.end_session_with_ease_voice_response(uuid, response)
        return response
    response = EaseVoiceResponse(ResponseStatus.FAILED, "No task to stop.")
    session_manager.end_session_with_ease_voice_response(uuid, response)
    session_manager.remove_session_task(uuid)
    return response


def stop_task_with_subprocess(uuid: str, task_name: str):
    response = _check_session(uuid, task_name)
    if response:
        return response

    pid = session_manager.get_session_subprocess(uuid)
    if pid:
        _kill_proc_tree(pid)
        session_manager.remove_session_subprocess(uuid)
        response = EaseVoiceResponse(ResponseStatus.SUCCESS, "Task stopped by user.")
        session_manager.end_session_with_ease_voice_response(uuid, response)
        return response
    response = EaseVoiceResponse(ResponseStatus.FAILED, "No task to stop.")
    session_manager.end_session_with_ease_voice_response(uuid, response)
    return response


def _kill_proc_tree(pid, including_parent=True):
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    children = parent.children(recursive=True)
    for child in children:
        try:
            os.kill(child.pid, signal.SIGTERM)
        except OSError:
            pass
    if including_parent:
        try:
            os.kill(parent.pid, signal.SIGTERM)
        except OSError:
            pass
