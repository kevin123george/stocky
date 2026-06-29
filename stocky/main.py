#!/usr/bin/env python3
"""stocky — professional stock TUI with AI analysis"""

import sys, threading, io, json, os, time
from datetime import datetime
from pathlib import Path
from rich.ansi import AnsiDecoder

from textual.app import App, ComposeResult
from textual.widgets import (
    Static, ListView, ListItem, Label, Footer, Header,
    Input, DataTable, TabbedContent, TabPane, Button,
)
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual.screen import ModalScreen
import yfinance as yf
import plotext as plt
import pandas as pd

_ansi = AnsiDecoder()

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR   = Path.home() / ".stocky"
DATA_DIR.mkdir(exist_ok=True)
WL_FILE    = DATA_DIR / "watchlists.json"
PORT_FILE  = DATA_DIR / "portfolio.json"
ALERT_FILE = DATA_DIR / "alerts.json"

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_WL = {
    "Main": ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL"],
    "Tech": ["META", "AMZN", "NFLX", "AMD",  "INTC"],
    "ETFs": ["SPY",  "QQQ",  "VTI",  "IWM"],
}

TIMEFRAMES = {
    "1D": ("1d",  "5m"),
    "1W": ("5d",  "30m"),
    "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"),
    "1Y": ("1y",  "1wk"),
    "5Y": ("5y",  "1mo"),
}

CURRENCIES = {
    "USD": ("$",  None),
    "EUR": ("€",  "USDEUR=X"),
    "GBP": ("£",  "USDGBP=X"),
    "JPY": ("¥",  "USDJPY=X"),
    "CAD": ("C$", "USDCAD=X"),
}
CCY_LIST = list(CURRENCIES)

INDICATORS = ["None", "SMA20", "EMA20", "BB", "RSI", "MACD"]

SP500 = [
    "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","UNH","LLY","JPM",
    "V","XOM","AVGO","PG","MA","HD","CVX","MRK","ABBV","COST",
    "PEP","KO","WMT","BAC","ACN","MCD","CRM","NFLX","TMO","AMD",
    "INTC","DIS","CSCO","ADBE","PFE","VZ","CMCSA","NEE","RTX","BMY",
]

# ── Technical indicators ───────────────────────────────────────────────────────
def _sma(s, n):  return pd.Series(s).rolling(n).mean().tolist()
def _ema(s, n):  return pd.Series(s).ewm(span=n, adjust=False).mean().tolist()

def _bb(prices, n=20):
    s = pd.Series(prices); m = s.rolling(n).mean(); d = s.rolling(n).std()
    return (m-2*d).tolist(), m.tolist(), (m+2*d).tolist()

def _rsi(prices, n=14):
    s = pd.Series(prices); dif = s.diff()
    g = dif.clip(lower=0).rolling(n).mean()
    l = (-dif.clip(upper=0)).rolling(n).mean()
    return (100 - 100/(1 + g/l)).tolist()

def _macd(prices):
    s = pd.Series(prices)
    e12 = s.ewm(span=12, adjust=False).mean()
    e26 = s.ewm(span=26, adjust=False).mean()
    m = e12 - e26; sig = m.ewm(span=9, adjust=False).mean()
    return m.tolist(), sig.tolist(), (m-sig).tolist()

def _signals(closes, volumes=None):
    """Compute technical signals. Returns list of (name, value, verdict, score)."""
    if len(closes) < 30:
        return []
    sigs = []
    score = 0

    # RSI
    rsi_vals = _rsi(closes)
    rsi = next((v for v in reversed(rsi_vals) if v == v), None)  # last non-nan
    if rsi is not None:
        if rsi < 30:
            v, s = "Oversold — potential BUY", +1
        elif rsi > 70:
            v, s = "Overbought — potential SELL", -1
        else:
            v, s = "Neutral", 0
        score += s
        sigs.append(("RSI(14)", f"{rsi:.1f}", v, s))

    # MACD histogram
    m, sig, hist = _macd(closes)
    last_hist = next((v for v in reversed(hist) if v == v), None)
    prev_hist = None
    for v in reversed(hist[:-1]):
        if v == v: prev_hist = v; break
    if last_hist is not None and prev_hist is not None:
        if last_hist > 0 and prev_hist <= 0:
            v, s = "Bullish crossover", +2
        elif last_hist < 0 and prev_hist >= 0:
            v, s = "Bearish crossover", -2
        elif last_hist > 0:
            v, s = "Bullish momentum", +1
        elif last_hist < 0:
            v, s = "Bearish momentum", -1
        else:
            v, s = "Neutral", 0
        score += s
        sigs.append(("MACD", f"{last_hist:.3f}", v, s))

    # SMA20 vs SMA50
    sma20 = _sma(closes, 20); sma50 = _sma(closes, 50)
    s20 = next((v for v in reversed(sma20) if v == v), None)
    s50 = next((v for v in reversed(sma50) if v == v), None)
    price = closes[-1]
    if s20 and s50:
        if price > s20 > s50:
            v, s = "Price > SMA20 > SMA50 — Bullish", +1
        elif price < s20 < s50:
            v, s = "Price < SMA20 < SMA50 — Bearish", -1
        elif price > s20:
            v, s = "Price above SMA20", +1
        else:
            v, s = "Price below SMA20", -1
        score += s
        sigs.append(("SMA 20/50", f"{s20:.2f} / {s50:.2f}", v, s))

    # 52-week position
    hi = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    lo = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    pos = (price - lo) / (hi - lo) * 100 if hi != lo else 50
    if pos > 80:
        v, s = "Near 52-week high", -1
    elif pos < 20:
        v, s = "Near 52-week low — potential value", +1
    else:
        v, s = f"{pos:.0f}% of 52-week range", 0
    score += s
    sigs.append(("52W Position", f"{pos:.0f}%", v, s))

    # Overall verdict
    if   score >= 4:  overall = ("STRONG BUY",  "green")
    elif score >= 2:  overall = ("BUY",          "green")
    elif score >= -1: overall = ("HOLD",         "yellow")
    elif score >= -3: overall = ("SELL",         "red")
    else:             overall = ("STRONG SELL",  "red")

    return sigs, score, overall

# ── Helpers ────────────────────────────────────────────────────────────────────
def _load(p, default):
    try:    return json.loads(Path(p).read_text())
    except: return default

def _save(p, data): Path(p).write_text(json.dumps(data, indent=2))

def _notify(title, msg):
    os.system(f"osascript -e 'display notification \"{msg}\" with title \"{title}\"'")

def _fmt_large(v):
    if v is None: return "N/A"
    v = float(v)
    if v >= 1e12: return f"{v/1e12:.2f}T"
    if v >= 1e9:  return f"{v/1e9:.2f}B"
    if v >= 1e6:  return f"{v/1e6:.2f}M"
    if v >= 1e3:  return f"{v/1e3:.0f}K"
    return str(int(v))

def _pct(v):
    if v is None: return "N/A"
    return f"{float(v)*100:.1f}%"

def _plt_build():
    try:
        return plt.build()
    except AttributeError:
        old = sys.stdout; sys.stdout = buf = io.StringIO()
        plt.show(); sys.stdout = old
        return buf.getvalue()

def _stream_claude(system, messages, on_text, on_done, on_error):
    """Stream a Claude response via the `claude` CLI (uses existing login).
    Calls on_text(chunk) for each text delta, on_done() when finished,
    on_error(msg) on failure.
    """
    import subprocess, json as _json, shutil

    if not shutil.which("claude"):
        on_error("claude CLI not found — install Claude Code first")
        return

    # Build prompt: system context + full message history
    parts = []
    if system:
        parts.append(f"<system_instructions>\n{system}\n</system_instructions>\n\n")
    for msg in messages:
        tag = "Human" if msg["role"] == "user" else "Assistant"
        parts.append(f"{tag}: {msg['content']}\n\n")
    full_prompt = "".join(parts).rstrip()

    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        full_prompt,
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.strip()
            if not line: continue
            try:
                obj = _json.loads(line)
                # Stream text deltas
                if obj.get("type") == "stream_event":
                    ev = obj.get("event", {})
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            on_text(delta.get("text", ""))
                # Final result (non-streaming fallback)
                elif obj.get("type") == "result" and obj.get("subtype") == "success":
                    if not obj.get("result", ""):
                        pass  # already streamed
            except (_json.JSONDecodeError, KeyError):
                pass
        proc.wait()
        if proc.returncode not in (0, None):
            err = proc.stderr.read().strip()
            if err: on_error(err); return
        on_done()
    except Exception as exc:
        on_error(str(exc))

def _build_stock_context(sym, info, hist, signals_data, financials):
    """Build a rich context string for Claude from stock data."""
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    prev  = info.get("previousClose") or price
    chg   = ((price-prev)/prev*100) if prev else 0

    lines = [
        f"Stock: {sym} — {info.get('longName','N/A')}",
        f"Exchange: {info.get('exchange','N/A')} | Sector: {info.get('sector','N/A')} | Industry: {info.get('industry','N/A')}",
        f"",
        f"=== Price ===",
        f"Current: ${price:.2f} ({chg:+.2f}%)",
        f"Previous Close: ${prev:.2f}",
        f"Day Range: ${info.get('dayLow','N/A')} – ${info.get('dayHigh','N/A')}",
        f"52W Range: ${info.get('fiftyTwoWeekLow','N/A')} – ${info.get('fiftyTwoWeekHigh','N/A')}",
        f"Volume: {_fmt_large(info.get('volume'))} (avg {_fmt_large(info.get('averageVolume'))})",
        f"",
        f"=== Valuation ===",
        f"Market Cap: {_fmt_large(info.get('marketCap'))}",
        f"P/E (TTM): {info.get('trailingPE','N/A')}",
        f"Forward P/E: {info.get('forwardPE','N/A')}",
        f"P/B: {info.get('priceToBook','N/A')}",
        f"P/S (TTM): {info.get('priceToSalesTrailing12Months','N/A')}",
        f"EV/EBITDA: {info.get('enterpriseToEbitda','N/A')}",
        f"EPS (TTM): ${info.get('trailingEps','N/A')}",
        f"EPS Forward: ${info.get('forwardEps','N/A')}",
        f"",
        f"=== Financials ===",
    ]
    for k, v in financials.items():
        lines.append(f"{k}: {v}")

    lines += [
        f"",
        f"=== Dividends & Growth ===",
        f"Dividend Yield: {_pct(info.get('dividendYield'))}",
        f"Payout Ratio: {_pct(info.get('payoutRatio'))}",
        f"Revenue Growth (YoY): {_pct(info.get('revenueGrowth'))}",
        f"Earnings Growth (YoY): {_pct(info.get('earningsGrowth'))}",
        f"",
        f"=== Analyst Consensus ===",
        f"Rating: {(info.get('recommendationKey') or 'N/A').upper()} ({info.get('numberOfAnalystOpinions','N/A')} analysts)",
        f"Price Target: ${info.get('targetMeanPrice','N/A')} (lo ${info.get('targetLowPrice','N/A')} / hi ${info.get('targetHighPrice','N/A')})",
        f"",
    ]

    if signals_data:
        sigs, score, (verdict, _) = signals_data
        lines.append(f"=== Technical Signals (score {score:+d}) ===")
        lines.append(f"Overall: {verdict}")
        for name, val, desc, s in sigs:
            lines.append(f"  {name}: {val} — {desc}")

    if hist is not None and not hist.empty:
        closes = hist["Close"].tolist()
        lines += [
            f"",
            f"=== Price History ({len(closes)} bars) ===",
            f"Period open: ${closes[0]:.2f}  |  Period close: ${closes[-1]:.2f}",
            f"Period return: {((closes[-1]-closes[0])/closes[0]*100):+.1f}%",
        ]

    return "\n".join(lines)


# ── Modals ─────────────────────────────────────────────────────────────────────
class InputModal(ModalScreen):
    CSS = """
    InputModal { align: center middle; }
    #box { width: 50; padding: 1 2; border: solid #89b4fa; background: #313244; }
    Label { color: #cdd6f4; margin-bottom: 1; }
    Input { background: #45475a; color: #cdd6f4; border: solid #585b70; }
    """
    def __init__(self, prompt):
        super().__init__(); self._prompt = prompt
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._prompt)
            yield Input(id="inp")
    def on_mount(self): self.query_one("#inp", Input).focus()
    def on_input_submitted(self, e): self.dismiss(e.value.strip() or None)
    def on_key(self, e):
        if e.key == "escape": self.dismiss(None)


class AlertModal(ModalScreen):
    CSS = """
    AlertModal { align: center middle; }
    #box { width: 54; padding: 1 2; border: solid #89b4fa; background: #313244; }
    Label { color: #cdd6f4; margin-bottom: 1; }
    Input { background: #45475a; color: #cdd6f4; border: solid #585b70; margin-bottom: 1; }
    Button { margin-right: 1; }
    """
    def __init__(self, sym):
        super().__init__(); self._sym = sym
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"Price alert — {self._sym}")
            yield Input(placeholder="Price  e.g. 200.00", id="price")
            yield Input(placeholder="Direction:  above  or  below", id="dir")
            with Horizontal():
                yield Button("Add", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")
    def on_mount(self): self.query_one("#price").focus()
    def on_button_pressed(self, e):
        if e.button.id == "ok":
            try:
                p = float(self.query_one("#price").value)
                d = self.query_one("#dir").value.strip().lower()
                if d not in ("above","below"): d = "above"
                self.dismiss({"symbol": self._sym, "price": p,
                               "direction": d, "triggered": False})
            except ValueError: self.dismiss(None)
        else: self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape": self.dismiss(None)


class PositionModal(ModalScreen):
    CSS = """
    PositionModal { align: center middle; }
    #box { width: 54; padding: 1 2; border: solid #89b4fa; background: #313244; }
    Label { color: #cdd6f4; margin-bottom: 1; }
    Input { background: #45475a; color: #cdd6f4; border: solid #585b70; margin-bottom: 1; }
    Button { margin-right: 1; }
    """
    def __init__(self, sym):
        super().__init__(); self._sym = sym
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"Position — {self._sym}")
            yield Input(placeholder="Shares  e.g. 10", id="shares")
            yield Input(placeholder="Avg cost per share  e.g. 150.00", id="cost")
            with Horizontal():
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")
    def on_mount(self): self.query_one("#shares").focus()
    def on_button_pressed(self, e):
        if e.button.id == "ok":
            try:
                self.dismiss({"symbol": self._sym,
                               "shares": float(self.query_one("#shares").value),
                               "cost":   float(self.query_one("#cost").value)})
            except ValueError: self.dismiss(None)
        else: self.dismiss(None)
    def on_key(self, e):
        if e.key == "escape": self.dismiss(None)


class ScreenerScreen(ModalScreen):
    CSS = """
    ScreenerScreen { background: #1e1e2e; }
    #sc-hdr { height: 1; background: #313244; color: #89b4fa; padding: 0 2; }
    DataTable { height: 1fr; }
    #sc-sta { height: 1; color: #585b70; padding: 0 2; }
    """
    def compose(self) -> ComposeResult:
        yield Static("  S&P 500 Screener   Enter=select   Esc=close", id="sc-hdr")
        yield DataTable(id="sc-tbl", zebra_stripes=True, cursor_type="row")
        yield Static("Loading…", id="sc-sta")

    def on_mount(self):
        t = self.query_one("#sc-tbl", DataTable)
        t.add_columns("Symbol","Price","Chg%","Volume","Mkt Cap","P/E","Sector")
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        for sym in SP500:
            try:
                fi   = yf.Ticker(sym).fast_info
                p    = fi.last_price or 0
                pc   = fi.previous_close or p
                chg  = ((p-pc)/pc*100) if pc else 0
                info = yf.Ticker(sym).info
                vol  = _fmt_large(fi.three_month_average_volume)
                cap  = _fmt_large(fi.market_cap)
                pe   = f"{info.get('trailingPE',0):.1f}" if info.get('trailingPE') else "N/A"
                sec  = (info.get("sector") or "")[:14]
                clr  = "green" if chg >= 0 else "red"
                self.call_from_thread(
                    self.query_one("#sc-tbl", DataTable).add_row,
                    sym, f"${p:.2f}", f"[{clr}]{chg:+.2f}%[/{clr}]",
                    vol, cap, pe, sec)
                self.call_from_thread(self.query_one("#sc-sta").update, f"Loaded {sym}")
            except Exception: pass

    def on_data_table_row_selected(self, e: DataTable.RowSelected):
        row = self.query_one("#sc-tbl", DataTable).get_row(e.row_key)
        self.dismiss(str(row[0]))

    def on_key(self, e):
        if e.key == "escape": self.dismiss(None)


# ── Main App ───────────────────────────────────────────────────────────────────
class StockApp(App):
    CSS = """
    Screen  { background: #1e1e2e; }
    Header  { background: #181825; color: #cdd6f4; }
    Footer  { background: #181825; color: #585b70; }
    #main   { height: 1fr; }
    #left   { width: 28; border-right: solid #313244; }
    #wl-hdr { height: 1; background: #313244; color: #89b4fa; padding: 0 1; }
    ListView { background: #1e1e2e; border: none; }
    ListItem { background: #1e1e2e; color: #cdd6f4; padding: 0 1; height: 1; }
    ListItem:hover { background: #313244; }
    ListItem.--highlight { background: #45475a; color: #89b4fa; }
    #right      { width: 1fr; }
    #sym-line   { height: 1; margin-top: 1; padding: 0 2; color: #89b4fa; }
    #price-line { height: 2; padding: 0 2; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    #ch-ctrl  { height: 1; background: #181825; padding: 0 1; }
    #ch-area  { height: 1fr; }
    #sig-area { height: 7; background: #181825; padding: 0 1; }
    DataTable { height: 1fr; }
    #ov-scroll  { height: 1fr; }
    #ai-scroll  { height: 1fr; }
    #ai-input   { height: 3; background: #313244; border: solid #585b70; }
    #ai-status  { height: 1; color: #585b70; padding: 0 1; }
    #port-hdr  { height: 1; color: #a6e3a1; padding: 0 1; }
    #alert-hdr { height: 1; color: #6c7086; padding: 0 1; }
    #status { height: 1; padding: 0 2; color: #585b70; }
    """

    BINDINGS = [
        Binding("q", "quit",          "Quit"),
        Binding("a", "add_ticker",    "Add"),
        Binding("d", "del_ticker",    "Del"),
        Binding("r", "refresh",       "Refresh"),
        Binding("c", "cycle_ccy",     "Currency"),
        Binding("t", "toggle_chart",  "Candle/Line"),
        Binding("i", "cycle_ind",     "Indicator"),
        Binding("n", "add_alert",     "Alert"),
        Binding("p", "add_position",  "Position"),
        Binding("s", "open_screener", "Screener"),
        Binding("[", "prev_wl",       "Prev list", show=False),
        Binding("]", "next_wl",       "Next list", show=False),
        Binding("1", "tf_1d",  "1D"),
        Binding("2", "tf_1w",  "1W"),
        Binding("3", "tf_1m",  "1M"),
        Binding("4", "tf_3m",  "3M"),
        Binding("5", "tf_1y",  "1Y"),
        Binding("6", "tf_5y",  "5Y"),
        Binding("j", "cursor_down", "↓", show=False),
        Binding("k", "cursor_up",   "↑", show=False),
    ]

    REFRESH_INTERVAL = 3

    def __init__(self):
        super().__init__()
        self._wls        = _load(WL_FILE,    DEFAULT_WL)
        self._portfolio  = _load(PORT_FILE,  [])
        self._alerts     = _load(ALERT_FILE, [])
        self._wl_names   = list(self._wls.keys())
        self._wl_idx     = 0
        self._cur_sym    = self._wls[self._wl_names[0]][0]
        self._cur_tf     = "1M"
        self._cur_ccy    = "USD"
        self._chart_t    = "line"
        self._ind_idx    = 0
        self._indicator  = "None"
        self._info_cache  = {}
        self._price_cache = {}
        self._hist_cache  = {}
        self._fin_cache   = {}
        self._forex_cache = {"USD": 1.0}
        self._ai_history  = {}   # sym → list of {"role","content"}
        self._lock        = threading.Lock()

    @property
    def _cur_wl(self):      return self._wls[self._wl_names[self._wl_idx]]
    @property
    def _cur_wl_name(self): return self._wl_names[self._wl_idx]

    # ── Compose ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("", id="wl-hdr")
                yield ListView(id="wl")
            with Vertical(id="right"):
                yield Static("", id="sym-line")
                yield Static("", id="price-line")
                with TabbedContent(id="tabs"):
                    # Tab 1: Google Finance-style overview + financials
                    with TabPane("Overview", id="tab-overview"):
                        with ScrollableContainer(id="ov-scroll"):
                            yield Static("", id="ov-content")
                    # Tab 2: Chart + technical signals
                    with TabPane("Chart", id="tab-chart"):
                        yield Static("", id="ch-ctrl")
                        yield Static("", id="ch-area")
                        yield Static("", id="sig-area")
                    # Tab 3: Options chain
                    with TabPane("Options", id="tab-options"):
                        yield Static("", id="opt-ctrl")
                        yield DataTable(id="opt-tbl", zebra_stripes=True)
                    # Tab 4: AI analysis + Q&A
                    with TabPane("AI", id="tab-ai"):
                        with ScrollableContainer(id="ai-scroll"):
                            yield Static("", id="ai-output")
                        yield Input(placeholder="Ask Claude anything about this stock…", id="ai-input")
                        yield Static("", id="ai-status")
                    # Tab 5: Portfolio + Alerts
                    with TabPane("Portfolio/Alerts", id="tab-portaler"):
                        yield Static("", id="port-hdr")
                        yield DataTable(id="port-tbl", zebra_stripes=True)
                        yield Static("", id="alert-hdr")
                        yield DataTable(id="alert-tbl", zebra_stripes=True)
                yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        self._init_tables()
        self._rebuild_wl()
        self._status("Fetching data…")
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self._alert_loop, daemon=True).start()
        self.set_interval(self.REFRESH_INTERVAL, self._auto_refresh)

    def _init_tables(self):
        self.query_one("#opt-tbl",  DataTable).add_columns(
            "Strike","Last","Bid","Ask","IV","OI","Volume","ITM")
        self.query_one("#port-tbl", DataTable).add_columns(
            "Symbol","Shares","Avg Cost","Current","P&L","P&L%","Value")
        self.query_one("#alert-tbl",DataTable).add_columns(
            "Symbol","Condition","Status")

    # ── Boot ───────────────────────────────────────────────────────────────────
    def _boot(self):
        self._load_forex()
        for sym in self._cur_wl:
            self._load_price(sym)
        self._load_details(self._cur_sym)
        self._load_hist(self._cur_sym, self._cur_tf)
        self._load_financials(self._cur_sym)
        self.call_from_thread(self._draw_portfolio)
        self.call_from_thread(self._draw_alerts)

    def _load_forex(self):
        for ccy, (_, pair) in CURRENCIES.items():
            if pair is None: self._forex_cache[ccy] = 1.0; continue
            try:
                r = yf.Ticker(pair).fast_info.last_price
                if r: self._forex_cache[ccy] = float(r)
            except Exception: pass

    def _load_price(self, sym):
        try:
            fi = yf.Ticker(sym).fast_info
            p  = fi.last_price; pc = fi.previous_close or p
            if p:
                with self._lock:
                    self._price_cache[sym] = (float(p), float(pc or p))
                self.call_from_thread(self._draw_wl_item, sym)
        except Exception: pass

    def _load_details(self, sym):
        try:
            t    = yf.Ticker(sym)
            info = t.info
            try:   cal  = t.calendar
            except: cal = {}
            try:   pre  = t.fast_info.pre_market_price
            except: pre = None
            try:   post = t.fast_info.post_market_price
            except: post = None
            with self._lock:
                self._info_cache[sym] = {"info": info, "cal": cal,
                                          "pre": pre, "post": post}
            self.call_from_thread(self._draw_details, sym)
        except Exception as exc:
            self.call_from_thread(self._status, f"Error: {exc}")

    def _load_financials(self, sym):
        try:
            t = yf.Ticker(sym)
            fin = {}
            try:
                is_ = t.income_stmt
                if is_ is not None and not is_.empty:
                    rev = is_.loc["Total Revenue"].iloc[0] if "Total Revenue" in is_.index else None
                    ni  = is_.loc["Net Income"].iloc[0]    if "Net Income"    in is_.index else None
                    gp  = is_.loc["Gross Profit"].iloc[0]  if "Gross Profit"  in is_.index else None
                    oi  = is_.loc["Operating Income"].iloc[0] if "Operating Income" in is_.index else None
                    fin["Revenue (TTM)"]         = _fmt_large(rev)
                    fin["Net Income (TTM)"]       = _fmt_large(ni)
                    fin["Gross Profit"]           = _fmt_large(gp)
                    fin["Operating Income"]       = _fmt_large(oi)
                    if rev and gp:   fin["Gross Margin"]     = f"{float(gp)/float(rev)*100:.1f}%"
                    if rev and oi:   fin["Operating Margin"] = f"{float(oi)/float(rev)*100:.1f}%"
                    if rev and ni:   fin["Net Margin"]       = f"{float(ni)/float(rev)*100:.1f}%"
            except Exception: pass
            try:
                bs = t.balance_sheet
                if bs is not None and not bs.empty:
                    ta  = bs.loc["Total Assets"].iloc[0]       if "Total Assets"       in bs.index else None
                    tl  = bs.loc["Total Liabilities Net Minority Interest"].iloc[0] \
                          if "Total Liabilities Net Minority Interest" in bs.index else None
                    seq = bs.loc["Stockholders Equity"].iloc[0] if "Stockholders Equity" in bs.index else None
                    fin["Total Assets"]       = _fmt_large(ta)
                    fin["Total Liabilities"]  = _fmt_large(tl)
                    if ta and tl: fin["Debt/Equity"] = f"{float(tl)/float(seq):.2f}" if seq and float(seq) != 0 else "N/A"
            except Exception: pass
            try:
                cf = t.cashflow
                if cf is not None and not cf.empty:
                    fcf = cf.loc["Free Cash Flow"].iloc[0] if "Free Cash Flow" in cf.index else None
                    ocf = cf.loc["Operating Cash Flow"].iloc[0] if "Operating Cash Flow" in cf.index else None
                    fin["Free Cash Flow"]      = _fmt_large(fcf)
                    fin["Operating Cash Flow"] = _fmt_large(ocf)
            except Exception: pass
            with self._lock:
                self._fin_cache[sym] = fin
            self.call_from_thread(self._draw_overview, sym)
        except Exception: pass

    def _load_hist(self, sym, tf):
        period, interval = TIMEFRAMES[tf]
        try:
            hist = yf.Ticker(sym).history(period=period, interval=interval)
            with self._lock:
                self._hist_cache[(sym, tf)] = hist
            self.call_from_thread(self._draw_chart, sym, tf)
            self.call_from_thread(self._draw_signals, sym, tf)
        except Exception as exc:
            self.call_from_thread(self._status, f"Chart error: {exc}")

    def _load_options(self, sym):
        try:
            t = yf.Ticker(sym); exps = t.options
            if not exps: return
            chain = t.option_chain(exps[0])
            self.call_from_thread(self._draw_options, chain.calls, exps[0])
        except Exception as exc:
            self.call_from_thread(self._status, f"Options: {exc}")

    # ── Auto refresh ───────────────────────────────────────────────────────────
    def _auto_refresh(self):
        threading.Thread(target=self._refresh_prices, daemon=True).start()

    def _refresh_prices(self):
        for sym in self._cur_wl:
            self._load_price(sym)
        self._load_details(self._cur_sym)
        now = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(self._status, f"Updated {now}")

    # ── Currency ───────────────────────────────────────────────────────────────
    def _rate(self): return self._forex_cache.get(self._cur_ccy, 1.0)
    def _csym(self): return CURRENCIES[self._cur_ccy][0]

    def _fmt(self, usd):
        if usd is None: return "N/A"
        v = float(usd) * self._rate(); s = self._csym()
        return f"{s}{v:,.0f}" if self._cur_ccy == "JPY" else f"{s}{v:,.2f}"

    def _fmt_cap(self, usd):
        if usd is None: return "N/A"
        v = float(usd) * self._rate(); s = self._csym()
        if v >= 1e12: return f"{s}{v/1e12:.2f}T"
        if v >= 1e9:  return f"{s}{v/1e9:.2f}B"
        if v >= 1e6:  return f"{s}{v/1e6:.2f}M"
        return f"{s}{v:,.0f}"

    # ── Draw ───────────────────────────────────────────────────────────────────
    def _rebuild_wl(self):
        n = len(self._wl_names); idx = self._wl_idx
        self.query_one("#wl-hdr").update(
            f"  [{idx+1}/{n}] [bold]{self._cur_wl_name}[/bold]  [ ] switch")
        lv = self.query_one("#wl", ListView)
        for _ in range(len(list(lv.query(ListItem)))): lv.pop(0)
        for sym in self._cur_wl:
            p, pc = self._price_cache.get(sym, (None, None))
            lv.append(ListItem(Label(self._wl_label(sym, p, pc))))

    def _wl_label(self, sym, price, prev):
        if price is None: return f"{sym:<6} …"
        chg = ((price-prev)/prev*100) if prev else 0
        clr = "green" if chg >= 0 else "red"; a = "▲" if chg >= 0 else "▼"
        return f"{sym:<6} {self._fmt(price)}  [{clr}]{a}{abs(chg):.1f}%[/{clr}]"

    def _draw_wl_item(self, sym):
        if sym not in self._cur_wl: return
        idx = self._cur_wl.index(sym)
        items = list(self.query_one("#wl", ListView).query(ListItem))
        if idx < len(items):
            p, pc = self._price_cache.get(sym, (None, None))
            items[idx].query_one(Label).update(self._wl_label(sym, p, pc))

    def _draw_details(self, sym):
        c = self._info_cache.get(sym, {})
        if not c: return
        info = c.get("info", {}); pre = c.get("pre"); post = c.get("post")

        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev  = info.get("previousClose") or 0
        chg   = price - prev; pct = (chg/prev*100) if prev else 0
        clr   = "green" if chg >= 0 else "red"; arrow = "▲" if chg >= 0 else "▼"

        self.query_one("#sym-line").update(
            f" [bold]{sym}[/bold]  {info.get('longName',sym)[:40]}"
            f"  [#6c7086]{info.get('exchange','')}  {self._cur_ccy}[/#6c7086]")
        ext = ""
        if pre:  ext += f"  Pre: {self._fmt(pre)}"
        if post: ext += f"  Post: {self._fmt(post)}"
        self.query_one("#price-line").update(
            f" [bold]{self._fmt(price)}[/bold]"
            f"  [{clr}]{arrow} {self._fmt(abs(chg))} ({pct:+.2f}%)[/{clr}]"
            f"[#6c7086]{ext}[/#6c7086]")
        self._draw_overview(sym)
        self._draw_chart_ctrl()
        self._status(f"Updated {sym}")

    def _draw_overview(self, sym):
        c    = self._info_cache.get(sym, {})
        info = c.get("info", {}) if c else {}
        cal  = c.get("cal", {})  if c else {}
        pre  = c.get("pre")
        post = c.get("post")
        fin  = self._fin_cache.get(sym, {})

        if not info: return

        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev  = info.get("previousClose") or price
        chg   = price - prev; pct = (chg/prev*100) if prev else 0
        clr   = "green" if chg >= 0 else "red"

        rec     = (info.get("recommendationKey") or "N/A").upper()
        rec_n   = info.get("numberOfAnalystOpinions", "N/A")
        tp      = self._fmt(info.get("targetMeanPrice"))
        tp_hi   = self._fmt(info.get("targetHighPrice"))
        tp_lo   = self._fmt(info.get("targetLowPrice"))
        rec_clr = {"STRONG_BUY":"green","BUY":"green","HOLD":"yellow",
                   "SELL":"red","STRONG_SELL":"red"}.get(rec, "white")

        dy  = info.get("dividendYield")
        dy_s = f"{dy*100:.2f}%" if dy else "N/A"

        earn_date = earn_eps = "N/A"
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or cal.get("earningsDate")
            if ed:
                items = list(ed) if hasattr(ed,"__iter__") and not isinstance(ed,str) else [ed]
                earn_date = str(items[0])[:10] if items else "N/A"
            ee = cal.get("EPS Estimate") or cal.get("epsAverage")
            if ee:
                try: earn_eps = f"${float(ee):.2f}"
                except: pass

        R = 20
        about = info.get("longBusinessSummary","")
        about_short = (about[:300] + "…") if len(about) > 300 else about

        lines = [
            f"[bold #89b4fa]── Price ────────────────────────────────────────────────────────────────[/bold #89b4fa]",
            f"{'Open':<{R}}{self._fmt(info.get('open'))}"
            f"   {'Prev Close':<{R}}{self._fmt(info.get('previousClose'))}"
            f"   {'Day High':<{R}}{self._fmt(info.get('dayHigh'))}",
            f"{'Day Low':<{R}}{self._fmt(info.get('dayLow'))}"
            f"   {'52W High':<{R}}{self._fmt(info.get('fiftyTwoWeekHigh'))}"
            f"   {'52W Low':<{R}}{self._fmt(info.get('fiftyTwoWeekLow'))}",
            f"{'Volume':<{R}}{_fmt_large(info.get('volume'))}"
            f"   {'Avg Volume':<{R}}{_fmt_large(info.get('averageVolume'))}"
            f"   {'Beta':<{R}}{info.get('beta','N/A')}",
            f"",
            f"[bold #89b4fa]── Valuation ────────────────────────────────────────────────────────────[/bold #89b4fa]",
            f"{'Market Cap':<{R}}{self._fmt_cap(info.get('marketCap'))}"
            f"   {'P/E (TTM)':<{R}}{info.get('trailingPE','N/A')}"
            f"   {'Fwd P/E':<{R}}{info.get('forwardPE','N/A')}",
            f"{'EPS (TTM)':<{R}}{self._fmt(info.get('trailingEps'))}"
            f"   {'EPS Fwd':<{R}}{self._fmt(info.get('forwardEps'))}"
            f"   {'P/B':<{R}}{info.get('priceToBook','N/A')}",
            f"{'P/S':<{R}}{info.get('priceToSalesTrailing12Months','N/A')}"
            f"   {'EV/EBITDA':<{R}}{info.get('enterpriseToEbitda','N/A')}"
            f"   {'Div Yield':<{R}}{dy_s}",
            f"",
        ]

        if fin:
            lines += [
                f"[bold #89b4fa]── Financials (TTM) ─────────────────────────────────────────────────────[/bold #89b4fa]",
            ]
            fin_items = list(fin.items())
            # 3-column layout
            for i in range(0, len(fin_items), 3):
                row = fin_items[i:i+3]
                row_str = "".join(f"{k:<{R}}{v}   " for k,v in row)
                lines.append(row_str)
            lines.append("")

        lines += [
            f"[bold #89b4fa]── Analyst & Earnings ───────────────────────────────────────────────────[/bold #89b4fa]",
            f"{'Rating':<{R}}[{rec_clr}]{rec}[/{rec_clr}] ({rec_n} analysts)"
            f"   {'Target':<{R}}{tp}"
            f"   {'Target Hi':<{R}}{tp_hi}",
            f"{'Earn Date':<{R}}{earn_date}"
            f"   {'EPS Est':<{R}}{earn_eps}"
            f"   {'Target Lo':<{R}}{tp_lo}",
            f"{'Pre-Mkt':<{R}}{self._fmt(pre) if pre else 'N/A'}"
            f"   {'After-Hrs':<{R}}{self._fmt(post) if post else 'N/A'}"
            f"   {'Sector':<{R}}{info.get('sector','N/A')}",
            f"",
        ]

        if about_short:
            lines += [
                f"[bold #89b4fa]── About ────────────────────────────────────────────────────────────────[/bold #89b4fa]",
                f"[#6c7086]{about_short}[/#6c7086]",
            ]

        self.query_one("#ov-content").update("\n".join(lines))

    def _draw_signals(self, sym, tf):
        hist = self._hist_cache.get((sym, tf))
        sig_w = self.query_one("#sig-area")
        if hist is None or hist.empty or len(hist) < 30:
            sig_w.update("[#6c7086]Not enough data for signals[/#6c7086]")
            return
        try:
            closes = hist["Close"].tolist()
            result = _signals(closes)
            if not result:
                sig_w.update("[#6c7086]Insufficient data[/#6c7086]")
                return
            sigs, score, (verdict, v_clr) = result

            # Score bar (−6 to +6 range)
            bar_total = 12; bar_filled = min(max(score + 6, 0), 12)
            bar = ""
            for i in range(bar_total):
                if i < bar_filled:
                    clr = "green" if score > 0 else "red" if score < 0 else "yellow"
                    bar += f"[{clr}]█[/{clr}]"
                else:
                    bar += "[#313244]░[/#313244]"

            # Verdict badge
            badge_bg = {"green": "#40a02b", "yellow": "#df8e1d", "red": "#d20f39"}
            badge_clr = badge_bg.get(v_clr, "#313244")

            lines = [
                f"  [on {badge_clr}] [bold]{verdict}[/bold] [/on {badge_clr}]"
                f"  score {score:+d}  {bar}  "
                f"[#6c7086]{sym} · {tf}[/#6c7086]",
            ]
            # Two signals per row
            row = []
            for name, val, desc, s in sigs:
                icon  = "▲" if s > 0 else "▼" if s < 0 else "●"
                clr   = "#a6e3a1" if s > 0 else "#f38ba8" if s < 0 else "#6c7086"
                entry = f"  [{clr}]{icon}[/{clr}] [bold]{name}[/bold] {val}  [#6c7086]{desc}[/#6c7086]"
                row.append(entry)
                if len(row) == 2:
                    lines.append("".join(row)); row = []
            if row: lines.append("".join(row))
            sig_w.update("\n".join(lines))
        except Exception as exc:
            sig_w.update(f"[red]Signal error: {exc}[/red]")

    def _draw_chart(self, sym, tf):
        hist = self._hist_cache.get((sym, tf))
        cw   = self.query_one("#ch-area")
        if hist is None or hist.empty:
            cw.update("No chart data"); return
        try:
            rate   = self._rate()
            closes = [p*rate for p in hist["Close"].tolist()]
            opens  = [p*rate for p in hist["Open"].tolist()]
            highs  = [p*rate for p in hist["High"].tolist()]
            lows   = [p*rate for p in hist["Low"].tolist()]
            vols   = hist["Volume"].tolist()
            xs     = list(range(len(closes)))
            w  = max(cw.size.width  or 100, 50)
            h  = max(cw.size.height or 22,  14)
            ind = self._indicator

            # Build x-axis date ticks (≤10 evenly spaced)
            try:
                idx = hist.index
                dates = [str(d)[:10] for d in idx]
                n_ticks = min(10, len(dates))
                step = max(1, len(dates) // n_ticks)
                tick_pos   = list(range(0, len(dates), step))
                tick_lbls  = [dates[i] for i in tick_pos]
            except Exception:
                tick_pos = tick_lbls = None

            # Price direction colour
            price_clr = "green+" if closes[-1] >= closes[0] else "red+"

            # Period return for title
            ret = ((closes[-1]-closes[0])/closes[0]*100) if closes[0] else 0
            ret_s = f"{ret:+.2f}%"
            csym  = self._csym()
            title = f"{sym}  {tf}  {csym}{closes[-1]:,.2f}  ({ret_s})"

            def _set_ticks():
                if tick_pos and tick_lbls:
                    try: plt.xticks(tick_pos, tick_lbls)
                    except Exception: pass

            plt.clf()
            plt.theme("dark")

            if ind in ("RSI", "MACD"):
                plt.subplots(2, 1)
                plt.subplot(1, 1); plt.plotsize(w, int(h*0.62))
                plt.plot(closes, color=price_clr, label=sym)
                _set_ticks()
                plt.title(title)
                plt.xlabel(""); plt.ylabel(f"{csym}")
                plt.subplot(2, 1); plt.plotsize(w, int(h*0.38))
                if ind == "RSI":
                    rsi_vals = _rsi(closes)
                    plt.plot(rsi_vals, color="yellow+", label="RSI(14)")
                    try: plt.hline(70, color="red"); plt.hline(30, color="green")
                    except Exception: pass
                    _set_ticks(); plt.title("RSI(14)"); plt.ylim(0, 100)
                else:
                    m, sig_line, hm = _macd(closes)
                    plt.plot(m, color="cyan+", label="MACD")
                    plt.plot(sig_line, color="orange+", label="Signal")
                    pos_hm = [v if v >= 0 else 0 for v in hm]
                    neg_hm = [v if v <  0 else 0 for v in hm]
                    try:
                        plt.bar(xs, pos_hm, color="green+", label="")
                        plt.bar(xs, neg_hm, color="red+",   label="")
                    except Exception:
                        plt.bar(hm)
                    _set_ticks(); plt.title("MACD")
            else:
                plt.subplots(2, 1)
                plt.subplot(1, 1); plt.plotsize(w, int(h*0.74))
                if self._chart_t == "candle":
                    try:
                        plt.candlestick(xs, {"Open":opens,"High":highs,
                                              "Low":lows,"Close":closes})
                    except Exception:
                        plt.plot(closes, color=price_clr, label=sym)
                else:
                    plt.plot(closes, color=price_clr, label=sym)
                    # Shaded area under line
                    try:
                        plt.fill_between(xs, closes, color=price_clr, alpha=0.15)
                    except Exception: pass

                if ind == "SMA20":
                    plt.plot(_sma(closes,20), color="yellow+", label="SMA20")
                elif ind == "EMA20":
                    plt.plot(_ema(closes,20), color="cyan+", label="EMA20")
                elif ind == "BB":
                    lo, mid, hi_ = _bb(closes)
                    plt.plot(hi_,  color="red+",    label="BB+")
                    plt.plot(mid,  color="white",   label="mid")
                    plt.plot(lo,   color="green+",  label="BB−")

                _set_ticks(); plt.title(title); plt.ylabel(f"{csym}")

                plt.subplot(2, 1); plt.plotsize(w, int(h*0.26))
                # Colour volume bars by up/down
                vol_clrs = []
                for i, v in enumerate(vols):
                    if i == 0:
                        vol_clrs.append("green+")
                    elif closes[i] >= closes[i-1]:
                        vol_clrs.append("green+")
                    else:
                        vol_clrs.append("red+")
                try:
                    for i, (x_, v_) in enumerate(zip(xs, vols)):
                        plt.bar([x_], [v_], color=vol_clrs[i])
                except Exception:
                    plt.bar(xs, vols)
                _set_ticks(); plt.title("Volume")

            chart_str = _plt_build()
            self._draw_chart_ctrl()
            cw.update("\n".join(str(l) for l in _ansi.decode(chart_str)))
        except Exception as exc:
            cw.update(f"Chart error: {exc}")

    def _draw_chart_ctrl(self):
        parts = []
        for tf in TIMEFRAMES:
            if tf == self._cur_tf:
                parts.append(f"[bold #89b4fa][{tf}][/bold #89b4fa]")
            else:
                parts.append(f"[#45475a]{tf}[/#45475a]")
        ct  = "[bold #a6e3a1]CANDLE[/bold #a6e3a1]" if self._chart_t == "candle" \
              else "[#6c7086]LINE[/#6c7086]"
        ind = f"[bold #f9e2af]{self._indicator}[/bold #f9e2af]"
        try:
            self.query_one("#ch-ctrl").update(
                f"  {' '.join(parts)}   t={ct}   i={ind}   {self._cur_ccy}")
        except Exception: pass

    def _draw_options(self, calls, expiry):
        self.query_one("#opt-ctrl").update(
            f"  [bold #89b4fa]Calls[/bold #89b4fa]   Expiry: {expiry}")
        tbl = self.query_one("#opt-tbl", DataTable); tbl.clear()
        for _, row in calls.head(30).iterrows():
            itm = "[green]✓[/green]" if row.get("inTheMoney") else ""
            tbl.add_row(
                f"${row.get('strike',0):.2f}",
                f"${row.get('lastPrice',0):.2f}",
                f"${row.get('bid',0):.2f}",
                f"${row.get('ask',0):.2f}",
                f"{row.get('impliedVolatility',0)*100:.1f}%",
                _fmt_large(row.get("openInterest",0)),
                _fmt_large(row.get("volume",0)),
                itm)

    def _draw_portfolio(self):
        tbl = self.query_one("#port-tbl", DataTable); tbl.clear()
        tv = tp = 0.0
        for pos in self._portfolio:
            sym = pos["symbol"]; sh = pos["shares"]; cost = pos["cost"]
            p, _ = self._price_cache.get(sym, (cost, cost))
            pl = (p-cost)*sh; pct = ((p-cost)/cost*100) if cost else 0
            val = p*sh; tv += val; tp += pl
            clr = "green" if pl >= 0 else "red"
            tbl.add_row(sym, f"{sh:.2f}", self._fmt(cost), self._fmt(p),
                        f"[{clr}]{self._fmt(pl)}[/{clr}]",
                        f"[{clr}]{pct:+.2f}%[/{clr}]", self._fmt(val))
        clr_t = "green" if tp >= 0 else "red"
        self.query_one("#port-hdr").update(
            f"  Portfolio  Value: [bold]{self._fmt(tv)}[/bold]  "
            f"P&L: [{clr_t}][bold]{self._fmt(tp)}[/bold][/{clr_t}]  "
            f"[#6c7086]p=add position[/#6c7086]")

    def _draw_alerts(self):
        tbl = self.query_one("#alert-tbl", DataTable); tbl.clear()
        for a in self._alerts:
            s = "[green]✓ Triggered[/green]" if a.get("triggered") \
                else "[#6c7086]Watching[/#6c7086]"
            tbl.add_row(a["symbol"], f"{a['direction']} {self._fmt(a['price'])}", s)
        self.query_one("#alert-hdr").update(
            "  Alerts  [#6c7086]n=add alert for current ticker[/#6c7086]")

    # ── AI Analysis ─────────────────────────────────────────────────────────────
    def _ai_append(self, text):
        """Append text to the AI output widget."""
        w = self.query_one("#ai-output")
        current = getattr(w, "_ai_text", "")
        new_text = current + text
        w._ai_text = new_text
        w.update(new_text)

    def _ai_set(self, text):
        w = self.query_one("#ai-output")
        w._ai_text = text
        w.update(text)

    def _load_ai_analysis(self, sym):
        info_c = self._info_cache.get(sym, {})
        info   = info_c.get("info", {}) if info_c else {}
        hist   = self._hist_cache.get((sym, self._cur_tf))
        fin    = self._fin_cache.get(sym, {})

        sigs_data = None
        if hist is not None and not hist.empty and len(hist) >= 30:
            try: sigs_data = _signals(hist["Close"].tolist())
            except Exception: pass

        context  = _build_stock_context(sym, info, hist, sigs_data, fin)
        system   = (
            "You are a professional financial analyst assistant embedded in a stock trading terminal. "
            "Provide concise, actionable analysis. Use plain text only (no markdown, no bullet symbols, "
            "no headers with # or *). "
            "Structure: Quick summary (2-3 sentences), Key strengths, Key risks, "
            "Technical outlook, Verdict. Be direct. No disclaimers."
        )
        user_msg = f"Analyze this stock:\n\n{context}"
        messages = [{"role": "user", "content": user_msg}]

        header = f"[bold #89b4fa]AI Analysis — {sym}[/bold #89b4fa]\n\n"
        self.call_from_thread(self._ai_set, header)
        self.call_from_thread(self.query_one("#ai-status").update,
            "[#f9e2af]Claude is thinking…[/#f9e2af]")

        acc = {"text": ""}

        def on_text(chunk):
            acc["text"] += chunk
            self.call_from_thread(self._ai_set, header + acc["text"])

        def on_done():
            self._ai_history[sym] = [
                {"role": "user",      "content": user_msg},
                {"role": "assistant", "content": acc["text"]},
            ]
            self.call_from_thread(self.query_one("#ai-status").update,
                "[#6c7086]Type a question below and press Enter[/#6c7086]")

        def on_error(msg):
            self.call_from_thread(self._ai_set,
                header + f"[red]Error: {msg}[/red]")
            self.call_from_thread(self.query_one("#ai-status").update, "")

        _stream_claude(system, messages, on_text, on_done, on_error)

    def _ask_claude(self, sym, question):
        history = list(self._ai_history.get(sym, []))
        history.append({"role": "user", "content": question})

        self.call_from_thread(self._ai_append,
            f"\n\n[bold #f9e2af]You:[/bold #f9e2af] {question}\n\n"
            f"[bold #89b4fa]Claude:[/bold #89b4fa] ")
        self.call_from_thread(self.query_one("#ai-status").update,
            "[#f9e2af]Thinking…[/#f9e2af]")

        system  = "You are a professional financial analyst. Answer concisely in plain text. No markdown."
        acc     = {"text": ""}

        def on_text(chunk):
            acc["text"] += chunk
            self.call_from_thread(self._ai_append, chunk)

        def on_done():
            history.append({"role": "assistant", "content": acc["text"]})
            self._ai_history[sym] = history
            self.call_from_thread(self.query_one("#ai-status").update,
                "[#6c7086]Type a question and press Enter[/#6c7086]")

        def on_error(msg):
            self.call_from_thread(self._ai_append, f"[red]Error: {msg}[/red]")
            self.call_from_thread(self.query_one("#ai-status").update, "")

        _stream_claude(system, history, on_text, on_done, on_error)

    # ── Alert loop ──────────────────────────────────────────────────────────────
    def _alert_loop(self):
        while True:
            time.sleep(30)
            changed = False
            for a in self._alerts:
                if a.get("triggered"): continue
                try:
                    p = yf.Ticker(a["symbol"]).fast_info.last_price
                    if p is None: continue
                    hit = (a["direction"] == "above" and p >= a["price"]) or \
                          (a["direction"] == "below" and p <= a["price"])
                    if hit:
                        a["triggered"] = True; changed = True
                        _notify(f"stocky: {a['symbol']}",
                                f"{a['symbol']} is {a['direction']} {self._fmt(a['price'])}")
                except Exception: pass
            if changed:
                _save(ALERT_FILE, self._alerts)
                self.call_from_thread(self._draw_alerts)

    # ── Events ──────────────────────────────────────────────────────────────────
    def on_list_view_highlighted(self, e: ListView.Highlighted):
        if e.item is None: return
        idx = self.query_one("#wl", ListView).index
        if idx is None or idx >= len(self._cur_wl): return
        sym = self._cur_wl[idx]
        if sym == self._cur_sym: return
        self._cur_sym = sym
        self._status(f"Loading {sym}…")
        threading.Thread(target=self._load_details, args=(sym,), daemon=True).start()
        threading.Thread(target=self._load_hist, args=(sym, self._cur_tf), daemon=True).start()
        threading.Thread(target=self._load_financials, args=(sym,), daemon=True).start()

    def on_tabbed_content_tab_activated(self, e: TabbedContent.TabActivated):
        try:    pid = e.pane.id
        except: pid = str(e.tab.id)
        sym = self._cur_sym
        if pid == "tab-options":
            threading.Thread(target=self._load_options, args=(sym,), daemon=True).start()
        elif pid == "tab-ai":
            # Only auto-generate if no history yet for this sym
            if sym not in self._ai_history:
                self.query_one("#ai-status").update("[#f9e2af]Generating analysis…[/#f9e2af]")
                threading.Thread(target=self._load_ai_analysis, args=(sym,), daemon=True).start()
            else:
                # Redisplay existing history
                history = self._ai_history[sym]
                text = f"[bold #89b4fa]AI Analysis — {sym}[/bold #89b4fa]\n\n"
                for msg in history:
                    if msg["role"] == "user":
                        text += f"\n[bold #f9e2af]You:[/bold #f9e2af] {msg['content']}\n\n"
                    else:
                        text += f"[bold #89b4fa]Claude:[/bold #89b4fa] {msg['content']}\n"
                self._ai_set(text)
                self.query_one("#ai-status").update(
                    "[#6c7086]Type a question and press Enter[/#6c7086]")
        elif pid == "tab-portaler":
            self._draw_portfolio(); self._draw_alerts()

    def on_input_submitted(self, e: Input.Submitted):
        if e.input.id != "ai-input": return
        question = e.value.strip()
        if not question: return
        e.input.value = ""
        sym = self._cur_sym
        threading.Thread(target=self._ask_claude, args=(sym, question), daemon=True).start()

    def on_resize(self, _):
        key = (self._cur_sym, self._cur_tf)
        if key in self._hist_cache: self._draw_chart(*key)

    # ── Actions ─────────────────────────────────────────────────────────────────
    def action_add_ticker(self):
        def done(sym):
            if not sym: return
            sym = sym.upper()
            if sym in self._cur_wl:
                self._status(f"{sym} already in list"); return
            self._cur_wl.append(sym)
            _save(WL_FILE, self._wls)
            self.query_one("#wl", ListView).append(ListItem(Label(sym)))
            threading.Thread(target=self._load_price, args=(sym,), daemon=True).start()
        self.push_screen(InputModal("Add ticker (Enter / Esc):"), done)

    def action_del_ticker(self):
        lv  = self.query_one("#wl", ListView)
        idx = lv.index
        if idx is None or len(self._cur_wl) <= 1: return
        self._cur_wl.pop(idx); _save(WL_FILE, self._wls); lv.pop(idx)
        self._cur_sym = self._cur_wl[min(idx, len(self._cur_wl)-1)]
        threading.Thread(target=self._load_details, args=(self._cur_sym,), daemon=True).start()
        threading.Thread(target=self._load_hist, args=(self._cur_sym, self._cur_tf), daemon=True).start()

    def action_refresh(self):
        self._status("Refreshing…")
        threading.Thread(target=self._boot, daemon=True).start()

    def action_cycle_ccy(self):
        self._cur_ccy = CCY_LIST[(CCY_LIST.index(self._cur_ccy)+1) % len(CCY_LIST)]
        threading.Thread(target=self._apply_ccy, daemon=True).start()

    def _apply_ccy(self):
        self._load_forex()
        sym = self._cur_sym
        for s in self._cur_wl: self.call_from_thread(self._draw_wl_item, s)
        self.call_from_thread(self._draw_details, sym)
        self.call_from_thread(self._draw_chart, sym, self._cur_tf)
        self.call_from_thread(self._draw_portfolio)

    def action_toggle_chart(self):
        self._chart_t = "candle" if self._chart_t == "line" else "line"
        key = (self._cur_sym, self._cur_tf)
        if key in self._hist_cache: self._draw_chart(*key)

    def action_cycle_ind(self):
        self._ind_idx   = (self._ind_idx+1) % len(INDICATORS)
        self._indicator = INDICATORS[self._ind_idx]
        key = (self._cur_sym, self._cur_tf)
        if key in self._hist_cache: self._draw_chart(*key)

    def _switch_tf(self, tf):
        self._cur_tf = tf; self._draw_chart_ctrl()
        key = (self._cur_sym, tf)
        if key in self._hist_cache:
            self._draw_chart(*key)
            self._draw_signals(*key)
        else:
            self._status(f"Loading {tf}…")
            threading.Thread(target=self._load_hist, args=(self._cur_sym, tf), daemon=True).start()

    def action_tf_1d(self): self._switch_tf("1D")
    def action_tf_1w(self): self._switch_tf("1W")
    def action_tf_1m(self): self._switch_tf("1M")
    def action_tf_3m(self): self._switch_tf("3M")
    def action_tf_1y(self): self._switch_tf("1Y")
    def action_tf_5y(self): self._switch_tf("5Y")

    def action_add_alert(self):
        def done(a):
            if not a: return
            self._alerts.append(a); _save(ALERT_FILE, self._alerts)
            self._draw_alerts()
        self.push_screen(AlertModal(self._cur_sym), done)

    def action_add_position(self):
        def done(pos):
            if not pos: return
            for p in self._portfolio:
                if p["symbol"] == pos["symbol"]:
                    p.update(pos); break
            else:
                self._portfolio.append(pos)
            _save(PORT_FILE, self._portfolio); self._draw_portfolio()
        self.push_screen(PositionModal(self._cur_sym), done)

    def action_open_screener(self):
        def done(sym):
            if not sym: return
            self._cur_sym = sym
            threading.Thread(target=self._load_details, args=(sym,), daemon=True).start()
            threading.Thread(target=self._load_hist, args=(sym, self._cur_tf), daemon=True).start()
            threading.Thread(target=self._load_financials, args=(sym,), daemon=True).start()
        self.push_screen(ScreenerScreen(), done)

    def _switch_wl(self, delta):
        self._wl_idx = (self._wl_idx + delta) % len(self._wl_names)
        self._rebuild_wl()
        if self._cur_wl:
            self._cur_sym = self._cur_wl[0]
            threading.Thread(target=self._load_details, args=(self._cur_sym,), daemon=True).start()
            threading.Thread(target=self._load_hist, args=(self._cur_sym, self._cur_tf), daemon=True).start()

    def action_prev_wl(self): self._switch_wl(-1)
    def action_next_wl(self): self._switch_wl(+1)
    def action_cursor_down(self): self.query_one("#wl", ListView).action_cursor_down()
    def action_cursor_up(self):   self.query_one("#wl", ListView).action_cursor_up()

    def _status(self, msg):
        try: self.query_one("#status").update(f"[#585b70]{msg}[/#585b70]")
        except Exception: pass


def run():
    StockApp().run()

if __name__ == "__main__":
    run()
