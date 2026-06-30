from flask import Flask, jsonify, request
from pymodbus.client import ModbusTcpClient
import json
import os
import sys
import time
import threading
from datetime import date

app = Flask(__name__)

SOLAX_PORT = 502

# Slozka pro zapisovatelna data (config, history).
# Vzdy do uzivatelskeho profilu - %APPDATA%\SolaX\ (Windows) / ~/.solax (jinde),
# kam ma uzivatel pravo zapisu, at je .exe kdekoli (i v Program Files).
if sys.platform == "win32":
    _base = os.environ.get("APPDATA") or os.path.expanduser("~")
    DATA_DIR = os.path.join(_base, "SolaX")
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".solax")

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = os.path.abspath(".")   # nouzovy fallback

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

DEFAULT_CONFIG = {
    "solax_ip": "",          # prazdne = uzivatel zada pri prvnim spusteni
    "dongle_pwd": "",        # heslo donglu (serial) pro lokalni HTTP API
    "wallbox_ip": "",
    "wallbox_pwd": "",       # heslo donglu wallboxu (serial) pro HTTP API
    "wallbox2_ip": "",
    "wallbox2_pwd": "",
    "has_wallbox": False,
    "lang": "en",
}

# verze firmwaru se nemeni, tak je cteme jen jednou (cache)
_versions_cache = None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            if data.get("solax_ip"):
                cfg["solax_ip"] = data["solax_ip"]
            if "dongle_pwd" in data:
                cfg["dongle_pwd"] = str(data["dongle_pwd"])
            if "wallbox_ip" in data:
                cfg["wallbox_ip"] = data["wallbox_ip"]
            if "wallbox_pwd" in data:
                cfg["wallbox_pwd"] = str(data["wallbox_pwd"])
            if "wallbox2_ip" in data:
                cfg["wallbox2_ip"] = data["wallbox2_ip"]
            if "wallbox2_pwd" in data:
                cfg["wallbox2_pwd"] = str(data["wallbox2_pwd"])
            if "has_wallbox" in data:
                cfg["has_wallbox"] = bool(data["has_wallbox"])
            if data.get("lang") in ("cs", "en"):
                cfg["lang"] = data["lang"]
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)


def valid_ip(value):
    parts = str(value).strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        if not 0 <= int(p) <= 255:
            return False
    return True


# ====== HISTORIE (dnesni den, vzorek po minute) ======
# struktura: {"day": "2026-06-27", "points": [{"t": "HH:MM", "pv":, "load":, "soc":, "grid":}, ...]}
_history = {"day": str(date.today()), "points": []}
_history_lock = threading.Lock()

# ====== zive hodnoty ctene JEDNIM vlaknem (jediny ctenar Modbusu) ======
# Tim odpada soubeh spojeni na menic (dongle pousti typicky jen 1 spojeni).
_latest = {"error": "starting"}
_latest_lock = threading.Lock()


def get_latest():
    with _latest_lock:
        return dict(_latest)


def _poller_loop():
    global _latest
    while True:
        try:
            r = read_solax()
            with _latest_lock:
                _latest = r
        except Exception:
            pass
        time.sleep(5)


def _load_history():
    global _history
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        # nacti jen kdyz je to dnesni den, jinak zacni cisty
        if isinstance(data, dict) and data.get("day") == str(date.today()):
            _history = {"day": data["day"], "points": data.get("points", [])}
        else:
            _history = {"day": str(date.today()), "points": []}
    except Exception:
        _history = {"day": str(date.today()), "points": []}


def _save_history():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(_history, f)
    except Exception:
        pass


def record_history_point():
    """Zaznamena jeden bod z aktualnich (cache) dat. Vola se 1x za minutu."""
    data = get_latest()
    if data.get("error"):
        return

    today = str(date.today())

    with _history_lock:
        # reset o pulnoci (zmena dne)
        if _history["day"] != today:
            _history["day"] = today
            _history["points"] = []

        _history["points"].append({
            "t": time.strftime("%H:%M"),
            "pv": round(data["pv_total"] / 1000, 2),
            "load": round(data["load_power"] / 1000, 2),
            "soc": data["soc"],
            "grid": round(data["grid_power"] / 1000, 2),
        })
        _save_history()


def _history_loop():
    time.sleep(8)   # pockej, az poller poprve nacte data
    while True:
        try:
            record_history_point()
        except Exception:
            pass
        time.sleep(60)


def start_background():
    _load_history()
    # jediny ctenar Modbusu + zapis historie
    threading.Thread(target=_poller_loop, daemon=True).start()
    threading.Thread(target=_history_loop, daemon=True).start()


@app.after_request
def add_no_cache(resp):
    resp.headers["Cache-Control"] = \
        "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def int16(value):
    if value > 32767:
        return value - 65536
    return value


def _read_block(client, address, count):
    """Precte jeden blok registru. Jeden opakovany pokus pri chybe
    (starsi/pomalejsi dongly obcas odpovi az napodruhe)."""
    for attempt in range(2):
        try:
            r = client.read_input_registers(address=address, count=count)
            if r is not None and not r.isError():
                return r.registers
        except Exception:
            pass
        time.sleep(0.3)
    return None


def get_registers():

    cfg = load_config()

    solax_ip = (cfg.get("solax_ip") or "").strip()
    if not valid_ip(solax_ip):
        return None

    client = ModbusTcpClient(
        host=solax_ip,
        port=SOLAX_PORT,
        timeout=4        # delsi timeout - starsi dongly odpovidaji pomaleji
    )

    if not client.connect():
        return None

    try:
        # Cti po mensich blocich (po 50). Starsi firmware donglu nezvladne
        # velky blok 125 najednou a odpovi chybou; mensi bloky zvladnou i ony.
        # Pokryje registry 0-249 (5 bloku po 50) - stejne indexy jako predtim.
        regs = []
        for start in range(0, 250, 50):
            block = _read_block(client, start, 50)
            if block is None:
                return None      # hned konec, at se necteni netahne dlouho
            regs += block

        return regs

    except Exception:
        return None

    finally:
        client.close()


def _wallbox_offline():
    return {
        "online": False,
        "power": 0,
        "total_power": 0,
        "l1_power": 0,
        "l2_power": 0,
        "l3_power": 0
    }


def _decode_evcharger(j):
    """Dekoduje JSON odpoved z wallboxu (EV nabijecka, type 1) na hodnoty,
    ktere appka pouziva. Mapovani dle DataEvCharger.txt (overeno na realnych datech)."""
    D = j.get("Data") or []
    if len(D) < 12:
        return None

    def g(i):
        return D[i] if i < len(D) else 0

    return {
        "online": True,
        "power": g(11),          # Total_ChargePower (W) - celkovy vykon nabijeni
        "total_power": g(11),    # (appka zobrazuje jen power; drzime konzistentne)
        "l1_power": g(8),        # ChargePowerA
        "l2_power": g(9),        # ChargePowerB
        "l3_power": g(10),       # ChargePowerC
    }


def read_wallbox(ip, pwd=""):

    ip = (ip or "").strip()

    # prazdna nebo neplatna IP -> hned offline, zadne cekani
    if not valid_ip(ip):
        return _wallbox_offline()

    # cteni pres stejne lokalni HTTP API jako menic (port 80)
    j = _http_post_dongle(ip, pwd)
    if j is None:
        return _wallbox_offline()

    wb = _decode_evcharger(j)
    if wb is None:
        return _wallbox_offline()

    return wb


def fmt_version(v):
    # SolaX format: hodnota 27 -> "1.27"
    return "1." + str(v).zfill(2)


def get_versions():
    global _versions_cache
    if _versions_cache is not None:
        return _versions_cache

    cfg = load_config()
    client = ModbusTcpClient(host=cfg["solax_ip"], port=SOLAX_PORT)

    if not client.connect():
        return {"dsp": "?", "arm": "?"}

    try:
        # holding registry: 125=DSP, 131=ARM (overeno na realnem menici)
        rr = client.read_holding_registers(address=125, count=7)
        if rr is None or rr.isError():
            return {"dsp": "?", "arm": "?"}
        result = {
            "dsp": fmt_version(rr.registers[0]),   # reg 125
            "arm": fmt_version(rr.registers[6]),   # reg 131
        }
        _versions_cache = result   # uloz do cache jen pri uspechu
        return result
    except Exception:
        return {"dsp": "?", "arm": "?"}
    finally:
        client.close()


# ====== CTENI MENICE PRES LOKALNI HTTP API DONGLU (port 80) ======
# Stejny zpusob, jaky pouziva oficialni appka i Commander. Funguje i tam,
# kde Modbus na donglu zlobi. Posila POST optType=ReadRealTimeData&pwd=<heslo>
# a dongle vrati JSON s polem Data[] (cisla) + Information[].
# Mapovani je pro type 14 = X3-Hybrid G4 (overeno proti realnym datum z Commanderu).

def _s16(n):
    return n - 65536 if n >= 32768 else n


def _u32(a, b):
    return a + 65536 * b


def _s32(a, b):
    v = a + 65536 * b
    return v - 4294967296 if b >= 32768 else v


def _http_post_dongle(ip, pwd):
    """POST na dongle, vrati rozparsovany JSON nebo None.
    Zkusi zakladni dotaz; pri neuspechu jeste s hlavickou Host: 5.8.8.8
    (nektere firmwary donglu to pres LAN vyzaduji)."""
    import urllib.request
    import urllib.parse

    url = "http://" + ip + "/"
    body = urllib.parse.urlencode({
        "optType": "ReadRealTimeData",
        "pwd": pwd or "",
    }).encode()

    for extra in ({}, {"Host": "5.8.8.8"}):
        try:
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            for k, v in extra.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, timeout=5)
            text = resp.read().decode("utf-8", "replace")
            data = json.loads(text)
            if isinstance(data, dict) and "Data" in data:
                return data
        except Exception:
            pass
    return None


def _decode_type14(j):
    """Dekoduje JSON odpoved pro X3-Hybrid G4 (type 14) na pojmenovane hodnoty."""
    D = j.get("Data") or []
    if len(D) < 171:
        return None
    info = j.get("Information") or []

    def g(i):
        return D[i] if i < len(D) else 0

    out = {}
    # faze (sit)
    out["l1_voltage"] = g(0) / 10
    out["l2_voltage"] = g(1) / 10
    out["l3_voltage"] = g(2) / 10
    out["l1_current"] = _s16(g(3)) / 10
    out["l2_current"] = _s16(g(4)) / 10
    out["l3_current"] = _s16(g(5)) / 10
    out["l1_power"] = _s16(g(6))
    out["l2_power"] = _s16(g(7))
    out["l3_power"] = _s16(g(8))
    # stringy PV (Fasada / Pergola)
    out["pv1_power"] = g(14)
    out["pv2_power"] = g(15)
    out["pv_total"] = g(14) + g(15)
    # baterie  (POZOR: znamenko battery_power potvrdime vzorkem pri nabijeni/vybijeni)
    out["battery_power"] = _s16(g(41))
    out["soc"] = g(103)
    out["battery_voltage"] = _u32(g(169), g(170)) / 100
    # detailni info o baterii (overeno proti Commanderu)
    out["battery_temp"] = _s16(g(105))            # teplota baterie (C) - NE teplota stridace
    out["battery_capacity"] = g(106) / 10         # nabiti (kWh)
    out["cell_volt_max"] = g(125) / 1000          # napeti clanku max (V)
    out["cell_volt_min"] = g(126) / 1000          # napeti clanku min (V)
    out["cell_temp_max"] = g(112) / 10            # teplota clanku max (C)
    out["cell_temp_min"] = g(113) / 10            # teplota clanku min (C)
    out["bat_discharge_today"] = g(78) / 10       # vybijeni dnes (kWh)
    out["bat_charge_today"] = g(79) / 10          # nabijeni dnes (kWh)
    out["bat_discharge_total"] = _u32(g(74), g(75)) / 10   # vybijeni celkem (kWh)
    out["bat_charge_total"] = _u32(g(76), g(77)) / 10      # nabijeni celkem (kWh)
    # teplota stridace (index 54 = "Teplota"/chladic v Commanderu; v dokumentaci API neni)
    out["temperature"] = g(54)
    out["inner_temp"] = g(46)                        # vnitrni teplota (Commander "Vnitrni teplota")
    # sit (POZOR: znamenko potvrdime vzorkem; default + = dodavka/export do site)
    out["grid_power"] = _s32(g(34), g(35))
    # vystup stridace = soucet fazovych vykonu (pro vypocet spotreby)
    out["inverter_output"] = _s16(g(6)) + _s16(g(7)) + _s16(g(8))
    out["run_mode"] = g(19)                          # rezim mence (2 = Normal)
    # energie
    out["yield_today"] = g(70) / 10                  # vyroba dnes (kWh)
    out["feedin_total"] = _u32(g(86), g(87)) / 100   # pretok celkem (kWh)
    out["consume_total"] = _u32(g(88), g(89)) / 100  # ze site celkem (kWh)
    # verze firmwaru z Information (drzime stejne razeni jako mela Modbus verze: DSP, ARM)
    try:
        out["dsp_version"] = ("%.2f" % float(info[4]))
        out["arm_version"] = ("%.2f" % float(info[6]))
    except Exception:
        out["dsp_version"] = "?"
        out["arm_version"] = "?"
    return out


# ====== denni zaklad pro pretok/odber ======
# HTTP API dava pretok i odber jen jako CELKOVE soucty (ne "dnes").
# Dnesek dopocitame jako rozdil proti zakladu ulozenemu po pulnoci.
# (Vyroba dnes chodi z menice primo, ta zaklad nepotrebuje.)
_baseline = None
_baseline_lock = threading.Lock()


def _baseline_file():
    return os.path.join(DATA_DIR, "daily_baseline.json")


def _today_energy(feedin_total, consume_total):
    global _baseline
    today = str(date.today())
    with _baseline_lock:
        if _baseline is None:
            try:
                with open(_baseline_file()) as f:
                    _baseline = json.load(f)
            except Exception:
                _baseline = None
        b = _baseline
        # novy den / chybi zaklad / citac klesl (reset) -> nastav novy zaklad
        if (not isinstance(b, dict) or b.get("day") != today
                or feedin_total < b.get("feedin", 0)
                or consume_total < b.get("consume", 0)):
            b = {"day": today, "feedin": feedin_total, "consume": consume_total}
            _baseline = b
            try:
                with open(_baseline_file(), "w") as f:
                    json.dump(b, f)
            except Exception:
                pass
        export_today = max(0.0, feedin_total - b["feedin"])
        import_today = max(0.0, consume_total - b["consume"])
    return round(export_today, 2), round(import_today, 2)


def read_solax():

    cfg = load_config()
    solax_ip = (cfg.get("solax_ip") or "").strip()
    if not valid_ip(solax_ip):
        return {"error": "Cannot connect to inverter"}

    j = _http_post_dongle(solax_ip, cfg.get("dongle_pwd", ""))
    if j is None:
        return {"error": "Cannot connect to inverter"}

    # Appka umi jen X3-Hybrid G4 (type 14). Jiny model ma jine usporadani
    # pole Data[] -> radeji jasna hlaska nez tiche zobrazeni spatnych cisel.
    mtype = j.get("type")
    if mtype != 14:
        return {
            "error": "Unsupported inverter model",
            "error_code": "unsupported_model",
            "model_type": mtype
        }

    inv = _decode_type14(j)
    if inv is None:
        return {"error": "Cannot connect to inverter"}

    grid_power = inv["grid_power"]
    inverter_output = inv["inverter_output"]

    # spotreba = vystup stridace - export do site (grid_power: + = export)
    consumption = inverter_output - grid_power
    if consumption < 0:
        consumption = 0

    has_wallbox = cfg.get("has_wallbox", False)

    if has_wallbox:
        wb1 = read_wallbox(cfg["wallbox_ip"], cfg.get("wallbox_pwd", ""))
        wb2 = read_wallbox(cfg.get("wallbox2_ip", ""), cfg.get("wallbox2_pwd", ""))
    else:
        wb1 = _wallbox_offline()
        wb2 = _wallbox_offline()

    wb1_power = wb1["power"]
    wb2_power = wb2["power"]
    wallbox_total_power = wb1["total_power"] + wb2["total_power"]

    # Dum = spotreba bez (citelneho) wallboxu
    house_power = consumption - wb1_power - wb2_power
    if house_power < 0:
        house_power = 0

    export_today, import_today = _today_energy(
        inv["feedin_total"], inv["consume_total"])

    return {

        "pv_total": inv["pv_total"],
        "load_power": house_power,
        "total_load": consumption,

        "battery_power": inv["battery_power"],

        "soc": inv["soc"],

        "battery_voltage": inv["battery_voltage"],

        "battery_temp": inv["battery_temp"],
        "battery_capacity": inv["battery_capacity"],
        "cell_volt_max": inv["cell_volt_max"],
        "cell_volt_min": inv["cell_volt_min"],
        "cell_temp_max": inv["cell_temp_max"],
        "cell_temp_min": inv["cell_temp_min"],
        "bat_charge_today": inv["bat_charge_today"],
        "bat_discharge_today": inv["bat_discharge_today"],
        "bat_charge_total": inv["bat_charge_total"],
        "bat_discharge_total": inv["bat_discharge_total"],

        "temperature": inv["temperature"],
        "inner_temp": inv["inner_temp"],

        "run_mode": inv["run_mode"],

        "grid_power": grid_power,

        "pv1_power": inv["pv1_power"],
        "pv2_power": inv["pv2_power"],

        "l1_voltage": inv["l1_voltage"],
        "l2_voltage": inv["l2_voltage"],
        "l3_voltage": inv["l3_voltage"],

        "l1_current": inv["l1_current"],
        "l2_current": inv["l2_current"],
        "l3_current": inv["l3_current"],

        "l1_power": inv["l1_power"],
        "l2_power": inv["l2_power"],
        "l3_power": inv["l3_power"],

        "wb1_power": wb1_power,

        "wb1_online": wb1["online"],
        "wb2_power": wb2_power,
        "wb2_online": wb2["online"],

        "wb1_configured": has_wallbox,
        "wb2_configured": has_wallbox,

        "wallbox_total_power": wallbox_total_power,

        "wb1_l1_power": wb1["l1_power"],
        "wb1_l2_power": wb1["l2_power"],
        "wb1_l3_power": wb1["l3_power"],
        "wb2_l1_power": wb2["l1_power"],
        "wb2_l2_power": wb2["l2_power"],
        "wb2_l3_power": wb2["l3_power"],

        "output_today": inv["yield_today"],
        "export_today": export_today,
        "import_today": import_today,

        "dsp_version": inv["dsp_version"],
        "arm_version": inv["arm_version"]
    }


@app.route("/api/live")
def api_live():
    return jsonify(get_latest())


@app.route("/api/history")
def api_history():
    with _history_lock:
        return jsonify(dict(_history))


@app.route("/api/raw")
def api_raw():

    regs = get_registers()

    if regs is None:
        return jsonify({
            "error": "Cannot connect"
        })

    return jsonify(regs)


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    cfg = load_config()
    cfg["first_run"] = not os.path.exists(CONFIG_FILE)
    return jsonify(cfg)


@app.route("/api/settings", methods=["POST"])
def api_set_settings():

    data = request.get_json(silent=True) or {}

    solax_ip = str(data.get("solax_ip", "")).strip()
    dongle_pwd = str(data.get("dongle_pwd", "")).strip()
    wallbox_ip = str(data.get("wallbox_ip", "")).strip()
    wallbox_pwd = str(data.get("wallbox_pwd", "")).strip()
    wallbox2_ip = str(data.get("wallbox2_ip", "")).strip()
    wallbox2_pwd = str(data.get("wallbox2_pwd", "")).strip()
    has_wallbox = bool(data.get("has_wallbox", False))
    lang = data.get("lang") if data.get("lang") in ("cs", "en") else load_config()["lang"]

    # menic musi byt vzdy platny
    if not valid_ip(solax_ip):
        return jsonify({
            "ok": False,
            "code": "bad_inverter_ip",
            "error": "Neplatná IP adresa měniče"
        }), 400

    # wallbox IP validuj jen kdyz uzivatel zaskrtl "mam wallbox"
    if has_wallbox and not valid_ip(wallbox_ip):
        return jsonify({
            "ok": False,
            "code": "bad_wallbox_ip",
            "error": "Zadej platnou IP adresu wallboxu (nebo odškrtni 'Mám wallbox')"
        }), 400

    if wallbox2_ip and not valid_ip(wallbox2_ip):
        return jsonify({
            "ok": False,
            "code": "bad_wallbox2_ip",
            "error": "Zadej platnou IP adresu wallboxu 2"
        }), 400

    save_config({
        "solax_ip": solax_ip,
        "dongle_pwd": dongle_pwd,
        "wallbox_ip": wallbox_ip,
        "wallbox_pwd": wallbox_pwd,
        "wallbox2_ip": wallbox2_ip,
        "wallbox2_pwd": wallbox2_pwd,
        "has_wallbox": has_wallbox,
        "lang": lang
    })

    global _versions_cache
    _versions_cache = None   # IP se mohla zmenit, znovu nacti verze

    return jsonify({"ok": True})


@app.route("/")
def index():

    return """
<!DOCTYPE html>
<html>

<head>

<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>SolaX Energy Flow</title>

<style>

body{
    background:#0b1220;
    color:white;
    font-family:Segoe UI,Arial,sans-serif;
    margin:0;
    padding:14px;
}

h1{
    text-align:center;
    font-size:24px;
    margin:6px 0 4px;
}

.versions{
    text-align:center;
    color:#64748b;
    font-size:13px;
    margin-bottom:18px;
}

.signature{
    position:fixed;
    left:18px;
    bottom:16px;
    color:#475569;
    font-size:12px;
    z-index:1000;
}

.error-overlay{
    position:fixed;
    inset:0;
    display:none;
    align-items:center;
    justify-content:center;
    background:rgba(15,23,42,0.92);
    z-index:2000;
    text-align:center;
    padding:24px;
}

.error-overlay .box{
    max-width:480px;
}

.error-overlay .ico{
    font-size:48px;
    margin-bottom:16px;
}

.error-overlay .msg{
    font-size:20px;
    color:#f1f5f9;
    line-height:1.5;
}

.error-overlay .err-btn{
    margin-top:24px;
    padding:12px 24px;
    font-size:16px;
    color:#f1f5f9;
    background:#1e293b;
    border:1px solid #334155;
    border-radius:10px;
    cursor:pointer;
}

.error-overlay .err-btn:hover{
    background:#334155;
}

.flow{

    max-width:880px;
    margin:auto;

    display:grid;

    grid-template-columns:
        1fr 1fr 1fr;

    grid-template-rows:
        105px
        125px
        105px;

    gap:12px;
}

/* dlazdice vlevo | kriz (puvodni velikost) uprostred | prstence vpravo | graf pres celou sirku dole */
.layout{
    display:grid;
    grid-template-columns: minmax(320px,420px) 1fr minmax(320px,420px);
    grid-template-areas:
        "stats cross donuts"
        "graph graph graph";
    gap:14px;
    align-items:start;
    max-width:1700px;
    margin:0 auto;
    padding:0 16px 16px;
    box-sizing:border-box;
}

.layout .stats     { grid-area:stats;  max-width:none; margin:0;
                     grid-template-columns:1fr 1fr; }
.layout .flow      { grid-area:cross; max-width:none; width:100%;
                     margin:0; }   /* roztahnout pres cely stredni sloupec */
.layout .energy-row{ grid-area:donuts; max-width:none; margin:0;
                     align-self:stretch;            /* roztahnout na vysku radku */
                     display:flex; flex-direction:column; gap:14px; }
.layout .energy-row .energy-card{ flex:1; }   /* oba prstence rovnomerne vypln vysku */
.layout .chart-card{ grid-area:graph;  max-width:none; margin:0; }

/* na uzkem displeji (mobil) slozit vse pod sebe a normalne skrolovat */
@media (max-width:1000px){
    .layout{
        display:block;
        padding:0;
    }
    .layout .flow,
    .layout .energy-row,
    .layout .chart-card,
    .layout .stats{ margin:16px auto; max-width:1100px; }
    .layout .stats{ grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
    .layout .energy-row{ grid-template-columns:1fr 1fr; }
}

.node{

    background:#182232;

    border-radius:16px;

    display:flex;

    flex-direction:column;

    justify-content:center;

    align-items:center;

    box-shadow:
        0 0 20px rgba(0,0,0,.4);
}

.solar{
    grid-column:2;
    grid-row:1;
}

.battery{
    grid-column:1;
    grid-row:2;
}

.inverter{
    grid-column:2;
    grid-row:2;

    background:#243247;
}

.gridnode{
    grid-column:3;
    grid-row:2;
}

.house{
    grid-column:2;
    grid-row:3;
}

.wallbox{
}

#wb1_node{
    grid-column:3;
    grid-row:3;
}

#wb2_node{
    grid-column:1;
    grid-row:3;
}

.icon{
    font-size:30px;
}

.value{
    font-size:22px;
    font-weight:bold;
}

.label{
    color:#9ca3af;
    font-size:13px;
    margin-top:6px;
}

.label.small{
    font-size:12px;
    opacity:0.8;
    margin-top:4px;
}

.label{
    color:#9ca3af;
    font-size:13px;
    margin-top:6px;
}

.stats{

    max-width:1100px;

    margin:20px auto;

    display:grid;

    grid-template-columns:
        repeat(auto-fit,minmax(190px,1fr));

    gap:12px;
}

.stat{

    background:#182232;

    border-radius:12px;

    padding:14px;
}

.stat-title{
    color:#9ca3af;
    font-size:13px;
}

.stat-value{
    margin-top:6px;
    font-size:17px;
    font-weight:bold;
    white-space:nowrap;
}

.fab-group{
    position:fixed;
    right:24px;
    bottom:18px;
    z-index:1000;

    display:flex;
    flex-direction:column;
    align-items:center;
    gap:14px;
}

.fab{
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:6px;
    cursor:pointer;
}

.fab-icon{
    width:60px;
    height:60px;
    border-radius:50%;

    background:#243247;
    box-shadow:0 4px 16px rgba(0,0,0,.5);

    display:flex;
    align-items:center;
    justify-content:center;

    font-size:28px;
}

.fab:hover .fab-icon{
    background:#2f4361;
}

.fab-label{
    font-size:12px;
    color:#94a3b8;
}

.settings-form{
    max-width:460px;
    margin:0 auto;
    background:#182232;
    border-radius:16px;
    padding:28px;
}

.about-card{
    max-width:460px;
    margin:0 auto;
    background:#182232;
    border-radius:16px;
    padding:28px;
    color:#cbd5e1;
    line-height:1.6;
}

.about-card .about-title{
    font-size:22px;
    font-weight:700;
    color:#f1f5f9;
    margin-bottom:16px;
}

.battery-stats{
    display:grid;
    grid-template-columns:repeat(3,minmax(220px,1fr));
    gap:14px;
    justify-content:center;
    max-width:calc(3 * 240px + 28px);
    margin:0 auto 24px;
}

#battery_info{
    display:none;
    box-sizing:border-box;
    padding:24px 16px 40px;
}

#battery_info h1{
    max-width:860px;
    width:100%;
    margin:0 auto 24px;
    text-align:center;
}

#battery_info > .stats{
    width:100%;
}

@media (max-width:900px){
    .battery-stats{
        grid-template-columns:repeat(2,minmax(220px,1fr));
        max-width:calc(2 * 240px + 14px);
    }
}

@media (max-width:680px){
    .battery-stats{
        grid-template-columns:1fr;
        max-width:100%;
    }
}

#battery_info > .back-tile{
    margin:0 auto;
    max-width:220px;
}

.about-card p{
    margin:0 0 14px;
}

.about-card .about-author{
    color:#64748b;
    font-size:14px;
    margin-top:20px;
}

.coffee-btn{
    margin-top:8px;
    width:100%;
    background:#FFDD00;
    color:#0b1220;
    border:none;
    border-radius:10px;
    font-size:17px;
    font-weight:700;
    padding:14px;
    cursor:pointer;
}

.coffee-btn:hover{
    background:#ffe533;
}

.coffee-note{
    text-align:center;
    color:#64748b;
    font-size:12px;
    margin-top:8px;
}

.settings-form{
    max-width:860px;
    width:100%;
    margin:0 auto;
}

.settings-form label{
    display:block;
    color:#9ca3af;
    font-size:14px;
    margin:18px 0 6px;
}

.settings-form .group-heading{
    margin:24px 0 8px;
    color:#cbd5e1;
    font-size:15px;
    font-weight:700;
}

.settings-form input{
    width:100%;
    box-sizing:border-box;
    background:#0b1220;
    border:1px solid #2a3850;
    border-radius:10px;
    color:white;
    font-size:18px;
    padding:12px;
}

.lang-row{
    display:flex;
    gap:10px;
    margin-bottom:6px;
}
.settings-form .wallbox-row{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:14px;
    margin-top:10px;
}
.settings-form .wallbox-col{
    padding:12px;
    background:#0b1220;
    border:1px solid #2a3850;
    border-radius:16px;
}
@media (max-width:860px){
    .settings-form .wallbox-row{
        grid-template-columns:1fr;
    }
}
.welcome-note{
    background:#1e3a5f;
    border:1px solid #2563eb;
    border-radius:10px;
    padding:12px 14px;
    color:#dbeafe;
    font-size:14px;
    line-height:1.4;
    margin-bottom:6px;
}
.net-note{
    color:#94a3b8;
    font-size:13px;
    line-height:1.45;
    margin-top:4px;
}
.lang-btn{
    flex:1;
    padding:10px;
    background:#0b1220;
    border:1px solid #2a3850;
    border-radius:10px;
    color:#cbd5e1;
    font-size:15px;
    cursor:pointer;
}
.lang-btn.active{
    background:#2563eb;
    border-color:#2563eb;
    color:white;
    font-weight:600;
}

.settings-form .check-row{
    display:flex;
    align-items:center;
    gap:10px;
    color:#e2e8f0;
    font-size:16px;
    margin:22px 0 6px;
    cursor:pointer;
}

.settings-form .check-row input{
    width:18px;
    height:18px;
    cursor:pointer;
}

.settings-form input:disabled{
    opacity:0.4;
    cursor:not-allowed;
}

.settings-form label.dim{
    opacity:0.4;
}

.settings-form .hint{
    font-size:12px;
    color:#94a3b8;
    margin:6px 0 14px;
    line-height:1.4;
}

.settings-form button{
    margin-top:24px;
    width:100%;
    background:#2563eb;
    color:white;
    border:none;
    border-radius:10px;
    font-size:16px;
    padding:14px;
    cursor:pointer;
}

.settings-form button:hover{
    background:#1d4ed8;
}

#save_msg{
    text-align:center;
    margin-top:14px;
    min-height:20px;
    font-size:15px;
}

.back-tile{
    margin:22px auto 0;
    max-width:220px;
    background:#243247;
    border-radius:12px;
    padding:14px;
    text-align:center;
    cursor:pointer;
    color:#9ca3af;
    font-size:15px;
}

.back-tile:hover{
    color:white;
}

.energy-row{
    max-width:1100px;
    margin:32px auto 20px;
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
}

.chart-card{
    max-width:1100px;
    margin:20px auto;
    background:#182232;
    border-radius:12px;
    padding:20px;
}

.chart-head{
    display:flex;
    justify-content:space-between;
    align-items:center;
    flex-wrap:wrap;
    gap:10px;
    margin-bottom:14px;
}

.chart-title{
    font-size:18px;
    font-weight:600;
}

.chart-legend{
    display:flex;
    gap:16px;
    flex-wrap:wrap;
    font-size:13px;
    color:#cbd5e1;
}

.chart-legend .lg{
    display:flex;
    align-items:center;
    gap:6px;
    cursor:pointer;
    user-select:none;
    transition:opacity .15s;
}

.chart-legend .lg.off{
    opacity:0.35;
}

.chart-legend i{
    width:12px;
    height:3px;
    border-radius:2px;
    display:inline-block;
}

#chart_vline{
    position:absolute;
    top:0;
    bottom:0;
    width:1px;
    background:#64748b;
    display:none;
    pointer-events:none;
}

#chart_dots{
    position:absolute;
    inset:0;
    pointer-events:none;
}

#chart_dots .dot{
    position:absolute;
    width:9px;
    height:9px;
    border-radius:50%;
    transform:translate(-50%,-50%);
    border:2px solid #0b1220;
}

#chart_tip{
    position:absolute;
    display:none;
    pointer-events:none;
    background:#0f172a;
    border:1px solid #334155;
    border-radius:8px;
    padding:8px 10px;
    font-size:12px;
    color:#e2e8f0;
    white-space:nowrap;
    z-index:5;
    box-shadow:0 4px 14px rgba(0,0,0,.5);
}

#chart_tip .tt{
    font-weight:600;
    margin-bottom:4px;
    color:#94a3b8;
}

#chart_tip .row{
    display:flex;
    align-items:center;
    gap:6px;
    margin-top:2px;
}

#chart_tip .row i{
    width:10px;
    height:10px;
    border-radius:2px;
    display:inline-block;
}

#chart_wrap{
    position:relative;
    width:100%;
    height:320px;
    background:#0b1220;
    border-radius:10px;
    overflow:hidden;
}

.chart-row{
    display:flex;
    align-items:flex-start;
}

.chart-mid{
    flex:1;
    min-width:0;
}

.yaxis{
    position:relative;
    width:52px;
    height:320px;
    flex-shrink:0;
}

.yaxis .ytick{
    position:absolute;
    right:8px;
    transform:translateY(-50%);
    font-size:11px;
    color:#64748b;
    white-space:nowrap;
}

.yaxis.yright .ytick{
    right:auto;
    left:8px;
}

#chart{
    width:100%;
    height:320px;
    display:block;
    background:transparent;
}

.chart-empty{
    position:absolute;
    inset:0;
    display:flex;
    align-items:center;
    justify-content:center;
    color:#64748b;
    font-size:14px;
    text-align:center;
    padding:0 20px;
}

.chart-axis{
    display:flex;
    justify-content:space-between;
    color:#64748b;
    font-size:11px;
    margin-top:6px;
}

.energy-card{
    background:#182232;
    border-radius:12px;
    padding:18px;
    display:flex;
    align-items:center;
    gap:26px;
}

.donut-wrap{
    position:relative;
    width:120px;
    height:120px;
    flex-shrink:0;
}

.donut-wrap svg{
    width:120px;
    height:120px;
}

.donut-bg{
    fill:none;
    stroke:#0b1220;
    stroke-width:10;
}

.donut-seg{
    fill:none;
    stroke-width:10;
    stroke-linecap:round;
    transition:stroke-dasharray .4s ease;
}

.donut-center{
    position:absolute;
    top:0;
    left:0;
    width:100%;
    height:100%;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
}

.donut-total{
    font-size:24px;
    font-weight:bold;
}

.donut-unit{
    color:#9ca3af;
    font-size:12px;
}

.energy-legend{
    flex:1;
}

.energy-title{
    font-weight:bold;
    margin-bottom:14px;
}

.leg-row{
    display:flex;
    align-items:center;
    gap:7px;
    margin-top:10px;
    color:#cbd5e1;
    font-size:13px;
    white-space:nowrap;
}

.leg-row span,
.leg-row b{
    white-space:nowrap;
}

.leg-row .dot{
    width:11px;
    height:11px;
    border-radius:3px;
    flex-shrink:0;
}

.leg-row b{
    color:white;
}

.leg-row .pct{
    color:#9ca3af;
    font-size:13px;
}

@media (max-width:700px){
    .energy-row{ grid-template-columns:1fr; }
}
</style>

</head>

<body>

<div id="main">

<h1>⚡ SolaX X3 Hybrid G4</h1>

<div class="versions" id="versions">DSP -- · ARM --</div>

<div class="layout">

<div class="flow">

    <div class="node solar">

        <div class="icon">☀️</div>

        <div
            class="value"
            id="pv">
            --
        </div>

        <div class="label" data-i18n="pv_prod">
            FV výroba
        </div>

    </div>

    <div class="node battery" onclick="showBattery()" style="cursor:pointer" title="Detail baterie">

        <div class="icon">🔋</div>

        <div
            class="value"
            id="battery">
            --
        </div>

        <div
            class="label"
            id="soc">
            --
        </div>

    </div>

    <div class="node inverter">

        <div class="icon">⚡</div>

        <div class="value">
            X3 Hybrid
        </div>

        <div class="label" id="inv_mode">
            SolaX
        </div>

    </div>

    <div class="node gridnode">

        <div class="icon">🌍</div>

        <div
            class="value"
            id="grid">
            --
        </div>

        <div class="label" id="grid_label">
            Síť
        </div>

    </div>

    <div class="node house">

        <div class="icon">🏠</div>

        <div
            class="value"
            id="load">
            --
        </div>

        <div class="label" data-i18n="house">
            Dům
        </div>

    </div>

    <div class="node wallbox" id="wb1_node">

        <div class="icon">🚗</div>

        <div
            class="value"
            id="wb1flow">
            --
        </div>

        <div class="label">
            Wallbox 1
        </div>

    </div>

    <div class="node wallbox" id="wb2_node">

        <div class="icon">🚗</div>

        <div
            class="value"
            id="wb2flow">
            --
        </div>

        <div class="label">
            Wallbox 2
        </div>

    </div>

</div>

<div class="energy-row">

    <div class="energy-card">
        <div class="donut-wrap">
            <svg viewBox="0 0 100 100">
                <circle class="donut-bg" cx="50" cy="50" r="42"></circle>
                <circle class="donut-seg" id="out_seg_home" cx="50" cy="50" r="42" stroke="#5eead4"></circle>
                <circle class="donut-seg" id="out_seg_grid" cx="50" cy="50" r="42" stroke="#14b8a6"></circle>
            </svg>
            <div class="donut-center">
                <div class="donut-total" id="out_total">--</div>
                <div class="donut-unit">kWh</div>
            </div>
        </div>
        <div class="energy-legend">
            <div class="energy-title" data-i18n="inv_output_h">📤 Výroba měniče</div>
            <div class="leg-row">
                <span class="dot" style="background:#5eead4"></span>
                <span data-i18n="to_home">Do domu</span> <b id="out_home_val">--</b>
                <span class="pct" id="out_home_pct"></span>
            </div>
            <div class="leg-row">
                <span class="dot" style="background:#14b8a6"></span>
                <span data-i18n="to_grid">Do sítě</span> <b id="out_grid_val">--</b>
                <span class="pct" id="out_grid_pct"></span>
            </div>
        </div>
    </div>

    <div class="energy-card">
        <div class="donut-wrap">
            <svg viewBox="0 0 100 100">
                <circle class="donut-bg" cx="50" cy="50" r="42"></circle>
                <circle class="donut-seg" id="con_seg_sys" cx="50" cy="50" r="42" stroke="#fbbf24"></circle>
                <circle class="donut-seg" id="con_seg_grid" cx="50" cy="50" r="42" stroke="#f97316"></circle>
            </svg>
            <div class="donut-center">
                <div class="donut-total" id="con_total">--</div>
                <div class="donut-unit">kWh</div>
            </div>
        </div>
        <div class="energy-legend">
            <div class="energy-title" data-i18n="consumption_h">🏠 Spotřeba</div>
            <div class="leg-row">
                <span class="dot" style="background:#fbbf24"></span>
                <span data-i18n="from_system">Ze systému</span> <b id="con_sys_val">--</b>
                <span class="pct" id="con_sys_pct"></span>
            </div>
            <div class="leg-row">
                <span class="dot" style="background:#f97316"></span>
                <span data-i18n="from_grid">Ze sítě</span> <b id="con_grid_val">--</b>
                <span class="pct" id="con_grid_pct"></span>
            </div>
        </div>
    </div>

</div>

<div class="chart-card">
    <div class="chart-head">
        <div class="chart-title" data-i18n="daily_trend_h">📈 Průběh dne</div>
        <div class="chart-legend">
            <span class="lg" data-k="pv"   onclick="toggleSeries('pv')"><i style="background:#fbbf24"></i><span data-i18n="production">Výroba</span></span>
            <span class="lg" data-k="load" onclick="toggleSeries('load')"><i style="background:#f97316"></i><span data-i18n="consumption">Spotřeba</span></span>
            <span class="lg" data-k="grid" onclick="toggleSeries('grid')"><i style="background:#22d3ee"></i><span data-i18n="grid">Síť</span></span>
            <span class="lg" data-k="soc"  onclick="toggleSeries('soc')"><i style="background:#a78bfa"></i><span data-i18n="soc">SOC %</span></span>
        </div>
    </div>
    <div class="chart-row">
        <div class="yaxis" id="yaxis_left"></div>
        <div class="chart-mid">
            <div id="chart_wrap">
                <svg id="chart" viewBox="0 0 1000 320"
                     preserveAspectRatio="none"></svg>
                <div id="chart_vline"></div>
                <div id="chart_dots"></div>
                <div id="chart_tip"></div>
                <div id="chart_empty" class="chart-empty" data-i18n="collecting">
                    Zatím se sbírají data — graf se objeví po pár minutách provozu.
                </div>
            </div>
            <div class="chart-axis" id="chart_axis"></div>
        </div>
        <div class="yaxis yright" id="yaxis_right"></div>
    </div>
</div>

<div class="stats">

    <div class="stat">
        <div class="stat-title">PV1</div>
        <div class="stat-value" id="pv1">--</div>
    </div>

    <div class="stat">
        <div class="stat-title">PV2</div>
        <div class="stat-value" id="pv2">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="l1_v">L1 napětí</div>
        <div class="stat-value" id="l1v">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="l1_p">L1 proud / Výkon</div>
        <div class="stat-value" id="l1info">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="l2_v">L2 napětí</div>
        <div class="stat-value" id="l2v">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="l2_p">L2 proud / Výkon</div>
        <div class="stat-value" id="l2info">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="l3_v">L3 napětí</div>
        <div class="stat-value" id="l3v">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="l3_p">L3 proud / Výkon</div>
        <div class="stat-value" id="l3info">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="inv_temp">Teplota chladiče</div>
        <div class="stat-value" id="temp">--</div>
    </div>

    <div class="stat">
        <div class="stat-title" data-i18n="inner_temp">Vnitřní teplota</div>
        <div class="stat-value" id="innertemp">--</div>
    </div>

</div>

</div><!-- /layout -->

<div class="fab-group" id="fab_group">
    <div class="fab" onclick="showBattery()" title="Baterie">
        <div class="fab-icon">🔋</div>
        <div class="fab-label" data-i18n="battery_info_title">Baterie</div>
    </div>
    <div class="fab" onclick="showSettings()" title="Nastavení">
        <div class="fab-icon">⚙️</div>
        <div class="fab-label" data-i18n="settings">Nastavení</div>
    </div>
    <div class="fab" onclick="showAbout()" title="O programu">
        <div class="fab-icon">ℹ️</div>
        <div class="fab-label" data-i18n="about">O programu</div>
    </div>
</div>

</div><!-- /main -->

<div id="battery_info" style="display:none">

    <h1 data-i18n="battery_info_h">Informace o baterii</h1>

    <div class="stats battery-stats">
        <div class="stat">
            <div class="stat-title" data-i18n="battery_power">Výkon baterie</div>
            <div class="stat-value" id="battery_info_power">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="battery_soc">SOC</div>
            <div class="stat-value" id="battery_info_soc">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="battery_capacity">Nabití</div>
            <div class="stat-value" id="battery_info_capacity">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="battery_voltage">Napětí baterie</div>
            <div class="stat-value" id="battery_info_voltage">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="battery_temp">Teplota baterie</div>
            <div class="stat-value" id="battery_info_temp">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="cell_voltage">Napětí článků (min–max)</div>
            <div class="stat-value" id="battery_info_cellv">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="cell_temp">Teplota článků (min–max)</div>
            <div class="stat-value" id="battery_info_cellt">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="bat_charge">Nabíjení (dnes / celkem)</div>
            <div class="stat-value" id="battery_info_charge">--</div>
        </div>
        <div class="stat">
            <div class="stat-title" data-i18n="bat_discharge">Vybíjení (dnes / celkem)</div>
            <div class="stat-value" id="battery_info_discharge">--</div>
        </div>
    </div>

    <div class="back-tile" onclick="showMain()" data-i18n="back">← Zpět na přehled</div>

</div>

<div id="settings" style="display:none">

    <h1 data-i18n="settings_h">⚙️ Nastavení</h1>

    <div class="settings-form">

        <div id="welcome_note" class="welcome-note" style="display:none" data-i18n="welcome">
            Vítejte! Pro začátek zadejte IP adresu svého měniče a uložte.
        </div>

        <label data-i18n="lang_label">Jazyk</label>
        <div class="lang-row">
            <button type="button" id="lang_cs" class="lang-btn" onclick="setLang('cs')">CZE</button>
            <button type="button" id="lang_en" class="lang-btn" onclick="setLang('en')">ENG</button>
        </div>

        <label data-i18n="inv_ip">IP adresa měniče</label>
        <input id="cfg_solax" type="text" placeholder="192.168.1.148">

        <label data-i18n="dongle_pwd">Heslo donglu (sériové číslo)</label>
        <input id="cfg_dongle_pwd" type="text" placeholder="SWxxxxxxxx">
        <div class="hint" data-i18n="dongle_pwd_hint">Najdete na štítku donglu nebo v aplikaci SolaX. Bez něj měnič data nevydá.</div>

        <label class="check-row">
            <input id="cfg_has_wallbox" type="checkbox"
                   onchange="toggleWallbox()">
            <span data-i18n="have_wb">Mám wallbox</span>
        </label>

        <div class="wallbox-row">
            <div class="wallbox-col">
                <div class="group-heading" data-i18n="wb_section1">Wallbox 1</div>
                <label id="cfg_wallbox_label" data-i18n="wb_ip">IP adresa wallboxu</label>
                <input id="cfg_wallbox" type="text" placeholder="192.168.1.198">
                <label id="cfg_wallbox_pwd_label" data-i18n="wb_pwd">Heslo donglu wallboxu</label>
                <input id="cfg_wallbox_pwd" type="text" placeholder="SCxxxxxxxx">
            </div>
            <div class="wallbox-col">
                <div class="group-heading" data-i18n="wb_section2">Wallbox 2</div>
                <label id="cfg_wallbox2_label" data-i18n="wb2_ip">IP adresa wallboxu 2</label>
                <input id="cfg_wallbox2" type="text" placeholder="192.168.1.199">
                <label id="cfg_wallbox2_pwd_label" data-i18n="wb2_pwd">Heslo donglu wallboxu 2</label>
                <input id="cfg_wallbox2_pwd" type="text" placeholder="SCxxxxxxxx">
                <div class="hint" data-i18n="wb2_note">Pokud nemáte druhý wallbox, nechte pole prázdná.</div>
            </div>
        </div>

        <button onclick="saveSettings()" data-i18n="save">Uložit</button>

        <div id="save_msg"></div>

        <div class="net-note" data-i18n="net_note">
            Vyčítání dat a graf fungují jen tehdy, když aplikace běží ve stejné síti jako měnič.
        </div>

        <div class="back-tile" onclick="showMain()" data-i18n="back">
            ← Zpět na přehled
        </div>

    </div>

</div>

<div id="about" style="display:none">

    <h1 data-i18n="about_h">ℹ️ O programu</h1>

    <div class="about-card">

        <div class="about-title">SolaX Dashboard</div>

        <p data-i18n="about_p1">Lokální přehled fotovoltaické elektrárny SolaX X3 Hybrid G4.
        Zobrazuje živá data z měniče (a volitelně z wallboxu) čtená po
        místní síti přes Modbus TCP.</p>

        <p data-i18n="about_p2">Data se čtou přímo z vašeho zařízení v síti a nikam se
        neodesílají.</p>

        <button class="coffee-btn" onclick="openSupport()" data-i18n="buy_kwh">
            ⚡ Kup mi kWh
        </button>
        <div class="coffee-note" data-i18n="support_note">Podpoř další vývoj — díky!</div>

        <p class="about-author">Created by Pavel Zahradník</p>

        <div class="back-tile" onclick="showMain()" data-i18n="back">
            ← Zpět na přehled
        </div>

    </div>

</div>

<div class="signature">Created by Pavel Zahradník</div>

<div class="error-overlay" id="error_overlay">
    <div class="box">
        <div class="ico">⚠️</div>
        <div class="msg" data-i18n="err_read">Chyba čtení z měniče — zkontroluj IP adresu</div>
        <button class="err-btn" onclick="showSettings()" data-i18n="open_settings">⚙️ Otevřít nastavení</button>
    </div>
</div>

<script>

// ====== PODPORA VYVOJE ======
// Odkaz vede na vlastni Cloudflare Worker, ktery presmeruje na BMaC.
// Cil presmerovani se meni v kodu Workeru, ne tady -> v aplikaci neni
// primy BMaC odkaz a da se kdykoliv zmenit bez nove verze appky.
const SUPPORT_URL = "https://solax-podpora.pavlikzahradnik.workers.dev/";
// =============================

function openExternal(url){
    // v pywebview okne otevri v systemovem prohlizeci,
    // v beznem prohlizeci otevri novy panel
    if(window.pywebview && window.pywebview.api && window.pywebview.api.open_url){
        window.pywebview.api.open_url(url);
    } else {
        window.open(url, "_blank");
    }
}

function openSupport(){
    if(!SUPPORT_URL){
        alert("Odkaz na podporu zatím není nastavený.");
        return;
    }
    openExternal(SUPPORT_URL);
}

// ====== JAZYKY ======
const I18N = {
  cs: {
    pv_prod:"FV výroba", house:"Dům",
    grid_export:"Síť — dodávka", grid_import:"Síť — odběr", grid:"Síť",
    inv_output_h:"📤 Výroba měniče", to_home:"Do domu", to_grid:"Do sítě",
    consumption_h:"🏠 Spotřeba", from_system:"Ze systému", from_grid:"Ze sítě",
    daily_trend_h:"📈 Průběh dne", production:"Výroba", consumption:"Spotřeba", soc:"SOC %",
    collecting:"Zatím se sbírají data — graf se objeví po pár minutách provozu.",
    l1_v:"L1 napětí", l2_v:"L2 napětí", l3_v:"L3 napětí",
    batt_v:"Napětí baterie", inv_temp:"Teplota chladiče", inner_temp:"Vnitřní teplota",
    l1_p:"L1 proud / Výkon", l2_p:"L2 proud / Výkon", l3_p:"L3 proud / Výkon",
    about:"O programu", settings:"Nastavení",
    settings_h:"⚙️ Nastavení", about_h:"ℹ️ O programu",
    lang_label:"Jazyk", inv_ip:"IP adresa měniče", have_wb:"Mám wallbox",
    dongle_pwd:"Heslo donglu (sériové číslo)",
    dongle_pwd_hint:"Najdete na štítku donglu nebo v aplikaci SolaX. Bez něj měnič data nevydá.",
    wb_ip:"IP adresa wallboxu", wb2_ip:"IP adresa wallboxu 2", save:"Uložit", back:"← Zpět na přehled",
    wb_pwd:"Heslo donglu wallboxu", wb2_pwd:"Heslo donglu wallboxu 2",
    wb_section1:"Wallbox 1", wb_section2:"Wallbox 2",
    wb2_note:"Pokud nemáte druhý wallbox, nechte pole prázdná.",
    battery_info_title:"Baterie", battery_info_h:"Informace o baterii",
    battery_power:"Výkon baterie", battery_soc:"SOC", battery_voltage:"Napětí baterie", battery_temp:"Teplota baterie",
    battery_capacity:"Nabití", cell_voltage:"Napětí článků (min–max)", cell_temp:"Teplota článků (min–max)",
    bat_charge:"Nabíjení (dnes / celkem)", bat_discharge:"Vybíjení (dnes / celkem)",
    run_modes:{0:"Čekání",1:"Kontrola",2:"Normální",3:"Porucha",4:"Trvalá porucha",5:"Aktualizace",6:"Kontrola EPS",7:"EPS režim",8:"Autotest",9:"Nečinný",10:"Pohotovost"},
    buy_kwh:"⚡ Kup mi kWh", support_note:"Podpoř další vývoj — díky!",
    about_p1:"Lokální přehled fotovoltaické elektrárny SolaX X3 Hybrid G4. Zobrazuje živá data z měniče (a volitelně z wallboxu) čtená po místní síti přes lokální HTTP API donglu.",
    about_p2:"Data se čtou přímo z vašeho zařízení v síti a nikam se neodesílají.",
    err_read:"Chyba čtení z měniče — zkontroluj IP adresu", open_settings:"⚙️ Otevřít nastavení",
    unsupported_model:"Tento měnič (typ {type}) zatím není podporován. Aplikace je určena pro SolaX X3 Hybrid G4.",
    saved_ok:"Uloženo ✓ (projeví se do 5 s)", conn_err:"Chyba spojení",
    bad_inverter_ip:"Neplatná IP adresa měniče",
    bad_wallbox_ip:"Zadej platnou IP adresu wallboxu (nebo odškrtni „Mám wallbox“)",
    welcome:"Vítejte! Pro začátek zadejte IP adresu měniče a heslo donglu (sériové číslo), pak uložte.",
    net_note:"Vyčítání dat a graf fungují jen tehdy, když aplikace běží ve stejné síti jako měnič."
  },
  en: {
    pv_prod:"PV production", house:"House",
    grid_export:"Grid — export", grid_import:"Grid — import", grid:"Grid",
    inv_output_h:"📤 Inverter output", to_home:"To home", to_grid:"To grid",
    consumption_h:"🏠 Consumption", from_system:"From system", from_grid:"From grid",
    daily_trend_h:"📈 Daily trend", production:"Production", consumption:"Consumption", soc:"SOC %",
    collecting:"Collecting data — the chart will appear after a few minutes.",
    l1_v:"L1 voltage", l2_v:"L2 voltage", l3_v:"L3 voltage",
    batt_v:"Battery voltage", inv_temp:"Heatsink temp.", inner_temp:"Inner temp.",
    l1_p:"L1 current / Power", l2_p:"L2 current / Power", l3_p:"L3 current / Power",
    about:"About", settings:"Settings",
    settings_h:"⚙️ Settings", about_h:"ℹ️ About",
    lang_label:"Language", inv_ip:"Inverter IP address", have_wb:"I have a wallbox",
    dongle_pwd:"Dongle password (serial number)",
    dongle_pwd_hint:"Found on the dongle label or in the SolaX app. Without it the inverter won't return data.",
    wb_ip:"Wallbox IP address", wb2_ip:"Wallbox 2 IP address", save:"Save", back:"← Back to dashboard",
    wb_pwd:"Wallbox dongle password", wb2_pwd:"Wallbox 2 dongle password",
    wb_section1:"Wallbox 1", wb_section2:"Wallbox 2",
    wb2_note:"If you don't have a second wallbox, leave these fields empty.",
    battery_info_title:"Battery", battery_info_h:"Battery information",
    battery_power:"Battery power", battery_soc:"SOC", battery_voltage:"Battery voltage", battery_temp:"Battery temperature",
    battery_capacity:"Charge", cell_voltage:"Cell voltage (min–max)", cell_temp:"Cell temperature (min–max)",
    bat_charge:"Charged (today / total)", bat_discharge:"Discharged (today / total)",
    run_modes:{0:"Waiting",1:"Checking",2:"Normal",3:"Fault",4:"Permanent fault",5:"Updating",6:"EPS check",7:"EPS mode",8:"Self-test",9:"Idle",10:"Standby"},
    buy_kwh:"⚡ Buy me a kWh", support_note:"Support further development — thanks!",
    about_p1:"Local overview of a SolaX X3 Hybrid G4 solar system. Shows live data from the inverter (and optionally a wallbox) read over the local network via the dongle's local HTTP API.",
    about_p2:"Data is read directly from your device on the network and is not sent anywhere.",
    err_read:"Cannot read from the inverter — check the IP address", open_settings:"⚙️ Open settings",
    unsupported_model:"This inverter (type {type}) is not supported yet. The app is built for the SolaX X3 Hybrid G4.",
    saved_ok:"Saved ✓ (applies within 5 s)", conn_err:"Connection error",
    bad_inverter_ip:"Invalid inverter IP address",
    bad_wallbox_ip:"Enter a valid wallbox IP (or uncheck the wallbox option)",
    wb2_ip:"Wallbox 2 IP address",
    wb2_pwd:"Wallbox 2 dongle password",
    welcome:"Welcome! To get started, enter the inverter IP address and the dongle password (serial number), then save.",
    net_note:"Data readout and the chart only work when the app runs on the same network as the inverter."
  }
};

let LANG = "en";
let T = I18N.en;

function applyLang(lang){
    LANG = (I18N[lang] ? lang : "en");
    T = I18N[LANG];
    document.querySelectorAll("[data-i18n]").forEach(el => {
        const k = el.getAttribute("data-i18n");
        if(T[k] !== undefined) el.textContent = T[k];
    });
    const cs = document.getElementById("lang_cs");
    const en = document.getElementById("lang_en");
    if(cs && en){
        cs.classList.toggle("active", LANG === "cs");
        en.classList.toggle("active", LANG === "en");
    }
    if(typeof refresh === "function") refresh();
    if(typeof loadChart === "function") loadChart();
}

function setLang(lang){
    applyLang(lang);
}

function showSettings(){
    document.getElementById("error_overlay").style.display = "none";
    document.getElementById("main").style.display = "none";
    document.getElementById("about").style.display = "none";
    document.getElementById("settings").style.display = "block";
    document.getElementById("fab_group").style.display = "none";
    loadSettings();
}

function showAbout(){
    document.getElementById("error_overlay").style.display = "none";
    document.getElementById("main").style.display = "none";
    document.getElementById("settings").style.display = "none";
    document.getElementById("battery_info").style.display = "none";
    document.getElementById("about").style.display = "block";
    document.getElementById("fab_group").style.display = "none";
}

function showBattery(){
    document.getElementById("error_overlay").style.display = "none";
    document.getElementById("main").style.display = "none";
    document.getElementById("settings").style.display = "none";
    document.getElementById("about").style.display = "none";
    document.getElementById("battery_info").style.display = "block";
    document.getElementById("fab_group").style.display = "none";
    loadBatteryInfo();
}

async function loadBatteryInfo(){
    try{
        const r = await fetch("/api/live");
        const data = await r.json();
        if(data.error) return;
        document.getElementById("battery_info_power").innerText =
            (data.battery_power/1000).toFixed(2) + " kW";
        document.getElementById("battery_info_soc").innerText =
            data.soc + " %";
        document.getElementById("battery_info_capacity").innerText =
            data.battery_capacity + " kWh";
        document.getElementById("battery_info_voltage").innerText =
            data.battery_voltage + " V";
        document.getElementById("battery_info_temp").innerText =
            data.battery_temp + " °C";
        document.getElementById("battery_info_cellv").innerText =
            data.cell_volt_min + " – " + data.cell_volt_max + " V";
        document.getElementById("battery_info_cellt").innerText =
            data.cell_temp_min + " – " + data.cell_temp_max + " °C";
        document.getElementById("battery_info_charge").innerText =
            data.bat_charge_today + " / " + data.bat_charge_total + " kWh";
        document.getElementById("battery_info_discharge").innerText =
            data.bat_discharge_today + " / " + data.bat_discharge_total + " kWh";
    }catch(e){ }
}

function showMain(){
    document.getElementById("settings").style.display = "none";
    document.getElementById("about").style.display = "none";
    document.getElementById("battery_info").style.display = "none";
    document.getElementById("main").style.display = "block";
    document.getElementById("fab_group").style.display = "flex";
}

function toggleWallbox(){
    const on = document.getElementById("cfg_has_wallbox").checked;
    document.getElementById("cfg_wallbox").disabled = !on;
    document.getElementById("cfg_wallbox_label").classList.toggle("dim", !on);
    document.getElementById("cfg_wallbox_pwd").disabled = !on;
    document.getElementById("cfg_wallbox_pwd_label").classList.toggle("dim", !on);
    document.getElementById("cfg_wallbox2").disabled = !on;
    document.getElementById("cfg_wallbox2_label").classList.toggle("dim", !on);
    document.getElementById("cfg_wallbox2_pwd").disabled = !on;
    document.getElementById("cfg_wallbox2_pwd_label").classList.toggle("dim", !on);
}

async function loadSettings(){
    try{
        const r = await fetch("/api/settings");
        const c = await r.json();
        document.getElementById("cfg_solax").value = c.solax_ip;
        document.getElementById("cfg_dongle_pwd").value = c.dongle_pwd || "";
        document.getElementById("cfg_wallbox").value = c.wallbox_ip;
        document.getElementById("cfg_wallbox2").value = c.wallbox2_ip || "";
        document.getElementById("cfg_wallbox_pwd").value = c.wallbox_pwd || "";
        document.getElementById("cfg_wallbox2_pwd").value = c.wallbox2_pwd || "";
        document.getElementById("cfg_has_wallbox").checked = !!c.has_wallbox;
        document.getElementById("lang_cs").classList.toggle("active", LANG === "cs");
        document.getElementById("lang_en").classList.toggle("active", LANG === "en");
        toggleWallbox();
        document.getElementById("save_msg").innerText = "";
    }catch(e){}
}

async function saveSettings(){

    const msg = document.getElementById("save_msg");

    const body = {
        solax_ip:
            document.getElementById("cfg_solax").value.trim(),
        dongle_pwd:
            document.getElementById("cfg_dongle_pwd").value.trim(),
        wallbox_ip:
            document.getElementById("cfg_wallbox").value.trim(),
        wallbox2_ip:
            document.getElementById("cfg_wallbox2").value.trim(),
        wallbox_pwd:
            document.getElementById("cfg_wallbox_pwd").value.trim(),
        wallbox2_pwd:
            document.getElementById("cfg_wallbox2_pwd").value.trim(),
        has_wallbox:
            document.getElementById("cfg_has_wallbox").checked,
        lang: LANG
    };

    try{
        const r = await fetch("/api/settings", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body)
        });

        const res = await r.json();

        if(res.ok){
            msg.style.color = "#4ade80";
            msg.innerText = T.saved_ok;
            const wn = document.getElementById("welcome_note");
            const wasWelcome = wn && wn.style.display !== "none";
            if(wn) wn.style.display = "none";
            // pri prvnim spusteni (uvodni obrazovka) po ulozeni rovnou na prehled
            if(wasWelcome){
                setTimeout(showMain, 700);
            }
        }else{
            msg.style.color = "#f87171";
            msg.innerText = (res.code && T[res.code]) ? T[res.code] : (res.error || "Error");
        }
    }catch(e){
        msg.style.color = "#f87171";
        msg.innerText = T.conn_err;
    }
}

function setDonut(seg1, seg2, frac1){
    const C = 2 * Math.PI * 42;
    const f1 = Math.max(0, Math.min(1, frac1));
    const f2 = 1 - f1;

    const s1 = document.getElementById(seg1);
    const s2 = document.getElementById(seg2);

    // maly odstup mezi segmenty kvuli zaoblenym koncum
    s1.setAttribute("stroke-dasharray", (f1 * C) + " " + C);
    s1.setAttribute("transform", "rotate(-90 50 50)");

    s2.setAttribute("stroke-dasharray", (f2 * C) + " " + C);
    s2.setAttribute("transform", "rotate(" + (-90 + f1 * 360) + " 50 50)");
}

let wb1LastOnline = Date.now();
let wb2LastOnline = Date.now();
let solaxLastOk = Date.now();

async function refresh(){

    const response =
        await fetch('/api/live');

    const data =
        await response.json();

    // --- chyba cteni z menice: po 6s ukaz hlasku pres obrazovku ---
    if(data.error){
        const settingsOpen =
            document.getElementById("settings").style.display === "block";
        const aboutOpen =
            document.getElementById("about").style.display === "block";
        // nepodporovany model -> jasna hlaska misto "zkontroluj IP"
        const msgEl = document.querySelector("#error_overlay .msg");
        if(data.error_code === "unsupported_model"){
            msgEl.innerText = (T["unsupported_model"] || "")
                .replace("{type}", data.model_type);
        } else {
            msgEl.innerText = T["err_read"];
        }
        if(!settingsOpen && !aboutOpen
                && Date.now() - solaxLastOk > 6000){
            document.getElementById("error_overlay").style.display = "flex";
        }
        return;
    }
    solaxLastOk = Date.now();
    document.getElementById("error_overlay").style.display = "none";

    document.getElementById("pv").innerText =
        (data.pv_total/1000).toFixed(2) + " kW";

    document.getElementById("battery").innerText =
        (data.battery_power/1000).toFixed(2) + " kW";

    document.getElementById("soc").innerText =
        data.soc + " %";

    document.getElementById("load").innerText =
        (data.load_power/1000).toFixed(2) + " kW";

    document.getElementById("grid").innerText =
        (Math.abs(data.grid_power)/1000).toFixed(2) + " kW";
    document.getElementById("grid_label").innerText =
        data.grid_power >= 0 ? T.grid_export : T.grid_import;

    document.getElementById("versions").innerText =
        "DSP " + data.dsp_version + " · ARM " + data.arm_version;

    const modeMap = T.run_modes || {};
    document.getElementById("inv_mode").innerText =
        (modeMap[data.run_mode] !== undefined ? modeMap[data.run_mode]
                                              : ("Mode " + data.run_mode));

    document.getElementById("pv1").innerText =
        data.pv1_power + " W";

    document.getElementById("pv2").innerText =
        data.pv2_power + " W";

    document.getElementById("innertemp").innerText =
        data.inner_temp + " °C";

    document.getElementById("temp").innerText =
        data.temperature + " °C";

    document.getElementById("l1v").innerText =
        data.l1_voltage + " V";

    document.getElementById("l2v").innerText =
        data.l2_voltage + " V";

    document.getElementById("l3v").innerText =
        data.l3_voltage + " V";

    document.getElementById("l1info").innerText =
        data.l1_current + " A / " +
        data.l1_power + " W";

    document.getElementById("l2info").innerText =
        data.l2_current + " A / " +
        data.l2_power + " W";

    document.getElementById("l3info").innerText =
        data.l3_current + " A / " +
        data.l3_power + " W";

    // --- wallbox ---
    const wb1Node = document.getElementById("wb1_node");
    const wb2Node = document.getElementById("wb2_node");
    if(!data.wb1_configured){
        wb1Node.style.display = "none";
    } else {
        wb1Node.style.display = "";
        if(data.wb1_online){
            wb1LastOnline = Date.now();
            document.getElementById("wb1flow").innerText =
                (data.wb1_power / 1000).toFixed(2) + " kW";
        } else if(Date.now() - wb1LastOnline > 6000){
            document.getElementById("wb1flow").innerText = "n/a";
        }
    }

    if(!data.wb2_configured){
        wb2Node.style.display = "none";
    } else {
        wb2Node.style.display = "";
        if(data.wb2_online){
            wb2LastOnline = Date.now();
            document.getElementById("wb2flow").innerText =
                (data.wb2_power / 1000).toFixed(2) + " kW";
        } else if(Date.now() - wb2LastOnline > 6000){
            document.getElementById("wb2flow").innerText = "n/a";
        }
    }

    // --- denni energie (prstence) ---

    const out = data.output_today;
    const exp = data.export_today;
    const imp = data.import_today;

    let toHome = out - exp;
    if(toHome < 0) toHome = 0;

    const cons = toHome + imp;

    // prstenec 1: Vyroba mevice (do domu / do site)
    document.getElementById("out_total").innerText =
        out.toFixed(2);
    document.getElementById("out_home_val").innerText =
        toHome.toFixed(2) + " kWh";
    document.getElementById("out_grid_val").innerText =
        exp.toFixed(2) + " kWh";

    const homeFrac = out > 0 ? toHome / out : 0;
    document.getElementById("out_home_pct").innerText =
        "(" + (homeFrac * 100).toFixed(1) + " %)";
    document.getElementById("out_grid_pct").innerText =
        "(" + ((1 - homeFrac) * 100).toFixed(1) + " %)";

    setDonut("out_seg_home", "out_seg_grid", homeFrac);

    // prstenec 2: Spotreba (ze systemu / ze site)
    document.getElementById("con_total").innerText =
        cons.toFixed(2);
    document.getElementById("con_sys_val").innerText =
        toHome.toFixed(2) + " kWh";
    document.getElementById("con_grid_val").innerText =
        imp.toFixed(2) + " kWh";

    const sysFrac = cons > 0 ? toHome / cons : 0;
    document.getElementById("con_sys_pct").innerText =
        "(" + (sysFrac * 100).toFixed(1) + " %)";
    document.getElementById("con_grid_pct").innerText =
        "(" + ((1 - sysFrac) * 100).toFixed(1) + " %)";

    setDonut("con_seg_sys", "con_seg_grid", sysFrac);
}

refresh();

setInterval(
    refresh,
    5000
);

// ====== GRAF PRUBEHU DNE ======
// ====== GRAF PRUBEHU DNE ======
let chartPts = [];
const chartSeries = {
    pv:   {name:"Výroba",   color:"#fbbf24", on:true, soc:false, get:p=>p.pv},
    load: {name:"Spotřeba", color:"#f97316", on:true, soc:false, get:p=>p.load},
    grid: {name:"Síť",      color:"#22d3ee", on:true, soc:false, get:p=>p.grid},
    soc:  {name:"SOC",      color:"#a78bfa", on:true, soc:true,  get:p=>p.soc},
};
let chartScale = {minKw:0, maxKw:1};
const CH_W = 1000, CH_H = 320, CH_PT = 12, CH_PB = 16;

async function loadChart(){
    let data;
    try{
        const r = await fetch("/api/history?t=" + Date.now());
        data = await r.json();
    }catch(e){ return; }
    chartPts = (data && data.points) ? data.points : [];
    renderChart();
}

function toggleSeries(key){
    chartSeries[key].on = !chartSeries[key].on;
    const el = document.querySelector('.chart-legend .lg[data-k="'+key+'"]');
    if(el) el.classList.toggle("off", !chartSeries[key].on);
    renderChart();
}

function yKw(v){
    const {minKw, maxKw} = chartScale;
    return CH_PT + (1 - (v - minKw)/(maxKw - minKw)) * (CH_H - CH_PT - CH_PB);
}
function ySoc(v){
    return CH_PT + (1 - v/100) * (CH_H - CH_PT - CH_PB);
}
function yOf(key, p){
    return chartSeries[key].soc ? ySoc(chartSeries[key].get(p))
                                : yKw(chartSeries[key].get(p));
}

function drawAxes(){
    const {minKw, maxKw} = chartScale;
    const topPct = y => (y / CH_H) * 100;   // viewBox Y -> % vysky

    // leva osa: kW (dynamicky rozsah), krok tak, aby bylo ~5 popisku
    let left = "";
    const span = maxKw - minKw;
    let step = Math.max(1, Math.ceil(span / 5));
    const startV = Math.ceil(minKw / step) * step;
    // mezikroky (bez horniho maxima)
    for(let v = startV; v < maxKw - 0.001; v += step){
        left += `<div class="ytick" style="top:${topPct(yKw(v)).toFixed(1)}%">${v}</div>`;
    }
    // horni popisek vzdy presne na maximu, s jednotkou
    left += `<div class="ytick" style="top:${topPct(yKw(maxKw)).toFixed(1)}%">${maxKw} kW</div>`;
    document.getElementById("yaxis_left").innerHTML = left;

    // prava osa: SOC 0-100 %
    let right = "";
    [100, 75, 50, 25, 0].forEach(v => {
        const label = (v === 100 ? v + " %" : "" + v);
        right += `<div class="ytick" style="top:${topPct(ySoc(v)).toFixed(1)}%">${label}</div>`;
    });
    document.getElementById("yaxis_right").innerHTML = right;
}

function renderChart(){
    const empty = document.getElementById("chart_empty");
    const svg = document.getElementById("chart");
    const n = chartPts.length;

    if(n < 2){
        empty.style.display = "flex";
        svg.innerHTML = "";
        document.getElementById("chart_axis").innerHTML = "";
        document.getElementById("yaxis_left").innerHTML = "";
        document.getElementById("yaxis_right").innerHTML = "";
        return;
    }
    empty.style.display = "none";

    const x = i => (i/(n-1))*CH_W;

    // skala kW jen z VIDITELNYCH vykonovych krivek (po vypnuti se prepocita)
    let maxKw = 0, minKw = 0;
    chartPts.forEach(p => {
        ["pv","load","grid"].forEach(k => {
            if(chartSeries[k].on){
                const v = chartSeries[k].get(p);
                maxKw = Math.max(maxKw, v);
                minKw = Math.min(minKw, v, 0);
            }
        });
    });
    // dynamicke maximum: namerene max + 1 kW (rezerva nad krivkou)
    maxKw = Math.ceil(maxKw + 1);
    // spodek na cele kW dolu (jen kdyz je sit zaporna), at popisky nevylezou ven
    minKw = Math.floor(minKw);
    chartScale = {minKw, maxKw};

    drawAxes();

    const linePath = (key) => {
        let dd = "";
        chartPts.forEach((p,i) => {
            dd += (i===0 ? "M" : "L") + x(i).toFixed(1) + " " + yOf(key,p).toFixed(1) + " ";
        });
        const s = chartSeries[key];
        return `<path d="${dd}" fill="none" stroke="${s.color}" stroke-width="2.5" ${s.soc?'stroke-dasharray="4 4"':''} vector-effect="non-scaling-stroke"/>`;
    };

    // vodorovna 0 kdyz je grid zaporna
    let zeroLine = "";
    if(chartScale.minKw < 0){
        const Y0 = yKw(0).toFixed(1);
        zeroLine = `<line x1="0" y1="${Y0}" x2="${CH_W}" y2="${Y0}" stroke="#334155" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
    }

    let paths = zeroLine;
    ["pv","load","grid","soc"].forEach(k => {
        if(chartSeries[k].on) paths += linePath(k);
    });
    svg.innerHTML = paths;

    // casova osa
    const axis = document.getElementById("chart_axis");
    axis.innerHTML =
        `<span>${chartPts[0].t}</span><span>${chartPts[Math.floor(n/2)].t}</span><span>${chartPts[n-1].t}</span>`;
}

// ---- tooltip / svisla cara ----
(function setupChartHover(){
    const wrap = document.getElementById("chart_wrap");
    const vline = document.getElementById("chart_vline");
    const dots = document.getElementById("chart_dots");
    const tip = document.getElementById("chart_tip");

    function hide(){
        vline.style.display = "none";
        tip.style.display = "none";
        dots.innerHTML = "";
    }

    wrap.addEventListener("mouseleave", hide);
    wrap.addEventListener("mousemove", (e) => {
        const n = chartPts.length;
        if(n < 2){ hide(); return; }

        const rect = wrap.getBoundingClientRect();
        let frac = (e.clientX - rect.left)/rect.width;
        frac = Math.max(0, Math.min(1, frac));
        const idx = Math.round(frac*(n-1));
        const p = chartPts[idx];
        const px = (idx/(n-1))*rect.width;

        vline.style.display = "block";
        vline.style.left = px + "px";

        // body + obsah tooltipu jen pro zapnute krivky
        let dotsHtml = "";
        let rows = "";
        ["pv","load","grid","soc"].forEach(k => {
            const s = chartSeries[k];
            if(!s.on) return;
            const py = (yOf(k,p)/CH_H)*rect.height;
            dotsHtml += `<div class="dot" style="left:${px}px;top:${py}px;background:${s.color}"></div>`;
            const nameMap = {pv:T.production, load:T.consumption, grid:T.grid, soc:"SOC"};
            const val = s.soc ? (s.get(p) + " %") : (s.get(p).toFixed(2) + " kW");
            rows += `<div class="row"><i style="background:${s.color}"></i>${nameMap[k]}: <b>${val}</b></div>`;
        });
        dots.innerHTML = dotsHtml;
        tip.innerHTML = `<div class="tt">${p.t}</div>${rows}`;

        // umisteni tooltipu (aby nevylezl z pravého kraje)
        tip.style.display = "block";
        const tw = tip.offsetWidth;
        let left = px + 14;
        if(left + tw > rect.width) left = px - tw - 14;
        if(left < 0) left = 4;
        tip.style.left = left + "px";
        tip.style.top = "12px";
    });
})();

loadChart();
setInterval(loadChart, 15000);  // graf obnov co 15 s (data se sbiraji po minute)

// nacti ulozeny jazyk a aplikuj ho; pri prvnim spusteni otevri nastaveni
(async function initLang(){
    let firstRun = false;
    try{
        const r = await fetch("/api/settings");
        const c = await r.json();
        applyLang(c.lang || "en");
        firstRun = !!c.first_run;
    }catch(e){
        applyLang("en");
    }
    if(firstRun){
        showSettings();
        const wn = document.getElementById("welcome_note");
        if(wn) wn.style.display = "block";
    }
})();

</script>

</body>

</html>
"""


# spusti ctenare na pozadi (poller + zaznam historie), funguje i pres waitress import
start_background()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )