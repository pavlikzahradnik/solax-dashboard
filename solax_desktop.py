"""
Desktopovy spoustec SolaX dashboardu.

- Pohani Flask aplikaci z app.py pres waitress (stabilni server).
- Zobrazi ji ve vlastnim okne aplikace (pywebview), ne v prohlizeci.
- Spusteno pres pythonw.exe nebezi zadna konzole.

INSTALACE (jednou):
    python -m pip install pywebview waitress flask pymodbus

SPUSTENI:
    pythonw solax_desktop.py        (bez konzole - doporuceno)
    nebo:  python solax_desktop.py  (s konzoli, pro ladeni)

Tento soubor musi byt ve stejne slozce jako app.py (C:\\Solax).

Server bezi i na 0.0.0.0:5000, takze dashboard je zaroven dostupny
z mobilu/jineho PC na siti pres http://<IP-tohoto-PC>:5000
"""

import threading
import time
import webbrowser

from waitress import serve

from app import app   # importuje Flask aplikaci z app.py

HOST = "0.0.0.0"       # 0.0.0.0 = dostupne i z mobilu na siti
PORT = 5000


def run_server():
    serve(app, host=HOST, port=PORT, threads=4)


class Api:
    """Zpristupneno do okna jako pywebview.api - otevre odkaz v prohlizeci."""
    def open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return True


def main():
    # server bezi na pozadi jako daemon - skonci s oknem
    threading.Thread(target=run_server, daemon=True).start()

    # cache-buster: ?v=<cas> -> okno nikdy nenacte starou cache
    url = "http://127.0.0.1:5000/?v=" + str(int(time.time()))

    import webview
    webview.create_window(
        "SolaX Dashboard",
        url,
        width=1280,
        height=800,
        maximized=True,
        js_api=Api(),
    )
    webview.start()


if __name__ == "__main__":
    main()