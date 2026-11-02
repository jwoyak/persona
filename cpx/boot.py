# Persona CPX - boot.py
# Adds a second USB serial port ("data") that Persona talks to, alongside the
# normal REPL console. Takes effect after a hard reset (press RESET once).
import usb_cdc

usb_cdc.enable(console=True, data=True)
