# Shop Accounting

A local, offline shop management app: point of sale, inventory and sales reports. Built with Python, Flask and SQLite. The interface runs in the browser, no subscriptions or internet connection required.

## Features

- **POS:** barcode scanner support (works as a keyboard), receipt printing for 80 mm printers
- **Products and stock:** add and edit products, prices and stock levels
- **Goods receipt:** scan items, set quantity and purchase cost, create unknown products on the fly
- **Write-offs** with a reason
- **Sales reports:** revenue, receipts and profit for a period
- **Roles:** owner and cashier with separate passwords
- **Automatic backups** on a schedule to a local folder and an external drive, with rotation
- **Remote access** for the owner over a local network (Tailscale or Cloudflare Tunnel recommended for internet access)

## Tech stack

Python 3.10+, Flask, Waitress, SQLite (WAL mode), Jinja2. Money is stored as integer cents. Stock operations run in database transactions. Can be packaged into a single `.exe` with PyInstaller (`build_exe.bat`).

## Getting started (Windows)

1. Install Python 3.10+ and add it to PATH.
2. Run `install.bat`, then `start.bat`.
3. Open `http://localhost:5000/pos` (or use `kasa.bat` for a kiosk-style cashier window).
4. Change the default passwords in `config.ini` (owner: `admin`, cashier: `1111`).

Ukrainian documentation: [README.uk.md](README.uk.md)
