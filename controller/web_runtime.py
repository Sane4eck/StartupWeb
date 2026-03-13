from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

import serial.tools.list_ports
from serial import SerialException

from controller.devices_psu_riden import RidenPSU
from controller.devices_vesc import VESCDevice
from controller.logger_csv import CSVLogger
from controller.pump_profile import interp_profile, load_pump_profile_xlsx
from controller.cycle_fsm import CycleFSM
from controller.cyclogram_startup import build_cooling_fsm, build_startup_fsm
from scheme.cycle import CycleInputs
from scheme.pump_profile import PumpProfile
from scheme.startup import StartupConfig
from scheme.vesc import VESCValues


PUMP_PROFILE_XLSX = "_Cyclogram_Pump.xlsx"
# PUMP_PROFILE_XLSX = "_Cyclogram_Pump_test.xlsx"
CYCLOGRAM_DIRNAME = "file_cyclogram"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _nan() -> float:
    return float("nan")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _cmd_snapshot(target: Dict[str, Any], pole_pairs: int) -> Dict[str, Any]:
    mode = str(target.get("mode", "duty"))
    val = float(target.get("value", 0.0))
    pp = max(1, int(pole_pairs))

    cmd = {
        "cmd_mode": mode,
        "cmd_value": val,
        "cmd_duty": _nan(),
        "cmd_rpm": _nan(),
        "cmd_erpm": _nan(),
    }

    if mode == "rpm":
        cmd["cmd_rpm"] = val
        cmd["cmd_erpm"] = val * pp
    else:
        cmd["cmd_duty"] = _clamp01(val)

    return cmd


class WebControllerRuntime:
    def __init__(self, dt: float = 1/10, publish: Optional[Callable[[str, Any], None]] = None):
        self.dt = float(dt)
        self._external_publish = publish or (lambda _event, _payload: None)
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._started = False
        self._in_tick = False

        self._loop = None
        self._subscribers = set()

        self._t0 = time.monotonic()
        self.stage = "idle"

        self.pump = VESCDevice(timeout=0.01)
        self.starter = VESCDevice(timeout=0.01)
        self.psu = RidenPSU()

        self.pole_pairs_pump = 7
        self.pole_pairs_starter = 3

        self.pump_target = {"mode": "rpm", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = False
        self._psu_applied = {"v": None, "i": None, "out": None}

        self._last_pump = VESCValues()
        self._last_starter = VESCValues()
        self._last_psu: Dict[str, Any] = {}
        self._last_error: Optional[str] = None
        self._last_log: Optional[str] = None
        self._last_sample: Dict[str, Any] = {}
        self._last_status: Dict[str, Any] = {}

        self._psu_next_read = 0.0
        self._psu_next_cmd = 0.0

        self.logger = CSVLogger()
        self.logging_on = False
        self._next_flush_t = 0.0

        self.ui_hz = 5.0
        self.log_hz = 5.0
        self._ui_dt = 1.0 / self.ui_hz
        self._log_dt = 1.0 / self.log_hz
        self._next_ui_emit = 0.0
        self._next_log_write = 0.0

        self.hz_psu_read = 2
        self.hz_psu_cmd = 2

        self._fsm: Optional[CycleFSM] = None
        self._fsm_prev_state: Optional[str] = None
        self.startup_cfg = StartupConfig()

        self._pump_profile: Optional[PumpProfile] = None
        self._pump_profile_mtime: Optional[float] = None
        self._starter_profile: Optional[PumpProfile] = None
        self._starter_profile_mtime: Optional[float] = None

        self._pump_prof_active = False
        self._pump_prof_path = ""
        self._pump_prof_mtime: Optional[float] = None
        self._pump_prof: Optional[PumpProfile] = None
        self._pump_prof_t0 = 0.0
        self._pump_prof_prev_stage = "idle"

        self._valve_macro_active = False
        self._valve_macro_t0 = 0.0
        self._valve_boost_v = 18.0
        self._valve_boost_i = 20.0
        self._valve_boost_s = 1.0
        self._valve_hold_v = 5.0
        self._valve_hold_i = 20.0

    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    def set_loop(self, loop):
        self._loop = loop

    async def subscribe(self):
        q = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    async def unsubscribe(self, q):
        self._subscribers.discard(q)

    async def _publish_async(self, event, payload):
        dead = []

        for q in list(self._subscribers):
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                q.put_nowait({"kind": event, "payload": payload})
            except Exception:
                dead.append(q)

        for q in dead:
            self._subscribers.discard(q)

    def publish(self, event, payload):
        safe = _json_safe(payload)

        try:
            self._external_publish(event, safe)
        except Exception:
            pass

        if self._loop is None:
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self._publish_async(event, safe),
                self._loop,
            )
        except Exception:
            pass
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_evt.clear()
            self._t0 = time.monotonic()
            self.stage = "idle"
            self._emit_connected()

            self._thread = threading.Thread(target=self._run_loop, name="startup-web-runtime", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop_evt.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)

        with self._lock:
            self._fsm = None
            self._stop_pump_profile_internal(notify=False)
            self._valve_macro_active = False
            self.stage = "stop"
            self._force_all_off()
            self._disconnect_pump()
            self._disconnect_starter()
            self._disconnect_psu()
            try:
                self.logger.stop()
            except Exception:
                pass
            self.logging_on = False
            self._emit_connected()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return _json_safe({
                "stage": self.stage,
                "status": self._last_status,
                "sample": self._last_sample,
                "last_error": self._last_error,
                "last_log": self._last_log,
                "ports": self.list_ports(),
            })

    def get_state(self) -> Dict[str, Any]:
        return self.snapshot()

    def _push(self, event: str, payload: Any) -> None:
        safe = _json_safe(payload)
        if event == "sample":
            self._last_sample = safe
        elif event == "status":
            self._last_status = safe
        elif event == "error":
            self._last_error = str(safe)
        elif event == "log":
            self._last_log = str(safe)
        self.publish(event, safe)

    def _emit_connected(self) -> None:
        self._push("status", {
            "connected": {
                "pump": self.pump.is_connected,
                "starter": self.starter.is_connected,
                "psu": self.psu.is_connected,
            },
            "stage": self.stage,
            "log_path": self.logger.path,
            "pump_profile": {"active": self._pump_prof_active, "path": self._pump_prof_path},
            "valve_macro": {"active": self._valve_macro_active},
        })

    def _emit_error(self, message: str) -> None:
        self._push("error", message)

    def _emit_log(self, message: str) -> None:
        self._push("log", message)

    def _run_loop(self) -> None:
        next_tick = time.monotonic()
        while not self._stop_evt.is_set():
            next_tick += self.dt
            with self._lock:
                self._tick()
            sleep_s = max(0.0, next_tick - time.monotonic())
            if self._stop_evt.wait(sleep_s):
                break

    # -------- commands
    def cmd_ready(self, prefix: str) -> None:
        with self._lock:
            self._t0 = time.monotonic()
            self.stage = "ready"
            self._fsm = None
            self._stop_pump_profile_internal(notify=False)
            try:
                self.logger.stop()
            except Exception:
                pass
            try:
                path = self.logger.start(prefix=(prefix or "session"))
                self.logging_on = True
                now = time.monotonic()
                self._next_flush_t = now + 1.0
                self._next_ui_emit = now
                self._next_log_write = now
                self._push("status", {"ready": True, "log_path": path, "reset_plot": True})
            except Exception as e:
                self.logging_on = False
                self._emit_error(f"Logger start failed: {e}")
            self._ensure_run_profiles()
            self._emit_connected()

    def cmd_update_reset(self) -> None:
        with self._lock:
            self._t0 = time.monotonic()
            self.stage = "idle"
            self._fsm = None
            self._stop_pump_profile_internal(notify=False)
            self._emit_connected()

    def cmd_run_cycle(self) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._valve_macro_active = False
            if not self._ensure_run_profiles():
                return
            now = time.monotonic()
            inp = self._make_inputs(now)
            self._fsm = build_startup_fsm(self._pump_profile, self._starter_profile, self.startup_cfg)
            self._fsm_prev_state = None
            self._fsm.start(inp)
            self.stage = self._fsm.state
            self._emit_connected()

    def cmd_cooling_cycle(self, duty: float) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._valve_macro_active = False
            now = time.monotonic()
            inp = self._make_inputs(now)
            self._fsm = build_cooling_fsm(float(duty))
            self._fsm_prev_state = None
            self._fsm.start(inp)
            self.stage = self._fsm.state
            self._emit_connected()

    def cmd_stop_all(self) -> None:
        with self._lock:
            self._fsm = None
            self._stop_pump_profile_internal(notify=False)
            self._valve_macro_active = False
            self.stage = "stop"
            self._force_all_off()
            self._emit_connected()

    def cmd_valve_on(self) -> None:
        with self._lock:
            if not self.psu.is_connected:
                self._emit_error("Valve: PSU not connected")
                return

            self._fsm = None
            self._valve_macro_active = True
            self._valve_macro_t0 = time.monotonic()
            self.stage = "manual"
            self._set_psu_target(self._valve_boost_v, self._valve_boost_i, True)
            self._emit_connected()

    def cmd_valve_off(self) -> None:
        with self._lock:
            self._valve_macro_active = False
            self.stage = "manual"
            self._set_psu_target(0.0, 0.0, False)
            self._emit_connected()

    def cmd_start_pump_profile(self, path: str) -> None:
        with self._lock:
            path = (path or "").strip()
            if not path or not os.path.exists(path):
                self._emit_error(f"Pump profile: file not found: {path}")
                return
            self._fsm = None
            try:
                mtime = os.path.getmtime(path)
                if (self._pump_prof is None) or (self._pump_prof_path != path) or (self._pump_prof_mtime != mtime):
                    prof = load_pump_profile_xlsx(path, sheet_name=None)
                    if not prof.t:
                        raise RuntimeError("profile is empty")
                    self._pump_prof = prof
                    self._pump_prof_path = path
                    self._pump_prof_mtime = mtime
            except Exception as e:
                self._pump_prof = None
                self._pump_prof_path = ""
                self._pump_prof_mtime = None
                self._emit_error(f"Pump profile load error: {e}")
                return
            self._pump_prof_prev_stage = self.stage
            self.stage = "PumpProfile"
            self._pump_prof_active = True
            self._pump_prof_t0 = time.monotonic()
            self._emit_connected()

    def cmd_stop_pump_profile(self) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=True)

    def _stop_pump_profile_internal(self, notify: bool) -> None:
        if not self._pump_prof_active:
            return
        self._pump_prof_active = False
        self._pump_prof_t0 = 0.0
        self.stage = self._pump_prof_prev_stage if self._pump_prof_prev_stage else "idle"
        if notify:
            self._emit_connected()

    def cmd_connect_pump(self, port: str) -> None:
        with self._lock:
            if not port:
                return
            try:
                self.pump.connect(port)
            except Exception as e:
                self._emit_error(f"Pump connect error: {e}")
                self._disconnect_pump()
            self._emit_connected()

    def cmd_disconnect_pump(self) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self.pump_target = {"mode": "rpm", "value": 0.0}
            self._disconnect_pump()
            self._emit_connected()

    def cmd_connect_starter(self, port: str) -> None:
        with self._lock:
            if not port:
                return
            try:
                self.starter.connect(port)
            except Exception as e:
                self._emit_error(f"Starter connect error: {e}")
                self._disconnect_starter()
            self._emit_connected()

    def cmd_disconnect_starter(self) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self.starter_target = {"mode": "duty", "value": 0.0}
            self._disconnect_starter()
            self._emit_connected()

    def cmd_connect_psu(self, port: str) -> None:
        with self._lock:
            if not port:
                return
            try:
                self.psu.connect(port)
            except Exception as e:
                self._emit_error(f"PSU connect error: {e}")
                self._disconnect_psu()
            self._emit_connected()

    def cmd_disconnect_psu(self) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._valve_macro_active = False
            self._set_psu_target(0.0, 0.0, False)
            self._disconnect_psu()
            self._emit_connected()

    def cmd_set_pole_pairs_pump(self, pp: int) -> None:
        with self._lock:
            self.pole_pairs_pump = max(1, int(pp))

    def cmd_set_pole_pairs_starter(self, pp: int) -> None:
        with self._lock:
            self.pole_pairs_starter = max(1, int(pp))

    def cmd_set_pump_rpm(self, rpm: float) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)

            if self._fsm is not None and self._fsm.state == "Running":
                self.pump_target = {"mode": "rpm", "value": float(rpm)}
                return

            self._fsm = None
            self.stage = "manual"
            self.pump_target = {"mode": "rpm", "value": float(rpm)}
            self._emit_connected()

    def cmd_set_pump_duty(self, duty: float) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)

            if self._fsm is not None and self._fsm.state == "Running":
                self.pump_target = {"mode": "duty", "value": _clamp01(duty)}
                return

            self._fsm = None
            self.stage = "manual"
            self.pump_target = {"mode": "duty", "value": _clamp01(duty)}
            self._emit_connected()

    def cmd_set_starter_duty(self, duty: float) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._fsm = None
            self.stage = "manual"
            self.starter_target = {"mode": "duty", "value": _clamp01(duty)}
            self._emit_connected()

    def cmd_set_starter_rpm(self, rpm: float) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._fsm = None
            self.stage = "manual"
            self.starter_target = {"mode": "rpm", "value": float(rpm)}
            self._emit_connected()

    def cmd_psu_set_vi(self, v: float, i: float) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._fsm = None
            self._valve_macro_active = False
            self.stage = "manual"
            self._set_psu_target(float(v), float(i), bool(self.psu_target.get("out", False)))
            self._emit_connected()

    def cmd_psu_output(self, on: bool) -> None:
        with self._lock:
            self._stop_pump_profile_internal(notify=False)
            self._fsm = None
            self._valve_macro_active = False
            self.stage = "manual"
            self._set_psu_target(
                float(self.psu_target.get("v", 0.0)),
                float(self.psu_target.get("i", 0.0)),
                bool(on),
            )
            self._emit_connected()
    # -------- internals
    def _disconnect_pump(self) -> None:
        try:
            self.pump.disconnect()
        except Exception:
            pass
        self._last_pump = VESCValues()

    def _disconnect_starter(self) -> None:
        try:
            self.starter.disconnect()
        except Exception:
            pass
        self._last_starter = VESCValues()

    def _disconnect_psu(self) -> None:
        try:
            self.psu.disconnect()
        except Exception:
            pass
        self._last_psu = {}

    def _force_all_off(self) -> None:
        self.pump_target = {"mode": "rpm", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self._set_psu_target(0.0, 0.0, False)
        try:
            if self.pump.is_connected:
                self.pump.set_rpm_mech(0.0, self.pole_pairs_pump)
            if self.starter.is_connected:
                self.starter.set_duty(0.0)
            if self.psu.is_connected:
                self.psu.output(False)
        except Exception:
            pass

    def _set_psu_target(self, v: float, i: float, out: bool) -> None:
        v = float(v)
        i = float(i)
        out = bool(out)
        if self.psu_target.get("v") == v and self.psu_target.get("i") == i and self.psu_target.get("out") == out:
            return
        self.psu_target = {"v": v, "i": i, "out": out}
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

    def _ensure_run_profiles(self) -> bool:
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cyclogram_dir = os.path.join(project_root, CYCLOGRAM_DIRNAME)
            p_path = os.path.join(cyclogram_dir, PUMP_PROFILE_XLSX)
            p_mtime = os.path.getmtime(p_path)
            if (self._pump_profile is None) or (self._pump_profile_mtime != p_mtime):
                self._pump_profile = load_pump_profile_xlsx(p_path, sheet_name=None)
                self._pump_profile_mtime = p_mtime
            return True
        except Exception as e:
            self._emit_error(f"Cannot load run profiles: {e}")
            return False

    def _make_inputs(self, now: float) -> CycleInputs:
        t = now - self._t0
        state_t = self._fsm.state_time(now) if self._fsm is not None else 0.0
        return CycleInputs(
            now=now,
            t=t,
            state_t=state_t,
            pump_rpm=float(self._last_pump.rpm_mech),
            starter_rpm=float(self._last_starter.rpm_mech),
            pump_current=float(self._last_pump.current_motor),
            starter_current=float(self._last_starter.current_motor),
            psu_v_out=float(self._last_psu.get("v_out", 0.0)) if self._last_psu else 0.0,
            psu_i_out=float(self._last_psu.get("i_out", 0.0)) if self._last_psu else 0.0,
            psu_output=bool(self._last_psu.get("output", False)) if self._last_psu else False,
        )

    def _tick(self) -> None:
        if self._in_tick:
            return
        self._in_tick = True
        try:
            now = time.monotonic()
            t = now - self._t0

            pv = self._vesc_read(self.pump, self.pole_pairs_pump, label="pump")
            if pv is not None:
                self._last_pump = pv

            sv = self._vesc_read(self.starter, self.pole_pairs_starter, label="starter")
            if sv is not None:
                self._last_starter = sv

            if self.psu.is_connected and now >= self._psu_next_read:
                try:
                    self._last_psu = self.psu.read() or {}
                except Exception as e:
                    self._emit_error(f"PSU read error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()
                self._psu_next_read = now + 1/self.hz_psu_read #0.1

            if self._fsm is None and self._pump_prof_active and self._pump_prof is not None:
                elapsed = now - self._pump_prof_t0
                end_t = self._pump_prof.end_time
                if end_t > 0.0 and elapsed >= end_t:
                    self._stop_pump_profile_internal(notify=False)
                    self.pump_target = {"mode": "rpm", "value": 0.0}
                if self._pump_prof_active:
                    rpm_cmd = interp_profile(self._pump_prof, elapsed)
                    self.pump_target = {"mode": "rpm", "value": float(rpm_cmd)}
                    self.stage = "PumpProfile"

            if self._fsm is not None:
                inp = self._make_inputs(now)
                old_state = self._fsm_prev_state or self._fsm.state
                targets = self._fsm.tick(inp)
                new_state = self._fsm.state
                self.stage = new_state
                if new_state != old_state:
                    reason = targets.meta.get("transition_reason")
                    if new_state == "Fault" and reason:
                        self._emit_error(reason)
                apply_pump = new_state != "Running"
                if (not apply_pump) and (old_state != "Running") and (new_state == "Running"):
                    if targets.meta.get("apply_pump_once_on_running_entry"):
                        apply_pump = True
                if apply_pump:
                    self.pump_target = targets.pump
                self.starter_target = targets.starter
                self._set_psu_target(targets.psu["v"], targets.psu["i"], targets.psu["out"])
                self._fsm_prev_state = new_state

            if self._fsm is None and self._valve_macro_active:
                elapsed = now - self._valve_macro_t0
                if elapsed < self._valve_boost_s:
                    self._set_psu_target(self._valve_boost_v, self._valve_boost_i, True)
                else:
                    self._set_psu_target(self._valve_hold_v, self._valve_hold_i, True)

            if self.psu.is_connected and self._psu_dirty and now >= self._psu_next_cmd:
                try:
                    v = self.psu_target["v"]
                    i = self.psu_target["i"]
                    out = self.psu_target["out"]
                    if self._psu_applied["v"] != v or self._psu_applied["i"] != i:
                        self.psu.set_vi(v, i)
                        self._psu_applied["v"] = v
                        self._psu_applied["i"] = i
                    if self._psu_applied["out"] != out:
                        self.psu.output(out)
                        self._psu_applied["out"] = out
                    self._psu_dirty = False
                    self._psu_next_cmd = now + self.hz_psu_cmd #0.1
                except Exception as e:
                    self._emit_error(f"PSU cmd error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()

            self._vesc_send_and_request(self.pump, self.pump_target, self.pole_pairs_pump, "pump")
            self._vesc_send_and_request(self.starter, self.starter_target, self.pole_pairs_starter, "starter")

            pump_cmd = _cmd_snapshot(self.pump_target, self.pole_pairs_pump)
            starter_cmd = _cmd_snapshot(self.starter_target, self.pole_pairs_starter)
            sample = {
                "t": t,
                "stage": self.stage,
                "connected": {
                    "pump": self.pump.is_connected,
                    "starter": self.starter.is_connected,
                    "psu": self.psu.is_connected,
                },
                "pump": {
                    "rpm_mech": self._last_pump.rpm_mech,
                    "duty": self._last_pump.duty,
                    "current_motor": self._last_pump.current_motor,
                    **pump_cmd,
                },
                "starter": {
                    "rpm_mech": self._last_starter.rpm_mech,
                    "duty": self._last_starter.duty,
                    "current_motor": self._last_starter.current_motor,
                    **starter_cmd,
                },
                "psu": self._last_psu,
            }
            if now >= self._next_ui_emit:
                self._push("sample", sample)
                self._next_ui_emit = now + self._ui_dt

            if self.logging_on and self.logger.path and (now >= self._next_log_write):
                try:
                    row = self.logger.build_row(
                        t=t,
                        stage=self.stage,
                        pump_target=self.pump_target,
                        starter_target=self.starter_target,
                        pole_pairs_pump=self.pole_pairs_pump,
                        pole_pairs_starter=self.pole_pairs_starter,
                        pump_vals=self._last_pump,
                        starter_vals=self._last_starter,
                        psu=self._last_psu,
                    )
                    self.logger.write_row(row)
                    self._next_log_write = now + self._log_dt
                    if now >= self._next_flush_t:
                        self.logger.flush()
                        self._next_flush_t = now + 1.0
                except Exception as e:
                    self._emit_error(f"CSV error: {e}")
        finally:
            self._in_tick = False

    def _vesc_send_and_request(self, dev: VESCDevice, target: Dict[str, Any], pp: int, label: str) -> None:
        if not dev.is_connected:
            return
        try:
            mode = str(target.get("mode", "duty"))
            val = float(target.get("value", 0.0))
            if mode == "rpm":
                dev.set_rpm_mech(val, pp)
            else:
                dev.set_duty(_clamp01(val))
            dev.request_values()
        except (SerialException, OSError) as e:
            self._emit_error(f"{label} disconnected: {e}")
            if label == "pump":
                self._disconnect_pump()
            else:
                self._disconnect_starter()
            self._emit_connected()
        except Exception as e:
            self._emit_error(f"{label} error: {e}")

    def _vesc_read(self, dev: VESCDevice, pp: int, label: str) -> Optional[VESCValues]:
        if not dev.is_connected:
            return None
        try:
            return dev.read_values(pp, timeout_s=0.01)
        except (SerialException, OSError) as e:
            self._emit_error(f"{label} disconnected: {e}")
            if label == "pump":
                self._disconnect_pump()
            else:
                self._disconnect_starter()
            self._emit_connected()
            return None
        except Exception as e:
            self._emit_error(f"{label} read error: {e}")
            return None
