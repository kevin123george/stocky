#!/usr/bin/env python3
"""stocky — professional stock TUI for traders"""

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

# ── Indicators ─────────────────────────────────────────────────────────────────
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

def _plt_build():
    try:
        return plt.build()
    except AttributeError:
        old = sys.stdout; sys.stdout = buf = io.StringIO()
        plt.show(); sys.stdout = old
        return buf.getvalue()

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
    #btns { margin-top: 1; }
    Button { margin-right: 1; }
    """
    def __init__(self, sym):
        super().__init__(); self._sym = sym
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"Price alert for {self._sym}")
            yield Input(placeholder="Price  e.g. 200.00", id="price")
            yield Input(placeholder="Direction:  above  or  below", id="dir")
            with Horizontal(id="btns"):
                yield Button("Add", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")
    def on_mount(self): self.query_one("#price").focus()
    def on_button_pressed(self, e):
        if e.button.id == "ok":
            try:
                p = float(self.query_one("#price").value)
                d = self.query_one("#dir").value.strip().lower()
                if d not in ("above", "below"): d = "above"
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
    #btns { margin-top: 1; }
    Button { margin-right: 1; }
    """
    def __init__(self, sym):
        super().__init__(); self._sym = sym
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"Position: {self._sym}")
            yield Input(placeholder="Shares  e.g. 10", id="shares")
            yield Input(placeholder="Avg cost per share  e.g. 150.00", id="cost")
            with Horizontal(id="btns"):
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
        t.add_columns("Symbol", "Price", "Chg%", "Volume", "Mkt Cap", "P/E", "Sector")
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
                row  = (sym, f"${p:.2f}", f"[{clr}]{chg:+.2f}%[/{clr}]", vol, cap, pe, sec)
                self.call_from_thread(self.query_one("#sc-tbl", DataTable).add_row, *row)
                self.call_from_thread(self.query_one("#sc-sta").update, f"Loaded {sym}")
            except Exception:
                pass

    def on_data_table_row_selected(self, e: DataTable.RowSelected):
        row = self.query_one("#sc-tbl", DataTable).get_row(e.row_key)
        self.dismiss(str(row[0]))

    def on_key(self, e):
        if e.key == "escape": self.dismiss(None)


# ── Main App ───────────────────────────────────────────────────────────────────
class StockApp(App):
    # NOTE: max 5 tabs in TabbedContent inside a Horizontal split (textual 8.x limit)
    CSS = """
    Screen  { background: #1e1e2e; }
    Header  { background: #181825; color: #cdd6f4; }
    Footer  { background: #181825; color: #585b70; }
    #main   { height: 1fr; }
    #left   { width: 28; border-right: solid #313244; }
    #wl-hdr { height: 1; background: #313244; color: #89b4fa; padding: 0 1; }
    ListView { background: #1e1e2e; border: none; }
    ListItem { background: #1e1e2e; color: #cdd6f4; padding: 0 1; height: 1; }
    ListItem:hover     { background: #313244; }
    ListItem.--highlight { background: #45475a; color: #89b4fa; }
    #right    { width: 1fr; }
    #sym-line { height: 1; margin-top: 1; padding: 0 2; color: #89b4fa; }
    #price-line { height: 1; padding: 0 2; margin-bottom: 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    #ch-ctrl  { height: 1; background: #181825; padding: 0 1; }
    #ch-area  { height: 1fr; }
    #opt-ctrl { height: 1; background: #181825; padding: 0 1; }
    DataTable { height: 1fr; }
    #news-sc  { height: 1fr; }
    #port-hdr  { height: 1; color: #a6e3a1; padding: 0 1; }
    #port-tbl  { height: 1fr; }
    #alert-hdr { height: 1; color: #6c7086; padding: 0 1; }
    #alert-tbl { height: 1fr; }
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

    def __init__(self):
        super().__init__()
        self._wls       = _load(WL_FILE,    DEFAULT_WL)
        self._portfolio = _load(PORT_FILE,  [])
        self._alerts    = _load(ALERT_FILE, [])
        self._wl_names  = list(self._wls.keys())
        self._wl_idx    = 0
        self._cur_sym   = self._wls[self._wl_names[0]][0]
        self._cur_tf    = "1M"
        self._cur_ccy   = "USD"
        self._chart_t   = "line"
        self._ind_idx   = 0
        self._indicator = "None"
        self._info_cache  = {}
        self._price_cache = {}
        self._hist_cache  = {}
        self._forex_cache = {"USD": 1.0}
        self._lock        = threading.Lock()

    @property
    def _cur_wl(self):      return self._wls[self._wl_names[self._wl_idx]]
    @property
    def _cur_wl_name(self): return self._wl_names[self._wl_idx]

    # ── Compose (max 5 tabs) ───────────────────────────────────────────────────
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
                    with TabPane("Overview", id="tab-overview"):
                        yield Static("", id="ov-content")
                    with TabPane("Chart", id="tab-chart"):
                        yield Static("", id="ch-ctrl")
                        yield Static("", id="ch-area")
                    with TabPane("Options", id="tab-options"):
                        yield Static("", id="opt-ctrl")
                        yield DataTable(id="opt-tbl", zebra_stripes=True)
                    with TabPane("News", id="tab-news"):
                        with ScrollableContainer(id="news-sc"):
                            yield Static("", id="news-body")
                    # Tab 5: Portfolio + Alerts combined
                    with TabPane("Portfolio/Alerts", id="tab-portaler"):
                        yield Static("", id="port-hdr")
                        yield DataTable(id="port-tbl", zebra_stripes=True)
                        yield Static("", id="alert-hdr")
                        yield DataTable(id="alert-tbl", zebra_stripes=True)
                yield Static("", id="status")
        yield Footer()

    REFRESH_INTERVAL = 3  # seconds between auto-refreshes

    def on_mount(self):
        self._init_tables()
        self._rebuild_wl()
        self._status("Fetching data…")
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self._alert_loop, daemon=True).start()
        self.set_interval(self.REFRESH_INTERVAL, self._auto_refresh)

    def _auto_refresh(self):
        threading.Thread(target=self._refresh_prices, daemon=True).start()

    def _refresh_prices(self):
        for sym in self._cur_wl:
            self._load_price(sym)
        self._load_details(self._cur_sym)
        now = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(self._status, f"Updated {now}")

    def _init_tables(self):
        self.query_one("#opt-tbl",   DataTable).add_columns(
            "Strike", "Last", "Bid", "Ask", "IV", "OI", "Volume", "ITM")
        self.query_one("#port-tbl",  DataTable).add_columns(
            "Symbol", "Shares", "Avg Cost", "Current", "P&L", "P&L%", "Value")
        self.query_one("#alert-tbl", DataTable).add_columns(
            "Symbol", "Condition", "Status")

    # ── Boot ───────────────────────────────────────────────────────────────────
    def _boot(self):
        self._load_forex()
        for sym in self._cur_wl:
            self._load_price(sym)
        self._load_details(self._cur_sym)
        self._load_hist(self._cur_sym, self._cur_tf)
        self.call_from_thread(self._draw_portfolio)
        self.call_from_thread(self._draw_alerts)

    def _load_forex(self):
        for ccy, (_, pair) in CURRENCIES.items():
            if pair is None:
                self._forex_cache[ccy] = 1.0; continue
            try:
                r = yf.Ticker(pair).fast_info.last_price
                if r: self._forex_cache[ccy] = float(r)
            except Exception: pass

    def _load_price(self, sym: str):
        try:
            fi = yf.Ticker(sym).fast_info
            p  = fi.last_price; pc = fi.previous_close or p
            if p:
                with self._lock:
                    self._price_cache[sym] = (float(p), float(pc or p))
                self.call_from_thread(self._draw_wl_item, sym)
        except Exception: pass

    def _load_details(self, sym: str):
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

    def _load_hist(self, sym: str, tf: str):
        period, interval = TIMEFRAMES[tf]
        try:
            hist = yf.Ticker(sym).history(period=period, interval=interval)
            with self._lock:
                self._hist_cache[(sym, tf)] = hist
            self.call_from_thread(self._draw_chart, sym, tf)
        except Exception as exc:
            self.call_from_thread(self._status, f"Chart error: {exc}")

    def _load_options(self, sym: str):
        try:
            t = yf.Ticker(sym); exps = t.options
            if not exps: return
            chain = t.option_chain(exps[0])
            self.call_from_thread(self._draw_options, chain.calls, exps[0])
        except Exception as exc:
            self.call_from_thread(self._status, f"Options: {exc}")

    def _load_news(self, sym: str):
        try:
            items = yf.Ticker(sym).news or []
            lines = []
            for it in items[:20]:
                ts    = it.get("providerPublishTime", 0)
                dt    = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if ts else ""
                title = it.get("title", "")
                pub   = it.get("publisher", "")
                lines.append(
                    f"[bold #cdd6f4]{title}[/bold #cdd6f4]\n"
                    f"[#6c7086]{pub}  {dt}[/#6c7086]\n"
                )
            content = "\n".join(lines) or "No news"
        except Exception as exc:
            content = f"News error: {exc}"
        self.call_from_thread(self.query_one("#news-body").update, content)

    # ── Currency helpers ────────────────────────────────────────────────────────
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

    # ── Draw helpers ────────────────────────────────────────────────────────────
    def _rebuild_wl(self):
        n = len(self._wl_names); idx = self._wl_idx
        self.query_one("#wl-hdr").update(
            f"  [{idx+1}/{n}] [bold]{self._cur_wl_name}[/bold]  [ ] switch")
        lv = self.query_one("#wl", ListView)
        for _ in range(len(list(lv.query(ListItem)))):
            lv.pop(0)
        for sym in self._cur_wl:
            p, pc = self._price_cache.get(sym, (None, None))
            lv.append(ListItem(Label(self._wl_label(sym, p, pc))))

    def _wl_label(self, sym, price, prev) -> str:
        if price is None: return f"{sym:<6} …"
        chg = ((price-prev)/prev*100) if prev else 0
        clr = "green" if chg >= 0 else "red"; a = "▲" if chg >= 0 else "▼"
        return f"{sym:<6} {self._fmt(price)}  [{clr}]{a}{abs(chg):.1f}%[/{clr}]"

    def _draw_wl_item(self, sym: str):
        if sym not in self._cur_wl: return
        idx   = self._cur_wl.index(sym)
        items = list(self.query_one("#wl", ListView).query(ListItem))
        if idx < len(items):
            p, pc = self._price_cache.get(sym, (None, None))
            items[idx].query_one(Label).update(self._wl_label(sym, p, pc))

    def _draw_details(self, sym: str):
        c = self._info_cache.get(sym, {})
        if not c: return
        info = c.get("info", {}); cal = c.get("cal", {})
        pre  = c.get("pre");      post = c.get("post")

        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev  = info.get("previousClose") or 0
        chg   = price - prev; pct = (chg/prev*100) if prev else 0
        clr   = "green" if chg >= 0 else "red"
        arrow = "▲" if chg >= 0 else "▼"
        name  = info.get("longName", sym); exch = info.get("exchange","")

        self.query_one("#sym-line").update(
            f" [bold]{sym}[/bold]  —  {name}   [{exch}]   [{self._cur_ccy}]")

        ext = ""
        if pre:  ext += f"  Pre: {self._fmt(pre)}"
        if post: ext += f"  Post: {self._fmt(post)}"
        self.query_one("#price-line").update(
            f" [bold]{self._fmt(price)}[/bold]"
            f"  [{clr}]{arrow} {self._fmt(abs(chg))} ({pct:+.2f}%)[/{clr}]"
            f"[#6c7086]{ext}[/#6c7086]")

        R = 16
        dy = info.get("dividendYield")
        dy_s = f"{dy*100:.2f}%" if dy else "N/A"

        rec     = (info.get("recommendationKey") or "N/A").upper()
        rec_n   = info.get("numberOfAnalystOpinions","N/A")
        tp      = self._fmt(info.get("targetMeanPrice"))
        tp_hi   = self._fmt(info.get("targetHighPrice"))
        tp_lo   = self._fmt(info.get("targetLowPrice"))
        rec_clr = {"STRONG_BUY":"green","BUY":"green","HOLD":"yellow",
                   "SELL":"red","STRONG_SELL":"red"}.get(rec, "white")

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

        self.query_one("#ov-content").update(
            f"[bold #89b4fa]── Daily {'─'*18}[/bold #89b4fa]"
            f"   [bold #89b4fa]── Fundamentals {'─'*12}[/bold #89b4fa]"
            f"   [bold #89b4fa]── Analyst & Earnings ──────[/bold #89b4fa]\n"
            f"{'Open':<{R}}{self._fmt(info.get('open'))}"
            f"   {'Market Cap':<{R}}{self._fmt_cap(info.get('marketCap'))}"
            f"   {'Rating':<{R}}[{rec_clr}]{rec}[/{rec_clr}] ({rec_n})\n"
            f"{'Prev Close':<{R}}{self._fmt(info.get('previousClose'))}"
            f"   {'P/E':<{R}}{info.get('trailingPE','N/A')}"
            f"   {'Target':<{R}}{tp}\n"
            f"{'Day High':<{R}}{self._fmt(info.get('dayHigh'))}"
            f"   {'Fwd P/E':<{R}}{info.get('forwardPE','N/A')}"
            f"   {'Target Hi':<{R}}{tp_hi}\n"
            f"{'Day Low':<{R}}{self._fmt(info.get('dayLow'))}"
            f"   {'EPS':<{R}}{self._fmt(info.get('trailingEps'))}"
            f"   {'Target Lo':<{R}}{tp_lo}\n"
            f"{'Volume':<{R}}{_fmt_large(info.get('volume'))}"
            f"   {'Beta':<{R}}{info.get('beta','N/A')}"
            f"   {'Earn Date':<{R}}{earn_date}\n"
            f"{'Avg Volume':<{R}}{_fmt_large(info.get('averageVolume'))}"
            f"   {'Div Yield':<{R}}{dy_s}"
            f"   {'EPS Est':<{R}}{earn_eps}\n"
            f"{'52W High':<{R}}{self._fmt(info.get('fiftyTwoWeekHigh'))}"
            f"   {'Sector':<{R}}{info.get('sector','N/A')}"
            f"   {'Pre-Mkt':<{R}}{self._fmt(pre) if pre else 'N/A'}\n"
            f"{'52W Low':<{R}}{self._fmt(info.get('fiftyTwoWeekLow'))}"
            f"   {'Industry':<{R}}{(info.get('industry') or 'N/A')[:16]}"
            f"   {'After-Hrs':<{R}}{self._fmt(post) if post else 'N/A'}\n"
        )
        self._draw_chart_ctrl()
        self._status(f"Updated {sym}")

    def _draw_chart(self, sym: str, tf: str):
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
            w = max(cw.size.width  or 100, 50)
            h = max(cw.size.height or 25,  15)
            ind = self._indicator

            plt.clf()

            if ind in ("RSI", "MACD"):
                plt.subplots(2, 1)
                plt.subplot(1, 1); plt.plotsize(w, int(h*0.6))
                plt.plot(closes); plt.title(f"{sym}  {tf}")
                plt.subplot(2, 1); plt.plotsize(w, int(h*0.4))
                if ind == "RSI":
                    plt.plot(_rsi(closes), label="RSI(14)")
                    plt.hline(70); plt.hline(30); plt.title("RSI")
                else:
                    m, sig, hm = _macd(closes)
                    plt.plot(m, label="MACD"); plt.plot(sig, label="Signal")
                    plt.bar(hm); plt.title("MACD")
            else:
                plt.subplots(2, 1)
                plt.subplot(1, 1); plt.plotsize(w, int(h*0.72))
                if self._chart_t == "candle":
                    try:
                        plt.candlestick(xs, {"Open":opens,"High":highs,
                                              "Low":lows,"Close":closes})
                    except Exception:
                        plt.plot(closes)
                else:
                    plt.plot(closes)
                if ind == "SMA20": plt.plot(_sma(closes,20), label="SMA20")
                elif ind == "EMA20": plt.plot(_ema(closes,20), label="EMA20")
                elif ind == "BB":
                    lo,mid,hi = _bb(closes)
                    plt.plot(hi,label="BB+"); plt.plot(mid,label="mid"); plt.plot(lo,label="BB-")
                plt.title(f"{sym}  {tf}  ({self._cur_ccy})")
                plt.ylabel(f"({self._csym()})")
                plt.subplot(2, 1); plt.plotsize(w, int(h*0.28))
                plt.bar(xs, vols); plt.title("Volume")

            chart_str = _plt_build()
            self._draw_chart_ctrl()
            cw.update("\n".join(str(l) for l in _ansi.decode(chart_str)))
        except Exception as exc:
            cw.update(f"Chart error: {exc}")
            self._status(f"Chart error: {exc}")

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

    def _draw_options(self, calls, expiry: str):
        self.query_one("#opt-ctrl").update(
            f"  [bold #89b4fa]Calls[/bold #89b4fa]   Expiry: {expiry}   (nearest)")
        tbl = self.query_one("#opt-tbl", DataTable)
        tbl.clear()
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
                itm,
            )

    def _draw_portfolio(self):
        tbl = self.query_one("#port-tbl", DataTable)
        tbl.clear()
        tv = tp = 0.0
        for pos in self._portfolio:
            sym  = pos["symbol"]; sh = pos["shares"]; cost = pos["cost"]
            p, _ = self._price_cache.get(sym, (cost, cost))
            pl   = (p-cost)*sh; pct = ((p-cost)/cost*100) if cost else 0
            val  = p*sh; tv += val; tp += pl
            clr  = "green" if pl >= 0 else "red"
            tbl.add_row(sym, f"{sh:.2f}", self._fmt(cost), self._fmt(p),
                        f"[{clr}]{self._fmt(pl)}[/{clr}]",
                        f"[{clr}]{pct:+.2f}%[/{clr}]", self._fmt(val))
        clr_t = "green" if tp >= 0 else "red"
        self.query_one("#port-hdr").update(
            f"  Portfolio — Value: [bold]{self._fmt(tv)}[/bold]   "
            f"P&L: [{clr_t}][bold]{self._fmt(tp)}[/bold][/{clr_t}]   "
            f"[#6c7086]p = add position[/#6c7086]")

    def _draw_alerts(self):
        tbl = self.query_one("#alert-tbl", DataTable)
        tbl.clear()
        for a in self._alerts:
            s = "[green]✓ Triggered[/green]" if a.get("triggered") \
                else "[#6c7086]Watching[/#6c7086]"
            tbl.add_row(a["symbol"], f"{a['direction']} {self._fmt(a['price'])}", s)
        self.query_one("#alert-hdr").update(
            f"  Alerts   [#6c7086]n = add alert for current ticker[/#6c7086]")

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

    def on_tabbed_content_tab_activated(self, e: TabbedContent.TabActivated):
        try:    pid = e.pane.id
        except: pid = str(e.tab.id)
        sym = self._cur_sym
        if pid == "tab-options":
            threading.Thread(target=self._load_options, args=(sym,), daemon=True).start()
        elif pid == "tab-news":
            threading.Thread(target=self._load_news, args=(sym,), daemon=True).start()
        elif pid == "tab-portaler":
            self._draw_portfolio(); self._draw_alerts()

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

    def _switch_tf(self, tf: str):
        self._cur_tf = tf; self._draw_chart_ctrl()
        key = (self._cur_sym, tf)
        if key in self._hist_cache: self._draw_chart(*key)
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
            self._status(f"Alert set: {a['symbol']} {a['direction']} {a['price']}")
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
            self._status(f"Position saved: {pos['symbol']}")
        self.push_screen(PositionModal(self._cur_sym), done)

    def action_open_screener(self):
        def done(sym):
            if not sym: return
            self._cur_sym = sym
            threading.Thread(target=self._load_details, args=(sym,), daemon=True).start()
            threading.Thread(target=self._load_hist, args=(sym, self._cur_tf), daemon=True).start()
        self.push_screen(ScreenerScreen(), done)

    def _switch_wl(self, delta: int):
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

    def _status(self, msg: str):
        try: self.query_one("#status").update(f"[#585b70]{msg}[/#585b70]")
        except Exception: pass


def run():
    StockApp().run()

if __name__ == "__main__":
    run()
