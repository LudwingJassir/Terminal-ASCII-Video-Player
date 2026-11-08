import os
import time
import threading
import sys

import cv2
import numpy as np
from playsound import playsound


# Convertir imagen a ASCII
def image_to_ascii(image, cmd_width, aspect_ratio_correction=2):
    height, width = image.shape
    aspect_ratio = height / width
    new_height = int(aspect_ratio * cmd_width / aspect_ratio_correction)
    resized_image = cv2.resize(image, (cmd_width, new_height))

    ascii_chars = " .:-=+*#%@"
    pixels = resized_image / 255.0 * (len(ascii_chars) - 1)
    ascii_str = '\n'.join(''.join(ascii_chars[int(pixel)] for pixel in row) for row in pixels)
    return ascii_str


def play_video(video_path, audio_path):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0:
        fps = 30  # fallback
    frame_time = 1 / fps

    # Iniciar audio en thread
    audio_thread = threading.Thread(target=playsound, args=(audio_path,), daemon=True)
    audio_thread.start()

    time.sleep(0.5)  # Dar tiempo al audio de iniciar

    # Ocultar cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    while cap.isOpened():
        start_time = time.perf_counter()

        ret, frame = cap.read()
        if not ret:
            break

        # Obtener ancho de consola en cada frame para adaptación en fullscreen
        cmd_width = os.get_terminal_size().columns - 1

        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        ascii_frame = image_to_ascii(gray_frame, cmd_width)

        sys.stdout.write("\033[H")  # Mover cursor al inicio sin limpiar pantalla completa
        sys.stdout.write(ascii_frame + "\n")
        sys.stdout.flush()

        while (time.perf_counter() - start_time) < frame_time:
            pass

    cap.release()

    # Mostrar cursor al final
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def main():
    print("Disponibles:")
    videos = os.listdir(path="videos")
    for n, item in enumerate(videos):
        print(f" {n}.", item.split(".")[0])
    idx = int(input("Seleccione un Video \n"))

    name = videos[idx]
    video_path = f"videos/{name}"
    audio_name = os.path.splitext(name)[0]
    audio_path = f"audios/{audio_name}.mp3"

    if not os.path.exists(audio_path):
        print("No se encontró el audio para este video.")
        return

    play_video(video_path, audio_path)

    os.system('cls')
    print("Reproducción terminada.")
    time.sleep(1)
    print("By LudwingJassir y muchos repos de Github.")


if __name__ == "__main__":
    main()
