"""Reproduce videos convertidos a ASCII en la terminal, con su audio.

El audio se reproduce directamente desde el propio archivo de video (ffpyplayer),
y el video se sincroniza contra la posición real del audio: si la terminal se
atrasa, se saltan frames; si va adelantada, se espera.
"""

import os
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from ffpyplayer.player import MediaPlayer
from ffpyplayer.tools import set_loglevel

BASE_DIR = Path(__file__).resolve().parent
VIDEOS_DIR = BASE_DIR / "videos"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}

# Rampa de caracteres de oscuro a claro.
ASCII_RAMP = " .:-=+*#%@"
# Tabla de búsqueda: valor de gris (0-255) -> código ASCII del carácter.
# Reparte los 256 niveles en partes iguales entre los caracteres de la rampa.
_RAMP_CODES = np.frombuffer(ASCII_RAMP.encode("ascii"), dtype=np.uint8)
ASCII_LUT = _RAMP_CODES[(np.arange(256) * len(ASCII_RAMP)) >> 8]

# Un carácter de terminal es aprox. el doble de alto que de ancho.
CHAR_ASPECT = 2.0

# Compensación de latencia: un frame no aparece en pantalla en el instante en
# que se escribe. Primero hay que convertirlo y escribirlo, y luego la terminal
# tarda en pintarlo (más cuanto más pequeña la letra, porque hay más caracteres).
# Por eso se dibuja por adelantado el frame que corresponderá cuando aparezca.
DISPLAY_BASE_LATENCY = 0.030  # s que tarda la terminal en pintar un frame pequeño
PAINT_FACTOR = 3.0            # pintar tarda ~3 veces lo que la terminal tarda en aceptar el texto
SYNC_OFFSET = 0.0             # ajuste manual en s: + adelanta la imagen, - la retrasa
MAX_LEAD = 0.3                # límite de la compensación, por seguridad
EMA_ALPHA = 0.1               # suavizado de las mediciones de tiempo

# Secuencias ANSI
ALT_SCREEN_ON = "\033[?1049h"   # pantalla alternativa (no ensucia el historial)
ALT_SCREEN_OFF = "\033[?1049l"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_SCREEN = "\033[2J"


def enable_ansi_on_windows():
    """Activa el procesamiento de secuencias ANSI en la consola de Windows."""
    if os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VIRTUAL_TERMINAL_PROCESSING


def fit_size(width, height, cols, rows):
    """Tamaño en caracteres que cabe en cols x rows manteniendo la proporción."""
    out_w = cols
    out_h = int(height / width * cols / CHAR_ASPECT)
    if out_h > rows:  # video muy alto (p. ej. vertical): limitar por el alto
        out_h = rows
        out_w = int(width / height * rows * CHAR_ASPECT)
    return max(1, out_w), max(1, out_h)


def image_to_ascii(gray, pad_left=0):
    """Convierte una imagen en escala de grises (ya redimensionada) a texto ASCII.

    Todo se hace con numpy: la LUT traduce cada píxel a su carácter y se agrega
    una columna de saltos de línea, así no hay bucles de Python por píxel.
    """
    rows, cols = gray.shape
    buf = np.empty((rows, pad_left + cols + 1), dtype=np.uint8)
    buf[:, :pad_left] = ord(" ")
    buf[:, pad_left:-1] = ASCII_LUT[gray]
    buf[:, -1] = ord("\n")
    return buf.tobytes()[:-1].decode("ascii")  # sin el último salto de línea


class AudioClock:
    """Reloj maestro basado en la posición real del audio.

    ffpyplayer actualiza la posición por bloques, así que entre actualizaciones
    se extrapola con el reloj del sistema. Si el audio termina antes que el
    video, el reloj sigue avanzando y el video no se congela.
    """

    def __init__(self, player):
        self.player = player
        self.last_pts = 0.0
        self.last_wall = time.perf_counter()

    def now(self):
        wall = time.perf_counter()
        pts = self.player.get_pts()
        if pts and pts == pts and pts != self.last_pts:  # válido, no NaN, avanzó
            self.last_pts, self.last_wall = pts, wall
            return pts
        return self.last_pts + (wall - self.last_wall)


def open_audio(video_path):
    """Abre solo la pista de audio del video, en pausa hasta que esté lista."""
    player = MediaPlayer(str(video_path), ff_opts={"vn": True, "sn": True, "paused": True})
    deadline = time.perf_counter() + 3
    while not player.get_metadata().get("duration") and time.perf_counter() < deadline:
        time.sleep(0.01)
    return player


def play_video(video_path):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"No se pudo abrir el video: {video_path}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 30  # fallback si OpenCV devuelve 0
    src_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    src_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)

    player = open_audio(video_path)
    out = sys.stdout
    out.write(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR_SCREEN)
    out.flush()

    last_term_size = None
    layout = None
    next_frame = 0  # índice del siguiente frame por leer
    work_time = 0.0   # promedio: convertir + escribir un frame (s)
    accept_time = 0.0  # promedio: lo que tarda la terminal en aceptar el texto (s)

    try:
        player.set_pause(False)
        clock = AudioClock(player)

        while True:
            loop_start = time.perf_counter()
            # Tiempo hasta que el frame se vea en pantalla.
            lead = work_time + DISPLAY_BASE_LATENCY + PAINT_FACTOR * accept_time + SYNC_OFFSET
            lead = max(-MAX_LEAD, min(lead, MAX_LEAD))
            # Frame que corresponderá al audio cuando este frame aparezca.
            target = int((clock.now() + lead) * fps)

            if target < next_frame:
                # Vamos adelantados al audio: esperar al siguiente frame
                # (sleep en vez de espera activa para no quemar CPU).
                wait = next_frame / fps - (clock.now() + lead)
                time.sleep(min(max(wait, 0.001), 0.05))
                continue

            # Vamos atrasados: saltar frames sin convertirlos ni dibujarlos.
            while next_frame < target and cap.grab():
                next_frame += 1

            ret, frame = cap.read()
            if not ret:
                break
            next_frame += 1

            term_size = shutil.get_terminal_size(fallback=(120, 40))
            if term_size != last_term_size:
                # Recalcular tamaño/centrado solo si cambió la terminal,
                # y limpiar para no dejar restos del frame anterior.
                last_term_size = term_size
                cols = max(1, term_size.columns - 1)  # -1 evita el salto de línea automático
                rows = max(1, term_size.lines)
                out_w, out_h = fit_size(src_w or frame.shape[1], src_h or frame.shape[0], cols, rows)
                pad_left = (cols - out_w) // 2
                top = (rows - out_h) // 2
                layout = (out_w, out_h, pad_left, f"\033[{top + 1};1H")
                out.write(CLEAR_SCREEN)

            out_w, out_h, pad_left, move_cursor = layout
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # INTER_AREA promedia los píxeles: reduce mucho mejor que el lineal.
            small = cv2.resize(gray, (out_w, out_h), interpolation=cv2.INTER_AREA)

            text = move_cursor + image_to_ascii(small, pad_left)
            write_start = time.perf_counter()
            out.write(text)
            out.flush()
            done = time.perf_counter()

            # Promedios móviles para ajustar la compensación al tamaño de la terminal.
            accept_time += EMA_ALPHA * ((done - write_start) - accept_time)
            work_time += EMA_ALPHA * ((done - loop_start) - work_time)
    except KeyboardInterrupt:
        pass  # Ctrl+C: salir limpio
    finally:
        player.close_player()
        cap.release()
        out.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        out.flush()


def list_videos():
    if not VIDEOS_DIR.is_dir():
        return []
    return sorted(
        (p for p in VIDEOS_DIR.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p: p.name.lower(),
    )


def choose_video(videos):
    print("Disponibles:")
    for n, video in enumerate(videos):
        print(f" {n}. {video.stem}")

    while True:
        choice = input("Seleccione un video (q para salir): ").strip()
        if choice.lower() == "q":
            return None
        if choice.isdigit() and int(choice) < len(videos):
            return videos[int(choice)]
        print(f"Opción inválida, escriba un número entre 0 y {len(videos) - 1}.")


def main():
    enable_ansi_on_windows()
    set_loglevel("quiet")  # que ffmpeg no escriba mensajes encima del video

    videos = list_videos()
    if not videos:
        print(f"No hay videos en {VIDEOS_DIR}")
        return

    try:
        video = choose_video(videos)
    except (KeyboardInterrupt, EOFError):
        print()
        return
    if video is None:
        return

    play_video(video)

    print("Reproducción terminada.")
    time.sleep(1)
    print("By LudwingJassir y muchos repos de Github.")


if __name__ == "__main__":
    main()
