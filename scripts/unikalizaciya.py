"""Обрабатывает готовый ролик и записывает новый MP4 с очищенными метаданными.

Только стандартная библиотека Python 3.9+ и ffmpeg с libx264/AAC.
Пример: python3 unikalizaciya.py input.MOV out.mp4 --profil myagko
Не определяет, сочтёт ли Instagram результат оригинальным контентом.
"""

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path


# Кроп — суммарная доля удаляемой ширины И высоты, а не процент с каждой стороны.
# Скорость и высота тона задаются независимо; зерно меняется с seed.
PROFILI = {
    "myagko": dict(crop=0.04, angle=0.25, speed=1.01, pitch=1.005,
                   brightness=0.003, contrast=1.008, saturation=1.01, hue=0.2,
                   noise=1, highpass=25, lowpass=19000, audio_bitrate="192k", crf=18),
    "sredne": dict(crop=0.08, angle=-0.6, speed=0.98, pitch=0.99,
                   brightness=-0.005, contrast=1.02, saturation=1.025, hue=-0.5,
                   noise=2, highpass=30, lowpass=18500, audio_bitrate="192k", crf=18),
}
LIMITS = dict(crop=(0, 0.08), angle=(-0.6, 0.6), speed=(0.98, 1.02), pitch=(0.99, 1.01))
FPS_CHANGE_LIMIT = Fraction(1, 1000)


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        # Не печатаем весь заголовок исходника: в нём могут быть геометки.
        errors = [line for line in result.stderr.splitlines()
                  if any(word in line.lower() for word in
                         ("error", "invalid", "failed", "cannot", "already exists"))]
        raise RuntimeError("Ошибка FFmpeg: " + "\n".join(errors[-8:]))
    return result


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def packet_ends(path):
    """Время конца каждой дорожки; учитываем PTS при перестановке B-кадров."""
    result = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                  "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "framecrc", "-"])
    timebases, types, ends = {}, {}, {}
    for line in result.stdout.splitlines():
        tb = re.match(r"#tb (\d+): (\d+/\d+)", line)
        kind = re.match(r"#media_type (\d+): (\w+)", line)
        if tb:
            timebases[int(tb.group(1))] = float(Fraction(tb.group(2)))
        elif kind:
            types[int(kind.group(1))] = kind.group(2)
        elif line and not line.startswith("#"):
            fields = line.split(",")
            index, pts, duration = int(fields[0]), int(fields[2]), int(fields[3])
            ends[index] = max(ends.get(index, 0), (pts + duration) * timebases[index])
    if "video" not in types.values() or not ends:
        raise ValueError("Не удалось проверить таймкоды дорожек")
    return {types[index]: round(end, 6) for index, end in ends.items()}


def probe(path):
    """Минимальная проверка без зависимости от отдельно установленного ffprobe.

    Возвращает только технические поля, не исходные значения метаданных.
    Проверка полного декодирования после сборки не зависит от разбора заголовка.
    """
    result = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True)
    header = result.stderr
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", header)
    video = re.search(r"^\s*Stream #\S+.*?: Video: (.+)$", header, re.M)
    if not duration or not video:
        raise ValueError("Не удалось прочитать длительность и видеодорожку: " + str(path))
    video_line = video.group(1)
    size = re.search(r"(?:^|, )([1-9]\d*)x([1-9]\d*)(?:[ ,\[]|$)", video_line)
    fps = re.search(r"([\d.]+) fps", video_line)
    audio = re.search(r"^\s*Stream #\S+.*?: Audio: (.+)$", header, re.M)
    sr = re.search(r"(\d+) Hz", audio.group(1)) if audio else None
    if not size or not fps or (audio and not sr):
        raise ValueError("Не удалось прочитать размеры, fps или частоту аудио: " + str(path))
    width, height = map(int, size.groups())
    rotation = re.search(r"rotation of (-?[\d.]+) degrees", header)
    degrees = float(rotation.group(1)) if rotation else 0.0
    if abs(degrees % 180 - 90) < 0.1:
        width, height = height, width
    elif abs(degrees % 180) > 0.1:
        raise ValueError("Поворот исходника не кратен 90°: требуется отдельная проверка геометрии")
    hours, minutes, seconds = duration.groups()
    forbidden = re.findall(
        r"^\s*((?:location[^ :]*|com\.apple\.quicktime\.[^ :]*|make|model))\s*:",
        header, re.M | re.I)
    sar = re.search(r"\bSAR (\d+):(\d+)", video_line)
    return dict(duration=int(hours) * 3600 + int(minutes) * 60 + float(seconds),
                width=width, height=height, fps=float(fps.group(1)),
                video_codec=video_line.split()[0], has_audio=bool(audio),
                sample_rate=int(sr.group(1)) if sr else None,
                audio_codec=audio.group(1).split()[0] if audio else None,
                hdr=any(tag in video_line.lower() for tag in
                        ("bt2020", "smpte2084", "arib-std-b67")),
                square_pixels=not sar or sar.group(1) == sar.group(2),
                sensitive_metadata_keys=forbidden,
                variant_tag=bool(re.search(r"^\s*comment\s*:\s*hermes-variant-", header, re.M)))


def parameters(args, source_fps):
    params = dict(PROFILI[args.profil])
    overrides = {"crop": args.crop, "angle": args.angle, "speed": args.speed,
                 "pitch": args.pitch}
    for key, value in overrides.items():
        if value is not None:
            params[key] = value
    for key, (lo, hi) in LIMITS.items():
        if not lo <= params[key] <= hi:
            raise ValueError(f"--{key}: допустимы значения от {lo} до {hi} (предел среднего профиля)")
    fps = args.fps
    if fps is None:
        fps = "30000/1001" if source_fps == 30 else "source"
    if fps != "source":
        try:
            requested_fps = Fraction(fps)
        except (ValueError, ZeroDivisionError):
            raise ValueError("--fps: укажи число, дробь или source") from None
        original_fps = Fraction(str(source_fps))
        if abs(requested_fps - original_fps) > original_fps * FPS_CHANGE_LIMIT:
            raise ValueError(f"--fps: допустимо отклонение до 0,1% от исходных {source_fps:g} к/с")
    params["fps"] = fps
    params["seed"] = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
    if not 0 <= params["seed"] < 2**31:
        raise ValueError("seed должен быть целым числом от 0 до 2147483647")
    return params


def filters(info, params):
    width, height = info["width"] // 2 * 2, info["height"] // 2 * 2
    cw = int(info["width"] * (1 - params["crop"])) // 2 * 2
    ch = int(info["height"] * (1 - params["crop"])) // 2 * 2
    if min(cw, ch) < 2:
        raise ValueError("Слишком маленький кадр")
    # Проверяем углы центрального кропа в повёрнутом изображении.
    # Это не даёт чёрным треугольникам попасть в результат.
    radians = math.radians(params["angle"])
    co, si = abs(math.cos(radians)), abs(math.sin(radians))
    margin = 2 if abs(radians) > 1e-8 else 0
    if cw * co + ch * si > info["width"] - margin or ch * co + cw * si > info["height"] - margin:
        raise ValueError("Кропа недостаточно для поворота без чёрных углов; увеличь --crop")
    fps = params["fps"] if params["fps"] != "source" else str(info["fps"])
    vf = ("setpts=(PTS-STARTPTS)/{speed},"
          "rotate={angle}*PI/180:ow=iw:oh=ih:c=black,"
          "crop={cw}:{ch},scale={w}:{h}:flags=lanczos,setsar=1,"
          "eq=brightness={brightness}:contrast={contrast}:saturation={saturation},"
          "hue=h={hue},noise=alls={noise}:allf=t+u:all_seed={seed},"
          "fps={fps}:round=near,format=yuv420p").format(cw=cw, ch=ch, w=width, h=height,
                                                       fps=fps, **{k: v for k, v in params.items() if k != "fps"})
    af = None
    if info["has_audio"]:
        # asetrate меняет и тон, и темп; atempo компенсирует темп до скорости видео.
        changed_rate = round(info["sample_rate"] * params["pitch"])
        actual_pitch = changed_rate / info["sample_rate"]
        lowpass = min(params["lowpass"], int(info["sample_rate"] * 0.45))
        af = ("asetpts=PTS-STARTPTS,asetrate={sr},aresample=48000,atempo={tempo:.10f},"
              "highpass=f={hp},lowpass=f={lp}").format(
                  sr=changed_rate, tempo=params["speed"] / actual_pitch,
                  hp=params["highpass"], lp=lowpass)
    return vf, af, (width, height)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profil", choices=PROFILI, default="myagko")
    parser.add_argument("--crop", type=float, help="удаляемая доля ширины и высоты, от 0 до 0.08")
    parser.add_argument("--angle", type=float, help="поворот от -0.6 до +0.6 градуса")
    parser.add_argument("--speed", type=float, help="скорость от 0.98 до 1.02")
    parser.add_argument("--pitch", type=float, help="высота тона от 0.99 до 1.01")
    parser.add_argument("--fps", help="отклонение до 0.1%% от исходных fps или source. "
                                     "По умолчанию 29.97 для исходных 30 к/с, иначе source")
    parser.add_argument("--seed", type=int, help="зерно; сохрани значение для повторения обработки")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.error("Не найден ffmpeg")
    source, output = args.input.resolve(), args.output.resolve()
    report_path = output.with_suffix(".json")
    if not source.is_file():
        parser.error("Исходник не найден: " + str(source))
    if output.suffix.lower() != ".mp4":
        parser.error("Выход должен иметь расширение .mp4")
    if output == source or output.exists() or report_path.exists():
        parser.error("Выход или отчёт уже существует; укажи новое имя")
    info = probe(source)
    params = parameters(args, info["fps"])
    if info["hdr"]:
        parser.error("HDR требует согласованного преобразования в SDR; этот режим обрабатывает SDR")
    if not info["square_pixels"]:
        parser.error("Несовпадающий SAR требует отдельного расчёта геометрии")
    vf, af, size = filters(info, params)
    original_hash = sha256(source)
    variant = "hermes-variant-" + uuid.uuid4().hex
    created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(source),
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", str(params["crf"]),
           "-profile:v", "high", "-pix_fmt", "yuv420p", "-tag:v", "avc1"]
    if af:
        cmd += ["-af", af, "-c:a", "aac", "-b:a", params["audio_bitrate"], "-ar", "48000"]
    cmd += ["-t", "{:.6f}".format(info["duration"] / params["speed"]),
            "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
            "-metadata", "comment=" + variant, "-metadata", "creation_time=" + created,
            "-movflags", "+faststart", str(output)]
    output.parent.mkdir(parents=True, exist_ok=True)
    print("Сборка {}: {}".format(args.profil, output.name), flush=True)
    run(cmd)
    result = probe(output)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-i", str(output),
         "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"])
    ends = packet_ends(output)
    source_unchanged = sha256(source) == original_hash
    expected_duration = info["duration"] / params["speed"]
    output_hash = sha256(output)
    checks = dict(full_decode=True, source_unchanged=source_unchanged,
                  file_hash_changed=output_hash != original_hash,
                  size_preserved=(result["width"], result["height"]) == size,
                  duration_ok=abs(result["duration"] - expected_duration) < 0.12,
                  av_ends_ok=not info["has_audio"] or
                  ("audio" in ends and abs(ends["video"] - ends["audio"]) < 0.12),
                  audio_presence_preserved=result["has_audio"] == info["has_audio"],
                  codecs_ok=result["video_codec"] == "h264" and
                  (not result["has_audio"] or result["audio_codec"] == "aac"),
                  no_sensitive_metadata=not result["sensitive_metadata_keys"],
                  variant_tag_present=result["variant_tag"])
    report = dict(source=str(source), output=str(output), source_sha256=original_hash,
                  output_sha256=output_hash, variant=variant, created_utc=created,
                  profile=args.profil, parameters=params, input_media=info, output_media=result,
                  expected_duration=expected_duration, track_ends=ends, checks=checks, command=cmd,
                  note="Проверка файла и изменений; не оценка оригинальности в Instagram.")
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if not all(checks.values()):
        raise RuntimeError("Не пройдены проверки: " + ", ".join(k for k, v in checks.items() if not v))
    print("Готово: {:.2f} с, {}×{}, {:.2f} МБ; проверки пройдены".format(
        result["duration"], *size, output.stat().st_size / 1024**2))
    print("Параметры и проверки: " + str(report_path))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        sys.exit(str(error))
