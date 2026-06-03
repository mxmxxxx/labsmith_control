"""In-memory mock of LabsmithBoard for UI testing without hardware.

This lets you exercise the GUI (connect, manual control buttons, Flow Designer,
Flow Graph) on a machine that has no LabSmith board / COM port attached. It
mimics the small surface of ``LabsmithBoard`` / ``CSyringe`` / ``CManifold``
that ``labsmith_gui`` actually calls. No uProcess / COM access is performed.

Select "Mock (test) board" in the COM Port box and click Connect Board.
"""
import time

try:
    from output_log import output_txt_path
except Exception:  # pragma: no cover - logging is best-effort only
    output_txt_path = None


def _log(message: str) -> None:
    """Append to OUTPUT.txt so the GUI log panel shows mock actions."""
    line = f"[MOCK] {message}"
    print(line)
    if output_txt_path is None:
        return
    try:
        with open(output_txt_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


class MockSyringe:
    """Simulated SPS01 syringe pump wrapper."""

    def __init__(self, index: int, name: str, addr: int):
        self.index = index
        self.name = name
        self.add_syr = addr
        self.address = addr
        # Default to a standard SPS01 size (20 µL) so the GUI size dropdown,
        # info label and set-point range all agree out of the box.
        self.diameter = 1.457
        self.maxVolume = 20.0
        self.minFlowrate = 1.0
        self.maxFlowrate = 6000.0

        self.volume_ul = 0.0
        self.Flowrate = 100.0
        self.FlagIsOnline = True
        self.FlagIsMoving = False
        self.FlagIsDone = True
        self.FlagIsStalled = False
        self.FlagIsMovingIn = False
        self.FlagIsMovingOut = False
        self.FlagReady = True

    def UpdateStatus(self):
        return True

    def MoveTo(self, flowrate, volume):
        _log(f"Syringe {self.name}: MoveTo(flowrate={flowrate}, volume={volume})")
        self.Flowrate = float(flowrate)
        self.volume_ul = float(volume)
        self.FlagIsDone = True
        self.FlagIsMoving = False
        return True

    def Stop(self):
        _log(f"Syringe {self.name}: Stop()")
        self.FlagIsMoving = False
        self.FlagIsDone = True
        return True

    def ResetRuntimeState(self):
        before = self.volume_ul
        self.FlagIsStalled = False
        self.FlagIsMoving = False
        self.FlagIsDone = True
        self.FlagReady = True
        _log(f"Syringe {self.name}: ResetRuntimeState()")
        return {
            "before_volume_ul": before,
            "after_volume_ul": self.volume_ul,
            "attempted_zero_recover": False,
        }

    def BeginManualMicrostep(self, push: bool) -> bool:
        _log(f"Syringe {self.name}: BeginManualMicrostep(push={push})")
        self.FlagIsMovingIn = bool(push)
        self.FlagIsMovingOut = not bool(push)
        return True

    def MicrostepRepeat(self, n: int):
        _log(f"Syringe {self.name}: MicrostepRepeat({n})")
        return True

    def MoveToPosition16(self, pos: int) -> bool:
        _log(f"Syringe {self.name}: MoveToPosition16({pos})")
        return True

    def SetMaxVolume(self, max_volume_ul, stroke_mm: float = 12.0):
        import math
        v = float(max_volume_ul)
        self.diameter = math.sqrt(4.0 * v / (math.pi * float(stroke_mm)))
        self.maxVolume = v
        if self.volume_ul > v:
            self.volume_ul = v
        _log(
            f"Syringe {self.name}: size set to {v:.4g} µL "
            f"(diameter {self.diameter:.4f} mm)."
        )
        return {
            "diameter": self.diameter,
            "maxVolume": self.maxVolume,
            "minFlowrate": self.minFlowrate,
            "maxFlowrate": self.maxFlowrate,
        }

    def Calibrate(self, timeout_seconds: int = 180):
        _log(f"Syringe {self.name}: Calibrate() (auto-cal simulated).")
        return True

    def MoveDirectional(self, flowrate, amount_ul, push_out):
        current = self.volume_ul if isinstance(self.volume_ul, (int, float)) else 0.0
        amount = abs(float(amount_ul))
        target = current - amount if push_out else current + amount
        if target < 0.0:
            target = 0.0
        if target > float(self.maxVolume):
            target = float(self.maxVolume)
        self.MoveTo(flowrate, target)
        _log(
            f"Syringe {self.name}: MoveDirectional("
            f"{'push out' if push_out else 'pull in'}, amount={amount}, target={target})"
        )
        return target


class MockManifold:
    """Simulated C4VM 4-valve manifold wrapper."""

    def __init__(self, index: int, name: str, addr: int):
        self.index = index
        self.name = name
        self.add_man = addr
        self.address = addr
        self.V_status = [1, 1, 1, 1]
        self.V_missing = [False, False, False, False]
        self.FlagIsOnline = True
        self.FlagIsDone = True
        self.FlagIsMoving = False
        self.FlagIsStuck = False

    def UpdateStatus(self):
        return True

    def SwitchValves(self, v1, v2, v3, v4):
        _log(f"Manifold {self.name}: SwitchValves({v1}, {v2}, {v3}, {v4})")
        for i, v in enumerate((v1, v2, v3, v4)):
            if int(v) in (0, 1):
                self.V_status[i] = int(v)
        self.FlagIsDone = True
        return True

    def SetValvesNative(self, v1, v2, v3, v4):
        _log(f"Manifold {self.name}: SetValvesNative({v1}, {v2}, {v3}, {v4})")
        for i, v in enumerate((v1, v2, v3, v4)):
            self.V_status[i] = int(v)
        return True

    def SetSingleValveMotion(self, valve_index: int, code: int) -> bool:
        _log(f"Manifold {self.name}: SetSingleValveMotion(valve={valve_index}, code={code})")
        if 1 <= int(valve_index) <= 4:
            self.V_status[int(valve_index) - 1] = int(code)
        return True

    def Stop(self):
        _log(f"Manifold {self.name}: Stop()")
        return True


class MockLabsmithBoard:
    """Drop-in stand-in for LabsmithBoard with two syringes and one manifold."""

    def __init__(self, n_syringes: int = 2, n_manifolds: int = 1):
        self.isConnected = True
        self.isDisconnected = False
        self.Stop = False
        self.Pause = False
        self.Resume = False
        self.cancel_requested = False
        self.poll_hook = None

        self.SPS01 = [
            MockSyringe(i, f"SPS01_{i + 1}", 16 + i) for i in range(n_syringes)
        ]
        self.C4VM = [
            MockManifold(i, f"C4VM_{i + 1}", 10 + i) for i in range(n_manifolds)
        ]
        self.C4AM = []
        self.C4PM = []
        self.CEP01 = []
        _log(
            f"Connected mock board: {n_syringes} syringe(s), "
            f"{n_manifolds} manifold(s)."
        )

    def connected_devices(self):
        out = []
        for idx, dev in enumerate(self.SPS01):
            out.append(
                {
                    "type": "syringe",
                    "index": idx,
                    "addr": dev.add_syr,
                    "name": dev.name,
                }
            )
        for idx, dev in enumerate(self.C4VM):
            out.append(
                {
                    "type": "manifold",
                    "index": idx,
                    "addr": dev.add_man,
                    "name": dev.name,
                }
            )
        return out

    def FindIndexS(self, name):
        for i, dev in enumerate(self.SPS01):
            if dev.name == name:
                return i
        raise ValueError(f"Mock: syringe {name!r} not found.")

    def FindIndexM(self, name):
        for i, dev in enumerate(self.C4VM):
            if dev.name == name:
                return i
        raise ValueError(f"Mock: manifold {name!r} not found.")

    def Move(self, namedevice, flowrate, volume):
        i = self.FindIndexS(namedevice)
        self.SPS01[i].MoveTo(flowrate, volume)

    def MoveParallel(self, moves):
        pairs = list(moves or [])
        if len(pairs) < 2:
            raise ValueError("MoveParallel requires at least 2 pumps.")
        if len(pairs) > 4:
            raise ValueError(f"MoveParallel supports at most 4 pumps, got {len(pairs)}.")
        names = [str(n).strip() for n, _, _ in pairs]
        if len(names) != len(set(names)):
            raise ValueError("MoveParallel: duplicate syringe in the same step.")
        _log(f"MoveParallel: {pairs}")
        for name, flow, vol in pairs:
            i = self.FindIndexS(name)
            self.SPS01[i].MoveTo(flow, vol)
        # Simulate a short parallel move while keeping the UI responsive.
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            if getattr(self, "cancel_requested", False) or getattr(self, "Stop", False):
                break
            hook = getattr(self, "poll_hook", None)
            if callable(hook):
                hook()
            time.sleep(0.05)

    def StopBoard(self):
        _log("StopBoard()")
        self.cancel_requested = True
        self.Stop = True
        for s in self.SPS01:
            s.Stop()
        for m in self.C4VM:
            m.Stop()

    def Disconnect(self):
        self.isConnected = False
        self.isDisconnected = True
        _log("Disconnect()")
        return "Mock board disconnected."
