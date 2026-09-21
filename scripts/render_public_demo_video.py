"""Assemble the public demo from real source footage and a real run capture."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


PROJECT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1280, 720
FONT = Path("C:/Windows/Fonts/NotoSansSC-VF.ttf")
FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")
TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, width: int, chosen: ImageFont.FreeTypeFont) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if draw.textlength(candidate, font=chosen) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def poster(source_frame: Path, output: Path) -> None:
    image = Image.open(source_frame).convert("RGB").resize((WIDTH, HEIGHT))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 430, WIDTH, HEIGHT), fill=(4, 15, 24, 220))
    draw.text((70, 470), "真人公开视频 → 可追溯知识", font=font(46, True), fill="#ffffff")
    draw.text((72, 550), "83 秒真实运行 · 真人原声 · 中文旁白", font=font(27), fill="#48e0bd")
    draw.text((72, 615), "Douyin Knowledge Ingest", font=font(20, True), fill="#aac0d0")
    Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB").save(output)


def result_frame(source_frame: Path, manifest: dict, output: Path) -> None:
    base = Image.open(source_frame).convert("RGB").resize((WIDTH, HEIGHT)).filter(ImageFilter.GaussianBlur(9))
    image = Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (4, 15, 24, 190)))
    draw = ImageDraw.Draw(image)
    draw.text((70, 58), "真实运行完成", font=font(24, True), fill="#48e0bd")
    draw.text((70, 104), "原始材料没有自动冒充知识", font=font(42, True), fill="#ffffff")
    conclusion = (
        "真人视频转录、原始片头帧 OCR 和人工文字进入同一套可追溯结构；"
        "只有经过明确审阅的来源，才会提升为 confirmed 知识笔记。"
    )
    chosen = font(28)
    draw.multiline_text(
        (74, 190), "\n".join(_wrap(draw, conclusion, 1080, chosen)),
        font=chosen, fill="#eef5f8", spacing=15,
    )
    y = 350
    for row in manifest["sources"]:
        label = f"{row['kind'].upper():5}  {row['source_id']}  {row['license']}"
        draw.rounded_rectangle((72, y, 1208, y + 62), radius=13, fill=(16, 43, 61, 235))
        draw.text((96, y + 16), label, font=font(19), fill="#b9ccda")
        y += 75
    draw.text((74, 620), "103 tests passed  ·  private_data_used: false", font=font(22, True), fill="#48e0bd")
    image.convert("RGB").save(output)


def _shift_timestamp(match: re.Match[str], offset_seconds: float) -> str:
    hours, minutes, seconds, millis = map(int, match.groups())
    value = timedelta(hours=hours, minutes=minutes, seconds=seconds, milliseconds=millis)
    total_ms = int((value + timedelta(seconds=offset_seconds)).total_seconds() * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def shift_subtitles(source: Path, target: Path, seconds: float) -> None:
    text = source.read_text(encoding="utf-8-sig")
    target.write_text(
        TIME_RE.sub(lambda match: _shift_timestamp(match, seconds), text),
        encoding="utf-8",
    )


def render(
    source_root: Path,
    terminal_capture: Path,
    narration: Path,
    subtitles: Path,
    output: Path,
) -> None:
    manifest = json.loads((source_root / "public-demo-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("private_data_used") is not False:
        raise ValueError("只允许渲染明确标记为不含私人数据的演示")
    source_video = source_root / "assets" / "public-demo" / "source-clip.mp4"
    if not source_video.is_file() or not terminal_capture.is_file():
        raise FileNotFoundError("缺少真人公开视频或真实运行录屏")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("找不到 ffmpeg")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="douyin-real-demo-edit-") as temp:
        temp_dir = Path(temp)
        source_frame = temp_dir / "source-frame.jpg"
        subprocess.run(
            [ffmpeg, "-y", "-ss", "8", "-i", str(source_video), "-frames:v", "1", str(source_frame)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        poster_path = output.with_name(output.stem + "-poster.png")
        poster(source_frame, poster_path)
        final_frame = temp_dir / "result.png"
        result_frame(source_frame, manifest, final_frame)
        shift_subtitles(subtitles, temp_dir / "shifted.srt", 10)

        video_filter = (
            "[0:v]trim=4.5:14.5,setpts=PTS-STARTPTS,scale=1280:720,format=yuv420p[intro];"
            "[1:v]trim=0:48,setpts=PTS-STARTPTS,crop=1160:680:0:0,"
            "scale=1228:720,pad=1280:720:26:0:#07131f,format=yuv420p[run];"
            "[0:v]trim=14.5:25,setpts=PTS-STARTPTS,scale=1280:720,format=yuv420p[source];"
            "[3:v]trim=duration=15,setpts=PTS-STARTPTS,scale=1280:720,format=yuv420p[result];"
            "[intro][run][source][result]concat=n=4:v=1:a=0[sequence];"
            "[sequence]subtitles=shifted.srt:force_style='FontName=Microsoft YaHei,FontSize=18,"
            "PrimaryColour=&H00FFFFFF,BackColour=&H90000000,BorderStyle=3,Outline=1,"
            "Shadow=0,MarginV=26,Alignment=2'[video];"
            "[0:a]atrim=4.5:14.5,asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11[original];"
            "[2:a]loudnorm=I=-16:TP=-1.5:LRA=11,adelay=10000:all=1,apad=whole_dur=83.5[voice];"
            "[original]apad=whole_dur=83.5[originalpad];"
            "[originalpad][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[audio]"
        )
        command = [
            ffmpeg, "-y", "-i", str(source_video), "-i", str(terminal_capture),
            "-i", str(narration), "-loop", "1", "-t", "15", "-i", str(final_frame),
            "-filter_complex", video_filter, "-map", "[video]", "-map", "[audio]",
            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", "83.5",
            str(output),
        ]
        subprocess.run(command, cwd=temp_dir, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="用真人素材、真实运行录屏和旁白渲染公开演示")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--terminal-capture", type=Path, required=True)
    parser.add_argument("--narration", type=Path, required=True)
    parser.add_argument("--subtitles", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path,
        default=PROJECT / "docs" / "assets" / "public-media-demo.mp4",
    )
    args = parser.parse_args()
    render(args.source_root, args.terminal_capture, args.narration, args.subtitles, args.output)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
