#!/usr/bin/env python3
"""Modern light-themed wxPython dashboard for the llm wrapper."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import wx
    import wx.lib.scrolledpanel as scrolled
except ImportError:
    print('Error: wxPython is required for the GUI.', file=sys.stderr)
    print(
        'Install with: pip install wxPython  (or: pixi install)',
        file=sys.stderr,
    )
    sys.exit(1)

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import llm  # noqa: E402


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG = wx.Colour(244, 244, 250)
CARD = wx.Colour(255, 255, 255)
BORDER = wx.Colour(220, 220, 240)
TEXT = wx.Colour(26, 26, 48)
TEXT_MUTED = wx.Colour(104, 104, 160)
ACCENT = wx.Colour(124, 58, 237)
SUCCESS = wx.Colour(5, 150, 105)
ERROR = wx.Colour(220, 38, 38)
WARN = wx.Colour(217, 119, 6)
DOT_IDLE = wx.Colour(180, 180, 200)

POLL_MS = 2000


def _font(size: int, *, bold: bool = False, mono: bool = False) -> wx.Font:
    family = wx.FONTFAMILY_TELETYPE if mono else wx.FONTFAMILY_DEFAULT
    weight = wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL
    return wx.Font(size, family, wx.FONTSTYLE_NORMAL, weight)


def _label(
    parent, text, *, size=11, bold=False, muted=False, mono=False, color=None
):
    lbl = wx.StaticText(parent, label=text)
    lbl.SetFont(_font(size, bold=bold, mono=mono))
    if color is not None:
        lbl.SetForegroundColour(color)
    elif muted:
        lbl.SetForegroundColour(TEXT_MUTED)
    else:
        lbl.SetForegroundColour(TEXT)
    return lbl


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def run_detached(provider: str, model: str, host: str, port: int, ctx: int | None = None) -> int:
    """Spawn the model server detached. Writes state file. Returns the PID."""
    cmd, cwd = llm._build_run_cmd(provider, model, host, port, [], ctx)
    env = os.environ.copy()
    if provider == 'ollama':
        env['OLLAMA_HOST'] = f'{host}:{port}'
        if ctx is not None:
            env['OLLAMA_NUM_CTX'] = str(ctx)

    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    llm.write_state(
        {
            'provider': provider,
            'model': model,
            'host': host,
            'port': port,
            'pid': proc.pid,
            'started_at': datetime.now().isoformat(timespec='seconds'),
        }
    )
    return proc.pid


def stop_running() -> tuple[bool, str]:
    state = llm.read_state()
    if not state:
        return False, 'No model is running.'

    pid = state.get('pid')
    provider = state.get('provider')
    model = state.get('model')
    port = state.get('port')

    if pid and llm.is_process_alive(pid):
        llm._kill_process(pid)
    elif port:
        # Model was auto-detected via HTTP (no PID stored) — find it by port.
        for p in llm._pids_on_port(port):
            llm._kill_process(p)

    if provider == 'ollama' and model:
        try:
            subprocess.run(
                ['ollama', 'stop', model], check=True, capture_output=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    llm.clear_state()
    return True, f'Stopped {provider}: {model}'


# ---------------------------------------------------------------------------
# Status banner
# ---------------------------------------------------------------------------


class StatusBanner(wx.Panel):
    def __init__(self, parent, on_stop, on_refresh):
        super().__init__(parent)
        self.SetBackgroundColour(CARD)
        self.on_stop = on_stop
        self.on_refresh = on_refresh

        outer = wx.BoxSizer(wx.HORIZONTAL)

        self.dot = wx.StaticText(self, label='●')
        self.dot.SetFont(_font(18))
        self.dot.SetForegroundColour(DOT_IDLE)
        outer.Add(
            self.dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 14
        )

        info = wx.BoxSizer(wx.VERTICAL)
        self.title = _label(self, 'No model running', size=14, bold=True)
        self.detail = _label(
            self, 'Use the Models tab to start one.', muted=True
        )
        info.Add(self.title, 0, wx.BOTTOM, 4)
        info.Add(self.detail, 0)
        outer.Add(info, 1, wx.ALIGN_CENTER_VERTICAL | wx.TOP | wx.BOTTOM, 16)

        self.stop_btn = wx.Button(self, label='Stop')
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda e: self.on_stop())
        self.stop_btn.Hide()
        outer.Add(self.stop_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

        self.refresh_btn = wx.Button(self, label='Refresh')
        self.refresh_btn.Bind(wx.EVT_BUTTON, lambda e: self.on_refresh())
        outer.Add(self.refresh_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)

        self.SetSizer(outer)

    def update_status(self, state):
        if not state:
            self.dot.SetForegroundColour(DOT_IDLE)
            self.title.SetLabel('No model running')
            self.detail.SetLabel('Use the Models tab to start one.')
            self.stop_btn.Hide()
            self.Layout()
            self.GetParent().Layout()
            return

        is_external = state.get('external', False)
        pid = state.get('pid')
        alive = False
        if is_external:
            alive = True
        elif pid:
            alive = llm.is_process_alive(pid)

        provider = state.get('provider', '?')
        model = state.get('model', '?')
        host = state.get('host', '127.0.0.1')
        port = state.get('port', '?')
        base_url = llm.BASE_URL_TEMPLATES.get(
            provider, 'http://{host}:{port}'
        ).format(host=host, port=port)

        if alive:
            self.dot.SetForegroundColour(SUCCESS)
            self.title.SetLabel(f'{provider}    ·   {model}')
            if pid is not None:
                self.detail.SetLabel(
                     f'{base_url}    PID {pid}     '
                     f'started {state.get("started_at", "?")}'
                    )
            else:
                self.detail.SetLabel(f'{base_url}    (external, auto-detected)')
        else:
            self.dot.SetForegroundColour(WARN)
            self.title.SetLabel(f'{provider}    ·   {model}    (not responding)')
            self.detail.SetLabel(f'PID {pid} no longer alive — clearing state…')
        self.stop_btn.Show()

        self.Layout()
        self.Refresh()
        self.GetParent().Layout()
        self.GetParent().Refresh()
        frame = self.GetTopLevelParent()
        if frame:
            frame.Layout()
            frame.Refresh()


# ---------------------------------------------------------------------------
# Models tab
# ---------------------------------------------------------------------------


class ModelsTab(wx.Panel):
    def __init__(self, parent, on_run, on_configure, on_delete, on_download):
        super().__init__(parent)
        self.SetBackgroundColour(BG)
        self.on_run = on_run
        self.on_configure = on_configure
        self.on_delete = on_delete
        self.on_download = on_download

        sizer = wx.BoxSizer(wx.VERTICAL)

        tb = wx.BoxSizer(wx.HORIZONTAL)
        tb.Add(
            _label(self, 'Local models', bold=True, size=14),
            1,
            wx.ALIGN_CENTER_VERTICAL,
        )

        download_btn = wx.Button(self, label='Download…')
        download_btn.Bind(wx.EVT_BUTTON, lambda e: self.on_download())
        tb.Add(download_btn, 0, wx.RIGHT, 6)

        refresh_btn = wx.Button(self, label='Refresh')
        refresh_btn.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        tb.Add(refresh_btn, 0)
        sizer.Add(tb, 0, wx.EXPAND | wx.ALL, 14)

        self.list = wx.ListCtrl(
            self,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_NONE,
        )
        self.list.SetBackgroundColour(CARD)
        self.list.AppendColumn('Provider', width=160)
        self.list.AppendColumn('Model', width=420)
        self.list.AppendColumn('Source', width=140)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_double)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.run_btn = wx.Button(self, label='Run selected')
        self.run_btn.Bind(wx.EVT_BUTTON, self._on_run_clicked)
        actions.Add(self.run_btn, 0, wx.RIGHT, 8)

        self.delete_btn = wx.Button(self, label='Delete')
        self.delete_btn.Bind(wx.EVT_BUTTON, self._on_delete_clicked)
        actions.Add(self.delete_btn, 0)

        sizer.Add(actions, 0, wx.EXPAND | wx.ALL, 14)
        self.SetSizer(sizer)
        self.refresh()

    def refresh(self):
        self.list.DeleteAllItems()
        ollama_models = llm.get_ollama_models()
        hf_models = llm.get_huggingface_models()

        rows = [('ollama', m, 'ollama') for m in ollama_models]
        rows += [('mlx-lm', m, 'huggingface') for m in hf_models]
        rows += [('vllm-mlx', m, 'huggingface') for m in hf_models]

        for provider, model, source in rows:
            idx = self.list.InsertItem(self.list.GetItemCount(), provider)
            self.list.SetItem(idx, 1, model)
            self.list.SetItem(idx, 2, source)

    def _selected_row(self):
        idx = self.list.GetFirstSelected()
        if idx == -1:
            return None
        return self.list.GetItemText(idx, 0), self.list.GetItemText(idx, 1)

    def _on_double(self, event):
        sel = self._selected_row()
        if sel:
            self.on_configure(*sel)

    def _on_run_clicked(self, event):
        sel = self._selected_row()
        if not sel:
            wx.MessageBox(
                'Select a model first.',
                'No selection',
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        self.on_run(*sel)

    def _on_delete_clicked(self, event):
        sel = self._selected_row()
        if not sel:
            wx.MessageBox(
                'Select a model first.',
                'No selection',
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        self.on_delete(*sel)


# ---------------------------------------------------------------------------
# Providers tab
# ---------------------------------------------------------------------------


class ProvidersTab(scrolled.ScrolledPanel):
    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(BG)

        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(
            _label(self, 'Providers', bold=True, size=14), 0, wx.ALL, 14
        )

        self.cards_sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(
            self.cards_sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14
        )

        self.SetSizer(self.sizer)
        self.refresh()

    def refresh(self):
        self.cards_sizer.Clear(True)
        for provider in llm.PROVIDERS:
            card = self._make_card(provider)
            self.cards_sizer.Add(card, 0, wx.EXPAND | wx.BOTTOM, 10)
        self.Layout()
        self.SetupScrolling(scroll_x=False, scrollToTop=False)

    def _make_card(self, provider):
        card = wx.Panel(self)
        card.SetBackgroundColour(CARD)
        sizer = wx.BoxSizer(wx.VERTICAL)

        head = wx.BoxSizer(wx.HORIZONTAL)
        head.Add(
            _label(card, provider, bold=True, size=14),
            1,
            wx.ALIGN_CENTER_VERTICAL,
        )
        executable, _ = llm._provider_executable(provider)
        sizer.Add(head, 0, wx.EXPAND | wx.ALL, 12)

        port = llm.DEFAULT_PORTS[provider]
        base_url = llm.BASE_URL_TEMPLATES[provider].format(
            host=llm.DEFAULT_HOST,
            port=port,
        )
        model_dir = llm.PROVIDER_MODEL_DIRS[provider]

        grid = wx.FlexGridSizer(rows=4, cols=2, hgap=12, vgap=4)
        grid.AddGrowableCol(1, 1)
        for key, val in [
            ('Executable', executable),
            ('Default port', str(port)),
            ('Base URL', base_url),
            ('Model dir', str(model_dir)),
        ]:
            grid.Add(
                _label(card, key, muted=True, size=10),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            grid.Add(
                _label(card, val, mono=True, size=10),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.EXPAND,
            )
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        if provider in ('ollama', 'mlx-lm', 'vllm-mlx'):
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.AddStretchSpacer()
            btn = wx.Button(card, label='Set path…')
            btn.Bind(wx.EVT_BUTTON, lambda e, p=provider: self._set_path(p))
            row.Add(btn, 0, wx.RIGHT, 12)
            sizer.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)

        card.SetSizer(sizer)
        return card

    def _set_path(self, provider):
        config = llm.read_config()
        current = config.get('providers', {}).get(provider, {}).get('path', '')

        dlg = wx.TextEntryDialog(
            self,
            f'Path to {provider} executable, or directory containing pixi.toml:',
            f'Set {provider} path',
            current,
        )
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetValue().strip()
            if path:
                config = llm.read_config()
                config.setdefault('providers', {}).setdefault(provider, {})[
                    'path'
                ] = path
                llm.write_config(config)
                self.refresh()
        dlg.Destroy()


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


class RunDialog(wx.Dialog):
    def __init__(
        self,
        parent,
        provider_default='',
        model_default='',
        host_default=None,
        port_default=None,
        ctx_default=None,
    ):
        super().__init__(parent, title='Model settings', size=(480, 300))
        self.SetBackgroundColour(BG)

        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=5, cols=2, hgap=12, vgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(
            _label(self, 'Provider', muted=True), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.provider = wx.Choice(self, choices=llm.PROVIDERS)
        if provider_default in llm.PROVIDERS:
            self.provider.SetSelection(llm.PROVIDERS.index(provider_default))
        elif 'mlx' in provider_default:
            self.provider.SetSelection(llm.PROVIDERS.index('mlx-lm'))
        else:
            self.provider.SetSelection(0)
        grid.Add(self.provider, 0, wx.EXPAND)

        grid.Add(_label(self, 'Model', muted=True), 0, wx.ALIGN_CENTER_VERTICAL)
        self.model = wx.TextCtrl(self, value=model_default)
        grid.Add(self.model, 0, wx.EXPAND)

        grid.Add(_label(self, 'Host', muted=True), 0, wx.ALIGN_CENTER_VERTICAL)
        self.host = wx.TextCtrl(self, value=host_default or llm.DEFAULT_HOST)
        grid.Add(self.host, 0, wx.EXPAND)

        grid.Add(_label(self, 'Port', muted=True), 0, wx.ALIGN_CENTER_VERTICAL)
        port_str = str(port_default) if port_default is not None else ''
        self.port = wx.TextCtrl(self, value=port_str)
        if not port_str:
            self.port.SetHint('default for provider')
        grid.Add(self.port, 0, wx.EXPAND)

        grid.Add(_label(self, 'Context window', muted=True), 0, wx.ALIGN_CENTER_VERTICAL)
        ctx_str = str(ctx_default) if ctx_default is not None else str(llm.DEFAULT_CTX)
        self.ctx = wx.TextCtrl(self, value=ctx_str)
        grid.Add(self.ctx, 0, wx.EXPAND)

        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 18)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
        run_btn = wx.Button(self, wx.ID_OK, 'Save & Run')
        run_btn.SetDefault()
        btn_row.Add(cancel, 0, wx.RIGHT, 6)
        btn_row.Add(run_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 18)

        self.SetSizer(sizer)

    def get_values(self):
        provider = llm.PROVIDERS[self.provider.GetSelection()]
        model = self.model.GetValue().strip()
        host = self.host.GetValue().strip() or llm.DEFAULT_HOST
        port_str = self.port.GetValue().strip()
        port = int(port_str) if port_str else llm.DEFAULT_PORTS[provider]
        ctx_str = self.ctx.GetValue().strip()
        ctx = int(ctx_str) if ctx_str else llm.DEFAULT_CTX
        return provider, model, host, port, ctx


class DownloadDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title='Download model', size=(480, 200))
        self.SetBackgroundColour(BG)

        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=2, cols=2, hgap=12, vgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(
            _label(self, 'Provider', muted=True), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.provider = wx.Choice(self, choices=llm.PROVIDERS)
        self.provider.SetSelection(0)
        grid.Add(self.provider, 0, wx.EXPAND)

        grid.Add(_label(self, 'Model', muted=True), 0, wx.ALIGN_CENTER_VERTICAL)
        self.model = wx.TextCtrl(self)
        grid.Add(self.model, 0, wx.EXPAND)

        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 18)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
        ok = wx.Button(self, wx.ID_OK, 'Download')
        ok.SetDefault()
        btn_row.Add(cancel, 0, wx.RIGHT, 6)
        btn_row.Add(ok, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 18)

        self.SetSizer(sizer)

    def get_values(self):
        provider = llm.PROVIDERS[self.provider.GetSelection()]
        model = self.model.GetValue().strip()
        return provider, model


# ---------------------------------------------------------------------------
# Main frame
# ---------------------------------------------------------------------------

DASHBOARD_VERSION = '1.0.0'
DOCS_URL = 'https://www.llmwrapper.com'


class LlmFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title='LLM Dashboard', size=(960, 680))
        self.SetBackgroundColour(BG)
        self.SetMinSize((720, 520))
        self.Bind(wx.EVT_CLOSE, self._on_close)

        root = wx.BoxSizer(wx.VERTICAL)

        header = wx.Panel(self)
        header.SetBackgroundColour(BG)
        h = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(header, label='LLM Dashboard')
        title.SetFont(_font(20, bold=True))
        title.SetForegroundColour(ACCENT)
        h.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        tag = wx.StaticText(header, label='   model manager')
        tag.SetForegroundColour(TEXT_MUTED)
        h.Add(tag, 0, wx.ALIGN_CENTER_VERTICAL)
        h.AddStretchSpacer()
        header.SetSizer(h)
        root.Add(header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.banner = StatusBanner(self, on_stop=self.on_stop, on_refresh=self._manual_refresh)
        root.Add(self.banner, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.notebook = wx.Notebook(self)
        self.notebook.SetBackgroundColour(BG)

        self.models_tab = ModelsTab(
            self.notebook,
            on_run=self.on_run,
            on_configure=self.on_configure,
            on_delete=self.on_delete,
            on_download=self.on_download,
        )
        self.notebook.AddPage(self.models_tab, 'Models')

        self.providers_tab = ProvidersTab(self.notebook)
        self.notebook.AddPage(self.providers_tab, 'Providers')

        root.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 16)

        self.SetSizer(root)
        self.CreateStatusBar()
        self.SetStatusText('Ready')

        self.SetAcceleratorTable(
            wx.AcceleratorTable(
                [
                    (wx.ACCEL_CMD, ord('W'), wx.ID_CLOSE),
                    (wx.ACCEL_CMD, ord('Q'), wx.ID_EXIT),
                ]
            )
        )
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=wx.ID_CLOSE)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=wx.ID_EXIT)

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.refresh_status(), self.timer)
        self.timer.Start(POLL_MS)

        self._detect_running_on_startup()
        self.Centre()

    def refresh_status(self):
        live = llm.discover_running_models()
        state = llm.read_state()

        # If this dashboard started the process, check whether it has died.
        if state and not state.get('external') and state.get('pid'):
            if not llm.is_process_alive(state['pid']):
                llm.clear_state()
                state = None

        # HTTP endpoints are the ground truth.
        if live:
            entry = live[0]
            if state and state.get('model') == entry['model']:
                new_state = dict(state)
                new_state.update({'external': True, 'pid': None})
            else:
                new_state = {
                    'provider': entry['provider'],
                    'model': entry['model'],
                    'host': entry['host'],
                    'port': entry['port'],
                    'pid': None,
                    'external': True,
                    'started_at': datetime.now().isoformat(timespec='seconds'),
                }
            llm.write_state(new_state)
            self.banner.update_status(new_state)
            return

        # Nothing responding via HTTP.  Keep PID state only while the process is
        # still starting (alive but not yet serving requests).
        if state and not state.get('external') and state.get('pid') and llm.is_process_alive(state['pid']):
            self.banner.update_status(state)
            return

        if state:
            llm.clear_state()
        self.banner.update_status(None)

    def _manual_refresh(self):
        self.refresh_status()

    def _on_close(self, event):
        self.timer.Stop()
        self.Destroy()

    def _detect_running_on_startup(self):
        self.refresh_status()

    def _check_already_running(self) -> bool:
        """Return True (and show a warning) if a model is already running."""
        current = llm.read_state()
        if not current:
            return False
        pid = current.get('pid')
        if (pid and llm.is_process_alive(pid)) or current.get('external'):
            wx.MessageBox(
                f'Already running: {current["provider"]} · {current["model"]}\n'
                'Stop it first.',
                'Already running',
                wx.OK | wx.ICON_WARNING,
            )
            return True
        return False

    def _launch(self, provider, model, host, port, ctx=None):
        try:
            pid = run_detached(provider, model, host, port, ctx)
            self.SetStatusText(f'Started {provider}: {model}  (PID {pid})')
            wx.CallLater(400, self.refresh_status)
        except Exception as e:
            wx.MessageBox(f'Failed to start: {e}', 'Error', wx.OK | wx.ICON_ERROR)

    def on_run(self, provider, model):
        """Run immediately using saved settings (or defaults). No dialog."""
        if provider not in llm.PROVIDERS and 'mlx' in provider:
            provider = 'mlx-lm'
        if self._check_already_running():
            return
        saved = llm.get_model_settings(provider, model)
        host = saved.get('host', llm.DEFAULT_HOST)
        port = saved.get('port', llm.DEFAULT_PORTS[provider])
        # Only pass ctx when the user has explicitly saved settings; otherwise
        # let the provider use its own default to avoid e.g. vllm rejecting a
        # context size that exceeds the model's training max.
        ctx = saved.get('ctx')
        self._launch(provider, model, host, port, ctx)

    def on_configure(self, provider, model):
        """Open settings dialog; save settings and launch on confirm."""
        if provider not in llm.PROVIDERS and 'mlx' in provider:
            provider = 'mlx-lm'
        saved = llm.get_model_settings(provider, model)
        host_saved = saved.get('host', llm.DEFAULT_HOST)
        port_saved = saved.get('port', llm.DEFAULT_PORTS[provider])
        ctx_saved = saved.get('ctx', llm.DEFAULT_CTX)

        dlg = RunDialog(
            self,
            provider_default=provider,
            model_default=model,
            host_default=host_saved,
            port_default=port_saved,
            ctx_default=ctx_saved,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        try:
            p, m, host, port, ctx = dlg.get_values()
        except ValueError as e:
            dlg.Destroy()
            wx.MessageBox(f'Invalid input: {e}', 'Error', wx.OK | wx.ICON_ERROR)
            return
        dlg.Destroy()

        if not m:
            wx.MessageBox('Model name required.', 'Error', wx.OK | wx.ICON_ERROR)
            return

        llm.save_model_settings(p, m, host, port, ctx)

        if self._check_already_running():
            return
        self._launch(p, m, host, port, ctx)

    def on_delete(self, provider, model):
        shared_note = ''
        if provider in ('mlx-lm', 'vllm-mlx'):
            shared_note = (
                '\n\nThis removes the shared HuggingFace cache — '
                'the model will be gone for both mlx-lm and vllm-mlx.'
            )
        dlg = wx.MessageDialog(
            self,
            f'Permanently delete {model}?{shared_note}',
            'Confirm delete',
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        confirmed = dlg.ShowModal() == wx.ID_YES
        dlg.Destroy()
        if not confirmed:
            return

        success, msg = llm.delete_model(provider, model)
        if success:
            self.SetStatusText(msg)
            self.models_tab.refresh()
        else:
            wx.MessageBox(msg, 'Delete failed', wx.OK | wx.ICON_ERROR)

    def on_stop(self):
        success, msg = stop_running()
        self.SetStatusText(msg)
        wx.CallLater(300, self.refresh_status)

    def on_download(self):
        dlg = DownloadDialog(self)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        provider, model = dlg.get_values()
        dlg.Destroy()
        if not model:
            wx.MessageBox(
                'Model name required.', 'Error', wx.OK | wx.ICON_ERROR
            )
            return
        self._download(provider, model)

    def _download(self, provider, model):
        progress = wx.ProgressDialog(
            'Downloading…',
            f'Downloading {model} via {provider}…',
            maximum=100,
            parent=self,
            style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE,
        )

        result = {'success': False, 'message': '', 'aborted': False}

        def worker():
            if provider == 'ollama':
                cmd = ['ollama', 'pull', model]
                missing = 'ollama'
            else:
                cmd = ['huggingface-cli', 'download', model]
                missing = 'huggingface-cli (pip install huggingface-hub)'
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                while proc.poll() is None:
                    if result['aborted']:
                        proc.terminate()
                        proc.wait()
                        return
                    time.sleep(0.2)
                _, err = proc.communicate()
                if proc.returncode == 0:
                    result['success'] = True
                    result['message'] = f'Downloaded {model}'
                else:
                    err_text = (
                        err.decode(errors='replace').strip() if err else ''
                    )
                    result['message'] = (
                        f'Download failed (exit {proc.returncode})\n{err_text}'
                    )
            except FileNotFoundError:
                result['message'] = f'Command not found: {missing}'

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while thread.is_alive():
            keep_going, _ = progress.Pulse()
            if not keep_going:
                result['aborted'] = True
                break
            wx.MilliSleep(120)
        thread.join(timeout=2)

        progress.Destroy()

        if result['aborted']:
            self.SetStatusText('Download cancelled.')
        elif result['success']:
            self.SetStatusText(result['message'])
            self.models_tab.refresh()
        else:
            wx.MessageBox(
                result['message'] or 'Unknown error',
                'Download failed',
                wx.OK | wx.ICON_ERROR,
            )


def main():
    app = wx.App(False)
    frame = LlmFrame()
    frame.Show()
    app.MainLoop()


if __name__ == '__main__':
    main()
