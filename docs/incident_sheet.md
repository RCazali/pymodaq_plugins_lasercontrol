# Incident sheet — Laser Control (T11.2)

> **Reflex**: if the laser or the optics are in danger (beam leaving, heating, smell, unusual noise),
> **reduce the pump power by hand**. The extension does not control the pump. It is the ultimate
> physical safety action (D6).
>
> Only then: **EMERGENCY STOP** (red button, **F12** or **Pause**), observe, read the log.

What the software guarantees: in automatic mode, the motors only make **bounded relative steps** (S3),
inside an **envelope** around the engagement position (S4), with a limited **rate** (S5). No order is
queued (S10). A crash therefore leaves the motors still.

---

## 1. Emergency stop triggered (by you or by someone)

1. The badge shows **EMERGENCY STOP**. Every automation is stopped and a `stop_motion` was sent to
   **every** motor of the dashboard (S11). The state stays latched.
2. Look at what was moving: the cards of the Overview and the log (the last lines before
   "EMERGENCY STOP").
3. If a motor still seems to move (controller slow to stop, `limit` jog), use the stop button of the
   PyMoDAQ module or the controller itself.
4. Once the situation is understood: **Re-arm** (status bar). Nothing restarts: restart by hand what
   has to be.

## 2. The extension or PyMoDAQ crashes, freezes or closes

1. **Pump** if needed (see the reflex).
2. Motors: no automation runs anymore and no order is queued. The last steps sent are short.
   **Exception**: a jog in `limit` mode (Alignment) may continue up to Min/Max on the controller. Stop
   it on the controller (or switch it off) if this is the case.
3. Close PyMoDAQ, killing the process if it does not answer anymore.
4. Restart in order: dashboard, then extension. **Nothing starts by itself** (S9). The user interface
   is restored, without any active automation.
5. Before reactivating: check the positions (Overview, **⟳ Read** in Alignment) and the camera centroids.
6. To understand: the technical log `~/.pymodaq/laser_control/logs/laser_control.log` and the
   recording file of the session (readable up to the last write).

## 3. Computer B silent (red light, "stale data", "frame counter frozen")

Automatically: the automations using its data are **held** (HELD, S1). No motor moves and the PID
integral is frozen.

1. Nothing to do on the motor side.
2. On computer B: is the camera extension acquiring? Is the LECO coordinator running? Is the network
   working?
3. On computer A: the light must turn green again. If it stays gray or red, click **▶ Acquisition**,
   or re-initialize the LECO DAQ_Viewer in the dashboard.
4. Check the centroids, then **Resume** (Overview, or one click on each red button of the Alignment
   tab), once the values are back (O10-A).

## 4. Intensity at 0 or dropping (pump off, mode-lock lost)

Automatically: **hold** as soon as the intensity goes below the threshold (S2).

1. Click **ML OFF** (or **Unstable**) to annotate it, and add a **Note** if a cause is known.
2. Do not restart the PIDs to "look for" the laser: they cannot find a laser that is off.
3. Restore the laser by hand (pump, mode-lock). If needed, deactivate the stabilization and realign
   from the Alignment tab.
4. **Resume** is only accepted above the threshold plus the hysteresis. The reference stays the one
   taken at engagement. If the working power was changed on purpose: **Deactivate** then **Activate**,
   and the reference is taken again at that moment.
5. **ML ON** when the mode-lock is back.

## 5. A motor does not answer anymore ("timeout")

1. The matching automation is held and does not send anything to this motor anymore.
2. Check the controller: power supply, cable, state of the module in the dashboard. If needed,
   re-initialize the module.
3. Read the position again (**⟳ Read**) before restarting anything.

## 6. Envelope or rate reached ("envelope reached", "rate limit")

The PID or the optimizer pushed a motor up to its limit. It is held and the motor stayed inside the
envelope.

1. **Do not resume blindly.** Look at the PID curve: does it diverge? Is the sign wrong? Is the gain
   too high? Does the real drift exceed the allowed excursion?
2. If the position is correct: **New envelope reference** (PID tab), then **Resume**.
3. Otherwise: **Deactivate**, re-center by hand, fix the settings (`pid_tuning.md`), then reactivate.

## 7. Automation in "Error"

An exception in its computation stopped it (S8), without a new attempt. Read the message in the log
and restart once. If the error comes back, write down the message and the preset used.

## After an incident

- Add a **Note** describing what happened.
- Keep the HDF5 file of the session and the technical log.
- If it is reproducible, replay the scenario at the office with the simulator. The `SimB` viewer can
  make computer B silent, freeze its values or cause intensity drops.
