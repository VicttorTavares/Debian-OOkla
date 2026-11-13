"""Ícone na bandeja do Windows (indica de forma visível que a tela está sendo transmitida)."""
from __future__ import annotations

import logging
import os
import subprocess

from PIL import Image, ImageDraw

from .config import APP_NAME, user_runtime_dir

log = logging.getLogger(__name__)


def _icon(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 14, 48, 50), radius=6, fill=(40, 40, 40, 255))
    d.polygon([(48, 24), (60, 16), (60, 48), (48, 40)], fill=(40, 40, 40, 255))
    d.ellipse((16, 22, 36, 42), fill=color)
    return img


ICON_IDLE = (150, 150, 150, 255)
ICON_ACTIVE = (220, 30, 30, 255)
ICON_PAUSED = (230, 170, 0, 255)


def run_tray(app) -> None:
    import pystray

    def icon_for():
        if app.paused:
            return _icon(ICON_PAUSED)
        return _icon(ICON_ACTIVE if app.activity.active else ICON_IDLE)

    def open_path(p):
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as exc:
            log.error("Nao foi possivel abrir %s: %s", p, exc)

    def on_config(_icon, _item):
        if app.cfg.path:
            subprocess.Popen(["notepad.exe", str(app.cfg.path)])

    def on_toggle_pause(_icon, _item):
        app.set_paused(not app.paused)

    def on_quit(icon, _item):
        icon.visible = False
        app.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: app.status_text(), None, enabled=False),
        pystray.MenuItem(lambda _i: f"ONVIF: porta {app.cfg.network.onvif_port} | RTSP: {app.cfg.network.rtsp_port}",
                         None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pausar transmissao", on_toggle_pause, checked=lambda _i: app.paused),
        pystray.MenuItem("Abrir configuracao", on_config),
        pystray.MenuItem("Abrir pasta de logs", lambda *_: open_path(user_runtime_dir() / "logs")),
        pystray.MenuItem("Abrir gravacoes locais", lambda *_: open_path(app.cfg.recording_dir),
                         visible=app.cfg.recording.enabled),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair", on_quit),
    )
    icon = pystray.Icon(APP_NAME, icon_for(), f"{APP_NAME} - {app.cfg.device_name}", menu)

    def refresh():
        icon.icon = icon_for()
        icon.title = f"{app.cfg.device_name}: {app.status_text()}"[:127]
        icon.update_menu()

    app.listeners.append(refresh)
    icon.run()
