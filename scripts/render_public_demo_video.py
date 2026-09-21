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
WIDTH, HEIGHT = 1920, 1080
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
    draw.rectangle((0, 645, WIDTH, HEIGHT), fill=(4, 15, 24, 220))
    draw.text((105, 705), "真人公开视频 → 可追溯知识", font=font(69, True), fill="#ffffff")
    draw.text((108, 825), "66 秒真实运行 · 真人原声 · 中文旁白", font=font(40), fill="#48e0bd")
    draw.text((108, 930), "Douyin Knowledge Ingest", font=font(30, True), fill="#aac0d0")
    Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB").save(output)


def result_frame(source_frame: Path, manifest: dict, output: Path) -> None:
    base = Image.open(source_frame).convert("RGB").resize((WIDTH, HEIGHT)).filter(ImageFilter.GaussianBlur(9))
    image = Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (4, 15, 24, 190)))
    draw = ImageDraw.Draw(image)
    draw.text((105, 87), "真实运行完成", font=font(36, True), fill="#48e0bd")
    draw.text((105, 156), "原始材料没有自动冒充知识", font=font(63, True), fill="#ffffff")
    conclusion = (
        "真人视频转录、原始片头帧 OCR 和人工文字进入同一套可追溯结构；"
        "只有经过明确审阅的来源，才会提升为 confirmed 知识笔记。"
    )
    chosen = font(42)
    draw.multiline_text(
        (111, 285), "\n".join(_wrap(draw, conclusion, 1620, chosen)),
        font=chosen, fill="#eef5f8", spacing=22,
    )
    y = 525
    for row in manifest["sources"]:
        label = f"{row['kind'].upper():5}  {row['source_id']}  {row['license']}"
        draw.rounded_rectangle((108, y, 1812, y + 93), radius=20, fill=(16, 43, 61, 235))
        draw.text((144, y + 24), label, font=font(29), fill="#b9ccda")
        y += 112
    draw.text((111, 930), "103 tests passed  ·  private_data_used: false", font=font(33, True), fill="#48e0bd")
    image.convert("RGB").save(output)


def provenance_frame(source_frame: Path, manifest: dict, output: Path) -> None:
    base = Image.open(source_frame).convert("RGB").resize((WIDTH, HEIGHT)).filter(ImageFilter.GaussianBlur(12))
    image = Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (4, 15, 24, 215)))
    draw = ImageDraw.Draw(image)
    draw.text((105, 85), "TRACEABLE BY DEFAULT", font=font(30, True), fill="#48e0bd")
    draw.text((105, 145), "来源、许可、哈希和审阅状态一起留下", font=font(58, True), fill="#ffffff")
    y = 290
    for row in manifest["sources"]:
        digest = str(row["sha256"])[:16] + "…"
        label = f"{row['kind'].upper()}  ·  {row['source_id']}"
        detail = f"{row['license']}  ·  SHA-256 {digest}"
        draw.rounded_rectangle((105, y, 1815, y + 150), radius=24, fill=(16, 43, 61, 235))
        draw.text((145, y + 24), label, font=font(32, True), fill="#eef5f8")
        draw.text((145, y + 82), detail, font=font(26), fill="#9fb6c6")
        y += 175
    draw.text(
        (108, 905),
        "index.jsonl  ·  _source_packages/  ·  _审阅状态.jsonl  ·  notes/",
        font=font(27), fill="#48e0bd",
    )
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
        provenance = temp_dir / "provenance.png"
        provenance_frame(source_frame, manifest, provenance)
        final_frame = temp_dir / "result.png"
        result_frame(source_frame, manifest, final_frame)
        shift_subtitles(subtitles, temp_dir / "shifted.srt", 7)

        video_filter = (
            "[0:v]trim=4.5:11.5,setpts=PTS-STARTPTS,scale=1920:1080,format=yuv420p[intro];"
            "[3:v]trim=duration=3,setpts=PTS-STARTPTS,scale=1920:1080,format=yuv420p[title];"
            "[1:v]trim=0:4,setpts=PTS-STARTPTS,crop=1160:680:0:0,"
            "scale=1842:1080,pad=1920:1080:39:0:#07131f,format=yuv420p[runstart];"
            "[1:v]trim=4:34,setpts=(PTS-STARTPTS)/6,crop=1160:680:0:0,"
            "scale=1842:1080,pad=1920:1080:39:0:#07131f,format=yuv420p[runfast];"
            "[1:v]trim=34:47,setpts=PTS-STARTPTS,crop=1160:680:0:0,"
            "scale=1842:1080,pad=1920:1080:39:0:#07131f,format=yuv420p[runresult];"
            "[0:v]trim=11.5:19.5,setpts=PTS-STARTPTS,scale=1920:1080,format=yuv420p[source];"
            "[4:v]trim=duration=11,setpts=PTS-STARTPTS,scale=1920:1080,format=yuv420p[trace];"
            "[5:v]trim=duration=15,setpts=PTS-STARTPTS,scale=1920:1080,format=yuv420p[result];"
            "[intro][title][runstart][runfast][runresult][source][trace][result]"
            "concat=n=8:v=1:a=0[sequence];"
            "[sequence]subtitles=shifted.srt:force_style='FontName=Microsoft YaHei,FontSize=24,"
            "PrimaryColour=&H00FFFFFF,BackColour=&H90000000,BorderStyle=3,Outline=1,"
            "Shadow=0,MarginV=40,Alignment=2'[video];"
            "[0:a]atrim=4.5:11.5,asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11[original];"
            "[2:a]loudnorm=I=-16:TP=-1.5:LRA=11,adelay=7000:all=1,apad=whole_dur=66[voice];"
            "[original]apad=whole_dur=66[originalpad];"
            "[originalpad][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[audio]"
        )
        command = [
            ffmpeg, "-y", "-i", str(source_video), "-i", str(terminal_capture),
            "-i", str(narration),
            "-loop", "1", "-t", "3", "-i", str(poster_path),
            "-loop", "1", "-t", "11", "-i", str(provenance),
            "-loop", "1", "-t", "15", "-i", str(final_frame),
            "-filter_complex", video_filter, "-map", "[video]", "-map", "[audio]",
            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", "-t", "66",
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
