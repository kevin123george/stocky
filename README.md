# stocky

A professional stock market TUI for traders — built with [Textual](https://github.com/Textualize/textual).

![stocky demo](https://raw.githubusercontent.com/your-username/stocky/main/demo.png)

## Features

- Live price quotes with % change
- ASCII candlestick / line charts with multiple timeframes (1D → 5Y)
- Technical indicators: SMA, EMA, Bollinger Bands, RSI, MACD
- Options chain (nearest expiry)
- News feed
- Multi-currency support (USD, EUR, GBP, JPY, CAD)
- Multiple watchlists
- Portfolio tracker with live P&L
- Price alerts with macOS notifications
- S&P 500 screener
- Auto-refreshes every 3 seconds
- All data persisted locally in `~/.stocky/`

## Install

**Recommended — pipx (isolated, available everywhere):**

```sh
pipx install git+https://github.com/your-username/stocky.git
```

**Or with pip:**

```sh
pip install git+https://github.com/your-username/stocky.git
```

**Or clone and install locally:**

```sh
git clone https://github.com/your-username/stocky.git
cd stocky
pip install -e .
```

Then just run:

```sh
stocky
```

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `k` | Move up/down watchlist |
| `1`–`6` | Timeframe: 1D / 1W / 1M / 3M / 1Y / 5Y |
| `c` | Cycle currency (USD → EUR → GBP → JPY → CAD) |
| `t` | Toggle line / candlestick chart |
| `i` | Cycle indicator (SMA / EMA / BB / RSI / MACD) |
| `a` | Add ticker to watchlist |
| `d` | Delete ticker from watchlist |
| `[` / `]` | Switch watchlist |
| `n` | Add price alert for current ticker |
| `p` | Add / update portfolio position |
| `s` | Open S&P 500 screener |
| `r` | Force refresh |
| `q` | Quit |

## Data storage

All data is stored in `~/.stocky/` as plain JSON:

- `watchlists.json` — your watchlists
- `portfolio.json` — positions (symbol, shares, avg cost)
- `alerts.json` — price alerts

You can edit these files directly.

## Requirements

- Python 3.10+
- macOS / Linux (Windows untested)
- For macOS price alerts: no extra setup needed (uses `osascript`)

## License

MIT
