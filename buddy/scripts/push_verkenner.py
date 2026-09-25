#!/usr/bin/env python3
"""Compileer de verkenner naar .mpy en zet hem op de Cardputer.

Waarom .mpy: het toestel compileert een .py bij het importeren, en dat
kost tijdelijk veel meer RAM dan de module daarna nodig heeft. Gemeten op
2026-09-25: ``import verkenner_beeld`` (18 KB bron) liep vast op een
MemoryError met 46 KB vrij. Voorgecompileerde bytecode laadt zonder
parser.

- ``-march=xtensawin``: verkenner_exfat bevat een ``@micropython.viper``-
  functie, en die wordt machinecode voor de ESP32-S3.
- mpy-cross moet mpy v6.3 maken, net als de firmware (MicroPython 1.27):
  ``pip install --user mpy-cross==1.27.0.post2``.
- Een .py gaat bij importeren vóór een .mpy met dezelfde naam. Oude
  .py-versies op het toestel worden daarom verwijderd.

Na het uploaden controleert het script elk bestand met SHA-256 en reset het
toestel, tenzij je ``--no-reset`` meegeeft.

De bronnen staan bewust in ``buddy/verkenner/`` en niet in
``buddy/device/``: ``install_apps.py`` zet elke .py uit ``buddy/device``
op het toestel, en een .py gaat voor de .mpy. De verkenner compileert
op het toestel niet (MemoryError), dus hij heeft dit eigen pad, net als
``pager`` (push_pager_mpy.py).

Gebruik:
    python buddy/scripts/push_verkenner.py --port COM5
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

HIER = Path(__file__).resolve().parent
REPO = HIER.parent.parent
BRON = REPO / "buddy" / "verkenner"
sys.path.insert(0, str(REPO / ".claude" / "skills" / "m5-onboard" / "scripts"))

# (bron, doel op het toestel)
MODULES = [
    (BRON / "verkenner_ui.py", "/flash/verkenner_ui.mpy"),
    (BRON / "verkenner_bron.py", "/flash/verkenner_bron.mpy"),
    (BRON / "verkenner_exfat.py", "/flash/verkenner_exfat.mpy"),
    (BRON / "verkenner_fatvenster.py", "/flash/verkenner_fatvenster.mpy"),
    (BRON / "verkenner_exif.py", "/flash/verkenner_exif.mpy"),
    (BRON / "verkenner_beeld.py", "/flash/verkenner_beeld.mpy"),
    (BRON / "verkenner_tekst.py", "/flash/verkenner_tekst.mpy"),
    (BRON / "verkenner_info.py", "/flash/verkenner_info.mpy"),
    (BRON / "verkenner.py", "/flash/apps/verkenner.mpy"),
]


def compileer(bron: Path, uit: Path) -> None:
    try:
        import mpy_cross  # type: ignore
    except ImportError:
        sys.exit("mpy-cross ontbreekt: pip install --user mpy-cross==1.27.0.post2")
    rc = mpy_cross.run("-march=xtensawin", str(bron), "-o", str(uit)).wait()
    if rc != 0 or not uit.exists():
        sys.exit("mpy-cross faalde op {} (status {})".format(bron.name, rc))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", required=True)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    import install_apps  # type: ignore
    import mpy_repl  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="verkenner-mpy-"))
    plan = []
    for bron, doel in MODULES:
        uit = tmp / (bron.stem + ".mpy")
        compileer(bron, uit)
        plan.append((uit, doel))
        print("gecompileerd: {:28s} {:6d} -> {:6d} bytes".format(
            bron.name, bron.stat().st_size, uit.stat().st_size))

    s = mpy_repl.open_port(args.port)
    try:
        mpy_repl.interrupt_to_repl(s)
        mpy_repl.drain(s, wait=0.3)
        for uit, doel in plan:
            print("uploaden: {} -> {}".format(uit.name, doel))
            for poging in (1, 2):
                try:
                    install_apps._upload_file(s, str(uit), doel)
                    break
                except RuntimeError as e:
                    if poging == 2:
                        raise
                    print("  opnieuw ({})".format(str(e).splitlines()[0]))
                    mpy_repl.interrupt_to_repl(s)
                    mpy_repl.drain(s, wait=0.5)

        # Oude .py-versies weg: die zouden bij het importeren voorgaan.
        oud = [doel[:-4] + ".py" for _u, doel in plan]
        script = (
            "import os\n"
            "for p in {!r}:\n"
            "    try:\n"
            "        os.remove(p)\n"
            "        print('WEG', p)\n"
            "    except OSError:\n"
            "        pass\n"
        ).format(oud)
        uit_tekst = install_apps._paste_or_raise(s, script, settle=0.5, what="oude .py weg")
        for regel in uit_tekst.splitlines():
            if regel.startswith("WEG"):
                print(regel.strip())

        # Controle: hash van elk bestand op het toestel.
        script = (
            "import hashlib, binascii\n"
            "for p in {!r}:\n"
            "    h = hashlib.sha256()\n"
            "    with open(p, 'rb') as f:\n"
            "        while True:\n"
            "            b = f.read(1024)\n"
            "            if not b:\n"
            "                break\n"
            "            h.update(b)\n"
            "    print('HASH', p, binascii.hexlify(h.digest()).decode())\n"
        ).format([doel for _u, doel in plan])
        uit_tekst = install_apps._paste_or_raise(s, script, settle=1.0, what="hashes")
        fout = 0
        for uit, doel in plan:
            verwacht = hashlib.sha256(uit.read_bytes()).hexdigest()
            ok = "HASH {} {}".format(doel, verwacht) in uit_tekst
            fout += not ok
            print("  {:32s} {}".format(doel, "OK" if ok else "AFWIJKEND"))
        if fout:
            print("{} bestand(en) wijken af; niet resetten".format(fout))
            return 1

        if not args.no_reset:
            print("toestel resetten...")
            mpy_repl.repl_reset(s)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
