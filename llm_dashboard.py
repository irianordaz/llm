#!/usr/bin/env python3
"""Modern light-themed wxPython dashboard for the llm wrapper."""

from __future__ import annotations

import os
import re
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


def run_detached(
    provider: str, model: str, host: str, port: int,
    ctx: int | None = None, params: dict | None = None,
) -> int:
    """Spawn the model server detached. Writes state file. Returns the PID."""
    if params is None:
        params = {}

    run_model = model
    if provider == 'ollama' and params:
        run_model = llm._get_ollama_custom_model(model, params)

    cmd, cwd = llm._build_run_cmd(
        provider, run_model, host, port, [], ctx,
        params if provider != 'ollama' else None,
    )
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
            'ctx': ctx,
            'params': params,
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
        self.params_detail = _label(self, '', muted=True, size=10)
        self.params_detail.Hide()
        info.Add(self.title, 0, wx.BOTTOM, 2)
        info.Add(self.detail, 0, wx.BOTTOM, 2)
        info.Add(self.params_detail, 0)
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
            self.params_detail.Hide()
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
        ctx = state.get('ctx')
        params = state.get('params') or {}
        base_url = llm.BASE_URL_TEMPLATES.get(
            provider, 'http://{host}:{port}'
        ).format(host=host, port=port)

        if alive:
            self.dot.SetForegroundColour(SUCCESS)
            self.title.SetLabel(f'{provider}    ·   {model}')
            if pid is not None:
                self.detail.SetLabel(
                    f'{base_url}    ·    {host}:{port}    ·    PID {pid}    ·    '
                    f'started {state.get("started_at", "?")}'
                )
            else:
                self.detail.SetLabel(
                    f'{base_url}    ·    {host}:{port}    ·    (external, auto-detected)'
                )
        else:
            self.dot.SetForegroundColour(WARN)
            self.title.SetLabel(f'{provider}    ·   {model}    (not responding)')
            self.detail.SetLabel(f'PID {pid} no longer alive — clearing state…')

        param_parts = []
        if ctx is not None:
            param_parts.append(f'ctx={ctx}')
        for k, v in params.items():
            if v is not None:
                param_parts.append(f'{k}={v}')
        if param_parts:
            self.params_detail.SetLabel('    '.join(param_parts))
            self.params_detail.Show()
        else:
            self.params_detail.Hide()

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
        self.list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self._on_right_click)
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

    def _on_right_click(self, event):
        sel = self._selected_row()
        if not sel:
            return
        menu = wx.Menu()
        run_item = menu.Append(wx.ID_ANY, 'Run model')
        opts_item = menu.Append(wx.ID_ANY, 'Model options')
        menu.AppendSeparator()
        del_item = menu.Append(wx.ID_ANY, 'Delete model')
        self.Bind(wx.EVT_MENU, lambda e, s=sel: self.on_run(*s), run_item)
        self.Bind(wx.EVT_MENU, lambda e, s=sel: self.on_configure(*s), opts_item)
        self.Bind(wx.EVT_MENU, lambda e, s=sel: self.on_delete(*s), del_item)
        self.PopupMenu(menu)
        menu.Destroy()

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
        params_default=None,
    ):
        super().__init__(parent, title='Model settings', size=(520, 480))
        self.SetBackgroundColour(BG)
        if params_default is None:
            params_default = {}

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Connection settings
        grid = wx.FlexGridSizer(rows=5, cols=2, hgap=12, vgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(_label(self, 'Provider', muted=True), 0, wx.ALIGN_CENTER_VERTICAL)
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

        sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 18)

        # Parameters separator
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 18)
        sizer.Add(_label(self, 'Model parameters  (leave blank for provider default)', muted=True, size=10),
                  0, wx.LEFT | wx.TOP, 18)

        # 3×4 grid: label | field | label | field
        params_grid = wx.FlexGridSizer(rows=3, cols=4, hgap=8, vgap=8)
        params_grid.AddGrowableCol(1, 1)
        params_grid.AddGrowableCol(3, 1)

        param_defs = [
            ('temperature', 'Temperature'),
            ('top_p', 'Top-P'),
            ('top_k', 'Top-K'),
            ('min_p', 'Min-P'),
            ('repeat_penalty', 'Repeat Penalty'),
            ('presence_penalty', 'Presence Penalty'),
        ]
        self._param_fields: dict[str, wx.TextCtrl] = {}
        for pname, plabel in param_defs:
            params_grid.Add(_label(self, plabel, muted=True, size=10), 0, wx.ALIGN_CENTER_VERTICAL)
            val = params_default.get(pname, '')
            field = wx.TextCtrl(self, value=str(val) if val != '' else '')
            field.SetHint('default')
            self._param_fields[pname] = field
            params_grid.Add(field, 0, wx.EXPAND)

        sizer.Add(params_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 18)

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
        params: dict = {}
        for pname, field in self._param_fields.items():
            val_str = field.GetValue().strip()
            if val_str:
                try:
                    params[pname] = int(float(val_str)) if pname == 'top_k' else float(val_str)
                except ValueError:
                    pass
        return provider, model, host, port, ctx, params


class DownloadDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title='Download model', size=(600, 480))
        self.SetBackgroundColour(BG)
        self._hf_results: list[dict] = []
        self._sort_col: int = 1   # default: Downloads
        self._sort_rev: bool = True

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Provider row
        prov_row = wx.BoxSizer(wx.HORIZONTAL)
        prov_row.Add(_label(self, 'Provider', muted=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.provider = wx.Choice(self, choices=llm.PROVIDERS)
        self.provider.SetSelection(0)
        prov_row.Add(self.provider, 0)
        sizer.Add(prov_row, 0, wx.ALL, 16)

        # Simplebook: page 0 = ollama, page 1 = HuggingFace
        self.book = wx.Simplebook(self)
        self.book.SetBackgroundColour(BG)

        # --- Ollama panel ---
        ollama_panel = wx.Panel(self.book)
        ollama_panel.SetBackgroundColour(BG)
        op = wx.BoxSizer(wx.HORIZONTAL)
        op.Add(_label(ollama_panel, 'Model name', muted=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.ollama_model = wx.TextCtrl(ollama_panel, size=(320, -1))
        self.ollama_model.SetHint('e.g. llama3.2')
        op.Add(self.ollama_model, 1)
        ollama_panel.SetSizer(op)
        self.book.AddPage(ollama_panel, 'ollama')

        # --- HuggingFace panel ---
        hf_panel = wx.Panel(self.book)
        hf_panel.SetBackgroundColour(BG)
        hf = wx.BoxSizer(wx.VERTICAL)

        search_row = wx.BoxSizer(wx.HORIZONTAL)
        search_row.Add(_label(hf_panel, 'Search', muted=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.hf_query = wx.TextCtrl(hf_panel, size=(200, -1), style=wx.TE_PROCESS_ENTER)
        self.hf_query.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        search_row.Add(self.hf_query, 1, wx.RIGHT, 10)
        search_row.Add(_label(hf_panel, 'Filter', muted=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.hf_filter = wx.TextCtrl(hf_panel, size=(100, -1))
        self.hf_filter.SetHint('e.g. mlx')
        search_row.Add(self.hf_filter, 0, wx.RIGHT, 8)
        search_btn = wx.Button(hf_panel, label='Search')
        search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        search_row.Add(search_btn, 0)
        hf.Add(search_row, 0, wx.BOTTOM, 8)

        self.hf_list = wx.ListCtrl(
            hf_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE
        )
        self.hf_list.AppendColumn('Model', width=270)
        self.hf_list.AppendColumn('Downloads', width=90)
        self.hf_list.AppendColumn('Likes', width=60)
        self.hf_list.AppendColumn('Size', width=80)
        self.hf_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_hf_select)
        self.hf_list.Bind(wx.EVT_LIST_COL_CLICK, self._on_col_click)
        hf.Add(self.hf_list, 1, wx.EXPAND | wx.BOTTOM, 8)

        model_row = wx.BoxSizer(wx.HORIZONTAL)
        model_row.Add(_label(hf_panel, 'Model ID', muted=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.hf_model = wx.TextCtrl(hf_panel, size=(280, -1))
        self.hf_model.SetHint('org/model-name or select above')
        model_row.Add(self.hf_model, 1, wx.RIGHT, 6)
        copy_btn = wx.Button(hf_panel, label='Copy', size=(60, -1))
        copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
        model_row.Add(copy_btn, 0)
        hf.Add(model_row, 0)

        hf_panel.SetSizer(hf)
        self.book.AddPage(hf_panel, 'hf')

        sizer.Add(self.book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
        ok = wx.Button(self, wx.ID_OK, 'Download')
        ok.SetDefault()
        btn_row.Add(cancel, 0, wx.RIGHT, 6)
        btn_row.Add(ok, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 16)

        self.SetSizer(sizer)
        self.provider.Bind(wx.EVT_CHOICE, self._on_provider_change)
        self._on_provider_change(None)

    def _on_provider_change(self, event):
        sel = self.provider.GetSelection()
        self.book.SetSelection(0 if sel == 0 else 1)
        self.Layout()

    def _on_search(self, event):
        query = self.hf_query.GetValue().strip()
        if not query:
            return
        filter_tags = self.hf_filter.GetValue().strip()
        self.hf_list.DeleteAllItems()
        self.hf_list.InsertItem(0, 'Searching…')

        def worker():
            results = llm.search_huggingface_models(query, filter_tags, limit=40)
            wx.CallAfter(self._update_results, results)

        threading.Thread(target=worker, daemon=True).start()

    def _update_results(self, results):
        self._hf_results = results
        self._populate_list()

    def _populate_list(self):
        sort_keys = {0: 'id', 1: 'downloads', 2: 'likes', 3: 'size_bytes'}
        key = sort_keys.get(self._sort_col, 'downloads')
        self._hf_results.sort(
            key=lambda r: r.get(key, 0) if not isinstance(r.get(key, 0), str) else r.get(key, '').lower(),
            reverse=self._sort_rev,
        )
        self.hf_list.DeleteAllItems()
        for r in self._hf_results:
            size_str = llm._format_bytes(r.get('size_bytes', 0))
            idx = self.hf_list.InsertItem(self.hf_list.GetItemCount(), r['id'])
            self.hf_list.SetItem(idx, 1, f'{r["downloads"]:,}')
            self.hf_list.SetItem(idx, 2, str(r['likes']))
            self.hf_list.SetItem(idx, 3, size_str)

    def _on_col_click(self, event):
        col = event.GetColumn()
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = col != 0  # numeric cols default to descending
        self._populate_list()

    def _on_hf_select(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self._hf_results):
            self.hf_model.SetValue(self._hf_results[idx]['id'])

    def _on_copy(self, event):
        val = self.hf_model.GetValue().strip()
        if val and wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(val))
            wx.TheClipboard.Close()

    def get_values(self):
        provider = llm.PROVIDERS[self.provider.GetSelection()]
        if provider == 'ollama':
            model = self.ollama_model.GetValue().strip()
        else:
            model = self.hf_model.GetValue().strip()
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
        state = llm.read_state()
        if state and not state.get('external') and state.get('pid'):
            if llm.is_process_alive(state['pid']):
                provider = state.get('provider', 'model')
                model = state.get('model', '')
                dlg = wx.MessageDialog(
                    self,
                    f'{provider}  ·  {model} is still running.\n\nStop it before closing?',
                    'Model running',
                    wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION,
                )
                dlg.SetYesNoLabels('Stop & close', 'Keep running')
                resp = dlg.ShowModal()
                dlg.Destroy()
                if resp == wx.ID_CANCEL:
                    self.timer.Start(POLL_MS)
                    event.Veto()
                    return
                if resp == wx.ID_YES:
                    stop_running()
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

    def _launch(self, provider, model, host, port, ctx=None, params=None):
        try:
            pid = run_detached(provider, model, host, port, ctx, params)
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
        params = saved.get('params', {})
        self._launch(provider, model, host, port, ctx, params)

    def on_configure(self, provider, model):
        """Open settings dialog; save settings and launch on confirm."""
        if provider not in llm.PROVIDERS and 'mlx' in provider:
            provider = 'mlx-lm'
        saved = llm.get_model_settings(provider, model)
        host_saved = saved.get('host', llm.DEFAULT_HOST)
        port_saved = saved.get('port', llm.DEFAULT_PORTS[provider])
        ctx_saved = saved.get('ctx', llm.DEFAULT_CTX)
        params_saved = saved.get('params', {})
        if not params_saved and provider == 'ollama':
            params_saved = llm.get_ollama_model_params(model)

        dlg = RunDialog(
            self,
            provider_default=provider,
            model_default=model,
            host_default=host_saved,
            port_default=port_saved,
            ctx_default=ctx_saved,
            params_default=params_saved,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        try:
            p, m, host, port, ctx, params = dlg.get_values()
        except ValueError as e:
            dlg.Destroy()
            wx.MessageBox(f'Invalid input: {e}', 'Error', wx.OK | wx.ICON_ERROR)
            return
        dlg.Destroy()

        if not m:
            wx.MessageBox('Model name required.', 'Error', wx.OK | wx.ICON_ERROR)
            return

        llm.save_model_settings(p, m, host, port, ctx, params)

        if self._check_already_running():
            return
        self._launch(p, m, host, port, ctx, params)

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
        _ANSI = re.compile(r'\x1b\[[0-9;]*[mGKABCDJHfsu]?')
        _PCT = re.compile(r'(\d+)%\|')
        _SIZES = re.compile(r'([\d.]+\s*[KMGT]?i?B)\s*/\s*([\d.]+\s*[KMGT]?i?B)')
        _OLLAMA_PCT = re.compile(r'(\d+)%')

        state = {
            'pct': 0,
            'label': 'Connecting…',
            'aborted': False,
            'success': False,
            'error': None,
        }

        # No PD_APP_MODAL: main window stays interactive while downloading.
        progress = wx.ProgressDialog(
            f'Downloading {model}',
            'Connecting…',
            maximum=100,
            parent=self,
            style=wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME,
        )

        def _process_line(line: str, is_ollama: bool) -> None:
            clean = _ANSI.sub('', line).strip()
            if not clean:
                return
            if is_ollama:
                pm = _OLLAMA_PCT.search(clean)
                if pm:
                    pct = int(pm.group(1))
                    state['pct'] = pct
                    state['label'] = f'{pct}%'
            else:
                pm = _PCT.search(clean)
                if pm:
                    pct = int(pm.group(1))
                    sm = _SIZES.search(clean)
                    if sm:
                        state['label'] = f'{pct}%  ·  {sm.group(1)} / {sm.group(2)}'
                    else:
                        state['label'] = f'{pct}%'
                    state['pct'] = max(state['pct'], pct)

        def _read_stream(stream, is_ollama: bool) -> None:
            buf = b''
            while True:
                chunk = stream.read(256)
                if not chunk:
                    break
                buf += chunk
                while True:
                    r_pos = buf.find(b'\r')
                    n_pos = buf.find(b'\n')
                    if r_pos < 0 and n_pos < 0:
                        break
                    pos = min(r_pos if r_pos >= 0 else n_pos,
                              n_pos if n_pos >= 0 else r_pos)
                    _process_line(buf[:pos].decode('utf-8', errors='replace'), is_ollama)
                    buf = buf[pos + 1:]

        def worker():
            is_ollama = provider == 'ollama'
            if is_ollama:
                cmd = ['ollama', 'pull', model]
                popen_kwargs: dict = {'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL}
                stream_key = 'stdout'
            else:
                cmd = llm._hf_download_cmd(model)
                popen_kwargs = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.PIPE}
                stream_key = 'stderr'
            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
                reader = threading.Thread(
                    target=_read_stream,
                    args=(getattr(proc, stream_key), is_ollama),
                    daemon=True,
                )
                reader.start()
                while proc.poll() is None:
                    if state['aborted']:
                        proc.terminate()
                        proc.wait()
                        return
                    time.sleep(0.1)
                reader.join(timeout=2)
                if proc.returncode == 0:
                    state['success'] = True
                    state['pct'] = 100
                    state['label'] = f'Downloaded {model}'
                else:
                    state['error'] = f'Download failed (exit {proc.returncode})'
            except FileNotFoundError:
                state['error'] = f'Command not found: {cmd[0]}'

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        # Timer-based polling: lets the wx event loop run normally so the
        # progress dialog repaints on every tick and the main window stays live.
        _dl_timer = wx.Timer(self)
        _closed = [False]

        def _tick(event):
            if _closed[0]:
                return
            if not thread.is_alive():
                _closed[0] = True
                _dl_timer.Stop()
                self.Unbind(wx.EVT_TIMER, source=_dl_timer)
                try:
                    progress.Destroy()
                except Exception:
                    pass
                if state['aborted']:
                    self.SetStatusText('Download cancelled.')
                elif state['success']:
                    self.SetStatusText(state['label'])
                    self.models_tab.refresh()
                elif state['error']:
                    wx.MessageBox(state['error'], 'Download failed', wx.OK | wx.ICON_ERROR)
                return
            try:
                keep_going, _ = progress.Update(state['pct'], state['label'])
                if not keep_going:
                    state['aborted'] = True
            except Exception:
                pass

        self.Bind(wx.EVT_TIMER, _tick, _dl_timer)
        _dl_timer.Start(150)


def main():
    app = wx.App(False)
    frame = LlmFrame()
    frame.Show()
    app.MainLoop()


if __name__ == '__main__':
    main()
