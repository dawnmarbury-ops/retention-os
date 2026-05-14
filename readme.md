# 🚀 Retention OS v2.2

A disciplined, rules-based crypto portfolio operating system. No emotions. Just execution.

## Quick Start

1. Open `index.html` in any modern browser
2. Click "Fetch Prices" to load current market data
3. Enter your positions in "Edit Positions"
4. Log your first snapshot
5. Execute on the 5th of every month

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `R` | Refresh prices |
| `L` | Log snapshot |
| `E` | Edit positions |
| `1-6` | Switch tabs |
| `Esc` | Close modals |

## Auto-Refresh

Prices automatically refresh every 5 minutes when the tab is active.

## Data Storage

All data is stored locally in your browser's localStorage. Use Import/Export JSON to backup or transfer data.

## Architecture

- Single HTML file, zero dependencies (except CoinGecko API)
- ~1500 lines of vanilla JavaScript
- Responsive design, works on mobile
- Dark theme optimized for night trading

## Security

- XSS protection on all user inputs
- CSP headers prevent injection
- Input validation on import

## License

Personal use only. Not financial advice.
