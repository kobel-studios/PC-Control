"""iOS-style toggle switch widget for Tkinter.

A Canvas-based toggle that slides left/right and changes color:
  OFF = gray knob on the left
  ON  = green knob on the right

Usage:
    sw = ToggleSwitch(parent, text="Overclock", command=callback, initial=False)
    sw.pack()
    sw.set(True)        # programmatic toggle
    sw.get()            # -> True/False
    sw.configure(state="disabled")  # disable interaction
    sw.state(["disabled"])           # ttk-style state spec
    sw.instate(["disabled"])         # ttk-style state check

The widget exposes a Checkbutton-compatible API:
  - `text` shows a visible ON/OFF label next to the switch
  - `command` callback receives no arguments; read state via `get()`
  - `variable` (a tk.BooleanVar) can be supplied or auto-created
  - variable changes redraw WITHOUT firing command (set invoke=True to fire)
  - disabled blocks mouse click, Space, and Return
  - trace/animation callbacks cleaned up on destroy
"""
import tkinter as tk
from tkinter import ttk


class ToggleSwitch(tk.Frame):
    """A sliding on/off toggle styled like an iOS switch."""

    def __init__(self, master=None, *, text="", command=None, initial=False,
                 variable=None, state="normal", width=52, height=28,
                 off_color="#555555", on_color="#34c759",
                 knob_color="#ffffff", bg=None, **kwargs):
        if bg is None:
            try:
                bg = ttk.Style(master).lookup("TFrame", "background") or "#1e1e22"
            except Exception:
                bg = "#1e1e22"
        # do not pass `text` or `state` to tk.Frame
        kwargs.pop("text", None)
        super().__init__(master, background=bg, **kwargs)
        self._off_color = off_color
        self._on_color = on_color
        self._knob_color = knob_color
        self._bg = bg
        self._width = width
        self._height = height
        self._command = command
        self._state = state
        self._text = text
        self._animating = False
        self._destroyed = False

        if variable is not None:
            self.variable = variable
        else:
            self.variable = tk.BooleanVar(value=bool(initial))
        # keep external variable in sync if caller mutates it
        self._trace_name = self.variable.trace_add("write", self._var_changed)

        self.canvas = tk.Canvas(self, width=width, height=height,
                                background=bg, highlightthickness=0, bd=0,
                                takefocus=True)
        self.canvas.pack(side="left")

        # separate packed Label for the ON/OFF text (not clipped by Canvas)
        self._label_text = tk.StringVar()
        self._label = tk.Label(self, textvariable=self._label_text,
                               background=bg, foreground="#d0d0d0",
                               font=("Segoe UI", 9))
        self._label.pack(side="left", padx=(6, 0))

        self._knob_radius = (height // 2) - 3
        self._track_pad = 3
        self._x_off = self._track_pad + self._knob_radius
        self._x_on = width - self._track_pad - self._knob_radius

        self._draw()
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        # keyboard: Space / Return toggle when focused
        self.canvas.bind("<space>", self._on_key)
        self.canvas.bind("<Return>", self._on_key)
        self.bind("<space>", self._on_key)
        self.bind("<Return>", self._on_key)
        # cleanup
        self.bind("<Destroy>", self._on_destroy)

    # --- public API ---
    def get(self):
        return bool(self.variable.get())

    def set(self, value, *, invoke=False):
        if self._destroyed:
            return
        self.variable.set(bool(value))
        if invoke and self._command:
            self._command()

    def configure(self, cnf=None, **kw):
        if cnf is not None and isinstance(cnf, str):
            return self._configure_single(cnf, kw)
        cfg = cnf or {}
        cfg.update(kw)
        if "state" in cfg:
            self._state = cfg.pop("state")
        if "command" in cfg:
            self._command = cfg.pop("command")
        if "text" in cfg:
            self._text = cfg.pop("text")
        if "off_color" in cfg:
            self._off_color = cfg.pop("off_color")
        if "on_color" in cfg:
            self._on_color = cfg.pop("on_color")
        if "knob_color" in cfg:
            self._knob_color = cfg.pop("knob_color")
        super().configure(cfg)
        self._draw()

    def _configure_single(self, cnf, kw):
        if cnf == "state":
            self._state = kw.get(cnf, "normal")
            self._draw()
            return ()
        if cnf == "text":
            self._text = kw.get(cnf, "")
            self._draw()
            return ()
        return super().configure(cnf, **kw)

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def state(self, spec=None):
        """ttk-style state API. spec=None returns current state list;
        spec sets state (e.g. ["disabled"] or ["!disabled"])."""
        if spec is None:
            return [self._state] if self._state != "normal" else []
        for item in spec:
            if item == "disabled":
                self._state = "disabled"
            elif item == "!disabled":
                self._state = "normal"
        self._draw()
        return []

    def instate(self, spec, callback=None):
        """ttk-style instate check. Returns True if current state matches spec."""
        match = True
        for item in spec:
            if item == "disabled":
                match = match and self._state == "disabled"
            elif item == "!disabled":
                match = match and self._state != "disabled"
        if match and callback:
            callback()
        return match

    # --- internal ---
    def _var_changed(self, *_args):
        if self._destroyed:
            return
        self._draw()

    def _on_click(self, event=None):
        if self._state == "disabled" or self._destroyed:
            return
        self.variable.set(not self.variable.get())
        if self._command:
            self._command()

    def _on_key(self, event=None):
        self._on_click(event)
        return "break"

    def _on_destroy(self, event=None):
        if self._destroyed:
            return
        self._destroyed = True
        try:
            if self._trace_name:
                self.variable.trace_remove("write", self._trace_name)
        except Exception:
            pass

    def _draw(self):
        if self._destroyed:
            return
        self.canvas.delete("all")
        on = self.get()
        track_color = self._on_color if on else self._off_color
        # rounded track: center rectangle from x=r to x=width-r, plus end ovals
        r = self._height // 2
        x0, y0 = 0, 0
        x1, y1 = self._width, self._height
        # center rectangle (square corners covered by ovals)
        self.canvas.create_rectangle(
            r, y0, self._width - r, y1, fill=track_color, outline=""
        )
        # rounded corners via ovals at the ends
        self.canvas.create_oval(x0, y0, x0 + 2 * r, y1, fill=track_color, outline="")
        self.canvas.create_oval(x1 - 2 * r, y0, x1, y1, fill=track_color, outline="")
        # knob
        knob_x = self._x_on if on else self._x_off
        kr = self._knob_radius
        cy = self._height // 2
        self.canvas.create_oval(
            knob_x - kr, cy - kr, knob_x + kr, cy + kr,
            fill=self._knob_color, outline="#cccccc"
        )
        # update the packed Label text with name + ON/OFF state
        if self._text:
            state_text = "ON" if on else "OFF"
            self._label_text.set(f"{self._text} {state_text}")
        else:
            self._label_text.set("ON" if on else "OFF")
        # dim appearance when disabled
        if self._state == "disabled":
            self.canvas.itemconfigure("all", state="disabled")
            self._label.configure(foreground="#707070")
        else:
            self._label.configure(foreground="#d0d0d0")
