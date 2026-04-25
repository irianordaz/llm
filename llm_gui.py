#!/usr/bin/env python3
"""Modern light-themed wxPython GUI for the llm wrapper."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import wx
    import wx.lib.scrolledpanel as scrolled
except ImportError:
    print('Error: wxPython is required for the GUI.', file=sys.stderr)
    print('Install with: pip install wxPython  (or: pixi install)', file=sys.stderr)
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


def _label(parent, text, *, size=11, bold=False, muted=False, mono=False, color=None):
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


def run_detached(provider: str, model: str, host: str, port: int) -> int:
    """Spawn the model server detached. Writes state file. Returns the PID."""
    cmd, cwd = llm._build_run_cmd(provider, model, host, port, [])
    env = os.environ.copy()
    if provider == 'ollama':
        env['OLLAMA_HOST'] = f'{host}:{port}'

    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    llm.write_state({
        'provider': provider,
        'model': model,
        'host': host,
        'port': port,
        'pid': proc.pid,
        'started_at': datetime.now().isoformat(timespec='seconds'),
    })
    return proc.pid


def stop_running() -> tuple[bool, str]:
    state = llm.read_state()
    if not state:
        return False, 'No model is running.'

    pid = state.get('pid')
    provider = state.get('provider')
    model = state.get('model')

    if pid and llm.is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError) as e:
            return False, f'Failed to stop PID {pid}: {e}'

    if provider == 'ollama' and model:
        try:
            subprocess.run(['ollama', 'stop', model], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    llm.clear_state()
    return True, f'Stopped {provider}: {model}'


# ---------------------------------------------------------------------------
# Status banner
# ---------------------------------------------------------------------------


class StatusBanner(wx.Panel):
    def __init__(self, parent, on_stop):
        super().__init__(parent)
        self.SetBackgroundColour(CARD)
        self.on_stop = on_stop

        outer = wx.BoxSizer(wx.HORIZONTAL)

        self.dot = wx.StaticText(self, label='●')
        self.dot.SetFont(_font(18))
        self.dot.SetForegroundColour(DOT_IDLE)
        outer.Add(self.dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 14)

        info = wx.BoxSizer(wx.VERTICAL)
        self.title = _label(self, 'No model running', size=14, bold=True)
        self.detail = _label(self, 'Use the Models tab to start one.', muted=True)
        info.Add(self.title, 0, wx.BOTTOM, 4)
        info.Add(self.detail, 0)
        outer.Add(info, 1, wx.ALIGN_CENTER_VERTICAL | wx.TOP | wx.BOTTOM, 16)

        self.stop_btn = wx.Button(self, label='Stop')
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda e: self.on_stop())
        self.stop_btn.Hide()
        outer.Add(self.stop_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)

        self.SetSizer(outer)

    def update_status(self, state):
        if not state:
            self.dot.SetForegroundColour(DOT_IDLE)
            self.title.SetLabel('No model running')
            self.detail.SetLabel('Use the Models tab to start one.')
            self.stop_btn.Hide()
        else:
            pid = state.get('pid')
            alive = llm.is_process_alive(pid) if pid else False
            provider = state.get('provider', '?')
            model = state.get('model', '?')
            host = state.get('host', '127.0.0.1')
            port = state.get('port', '?')
            base_url = llm.BASE_URL_TEMPLATES.get(
                provider, 'http://{host}:{port}'
            ).format(host=host, port=port)

            if alive:
                self.dot.SetForegroundColour(SUCCESS)
                self.title.SetLabel(f'{provider}  ·  {model}')
                self.detail.SetLabel(
                    f'{base_url}    PID {pid}    '
                    f'started {state.get("started_at", "?")}'
                )
            else:
                self.dot.SetForegroundColour(WARN)
                self.title.SetLabel(f'{provider}  ·  {model}  (not responding)')
                self.detail.SetLabel(f'PID {pid} no longer alive — clearing state…')
            self.stop_btn.Show()

        self.Layout()
        self.GetParent().Layout()


# ---------------------------------------------------------------------------
# Models tab
# ---------------------------------------------------------------------------


class ModelsTab(wx.Panel):
    def __init__(self, parent, on_run, on_default, on_download):
        super().__init__(parent)
        self.SetBackgroundColour(BG)
        self.on_run = on_run
        self.on_default = on_default
        self.on_download = on_download

        sizer = wx.BoxSizer(wx.VERTICAL)

        tb = wx.BoxSizer(wx.HORIZONTAL)
        tb.Add(_label(self, 'Local models', bold=True, size=14),
               1, wx.ALIGN_CENTER_VERTICAL)

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
        self.list.AppendColumn('Provider', width=180)
        self.list.AppendColumn('Model', width=460)
        self.list.AppendColumn('Default', width=80)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_double)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.run_btn = wx.Button(self, label='Run selected')
        self.run_btn.Bind(wx.EVT_BUTTON, self._on_run_clicked)
        actions.Add(self.run_btn, 0, wx.RIGHT, 6)

        self.default_btn = wx.Button(self, label='Set as default')
        self.default_btn.Bind(wx.EVT_BUTTON, self._on_default_clicked)
        actions.Add(self.default_btn, 0)

        actions.AddStretchSpacer()
        self.default_label = _label(self, '', muted=True)
        actions.Add(self.default_label, 0, wx.ALIGN_CENTER_VERTICAL)

        sizer.Add(actions, 0, wx.EXPAND | wx.ALL, 14)
        self.SetSizer(sizer)
        self.refresh()

    def refresh(self):
        self.list.DeleteAllItems()
        config = llm.read_config()
        default_provider = config.get('default_provider')
        default_model = config.get('default_model')

        ollama_models = llm.get_ollama_models()
        hf_models = llm.get_huggingface_models()

        rows = (
            [('ollama', m) for m in ollama_models]
            + [('mlx-lm / vllm-mlx', m) for m in hf_models]
        )

        for provider, model in rows:
            idx = self.list.InsertItem(self.list.GetItemCount(), provider)
            self.list.SetItem(idx, 1, model)
            is_default = False
            if provider == 'ollama' and default_provider == 'ollama' and model == default_model:
                is_default = True
            elif (
                'mlx' in provider
                and default_provider in ('mlx-lm', 'vllm-mlx')
                and model == default_model
            ):
                is_default = True
            self.list.SetItem(idx, 2, '★' if is_default else '')

        if default_provider and default_model:
            self.default_label.SetLabel(
                f'Default: {default_provider} · {default_model}'
            )
        else:
            self.default_label.SetLabel('No default set')

    def _selected_row(self):
        idx = self.list.GetFirstSelected()
        if idx == -1:
            return None
        return self.list.GetItemText(idx, 0), self.list.GetItemText(idx, 1)

    def _on_double(self, event):
        self._on_run_clicked(event)

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

    def _on_default_clicked(self, event):
        sel = self._selected_row()
        if not sel:
            wx.MessageBox(
                'Select a model first.',
                'No selection',
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        self.on_default(*sel)


# ---------------------------------------------------------------------------
# Providers tab
# ---------------------------------------------------------------------------


class ProvidersTab(scrolled.ScrolledPanel):
    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(BG)

        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(_label(self, 'Providers', bold=True, size=14),
                       0, wx.ALL, 14)

        self.cards_sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(self.cards_sizer, 1,
                       wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

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
        head.Add(_label(card, provider, bold=True, size=14),
                 1, wx.ALIGN_CENTER_VERTICAL)
        executable, installed = llm._provider_executable(provider)
        status_color = SUCCESS if installed else ERROR
        status_label = '● installed' if installed else '● not installed'
        head.Add(_label(card, status_label, size=10, color=status_color),
                 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(head, 0, wx.EXPAND | wx.ALL, 12)

        port = llm.DEFAULT_PORTS[provider]
        base_url = llm.BASE_URL_TEMPLATES[provider].format(
            host=llm.DEFAULT_HOST, port=port,
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
            grid.Add(_label(card, key, muted=True, size=10),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(_label(card, val, mono=True, size=10),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.EXPAND)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        if provider in ('mlx-lm', 'vllm-mlx'):
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
                config.setdefault('providers', {}).setdefault(provider, {})['path'] = path
                llm.write_config(config)
                self.refresh()
        dlg.Destroy()


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


class RunDialog(wx.Dialog):
    def __init__(self, parent, provider_default='', model_default=''):
        super().__init__(parent, title='Run model', size=(480, 260))
        self.SetBackgroundColour(BG)

        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=4, cols=2, hgap=12, vgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(_label(self, 'Provider', muted=True),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.provider = wx.Choice(self, choices=llm.PROVIDERS)
        if provider_default in llm.PROVIDERS:
            self.provider.SetSelection(llm.PROVIDERS.index(provider_default))
        elif 'mlx' in provider_default:
            self.provider.SetSelection(llm.PROVIDERS.index('mlx-lm'))
        else:
            self.provider.SetSelection(0)
        grid.Add(self.provider, 0, wx.EXPAND)

        grid.Add(_label(self, 'Model', muted=True),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.model = wx.TextCtrl(self, value=model_default)
        grid.Add(self.model, 0, wx.EXPAND)

        grid.Add(_label(self, 'Host', muted=True),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.host = wx.TextCtrl(self, value=llm.DEFAULT_HOST)
        grid.Add(self.host, 0, wx.EXPAND)

        grid.Add(_label(self, 'Port', muted=True),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.port = wx.TextCtrl(self, value='')
        self.port.SetHint('default for provider')
        grid.Add(self.port, 0, wx.EXPAND)

        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 18)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
        run_btn = wx.Button(self, wx.ID_OK, 'Run')
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
        return provider, model, host, port


class DownloadDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title='Download model', size=(480, 200))
        self.SetBackgroundColour(BG)

        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=2, cols=2, hgap=12, vgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(_label(self, 'Provider', muted=True),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.provider = wx.Choice(self, choices=llm.PROVIDERS)
        self.provider.SetSelection(0)
        grid.Add(self.provider, 0, wx.EXPAND)

        grid.Add(_label(self, 'Model', muted=True),
                 0, wx.ALIGN_CENTER_VERTICAL)
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


class LlmFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title='llm', size=(960, 680))
        self.SetBackgroundColour(BG)
        self.SetMinSize((720, 520))

        root = wx.BoxSizer(wx.VERTICAL)

        header = wx.Panel(self)
        header.SetBackgroundColour(BG)
        h = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(header, label='llm')
        title.SetFont(_font(20, bold=True))
        title.SetForegroundColour(ACCENT)
        h.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        tag = wx.StaticText(header, label='   provider wrapper')
        tag.SetForegroundColour(TEXT_MUTED)
        h.Add(tag, 0, wx.ALIGN_CENTER_VERTICAL)
        h.AddStretchSpacer()
        header.SetSizer(h)
        root.Add(header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.banner = StatusBanner(self, on_stop=self.on_stop)
        root.Add(self.banner, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.notebook = wx.Notebook(self)
        self.notebook.SetBackgroundColour(BG)

        self.models_tab = ModelsTab(
            self.notebook,
            on_run=self.on_run,
            on_default=self.on_default,
            on_download=self.on_download,
        )
        self.notebook.AddPage(self.models_tab, 'Models')

        self.providers_tab = ProvidersTab(self.notebook)
        self.notebook.AddPage(self.providers_tab, 'Providers')

        root.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 16)

        self.SetSizer(root)
        self.CreateStatusBar()
        self.SetStatusText('Ready')

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.refresh_status(), self.timer)
        self.timer.Start(POLL_MS)

        self.refresh_status()
        self.Centre()

    def refresh_status(self):
        state = llm.read_state()
        if state and state.get('pid') and not llm.is_process_alive(state['pid']):
            llm.clear_state()
            state = None
        self.banner.update_status(state)

    def on_run(self, provider, model):
        if provider not in llm.PROVIDERS and 'mlx' in provider:
            provider = 'mlx-lm'

        dlg = RunDialog(self, provider_default=provider, model_default=model)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        try:
            p, m, host, port = dlg.get_values()
        except ValueError as e:
            dlg.Destroy()
            wx.MessageBox(f'Invalid input: {e}', 'Error', wx.OK | wx.ICON_ERROR)
            return
        dlg.Destroy()

        if not m:
            wx.MessageBox('Model name required.', 'Error', wx.OK | wx.ICON_ERROR)
            return

        current = llm.read_state()
        if current and current.get('pid') and llm.is_process_alive(current['pid']):
            wx.MessageBox(
                f'Already running: {current["provider"]} · {current["model"]}\n'
                'Stop it first.',
                'Already running',
                wx.OK | wx.ICON_WARNING,
            )
            return

        try:
            pid = run_detached(p, m, host, port)
            self.SetStatusText(f'Started {p}: {m}  (PID {pid})')
            wx.CallLater(400, self.refresh_status)
        except Exception as e:
            wx.MessageBox(f'Failed to start: {e}', 'Error', wx.OK | wx.ICON_ERROR)

    def on_stop(self):
        success, msg = stop_running()
        self.SetStatusText(msg)
        wx.CallLater(300, self.refresh_status)

    def on_default(self, provider, model):
        if provider not in llm.PROVIDERS and 'mlx' in provider:
            choices = ['mlx-lm', 'vllm-mlx']
            dlg = wx.SingleChoiceDialog(
                self,
                'Which provider should serve this model?',
                'Choose provider',
                choices,
            )
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                return
            provider = dlg.GetStringSelection()
            dlg.Destroy()

        config = llm.read_config()
        config['default_provider'] = provider
        config['default_model'] = model
        llm.write_config(config)
        self.SetStatusText(f'Default set: {provider} · {model}')
        self.models_tab.refresh()

    def on_download(self):
        dlg = DownloadDialog(self)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        provider, model = dlg.get_values()
        dlg.Destroy()
        if not model:
            wx.MessageBox('Model name required.', 'Error', wx.OK | wx.ICON_ERROR)
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
                    err_text = err.decode(errors='replace').strip() if err else ''
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
